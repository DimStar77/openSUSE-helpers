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
import urllib.parse
from typing import Optional, Dict, Any, List, Tuple, Set

# Module-level caches with TTL
_META_CACHE: Dict[str, Dict[str, Any]] = {}
_PENDING_REQUESTS_CACHE: Dict[str, Any] = {"time": 0.0, "data": {}}
_ACCEPTED_REQUESTS_CACHE: Dict[str, Any] = {"time": 0.0, "data": {}}
_DEVEL_PROJECT_CACHE: Dict[Tuple[str, str], str] = {}
_SCMSYNC_CACHE: Dict[Tuple[str, str], Optional[str]] = {}

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




def has_real_obs_diff(
    source_project: str,
    package_name: str,
    target_project: str = "openSUSE:Factory"
) -> Tuple[bool, str]:
    """
    Runs osc rdiff between source_project and target_project for package_name.
    Filters out internal scmbridge / service files (_scmsync.obsinfo, build.specials.obscpio).
    Returns (has_meaningful_diff, diff_text).
    """
    pkg_base = package_name.split("/")[-1]
    try:
        res = subprocess.run(
            ["osc", "rdiff", target_project, pkg_base, source_project],
            env=get_obs_env(),
            capture_output=True,
            text=True,
            timeout=15
        )
        if res.returncode != 0:
            return False, (res.stderr or res.stdout).strip()
        diff_text = res.stdout.strip()
        if not diff_text:
            return False, ""

        has_meaningful = False
        for line in diff_text.splitlines():
            if line.startswith("Index: "):
                filename = line.split("Index: ", 1)[1].strip()
                if filename not in ("_scmsync.obsinfo", "build.specials.obscpio"):
                    has_meaningful = True
                    break
        return has_meaningful, diff_text
    except Exception as e:
        return False, str(e)


def parse_scmsync(scmsync_str: str) -> Tuple[str, str, Optional[str]]:
    """
    Parses an OBS <scmsync> URL into (host, repo_path, branch).
    Examples:
    - https://src.opensuse.org/dimstar/vlc#factory -> ('src.opensuse.org', 'dimstar/vlc', 'factory')
    - https://src.opensuse.org/GNOME/mutter?trackingbranch=factory#5d125 -> ('src.opensuse.org', 'GNOME/mutter', 'factory')
    """
    parsed = urllib.parse.urlparse(scmsync_str.strip())
    path = parsed.path.strip("/").removesuffix(".git")
    query = urllib.parse.parse_qs(parsed.query)
    branch = None
    if "trackingbranch" in query and query["trackingbranch"]:
        branch = query["trackingbranch"][0]
    elif parsed.fragment and not re.match(r"^[0-9a-f]{40}$", parsed.fragment):
        branch = parsed.fragment
    return parsed.netloc.lower(), path, branch


def parse_git_remote_url(remote_url: str) -> Tuple[str, str]:
    """
    Parses a git remote URL into (host, repo_path).
    Examples:
    - gitea@src.opensuse.org:dimstar/vlc.git -> ('src.opensuse.org', 'dimstar/vlc')
    - https://src.opensuse.org/dimstar/vlc -> ('src.opensuse.org', 'dimstar/vlc')
    """
    url = remote_url.strip()
    if "@" in url and ":" in url:
        host_part, path_part = url.split("@", 1)[1].split(":", 1)
        return host_part.lower(), path_part.strip("/").removesuffix(".git")
    parsed = urllib.parse.urlparse(url)
    return parsed.netloc.lower(), parsed.path.strip("/").removesuffix(".git")


def get_obs_scmsync_commit(project: str, package: str) -> Optional[str]:
    """
    Extracts the latest Git commit SHA recorded in OBS for a scmsync-managed package.
    Inspects osc log --limit 1 (for [info=<sha>]), with fallback to _scmsync.obsinfo.
    """
    pkg_base = package.split("/")[-1]
    env = get_obs_env()
    try:
        res = subprocess.run(
            ["osc", "log", "--limit", "1", project, pkg_base],
            env=env,
            capture_output=True,
            text=True,
            timeout=10
        )
        if res.returncode == 0:
            m = re.search(r"\[info=([a-f0-9]{40,64})\]", res.stdout)
            if m:
                return m.group(1)
    except Exception:
        pass

    try:
        res2 = subprocess.run(
            ["osc", "api", f"/source/{project}/{pkg_base}/_scmsync.obsinfo"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10
        )
        if res2.returncode == 0:
            for line in res2.stdout.splitlines():
                if line.startswith("commit:"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass

    return None


def verify_scmsync_provenance(
    package_dir: str,
    source_project: str,
    package_name: str,
    target_project: str = "openSUSE:Factory"
) -> Tuple[bool, str]:
    """
    Verifies that the local git repository checkout matches the <scmsync> declaration
    in source_project. If source_project does not track this repository/branch, aborts.
    """
    if not os.path.exists(os.path.join(package_dir, ".git")):
        return True, "OSC checkout"

    pkg_base = package_name.split("/")[-1]
    env = get_obs_env()

    cache_key = (source_project, pkg_base)
    scmsync_url = None
    if cache_key in _SCMSYNC_CACHE:
        scmsync_url = _SCMSYNC_CACHE[cache_key]
    else:
        try:
            res = subprocess.run(
                ["osc", "meta", "pkg", source_project, pkg_base],
                env=env,
                capture_output=True,
                text=True,
                timeout=10
            )
            if res.returncode == 0 and res.stdout.strip():
                try:
                    root = ET.fromstring(res.stdout)
                    elem = root.find("scmsync")
                    if elem is not None and elem.text:
                        scmsync_url = elem.text.strip()
                except Exception:
                    pass
        except Exception:
            pass
        _SCMSYNC_CACHE[cache_key] = scmsync_url

    if not scmsync_url:
        return False, (
            f"Cannot detect what to submit where: devel project '{source_project}' for '{pkg_base}' "
            f"does not configure an <scmsync> Git target in OBS package metadata."
        )

    scm_host, scm_repo, scm_branch = parse_scmsync(scmsync_url)

    try:
        rem_res = subprocess.run(
            ["git", "-C", package_dir, "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True
        )
        local_host, local_repo = parse_git_remote_url(rem_res.stdout.strip())
    except Exception as e:
        return False, f"Failed to determine git remote 'origin' for {package_dir}: {e}"

    try:
        br_res = subprocess.run(
            ["git", "-C", package_dir, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, check=True
        )
        local_branch = br_res.stdout.strip()
    except Exception:
        local_branch = ""

    if scm_host != local_host or scm_repo != local_repo:
        return False, (
            f"Cannot detect what to submit where: devel project '{source_project}' for '{pkg_base}' "
            f"synchronizes from '{scm_repo}' ({scmsync_url}), which does not match current repository "
            f"('{local_repo}' on origin)."
        )

    if scm_branch and local_branch and scm_branch != local_branch:
        return False, (
            f"Cannot detect what to submit where: devel project '{source_project}' for '{pkg_base}' "
            f"synchronizes branch '{scm_branch}', which does not match current branch '{local_branch}'."
        )

    return True, "Provenance verified"

def is_project_git_managed(
    project: str = "openSUSE:Factory",
    package_name: Optional[str] = None,
    timeout: int = 15,
    force_refresh: bool = False
) -> Dict[str, Any]:
    """
    Checks if an OBS project or package is managed via Git by inspecting its
    project metadata (and optionally package metadata) for an <scmsync> declaration.
    When openSUSE:Factory acquires an scmsync attribute, it is considered migrated
    to Git and submissions transition automatically from osc sr to Gitea PRs.
    Returns: {"is_git": bool, "scmsync_url": Optional[str]}
    """
    now = time.time()
    cache_key = f"{project}:{package_name or 'all'}"
    if not force_refresh and cache_key in _META_CACHE:
        cached = _META_CACHE[cache_key]
        if (now - cached.get("timestamp", 0)) < META_CACHE_TTL:
            return cached["result"]

    # 1. Inspect project-level metadata
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
            _META_CACHE[cache_key] = {"timestamp": now, "result": result}
            return result
    except Exception:
        pass

    # 2. Inspect package-level metadata
    if package_name:
        pkg_base = package_name.split("/")[-1]
        try:
            res_pkg = subprocess.run(
                ["osc", "api", f"/source/{project}/{pkg_base}/_meta"],
                env=get_obs_env(),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True
            )
            root_pkg = ET.fromstring(res_pkg.stdout)
            scmsync_el = root_pkg.find("scmsync")
            if scmsync_el is not None and scmsync_el.text and scmsync_el.text.strip():
                result = {"is_git": True, "scmsync_url": scmsync_el.text.strip()}
                _META_CACHE[cache_key] = {"timestamp": now, "result": result}
                return result
        except Exception:
            pass

    result = {"is_git": False, "scmsync_url": None}
    _META_CACHE[cache_key] = {"timestamp": now, "result": result}
    return result


def parse_obs_requests_collection(xml_content: str, target_project: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
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

            if (target_project is None or t_prj == target_project) and t_pkg:
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
                if src_pkg and src_pkg != t_pkg:
                    requests_by_pkg.setdefault(src_pkg, []).append(req_info)

    return requests_by_pkg


def get_obs_user() -> Optional[str]:
    """Resolves the current OBS username from ~/.config/osc/oscrc or environment."""
    osc_paths = [
        os.path.expanduser("~/.config/osc/oscrc"),
        os.path.expanduser("~/.oscrc")
    ]
    for p in osc_paths:
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    c = f.read()
                m = re.search(r"^\s*user\s*=\s*(\S+)", c, re.MULTILINE)
                if m:
                    return m.group(1).strip()
            except Exception:
                pass
    return os.environ.get("USER") or os.environ.get("LOGNAME")


def get_pending_obs_requests(
    target_project: Optional[str] = "openSUSE:Factory",
    user: Optional[str] = None,
    timeout: int = 25,
    force_refresh: bool = False
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Fetches all pending submit requests (states=review,new) targeting target_project
    and/or created by user across all devel projects.
    Returns: Dict[package_name, List[request_dict]]
    """
    now = time.time()
    cache_key = f"{target_project or ''}:{user or ''}"
    if not force_refresh and cache_key in _PENDING_REQUESTS_CACHE.get("data", {}):
        cached_entry = _PENDING_REQUESTS_CACHE["data"][cache_key]
        if (now - cached_entry.get("timestamp", 0)) < PENDING_CACHE_TTL:
            return cached_entry["requests"]

    parsed = {}
    seen_req_ids = set()

    # 1. Query target project requests if specified
    if target_project:
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
            parsed_prj = parse_obs_requests_collection(res.stdout, target_project=target_project)
            for pkg, req_list in parsed_prj.items():
                for r in req_list:
                    seen_req_ids.add(r.get("id"))
                parsed[pkg] = list(req_list)
        except Exception:
            pass

    # 2. Query user's outgoing requests across all projects if specified
    if user:
        cmd_u = [
            "osc", "api",
            f"/request?view=collection&user={user}&states=review,new&types=submit"
        ]
        try:
            res_u = subprocess.run(
                cmd_u,
                env=get_obs_env(),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True
            )
            parsed_u = parse_obs_requests_collection(res_u.stdout)
            for pkg, req_list in parsed_u.items():
                for r in req_list:
                    r_id = r.get("id")
                    if r_id not in seen_req_ids:
                        seen_req_ids.add(r_id)
                        parsed.setdefault(pkg, []).append(r)
        except Exception:
            pass

    if "data" not in _PENDING_REQUESTS_CACHE:
        _PENDING_REQUESTS_CACHE["data"] = {}
    _PENDING_REQUESTS_CACHE["data"][cache_key] = {
        "timestamp": now,
        "requests": parsed
    }
    return parsed


def invalidate_obs_requests_cache(project: Optional[str] = None) -> None:
    """
    Invalidates the module-level caches for pending and accepted OBS requests.
    """
    global _PENDING_REQUESTS_CACHE, _ACCEPTED_REQUESTS_CACHE, _META_CACHE
    if project:
        if "data" in _PENDING_REQUESTS_CACHE and project in _PENDING_REQUESTS_CACHE["data"]:
            del _PENDING_REQUESTS_CACHE["data"][project]
        if "data" in _ACCEPTED_REQUESTS_CACHE and project in _ACCEPTED_REQUESTS_CACHE["data"]:
            del _ACCEPTED_REQUESTS_CACHE["data"][project]
        if project in _META_CACHE:
            del _META_CACHE[project]
    else:
        _PENDING_REQUESTS_CACHE = {"time": 0.0, "data": {}}
        _ACCEPTED_REQUESTS_CACHE = {"time": 0.0, "data": {}}
    _SRCMD5_CACHE.clear()
    _STREAM_SYNC_CACHE.clear()


def get_package_pending_obs_requests(
    package_name: str,
    target_project: Optional[str] = None,
    source_project: Optional[str] = None,
    timeout: int = 10
) -> List[Dict[str, Any]]:
    """
    Directly queries OBS for active pending submit requests for a specific package.
    Supports querying by target project, source project, or both.
    Fast (~0.3s) and always returns authoritative live state.
    """
    pkg_base = package_name.split("/")[-1]
    conditions = [
        "(state/@name='new' or state/@name='review')",
        f"(action/target/@package='{pkg_base}' or action/source/@package='{pkg_base}')"
    ]
    if target_project and not source_project:
        conditions.append(f"action/target/@project='{target_project}'")
    elif source_project and not target_project:
        conditions.append(f"action/source/@project='{source_project}'")
    elif source_project and target_project:
        conditions.append(f"(action/target/@project='{target_project}' or action/source/@project='{source_project}')")

    query = " and ".join(conditions)
    cmd = ["osc", "api", f"/search/request?match={query}"]
    try:
        res = subprocess.run(cmd, env=get_obs_env(), capture_output=True, text=True, timeout=timeout)
        if res.returncode == 0 and res.stdout.strip():
            parsed = parse_obs_requests_collection(res.stdout, target_project=None)
            return parsed.get(pkg_base, []) or parsed.get(package_name, [])
    except Exception:
        pass
    return []


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
    stable_branch: str = "factory",
    target_project: str = "openSUSE:Factory"
) -> bool:
    """
    Checks whether a recently accepted submit request should be considered completed/synced,
    meaning the package is ready for new submissions or has new unsubmitted changes.
    Returns:
    - True if real unsubmitted packaging differences exist in OBS (new changes ready to submit),
      if the local origin ref is already in sync with pool, or if the accepted changes have
      already landed in refs/pool/{stable_branch}.
    - False if there are no new differences between source and target in OBS and pool ref is
      behind origin (meaning it is truly waiting for the central pool Git mirror to catch up).
    """
    if not os.path.isdir(pkg_dir):
        return False

    pkg_name = os.path.basename(os.path.abspath(pkg_dir)).split("/")[-1]
    src_prj = sr.get("source_project") or resolve_source_obs_project(pkg_dir, default="", package_name=pkg_name, target_project=target_project)

    # 1. Primary check: check OBS ground truth differences
    if src_prj:
        has_diff, _ = has_real_obs_diff(src_prj, pkg_name, target_project=target_project)
        if has_diff:
            return True

    # 2. Check if local origin ref is already in sync with pool ref
    ensure_origin_ref_fresh(pkg_dir, branch=stable_branch)
    ensure_pool_ref_fresh(pkg_dir, stable_branch=stable_branch)
    pool_ref = f"refs/pool/{stable_branch}"
    origin_ref = f"origin/{stable_branch}"
    try:
        cnt_res = subprocess.run(
            ["git", "-C", pkg_dir, "rev-list", f"{pool_ref}..{origin_ref}", "--count"],
            capture_output=True, text=True
        )
        if cnt_res.returncode == 0 and cnt_res.stdout.strip() == "0":
            return True
    except Exception:
        pass

    # 3. Check if request ID landed in pool git ref
    sr_id = sr.get("id")
    if sr_id:
        try:
            res_id = subprocess.run(
                ["git", "-C", pkg_dir, "log", pool_ref, "-n", "30", f"--grep={sr_id}"],
                capture_output=True, text=True
            )
            if res_id.returncode == 0 and res_id.stdout.strip():
                return True
        except Exception:
            pass

    # 4. Check if the SR description landed in pool git ref
    desc = sr.get("description", "").strip()
    if desc:
        try:
            for line in desc.splitlines():
                line = line.strip()
                if line.startswith(("- ", "* ", "+ ")):
                    line = line[2:].strip()
                line = line.rstrip(":. ").strip()
                if line and not line.startswith("---") and "@" not in line and len(line) > 5:
                    res = subprocess.run(
                        ["git", "-C", pkg_dir, "log", pool_ref, "-n", "30", "--grep=" + line[:50]],
                        capture_output=True, text=True
                    )
                    if res.returncode == 0 and res.stdout.strip():
                        return True
                    break
        except Exception:
            pass

    return False

def get_package_pool_submission_status(
    package_name: str,
    target_project: Optional[str] = "openSUSE:Factory",
    pending_requests: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    accepted_requests: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    allow_accepted_query: bool = True,
    pkg_dir: Optional[str] = None,
    stable_branch: str = "factory",
    force_refresh: bool = False,
    check_srcmd5: bool = True,
    source_project: Optional[str] = None
) -> Dict[str, Any]:
    """
    Evaluates whether a package currently has an active pending or recently accepted
    submit request against target_project or from source_project.

    Returns dict:
    - status: "pending" | "pending_outdated" | "accepted_pending_sync" | "unsubmitted"
    - request: Dict representing the matching request (if any)
    """
    pkg_base = package_name.split("/")[-1]
    if pkg_dir and not source_project:
        source_project = resolve_source_obs_project(pkg_dir, default="", package_name=package_name)

    if force_refresh:
        pending_list = get_package_pending_obs_requests(package_name, target_project=target_project, source_project=source_project)
    else:
        if not isinstance(pending_requests, dict):
            pending_requests = get_pending_obs_requests(target_project=target_project or "openSUSE:Factory")
        pending_list = pending_requests.get(package_name) or pending_requests.get(pkg_base) or []

        # If not found in target_project cache and source_project is known (e.g. scratch checkout targeting a devel project)
        if not pending_list and source_project:
            pending_list = get_package_pending_obs_requests(package_name, target_project=None, source_project=source_project)

    if not isinstance(pkg_dir, str):
        pkg_dir = None
    if not isinstance(stable_branch, str):
        stable_branch = "factory"

    if pending_list:
        # Sort newest ID first
        sorted_reqs = sorted(pending_list, key=lambda r: int(r.get("id") or 0), reverse=True)
        primary = sorted_reqs[0]
        sr_rev = primary.get("source_rev")
        src_prj = primary.get("source_project") or source_project
        tgt_prj = primary.get("target_project") or target_project or "openSUSE:Factory"

        is_outdated = False
        if check_srcmd5 and src_prj and sr_rev:
            cur_md5 = get_source_package_srcmd5(src_prj, package_name, force_refresh=force_refresh)
            if cur_md5 and cur_md5 != sr_rev:
                # If cur_md5 != sr_rev, check whether sr_rev matches other active revisions
                # (e.g. short rev number '7' vs long srcmd5, or linkinfo expanded xsrcmd5)
                valid_revs = get_source_package_revisions(src_prj, package_name, force_refresh=force_refresh)
                if valid_revs:
                    if str(sr_rev) not in valid_revs:
                        is_outdated = True
                else:
                    is_outdated = True

        return {
            "status": "pending_outdated" if is_outdated else "pending",
            "request": primary,
            "requests": sorted_reqs,
            "state": primary.get("state", "review"),
            "request_id": primary.get("id"),
            "source_project": src_prj,
            "source_rev": sr_rev,
            "target_project": tgt_prj,
            "is_outdated": is_outdated,
        }

    if allow_accepted_query:
        if not isinstance(accepted_requests, dict):
            accepted_requests = get_recently_accepted_obs_requests(target_project=target_project)

        accepted_list = accepted_requests.get(package_name) or accepted_requests.get(pkg_base) or []

        if accepted_list:
            sorted_acc = sorted(accepted_list, key=lambda r: int(r.get("id") or 0), reverse=True)
            primary = sorted_acc[0]

            # Verify if this accepted request has ALREADY synced to pool or if new unsubmitted changes exist
            if pkg_dir and is_accepted_sr_already_synced(pkg_dir, primary, stable_branch=stable_branch, target_project=target_project):
                return {
                    "status": "unsubmitted",
                    "request": None,
                    "requests": [],
                    "state": None,
                    "request_id": None,
                    "source_project": None,
                }

            tgt_prj = primary.get("target_project") or target_project or "openSUSE:Factory"
            return {
                "status": "accepted_pending_sync",
                "request": primary,
                "requests": sorted_acc,
                "state": "accepted",
                "request_id": primary.get("id"),
                "source_project": primary.get("source_project"),
                "target_project": tgt_prj,
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


_STREAM_SYNC_CACHE: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
STREAM_SYNC_CACHE_TTL: float = 60.0

def check_pool_stream_sync(
    pkg_dir: str,
    branch: str,
    pkg_name: Optional[str] = None,
    skip_fetch: bool = False,
    force_refresh: bool = False
) -> Dict[str, Any]:
    """
    Checks synchronization status between origin/<branch> and refs/pool/<branch>
    for any pool stream (e.g. factory, leap-16.0, leap-16.1).
    Cached for 60 seconds. skip_fetch=True compares existing local refs in ~2ms without network fetch.
    Returns dict: {"exists": bool, "ahead": int, "behind": int}
    """
    if not os.path.isdir(pkg_dir):
        return {"exists": False, "ahead": 0, "behind": 0}

    cache_key = (os.path.abspath(pkg_dir), branch)
    now = time.time()
    if not force_refresh and cache_key in _STREAM_SYNC_CACHE:
        ts, val = _STREAM_SYNC_CACHE[cache_key]
        if (now - ts) < STREAM_SYNC_CACHE_TTL:
            return val

    name = pkg_name or os.path.basename(os.path.abspath(pkg_dir))
    pool_url = f"https://src.opensuse.org/pool/{name}.git"

    target_pool_branch = branch
    if branch.startswith("for-"):
        suffix = branch.removeprefix("for-")
        if suffix.startswith("leap-") or suffix.startswith("slfo-"):
            target_pool_branch = suffix
        else:
            target_pool_branch = "factory"

    if not skip_fetch:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        try:
            fetch_res = subprocess.run(
                ["git", "-C", pkg_dir, "fetch", "--quiet", pool_url, f"+refs/heads/{target_pool_branch}:refs/pool/{target_pool_branch}"],
                env=env, capture_output=True, text=True, timeout=10
            )
            if fetch_res.returncode != 0:
                res = {"exists": False, "ahead": 0, "behind": 0}
                _STREAM_SYNC_CACHE[cache_key] = (now, res)
                return res
        except Exception:
            res = {"exists": False, "ahead": 0, "behind": 0}
            _STREAM_SYNC_CACHE[cache_key] = (now, res)
            return res

    ahead = 0
    behind = 0
    local_ref = branch if branch.startswith("for-") else f"origin/{branch}"
    try:
        cnt_a = subprocess.run(
            ["git", "-C", pkg_dir, "rev-list", f"refs/pool/{target_pool_branch}..{local_ref}", "--count"],
            capture_output=True, text=True
        )
        if cnt_a.returncode == 0:
            ahead = int(cnt_a.stdout.strip() or "0")
    except Exception:
        pass

    try:
        cnt_b = subprocess.run(
            ["git", "-C", pkg_dir, "rev-list", f"{local_ref}..refs/pool/{target_pool_branch}", "--count"],
            capture_output=True, text=True
        )
        if cnt_b.returncode == 0:
            behind = int(cnt_b.stdout.strip() or "0")
    except Exception:
        pass

    res = {"exists": True, "ahead": ahead, "behind": behind, "target_pool_branch": target_pool_branch}
    _STREAM_SYNC_CACHE[cache_key] = (now, res)
    return res


_SRCMD5_CACHE: Dict[Tuple[str, str], Tuple[float, Optional[str]]] = {}
_PACKAGE_REVISIONS_CACHE: Dict[Tuple[str, str], Tuple[float, Set[str]]] = {}
SRCMD5_CACHE_TTL: float = 60.0
REVISIONS_CACHE_TTL: float = 60.0

def get_source_package_srcmd5(source_project: str, package_name: str, force_refresh: bool = False) -> Optional[str]:
    """
    Returns the current srcmd5 checksum of source_project/package_name in OBS.
    Cached for 60 seconds to prevent blocking UI threads.
    """
    pkg_base = package_name.split("/")[-1]
    cache_key = (source_project, pkg_base)
    now = time.time()
    if not force_refresh and cache_key in _SRCMD5_CACHE:
        ts, val = _SRCMD5_CACHE[cache_key]
        if (now - ts) < SRCMD5_CACHE_TTL:
            return val

    env = get_obs_env()
    md5 = None
    try:
        res = subprocess.run(
            ["osc", "api", f"/source/{source_project}/{pkg_base}"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10
        )
        if res.returncode == 0 and res.stdout.strip():
            root = ET.fromstring(res.stdout)
            md5 = root.get("srcmd5")
    except Exception:
        pass

    _SRCMD5_CACHE[cache_key] = (now, md5)
    return md5


def get_source_package_revisions(source_project: str, package_name: str, force_refresh: bool = False) -> Set[str]:
    """
    Returns the set of active source revisions and checksums (rev, vrev, srcmd5,
    xsrcmd5, lsrcmd5, baserev) for source_project/package_name in OBS.
    Handles both short revisions (e.g. '7') and long MD5 hashes (e.g. 32 hex chars).
    Cached for 60 seconds to prevent blocking UI threads.
    """
    pkg_base = package_name.split("/")[-1]
    cache_key = (source_project, pkg_base)
    now = time.time()
    if not force_refresh and cache_key in _PACKAGE_REVISIONS_CACHE:
        ts, val = _PACKAGE_REVISIONS_CACHE[cache_key]
        if (now - ts) < REVISIONS_CACHE_TTL:
            return val

    env = get_obs_env()
    valid_revs = set()
    try:
        res = subprocess.run(
            ["osc", "api", f"/source/{source_project}/{pkg_base}"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10
        )
        if res.returncode == 0 and res.stdout.strip():
            root = ET.fromstring(res.stdout)
            for attr in ("rev", "vrev", "srcmd5"):
                v = root.get(attr)
                if v:
                    valid_revs.add(str(v))
            linkinfo = root.find("linkinfo")
            if linkinfo is not None:
                for attr in ("xsrcmd5", "lsrcmd5", "srcmd5", "baserev"):
                    v = linkinfo.get(attr)
                    if v:
                        valid_revs.add(str(v))
    except Exception:
        pass

    _PACKAGE_REVISIONS_CACHE[cache_key] = (now, valid_revs)
    return valid_revs


def get_git_obs_meta_project(package_dir: str, branch: Optional[str] = None) -> Optional[str]:
    """
    Reads the OBS project configured in .git/obs/<branch>/meta.json (if present).
    This provides a cheap, local, zero-network lookup for Git checkouts configured via git-obs.
    """
    if not os.path.exists(os.path.join(package_dir, ".git")):
        return None

    active_br = branch
    if not active_br:
        try:
            res = subprocess.run(
                ["git", "-C", package_dir, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=5
            )
            if res.returncode == 0:
                active_br = res.stdout.strip()
        except Exception:
            pass

    if not active_br:
        return None

    meta_file = os.path.join(package_dir, ".git", "obs", active_br, "meta.json")
    if os.path.isfile(meta_file):
        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            proj = data.get("project")
            if proj and isinstance(proj, str) and proj.strip():
                return proj.strip()
        except Exception:
            pass

    return None


def resolve_package_linkinfo_target_project(pkg_dir: str) -> Optional[str]:
    """
    Inspects .osc/_files XML to extract the linked upstream project if package is a branch/link.
    Returns upstream target project (e.g. 'M17N' or 'openSUSE:Factory') or None.
    """
    if not pkg_dir or not os.path.isdir(pkg_dir):
        return None
    osc_files = os.path.join(pkg_dir, ".osc", "_files")
    if os.path.isfile(osc_files):
        try:
            with open(osc_files, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(2048)
            m = re.search(r'<linkinfo\s+[^>]*project=["\']([^"\']+)["\']', content)
            if m:
                return m.group(1).strip()
        except Exception:
            pass
    return None


def resolve_source_obs_project(
    package_dir: str,
    default: str = "GNOME:Factory",
    package_name: Optional[str] = None,
    target_project: str = "openSUSE:Factory",
    branch: Optional[str] = None
) -> str:
    """
    Determines the source OBS project to submit from.
    Inspects:
    1. staging.config in package_dir or parent directories (ObsProject key)
    2. .osc/_project
    3. .git/obs/<branch>/meta.json (cheap local lookup from git-obs)
    4. OBS develproject metadata for target_project / package_name
    5. Fallback default
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

    # Check .git/obs/<branch>/meta.json (cheap local lookup)
    git_obs_prj = get_git_obs_meta_project(package_dir, branch=branch)
    if git_obs_prj:
        return git_obs_prj

    # Interrogate OBS develproject metadata
    pkg_name = package_name or os.path.basename(os.path.abspath(package_dir))
    if pkg_name:
        pkg_base = pkg_name.split("/")[-1]
        cache_key = (target_project, pkg_base)
        if cache_key in _DEVEL_PROJECT_CACHE:
            cached_val = _DEVEL_PROJECT_CACHE[cache_key]
            if cached_val:
                return cached_val
        else:
            try:
                res = subprocess.run(
                    ["osc", "develproject", target_project, pkg_base],
                    capture_output=True, text=True, timeout=10, env=get_obs_env()
                )
                if res.returncode == 0 and res.stdout.strip():
                    dp = res.stdout.strip().split("/")[0].strip()
                    if dp and not dp.lower().startswith("error"):
                        _DEVEL_PROJECT_CACHE[cache_key] = dp
                        return dp
            except Exception:
                pass
            _DEVEL_PROJECT_CACHE[cache_key] = ""

    return default


def extract_pool_changes(package_dir: str, stable_branch: str = "factory") -> str:
    """
    is_osc = os.path.isdir(os.path.join(package_dir, ".osc")) and not os.path.exists(os.path.join(package_dir, ".git"))
    if is_osc:
        for f in os.listdir(package_dir):
            if f.endswith(".changes"):
                try:
                    with open(os.path.join(package_dir, f), "r", encoding="utf-8", errors="replace") as fh:
                        lines = []
                        in_entry = False
                        for line in fh:
                            if line.startswith("-------------------------------------------------------------------"):
                                if in_entry:
                                    break
                                in_entry = True
                                continue
                            if in_entry:
                                lines.append(line.rstrip())
                        txt = "\n".join(lines).strip()
                        if txt:
                            return txt
                except Exception:
                    pass
        return f"Update {os.path.basename(package_dir)}"

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
    Discovers all packages in a workspace using sync_backend.discover_workspace_packages.
    """
    try:
        from . import sync_backend as sb
        return sb.discover_workspace_packages(workspace_path)
    except ImportError:
        import sync_backend as sb
        return sb.discover_workspace_packages(workspace_path)


def create_gitea_pool_pr(
    package_dir: str,
    package_name: str,
    target_project: str,
    scmsync_url: str,
    title: str,
    description: str,
    stable_branch: str = "factory",
    dry_run: bool = False
) -> Tuple[bool, str]:
    """
    Creates a Gitea Pull Request when target_project is Git-managed via <scmsync>.
    Parses the target repository and branch from scmsync_url, verifies local
    pushed status on origin, and submits via tea CLI or Gitea compare URL.
    """
    scm_host, scm_repo, scm_branch = parse_scmsync(scmsync_url)
    target_branch = scm_branch or stable_branch
    target_repo = scm_repo
    if target_repo.endswith("/_ObsPrj"):
        org_name = target_repo.split("/")[0]
        target_repo = f"{org_name}/{package_name}"

    try:
        rem_res = subprocess.run(
            ["git", "-C", package_dir, "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True
        )
        local_host, local_repo = parse_git_remote_url(rem_res.stdout.strip())
    except Exception as e:
        return False, f"Could not determine git origin for {package_dir}: {e}"

    try:
        br_res = subprocess.run(
            ["git", "-C", package_dir, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, check=True
        )
        local_branch = br_res.stdout.strip()
    except Exception:
        local_branch = stable_branch

    local_user = local_repo.split("/")[0] if "/" in local_repo else local_repo
    compare_url = f"https://{scm_host or 'src.opensuse.org'}/{target_repo}/compare/{target_branch}...{local_user}:{local_branch}"

    if dry_run:
        return True, (
            f"[DRY-RUN] Target {target_project} is Git-managed via <scmsync>.\n"
            f"   Would create Gitea Pull Request from '{local_user}:{local_branch}' to '{target_repo}:{target_branch}':\n"
            f"   Title:       {title}\n"
            f"   Compare URL: {compare_url}\n"
            f"   Description:\n{description}"
        )

    import shutil
    tea_bin = shutil.which("tea")
    if tea_bin:
        cmd = [
            tea_bin, "pulls", "create",
            "--repo", target_repo,
            "--head", f"{local_user}:{local_branch}",
            "--base", target_branch,
            "--title", title,
            "--description", description
        ]
        try:
            res = subprocess.run(cmd, cwd=package_dir, capture_output=True, text=True, timeout=30)
            if res.returncode == 0:
                out = res.stdout.strip()
                return True, f"Successfully created Gitea Pull Request for {package_name} targeting {target_repo}:{target_branch}!\n{out}"
            else:
                err = (res.stderr or res.stdout).strip()
                return False, f"Failed to create PR via tea ({err}). Submit manually at:\n   {compare_url}"
        except Exception as e:
            return False, f"Failed executing tea ({e}). Submit manually at:\n   {compare_url}"
    return False, (
        f"Target {target_project} is Git-managed via <scmsync>.\n"
        f"   'tea' CLI is not installed to automate PR creation. Submit via Gitea Pull Request at:\n"
        f"   {compare_url}"
    )


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

    # Handle OSC checkouts (.osc without .git)
    is_osc = os.path.isdir(os.path.join(package_dir, ".osc")) and not os.path.exists(os.path.join(package_dir, ".git"))
    if is_osc:
        try:
            from .sync_backend import check_osc_worktree_fast
        except ImportError:
            from sync_backend import check_osc_worktree_fast
        dirty, untracked = check_osc_worktree_fast(package_dir)
        if dirty:
            print(f"ℹ️  Note: Working tree has uncommitted modifications ('osc status'). Submission will act on the remote state in {target_project}.", file=sys.stderr)

        source_project = resolve_source_obs_project(package_dir, default="")
        if not source_project:
            return False, f"Could not determine source OBS project for '{package_name}'."

        if not force:
            try:
                rdiff = subprocess.run(
                    ["osc", "rdiff", target_project, package_name, source_project],
                    capture_output=True, text=True, timeout=15, env=get_obs_env()
                )
                if rdiff.returncode == 0 and not rdiff.stdout.strip():
                    return False, f"Package '{package_name}' is already in sync with {target_project} (no differences between {source_project} and {target_project})."
            except Exception:
                pass

        status_info = get_package_pool_submission_status(package_name, target_project=target_project)
        supersede_id = None
        if status_info["status"] == "pending_outdated":
            supersede_id = status_info["request_id"]
            print(f"ℹ️  Note: Source package in {source_project} has newer changes than pending SR #{supersede_id}. Superseding request #{supersede_id}...", file=sys.stderr)
        elif status_info["status"] == "pending" and not force:
            req_id = status_info["request_id"]
            return False, f"Active submit request #{req_id} is already pending against {target_project} and up-to-date with latest sources."
        if status_info["status"] == "accepted_pending_sync" and not force:
            req_id = status_info["request_id"]
            return False, f"Submit request #{req_id} was already accepted; awaiting backend sync to central pool."

        if not message or not message.strip():
            message = extract_pool_changes(package_dir, stable_branch=stable_branch) or f"Update {package_name} from {source_project}"

        cmd = ["osc", "sr"]
        if supersede_id:
            cmd.extend(["-s", str(supersede_id), "--yes"])
        cmd.extend([source_project, package_name, target_project, "-m", message])

        if dry_run:
            act_note = f"Would supersede pending SR #{supersede_id}: " if supersede_id else "Would execute: "
            return True, f"[DRY-RUN] {act_note}{' '.join(cmd)}"

        try:
            env = dict(os.environ)
            if "DBUS_SESSION_BUS_ADDRESS" not in env:
                xdg_runtime = env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
                bus_path = os.path.join(xdg_runtime, "bus")
                if os.path.exists(bus_path):
                    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus_path}"

            res = subprocess.run(cmd, capture_output=True, text=True, check=True, env=get_obs_env())
            out = res.stdout.strip()
            return True, out if out else f"Submitted {package_name} to {target_project} via osc sr."
        except subprocess.CalledProcessError as e:
            err = (e.stderr or e.stdout or str(e)).strip()
            return False, f"osc sr failed (code {e.returncode}): {err}"

    # 0. Detect active branch for git checkouts
    active_branch = stable_branch
    if os.path.exists(os.path.join(package_dir, ".git")):
        try:
            br_res = subprocess.run(
                ["git", "-C", package_dir, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, check=True
            )
            active_branch = br_res.stdout.strip() or stable_branch
        except Exception:
            pass

    # 0b. If submitting to a central pool release stream (e.g. leap-16.1), route directly to Gitea PR pipeline!
    is_pool_stream = target_project.startswith("pool/")
    if not is_pool_stream and active_branch != "factory" and os.path.exists(os.path.join(package_dir, ".git")):
        stream_info = check_pool_stream_sync(package_dir, branch=active_branch, pkg_name=package_name)
        if stream_info.get("exists"):
            is_pool_stream = True

    if is_pool_stream:
        ensure_pool_ref_fresh(package_dir, stable_branch=active_branch)

        # Check unpushed commits on active stream branch
        try:
            ahead_res = subprocess.run(
                ["git", "-C", package_dir, "rev-list", f"origin/{active_branch}..HEAD", "--count"],
                capture_output=True, text=True, check=True
            )
            unpushed = int(ahead_res.stdout.strip() or "0")
            if unpushed > 0:
                print(f"ℹ️  Note: Local branch is ahead of origin/{active_branch} by {unpushed} unpushed commit('s' if unpushed != 1 else ''). Submission will act on the pushed state on origin.", file=sys.stderr)
        except Exception:
            pass

        # Check pool ahead count against central pool stream
        pool_ref = f"refs/pool/{active_branch}"
        pool_ahead = 0
        try:
            cnt_res = subprocess.run(
                ["git", "-C", package_dir, "rev-list", f"{pool_ref}..origin/{active_branch}", "--count"],
                capture_output=True, text=True, check=True
            )
            pool_ahead = int(cnt_res.stdout.strip() or "0")
        except Exception:
            pass

        if pool_ahead == 0 and not force:
            return False, f"Package '{package_name}' on branch '{active_branch}' is already in sync with central pool ({pool_ahead} commits ahead)."

        description = message or extract_pool_changes(package_dir, stable_branch=active_branch)
        title = message.splitlines()[0] if message else ""
        if not title:
            if description and description.strip().startswith("- "):
                title = description.strip().splitlines()[0][2:].strip()
            else:
                title = f"Sync {package_name} on {active_branch}"

        return create_gitea_pool_pr(
            package_dir=package_dir,
            package_name=package_name,
            target_project=f"pool/{package_name}",
            scmsync_url=f"https://src.opensuse.org/pool/{package_name}#{active_branch}",
            title=title,
            description=description or "",
            stable_branch=active_branch,
            dry_run=dry_run
        )

    # 1. Guarantee local refs match authoritative remotes on src.opensuse.org
    ensure_origin_ref_fresh(package_dir, branch=stable_branch)
    ensure_pool_ref_fresh(package_dir, stable_branch=stable_branch)

    # 2. Check working tree cleanliness (inform user if tracked files are modified, do not block)
    try:
        st_res = subprocess.run(
            ["git", "-C", package_dir, "status", "--porcelain"],
            capture_output=True, text=True, check=True
        )
        tracked_changes = [
            line for line in st_res.stdout.splitlines()
            if not line.startswith("??") and not line.startswith("!!")
        ]
        if tracked_changes:
            files_count = len(tracked_changes)
            print(f"ℹ️  Note: Working tree has uncommitted modifications to {files_count} tracked file{'s' if files_count != 1 else ''}. Submission will act on the pushed state on origin.", file=sys.stderr)
    except Exception:
        pass

    # 9. Check Pending and Recently Accepted Request Guards
    status_info = get_package_pool_submission_status(package_name, target_project=target_project, pkg_dir=package_dir, stable_branch=stable_branch)
    supersede_id = None
    if status_info["status"] == "pending_outdated":
        req = status_info["request"]
        req_id = req.get("id")
        src = req.get("source_project")
        supersede_id = req_id
        print(f"ℹ️  Note: Source package in {src} has newer changes than pending SR #{req_id}. Superseding request #{req_id}...", file=sys.stderr)
    elif status_info["status"] == "pending" and not force:
        req = status_info["request"]
        req_id = req.get("id")
        st = req.get("state")
        src = req.get("source_project")
        return False, f"Active submit request #{req_id} ({st} from {src}) is already pending against {target_project} and up-to-date with latest sources."

    if status_info["status"] == "accepted_pending_sync" and not force:
        req = status_info["request"]
        req_id = req.get("id")
        when = req.get("accepted_when", "recently")
        return False, f"Submit request #{req_id} was already accepted ({when}) and is currently pending backend sync to central pool."

    # 2b. Check if target is Git-managed (<scmsync> attribute in project or package metadata)
    # When openSUSE:Factory acquires an scmsync attribute, osc sr transitions automatically to Gitea PR!
    git_meta = is_project_git_managed(target_project, package_name=package_name)
    if git_meta.get("is_git"):
        if not message or not message.strip():
            message = extract_pool_changes(package_dir, stable_branch=stable_branch)
        title = message.splitlines()[0] if message else f"Update {package_name} in {target_project}"
        return create_gitea_pool_pr(
            package_dir=package_dir,
            package_name=package_name,
            target_project=target_project,
            scmsync_url=git_meta.get("scmsync_url", ""),
            title=title,
            description=message or "",
            stable_branch=stable_branch,
            dry_run=dry_run
        )

    # 3. If active branch is a release stream in central pool (e.g. leap-16.1), guide to PR instead of osc sr
    if active_branch != stable_branch:
        stream_info = check_pool_stream_sync(package_dir, branch=active_branch, pkg_name=package_name)
        if stream_info.get("exists"):
            return False, (
                f"Central pool stream '{active_branch}' is Git-managed (pool/{package_name}#{active_branch}).\n"
                f"   Submissions must be made via Gitea Pull Request rather than 'osc sr' to {target_project}.\n"
                f"   Create PR at: https://src.opensuse.org/pool/{package_name}/pulls"
            )

    # 4. Resolve source OBS project
    source_project = resolve_source_obs_project(package_dir, default="", package_name=package_name, target_project=target_project, branch=active_branch)
    if not source_project:
        return False, f"Could not determine source OBS project for '{package_name}' on branch '{active_branch}'."

    # 4. Verify SCM-sync provenance & branch compatibility BEFORE rev-list counts!
    ok_prov, prov_msg = verify_scmsync_provenance(package_dir, source_project, package_name, target_project=target_project)
    if not ok_prov and not force:
        return False, prov_msg

    # 5. Check if local commits are unpushed to origin for active branch (inform user, do not block)
    try:
        ahead_res = subprocess.run(
            ["git", "-C", package_dir, "rev-list", f"origin/{active_branch}..HEAD", "--count"],
            capture_output=True, text=True, check=True
        )
        unpushed = int(ahead_res.stdout.strip() or "0")
        if unpushed > 0:
            print(f"ℹ️  Note: Local branch is ahead of origin/{active_branch} by {unpushed} unpushed commit('s' if unpushed != 1 else ''). Submission will act on the pushed state on origin.", file=sys.stderr)
    except Exception:
        pass

    # 6. Check pool ahead count against target stable branch
    pool_ref = f"refs/pool/{stable_branch}"
    pool_ahead = 0
    try:
        cnt_res = subprocess.run(
            ["git", "-C", package_dir, "rev-list", f"{pool_ref}..origin/{stable_branch}", "--count"],
            capture_output=True, text=True, check=True
        )
        pool_ahead = int(cnt_res.stdout.strip() or "0")
    except Exception:
        pass

    # 8. Verify SCM-bridge sync completion (for Git checkouts)
    if os.path.exists(os.path.join(package_dir, ".git")):
        try:
            origin_commit = subprocess.run(
                ["git", "-C", package_dir, "rev-parse", f"origin/{stable_branch}"],
                capture_output=True, text=True, check=True
            ).stdout.strip()
            obs_commit = get_obs_scmsync_commit(source_project, package_name)
            if obs_commit and origin_commit and obs_commit != origin_commit and not force:
                return False, (
                    f"OBS package {source_project}/{package_name} has not finished syncing latest commit.\n"
                    f"   OBS synced commit: {obs_commit[:10]}\n"
                    f"   Remote origin:     {origin_commit[:10]}\n"
                    f"   Please wait for scmbridge to finish syncing to OBS before submitting."
                )
        except Exception:
            pass

    # 10. Verify real packaging differences in OBS
    if not force:
        has_diff, _ = has_real_obs_diff(source_project, package_name, target_project=target_project)
        if not has_diff and pool_ahead == 0:
            return False, f"Package '{package_name}' is already in sync with {target_project} (no differences between {source_project} and {target_project})."
    # 7. Extract message if not provided
    if not message or not message.strip():
        message = extract_pool_changes(package_dir, stable_branch=stable_branch)

    cmd = ["osc", "sr"]
    if supersede_id:
        cmd.extend(["-s", str(supersede_id), "--yes"])
    cmd.extend([source_project, package_name, target_project, "-m", message])

    if dry_run:
        act_note = f"Would supersede pending SR #{supersede_id}: " if supersede_id else "Would execute: "
        return True, f"[DRY-RUN] {act_note}{' '.join(cmd)}"

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
        "outdated_sr_packages": [],
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
            elif status_info["status"] == "pending_outdated":
                results["unsubmitted_packages"].append(pkg_info)
                results["outdated_sr_packages"].append(pkg_info)
            else:
                results["unsubmitted_packages"].append(pkg_info)

    return results
