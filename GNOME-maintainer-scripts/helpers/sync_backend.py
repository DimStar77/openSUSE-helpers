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

def check_repo_pr(repo_name):
    """
    Queries Gitea API to check if there is an open pull request from 'next' to 'factory'.
    Returns (repo_name, pr_info) where pr_info indicates whether a matching PR is active.
    """
    repo_path = os.path.join('.', repo_name)
    owner, gitea_name = get_gitea_owner_and_repo(repo_path, repo_name)

    url = f"https://src.opensuse.org/api/v1/repos/{owner}/{gitea_name}/pulls?state=open"
    try:
        res_curl = subprocess.run(
            ['curl', '-s', '-m', '10', url],
            capture_output=True, text=True, check=True
        )
        data = json.loads(res_curl.stdout)
        if isinstance(data, list):
            for pr in data:
                base_ref = pr.get("base", {}).get("ref")
                head_ref = pr.get("head", {}).get("ref")
                if base_ref == "factory" and head_ref == "next":
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

def check_repo_sync(repo_name):
    repo_path = os.path.join('.', repo_name)

    # 1. Fetch latest state from origin (src.opensuse.org/<devel_project>/<repo>)
    try:
        subprocess.run(
            ['git', '-C', repo_path, 'fetch', '--quiet', 'origin'],
            check=True, capture_output=True
        )
    except subprocess.CalledProcessError:
        pass

    # 2. Check if origin/factory exists locally
    try:
        subprocess.run(
            ['git', '-C', repo_path, 'show-ref', '--verify', '--quiet', 'refs/remotes/origin/factory'],
            check=True, capture_output=True
        )
        has_origin_factory = True
    except subprocess.CalledProcessError:
        has_origin_factory = False

    if not has_origin_factory:
        return repo_name, {
            "status": "error",
            "message": "Missing origin/factory branch"
        }

    # 3. Check if origin/next exists locally
    try:
        subprocess.run(
            ['git', '-C', repo_path, 'show-ref', '--verify', '--quiet', 'refs/remotes/origin/next'],
            check=True, capture_output=True
        )
        has_origin_next = True
    except subprocess.CalledProcessError:
        has_origin_next = False

    # 4. Fetch from pool/repo_name.git factory branch (src.opensuse.org/pool/<repo>)
    gitea_name = get_gitea_repo_name(repo_path, repo_name)
    pool_url = f"https://src.opensuse.org/pool/{gitea_name}.git"
    pool_status = "unknown"
    pool_ahead = 0
    pool_behind = 0

    try:
        subprocess.run(
            ['git', '-C', repo_path, 'fetch', '--quiet', pool_url, 'factory'],
            check=True, capture_output=True, text=True
        )
        # Compare origin/factory and FETCH_HEAD (pool/factory)
        res = subprocess.run(
            ['git', '-C', repo_path, 'rev-list', '--left-right', '--count', 'origin/factory...FETCH_HEAD'],
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
        elif "couldn't find remote ref factory" in stderr_lower or "no such ref" in stderr_lower or "fatal: couldn't find remote ref" in stderr_lower:
            pool_status = "No Factory in Pool"
        else:
            pool_status = "Fetch failed"

    # 5. Compare devel/factory and devel/next
    next_status = "N/A"
    next_ahead = 0
    next_behind = 0

    if has_origin_next:
        try:
            res_next = subprocess.run(
                ['git', '-C', repo_path, 'rev-list', '--left-right', '--count', 'origin/factory...origin/next'],
                check=True, capture_output=True, text=True
            )
            output_next = res_next.stdout.strip()
            parts_next = output_next.split()
            if len(parts_next) == 2:
                next_behind = int(parts_next[0]) # factory is ahead of next (next needs catch up)
                next_ahead = int(parts_next[1])  # next is ahead of factory (next has additional development)
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

    # Determine sync actions for daily run (e.g. pool update, submission update, or factory -> next merge)
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

def check_repo_version(repo_name, branch=None):
    repo_path = os.path.join('.', repo_name)

    # 1. Fetch latest state from origin (src.opensuse.org/<devel_project>/<repo>)
    try:
        subprocess.run(
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

    # 3. Get version from factory branch (if checking factory or both)
    factory_ver = None
    if branch is None or branch == "factory":
        try:
            res = subprocess.run(
                ['git', '-C', repo_path, 'show', f'refs/remotes/origin/factory:{spec_file}'],
                check=True, capture_output=True, text=True
            )
            for line in res.stdout.splitlines():
                if line.strip().lower().startswith('version:'):
                    factory_ver = line.split(':', 1)[1].strip()
                    break
        except subprocess.CalledProcessError:
            pass

    # 4. Get version from next branch (if checking next or both)
    next_ver = None
    if branch is None or branch == "next":
        try:
            res = subprocess.run(
                ['git', '-C', repo_path, 'show', f'refs/remotes/origin/next:{spec_file}'],
                check=True, capture_output=True, text=True
            )
            for line in res.stdout.splitlines():
                if line.strip().lower().startswith('version:'):
                    next_ver = line.split(':', 1)[1].strip()
                    break
        except subprocess.CalledProcessError:
            pass

    # 5. Query release-monitoring.org for upstream versions (using curl to bypass challenge)
    upstream_stable = None
    upstream_latest = None

    url = f"https://release-monitoring.org/api/v2/packages/?name={repo_name}&distribution=openSUSE"
    try:
        res_curl = subprocess.run(
            ['curl', '-s', '-m', '10', url],
            capture_output=True, text=True, check=True
        )
        data = json.loads(res_curl.stdout)
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

    # Compare versions
    needs_update = False

    if (branch is None or branch == "factory") and factory_ver and upstream_stable and factory_ver != upstream_stable:
        needs_update = True

    if (branch is None or branch == "next") and next_ver and next_ver != "—" and upstream_latest and next_ver != upstream_latest:
        needs_update = True

    return repo_name, {
        "status": "success",
        "factory_ver": factory_ver or "N/A",
        "next_ver": next_ver or "—",
        "upstream_stable": upstream_stable or "N/A",
        "upstream_latest": upstream_latest or "—",
        "needs_update": needs_update
    }

def get_git_diff(repo_name):
    """
    Returns the git diff between origin/factory and origin/next for a given repo.
    """
    repo_path = os.path.join('.', repo_name)
    try:
        res = subprocess.run(
            ['git', '-C', repo_path, 'diff', 'origin/factory...origin/next'],
            capture_output=True, text=True, check=True
        )
        return res.stdout
    except Exception as e:
        return f"Error loading diff: {str(e)}"

def get_gitea_pr_url(repo_name):
    """
    Returns the URL to create a pull request from next to factory.
    """
    repo_path = os.path.join('.', repo_name)
    owner, gitea_name = get_gitea_owner_and_repo(repo_path, repo_name)
    return f"https://src.opensuse.org/{owner}/{gitea_name}/compare/factory...next"

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
