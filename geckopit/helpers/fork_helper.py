#!/usr/bin/env python3
"""
openSUSE Contributor Fork & Branch Helper
Supports automated branching, Gitea forking, scratch workspace provisioning,
and universal submission back to upstream devel projects and central pool streams.
"""

import os
import sys
import re
import time
import json
import shutil
import urllib.parse
import urllib.request
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List, Callable

try:
    from . import sync_backend
    from . import pool_submit
    from .upgrade import changelog
except ImportError:
    import sync_backend
    import pool_submit
    try:
        from upgrade import changelog
    except ImportError:
        from helpers.upgrade import changelog


DEVEL_MAP_URL = "https://src.opensuse.org/openSUSE/Factory/raw/branch/main/pkgs/_meta/devel_packages"
DEVEL_MAP_CACHE_TTL = 86400.0  # 24 hours


def get_git_env() -> Dict[str, str]:
    """Returns an environment with GIT_SSH_COMMAND configured so git SSH never fails."""
    env = dict(os.environ)
    if not env.get("GIT_SSH_COMMAND"):
        env["GIT_SSH_COMMAND"] = "ssh"
    return env


def get_current_gitea_user() -> Optional[str]:
    """Resolves the current Gitea username from ~/.config/tea/config.yml or Gitea API."""
    tea_path = Path.home() / ".config" / "tea" / "config.yml"
    if tea_path.exists():
        try:
            with open(tea_path, "r", encoding="utf-8", errors="replace") as f:
                c = f.read()
            m = re.search(r"user:\s*(\S+)", c)
            if m:
                return m.group(1).strip()
        except Exception:
            pass

    token = sync_backend.get_gitea_token()
    if token:
        try:
            headers = {"Authorization": f"token {token}", "User-Agent": "geckopit"}
            session = sync_backend.get_http_session()
            resp = session.get("https://src.opensuse.org/api/v1/user", headers=headers, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("username"):
                    return data["username"].strip()
        except Exception:
            pass

    return os.environ.get("USER") or os.environ.get("LOGNAME")


def get_current_obs_user() -> Optional[str]:
    """Resolves the current OBS username from ~/.config/osc/oscrc."""
    osc_path = Path.home() / ".config" / "osc" / "oscrc"
    if osc_path.exists():
        try:
            with open(osc_path, "r", encoding="utf-8", errors="replace") as f:
                c = f.read()
            m = re.search(r"^\s*user\s*=\s*(\S+)", c, re.MULTILINE)
            if m:
                return m.group(1).strip()
        except Exception:
            pass
    return os.environ.get("USER") or os.environ.get("LOGNAME")


_DEVEL_MAP_MEM_CACHE: Optional[Dict[str, str]] = None


def get_devel_packages_map(force_refresh: bool = False) -> Dict[str, str]:
    """
    Retrieves the authoritative openSUSE:Factory devel_packages map.
    Caches on disk at ~/.cache/geckopit/devel_packages.map for 24 hours.
    """
    global _DEVEL_MAP_MEM_CACHE
    if not force_refresh and _DEVEL_MAP_MEM_CACHE is not None:
        return _DEVEL_MAP_MEM_CACHE

    cache_dir = Path.home() / ".cache" / "geckopit"
    cache_file = cache_dir / "devel_packages.map"
    cache_dir.mkdir(parents=True, exist_ok=True)

    now = time.time()
    needs_fetch = force_refresh or not cache_file.exists() or (now - cache_file.stat().st_mtime > DEVEL_MAP_CACHE_TTL)

    if needs_fetch:
        try:
            session = sync_backend.get_http_session()
            resp = session.get(DEVEL_MAP_URL, timeout=10)
            if resp.status_code == 200:
                with open(cache_file, "wb") as f:
                    f.write(resp.content)
        except Exception:
            pass

    devel_map: Dict[str, str] = {}
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        devel_map[parts[0]] = parts[1]
        except Exception:
            pass

    _DEVEL_MAP_MEM_CACHE = devel_map
    return devel_map


STREAM_BRANCH_EQUIVALENTS = {
    "leap-16.1": "slfo-1.3",
    "leap-16.0": "slfo-1.2",
    "slfo-1.3": "leap-16.1",
    "slfo-1.2": "leap-16.0",
}

def resolve_package_devel_target(package_name: str, stream: str = "factory") -> Dict[str, Any]:
    """
    Resolves the authoritative upstream devel target and source management mode (Git vs OBS)
    for a package across release streams.
    """
    pkg_clean = package_name.strip()
    stream_clean = (stream or "factory").strip().lower()

    if stream_clean.startswith("leap-") or stream_clean.startswith("slfo-"):
        target_branch = stream_clean
        repo_url = f"https://src.opensuse.org/pool/{pkg_clean}.git"
        return {
            "package": pkg_clean,
            "stream": stream_clean,
            "devel_project": f"openSUSE:Leap:{stream_clean.removeprefix('leap-')}",
            "devel_package": pkg_clean,
            "is_git": True,
            "scmsync_url": f"{repo_url}#{target_branch}",
            "git_url": repo_url,
            "git_owner": "pool",
            "git_repo": pkg_clean,
            "target_branch": target_branch,
        }

    # Factory Stream: 1. Consult devel_packages map
    devel_map = get_devel_packages_map()
    devel_project = devel_map.get(pkg_clean)
    devel_package = pkg_clean

    # 2. If not in map, inspect openSUSE:Factory package XML metadata
    if not devel_project:
        try:
            env = pool_submit.get_obs_env()
            res = subprocess.run(
                ["osc", "meta", "pkg", "openSUSE:Factory", pkg_clean],
                capture_output=True, text=True, env=env, timeout=10
            )
            if res.returncode == 0:
                import xml.etree.ElementTree as ET
                root = ET.fromstring(res.stdout)
                dev_el = root.find("devel")
                if dev_el is not None and dev_el.get("project"):
                    devel_project = dev_el.get("project")
                    devel_package = dev_el.get("package") or pkg_clean
        except Exception:
            pass

    if not devel_project:
        devel_project = "openSUSE:Factory"

    # 3. Inspect devel package in OBS for <scmsync>
    git_meta = pool_submit.is_project_git_managed(devel_project, package_name=devel_package)
    is_git = bool(git_meta.get("is_git"))
    scmsync_url = git_meta.get("scmsync_url") or ""

    git_owner = None
    git_repo = None
    target_branch = "factory"

    if is_git and scmsync_url:
        _, repo_path, branch_parsed = pool_submit.parse_scmsync(scmsync_url)
        if "/" in repo_path:
            parts = repo_path.split("/", 1)
            git_owner = parts[0]
            git_repo = parts[1]
        else:
            git_owner = "pool"
            git_repo = repo_path

        if branch_parsed:
            target_branch = branch_parsed
        else:
            # Check ?trackingbranch= in URL
            parsed_url = urllib.parse.urlparse(scmsync_url)
            qs = urllib.parse.parse_qs(parsed_url.query)
            if "trackingbranch" in qs and qs["trackingbranch"]:
                target_branch = qs["trackingbranch"][0]

    return {
        "package": pkg_clean,
        "stream": stream_clean,
        "devel_project": devel_project,
        "devel_package": devel_package,
        "is_git": is_git,
        "scmsync_url": scmsync_url,
        "git_url": f"https://src.opensuse.org/{git_owner}/{git_repo}.git" if git_owner and git_repo else None,
        "git_owner": git_owner,
        "git_repo": git_repo,
        "target_branch": target_branch,
    }


def is_inside_registered_workspace(path: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    """
    Checks whether the specified directory (defaults to cwd) is located inside
    an existing non-scratch workspace or superproject (.gitmodules).
    Returns (is_inside, workspace_name).
    """
    check_path = Path(path or os.getcwd()).resolve()

    cfg_path = Path.home() / ".config" / "geckopit.json"
    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            workspaces = data.get("workspaces", {})
            for ws_name, ws in workspaces.items():
                if ws.get("is_scratch"):
                    continue
                for p_key in ("stable_path", "unstable_path"):
                    p_val = ws.get(p_key)
                    if p_val:
                        ws_path = Path(p_val).resolve()
                        try:
                            if check_path == ws_path or ws_path in check_path.parents:
                                return True, ws_name
                        except Exception:
                            pass
        except Exception:
            pass

    # Also inspect parent hierarchy for .gitmodules
    curr = check_path
    while curr != curr.parent:
        if (curr / ".gitmodules").exists():
            return True, curr.name
        curr = curr.parent

    return False, None


def resolve_destination_directory(package_name: str, dest_dir_override: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Resolves the safe destination directory on disk for checking out a branched package.
    Guards against polluting existing superproject workspaces.
    Returns (dest_dir, notice_or_error_message).
    """
    if dest_dir_override:
        return str(Path(dest_dir_override).resolve()), None

    cfg_path = Path.home() / ".config" / "geckopit.json"
    scratch_dir = None
    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            scratch_dir = data.get("scratch_dir")
            if not scratch_dir:
                for ws in data.get("workspaces", {}).values():
                    if ws.get("is_scratch") and ws.get("stable_path"):
                        scratch_dir = ws.get("stable_path")
                        break
        except Exception:
            pass

    inside_ws, ws_name = is_inside_registered_workspace()

    if inside_ws:
        cwd_str = str(Path.cwd())
        if scratch_dir:
            dest = str((Path(scratch_dir).expanduser() / package_name).resolve())
            notice = f"ℹ️ Detected active workspace '{ws_name}' in {cwd_str}. Routing checkout out-of-tree to: {dest}"
            return dest, notice
        else:
            err = (
                f"[Error] Cannot check out into current directory: {cwd_str} is inside registered workspace '{ws_name}'.\n"
                f"Please specify a destination outside the workspace with -d <path> or configure 'scratch_dir' in ~/.config/geckopit.json."
            )
            return None, err
    # Outside existing workspace
    if scratch_dir:
        dest = str((Path(scratch_dir).expanduser() / package_name).resolve())
        return dest, None

    dest = str((Path.cwd() / package_name).resolve())
    return dest, None


def resolve_target_obs_project(devel_project: str, stream: str = "factory", target_project_override: Optional[str] = None) -> str:
    """
    Resolves the target OBS staging project (e.g. home:dimstar:Factory).
    """
    if target_project_override:
        return target_project_override

    cfg_path = Path.home() / ".config" / "geckopit.json"
    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("default_target_project") and stream.lower() == "factory":
                return data["default_target_project"].strip()
        except Exception:
            pass

    obs_user = get_current_obs_user() or "user"
    if stream.lower() == "factory":
        return f"home:{obs_user}:branches:{devel_project}"
    return f"home:{obs_user}:branches:openSUSE:Leap:{stream.removeprefix('leap-')}"


def ensure_gitea_fork(owner: str, repo: str) -> Tuple[bool, str]:
    """
    Ensures that the current user has a fork of owner/repo on src.opensuse.org.
    If the fork already exists, succeeds immediately.
    """
    current_user = get_current_gitea_user()
    if not current_user:
        return False, "Could not determine current Gitea username."

    if current_user.lower() == owner.lower():
        # User is already the owner; no fork needed!
        return True, f"{owner}/{repo}"

    token = sync_backend.get_gitea_token()
    headers = {"User-Agent": "geckopit"}
    if token:
        headers["Authorization"] = f"token {token}"

    session = sync_backend.get_http_session()

    # 1. Check if user fork already exists
    check_url = f"https://src.opensuse.org/api/v1/repos/{current_user}/{repo}"
    try:
        r = session.get(check_url, headers=headers, timeout=10)
        if r.status_code == 200:
            return True, f"{current_user}/{repo}"
    except Exception as e:
        pass

    # 2. Trigger fork creation
    fork_url = f"https://src.opensuse.org/api/v1/repos/{owner}/{repo}/forks"
    try:
        r_fork = session.post(fork_url, headers=headers, json={}, timeout=15)
        if r_fork.status_code in (200, 201, 202):
            return True, f"{current_user}/{repo}"
        elif r_fork.status_code == 409:
            # Fork already exists
            return True, f"{current_user}/{repo}"
        else:
            return False, f"Failed to fork {owner}/{repo}: HTTP {r_fork.status_code} - {r_fork.text}"
    except Exception as e:
        return False, f"Error during fork creation: {str(e)}"


def provision_obs_scmsync_package(
    target_project: str,
    package_name: str,
    scmsync_url: str,
    title: Optional[str] = None
) -> Tuple[bool, str]:
    """
    Creates or updates an OBS package in target_project with an <scmsync> attribute.
    """
    meta_xml = (
        f"<package name=\"{package_name}\" project=\"{target_project}\">\n"
        f"  <title>{title or package_name}</title>\n"
        f"  <description>Automated scratch build via Geckopit</description>\n"
        f"  <scmsync>{scmsync_url}</scmsync>\n"
        f"</package>\n"
    )
    env = pool_submit.get_obs_env()
    try:
        cmd = ["osc", "meta", "pkg", "-F", "-", target_project, package_name]
        res = subprocess.run(cmd, input=meta_xml, capture_output=True, text=True, env=env, timeout=15)
        if res.returncode == 0:
            return True, f"Configured {target_project}/{package_name} with <scmsync>"
        return False, (res.stderr or res.stdout).strip()
    except Exception as e:
        return False, str(e)


def fork_package(
    package_name: str,
    stream: str = "factory",
    target_project: Optional[str] = None,
    dest_dir: Optional[str] = None,
    no_build: bool = False,
    on_log: Optional[Callable[[str], None]] = None
) -> Tuple[bool, str, Optional[str]]:
    """
    Main provisioning engine. Resolves upstream devel target, establishes local
    checkout in scratch directory, sets up topic branches, and links OBS cloud build.
    Returns (success, message, checkout_path).
    """
    def log(msg: str):
        if on_log:
            on_log(msg)
        else:
            print(msg)

    log(f"=> Resolving upstream devel target for '{package_name}' ({stream})...")
    target_info = resolve_package_devel_target(package_name, stream=stream)
    devel_prj = target_info["devel_project"]
    devel_pkg = target_info["devel_package"]
    is_git = target_info["is_git"]

    resolved_dest, notice = resolve_destination_directory(package_name, dest_dir_override=dest_dir)
    if notice:
        log(notice)
    if not resolved_dest:
        return False, notice or "Invalid destination directory.", None

    resolved_obs_prj = resolve_target_obs_project(devel_prj, stream=stream, target_project_override=target_project)

    os.makedirs(os.path.dirname(os.path.abspath(resolved_dest)), exist_ok=True)

    if is_git:
        # TRACK A: Git-managed package (<scmsync> or pool)
        git_owner = target_info["git_owner"]
        git_repo = target_info["git_repo"]
        target_branch = target_info["target_branch"]
        topic_branch = f"for-{target_branch}"

        current_gitea_user = get_current_gitea_user() or "user"
        is_self_owned = (current_gitea_user.lower() == git_owner.lower())

        log(f"   Detected Git-managed package ({git_owner}/{git_repo} tracking '{target_branch}').")

        if not is_self_owned:
            log(f"   Ensuring Gitea fork in '{current_gitea_user}/{git_repo}'...")
            fork_ok, fork_msg = ensure_gitea_fork(git_owner, git_repo)
            if not fork_ok:
                return False, f"Gitea fork failed: {fork_msg}", None
            clone_url = f"gitea@src.opensuse.org:{current_gitea_user}/{git_repo}.git"
            upstream_url = f"https://src.opensuse.org/{git_owner}/{git_repo}.git"
        else:
            log(f"   Package is self-owned ({git_owner}/{git_repo}). Using direct repository clone.")
            clone_url = f"gitea@src.opensuse.org:{git_owner}/{git_repo}.git"
            upstream_url = clone_url

        # Check out or update local scratch repo
        if not os.path.exists(resolved_dest):
            log(f"   Cloning into {resolved_dest}...")
            cmd_clone = ["git", "clone"]
            if target_branch:
                cmd_clone.extend(["--branch", target_branch])
            cmd_clone.extend([clone_url, resolved_dest])
            res_clone = subprocess.run(cmd_clone, env=get_git_env(), capture_output=True, text=True)
            if res_clone.returncode != 0:
                res_clone = subprocess.run(["git", "clone", clone_url, resolved_dest], env=get_git_env(), capture_output=True, text=True)
            if res_clone.returncode != 0:
                return False, f"Git clone failed: {res_clone.stderr.strip()}", None

            if not is_self_owned:
                subprocess.run(["git", "-C", resolved_dest, "remote", "add", "upstream", upstream_url], env=get_git_env(), capture_output=True)
                subprocess.run(["git", "-C", resolved_dest, "fetch", "upstream"], env=get_git_env(), capture_output=True)
        else:
            log(f"   Reusing existing local checkout at {resolved_dest}...")
            subprocess.run(["git", "-C", resolved_dest, "fetch", "--all"], env=get_git_env(), capture_output=True)

        # Map stream branch if target_branch not directly present on pool (e.g. leap-16.1 -> slfo-1.3)
        remote_prefix = "upstream" if not is_self_owned else "origin"
        res_r = subprocess.run(["git", "-C", resolved_dest, "branch", "-r"], capture_output=True, text=True)
        remote_branches = [b.strip() for b in res_r.stdout.splitlines()]
        if f"{remote_prefix}/{target_branch}" not in remote_branches:
            mapped = STREAM_BRANCH_EQUIVALENTS.get(target_branch)
            if mapped and f"{remote_prefix}/{mapped}" in remote_branches:
                log(f"   Target branch mapped: '{target_branch}' ➔ '{mapped}' (upstream pool branch).")
                target_branch = mapped
                topic_branch = f"for-{target_branch}"

        # Ensure topic branch exists and tracks target_branch
        ref_base = f"{remote_prefix}/{target_branch}"
        res_chk = subprocess.run(["git", "-C", resolved_dest, "checkout", "-B", topic_branch, ref_base], env=get_git_env(), capture_output=True, text=True)
        if res_chk.returncode != 0:
            # Fallback to local branch checkout
            subprocess.run(["git", "-C", resolved_dest, "checkout", "-B", topic_branch], env=get_git_env(), capture_output=True)

        log(f"   Active branch: '{topic_branch}' (tracking '{ref_base}').")

        # Set up OBS cloud build package
        if not no_build:
            user_scmsync_url = f"https://src.opensuse.org/{current_gitea_user}/{git_repo}#{topic_branch}"
            log(f"   Linking OBS cloud build: {resolved_obs_prj}/{package_name}...")
            obs_ok, obs_msg = provision_obs_scmsync_package(
                resolved_obs_prj, package_name, user_scmsync_url, title=f"{package_name} ({topic_branch})"
            )
            if obs_ok:
                log(f"   OBS cloud build package created in {resolved_obs_prj}/{package_name}!")
            else:
                log(f"   Warning: Could not configure OBS cloud build: {obs_msg}")

        return True, f"Successfully branched '{package_name}' into {resolved_dest}", resolved_dest

    else:
        # TRACK B: Classic OBS package (e.g. M17N/ibus)
        log(f"   Detected classic OBS package ({devel_prj}/{devel_pkg}).")
        log(f"   Branching {devel_prj}/{devel_pkg} to {resolved_obs_prj}/{package_name}...")

        env = pool_submit.get_obs_env()
        cmd_branch = ["osc", "branch", devel_prj, devel_pkg, resolved_obs_prj, package_name]
        res_b = subprocess.run(cmd_branch, capture_output=True, text=True, env=env)
        if res_b.returncode != 0 and "already exists" not in (res_b.stderr or ""):
            return False, f"osc branch failed: {res_b.stderr.strip()}", None

        log(f"   Checking out {resolved_obs_prj}/{package_name} into {resolved_dest}...")
        cmd_co = ["osc", "co", "-o", resolved_dest, resolved_obs_prj, package_name]
        res_co = subprocess.run(cmd_co, capture_output=True, text=True, env=env)
        if res_co.returncode != 0 and not os.path.isdir(resolved_dest):
            return False, f"osc checkout failed: {res_co.stderr.strip()}", None

        return True, f"Successfully branched '{package_name}' into {resolved_dest}", resolved_dest


def submit_flyby_package(
    package_dir: str,
    message: Optional[str] = None,
    dry_run: bool = False,
    on_log: Optional[Callable[[str], None]] = None
) -> Tuple[bool, str]:
    """
    Universal submission engine. Detects whether package_dir is an OBS branch (.osc)
    or a Git fork (.git), extracts changes, pushes, and creates an OBS Submit Request
    or Gitea Pull Request.
    """
    def log(msg: str):
        if on_log:
            on_log(msg)
        else:
            print(msg)

    pkg_dir = os.path.abspath(package_dir)

    # 1. Detect Git Fork
    if os.path.isdir(os.path.join(pkg_dir, ".git")) or os.path.isfile(os.path.join(pkg_dir, ".git")):
        log("=> Detected Git-managed package.")

        # Get current branch
        res_b = subprocess.run(["git", "-C", pkg_dir, "branch", "--show-current"], capture_output=True, text=True)
        current_branch = res_b.stdout.strip()
        if not current_branch:
            return False, "Cannot submit from detached HEAD state."

        # Load workspace configuration if available
        prof = sync_backend.load_geckopit_profile_config(pkg_dir)
        st_branch = prof.get("stable_branch", "factory") or "factory"
        unst_branch = prof.get("unstable_branch", "next") or "next"
        is_scratch = bool(prof.get("is_scratch"))

        # Resolve remote URLs
        origin_url = ""
        res_rem_orig = subprocess.run(["git", "-C", pkg_dir, "remote", "get-url", "origin"], capture_output=True, text=True)
        if res_rem_orig.returncode == 0:
            origin_url = res_rem_orig.stdout.strip()

        origin_host, origin_repo_path = pool_submit.parse_git_remote_url(origin_url)
        origin_owner, origin_repo = origin_repo_path.split("/", 1) if "/" in origin_repo_path else ("", origin_repo_path)
        pkg_name = origin_repo or os.path.basename(pkg_dir)

        res_rem_up = subprocess.run(["git", "-C", pkg_dir, "remote", "get-url", "upstream"], capture_output=True, text=True)
        upstream_url = res_rem_up.stdout.strip() if res_rem_up.returncode == 0 else ""

        # Case 1: Dual-branch workspace on unstable branch (forward unstable -> stable)
        is_dual_branch_forward = False
        if not is_scratch and not current_branch.startswith("for-"):
            if unst_branch and current_branch == unst_branch:
                is_dual_branch_forward = True
            elif current_branch == "next" and sync_backend.has_git_ref(pkg_dir, f"refs/remotes/origin/{st_branch}"):
                is_dual_branch_forward = True

        if is_dual_branch_forward:
            target_branch = st_branch
            if upstream_url:
                target_host, target_repo_path = pool_submit.parse_git_remote_url(upstream_url)
                target_owner, target_repo = target_repo_path.split("/", 1) if "/" in target_repo_path else (origin_owner, target_repo_path)
            else:
                target_owner = origin_owner
                target_repo = origin_repo

            current_user = get_current_gitea_user() or origin_owner or "user"
            if origin_owner and origin_owner.lower() != target_owner.lower():
                head_spec = f"{origin_owner}:{current_branch}"
            else:
                head_spec = current_branch

            # Push current branch to origin
            if dry_run:
                log(f"   [DRY-RUN] Would push '{current_branch}' to origin.")
            else:
                log(f"   Pushing '{current_branch}' to origin...")
                res_push = subprocess.run(["git", "-C", pkg_dir, "push", "-u", "origin", current_branch], env=get_git_env(), capture_output=True, text=True)
                if res_push.returncode != 0:
                    return False, f"git push failed: {res_push.stderr.strip()}"

            if message:
                title = message.splitlines()[0]
                description = message
            else:
                prefill_title, prefill_desc = sync_backend.get_pr_prefill_info(
                    pkg_name,
                    stable_branch=st_branch,
                    unstable_branch=current_branch,
                    workspace_path=os.path.dirname(pkg_dir)
                )
                title = prefill_title or f"Update {pkg_name} for {target_branch}"
                description = prefill_desc or f"Automated {current_branch}-to-{target_branch} branch forwarding for {pkg_name} via Geckopit."

            if dry_run:
                log(f"   [DRY-RUN] Would create Pull Request against {target_owner}/{target_repo}:{target_branch} (head: {head_spec}):")
                log(f"             Title: {title}")
                return True, f"[DRY-RUN] Pull Request simulation successful (➔ {target_owner}/{target_repo}:{target_branch})"

            log(f"   Creating Pull Request against {target_owner}/{target_repo}:{target_branch}...")
            token = sync_backend.get_gitea_token()
            headers = {"User-Agent": "geckopit"}
            if token:
                headers["Authorization"] = f"token {token}"

            session = sync_backend.get_http_session()
            pr_payload = {
                "head": head_spec,
                "base": target_branch,
                "title": title,
                "body": description,
            }
            api_url = f"https://src.opensuse.org/api/v1/repos/{target_owner}/{target_repo}/pulls"
            resp = session.post(api_url, headers=headers, json=pr_payload, timeout=15)
            if resp.status_code in (200, 201):
                pr_data = resp.json()
                pr_url = pr_data.get("html_url") or f"#{pr_data.get('number')}"
                return True, f"Successfully created Pull Request: {pr_url}"
            elif resp.status_code == 409:
                return True, f"Pull request already exists for {head_spec} against {target_branch}."
            else:
                return False, f"Failed to create Gitea PR: HTTP {resp.status_code} - {resp.text}"

        # Case 2: Maintainer on stable branch in registered workspace (submitting to central pool)
        if not is_scratch and current_branch == st_branch:
            log(f"   Submitting package '{pkg_name}' on '{current_branch}' to central pool...")
            return pool_submit.submit_package_to_pool(
                package_name=pkg_name,
                package_dir=pkg_dir,
                target_project="openSUSE:Factory",
                stable_branch=st_branch,
                message=message,
                dry_run=dry_run
            )

        # Case 3: Topic branch (e.g. for-factory, for-leap-16.1) or scratchpad package fork
        target_branch = "factory"
        upstream_ref = ""
        if current_branch.startswith("for-"):
            stream_candidate = current_branch.removeprefix("for-")
            target_branch = stream_candidate
        else:
            # If tracking branch is configured on upstream remote, use that branch
            res_u = subprocess.run(["git", "-C", pkg_dir, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], capture_output=True, text=True)
            upstream_ref = res_u.stdout.strip() if res_u.returncode == 0 else ""
            if upstream_ref and "/" in upstream_ref:
                rem_name, rem_branch = upstream_ref.split("/", 1)
                if rem_name == "upstream":
                    target_branch = rem_branch
                else:
                    target_branch = current_branch
            else:
                target_branch = current_branch

        # Interrogate remote branches to resolve target_branch and base_ref
        res_r = subprocess.run(["git", "-C", pkg_dir, "branch", "-r"], capture_output=True, text=True)
        remote_branches = [line.strip() for line in res_r.stdout.splitlines()]

        # Map stream aliases if target_branch not directly present on remotes (e.g. leap-16.1 -> slfo-1.3)
        if f"upstream/{target_branch}" not in remote_branches and f"origin/{target_branch}" not in remote_branches:
            mapped = STREAM_BRANCH_EQUIVALENTS.get(target_branch)
            if mapped and (f"upstream/{mapped}" in remote_branches or f"origin/{mapped}" in remote_branches):
                log(f"   Target branch mapped: '{target_branch}' ➔ '{mapped}' (upstream remote branch)")
                target_branch = mapped

        # Extract changes diff
        base_ref = None
        for cand in [f"upstream/{target_branch}", f"origin/{target_branch}", f"refs/pool/{target_branch}"]:
            if cand in remote_branches or sync_backend.has_git_ref(pkg_dir, cand):
                base_ref = cand
                break
        if not base_ref:
            base_ref = upstream_ref or f"origin/{target_branch}"

        cmd_diff = ["git", "-C", pkg_dir, "diff", "--no-ext-diff", f"{base_ref}...HEAD", "--", "*.changes"]
        res_diff = subprocess.run(cmd_diff, capture_output=True, text=True)
        diff_text = res_diff.stdout.strip()

        added_lines = []
        in_hunk = False
        for line in diff_text.splitlines():
            if line.startswith("@@"):
                in_hunk = True
                continue
            if in_hunk:
                if line.startswith("+++ ") or line.startswith("--- "):
                    continue
                if line.startswith("+"):
                    added_lines.append(line[1:])
        desc_text = "\n".join(added_lines).strip()
        cleaned_desc = []
        for l in desc_text.splitlines():
            if re.match(r"^[-]{20,}", l):
                continue
            if re.match(r"^[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d+", l) and "@" in l:
                continue
            cleaned_desc.append(l)

        # Smart title from first commit message or changes diff
        title = message.splitlines()[0] if message else ""
        if not title:
            res_subj = subprocess.run(
                ["git", "-C", pkg_dir, "log", "-1", "--format=%s", f"{base_ref}...HEAD"],
                capture_output=True, text=True
            )
            subj = res_subj.stdout.strip()
            title = subj if subj else f"Update {os.path.basename(pkg_dir)} for {target_branch}"

        cleaned_body = "\n".join(cleaned_desc).strip()
        if not cleaned_body:
            res_full_diff = subprocess.run(["git", "-C", pkg_dir, "diff", "--no-ext-diff", f"{base_ref}...HEAD"], capture_output=True, text=True)
            cleaned_body = res_full_diff.stdout.strip()
            if not cleaned_body:
                cleaned_body = pool_submit.extract_pool_changes(pkg_dir, stable_branch=target_branch)

        description = message or cleaned_body or "Automated submission via Geckopit."

        if dry_run:
            log(f"   [DRY-RUN] Would push '{current_branch}' to origin.")
        else:
            log(f"   Pushing '{current_branch}' to origin...")
            res_push = subprocess.run(["git", "-C", pkg_dir, "push", "-u", "origin", current_branch], env=get_git_env(), capture_output=True, text=True)
            if res_push.returncode != 0:
                return False, f"git push failed: {res_push.stderr.strip()}"

        # Resolve upstream repo for PR target
        if upstream_url:
            target_host, target_repo_path = pool_submit.parse_git_remote_url(upstream_url)
            target_owner, target_repo = target_repo_path.split("/", 1) if "/" in target_repo_path else ("pool", target_repo_path)
        else:
            # Dynamically resolve upstream parent repository using get_upstream_gitea_target
            # (checks Gitea API 'parent' field, stream devel target, or falls back to 'pool')
            target_owner, target_repo = sync_backend.get_upstream_gitea_target(pkg_dir, origin_owner, origin_repo)

        # Self-PR collision guard: if origin is identical to target and branch matches, resolve central pool/devel repo
        if origin_owner and origin_owner.lower() == target_owner.lower() and current_branch == target_branch:
            dev_target = resolve_package_devel_target(os.path.basename(pkg_dir), stream=target_branch)
            if dev_target.get("is_git") and dev_target.get("git_owner") and dev_target.get("git_owner").lower() != origin_owner.lower():
                target_owner = dev_target.get("git_owner")
                target_repo = dev_target.get("git_repo")
            elif origin_owner.lower() != "pool":
                target_owner = "pool"
                target_repo = origin_repo

        current_user = get_current_gitea_user() or origin_owner or "user"
        if origin_owner and origin_owner.lower() != target_owner.lower():
            head_spec = f"{origin_owner}:{current_branch}"
        else:
            head_spec = current_branch

        if dry_run:
            log(f"   [DRY-RUN] Would create Pull Request against {target_owner}/{target_repo}:{target_branch} (head: {head_spec}):")
            log(f"             Title: {title}")
            return True, f"[DRY-RUN] Pull Request simulation successful (➔ {target_owner}/{target_repo}:{target_branch})"

        log(f"   Creating Pull Request against {target_owner}/{target_repo}:{target_branch}...")
        token = sync_backend.get_gitea_token()
        headers = {"User-Agent": "geckopit"}
        if token:
            headers["Authorization"] = f"token {token}"

        session = sync_backend.get_http_session()
        pr_payload = {
            "head": head_spec,
            "base": target_branch,
            "title": title,
            "body": description,
        }
        api_url = f"https://src.opensuse.org/api/v1/repos/{target_owner}/{target_repo}/pulls"
        resp = session.post(api_url, headers=headers, json=pr_payload, timeout=15)
        if resp.status_code in (200, 201):
            pr_data = resp.json()
            pr_url = pr_data.get("html_url") or f"#{pr_data.get('number')}"
            return True, f"Successfully created Pull Request: {pr_url}"
        elif resp.status_code == 409:
            return True, f"Pull request already exists for {head_spec} against {target_branch}."
        else:
            return False, f"Failed to create Gitea PR: HTTP {resp.status_code} - {resp.text}"

    # 2. Detect OBS Branch (.osc)
    elif os.path.isdir(os.path.join(pkg_dir, ".osc")):
        log("=> Detected classic OBS scratch branch.")
        env = pool_submit.get_obs_env()

        # Check status
        res_st = subprocess.run(["osc", "status"], cwd=pkg_dir, capture_output=True, text=True, env=env)
        if res_st.stdout.strip():
            log("   Committing local packaging changes via osc ci...")
            res_ci = subprocess.run(["osc", "ci", "-m", message or "Fly-by update via Geckopit"], cwd=pkg_dir, capture_output=True, text=True, env=env)
            if res_ci.returncode != 0:
                return False, f"osc ci failed: {res_ci.stderr.strip()}"

        log("   Creating Submit Request...")
        cmd_sr = ["osc", "sr", "-m", message or "Fly-by update via Geckopit", "--yes"]
        res_sr = subprocess.run(cmd_sr, cwd=pkg_dir, capture_output=True, text=True, env=env)
        if res_sr.returncode == 0:
            out = res_sr.stdout.strip()
            return True, f"Successfully created Submit Request: {out}"
        return False, f"osc sr failed: {(res_sr.stderr or res_sr.stdout).strip()}"

    return False, f"Directory '{pkg_dir}' is neither a Git repository nor an OBS checkout."


def prune_package(
    package_target: str,
    target_project: Optional[str] = None,
    force: bool = False,
    delete_remote_branch: bool = True,
    delete_obs_package: bool = True,
    on_log: Optional[Callable[[str], None]] = None
) -> Tuple[bool, str]:
    """
    Retires and cleans up a scratch/fly-by package:
    1. Safety check: verifies working directory is clean and commits are pushed (unless force=True).
    2. Deletes remote topic branch on Gitea (if Git-managed and branch starts with for-).
    3. Deletes OBS staging package in target_project (osc rdelete).
    4. Removes local checkout directory from disk.
    Returns (success, message).
    """
    def log(msg: str):
        if on_log:
            on_log(msg)
        else:
            print(msg)

    # 1. Resolve local path
    pkg_dir = None
    package_name = package_target
    if os.path.isdir(package_target):
        pkg_dir = os.path.abspath(package_target)
        package_name = os.path.basename(pkg_dir)
    else:
        # Check scratch_dir from config
        dest_res, _ = resolve_destination_directory(package_target)
        if dest_res and os.path.isdir(dest_res):
            pkg_dir = dest_res
            package_name = package_target
        elif os.path.isdir(os.path.join(".", package_target)):
            pkg_dir = os.path.abspath(os.path.join(".", package_target))
            package_name = package_target

    if not pkg_dir or not os.path.exists(pkg_dir):
        return False, f"Could not locate scratch checkout directory for '{package_target}'."

    log(f"=> Pruning scratchpad package '{package_name}' ({pkg_dir})...")

    # 2. Safety check (unless force)
    is_git = os.path.isdir(os.path.join(pkg_dir, ".git")) or os.path.isfile(os.path.join(pkg_dir, ".git"))
    is_osc = os.path.isdir(os.path.join(pkg_dir, ".osc"))

    if not force:
        if is_git:
            st_res = subprocess.run(["git", "-C", pkg_dir, "status", "--porcelain"], capture_output=True, text=True)
            if st_res.stdout.strip():
                return False, f"Cannot prune '{package_name}': uncommitted changes detected. Use --force to discard."
            try:
                res_u = subprocess.run(["git", "-C", pkg_dir, "rev-list", "@{u}..HEAD", "--count"], capture_output=True, text=True)
                if res_u.returncode == 0 and int(res_u.stdout.strip() or "0") > 0:
                    return False, f"Cannot prune '{package_name}': unpushed local commits detected. Use --force to discard."
            except Exception:
                pass
        elif is_osc:
            env = pool_submit.get_obs_env()
            st_res = subprocess.run(["osc", "status"], cwd=pkg_dir, capture_output=True, text=True, env=env)
            if st_res.stdout.strip():
                return False, f"Cannot prune '{package_name}': uncommitted changes in OSC checkout. Use --force to discard."

    # 3. Delete remote topic branch on Gitea (if Git)
    if is_git and delete_remote_branch:
        try:
            br_res = subprocess.run(["git", "-C", pkg_dir, "branch", "--show-current"], capture_output=True, text=True)
            curr_br = br_res.stdout.strip()
            if curr_br and curr_br.startswith("for-"):
                log(f"   Deleting remote branch '{curr_br}' on origin...")
                res_del_br = subprocess.run(["git", "-C", pkg_dir, "push", "origin", "--delete", curr_br], env=get_git_env(), capture_output=True, text=True)
                if res_del_br.returncode == 0:
                    log(f"   🗑️  Deleted remote topic branch '{curr_br}' on Gitea.")
        except Exception as e:
            log(f"   Warning: Could not delete remote topic branch: {e}")

    # 4. Delete OBS staging package
    if delete_obs_package:
        target_info = resolve_package_devel_target(package_name)
        devel_prj = target_info.get("devel_project", "openSUSE:Factory")

        projects_to_check = []
        if target_project:
            projects_to_check.append(target_project)
        else:
            def_prj = resolve_target_obs_project(devel_prj, stream="factory")
            obs_user = get_current_obs_user() or "user"
            branch_prj = f"home:{obs_user}:branches:{devel_prj}"
            projects_to_check.extend([def_prj, branch_prj])

        env = pool_submit.get_obs_env()
        deleted_any = False
        for obs_prj in set(projects_to_check):
            cmd_del = ["osc", "rdelete", "-f", "-m", f"Clean up scratch package {package_name} via Geckopit", obs_prj, package_name]
            res_del = subprocess.run(cmd_del, capture_output=True, text=True, env=env)
            if res_del.returncode == 0:
                log(f"   🗑️  Deleted OBS staging package '{obs_prj}/{package_name}'.")
                deleted_any = True
        if not deleted_any:
            log(f"   Notice: OBS package was not present or already removed.")

    # 5. Remove local directory from disk
    log(f"   Removing local directory '{pkg_dir}'...")
    try:
        shutil.rmtree(pkg_dir)
        log(f"   🗑️  Removed local checkout '{pkg_dir}'.")
    except Exception as e:
        return False, f"Failed to delete local directory '{pkg_dir}': {e}"

    return True, f"Successfully pruned '{package_name}'."
