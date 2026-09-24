#!/usr/bin/env python3
import threading
"""
openSUSE Workspace Downstream Sync Backend
Houses core logic for checking repo sync states, querying Gitea API for PRs,
and querying release-monitoring.org for upstream versions.
Shared between CLI and GUI frontends.
"""

import os
import sys
import json
import re
import subprocess
import concurrent.futures
import unicodedata
import configparser
import requests

try:
    import rpm
except ImportError:
    rpm = None

_active_processes_lock = threading.Lock()
_active_processes = []

def run_tracked(args, **kwargs):
    import subprocess
    check = kwargs.pop("check", False)
    timeout = kwargs.pop("timeout", None)
    input_data = kwargs.pop("input", None)
    with _active_processes_lock:
        if _active_processes is None:
            raise RuntimeError("Subprocess spawning blocked during teardown")
        if "capture_output" in kwargs and kwargs["capture_output"]:
            kwargs.pop("capture_output")
            kwargs["stdout"] = subprocess.PIPE
            kwargs["stderr"] = subprocess.PIPE

        p = subprocess.Popen(args, **kwargs)
        _active_processes.append(p)

    try:
        stdout, stderr = p.communicate(input=input_data, timeout=timeout)
        retcode = p.poll()
        if check and retcode:
            raise subprocess.CalledProcessError(retcode, args, output=stdout, stderr=stderr)

        class CompletedProcess:
            def __init__(self, args, returncode, stdout, stderr):
                self.args = args
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr
            def check_returncode(self):
                import subprocess
                if self.returncode:
                    raise subprocess.CalledProcessError(self.returncode, self.args, self.stdout, self.stderr)
        return CompletedProcess(args, retcode, stdout, stderr)
    finally:
        with _active_processes_lock:
            if _active_processes is not None and p in _active_processes:
                _active_processes.remove(p)

def terminate_all_subprocesses():
    global _active_processes
    with _active_processes_lock:
        if _active_processes is None:
            return
        procs = list(_active_processes)
        _active_processes = None

    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass

    for p in procs:
        try:
            p.wait(timeout=0.2)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


# ANSI Color Codes
GREEN = "\x1b[32m"
RED = "\x1b[31m"
YELLOW = "\x1b[33m"
CYAN = "\x1b[36m"
BOLD = "\x1b[1m"
RESET = "\x1b[0m"

def get_gitea_repo_name(repo_path, default_name):
    """
    Directly parses the local git configuration file in Python,
    avoiding any subprocess spawning overhead, to map disk submodule
    names to their actual Gitea repository names on src.opensuse.org.
    """
    try:
        git_path = os.path.join(repo_path, '.git')

        # Resolve actual git directory (handles submodules where .git is a file)
        if os.path.isfile(git_path):
            with open(git_path, 'r') as f:
                gitdir_line = f.read().strip()
            if gitdir_line.startswith('gitdir:'):
                gitdir = gitdir_line.split(':', 1)[1].strip()
                if not os.path.isabs(gitdir):
                    git_dir_path = os.path.abspath(os.path.join(repo_path, gitdir))
                else:
                    git_dir_path = gitdir
            else:
                return default_name
        elif os.path.isdir(git_path):
            git_dir_path = git_path
        else:
            return default_name

        config_path = os.path.join(git_dir_path, 'config')
        if os.path.exists(config_path):
            config = configparser.ConfigParser()
            config.read(config_path)

            # Extract origin remote URL
            remote_origin = 'remote "origin"'
            if remote_origin in config and 'url' in config[remote_origin]:
                url = config[remote_origin]['url'].strip()

                # Parse out repository name
                if url.endswith('.git'):
                    url = url[:-4]
                if url.endswith('/'):
                    url = url[:-1]
                parts = url.split('/')
                last_part = parts[-1]
                if ':' in last_part:
                    last_part = last_part.split(':')[-1]
                if last_part:
                    return last_part
    except Exception:
        pass
    return default_name

def get_gitea_owner_and_repo(repo_path, default_name):
    """
    Parses local git configuration to return (owner, repo_name) for Gitea.
    """
    owner = "GNOME"
    repo_name = default_name
    try:
        git_path = os.path.join(repo_path, '.git')
        if os.path.isfile(git_path):
            with open(git_path, 'r') as f:
                gitdir_line = f.read().strip()
            if gitdir_line.startswith('gitdir:'):
                gitdir = gitdir_line.split(':', 1)[1].strip()
                if not os.path.isabs(gitdir):
                    git_dir_path = os.path.abspath(os.path.join(repo_path, gitdir))
                else:
                    git_dir_path = gitdir
            else:
                return owner, repo_name
        elif os.path.isdir(git_path):
            git_dir_path = git_path
        else:
            return owner, repo_name

        config_path = os.path.join(git_dir_path, 'config')
        if os.path.exists(config_path):
            config = configparser.ConfigParser()
            config.read(config_path)

            remote_origin = 'remote "origin"'
            if remote_origin in config and 'url' in config[remote_origin]:
                url = config[remote_origin]['url'].strip()
                if url.endswith('.git'):
                    url = url[:-4]
                if url.endswith('/'):
                    url = url[:-1]

                # Replace colon with slash to simplify parsing ssh urls
                url = url.replace(':', '/')
                parts = [p for p in url.split('/') if p]
                if len(parts) >= 2:
                    repo_name = parts[-1]
                    owner = parts[-2]
    except Exception:
        pass
    return owner, repo_name

def check_repo_pr(repo_name, stable_branch="factory", unstable_branch="next", workspace_path="."):
    """
    Queries Gitea API to check if there is an open pull request from head to base branch.
    Returns (repo_name, pr_info) where pr_info indicates whether a matching PR is active.
    """
    repo_path = os.path.join(workspace_path, repo_name)
    owner, gitea_name = get_gitea_owner_and_repo(repo_path, repo_name)

    url = f"https://src.opensuse.org/api/v1/repos/{owner}/{gitea_name}/pulls?state=open"
    headers = {'User-Agent': 'curl/8.0.1'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, list):
                for pr in data:
                    base_ref = pr.get("base", {}).get("ref")
                    head_ref = pr.get("head", {}).get("ref")
                    if base_ref == stable_branch and head_ref == unstable_branch:
                        return repo_name, {
                            "has_pr": True,
                            "url": pr.get("html_url"),
                            "number": pr.get("number")
                        }
    except Exception:
        pass
    return repo_name, {
        "has_pr": False,
        "url": None,
        "number": None
    }

def check_repo_sync(repo_name, stable_branch="factory", unstable_branch="next", workspace_path="."):
    repo_path = os.path.join(workspace_path, repo_name)

    # 1. Fetch latest state from origin (src.opensuse.org/<devel_project>/<repo>)
    try:
        run_tracked(
            ['git', '-C', repo_path, 'fetch', '--quiet', 'origin'],
            check=True, capture_output=True
        )
    except subprocess.CalledProcessError:
        pass

    # 2. Check if origin/{stable_branch} exists locally
    try:
        run_tracked(
            ['git', '-C', repo_path, 'show-ref', '--verify', '--quiet', f'refs/remotes/origin/{stable_branch}'],
            check=True, capture_output=True
        )
        has_origin_stable = True
    except subprocess.CalledProcessError:
        has_origin_stable = False

    if not has_origin_stable:
        return repo_name, {
            "status": "error",
            "message": f"Missing origin/{stable_branch} branch"
        }

    # 3. Check if origin/{unstable_branch} exists locally
    has_origin_unstable = False
    if unstable_branch:
        try:
            run_tracked(
                ['git', '-C', repo_path, 'show-ref', '--verify', '--quiet', f'refs/remotes/origin/{unstable_branch}'],
                check=True, capture_output=True
            )
            has_origin_unstable = True
        except subprocess.CalledProcessError:
            has_origin_unstable = False

    # 4. Fetch from pool/repo_name.git {stable_branch} branch (src.opensuse.org/pool/<repo>)
    gitea_name = get_gitea_repo_name(repo_path, repo_name)
    pool_url = f"https://src.opensuse.org/pool/{gitea_name}.git"
    pool_status = "unknown"
    pool_ahead = 0
    pool_behind = 0

    try:
        run_tracked(
            ['git', '-C', repo_path, 'fetch', '--quiet', pool_url, stable_branch],
            check=True, capture_output=True, text=True
        )
        # Compare origin/{stable_branch} and FETCH_HEAD (pool/{stable_branch})
        res = run_tracked(
            ['git', '-C', repo_path, 'rev-list', '--left-right', '--count', f'origin/{stable_branch}...FETCH_HEAD'],
            check=True, capture_output=True, text=True
        )
        output = res.stdout.strip()
        parts = output.split()
        if len(parts) == 2:
            pool_ahead = int(parts[0])  # devel is ahead of pool (pending submissions)
            pool_behind = int(parts[1]) # devel is behind pool (needs catch up)
            if pool_ahead == 0 and pool_behind == 0:
                pool_status = "In Sync"
            elif pool_ahead > 0 and pool_behind > 0:
                pool_status = f"Diverged"
            elif pool_ahead > 0:
                pool_status = f"Ahead"
            else:
                pool_status = f"Behind"
        else:
            pool_status = "Error"
    except subprocess.CalledProcessError as e:
        stderr_lower = (e.stderr or "").lower()
        if "cannot find repository" in stderr_lower or "could not read from remote repository" in stderr_lower or "repository not found" in stderr_lower or "404" in stderr_lower:
            pool_status = "Not in Pool"
        elif f"couldn't find remote ref {stable_branch}" in stderr_lower or "no such ref" in stderr_lower or "fatal: couldn't find remote ref" in stderr_lower:
            pool_status = f"No {stable_branch} in Pool"
        else:
            pool_status = "Fetch failed"

    # 5. Compare devel/{stable_branch} and devel/{unstable_branch}
    next_status = "N/A"
    next_ahead = 0
    next_behind = 0

    if has_origin_unstable and unstable_branch:
        try:
            res_next = run_tracked(
                ['git', '-C', repo_path, 'rev-list', '--left-right', '--count', f'origin/{stable_branch}...origin/{unstable_branch}'],
                check=True, capture_output=True, text=True
            )
            output_next = res_next.stdout.strip()
            parts_next = output_next.split()
            if len(parts_next) == 2:
                next_behind = int(parts_next[0]) # stable is ahead of unstable (unstable needs catch up)
                next_ahead = int(parts_next[1])  # unstable is ahead of stable (unstable has additional development)
                if next_behind == 0 and next_ahead == 0:
                    next_status = "In Sync"
                elif next_behind > 0 and next_ahead > 0:
                    next_status = f"Diverged"
                elif next_behind > 0:
                    next_status = f"Behind"
                else:
                    next_status = f"Ahead (ok)"
            else:
                next_status = "Error"
        except subprocess.CalledProcessError as e:
            next_status = "Comparison failed"
    else:
        next_status = "No next branch"

    # Determine sync actions for daily run (e.g. pool update, submission update, or stable -> unstable merge)
    needs_action = (pool_behind > 0) or (pool_ahead > 0) or (next_behind > 0) or (pool_status in ('Fetch failed', 'Error')) or (next_status in ('Comparison failed', 'Error'))

    return repo_name, {
        "status": "success",
        "pool_status": pool_status,
        "pool_ahead": pool_ahead,
        "pool_behind": pool_behind,
        "next_status": next_status,
        "next_ahead": next_ahead,
        "next_behind": next_behind,
        "needs_action": needs_action,
    }

def clean_version(version_str, repo_name=None):
    """
    Normalize version string for comparison:
    Strips leading 'v' or 'V' prefix if immediately followed by a digit (e.g. 'v1.0.5' -> '1.0.5').
    """
    if not version_str:
        return ""

    cleaned = str(version_str).strip()

    # Strip leading 'v'/'V' prefix if immediately followed by a digit
    return re.sub(r'^[vV](?=\d)', '', cleaned)

def _pure_rpmvercmp(a, b):
    """
    Pure Python implementation of RPM's rpmvercmp algorithm for systems
    without native python3-rpm bindings.
    """
    if a == b:
        return 0
    i, j = 0, 0
    len_a, len_b = len(a), len(b)
    while i < len_a or j < len_b:
        # Handle tilde (~) which sorts before everything (even empty string)
        while (i < len_a and a[i] == "~") or (j < len_b and b[j] == "~"):
            if i < len_a and a[i] == "~" and (j >= len_b or b[j] != "~"):
                return -1
            if j < len_b and b[j] == "~" and (i >= len_a or a[i] != "~"):
                return 1
            i += 1
            j += 1

        # Handle caret (^) which sorts before all other chars EXCEPT empty string and tilde
        while (i < len_a and a[i] == "^") or (j < len_b and b[j] == "^"):
            if i < len_a and a[i] == "^" and (j >= len_b or b[j] != "^"):
                if j >= len_b:
                    return 1
                return -1
            if j < len_b and b[j] == "^" and (i >= len_a or a[i] != "^"):
                if i >= len_a:
                    return -1
                return 1
            i += 1
            j += 1

        # Skip non-alphanumeric separators
        while i < len_a and not a[i].isalnum() and a[i] not in "~^":
            i += 1
        while j < len_b and not b[j].isalnum() and b[j] not in "~^":
            j += 1

        if i >= len_a and j >= len_b:
            return 0
        if i >= len_a:
            return -1 if b[j] != "~" else 1
        if j >= len_b:
            return 1 if a[i] != "~" else -1

        # Extract next segment
        if a[i].isdigit():
            seg_a_start = i
            while i < len_a and a[i].isdigit():
                i += 1
            seg_a = a[seg_a_start:i]
            is_num_a = True
        else:
            seg_a_start = i
            while i < len_a and a[i].isalpha():
                i += 1
            seg_a = a[seg_a_start:i]
            is_num_a = False

        if b[j].isdigit():
            seg_b_start = j
            while j < len_b and b[j].isdigit():
                j += 1
            seg_b = b[seg_b_start:j]
            is_num_b = True
        else:
            seg_b_start = j
            while j < len_b and b[j].isalpha():
                j += 1
            seg_b = b[seg_b_start:j]
            is_num_b = False

        # Numeric segment always > alpha segment
        if is_num_a and not is_num_b:
            return 1
        if not is_num_a and is_num_b:
            return -1

        if is_num_a:
            val_a = int(seg_a)
            val_b = int(seg_b)
            if val_a != val_b:
                return 1 if val_a > val_b else -1
        else:
            if seg_a != seg_b:
                return 1 if seg_a > seg_b else -1

    return 0

def compare_versions(v1, v2, repo_name=None):
    """
    Compare two version strings using RPM version comparison semantics (rpmvercmp).
    Returns:
        1 if v1 > v2
        0 if v1 == v2
       -1 if v1 < v2
    """
    c1 = clean_version(v1, repo_name)
    c2 = clean_version(v2, repo_name)

    if c1 == c2:
        return 0
    if not c1:
        return -1
    if not c2:
        return 1

    if rpm is not None:
        try:
            res = rpm.labelCompare(("", c1, ""), ("", c2, ""))
            return 1 if res > 0 else (-1 if res < 0 else 0)
        except Exception:
            pass

    return _pure_rpmvercmp(c1, c2)

def is_version_newer(upstream, local, repo_name=None) -> bool:
    """
    Returns True if upstream version is semantically newer than local version.
    Returns False if either version is missing, empty, '—', or 'N/A'.
    """
    if not upstream or upstream in ("—", "N/A"):
        return False
    if not local or local in ("—", "N/A"):
        return False
    return compare_versions(upstream, local, repo_name) > 0

def is_version_equal(v1, v2, repo_name=None) -> bool:
    """
    Returns True if two versions are semantically equal under RPM rules
    (e.g. '1.0.5' and '1_0_5').
    """
    if not v1 or not v2 or v1 in ("—", "N/A") or v2 in ("—", "N/A"):
        return False
    return compare_versions(v1, v2, repo_name) == 0

def check_repo_version(repo_name, branch=None, stable_branch="factory", unstable_branch="next", workspace_path=".", ignored_unstable_versions=None):
    repo_path = os.path.join(workspace_path, repo_name)

    if ignored_unstable_versions is None:
        ignored_unstable_versions = {}

    ignored_ver = ignored_unstable_versions.get(repo_name)

    # 1. Fetch latest state from origin (src.opensuse.org/<devel_project>/<repo>)
    try:
        run_tracked(
            ['git', '-C', repo_path, 'fetch', '--quiet', 'origin'],
            check=True, capture_output=True
        )
    except subprocess.CalledProcessError:
        pass

    # 2. Find spec file locally
    spec_file = None
    try:
        for f in os.listdir(repo_path):
            if f.endswith('.spec'):
                spec_file = f
                break
    except Exception:
        pass

    if not spec_file:
        return repo_name, {
            "status": "error",
            "message": "No spec file found"
        }

    # 3. Get version from stable branch (if checking stable or both)
    factory_ver = None
    if branch is None or branch == stable_branch or branch == "factory":
        try:
            res = run_tracked(
                ['git', '-C', repo_path, 'show', f'refs/remotes/origin/{stable_branch}:{spec_file}'],
                check=True, capture_output=True, text=True
            )
            for line in res.stdout.splitlines():
                if line.strip().lower().startswith('version:'):
                    factory_ver = line.split(':', 1)[1].strip()
                    break
        except subprocess.CalledProcessError:
            pass

    # 4. Get version from unstable branch (if checking unstable or both)
    next_ver = None
    if unstable_branch and (branch is None or branch == unstable_branch or branch == "next"):
        try:
            res = run_tracked(
                ['git', '-C', repo_path, 'show', f'refs/remotes/origin/{unstable_branch}:{spec_file}'],
                check=True, capture_output=True, text=True
            )
            for line in res.stdout.splitlines():
                if line.strip().lower().startswith('version:'):
                    next_ver = line.split(':', 1)[1].strip()
                    break
        except subprocess.CalledProcessError:
            pass

    # 5. Query release-monitoring.org for upstream versions (spoofing curl User-Agent)
    upstream_stable = None
    upstream_latest = None
    exact_item = None

    url = f"https://release-monitoring.org/api/v2/packages/?name={repo_name}&distribution=openSUSE"
    headers = {'User-Agent': 'curl/8.0.1'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            items = data.get("items", [])
            if items:
                exact_item = None
                for item in items:
                    if item.get("name") == repo_name:
                        exact_item = item
                        break
                if not exact_item:
                    exact_item = items[0]

                upstream_stable = exact_item.get("stable_version")
                upstream_latest = exact_item.get("version")
    except Exception:
        # If API is unreachable or rate-limited, we report partial error but keep spec versions
        return repo_name, {
            "status": "partial_error",
            "message": "Upstream API error",
            "factory_ver": factory_ver or "N/A",
            "next_ver": next_ver or "—"
        }

    # Compare versions using semantic RPM rules: only trigger if upstream is newer than local
    needs_update = False

    if (branch is None or branch == stable_branch or branch == "factory") and factory_ver and upstream_stable:
        if is_version_newer(upstream_stable, factory_ver, repo_name):
            needs_update = True

    if unstable_branch and (branch is None or branch == unstable_branch or branch == "next") and next_ver and next_ver != "—" and upstream_latest:
        if is_version_newer(upstream_latest, next_ver, repo_name):
            # Check if this specific found unstable version is in our ignore list!
            is_ignored = bool(ignored_ver and is_version_equal(upstream_latest, ignored_ver, repo_name))
            if not is_ignored:
                needs_update = True

    return repo_name, {
        "status": "success",
        "factory_ver": factory_ver or "N/A",
        "next_ver": next_ver or "—",
        "upstream_stable": upstream_stable or "N/A",
        "upstream_latest": upstream_latest or "—",
        "needs_update": needs_update,
        "project": exact_item.get("project") if exact_item else repo_name
    }

def get_git_diff(repo_name, stable_branch="factory", unstable_branch="next", workspace_path="."):
    """
    Returns the git diff between stable and unstable branches for a given repo.
    """
    repo_path = os.path.join(workspace_path, repo_name)
    if not unstable_branch:
        return "No unstable branch configured for this workspace."
    try:
        res = run_tracked(
            ['git', '-C', repo_path, 'diff', '--no-ext-diff', f'origin/{stable_branch}...origin/{unstable_branch}'],
            capture_output=True, text=True, check=True
        )
        diff_text = res.stdout
        if not diff_text.strip():
            return f"No differences in spec files or sources detected between local Unstable ({unstable_branch}) and local Stable ({stable_branch}) branches."
        return diff_text
    except Exception as e:
        return f"Error loading diff: {str(e)}"

def parse_changes_diff(diff_output):
    """
    Extracts added changelog content from a git diff of *.changes.
    Returns cleaned markdown/text suitable for a PR description.
    """
    if not diff_output:
        return ""

    added_lines = []
    in_hunk = False

    for line in diff_output.splitlines():
        if line.startswith("@@"):
            in_hunk = True
            continue
        if in_hunk:
            if line.startswith("+++ ") or line.startswith("--- "):
                continue
            if line.startswith("+"):
                added_lines.append(line[1:])

    if not added_lines:
        return ""

    text = "\n".join(added_lines).strip()
    text = re.sub(r"^[-]{20,}\s*\n", "", text)
    text = re.sub(r"\n[-]{20,}\s*$", "", text)
    return text.strip()

def get_pr_prefill_info(repo_name, stable_branch="factory", unstable_branch="next", workspace_path="."):
    """
    Inspects branch differences between origin/{stable_branch} and origin/{unstable_branch}
    to generate an intelligent PR title and prefilled description.

    1. If a version bump is detected (unstable > stable), title is:
       'Update {repo_name} to version {unstable_ver}'
    2. If no version bump:
       - 1 commit: commit subject line
       - Multiple commits: '{first_commit_subject} (+N more commits)'
       - Fallback: 'Forward {unstable_branch} to {stable_branch}: {repo_name}'
    3. Description is extracted from the added lines in the *.changes git diff.
    """
    repo_path = os.path.join(workspace_path, repo_name)
    if not unstable_branch:
        return f"Update {repo_name}", ""

    # 1. Find spec file locally
    spec_file = None
    try:
        for f in os.listdir(repo_path):
            if f.endswith('.spec'):
                spec_file = f
                break
    except Exception:
        pass
    if not spec_file:
        spec_file = f"{repo_name}.spec"

    # 2. Extract versions from spec file in both branches
    stable_ver = None
    unstable_ver = None
    try:
        res_s = run_tracked(
            ['git', '-C', repo_path, 'show', f'refs/remotes/origin/{stable_branch}:{spec_file}'],
            capture_output=True, text=True
        )
        for line in res_s.stdout.splitlines():
            if line.strip().lower().startswith('version:'):
                stable_ver = line.split(':', 1)[1].strip()
                break
    except Exception:
        pass

    try:
        res_u = run_tracked(
            ['git', '-C', repo_path, 'show', f'refs/remotes/origin/{unstable_branch}:{spec_file}'],
            capture_output=True, text=True
        )
        for line in res_u.stdout.splitlines():
            if line.strip().lower().startswith('version:'):
                unstable_ver = line.split(':', 1)[1].strip()
                break
    except Exception:
        pass

    # 3. Retrieve non-merge commit subjects between branches
    commits = []
    try:
        res_log = run_tracked(
            ['git', '-C', repo_path, 'log', '--no-merges', '--format=%s', f'refs/remotes/origin/{stable_branch}..refs/remotes/origin/{unstable_branch}'],
            capture_output=True, text=True
        )
        commits = [line.strip() for line in res_log.stdout.splitlines() if line.strip()]
    except Exception:
        pass

    # 4. Determine smart PR Title
    if unstable_ver and stable_ver and is_version_newer(unstable_ver, stable_ver, repo_name):
        title = f"Update {repo_name} to version {unstable_ver}"
    elif unstable_ver and stable_ver and unstable_ver != stable_ver:
        title = f"Update {repo_name} to version {unstable_ver}"
    elif len(commits) == 1:
        title = commits[0]
    elif len(commits) == 2:
        title = f"{commits[0]} (+1 more commit)"
    elif len(commits) > 2:
        title = f"{commits[0]} (+{len(commits) - 1} more commits)"
    else:
        title = f"Forward {unstable_branch} to {stable_branch}: {repo_name}"

    # 5. Extract *.changes diff for PR Description
    description = ""
    try:
        res_diff = run_tracked(
            ['git', '-C', repo_path, 'diff', '--no-ext-diff', f'refs/remotes/origin/{stable_branch}...refs/remotes/origin/{unstable_branch}', '--', '*.changes'],
            capture_output=True, text=True
        )
        description = parse_changes_diff(res_diff.stdout)
    except Exception:
        pass

    if not description:
        description = f"Automated {unstable_branch}-to-{stable_branch} branch forwarding for {repo_name} via Geckopit."

    return title, description

def get_gitea_pr_url(repo_name, stable_branch="factory", unstable_branch="next", workspace_path="."):
    """
    Returns the URL to create a pull request from unstable to stable branch.
    """
    repo_path = os.path.join(workspace_path, repo_name)
    owner, gitea_name = get_gitea_owner_and_repo(repo_path, repo_name)
    if not unstable_branch:
        return f"https://src.opensuse.org/{owner}/{gitea_name}"
    return f"https://src.opensuse.org/{owner}/{gitea_name}/compare/{stable_branch}...{unstable_branch}"

def create_gitea_pr(repo_name, title, description, stable_branch="factory", unstable_branch="next", workspace_path="."):
    """
    Create a Gitea pull request from unstable to stable branch using the 'tea' CLI utility.
    """
    import shutil
    if not unstable_branch:
        return False, "Error: No unstable branch configured for this workspace."
    repo_path = os.path.join(workspace_path, repo_name)
    try:
        if shutil.which("tea") is None:
            return False, "Error: 'tea' CLI utility is not installed on the system."

        cmd = [
            "tea", "pulls", "create",
            "--repo", repo_path,
            "--head", unstable_branch,
            "--base", stable_branch,
            "--title", title,
            "--description", description
        ]

        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode == 0:
            return True, res.stdout.strip()
        else:
            err = res.stderr.strip() or res.stdout.strip()
            return False, f"Gitea error: {err}"
    except subprocess.TimeoutExpired:
        return False, "Gitea error: The 'tea' command timed out after 30 seconds."
    except Exception as e:
        return False, f"Exception occurred: {str(e)}"


def check_worktree_status(repo_path, expected_branch=None):
    """
    Checks the local on-disk git status for a repository checkout or worktree.
    Returns a dict with:
      - exists: bool
      - head: str (branch name or '(detached)')
      - upstream: str (e.g. 'origin/next')
      - ahead: int (commits local is ahead of upstream)
      - behind: int (commits local is behind upstream)
      - dirty: bool (uncommitted tracked changes)
      - untracked: bool (untracked files)
    """
    if not repo_path or not os.path.exists(repo_path):
        return {
            "exists": False,
            "head": None,
            "upstream": None,
            "ahead": 0,
            "behind": 0,
            "dirty": False,
            "untracked": False
        }

    try:
        res = run_tracked(
            ['git', '-C', repo_path, 'status', '--porcelain=v2', '--branch'],
            capture_output=True, text=True, check=True
        )
        head = None
        upstream = None
        ahead = 0
        behind = 0
        dirty = False
        untracked = False

        for line in res.stdout.splitlines():
            if line.startswith('# branch.head '):
                head = line.split()[2]
            elif line.startswith('# branch.upstream '):
                upstream = line.split()[2]
            elif line.startswith('# branch.ab '):
                parts = line.split()
                for p in parts[2:]:
                    if p.startswith('+'):
                        ahead = int(p[1:])
                    elif p.startswith('-'):
                        behind = int(p[1:])
            elif line.startswith('? '):
                entry_name = line[2:].strip()
                if not entry_name.endswith("/") and not os.path.isdir(os.path.join(repo_path, entry_name)):
                    untracked = True
            elif line.startswith('1 ') or line.startswith('2 ') or line.startswith('u '):
                dirty = True

        if upstream is None and expected_branch:
            try:
                rl = run_tracked(
                    ['git', '-C', repo_path, 'rev-list', '--left-right', '--count', f'HEAD...origin/{expected_branch}'],
                    capture_output=True, text=True, check=True
                )
                pts = rl.stdout.strip().split()
                if len(pts) == 2:
                    ahead = int(pts[0])
                    behind = int(pts[1])
            except Exception:
                pass

        return {
            "exists": True,
            "head": head,
            "upstream": upstream,
            "ahead": ahead,
            "behind": behind,
            "dirty": dirty,
            "untracked": untracked
        }
    except Exception as e:
        return {
            "exists": True,
            "head": None,
            "upstream": None,
            "ahead": 0,
            "behind": 0,
            "dirty": False,
            "untracked": False,
            "error": str(e)
        }


def check_repo_worktrees(repo_name, stable_path=None, unstable_path=None, stable_branch="factory", unstable_branch="next"):
    """
    Checks worktree statuses for both stable and unstable checkouts of a given repo.
    """
    stable_wt = None
    if stable_path:
        p = os.path.join(stable_path, repo_name)
        stable_wt = check_worktree_status(p, expected_branch=stable_branch)

    unstable_wt = None
    if unstable_path:
        p = os.path.join(unstable_path, repo_name)
        unstable_wt = check_worktree_status(p, expected_branch=unstable_branch)

    return repo_name, {
        "stable": stable_wt,
        "unstable": unstable_wt
    }


def pull_worktree(repo_path, remote="origin", branch=None):
    """
    Attempts a fast-forward pull (git pull --ff-only) in the given repo_path.
    Returns (success: bool, message: str).
    """
    if not repo_path or not os.path.exists(repo_path):
        return False, "Worktree directory does not exist"

    cmd = ['git', '-C', repo_path, 'pull', '--ff-only']
    if remote and branch:
        cmd.extend([remote, branch])

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"

    try:
        res = run_tracked(cmd, capture_output=True, text=True, check=True, env=env)
        msg = (res.stdout or res.stderr or "").strip()
        return True, msg or "Fast-forward pull succeeded."
    except subprocess.CalledProcessError as e:
        err = (e.stderr or e.stdout or "").strip()
        return False, err or f"Pull failed with exit code {e.returncode}"
    except Exception as e:
        return False, str(e)


def push_worktree(repo_path, remote="origin", branch=None):
    """
    Pushes local commits to remote (git push).
    Returns (success: bool, message: str).
    """
    if not repo_path or not os.path.exists(repo_path):
        return False, "Worktree directory does not exist"

    cmd = ['git', '-C', repo_path, 'push']
    if remote and branch:
        cmd.extend([remote, branch])

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"

    try:
        res = run_tracked(cmd, capture_output=True, text=True, check=True, env=env)
        msg = (res.stdout or res.stderr or "").strip()
        return True, msg or "Push succeeded."
    except subprocess.CalledProcessError as e:
        err = (e.stderr or e.stdout or "").strip()
        return False, err or f"Push failed with exit code {e.returncode}"
    except Exception as e:
        return False, str(e)

def strip_ansi(text):
    """Strip ANSI escape sequences from text for accurate visual width calculations."""
    ansi_escape = re.compile(r'\x1b\[[0-9;]*m')
    return ansi_escape.sub('', text)

def visual_len(text):
    """Calculate the exact monospace visual display width of text, ignoring ANSI colors and accounting for wide emojis."""
    clean_text = strip_ansi(text)
    return sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in clean_text)

def pad_left(text, width):
    """Pads a left-aligned string using the calculated visual display width."""
    v_len = visual_len(text)
    return text + ' ' * max(0, width - v_len)


def is_package_dir(path="."):
    """
    Checks if path is an individual openSUSE package directory
    (contains packaging files such as .spec, _service, .changes, or .obsinfo).
    Supports Git repositories, legacy OSC checkouts, and standalone package checkouts.
    """
    try:
        entries = os.listdir(path)
        has_spec = any(f.endswith(".spec") for f in entries)
        has_service = "_service" in entries
        has_changes = any(f.endswith(".changes") for f in entries)
        has_obsinfo = any(f.endswith(".obsinfo") for f in entries)
        return has_spec or has_service or has_changes or has_obsinfo
    except OSError:
        return False


def get_package_name_from_dir(path="."):
    """Extracts package name from spec file, _service, obsinfo, or directory name."""
    path = os.path.abspath(path)
    base = os.path.basename(path)
    try:
        for f in os.listdir(path):
            if f.endswith(".spec"):
                return f[:-5]
    except OSError:
        pass
    service_file = os.path.join(path, "_service")
    if os.path.isfile(service_file):
        try:
            with open(service_file, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            m = re.search(r'<param\s+name=["\']url["\']>([^<]+)</param>', content)
            if m:
                url = m.group(1).strip()
                url = re.sub(r'\.git/?$', '', url)
                name = url.rstrip('/').split('/')[-1]
                if name:
                    return name
        except Exception:
            pass
    try:
        if os.path.isfile(os.path.join(path, f"{base}.spec")):
            return base
        specs = [f[:-5] for f in os.listdir(path) if f.endswith(".spec")]
        if specs:
            return specs[0]
    except OSError:
        pass
    if os.path.isfile(os.path.join(path, f"{base}.obsinfo")):
        return base
    return base


def load_geckopit_profile_config():
    """Loads active workspace profile settings from ~/.config/geckopit.json."""
    from pathlib import Path
    path = Path.home() / ".config" / "geckopit.json"
    config = {
        "active_workspace": "Default",
        "stable_branch": "factory",
        "unstable_branch": "next",
        "stable_path": ".",
        "unstable_path": None,
        "ignored_unstable_versions": {}
    }
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                active_ws = data.get("active_workspace", "Default")
                workspaces = data.get("workspaces", {})
                active_profile = workspaces.get(active_ws, {})
                config["active_workspace"] = active_ws
                config["stable_branch"] = active_profile.get("stable_branch", "factory") or "factory"
                config["unstable_branch"] = active_profile.get("unstable_branch", "next") or "next"
                config["stable_path"] = active_profile.get("stable_path", ".") or "."
                config["unstable_path"] = active_profile.get("unstable_path")
                config["ignored_unstable_versions"] = active_profile.get("ignored_unstable_versions", {})
        except Exception:
            pass
    return config


def get_local_package_version(repo_path):
    """
    Extracts the current version from the local on-disk package .spec file.
    The .spec file is the single authoritative source of truth for packaging version state.
    """
    if not repo_path or not os.path.isdir(repo_path):
        return None

    pkg_name = get_package_name_from_dir(repo_path)

    # 1. Check <pkg_name>.spec directly
    primary_spec = os.path.join(repo_path, f"{pkg_name}.spec")
    if os.path.isfile(primary_spec):
        try:
            with open(primary_spec, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = re.match(r"^Version:\s*(\S+)", line, re.IGNORECASE)
                    if m:
                        return clean_version(m.group(1))
        except OSError:
            pass

    # 2. Fallback: check any .spec file in the package directory
    try:
        for f in os.listdir(repo_path):
            if f.endswith(".spec"):
                spec_path = os.path.join(repo_path, f)
                try:
                    with open(spec_path, "r", encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            m = re.match(r"^Version:\s*(\S+)", line, re.IGNORECASE)
                            if m:
                                return clean_version(m.group(1))
                except OSError:
                    pass
    except OSError:
        pass

    return None


def check_single_package_full(pkg_dir=".", stable_branch=None, unstable_branch=None):
    """
    Gathers all synchronization, version, PR, and local worktree data for a single package directory.
    Queries backend services concurrently and resolves local on-disk package state.
    """
    pkg_dir = os.path.abspath(pkg_dir)
    parent_dir = os.path.dirname(pkg_dir)
    repo_name = get_package_name_from_dir(pkg_dir)

    prof = load_geckopit_profile_config()
    st_branch = stable_branch or prof.get("stable_branch", "factory") or "factory"
    unst_branch = unstable_branch or prof.get("unstable_branch", "next") or "next"
    ignored_vers = prof.get("ignored_unstable_versions", {})

    wt = check_worktree_status(pkg_dir, expected_branch=unst_branch)
    active_branch = wt.get("head") or "unknown"

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        f_sync = ex.submit(check_repo_sync, repo_name, st_branch, unst_branch, parent_dir)
        f_ver = ex.submit(check_repo_version, repo_name, None, st_branch, unst_branch, parent_dir, ignored_vers)
        f_pr = ex.submit(check_repo_pr, repo_name, st_branch, unst_branch, parent_dir)

        sync = f_sync.result()[1]
        ver = f_ver.result()[1]
        pr = f_pr.result()[1]

    # Resolve local on-disk version and re-evaluate needs_update against upstream
    local_ver = get_local_package_version(pkg_dir)
    if local_ver and active_branch == unst_branch:
        ver["next_ver"] = local_ver
        u_latest = ver.get("upstream_latest", "—")
        if u_latest and u_latest != "—":
            ver["needs_update"] = is_version_newer(u_latest, local_ver, repo_name)
    elif local_ver and active_branch == st_branch:
        ver["factory_ver"] = local_ver
        u_stable = ver.get("upstream_stable", "N/A")
        if u_stable and u_stable != "N/A":
            ver["needs_update"] = is_version_newer(u_stable, local_ver, repo_name)

    # Check local commits ahead of factory if checked out on unstable branch
    if active_branch == unst_branch:
        try:
            rl = run_tracked(
                ["git", "-C", pkg_dir, "rev-list", "--left-right", "--count", f"origin/{st_branch}...HEAD"],
                capture_output=True, text=True, check=True
            ).stdout.strip().split()
            if len(rl) == 2:
                sync["local_next_behind"] = int(rl[0])
                sync["local_next_ahead"] = int(rl[1])
        except Exception:
            pass

    return {
        "repo_name": repo_name,
        "pkg_dir": pkg_dir,
        "active_branch": active_branch,
        "stable_branch": st_branch,
        "unstable_branch": unst_branch,
        "worktree": wt,
        "sync": sync,
        "version": ver,
        "pr": pr,
        "local_version": local_ver
    }
