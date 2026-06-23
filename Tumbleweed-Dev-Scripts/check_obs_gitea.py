import os
import sys
import time
import argparse
import subprocess
from typing import Dict, List, Tuple, Optional, Set
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# Default Configurations
DEFAULT_OBS_PROJECT = "openSUSE:Factory"
DEFAULT_GITEA_API_URL = "https://src.opensuse.org/api/v1"
DEFAULT_GITEA_ORG = "pool"
DEFAULT_TARGET_BRANCH = "factory"
DEFAULT_RENAME_BRANCH_TO = "factory-deleted"
DEFAULT_MAX_WORKERS = 5


def get_http_session(max_workers: int) -> requests.Session:
    """Configures a requests Session with robust retry policies and connection pooling."""
    session = requests.Session()
    retries = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        raise_on_status=False
    )
    adapter = HTTPAdapter(
        max_retries=retries,
        pool_connections=max_workers,
        pool_maxsize=max_workers
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def get_obs_packages(obs_project: str) -> List[str]:
    """Fetches package names from OBS using osc, skipping subprojects (containing ':')"""
    print(f"[*] Fetching packages from OBS project '{obs_project}'...", file=sys.stderr)
    try:
        result = subprocess.run(
            ["osc", "ls", obs_project],
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, 
            text=True, 
            check=True
        )
        packages = [line.strip() for line in result.stdout.splitlines() if line.strip() and ":" not in line]
        print(f"[+] Found {len(packages)} source packages in OBS.", file=sys.stderr)
        return packages
    except subprocess.CalledProcessError as e:
        print(f"[-] Error running 'osc': {e.stderr}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print("[-] Error: 'osc' command-line tool not found.", file=sys.stderr)
        sys.exit(1)


def get_gitea_pool_repos(
    session: requests.Session,
    api_url: str,
    org: str,
    token: Optional[str]
) -> Dict[str, dict]:
    """Fetches all repository names within Gitea organization with optimized pagination and connection reuse."""
    print(f"[*] Fetching active repositories from Gitea organization '{org}'...", file=sys.stderr)
    repos: Dict[str, dict] = {}
    page = 1
    limit = 100  # Optimized from 50 to 100 to reduce network roundtrips by half
    
    total_fetched = 0
    checkpoint = 250
    line_break_threshold = 5000
    dots_on_current_line = 0
    
    headers = {"Authorization": f"token {token}"} if token else {}
    
    while True:
        url = f"{api_url}/orgs/{org}/repos?page={page}&limit={limit}&sort=name"
        try:
            response = session.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as e:
            print(f"\n[-] Failed to fetch Gitea repos on page {page}: {e}", file=sys.stderr)
            sys.exit(1)
        
        if not data:
            break
            
        for repo in data:
            repos[repo['name'].lower()] = {
                'original_name': repo['name'],
                'default_branch': repo.get('default_branch', ''),
                'is_empty': repo.get('empty', False)
            }
            total_fetched += 1
            
            if total_fetched % checkpoint == 0:
                print(".", end="", flush=True, file=sys.stderr)
                dots_on_current_line += 1
                
                if dots_on_current_line >= (line_break_threshold // checkpoint):
                    print(f" ({total_fetched})", file=sys.stderr)
                    dots_on_current_line = 0
            
        page += 1
        
    if dots_on_current_line != 0:
        print(file=sys.stderr)
        
    print(f"[+] Found {len(repos)} total repositories in Gitea pool.", file=sys.stderr)
    return repos


def check_branch_exists(
    session: requests.Session,
    api_url: str,
    org: str,
    repo_name: str,
    branch_name: str,
    token: Optional[str]
) -> Tuple[str, bool]:
    """Hits Gitea branch endpoint utilizing the pooled HTTP session."""
    url = f"{api_url}/repos/{org}/{repo_name}/branches/{branch_name}"
    headers = {"Authorization": f"token {token}"} if token else {}
    try:
        res = session.get(url, headers=headers, timeout=10)
        return repo_name, (res.status_code == 200)
    except requests.RequestException:
        return repo_name, False


def analyze_alternative_branches(
    session: requests.Session,
    api_url: str,
    org: str,
    repo_name: str,
    target_branch: str,
    rename_branch_to: str,
    token: Optional[str]
) -> Tuple[Optional[str], Optional[str]]:
    """Fetches alternative tracks, returning the name of the most recently updated branch or None."""
    headers = {"Authorization": f"token {token}"} if token else {}
    url = f"{api_url}/repos/{org}/{repo_name}/branches"
    try:
        res = session.get(url, headers=headers, timeout=15)
        if res.status_code == 200:
            valid_alternatives = []
            for b in res.json():
                b_name = b['name']
                if b_name.lower() not in [target_branch.lower(), rename_branch_to.lower()]:
                    commit_data = b.get('commit')
                    # Defensive parsing in case Gitea returns None/empty objects for commit values
                    if isinstance(commit_data, dict):
                        commit_time = commit_data.get('timestamp', '0000-00-00T00:00:00Z')
                    else:
                        commit_time = '0000-00-00T00:00:00Z'
                    valid_alternatives.append((b_name, commit_time))
            if valid_alternatives:
                valid_alternatives.sort(key=lambda x: x[1], reverse=True)
                return valid_alternatives[0][0], valid_alternatives[0][1]
    except requests.RequestException:
        pass
    return None, None


def process_orphan_branch(
    session: requests.Session,
    api_url: str,
    org: str,
    repo_name: str,
    current_default: str,
    alternative_default: Optional[str],
    target_branch: str,
    rename_branch_to: str,
    token: Optional[str],
    dry_run: bool = False
) -> bool:
    """Executes or simulates the default branch migration and target branch deletion rules."""
    if dry_run:
        target_preview = alternative_default if alternative_default else rename_branch_to
        print(f"    [DRY-RUN] Repo '{repo_name}': Will create '{rename_branch_to}'. ", end="")
        if current_default.lower() == target_branch.lower():
            print(f"Will change default branch from '{target_branch}' -> '{target_preview}'. ", end="")
        print(f"Will delete '{target_branch}'.")
        return True

    headers = {
        "Authorization": f"token {token}",
        "Content-Type": "application/json"
    }
    
    # Step 1: Create the backup branch
    create_url = f"{api_url}/repos/{org}/{repo_name}/branches"
    payload = {"new_branch_name": rename_branch_to, "old_branch_name": target_branch}
    try:
        create_res = session.post(create_url, headers=headers, json=payload, timeout=10)
        if create_res.status_code not in [200, 201]:
            print(f"    [-] Failed to create backup branch for {repo_name}: {create_res.text.strip()}", file=sys.stderr)
            return False
    except requests.RequestException as e:
        print(f"    [-] Exception creating branch for {repo_name}: {e}", file=sys.stderr)
        return False

    # Step 2: Reroute default branch pointer
    if current_default.lower() == target_branch.lower():
        target_default_pointer = alternative_default if alternative_default else rename_branch_to
        patch_url = f"{api_url}/repos/{org}/{repo_name}"
        try:
            res = session.patch(patch_url, headers=headers, json={"default_branch": target_default_pointer}, timeout=10)
            if res.status_code != 200:
                print(f"    [-] Failed to redirect default branch to {target_default_pointer} for {repo_name}: {res.text.strip()}", file=sys.stderr)
                return False
        except requests.RequestException as e:
            print(f"    [-] Exception re-routing default branch configuration for {repo_name}: {e}", file=sys.stderr)
            return False

    # Step 3: Delete original target branch
    delete_url = f"{api_url}/repos/{org}/{repo_name}/branches/{target_branch}"
    try:
        delete_res = session.delete(delete_url, headers=headers, timeout=10)
        return delete_res.status_code in [200, 204]
    except requests.RequestException as e:
        print(f"    [-] Exception dropping old branch target on {repo_name}: {e}", file=sys.stderr)
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit and manage openSUSE OBS to Gitea migration mismatches.")
    parser.add_argument("--remove-obsoletes", action="store_true", help="Rename the target branch of true orphans.")
    parser.add_argument("--dry", action="store_true", help="Global non-destructive simulation mode for actions.")
    parser.add_argument(
        "--only", 
        choices=["missing", "orphans"], 
        help="Filter output report: 'missing' shows only OBS packages absent from Gitea; 'orphans' shows only Gitea cleanup targets."
    )

    # Exposing configuration parameters via CLI arguments with default values
    parser.add_argument("--obs-project", default=os.getenv("OBS_PROJECT", DEFAULT_OBS_PROJECT), help="OBS Project to audit.")
    parser.add_argument("--gitea-api-url", default=os.getenv("GITEA_API_URL", DEFAULT_GITEA_API_URL), help="Gitea base API URL.")
    parser.add_argument("--gitea-org", default=os.getenv("GITEA_ORG", DEFAULT_GITEA_ORG), help="Gitea target organization.")
    parser.add_argument("--target-branch", default=os.getenv("TARGET_BRANCH", DEFAULT_TARGET_BRANCH), help="Target branch to audit/clean.")
    parser.add_argument("--rename-branch-to", default=os.getenv("RENAME_BRANCH_TO", DEFAULT_RENAME_BRANCH_TO), help="Backup name for target branch on removal.")
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS, help="Polite concurrency worker limit.")

    args = parser.parse_args()

    # Retrieve Token from environment
    gitea_token = os.getenv("GITEA_TOKEN")

    # Integrity gate checks
    if args.remove_obsoletes and not gitea_token:
        print("[-] Error: The --remove-obsoletes action requires API authorization. Please export your 'GITEA_TOKEN'.", file=sys.stderr)
        sys.exit(1)

    # Initialize resilient connection-pooled HTTP Session
    session = get_http_session(args.max_workers)

    obs_packages = get_obs_packages(args.obs_project)
    gitea_repos = get_gitea_pool_repos(session, args.gitea_api_url, args.gitea_org, gitea_token)
    
    missing_in_gitea = []
    gitea_mapped_names: Set[str] = set()
    
    print("\n[*] Auditing: OBS -> Gitea...", file=sys.stderr)
    for pkg in obs_packages:
        transformed_name = pkg.replace("+", "_")
        transformed_lower = transformed_name.lower()
        
        if transformed_lower not in gitea_repos:
            missing_in_gitea.append((pkg, transformed_name))
        else:
            gitea_mapped_names.add(transformed_lower)

    print("[*] Auditing: Gitea -> OBS (Validating true orphans)...", file=sys.stderr)
    orphaned_repos = []
    potential_orphans = {k: v for k, v in gitea_repos.items() if k not in gitea_mapped_names}
    
    # Isolate targets for fallback validation
    fallback_candidates = []
    for repo_lower, info in potential_orphans.items():
        if info['is_empty']:
            continue
        if info['default_branch'].lower() == args.target_branch.lower():
            orphaned_repos.append(info)
        else:
            fallback_candidates.append(info)

    # Performance optimization: skip network branch lookups if user explicitly requested only 'missing'
    if fallback_candidates and args.only != "missing":
        print(f"[*] Dispatching parallel threads to verify remaining {len(fallback_candidates)} unmapped candidates...", file=sys.stderr)
        completed_count = 0
        with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
            futures = {
                executor.submit(
                    check_branch_exists, session, args.gitea_api_url, args.gitea_org, item['original_name'], args.target_branch, gitea_token
                ): item for item in fallback_candidates
            }
            for future in as_completed(futures):
                corresponding_item = futures[future]
                try:
                    _, branch_exists = future.result()
                    if branch_exists:
                        orphaned_repos.append(corresponding_item)
                except Exception as exc:
                    # Isolated error handling ensures one API thread error won't crash the entire analysis
                    print(f"\n[!] Error checking branch for {corresponding_item['original_name']}: {exc}", file=sys.stderr)
                
                completed_count += 1
                if completed_count % 100 == 0 or completed_count == len(fallback_candidates):
                    print(f"\r    -> Progress: {completed_count}/{len(fallback_candidates)} checked...", end="", flush=True, file=sys.stderr)
        print("\r[+] Branch tracking complete.                                            ", file=sys.stderr)
    elif args.only == "missing":
        print("[*] Skipping extended Gitea branch validation rules due to '--only missing' filter constraint.", file=sys.stderr)

    # -------------------------------------------------------------------------
    # Report Part 1: Missing in Git (OBS -> Gitea)
    # -------------------------------------------------------------------------
    if args.only != "orphans":
        print("\n" + "=" * 60)
        if missing_in_gitea:
            print(f"[!] Alert: {len(missing_in_gitea)} packages are missing on src.opensuse.org:")
            print(f"{'OBS Package Name':<35} -> {'Expected Gitea Repo Name'}")
            print("-" * 60)
            for obs_name, expected_git in sorted(missing_in_gitea):
                print(f"{obs_name:<35} -> {expected_git}")
        else:
            print("[+] Success! All OBS packages exist in Gitea.")
        print("=" * 60)
        
    # -------------------------------------------------------------------------
    # Report Part 2: Orphan Management Phase (Gitea -> OBS)
    # -------------------------------------------------------------------------
    if args.only != "missing":
        print("\n" + "=" * 60)
        if orphaned_repos:
            print(f"[!] Alert: {len(orphaned_repos)} verified orphans found (repos containing branch '{args.target_branch}', missing in OBS).")
            
            # Action triggered (Live or Simulation)
            if args.remove_obsoletes:
                mode_label = "DRY-RUN SIMULATION" if args.dry else "LIVE REMOVAL PHASE"
                print(f"[*] Activating {mode_label} for obsoletes cleanup. Resolving dynamic defaults...", file=sys.stderr)
                
                success_count = 0
                for index, repo_info in enumerate(sorted(orphaned_repos, key=lambda x: x['original_name']), 1):
                    name = repo_info['original_name']
                    alt_branch, alt_time = analyze_alternative_branches(
                        session, args.gitea_api_url, args.gitea_org, name, args.target_branch, args.rename_branch_to, gitea_token
                    )
                    
                    if not args.dry:
                        print(f"    [{index}/{len(orphaned_repos)}] Processing branch transitions for {name}...", end="", flush=True, file=sys.stderr)
                        if alt_branch:
                            print(f" [Switching default to track '{alt_branch}' ({alt_time})]...", end="", flush=True, file=sys.stderr)
                        
                    is_success = process_orphan_branch(
                        session, args.gitea_api_url, args.gitea_org, name, repo_info['default_branch'], alt_branch,
                        args.target_branch, args.rename_branch_to, gitea_token, dry_run=args.dry
                    )
                    if is_success:
                        if not args.dry:
                            print(" SUCCESS.", file=sys.stderr)
                        success_count += 1
                    else:
                        print(" FAILED.", file=sys.stderr)
                        
                if args.dry:
                    print(f"[+] Simulation complete. Mapped execution plans for all {success_count}/{len(orphaned_repos)} obsolete candidates.")
                else:
                    print(f"[+] Mutation run complete. Successfully processed {success_count}/{len(orphaned_repos)} branches.")
            else:
                if args.dry:
                    print("[!] Notice: Running with '--dry' but no active mutation flags (like '--remove-obsoletes') specified. Nothing to simulate.")
                print("[-] Run with '--remove-obsoletes' to process branch renames.")
                print("-" * 60)
                for repo_info in sorted(orphaned_repos, key=lambda x: x['original_name']):
                    print(f"  - {repo_info['original_name']} (Current default branch: '{repo_info['default_branch']}')")
        else:
            print(f"[+] Success! No orphaned repositories containing branch '{args.target_branch}' found in Gitea pool.")
        print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
