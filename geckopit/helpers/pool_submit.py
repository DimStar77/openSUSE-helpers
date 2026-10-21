#!/usr/bin/env python3
"""
Central Pool Submit Helper and OBS Request Auditor for Geckopit.

Manages package submissions from devel projects (e.g. GNOME:Factory)
to the central openSUSE pool (openSUSE:Factory).

Key Responsibilities:
1. Detect whether target project is OBS-managed or Git-managed (<scmsync> in project meta).
2. Query and cache pending OBS submit requests (states=new,review) across openSUSE:Factory.
3. Catch the 'accepted but not yet synced' gap by auditing recently accepted requests.
4. Provide pre-flight validation preventing duplicate or stale submissions.
5. Extract .changes diffs to automatically prefill submit request descriptions.
6. Support tree-wide workspace auditing to identify which packages actually need submission.
"""

import os
import sys
import re
import time
import json
import datetime
import subprocess
import xml.etree.ElementTree as ET
from typing import Optional, Dict, Any, List, Tuple

# Module-level caches with TTL
_META_CACHE: Dict[str, Dict[str, Any]] = {}
_PENDING_REQUESTS_CACHE: Dict[str, Any] = {"time": 0.0, "data": {}}
_ACCEPTED_REQUESTS_CACHE: Dict[str, Any] = {"time": 0.0, "data": {}}

PENDING_CACHE_TTL = 60.0    # 1 minute
ACCEPTED_CACHE_TTL = 120.0  # 2 minutes
META_CACHE_TTL = 3600.0     # 1 hour


def get_obs_env() -> Dict[str, str]:
    """
    Returns an execution environment with a valid DBUS_SESSION_BUS_ADDRESS
    configured so secretstorage/keyring in osc never fails in subprocesses.
    """
    env = dict(os.environ)
    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        xdg_runtime = env.get("XDG_RUNTIME_DIR")
        if not xdg_runtime:
            uid = os.getuid()
            xdg_runtime = f"/run/user/{uid}"
        bus_path = os.path.join(xdg_runtime, "bus")
        if os.path.exists(bus_path):
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus_path}"
    return env


def is_project_git_managed(project: str = "openSUSE:Factory", timeout: int = 15, force_refresh: bool = False) -> Dict[str, Any]:
    """
    Checks if an OBS project is managed via Git by inspecting its project meta
    for an <scmsync> declaration.
    Returns: {"is_git": bool, "scmsync_url": Optional[str]}
    """
    now = time.time()
    if not force_refresh and project in _META_CACHE:
        cached = _META_CACHE[project]
        if (now - cached.get("timestamp", 0)) < META_CACHE_TTL:
            return cached["result"]

    cmd = ["osc", "api", f"/source/{project}/_meta"]
    try:
        res = subprocess.run(
            cmd,
            env=get_obs_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True
        )
        root = ET.fromstring(res.stdout)
        scmsync_el = root.find("scmsync")
        if scmsync_el is not None and scmsync_el.text and scmsync_el.text.strip():
            result = {"is_git": True, "scmsync_url": scmsync_el.text.strip()}
        else:
            result = {"is_git": False, "scmsync_url": None}
    except Exception:
        # If meta query fails (network / unauthorized), default to False for openSUSE:Factory
        result = {"is_git": False, "scmsync_url": None}

    _META_CACHE[project] = {"timestamp": now, "result": result}
    return result


def parse_obs_requests_collection(xml_content: str, target_project: str = "openSUSE:Factory") -> Dict[str, List[Dict[str, Any]]]:
    """
    Parses an OBS collection XML into a dictionary mapping target_package -> list of request dicts.
    """
    requests_by_pkg: Dict[str, List[Dict[str, Any]]] = {}
    if not xml_content or not xml_content.strip():
        return requests_by_pkg

    try:
        root = ET.fromstring(xml_content)
    except Exception:
        return requests_by_pkg

    for req in root.findall("request"):
        req_id = req.get("id")
        creator = req.get("creator") or ""
        state_el = req.find("state")
        state_name = state_el.get("name") if state_el is not None else "unknown"
        state_who = state_el.get("who") if state_el is not None else ""
        state_when = state_el.get("when") if state_el is not None else ""

        desc_el = req.find("description")
        description = desc_el.text.strip() if desc_el is not None and desc_el.text else ""

        for action in req.findall("action"):
            if action.get("type") != "submit":
                continue
            target_el = action.find("target")
            source_el = action.find("source")
            acceptinfo_el = action.find("acceptinfo")

            if target_el is None:
                continue
            t_prj = target_el.get("project")
            t_pkg = target_el.get("package")

            if t_prj == target_project and t_pkg:
                src_prj = source_el.get("project") if source_el is not None else ""
                src_pkg = source_el.get("package") if source_el is not None else t_pkg
                src_rev = source_el.get("rev") if source_el is not None else ""

                accept_info = None
                if acceptinfo_el is not None:
                    accept_info = {
                        "rev": acceptinfo_el.get("rev"),
                        "srcmd5": acceptinfo_el.get("srcmd5"),
                        "osrcmd5": acceptinfo_el.get("osrcmd5"),
                    }

                req_info = {
                    "id": req_id,
                    "creator": creator,
                    "state": state_name,
                    "state_who": state_who,
                    "state_when": state_when,
                    "target_project": t_prj,
                    "target_package": t_pkg,
                    "source_project": src_prj,
                    "source_package": src_pkg,
                    "source_rev": src_rev,
                    "acceptinfo": accept_info,
                    "description": description,
                }
                requests_by_pkg.setdefault(t_pkg, []).append(req_info)

    return requests_by_pkg


def get_pending_obs_requests(
    target_project: str = "openSUSE:Factory",
    timeout: int = 25,
    force_refresh: bool = False
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Fetches all pending submit requests (states=review,new) targeting target_project.
    Returns: Dict[package_name, List[request_dict]]
    """
    now = time.time()
    if not force_refresh and target_project in _PENDING_REQUESTS_CACHE.get("data", {}):
        cached_entry = _PENDING_REQUESTS_CACHE["data"][target_project]
        if (now - cached_entry.get("timestamp", 0)) < PENDING_CACHE_TTL:
            return cached_entry["requests"]

    cmd = [
        "osc", "api",
        f"/request?view=collection&project={target_project}&states=review,new&types=submit"
    ]
    try:
        res = subprocess.run(
            cmd,
            env=get_obs_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True
        )
        parsed = parse_obs_requests_collection(res.stdout, target_project=target_project)
    except Exception:
        parsed = {}

    if "data" not in _PENDING_REQUESTS_CACHE:
        _PENDING_REQUESTS_CACHE["data"] = {}
    _PENDING_REQUESTS_CACHE["data"][target_project] = {
        "timestamp": now,
        "requests": parsed
    }
    return parsed


def get_recently_accepted_obs_requests(
    target_project: str = "openSUSE:Factory",
    days: int = 7,
    timeout: int = 25,
    force_refresh: bool = False
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Queries OBS for submit requests targeting target_project accepted in the last N days.
    Catches the gap where a request was accepted in OBS but not yet mirrored to Gitea pool.
    Returns: Dict[package_name, List[request_dict]]
    """
    now = time.time()
    if not force_refresh and target_project in _ACCEPTED_REQUESTS_CACHE.get("data", {}):
        cached_entry = _ACCEPTED_REQUESTS_CACHE["data"][target_project]
        if (now - cached_entry.get("timestamp", 0)) < ACCEPTED_CACHE_TTL:
            return cached_entry["requests"]

    # Calculate ISO cutoff timestamp in UTC
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    xpath = f"state/@name='accepted' and action/target/@project='{target_project}' and state/@when>'{cutoff}'"
    cmd = ["osc", "api", f"/search/request?match={xpath}"]
    try:
        res = subprocess.run(
            cmd,
            env=get_obs_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True
        )
        parsed = parse_obs_requests_collection(res.stdout, target_project=target_project)
    except Exception:
        parsed = {}

    if "data" not in _ACCEPTED_REQUESTS_CACHE:
        _ACCEPTED_REQUESTS_CACHE["data"] = {}
    _ACCEPTED_REQUESTS_CACHE["data"][target_project] = {
        "timestamp": now,
        "requests": parsed
    }
    return parsed




def ensure_pool_ref_fresh(pkg_dir: str, stable_branch: str = "factory") -> bool:
    """
    Fetches the latest refs/heads/{stable_branch} from src.opensuse.org/pool/<pkg>.git
    into refs/pool/{stable_branch} to guarantee the local pool ref matches src.o.o.
    """
    if not os.path.isdir(pkg_dir):
        return False
    pkg_name = os.path.basename(os.path.abspath(pkg_dir))
    pool_url = f"https://src.opensuse.org/pool/{pkg_name}.git"
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        res = subprocess.run(
            ["git", "-C", pkg_dir, "fetch", "--quiet", pool_url, f"+refs/heads/{stable_branch}:refs/pool/{stable_branch}"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10
        )
        return res.returncode == 0
    except Exception:
        return False


def ensure_origin_ref_fresh(pkg_dir: str, branch: str = "factory") -> bool:
    """
    Fetches origin/{branch} to guarantee local tracking branch matches src.opensuse.org.
    """
    if not os.path.isdir(pkg_dir):
        return False
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        res = subprocess.run(
            ["git", "-C", pkg_dir, "fetch", "--quiet", "origin", branch],
            env=env,
            capture_output=True,
            text=True,
            timeout=10
        )
        return res.returncode == 0
    except Exception:
        return False

def is_accepted_sr_already_synced(
    pkg_dir: str,
    sr: Dict[str, Any],
    stable_branch: str = "factory"
) -> bool:
    """
    Checks whether the changes described in an accepted submit request have
    already landed in refs/pool/{stable_branch}.
    If they are already present in the pool ref, the request has finished syncing,
    and any remaining commits in origin/{stable_branch} are unsubmitted updates.
    """
    desc = sr.get("description", "").strip()
    if not desc or not os.path.isdir(pkg_dir):
        return False

    ensure_pool_ref_fresh(pkg_dir, stable_branch=stable_branch)
    pool_ref = f"refs/pool/{stable_branch}"
    try:
        # Extract first meaningful bullet or line
        first_line = ""
        for line in desc.splitlines():
            line = line.strip()
            if line.startswith("- "):
                line = line[2:].strip()
            elif line.startswith("* "):
                line = line[2:].strip()
            if line and not line.startswith("---") and not (re.match(r"^[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d+", line) and "@" in line):
                first_line = line
                break

        if first_line:
            # Check git log of pool_ref
            res = subprocess.run(
                ["git", "-C", pkg_dir, "log", pool_ref, "--grep=" + first_line[:50]],
                capture_output=True, text=True
            )
            if res.returncode == 0 and res.stdout.strip():
                return True

            # Also check .changes files at pool_ref
            try:
                changes_files = [f for f in os.listdir(pkg_dir) if f.endswith(".changes")]
                for cf in changes_files:
                    res_show = subprocess.run(
                        ["git", "-C", pkg_dir, "show", f"{pool_ref}:{cf}"],
                        capture_output=True, text=True
                    )
                    if res_show.returncode == 0 and first_line[:40] in res_show.stdout:
                        return True
            except Exception:
                pass
    except Exception:
        pass

    return False

def get_package_pool_submission_status(
    package_name: str,
    target_project: str = "openSUSE:Factory",
    pending_requests: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    accepted_requests: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    allow_accepted_query: bool = True,
    pkg_dir: Optional[str] = None,
    stable_branch: str = "factory"
) -> Dict[str, Any]:
    """
    Evaluates whether a package currently has an active pending or recently accepted
    submit request against target_project.

    Returns dict:
    - status: "pending" | "accepted_pending_sync" | "unsubmitted"
    - request: Dict representing the matching request (if any)
    """
    if not isinstance(pending_requests, dict):
        pending_requests = get_pending_obs_requests(target_project=target_project)
    if not isinstance(pkg_dir, str):
        pkg_dir = None
    if not isinstance(stable_branch, str):
        stable_branch = "factory"

    if package_name in pending_requests and pending_requests[package_name]:
        # Sort newest ID first
        sorted_reqs = sorted(pending_requests[package_name], key=lambda r: int(r.get("id") or 0), reverse=True)
        primary = sorted_reqs[0]
        return {
            "status": "pending",
            "request": primary,
            "requests": sorted_reqs,
            "state": primary.get("state", "review"),
            "request_id": primary.get("id"),
            "source_project": primary.get("source_project"),
        }

    if allow_accepted_query:
        if not isinstance(accepted_requests, dict):
            accepted_requests = get_recently_accepted_obs_requests(target_project=target_project)

        if package_name in accepted_requests and accepted_requests[package_name]:
            sorted_acc = sorted(accepted_requests[package_name], key=lambda r: int(r.get("id") or 0), reverse=True)
            primary = sorted_acc[0]

            # Verify if this accepted request has ALREADY synced to pool
            if pkg_dir and is_accepted_sr_already_synced(pkg_dir, primary, stable_branch=stable_branch):
                return {
                    "status": "unsubmitted",
                    "request": None,
                    "requests": [],
                    "state": None,
                    "request_id": None,
                    "source_project": None,
                }

            return {
                "status": "accepted_pending_sync",
                "request": primary,
                "requests": sorted_acc,
                "state": "accepted",
                "request_id": primary.get("id"),
                "source_project": primary.get("source_project"),
                "accepted_when": primary.get("state_when"),
            }

    return {
        "status": "unsubmitted",
        "request": None,
        "requests": [],
        "state": None,
        "request_id": None,
        "source_project": None,
    }


def resolve_source_obs_project(package_dir: str, default: str = "GNOME:Factory") -> str:
    """
    Determines the source OBS project to submit from.
    Inspects:
    1. staging.config in package_dir or parent directories (ObsProject key)
    2. .osc/_project
    3. Fallback default
    """
    curr = os.path.abspath(package_dir)
    # Walk up to find staging.config
    while curr and curr != "/":
        staging_cfg = os.path.join(curr, "staging.config")
        if os.path.isfile(staging_cfg):
            try:
                with open(staging_cfg, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("ObsProject"):
                    return data["ObsProject"].strip()
            except Exception:
                pass
        curr = os.path.dirname(curr)

    # Check .osc/_project
    osc_prj = os.path.join(package_dir, ".osc", "_project")
    if os.path.isfile(osc_prj):
        try:
            with open(osc_prj, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                return content
        except Exception:
            pass

    return default


def extract_pool_changes(package_dir: str, stable_branch: str = "factory") -> str:
    """
    Extracts added .changes entries between refs/pool/{stable_branch} and origin/{stable_branch} (or HEAD).
    Returns clean changelog text suitable for the -m description of an osc submit request.
    Uses --ignore-space-at-eol to eliminate trailing whitespace edits on older changelog entries.
    """
    pool_ref = f"refs/pool/{stable_branch}"
    target_ref = f"origin/{stable_branch}"

    has_pool = False
    try:
        subprocess.run(["git", "-C", package_dir, "rev-parse", "--verify", pool_ref], capture_output=True, check=True)
        has_pool = True
    except Exception:
        pass

    if not has_pool:
        # Fallback to HEAD log
        try:
            res = subprocess.run(
                ["git", "-C", package_dir, "log", "-n", "1", "--format=%s"],
                capture_output=True, text=True, check=True
            )
            return res.stdout.strip()
        except Exception:
            return f"Update {os.path.basename(package_dir)}"

    # Check if target_ref exists, else HEAD
    try:
        subprocess.run(["git", "-C", package_dir, "rev-parse", "--verify", target_ref], capture_output=True, check=True)
        compare_ref = target_ref
    except Exception:
        compare_ref = "HEAD"

    # Diff .changes with --ignore-space-at-eol
    try:
        res_diff = subprocess.run(
            ["git", "-C", package_dir, "--no-pager", "diff", "--no-ext-diff", "--ignore-space-at-eol", f"{pool_ref}...{compare_ref}", "--", "*.changes"],
            capture_output=True, text=True, check=True
        )
        diff_text = res_diff.stdout
    except Exception:
        diff_text = ""

    if diff_text:
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
        if added_lines:
            text = "\n".join(added_lines).strip()
            # Clean header lines (e.g. date headers or divider lines)
            lines = text.splitlines()
            cleaned_lines = []
            for l in lines:
                if re.match(r"^[-]{20,}", l):
                    continue
                if re.match(r"^[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d+", l) and "@" in l:
                    continue
                cleaned_lines.append(l)
            extracted = "\n".join(cleaned_lines).strip()
            if extracted:
                return extracted

    # Fallback to non-merge commit subjects
    try:
        res_log = subprocess.run(
            ["git", "-C", package_dir, "log", "--no-merges", "--format=- %s", f"{pool_ref}..{compare_ref}"],
            capture_output=True, text=True, check=True
        )
        commits = res_log.stdout.strip()
        if commits:
            return commits
    except Exception:
        pass

    return f"Update {os.path.basename(package_dir)} to match devel project {stable_branch}"


def discover_workspace_packages(workspace_path: str) -> List[str]:
    """
    Discovers all packages in a workspace, checking both .gitmodules
    and directories containing .git or .osc.
    """
    pkgs = set()
    gitmodules = os.path.join(workspace_path, ".gitmodules")
    if os.path.isfile(gitmodules):
        try:
            res = subprocess.run(
                ["git", "config", "--file", gitmodules, "--get-regexp", "path"],
                capture_output=True, text=True, check=True
            )
            for line in res.stdout.splitlines():
                pts = line.split()
                if len(pts) >= 2:
                    pkgs.add(pts[1])
        except Exception:
            pass

    try:
        for entry in os.listdir(workspace_path):
            full = os.path.join(workspace_path, entry)
            if os.path.isdir(full) and (os.path.exists(os.path.join(full, ".git")) or os.path.exists(os.path.join(full, ".osc"))):
                pkgs.add(entry)
    except Exception:
        pass

    return sorted(pkgs)


def submit_package_to_pool(
    package_name: str,
    package_dir: str,
    target_project: str = "openSUSE:Factory",
    stable_branch: str = "factory",
    message: Optional[str] = None,
    dry_run: bool = False,
    force: bool = False
) -> Tuple[bool, str]:
    """
    Submits a package from the local devel checkout to target_project (openSUSE:Factory).
    Performs full pre-flight verification:
    - Verifies pool_ahead > 0
    - Checks that working tree is clean
    - Checks that local branch has been pushed to remote tracking branch
    - Checks whether an active pending SR or recently accepted SR already exists
    - Executes 'osc sr <source_prj> <pkg> <target_prj> -m <msg>'
    """
    if not os.path.isdir(package_dir):
        return False, f"Package directory '{package_dir}' does not exist."

    # 0. Guarantee local refs match authoritative remotes on src.opensuse.org
    ensure_origin_ref_fresh(package_dir, branch=stable_branch)
    ensure_pool_ref_fresh(package_dir, stable_branch=stable_branch)

    # 1. Check working tree cleanliness
    try:
        st_res = subprocess.run(
            ["git", "-C", package_dir, "status", "--porcelain"],
            capture_output=True, text=True, check=True
        )
        if st_res.stdout.strip() and not force:
            return False, f"Working tree for '{package_name}' has uncommitted changes. Please commit or stash first."
    except Exception as e:
        return False, f"Git status check failed: {e}"

    # 2. Check if local commits are unpushed to origin
    try:
        ahead_res = subprocess.run(
            ["git", "-C", package_dir, "rev-list", f"origin/{stable_branch}..HEAD", "--count"],
            capture_output=True, text=True, check=True
        )
        unpushed = int(ahead_res.stdout.strip() or "0")
        if unpushed > 0 and not force:
            return False, f"Local branch has {unpushed} unpushed commit(s) on '{stable_branch}'. Push to origin first."
    except Exception:
        pass

    # 3. Check pool ahead count
    pool_ref = f"refs/pool/{stable_branch}"
    try:
        cnt_res = subprocess.run(
            ["git", "-C", package_dir, "rev-list", f"{pool_ref}..origin/{stable_branch}", "--count"],
            capture_output=True, text=True, check=True
        )
        pool_ahead = int(cnt_res.stdout.strip() or "0")
        if pool_ahead == 0 and not force:
            return False, f"Package '{package_name}' is already in sync with {target_project} (pool_ahead is 0)."
    except Exception:
        pass

    # 4. Check Pending and Recently Accepted Request Guards
    status_info = get_package_pool_submission_status(package_name, target_project=target_project, pkg_dir=package_dir, stable_branch=stable_branch)
    if status_info["status"] == "pending" and not force:
        req = status_info["request"]
        req_id = req.get("id")
        st = req.get("state")
        src = req.get("source_project")
        return False, f"Active submit request #{req_id} ({st} from {src}) is already pending against {target_project}."

    if status_info["status"] == "accepted_pending_sync" and not force:
        req = status_info["request"]
        req_id = req.get("id")
        when = req.get("accepted_when", "recently")
        return False, f"Submit request #{req_id} was already accepted ({when}) and is currently pending backend sync to central pool."

    # 5. Check if target is Git-managed (scmsync)
    git_meta = is_project_git_managed(target_project)
    if git_meta.get("is_git"):
        sync_url = git_meta.get("scmsync_url")
        return False, f"Target project {target_project} is Git-managed via {sync_url}. Submit via Gitea Pull Request instead of 'osc sr'."

    # 6. Resolve source project
    source_project = resolve_source_obs_project(package_dir, default="GNOME:Factory")

    # 7. Extract message if not provided
    if not message or not message.strip():
        message = extract_pool_changes(package_dir, stable_branch=stable_branch)

    cmd = ["osc", "sr", source_project, package_name, target_project, "-m", message]

    if dry_run:
        return True, f"[DRY-RUN] Would execute: osc sr {source_project} {package_name} {target_project} -m '{message}'"

    try:
        sr_res = subprocess.run(
            cmd,
            env=get_obs_env(),
            capture_output=True,
            text=True,
            check=True
        )
        out = sr_res.stdout.strip()
        # Invalidate pending requests cache for this project
        if target_project in _PENDING_REQUESTS_CACHE.get("data", {}):
            del _PENDING_REQUESTS_CACHE["data"][target_project]
        return True, out if out else f"Submitted {package_name} to {target_project}."
    except subprocess.CalledProcessError as e:
        err = (e.stderr or e.stdout or str(e)).strip()
        return False, f"osc sr failed (code {e.returncode}): {err}"
    except Exception as e:
        return False, f"Failed to execute osc sr: {e}"


def audit_workspace_pool_submissions(
    workspace_path: str,
    stable_branch: str = "factory",
    target_project: str = "openSUSE:Factory",
    use_cache: bool = True
) -> Dict[str, Any]:
    """
    Audits an entire workspace (e.g. GNOME superproject) against openSUSE:Factory.
    Queries the pending requests collection once, and cross-references all packages
    where pool_ahead > 0 to identify which packages truly need submission.
    """
    pending = get_pending_obs_requests(target_project=target_project)
    accepted = get_recently_accepted_obs_requests(target_project=target_project)

    results: Dict[str, Any] = {
        "target_project": target_project,
        "total_packages": 0,
        "ahead_packages": [],
        "pending_sr_packages": [],
        "accepted_sync_packages": [],
        "unsubmitted_packages": [],
    }

    if not os.path.isdir(workspace_path):
        return results

    pool_ref = f"refs/pool/{stable_branch}"
    target_ref = f"origin/{stable_branch}"

    # Check if cache is available for fast retrieval
    cached_pkgs = {}
    if use_cache:
        cache_dir = os.path.expanduser("~/.cache/geckopit")
        if os.path.isdir(cache_dir):
            for cf in os.listdir(cache_dir):
                if cf.startswith("cache_") and cf.endswith(".json"):
                    try:
                        with open(os.path.join(cache_dir, cf), "r", encoding="utf-8") as f:
                            d = json.load(f)
                            cached_pkgs.update(d.get("package_data", {}))
                    except Exception:
                        pass

    packages = discover_workspace_packages(workspace_path)
    results["total_packages"] = len(packages)

    for pkg in packages:
        pkg_dir = os.path.join(workspace_path, pkg)
        pool_ahead = 0

        # Try cache first
        if pkg in cached_pkgs:
            pool_ahead = cached_pkgs[pkg].get("sync", {}).get("pool_ahead", 0)

        # Fallback to direct git check if not cached or pool_ahead <= 0
        if pool_ahead <= 0 and os.path.exists(os.path.join(pkg_dir, ".git")):
            try:
                cnt_res = subprocess.run(
                    ["git", "-C", pkg_dir, "rev-list", f"{pool_ref}..{target_ref}", "--count"],
                    capture_output=True, text=True
                )
                if cnt_res.returncode == 0:
                    pool_ahead = int(cnt_res.stdout.strip() or "0")
            except Exception:
                pass

        if pool_ahead > 0:
            status_info = get_package_pool_submission_status(
                pkg,
                target_project=target_project,
                pending_requests=pending,
                accepted_requests=accepted,
                pkg_dir=pkg_dir,
                stable_branch=stable_branch
            )

            # Extract short change summary
            summary = ""
            if os.path.exists(pkg_dir):
                summary = extract_pool_changes(pkg_dir, stable_branch=stable_branch)
                # First line only for compact table
                if summary:
                    summary = summary.splitlines()[0].strip()
                    if summary.startswith("- "):
                        summary = summary[2:].strip()

            pkg_info = {
                "package": pkg,
                "pool_ahead": pool_ahead,
                "pkg_dir": pkg_dir,
                "summary": summary,
            }
            pkg_info.update(status_info)
            results["ahead_packages"].append(pkg_info)

            if status_info["status"] == "pending":
                results["pending_sr_packages"].append(pkg_info)
            elif status_info["status"] == "accepted_pending_sync":
                results["accepted_sync_packages"].append(pkg_info)
            else:
                results["unsubmitted_packages"].append(pkg_info)

    return results
