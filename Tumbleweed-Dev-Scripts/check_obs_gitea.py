import subprocess
import sys
import requests
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# Configuration
OBS_PROJECT = "openSUSE:Factory"
GITEA_API_URL = "https://src.opensuse.org/api/v1"
GITEA_ORG = "pool"
TARGET_BRANCH = "factory"
MAX_WORKERS = 25  # Number of concurrent network threads for the fallback check

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
    
    while True:
        url = f"{GITEA_API_URL}/orgs/{GITEA_ORG}/repos?page={page}&limit={limit}&sort=name"
        
        max_retries = 5
        for attempt in range(1, max_retries + 1):
            try:
                response = requests.get(url, timeout=30)
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
    """Hits the specific Gitea branch endpoint to confirm branch presence. Returns (repo_name, exists)."""
    url = f"{GITEA_API_URL}/repos/{GITEA_ORG}/{repo_name}/branches/{branch_name}"
    try:
        res = requests.get(url, timeout=10)
        return repo_name, (res.status_code == 200)
    except requests.RequestException:
        return repo_name, False

def main():
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

    print("[*] Auditing: Gitea -> OBS (Validating true orphans with concurrent branch check)...")
    orphaned_in_gitea = []
    
    # Isolate unmapped candidates
    potential_orphans = {k: v for k, v in gitea_repos.items() if k not in gitea_mapped_names}
    
    # Pre-filter using local metadata to minimize necessary network hits
    fallback_candidates = []
    for repo_lower, info in potential_orphans.items():
        if info['is_empty']:
            continue
        if info['default_branch'].lower() == TARGET_BRANCH.lower():
            orphaned_in_gitea.append(info['original_name'])
        else:
            fallback_candidates.append(info['original_name'])

    print(f"[*] Found {len(potential_orphans)} unmapped repos ({len(orphaned_in_gitea)} verified via default_branch).")
    print(f"[*] Launching {MAX_WORKERS} parallel threads to check the remaining {len(fallback_candidates)} candidates...")

    # Execute concurrent branch verification calls
    completed_count = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(check_branch_exists, repo, TARGET_BRANCH): repo for repo in fallback_candidates}
        
        for future in as_completed(futures):
            repo_name, branch_exists = future.result()
            if branch_exists:
                orphaned_in_gitea.append(repo_name)
            
            completed_count += 1
            if completed_count % 100 == 0 or completed_count == len(fallback_candidates):
                print(f"\r    -> Progress: {completed_count}/{len(fallback_candidates)} checked...", end="", flush=True)
                
    print("\r[+] Branch tracking complete.                                            ")

    # Output Report
    print("\n" + "=" * 60)
    if missing_in_gitea:
        print(f"[!] Alert: {len(missing_in_gitea)} packages are missing on src.opensuse.org:")
        print(f"{'OBS Package Name':<35} -> {'Expected Gitea Repo Name'}")
        print("-" * 60)
        for obs_name, expected_git in sorted(missing_in_gitea):
            print(f"{obs_name:<35} -> {expected_git}")
    else:
        print("[+] Success! All OBS packages exist in Gitea.")
        
    print("\n" + "=" * 60)
    if orphaned_in_gitea:
        print(f"[!] Alert: {len(orphaned_in_gitea)} verified orphans (repos with '{TARGET_BRANCH}' branch, missing in OBS):")
        print("-" * 60)
        for git_name in sorted(orphaned_in_gitea):
            print(f"  - {git_name}")
    else:
        print(f"[+] Success! No orphaned repositories containing branch '{TARGET_BRANCH}' found in Gitea pool.")
    print("=" * 60 + "\n")

if __name__ == "__main__":
    main()
