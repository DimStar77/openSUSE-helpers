import os
import sys
import time
import argparse
import subprocess
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# Configuration
OBS_PROJECT = "openSUSE:Factory"
GITEA_API_URL = "https://src.opensuse.org/api/v1"
GITEA_ORG = "pool"
TARGET_BRANCH = "factory"
RENAME_BRANCH_TO = "factory-deleted"
MAX_WORKERS = 5  # Polite concurrency to protect server resources

# Retrieve Token from environment
GITEA_TOKEN = os.getenv("GITEA_TOKEN")

def get_obs_packages():
    """Fetches package names from OBS using osc, skipping subprojects (containing ':')"""
    print(f"[*] Fetching packages from OBS project '{OBS_PROJECT}'...")
    try:
        result = subprocess.run(
            ["osc", "ls", OBS_PROJECT], 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, 
            text=True, 
            check=True
        )
        packages = [line.strip() for line in result.stdout.splitlines() if line.strip() and ":" not in line]
        print(f"[+] Found {len(packages)} source packages in OBS.")
        return packages
    except subprocess.CalledProcessError as e:
        print(f"[-] Error running 'osc': {e.stderr}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print("[-] Error: 'osc' command-line tool not found.", file=sys.stderr)
        sys.exit(1)

def get_gitea_pool_repos():
    """Fetches all repository names within the target Gitea organization using pagination with retries."""
    print(f"[*] Fetching active repositories from Gitea organization '{GITEA_ORG}'...")
    repos = {}
    page = 1
    limit = 50 
    
    total_fetched = 0
    checkpoint = 250
    line_break_threshold = 5000
    dots_on_current_line = 0
    
    headers = {"Authorization": f"token {GITEA_TOKEN}"} if GITEA_TOKEN else {}
    
    while True:
        url = f"{GITEA_API_URL}/orgs/{GITEA_ORG}/repos?page={page}&limit={limit}&sort=name"
        
        max_retries = 5
        for attempt in range(1, max_retries + 1):
            try:
                response = requests.get(url, headers=headers, timeout=30)
                response.raise_for_status()
                data = response.json()
                break  
            except requests.RequestException as e:
                if attempt == max_retries:
                    print(f"\n[-] Failed to fetch page {page} after {max_retries} attempts: {e}", file=sys.stderr)
                    sys.exit(1)
                print(f"\n[!] Server hitch on page {page} (Attempt {attempt}/{max_retries}). Retrying in 2s...", end="")
                time.sleep(2)
        
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
                print(".", end="", flush=True)
                dots_on_current_line += 1
                
                if dots_on_current_line >= (line_break_threshold // checkpoint):
                    print(f" ({total_fetched})")
                    dots_on_current_line = 0
            
        page += 1
        
    if dots_on_current_line != 0:
        print()
        
    print(f"[+] Found {len(repos)} total repositories in Gitea pool.")
    return repos

def check_branch_exists(repo_name, branch_name):
    """Hits the specific Gitea branch endpoint to confirm branch presence."""
    url = f"{GITEA_API_URL}/repos/{GITEA_ORG}/{repo_name}/branches/{branch_name}"
    headers = {"Authorization": f"token {GITEA_TOKEN}"} if GITEA_TOKEN else {}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        return repo_name, (res.status_code == 200)
    except requests.RequestException:
        return repo_name, False

def analyze_alternative_branches(repo_name):
    """Fetches alternative tracks, returning the name of the most recently updated branch or None."""
    headers = {"Authorization": f"token {GITEA_TOKEN}"} if GITEA_TOKEN else {}
    url = f"{GITEA_API_URL}/repos/{GITEA_ORG}/{repo_name}/branches"
    try:
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code == 200:
            valid_alternatives = []
            for b in res.json():
                b_name = b['name']
                if b_name.lower() not in [TARGET_BRANCH.lower(), RENAME_BRANCH_TO.lower()]:
                    commit_time = b.get('commit', {}).get('timestamp', '0000-00-00T00:00:00Z')
                    valid_alternatives.append((b_name, commit_time))
            if valid_alternatives:
                valid_alternatives.sort(key=lambda x: x[1], reverse=True)
                return valid_alternatives[0][0], valid_alternatives[0][1]
    except requests.RequestException:
        pass
    return None, None

def process_orphan_branch(repo_name, current_default, alternative_default, dry_run=False):
    """Executes or simulates the default branch migration and factory branch deletion rules."""
    if dry_run:
        target_preview = alternative_default if alternative_default else RENAME_BRANCH_TO
        print(f"    [DRY-RUN] Repo '{repo_name}': Will create '{RENAME_BRANCH_TO}'. ", end="")
        if current_default.lower() == TARGET_BRANCH.lower():
            print(f"Will change default branch from '{TARGET_BRANCH}' -> '{target_preview}'. ", end="")
        print(f"Will delete '{TARGET_BRANCH}'.")
        return True

    headers = {
        "Authorization": f"token {GITEA_TOKEN}",
        "Content-Type": "application/json"
    }
    
    # Step 1: Create the new branch tracking the old 'factory' branch
    create_url = f"{GITEA_API_URL}/repos/{GITEA_ORG}/{repo_name}/branches"
    payload = {"new_branch_name": RENAME_BRANCH_TO, "old_branch_name": TARGET_BRANCH}
    try:
        create_res = requests.post(create_url, headers=headers, json=payload, timeout=10)
        if create_res.status_code not in [200, 201]:
            print(f"    [-] Failed to create backup branch for {repo_name}: {create_res.text.strip()}")
            return False
    except requests.RequestException as e:
        print(f"    [-] Exception creating branch for {repo_name}: {e}")
        return False

    # Step 2: Handle default branch re-routing if 'factory' holds the pointer
    if current_default.lower() == TARGET_BRANCH.lower():
        target_default_pointer = alternative_default if alternative_default else RENAME_BRANCH_TO
        patch_url = f"{GITEA_API_URL}/repos/{GITEA_ORG}/{repo_name}"
        try:
            res = requests.patch(patch_url, headers=headers, json={"default_branch": target_default_pointer}, timeout=10)
            if res.status_code != 200:
                print(f"    [-] Failed to redirect default branch to {target_default_pointer} for {repo_name}: {res.text.strip()}")
                return False
        except requests.RequestException as e:
            print(f"    [-] Exception re-routing default branch configuration for {repo_name}: {e}")
            return False

    # Step 3: Safely delete the old factory branch
    delete_url = f"{GITEA_API_URL}/repos/{GITEA_ORG}/{repo_name}/branches/{TARGET_BRANCH}"
    try:
        delete_res = requests.delete(delete_url, headers=headers, timeout=10)
        return delete_res.status_code in [200, 204]
    except requests.RequestException as e:
        print(f"    [-] Exception dropping old branch target on {repo_name}: {e}")
        return False

def main():
    parser = argparse.ArgumentParser(description="Audit and manage openSUSE OBS to Gitea migration mismatches.")
    parser.add_argument("--remove-obsoletes", action="store_true", help="Rename the factory branch of true orphans.")
    parser.add_argument("--dry", action="store_true", help="Global non-destructive simulation mode for actions.")
    parser.add_argument(
        "--only", 
        choices=["missing", "orphans"], 
        help="Filter output report: 'missing' shows only OBS packages absent from Gitea; 'orphans' shows only Gitea cleanup targets."
    )
    args = parser.parse_args()

    # Integrity gate checks
    if args.remove_obsoletes and not GITEA_TOKEN:
        print("[-] Error: The --remove-obsoletes action requires API authorization. Please export your 'GITEA_TOKEN'.", file=sys.stderr)
        sys.exit(1)

    obs_packages = get_obs_packages()
    gitea_repos = get_gitea_pool_repos()
    
    missing_in_gitea = []
    gitea_mapped_names = set()
    
    print("\n[*] Auditing: OBS -> Gitea...")
    for pkg in obs_packages:
        transformed_name = pkg.replace("+", "_")
        transformed_lower = transformed_name.lower()
        
        if transformed_lower not in gitea_repos:
            missing_in_gitea.append((pkg, transformed_name))
        else:
            gitea_mapped_names.add(transformed_lower)

    print("[*] Auditing: Gitea -> OBS (Validating true orphans)...")
    orphaned_repos = []
    potential_orphans = {k: v for k, v in gitea_repos.items() if k not in gitea_mapped_names}
    
    # Isolate targets for fallback validation
    fallback_candidates = []
    for repo_lower, info in potential_orphans.items():
        if info['is_empty']:
            continue
        if info['default_branch'].lower() == TARGET_BRANCH.lower():
            orphaned_repos.append(info)
        else:
            fallback_candidates.append(info)

    # Performance optimization: skip network branch lookups if the user explicitly requested only the 'missing' list
    if fallback_candidates and args.only != "missing":
        print(f"[*] Dispatching parallel threads to verify remaining {len(fallback_candidates)} unmapped candidates...")
        completed_count = 0
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(check_branch_exists, item['original_name'], TARGET_BRANCH): item for item in fallback_candidates}
            for future in as_completed(futures):
                corresponding_item = futures[future]
                _, branch_exists = future.result()
                if branch_exists:
                    orphaned_repos.append(corresponding_item)
                
                completed_count += 1
                if completed_count % 100 == 0 or completed_count == len(fallback_candidates):
                    print(f"\r    -> Progress: {completed_count}/{len(fallback_candidates)} checked...", end="", flush=True)
        print("\r[+] Branch tracking complete.                                            ")
    elif args.only == "missing":
        print("[*] Skipping extended Gitea branch validation rules due to '--only missing' filter constraint.")

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
            print(f"[!] Alert: {len(orphaned_repos)} verified orphans found (repos containing branch '{TARGET_BRANCH}', missing in OBS).")
            
            # Action triggered (Live or Simulation)
            if args.remove_obsoletes:
                mode_label = "DRY-RUN SIMULATION" if args.dry else "LIVE REMOVAL PHASE"
                print(f"[*] Activating {mode_label} for obsoletes cleanup. Resolving dynamic defaults...")
                
                success_count = 0
                for index, repo_info in enumerate(sorted(orphaned_repos, key=lambda x: x['original_name']), 1):
                    name = repo_info['original_name']
                    alt_branch, alt_time = analyze_alternative_branches(name)
                    
                    if not args.dry:
                        print(f"    [{index}/{len(orphaned_repos)}] Processing branch transitions for {name}...", end="", flush=True)
                        if alt_branch:
                            print(f" [Switching default to track '{alt_branch}' ({alt_time})]...", end="", flush=True)
                        
                    if process_orphan_branch(name, repo_info['default_branch'], alt_branch, dry_run=args.dry):
                        if not args.dry:
                            print(" SUCCESS.")
                        success_count += 1
                    else:
                        print(" FAILED.")
                        
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
            print(f"[+] Success! No orphaned repositories containing branch '{TARGET_BRANCH}' found in Gitea pool.")
        print("=" * 60 + "\n")

if __name__ == "__main__":
    main()
