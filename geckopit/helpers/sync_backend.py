import threading
#!/usr/bin/env python3
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
    needs_action = (pool_behind > 0) or (pool_ahead > 0) or (next_behind > 0)

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

# Centralized registry of special-case version normalization overrides
# Key: package name (repo_name)
# Value: a function (or a list of regex/string replacers) to apply
CUSTOM_VERSION_NORMALIZERS = {
    "gnome-tour": lambda v: re.sub(r'\.openSUSE\b', '', v, flags=re.IGNORECASE)
}

def clean_version(version_str, repo_name=None):
    """
    Normalize version string by splitting on '+' to strip downstream git snapshot increments safely,
    and applying package-specific custom overrides from our centralized registry.
    """
    if not version_str:
        return ""

    # 1. Base normalization: strip downstream git snapshot suffixes
    cleaned = version_str.split('+')[0].strip()

    # 2. Package-specific overrides (Centralized Registry)
    if repo_name and repo_name in CUSTOM_VERSION_NORMALIZERS:
        cleaned = CUSTOM_VERSION_NORMALIZERS[repo_name](cleaned)

    return cleaned

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

    # Compare versions with normalization to ignore git snapshot increments (+git...)
    needs_update = False

    if (branch is None or branch == stable_branch or branch == "factory") and factory_ver and upstream_stable:
        if clean_version(factory_ver, repo_name) != clean_version(upstream_stable, repo_name):
            needs_update = True

    if unstable_branch and (branch is None or branch == unstable_branch or branch == "next") and next_ver and next_ver != "—" and upstream_latest:
        if clean_version(next_ver, repo_name) != clean_version(upstream_latest, repo_name):
            # Check if this specific found unstable version is in our ignore list!
            is_ignored = (ignored_ver and clean_version(upstream_latest, repo_name) == clean_version(ignored_ver, repo_name))
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
            ['git', '-C', repo_path, 'diff', f'origin/{stable_branch}...origin/{unstable_branch}'],
            capture_output=True, text=True, check=True
        )
        diff_text = res.stdout
        if not diff_text.strip():
            return f"No differences in spec files or sources detected between local Unstable ({unstable_branch}) and local Stable ({stable_branch}) branches."
        return diff_text
    except Exception as e:
        return f"Error loading diff: {str(e)}"

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
