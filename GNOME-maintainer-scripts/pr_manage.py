#!/usr/bin/env python3
"""
openSUSE GNOME Superproject (_ObsPrj) Pull Request Grouping & Management Tool.

Manages forwarded pull requests on src.opensuse.org (Gitea) for GNOME/_ObsPrj:
- Lists open PRs by target branch with host and peer package indicators.
- Combines individual forwarded PRs into staging group PRs for OBS testing.
- Enforces uniqueness: guarantees that any package name can only exist a SINGLE
  time in a combined PR (preventing duplicate or conflicting PRs for the same
  submodule from being grouped together).
- Reopens child PRs cleanly when unselecting or disintegrating groups.
- Signals staging merge approval with 'merge ok'.
"""

import os
import sys
import subprocess
import re
import json
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Set
from concurrent.futures import ThreadPoolExecutor

import yaml
import requests
from colorama import init, Fore, Style

# Initialize colorama for clean cross-platform terminal sequences
init(autoreset=True)

DEFAULT_REPO = "GNOME/_ObsPrj"
TOKEN_REGEX = re.compile(r"^\s*PR:\s*([^/\s]+)/([^!\s]+)!(\d+)\s*$", re.MULTILINE)
HEAD_PR_REGEX = re.compile(r"^PR_([^#\s]+)#(\d+)$")


# ---------------------------------------------------------------------
# DATA MODELS & TOKEN UTILITIES
# ---------------------------------------------------------------------
class PackageToken:
    def __init__(self, owner: str, package: str, pr_number: int, raw_line: str = ""):
        self.owner = owner
        self.package = package
        self.pr_number = pr_number
        self.raw_line = raw_line or f"PR: {owner}/{package}!{pr_number}"

    @property
    def key(self) -> str:
        """Case-insensitive package identifier."""
        return self.package.lower()

    def __repr__(self) -> str:
        return f"PackageToken({self.owner}/{self.package}!{self.pr_number})"

    def __eq__(self, other) -> bool:
        if isinstance(other, PackageToken):
            return self.key == other.key and self.pr_number == other.pr_number
        return False

    def __hash__(self) -> int:
        return hash((self.key, self.pr_number))


def extract_tokens(body: Optional[str]) -> List[PackageToken]:
    """Extracts all 'PR: <owner>/<package>!<pr_number>' tokens from the PR description."""
    if not body:
        return []
    tokens = []
    for line in body.splitlines():
        m = TOKEN_REGEX.match(line)
        if m:
            tokens.append(PackageToken(
                owner=m.group(1),
                package=m.group(2),
                pr_number=int(m.group(3)),
                raw_line=line.strip()
            ))
    return tokens


def get_host_package_from_pr(pr_data: dict) -> Optional[str]:
    """Extracts the host package name from the PR's immutable head reference (e.g. PR_zenity#4)."""
    head_ref = pr_data.get("head", {}).get("ref", "")
    m = HEAD_PR_REGEX.match(head_ref)
    if m:
        return m.group(1)
    # Fallback to first referenced token in body
    tokens = extract_tokens(pr_data.get("body", ""))
    if tokens:
        return tokens[0].package
    return None





def get_branch_color(branch_name: str) -> str:
    """Returns distinct colors for release lines to ease visual sorting."""
    if branch_name == "factory":
        return Fore.GREEN
    if branch_name == "next":
        return Fore.CYAN
    return Fore.YELLOW


# ---------------------------------------------------------------------
# CONFIGURATION & REPOSITORY RESOLUTION
# ---------------------------------------------------------------------
def is_valid_workspace_slug(slug: Optional[str]) -> bool:
    """Guards against saving arbitrary non-staging git repos (like openSUSE-helpers)."""
    if not slug or "/" not in slug:
        return False
    if "openSUSE-helpers" in slug or "github.com" in slug:
        return False
    return True


def is_staging_metaproject(directory: Path, remote_url: str, detected_repo: str) -> bool:
    """
    Validates whether a local directory and git remote represent an authentic
    openSUSE staging metaproject rather than an arbitrary git repository.
    Criteria:
    1. Remote URL must point to src.opensuse.org (or custom GITEA_URL).
    2. Must be an identifiable staging project:
       - Repo ends with _ObsPrj (e.g. GNOME/_ObsPrj, filesystems/_ObsPrj)
       - Or openSUSE/Factory
       - Or directory contains a valid workflow.config or _manifest file.
    """
    gitea_host = os.environ.get("GITEA_URL", "src.opensuse.org")
    if "://" in gitea_host:
        gitea_host = gitea_host.split("://")[1].split("/")[0]

    if gitea_host not in remote_url:
        return False

    if detected_repo.endswith("/_ObsPrj") or detected_repo.lower() == "opensuse/factory":
        return True

    if (directory / "workflow.config").is_file() or (directory / "_manifest").is_file():
        return True

    return False

CONFIG_PATH = Path.home() / ".config" / "pr-manage.json"


def load_config() -> Dict:
    """Loads user configuration from ~/.config/pr-manage.json, filtering out invalid slugs."""
    if CONFIG_PATH.is_file():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["workspaces"] = [w for w in data.get("workspaces", []) if is_valid_workspace_slug(w)]
            if not is_valid_workspace_slug(data.get("active_workspace")):
                data["active_workspace"] = "GNOME/_ObsPrj"
            return data
        except Exception:
            pass
    return {}


def save_config(active_workspace: Optional[str] = None, add_workspace: Optional[str] = None):
    """Persists active workspace and configured workspace list to ~/.config/pr-manage.json."""
    cfg = load_config()
    workspaces = cfg.get("workspaces", [])

    if add_workspace and is_valid_workspace_slug(add_workspace):
        w_clean = add_workspace.strip()
        if w_clean and w_clean not in workspaces:
            workspaces.append(w_clean)
        cfg["workspaces"] = workspaces

    if active_workspace and is_valid_workspace_slug(active_workspace):
        act = active_workspace.strip()
        cfg["active_workspace"] = act
        if act not in workspaces:
            workspaces.insert(0, act)
            cfg["workspaces"] = workspaces

    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass

def resolve_repository() -> str:
    """
    Resolves the target Gitea repository name (e.g. 'GNOME/_ObsPrj', 'KDE/_ObsPrj', 'openSUSE/Factory').
    Resolution Hierarchy:
    1. GITEA_REPO environment variable
    2. Local Git remote inspection ('git remote get-url origin') in current or parent directory
    3. workflow.config inspection ('GitProjectName') in current or parent directory
    4. Fallback default ('GNOME/_ObsPrj')
    """
    env_repo = os.environ.get("GITEA_REPO")
    if env_repo:
        return env_repo.strip()

    # Strategy 2: Interrogate local git remote in current or parent directory
    pattern = re.compile(r"[:/]([a-zA-Z0-9_\-\.:]+)/([a-zA-Z0-9_\-]+?)(?:\.git)?$")
    for check_dir in [Path.cwd(), Path.cwd().parent]:
        try:
            res = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=check_dir,
                capture_output=True,
                text=True,
                timeout=2
            )
            if res.returncode == 0:
                remote_url = res.stdout.strip()
                m = pattern.search(remote_url)
                if m:
                    detected = f"{m.group(1)}/{m.group(2)}"
                    if is_staging_metaproject(check_dir, remote_url, detected):
                        return detected
        except Exception:
            pass

    # Strategy 3: Check workflow.config in current or parent directory
    for check_dir in [Path.cwd(), Path.cwd().parent]:
        wf_cfg = check_dir / "workflow.config"
        if wf_cfg.is_file():
            try:
                with open(wf_cfg, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                proj = cfg.get("GitProjectName", "")
                if proj and "/" in proj:
                    return proj.split("#")[0].strip()
            except Exception:
                pass

    # Strategy 4: Active / Last-used workspace saved in ~/.config/pr-manage.json
    cfg = load_config()
    last_used = cfg.get("active_workspace")
    if last_used and "/" in last_used:
        return last_used.strip()

    return DEFAULT_REPO


def get_gitea_credentials(login_name: str = "OBS") -> Tuple[str, str]:
    """Extracts Gitea API URL and token from tea configuration or environment."""
    env_token = os.environ.get("GITEA_TOKEN")
    env_url = os.environ.get("GITEA_URL", "https://src.opensuse.org").rstrip("/")
    if env_token:
        return env_url, env_token

    config_path = Path.home() / ".config" / "tea" / "config.yml"
    if not config_path.exists():
        print(f"{Fore.RED}Error: tea configuration not found at {config_path}")
        print("Please run 'tea login add' or set GITEA_TOKEN first.")
        sys.exit(1)

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        logins = config.get("logins", [])

        # 1. Match explicit profile name
        for login in logins:
            if login.get("name") == login_name:
                return login.get("url").rstrip("/"), login.get("token")

        # 2. Match default profile
        for login in logins:
            if login.get("default") is True:
                return login.get("url").rstrip("/"), login.get("token")

        # 3. Match any profile pointing to src.opensuse.org
        for login in logins:
            if "src.opensuse.org" in login.get("url", ""):
                return login.get("url").rstrip("/"), login.get("token")

        if logins:
            return logins[0].get("url").rstrip("/"), logins[0].get("token")
    except Exception as e:
        print(f"{Fore.RED}Error reading YAML configuration at {config_path}: {e}")
        sys.exit(1)

    print(f"{Fore.RED}Error: No valid login profiles found in {config_path}.")
    sys.exit(1)


# ---------------------------------------------------------------------
# GITEA API CLIENT
# ---------------------------------------------------------------------
class GiteaClient:
    def __init__(self, repo: Optional[str] = None):
        self.base_url, self.token = get_gitea_credentials()
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"token {self.token}",
            "Content-Type": "application/json",
            "User-Agent": "pr-manage/2.0 (openSUSE Maintainer Tools)"
        })
        self._permissions: Optional[Dict[str, bool]] = None
        self.set_repo(repo or resolve_repository())

    def set_repo(self, repo_name: str, persist: bool = False):
        self.repo = repo_name.strip()
        if "/" in self.repo:
            self.owner, self.repo_name = self.repo.split("/", 1)
        else:
            self.owner, self.repo_name = "", self.repo
        self._permissions = None
        if persist:
            save_config(active_workspace=self.repo, add_workspace=self.repo)

    def get_repo_info(self, repo_target: Optional[str] = None) -> Optional[Dict]:
        """Fetches repository metadata and permissions without exiting on 404."""
        target = (repo_target or self.repo).lstrip("/")
        url = f"{self.base_url}/api/v1/repos/{target}"
        try:
            res = self.session.get(url, timeout=5)
            if res.status_code == 200:
                return res.json()
            return None
        except Exception:
            return None

    @property
    def permissions(self) -> Dict[str, bool]:
        if self._permissions is None:
            info = self.get_repo_info()
            if info and "permissions" in info:
                self._permissions = info["permissions"]
            else:
                self._permissions = {"admin": False, "push": False, "pull": True}
        return self._permissions

    @property
    def has_push_access(self) -> bool:
        return self.permissions.get("push", False)

    @property
    def has_admin_access(self) -> bool:
        return self.permissions.get("admin", False)

    @property
    def is_read_only(self) -> bool:
        return not self.has_push_access

    def request(self, endpoint: str, method: str = "GET", data: Optional[dict] = None) -> dict:
        url = f"{self.base_url}/api/v1/{endpoint.lstrip('/')}"
        try:
            if method == "GET":
                res = self.session.get(url, timeout=15)
            elif method == "POST":
                res = self.session.post(url, json=data, timeout=15)
            elif method == "PATCH":
                res = self.session.patch(url, json=data, timeout=15)
            elif method == "DELETE":
                res = self.session.delete(url, timeout=15)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            if res.status_code >= 400:
                print(f"{Fore.RED}API Error ({res.status_code}) on {method} {endpoint}: {res.text}")
                sys.exit(1)

            if res.text:
                return res.json()
            return {}
        except requests.RequestException as e:
            print(f"{Fore.RED}Network error during API request: {e}")
            sys.exit(1)

    def get_all_open_prs(self) -> List[dict]:
        """Fetches ALL open pull requests on the repository with proper pagination."""
        prs = []
        page = 1
        while True:
            batch = self.request(f"repos/{self.repo}/pulls?state=open&limit=50&page={page}")
            if not isinstance(batch, list) or not batch:
                break
            prs.extend(batch)
            if len(batch) < 50:
                break
            page += 1
        return prs

    def get_pr(self, pr_id: int) -> dict:
        return self.request(f"repos/{self.repo}/pulls/{pr_id}")

    def update_pr(self, pr_id: int, body: Optional[str] = None, title: Optional[str] = None, state: Optional[str] = None):
        payload = {}
        if body is not None:
            payload["body"] = body
        if title is not None:
            payload["title"] = title
        if state is not None:
            payload["state"] = state
        if payload:
            return self.request(f"repos/{self.repo}/pulls/{pr_id}", method="PATCH", data=payload)
        return {}

    def add_comment(self, pr_id: int, comment: str):
        return self.request(f"repos/{self.repo}/issues/{pr_id}/comments", method="POST", data={"body": comment})


# ---------------------------------------------------------------------
# PACKAGE RESOLUTION & RELATIONSHIP TRACING
# ---------------------------------------------------------------------
def find_open_pr_for_package(open_prs: List[dict], package_name: str) -> Optional[dict]:
    """
    Finds the open PR in _ObsPrj representing the given package name.
    Matches strictly:
    1. Exact match on head_ref 'PR_<pkg>#<num>'
    2. Exact match on referenced token in body 'PR: <owner>/<pkg>!<num>'
    3. Whole-word match in title
    """
    pkg_clean = package_name.strip().lower()

    # Priority 1: Match head ref
    for pr in open_prs:
        head_ref = pr.get("head", {}).get("ref", "")
        m = HEAD_PR_REGEX.match(head_ref)
        if m and m.group(1).lower() == pkg_clean:
            return pr

    # Priority 2: Match token in body
    for pr in open_prs:
        tokens = extract_tokens(pr.get("body", ""))
        for tok in tokens:
            if tok.key == pkg_clean:
                return pr

    # Priority 3: Word boundary match in title
    word_pattern = re.compile(rf"{re.escape(pkg_clean)}", re.IGNORECASE)
    for pr in open_prs:
        title = pr.get("title", "")
        if word_pattern.search(title):
            return pr

    return None


def find_forwarded_child_id(client: GiteaClient, sub_repo: str, parent_id: str, exclude_pr_id: Optional[int] = None) -> Optional[int]:
    """
    Finds the exact child PR on _ObsPrj that was generated for sub_repo!parent_id.
    First checks recent _ObsPrj PRs matching head_ref 'PR_<pkg>#<parent_id>',
    falling back to scanning the parent issue's timeline in reverse order,
    strictly validating head_ref and ignoring exclude_pr_id (e.g. the active group).
    """
    pkg_name = sub_repo.split("/")[-1]
    expected_head = f"PR_{pkg_name}#{parent_id}".lower()

    # Strategy 1: Look for exact matching head ref on _ObsPrj (state=all)
    try:
        prs = client.request(f"repos/{client.repo}/pulls?state=all&limit=50")
        if isinstance(prs, list):
            for pr in prs:
                num = int(pr.get("number", 0))
                if exclude_pr_id and num == exclude_pr_id:
                    continue
                if pr.get("head", {}).get("ref", "").lower() == expected_head:
                    return num
    except Exception:
        pass

    # Strategy 2: Scan parent issue timeline in reverse order, verifying head ref
    try:
        timeline = client.request(f"repos/{sub_repo}/issues/{parent_id}/timeline")
        if isinstance(timeline, list):
            for event in reversed(timeline):
                ref_issue = event.get("ref_issue")
                if not ref_issue:
                    continue
                num = int(ref_issue.get("number", 0))
                if exclude_pr_id and num == exclude_pr_id:
                    continue
                repo_name = ref_issue.get("repository", {}).get("name", "")
                if repo_name.lower() != client.repo_name.lower():
                    continue

                # Verify actual PR head ref to avoid returning parent group PR
                try:
                    pr_data = client.request(f"repos/{client.repo}/pulls/{num}")
                    if isinstance(pr_data, dict):
                        h_ref = pr_data.get("head", {}).get("ref", "")
                        if h_ref:
                            if h_ref.lower() == expected_head:
                                return num
                            continue
                    return num
                except Exception:
                    return num
    except Exception as e:
        print(f"{Fore.YELLOW}Warning: Timeline lookup failed for {sub_repo}!{parent_id}: {e}")

    return None


# ---------------------------------------------------------------------
# ACTION IMPLEMENTATIONS
# ---------------------------------------------------------------------
def action_list(client: GiteaClient, filter_branch: Optional[str] = None):
    target_repo = client.repo
    if filter_branch:
        print(f"=== Fetching active open PRs on {target_repo} targeting '{get_branch_color(filter_branch)}{filter_branch}{Style.RESET_ALL}'... ===")
    else:
        print(f"=== Fetching all active open PRs on {target_repo}... ===")

    summaries = client.get_all_open_prs()
    if not summaries:
        print(f"{Fore.YELLOW}No open PRs found on {target_repo}.")
        return

    print("=== Resolving target branches and details in parallel... ===")
    pr_ids = [int(p["number"]) for p in summaries]

    with ThreadPoolExecutor(max_workers=10) as executor:
        records = list(executor.map(client.get_pr, pr_ids))

    if filter_branch:
        records = [r for r in records if r.get("base", {}).get("ref") == filter_branch]
        records.sort(key=lambda x: x["number"])
    else:
        records.sort(key=lambda x: (x.get("base", {}).get("ref", ""), x["number"]))

    print(f"\n{Style.BRIGHT}{'INDEX':<8} {'TARGET BRANCH':<15} {'PACKAGE / TITLE'}")
    print(f"{'-'*5:<8} {'-'*13:<15} {'-'*15}")

    for r in records:
        branch = r.get("base", {}).get("ref", "unknown")
        b_color = get_branch_color(branch)
        raw_title = r.get("title", "").replace("Forwarded PRs: ", "")

        host_package = get_host_package_from_pr(r)
        tokens = extract_tokens(r.get("body", ""))
        token_pkgs = [t.package for t in tokens]

        if host_package:
            packages = [p.strip() for p in raw_title.split(",") if p.strip()]
            if host_package in packages:
                packages.remove(host_package)

            for tp in token_pkgs:
                if tp != host_package and tp not in packages:
                    packages.append(tp)

            host_str = f"{Fore.YELLOW}{Style.BRIGHT}★ {host_package}{Style.RESET_ALL}"
            if packages:
                peer_str = f" (+ {len(packages)} peers: {', '.join(packages[:4])}{'...' if len(packages) > 4 else ''})"
                clean_title = f"{host_str}{peer_str}"
            else:
                clean_title = host_str
        else:
            clean_title = raw_title

        print(f"{Fore.WHITE}{r['number']:<8} {b_color}{branch:<15} {Style.RESET_ALL}{clean_title}")


def action_select(client: GiteaClient, target_id: int, source_packages: List[str]):
    target = client.get_pr(target_id)
    target_branch = target.get("base", {}).get("ref", "")
    b_color = get_branch_color(target_branch)

    print(f"Target PR #{target_id} points to branch: {b_color}{target_branch}{Style.RESET_ALL}")
    print("-" * 60)

    # 1. Existing packages in target PR
    target_tokens = extract_tokens(target.get("body", ""))
    target_pkg_map: Dict[str, PackageToken] = {t.key: t for t in target_tokens}

    host_pkg = get_host_package_from_pr(target)
    if host_pkg and host_pkg.lower() not in target_pkg_map:
        target_pkg_map[host_pkg.lower()] = PackageToken(client.owner or "GNOME", host_pkg, 0)

    open_prs = client.get_all_open_prs()
    new_tokens: List[PackageToken] = []
    ids_to_close: List[int] = []

    # De-duplicate requested source packages preserving order
    seen_req = set()
    cleaned_requests = []
    for pkg in source_packages:
        if pkg.lower() not in seen_req:
            seen_req.add(pkg.lower())
            cleaned_requests.append(pkg)

    for package in cleaned_requests:
        print(f"Processing package: '{Fore.BLUE}{package}{Style.RESET_ALL}'...")

        # 2. DUPLICATE PACKAGE COLLISION GUARD: check if package is already in target PR
        if package.lower() in target_pkg_map:
            existing = target_pkg_map[package.lower()]
            ref_str = f"!{existing.pr_number}" if existing.pr_number else "host"
            print(f"--> {Fore.RED}DUPLICATE PACKAGE CONFLICT: Package '{package}' already exists in target PR #{target_id} "
                  f"(referencing {existing.owner}/{existing.package}#{ref_str}).")
            print(f"    {Fore.YELLOW}A combined PR cannot contain multiple PRs for the same submodule. Skipping duplicate!")
            continue

        # 3. Find matching open PR for package
        match = find_open_pr_for_package(open_prs, package)
        if not match:
            print(f"--> {Fore.RED}ERROR: Could not find an open PR on {client.repo} for package '{package}'. Skipping!")
            continue

        if int(match["number"]) == target_id:
            print(f"--> {Fore.YELLOW}Warning: Cannot add target PR #{target_id} to itself. Skipping.")
            continue

        source = client.get_pr(int(match["number"]))
        source_branch = source.get("base", {}).get("ref", "")

        if target_branch != source_branch:
            print(f"--> {Fore.RED}CRITICAL BRANCH MISMATCH for '{package}'! "
                  f"Target #{target_id} is '{target_branch}', but Source #{source['number']} is '{source_branch}'. Skipping.")
            continue

        source_tokens = extract_tokens(source.get("body", ""))
        if not source_tokens:
            print(f"--> {Fore.RED}ERROR: No valid reference tokens ('PR: ...') found in PR #{source['number']}. Skipping.")
            continue

        # Check all tokens in the source PR for duplicates against target
        has_sub_conflict = False
        for s_tok in source_tokens:
            if s_tok.key in target_pkg_map:
                existing = target_pkg_map[s_tok.key]
                print(f"--> {Fore.RED}DUPLICATE PACKAGE CONFLICT: Source PR #{source['number']} references '{s_tok.package}', "
                      f"which already exists in target PR #{target_id} (referencing !{existing.pr_number}).")
                print(f"    {Fore.YELLOW}A combined PR cannot contain multiple PRs for the same submodule. Skipping source PR!")
                has_sub_conflict = True
                break

        if has_sub_conflict:
            continue

        for s_tok in source_tokens:
            print(f"--> Staging reference line: {Style.DIM}{s_tok.raw_line}{Style.RESET_ALL}")
            new_tokens.append(s_tok)
            target_pkg_map[s_tok.key] = s_tok

        ids_to_close.append(int(source["number"]))

    if not new_tokens:
        print(f"{Fore.YELLOW}No valid packages were processed.")
        return

    # Update body
    body_lines = (target.get("body") or "").splitlines()
    last_pr_idx = max((i for i, line in enumerate(body_lines) if TOKEN_REGEX.match(line)), default=-1)

    if last_pr_idx != -1:
        for tok in reversed(new_tokens):
            body_lines.insert(last_pr_idx + 1, tok.raw_line)
    else:
        body_lines.extend([""] + [t.raw_line for t in new_tokens])

    new_body = "\n".join(body_lines)

    client.update_pr(target_id, body=new_body)

    for close_id in ids_to_close:
        client.update_pr(close_id, state="closed")

    print(f"=== {Fore.GREEN}Done! {len(new_tokens)} package(s) selected into group #{target_id} ===")


def action_combine(client: GiteaClient, target_id: int, source_id: int):
    if target_id == source_id:
        print(f"{Fore.RED}Error: Cannot combine PR #{target_id} into itself.")
        sys.exit(1)

    target = client.get_pr(target_id)
    source = client.get_pr(source_id)

    target_branch = target.get("base", {}).get("ref", "")
    source_branch = source.get("base", {}).get("ref", "")

    if target_branch != source_branch:
        print(f"{Fore.RED}CRITICAL ERROR: Branch mismatch! Target #{target_id} points to '{target_branch}', "
              f"Source #{source_id} points to '{source_branch}'.")
        sys.exit(1)

    target_tokens = extract_tokens(target.get("body", ""))
    source_tokens = extract_tokens(source.get("body", ""))

    if not source_tokens:
        print(f"{Fore.RED}Error: No tracking tokens found in source PR #{source_id}.")
        sys.exit(1)

    target_pkg_map = {t.key: t for t in target_tokens}
    source_pkg_map = {t.key: t for t in source_tokens}

    host_target = get_host_package_from_pr(target)
    if host_target and host_target.lower() not in target_pkg_map:
        target_pkg_map[host_target.lower()] = PackageToken(client.owner or "GNOME", host_target, 0)

    # DUPLICATE PACKAGE COLLISION GUARD:
    conflicts = set(target_pkg_map.keys()) & set(source_pkg_map.keys())
    if conflicts:
        print(f"{Fore.RED}❌ CRITICAL CONFLICT: Cannot combine PR #{source_id} into #{target_id}!")
        print(f"{Fore.RED}The following submodule(s) already exist in target PR #{target_id}:")
        for pkg_key in sorted(conflicts):
            t_tok = target_pkg_map[pkg_key]
            s_tok = source_pkg_map[pkg_key]
            t_ref = f"!{t_tok.pr_number}" if t_tok.pr_number else "host"
            print(f"  - {Fore.YELLOW}{s_tok.package}{Fore.RED}: Target #{target_id} has {t_ref}, "
                  f"Source #{source_id} has !{s_tok.pr_number}")
        print(f"\n{Fore.RED}A combined PR cannot contain multiple PRs for the same submodule. Combine aborted.")
        sys.exit(1)

    # Insert source tokens into target body
    body_lines = (target.get("body") or "").splitlines()
    last_pr_idx = max((i for i, line in enumerate(body_lines) if TOKEN_REGEX.match(line)), default=-1)

    if last_pr_idx != -1:
        for tok in reversed(source_tokens):
            body_lines.insert(last_pr_idx + 1, tok.raw_line)
    else:
        body_lines.extend([""] + [t.raw_line for t in source_tokens])

    new_body = "\n".join(body_lines)

    client.update_pr(target_id, body=new_body)
    client.update_pr(source_id, state="closed")
    print(f"{Fore.GREEN}Done! Group PR #{source_id} combined into #{target_id}.")


def action_unselect(client: GiteaClient, target_id: int, package: str):
    target = client.get_pr(target_id)
    all_tokens = extract_tokens(target.get("body", ""))

    if not all_tokens:
        print(f"{Fore.RED}Error: PR #{target_id} has no token trackers.")
        return

    host_package = get_host_package_from_pr(target)
    if host_package and package.lower() == host_package.lower():
        print(f"{Fore.RED}!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print(f"{Fore.RED}CRITICAL WARNING: Target package '{package}' is the group host.")
        print(f"{Fore.YELLOW}To tear down this stack safely, use: pr-manage disintegrate {target_id}")
        print(f"{Fore.RED}!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        sys.exit(1)

    target_token = next((t for t in all_tokens if t.key == package.lower()), None)
    if not target_token:
        print(f"{Fore.RED}Error: Package '{package}' tracking line missing in PR #{target_id}.")
        return

    sub_repo = f"{target_token.owner}/{target_token.package}"
    parent_id = str(target_token.pr_number)

    print(f"Resolving forwarded child PR ID for {package} via {sub_repo}#{parent_id}...")
    child_id = find_forwarded_child_id(client, sub_repo, parent_id, exclude_pr_id=target_id)

    if not child_id:
        print(f"{Fore.RED}Error: Could not resolve the forwarded child PR ID for '{package}'. Aborting unselect.")
        return

    # Filter out unselected package
    remaining_tokens = [t for t in all_tokens if t.key != package.lower()]
    body_lines = [line for line in target.get("body", "").splitlines() if not TOKEN_REGEX.match(line) or line.strip() != target_token.raw_line]
    new_body = "\n".join(body_lines)

    client.update_pr(target_id, body=new_body)

    print(f"=== Reopening original forwarded child PR #{child_id} in {client.repo} ===")
    client.update_pr(child_id, state="open")
    print(f"=== {Fore.GREEN}Done! Successfully unselected and restored '{package}' ===")


def action_disintegrate(client: GiteaClient, target_id: int):
    target = client.get_pr(target_id)
    all_tokens = extract_tokens(target.get("body", ""))

    if len(all_tokens) <= 1:
        print(f"{Fore.YELLOW}This PR doesn't track any nested peer groups. Already standalone.")
        return

    host_package = get_host_package_from_pr(target)
    peer_tokens = [t for t in all_tokens if host_package and t.key != host_package.lower()]
    if not peer_tokens and len(all_tokens) > 1:
        peer_tokens = all_tokens[1:]

    print(f"Resolving and reopening {len(peer_tokens)} forwarded child PRs in parallel...")

    def restore_child_pr(tok: PackageToken):
        sub_repo = f"{tok.owner}/{tok.package}"
        parent_id = str(tok.pr_number)
        child_id = find_forwarded_child_id(client, sub_repo, parent_id)
        if child_id:
            print(f"--> Reopening forwarded child #{child_id} ({tok.package}) in {client.repo}...")
            client.update_pr(child_id, state="open")
        else:
            print(f"--> {Fore.RED}Error: Could not trace child PR for package {tok.package} (!{parent_id})")

    with ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(restore_child_pr, peer_tokens))

    peer_lines = {t.raw_line for t in peer_tokens}
    cleaned_lines = [line for line in target.get("body", "").splitlines() if line.strip() not in peer_lines]
    new_body = "\n".join(cleaned_lines)

    client.update_pr(target_id, body=new_body)
    print(f"=== {Fore.GREEN}Disintegration Complete! Reset #{target_id} back to single track. ===")


def action_accept(client: GiteaClient, target_id: int):
    print(f"=== Signaling Staging Approval for Group PR #{target_id} ===")
    client.add_comment(target_id, "merge ok")
    print(f"{Fore.GREEN}Successfully commented 'merge ok' on PR #{target_id}.")


# ---------------------------------------------------------------------
# MAIN EXECUTOR CLI FRONTEND
# ---------------------------------------------------------------------

def action_audit(client: GiteaClient, filter_branch: Optional[str] = None):
    import staging_service as ss
    service = ss.StagingService(client=client)
    branch_str = f" targeting '{filter_branch}'" if filter_branch else ""
    print(f"=== Auditing open package PRs in {service.owner} against {client.repo}{branch_str}... ===")
    orphans = service.audit_orphans(filter_branch=filter_branch)

    if not orphans:
        print(f"\n{Fore.GREEN}✅ Audit clean: 100% of open package PRs in {service.owner} are tracked in staging!")
        return

    print(f"\n{Fore.RED}⚠️ Found {len(orphans)} out-of-sync package PR(s) not tracked in {client.repo}:{Style.RESET_ALL}\n")
    print(f"{Style.BRIGHT}{'PACKAGE':<28} {'PKG PR':<8} {'AUTHOR':<12} {'TARGET':<10} {'FORWARD PR':<16} {'TITLE'}")
    print(f"{'-'*7:<28} {'-'*6:<8} {'-'*10:<12} {'-'*8:<10} {'-'*14:<16} {'-'*15}")

    for o in orphans:
        fwd_info = f"#{o.forward_pr_id} ({o.forward_pr_state})" if o.forward_pr_id else "None"
        b_color = get_branch_color(o.base_branch)
        print(f"{Fore.WHITE}{o.package:<28} !{o.pr_number:<7} {o.author:<12} {b_color}{o.base_branch:<10} {Style.RESET_ALL}{fwd_info:<16} {o.title}")

    print(f"\n{Style.BRIGHT}Remediation options:")
    print("  1. In TUI: Run 'pr-manage tui' and press [O] to interactively reopen or adopt them into a group.")
    print(f"  2. In CLI: Use 'pr-manage select <group-pr-id> <package...>' to pull them into a staging group.\n")

def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(f"{Style.BRIGHT}Usage:")
        print("  pr-manage list [factory|next]")
        print("  pr-manage audit [factory|next]           Audit and detect out-of-sync / orphan PRs")
        print("  pr-manage tui                            Interactive 3-column staging queue TUI")
        print("  pr-manage gui                            Launch graphical GTK4 / Libadwaita interface")
        print("  pr-manage select <target-pr-id> <package1> [package2 ...]")
        print("  pr-manage unselect <target-pr-id> <package-name>")
        print("  pr-manage combine <target-pr-id> <source-pr-id>")
        print("  pr-manage disintegrate <target-pr-id>")
        print("  pr-manage accept <target-pr-id>")
        print("\nOptions:")
        print("  --repo, -R <owner/repo>  Target Gitea repository (e.g. GNOME/_ObsPrj, KDE/_ObsPrj, openSUSE/Factory)")
        sys.exit(0 if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help", "help") else 1)

    args = sys.argv[1:]
    repo_override = None
    for flag in ("--repo", "-R"):
        if flag in args:
            idx = args.index(flag)
            if idx + 1 < len(args):
                repo_override = args[idx + 1]
                del args[idx:idx + 2]
                break

    cmd = args[0]
    client = GiteaClient(repo=repo_override)
    if repo_override:
        save_config(active_workspace=client.repo, add_workspace=client.repo)

    if cmd == "list":
        branch = args[1] if len(args) > 1 else None
        action_list(client, branch)
    elif cmd == "audit":
        branch = args[1] if len(args) > 1 else None
        action_audit(client, branch)
    elif cmd in ("tui", "ui"):
        import pr_manage_tui
        pr_manage_tui.run_tui(client)
    elif cmd in ("gui", "gtk"):
        import pr_manage_gui
        gui_client = client if repo_override else None
        pr_manage_gui.run_gui(gui_client)
    else:
        if len(args) < 2:
            print(f"{Fore.RED}Error: Command '{cmd}' requires a target PR ID.")
            sys.exit(1)
        try:
            target_id = int(args[1])
        except ValueError:
            print(f"{Fore.RED}Error: Target PR ID must be an integer.")
            sys.exit(1)

        if cmd == "select":
            if len(args) < 3:
                print(f"{Fore.RED}Error: Missing package arguments for select.")
                sys.exit(1)
            action_select(client, target_id, args[2:])
        elif cmd == "unselect":
            if len(args) < 3:
                print(f"{Fore.RED}Error: Missing package argument for unselect.")
                sys.exit(1)
            action_unselect(client, target_id, args[2])
        elif cmd == "combine":
            if len(args) < 3:
                print(f"{Fore.RED}Error: Missing source PR ID for combine.")
                sys.exit(1)
            try:
                source_id = int(args[2])
            except ValueError:
                print(f"{Fore.RED}Error: Source PR ID must be an integer.")
                sys.exit(1)
            action_combine(client, target_id, source_id)
        elif cmd == "disintegrate":
            action_disintegrate(client, target_id)
        elif cmd == "accept":
            action_accept(client, target_id)
        else:
            print(f"{Fore.RED}Unknown command: {cmd}")
            sys.exit(1)


if __name__ == "__main__":
    main()
