#!/usr/bin/env python3
"""
openSUSE Workspace Downstream Checker
Checks either:
  1. Repository sync status: pool:factory -> devel:factory -> devel:next (default)
  2. Package version status: compares factory and next against release-monitoring.org (--version)
  3. Staging/forward status: checks which next branches can be forwarded to factory (--forward)
"""

import os
import sys
import json
import re
import subprocess
import concurrent.futures
import unicodedata
from collections import defaultdict

# ANSI Color Codes
GREEN = "\x1b[32m"
RED = "\x1b[31m"
YELLOW = "\x1b[33m"
CYAN = "\x1b[36m"
BOLD = "\x1b[1m"
RESET = "\x1b[0m"

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
    pool_url = f"https://src.opensuse.org/pool/{repo_name}.git"
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


def check_repo_version(repo_name):
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

    # 3. Get version from factory branch
    factory_ver = None
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

    # 4. Get version from next branch (if it exists)
    next_ver = None
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
    
    if factory_ver and upstream_stable and factory_ver != upstream_stable:
        needs_update = True
        
    if next_ver and upstream_latest and next_ver != upstream_latest:
        needs_update = True

    return repo_name, {
        "status": "success",
        "factory_ver": factory_ver or "N/A",
        "next_ver": next_ver or "—",
        "upstream_stable": upstream_stable or "N/A",
        "upstream_latest": upstream_latest or "—",
        "needs_update": needs_update
    }


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


def run_sync_check(repos):
    total = len(repos)
    print(f"Checking {total} packages in parallel (50 threads)...", file=sys.stderr)
    
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        futures = {executor.submit(check_repo_sync, repo): repo for repo in repos}
        completed = 0
        for future in concurrent.futures.as_completed(futures):
            repo_name, data = future.result()
            results[repo_name] = data
            completed += 1
            if completed % 50 == 0 or completed == total:
                print(f"Progress: {completed}/{total} checked...", end='\r', file=sys.stderr)
    print("", file=sys.stderr)

    has_next_count = sum(1 for r, d in results.items() if d.get("status") == "success" and d.get("next_status") != "No next branch")

    rows = []
    for repo in sorted(results.keys()):
        data = results[repo]
        if data["status"] != "success":
            continue
        if data["needs_action"]:
            # Stage 1 format
            if data["pool_behind"] > 0 and data["pool_ahead"] > 0:
                stage1_str = f"{RED}❌ Diverged (Behind {data['pool_behind']}, Ahead {data['pool_ahead']}){RESET}"
            elif data["pool_behind"] > 0:
                stage1_str = f"{RED}📥 **Behind by {data['pool_behind']}** (needs pull){RESET}"
            elif data["pool_ahead"] > 0:
                stage1_str = f"{CYAN}📤 **Ahead by {data['pool_ahead']}** (needs submit){RESET}"
            elif data["pool_status"] == "Not in Pool":
                stage1_str = f"{YELLOW}❓ Not in Pool{RESET}"
            else:
                stage1_str = f"{GREEN}✅ In Sync{RESET}"
                
            # Stage 2 format
            if data["next_status"] == "No next branch":
                stage2_str = "—"
            elif data["next_behind"] > 0 and data["next_ahead"] > 0:
                stage2_str = f"{RED}❌ Diverged (Behind {data['next_behind']}, Ahead {data['next_ahead']}){RESET}"
            elif data["next_behind"] > 0:
                stage2_str = f"{RED}📥 **Behind by {data['next_behind']}** (needs merge){RESET}"
            elif data["next_ahead"] > 0:
                stage2_str = f"{GREEN}✅ In Sync (+{data['next_ahead']} next commits){RESET}"
            else:
                stage2_str = f"{GREEN}✅ In Sync{RESET}"
                
            # Recommended Actions
            actions = []
            if data["pool_behind"] > 0:
                actions.append("📥 Pull Pool")
            if data["pool_ahead"] > 0:
                actions.append("📤 Submit Pool")
            if data["next_behind"] > 0:
                actions.append("🔀 Merge next")
            action_str = " and ".join(actions) if actions else "None"
            
            rows.append((repo, stage1_str, stage2_str, action_str))

    headers = [
        "Package", 
        "Stage 1: pool ➔ devel:factory", 
        "Stage 2: devel:factory ➔ devel:next", 
        "Recommended Action"
    ]
    
    w1 = max(visual_len(headers[0]), max(visual_len(r[0]) for r in rows) if rows else 0)
    w2 = max(visual_len(headers[1]), max(visual_len(r[1]) for r in rows) if rows else 0)
    w3 = max(visual_len(headers[2]), max(visual_len(r[2]) for r in rows) if rows else 0)
    w4 = max(visual_len(headers[3]), max(visual_len(r[3]) for r in rows) if rows else 0)

    print(f"Total Packages: {total} | Packages with 'next' branch: {has_next_count}\n")
    print(f"### 🔄 Downstream Sync Table ({BOLD}Action Required{RESET})\n")
    print(f"| {pad_left(headers[0], w1)} | {pad_left(headers[1], w2)} | {pad_left(headers[2], w3)} | {pad_left(headers[3], w4)} |")
    print(f"| {'-'*w1} | {'-'*w2} | {'-'*w3} | {'-'*w4} |")
    
    for r in rows:
        print(f"| {pad_left(r[0], w1)} | {pad_left(r[1], w2)} | {pad_left(r[2], w3)} | {pad_left(r[3], w4)} |")

    print(f"\n*Total packages requiring sync action: {len(rows)}*")
    print(f"\n💡 {BOLD}Tip:{RESET} Try './check_sync.py --version' to check upstream releases, or '--forward' to find forwardable next branches.")


def run_version_check(repos):
    total = len(repos)
    # Be gentle with release-monitoring.org by using 30 parallel workers
    print(f"Checking versions for {total} packages against release-monitoring.org (30 threads)...", file=sys.stderr)
    
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as executor:
        futures = {executor.submit(check_repo_version, repo): repo for repo in repos}
        completed = 0
        for future in concurrent.futures.as_completed(futures):
            repo_name, data = future.result()
            results[repo_name] = data
            completed += 1
            if completed % 25 == 0 or completed == total:
                print(f"Progress: {completed}/{total} checked...", end='\r', file=sys.stderr)
    print("", file=sys.stderr)

    rows = []
    up_to_date_count = 0
    api_error_count = 0
    missing_spec_count = 0
    
    for repo in sorted(results.keys()):
        data = results[repo]
        if data["status"] == "error":
            missing_spec_count += 1
            continue
            
        if data["status"] == "partial_error":
            api_error_count += 1
            continue
            
        if data["needs_update"]:
            # Format factory columns
            if data["factory_ver"] != data["upstream_stable"]:
                f_local = f"{RED}{data['factory_ver']}{RESET}"
                f_up = f"{GREEN}{data['upstream_stable']}{RESET}"
            else:
                f_local = f"{GREEN}{data['factory_ver']}{RESET}"
                f_up = f"{GREEN}{data['upstream_stable']}{RESET}"
                
            # Format next columns
            if data["next_ver"] != "—" and data["next_ver"] != data["upstream_latest"]:
                n_local = f"{RED}{data['next_ver']}{RESET}"
                n_up = f"{GREEN}{data['upstream_latest']}{RESET}"
            else:
                # If they match or there's no next branch
                n_local = f"{GREEN}{data['next_ver']}{RESET}" if data["next_ver"] != "—" else "—"
                n_up = f"{GREEN}{data['upstream_latest']}{RESET}" if data["upstream_latest"] != "—" else "—"

            rows.append((
                repo,
                f_local,
                f_up,
                n_local,
                n_up
            ))
        else:
            up_to_date_count += 1

    headers = [
        "Package", 
        "Local (factory)", 
        "Upstream (stable)", 
        "Local (next)", 
        "Upstream (unstable)"
    ]
    
    w1 = max(visual_len(headers[0]), max(visual_len(r[0]) for r in rows) if rows else 0)
    w2 = max(visual_len(headers[1]), max(visual_len(r[1]) for r in rows) if rows else 0)
    w3 = max(visual_len(headers[2]), max(visual_len(r[2]) for r in rows) if rows else 0)
    w4 = max(visual_len(headers[3]), max(visual_len(r[3]) for r in rows) if rows else 0)
    w5 = max(visual_len(headers[4]), max(visual_len(r[4]) for r in rows) if rows else 0)

    print(f"Total Checked: {total} | Up to date: {up_to_date_count} | API errors: {api_error_count} | Missing specs: {missing_spec_count}\n")
    print(f"### 📦 Upstream Version Sync Table ({BOLD}Updates Available{RESET})\n")
    print(f"| {pad_left(headers[0], w1)} | {pad_left(headers[1], w2)} | {pad_left(headers[2], w3)} | {pad_left(headers[3], w4)} | {pad_left(headers[4], w5)} |")
    print(f"| {'-'*w1} | {'-'*w2} | {'-'*w3} | {'-'*w4} | {'-'*w5} |")
    
    for r in rows:
        print(f"| {pad_left(r[0], w1)} | {pad_left(r[1], w2)} | {pad_left(r[2], w3)} | {pad_left(r[3], w4)} | {pad_left(r[4], w5)} |")

    print(f"\n*Total packages that could use an update: {len(rows)}*")


def run_forward_check(repos):
    total = len(repos)
    print(f"Checking {total} packages for forwardable next branches (50 threads)...", file=sys.stderr)
    
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        futures = {executor.submit(check_repo_sync, repo): repo for repo in repos}
        completed = 0
        for future in concurrent.futures.as_completed(futures):
            repo_name, data = future.result()
            results[repo_name] = data
            completed += 1
            if completed % 50 == 0 or completed == total:
                print(f"Progress: {completed}/{total} checked...", end='\r', file=sys.stderr)
    print("", file=sys.stderr)

    rows = []
    ready_count = 0
    needs_merge_count = 0
    
    for repo in sorted(results.keys()):
        data = results[repo]
        if data["status"] != "success":
            continue
        if data["next_status"] != "No next branch" and data["next_status"] != "N/A":
            next_ahead = data["next_ahead"]
            next_behind = data["next_behind"]
            
            if next_ahead > 0:
                if next_behind == 0:
                    status_str = f"{GREEN}✅ Ready (Clean Forward){RESET}"
                    ready_count += 1
                else:
                    status_str = f"{YELLOW}⚠️ Needs Merge first (Behind {next_behind}){RESET}"
                    needs_merge_count += 1
                    
                rows.append((
                    repo,
                    f"{GREEN}{next_ahead}{RESET}",
                    f"{RED}{next_behind}{RESET}" if next_behind > 0 else f"{GREEN}0{RESET}",
                    status_str
                ))

    headers = [
        "Package", 
        "Commits Ahead (on next)", 
        "Commits Behind (from factory)", 
        "Forward Status"
    ]
    
    w1 = max(visual_len(headers[0]), max(visual_len(r[0]) for r in rows) if rows else 0)
    w2 = max(visual_len(headers[1]), max(visual_len(r[1]) for r in rows) if rows else 0)
    w3 = max(visual_len(headers[2]), max(visual_len(r[2]) for r in rows) if rows else 0)
    w4 = max(visual_len(headers[3]), max(visual_len(r[3]) for r in rows) if rows else 0)

    print(f"Total Packages: {total} | Ready to Forward: {ready_count} | Needs Merge: {needs_merge_count}\n")
    print(f"### 🔀 Next-to-Factory Forwarding Table\n")
    print(f"| {pad_left(headers[0], w1)} | {pad_left(headers[1], w2)} | {pad_left(headers[2], w3)} | {pad_left(headers[3], w4)} |")
    print(f"| {'-'*w1} | {'-'*w2} | {'-'*w3} | {'-'*w4} |")
    
    for r in rows:
        print(f"| {pad_left(r[0], w1)} | {pad_left(r[1], w2)} | {pad_left(r[2], w3)} | {pad_left(r[3], w4)} |")

    print(f"\n*Total packages with next-specific staging commits: {len(rows)}*")


def print_help():
    print(f"{BOLD}openSUSE Workspace Downstream Checker{RESET}")
    print("\nAn optimized parallel dashboard for openSUSE package maintainers.")
    print("\nUsage:")
    print("  ./check_sync.py [options]")
    print("\nModes (choose exactly one):")
    print(f"  {BOLD}(default){RESET}          Checks downstream git sync status (pool ➔ devel:factory ➔ devel:next).")
    print(f"  {BOLD}--todo{RESET}             Alias for the default sync checking mode.")
    print(f"  {BOLD}--version, -v{RESET}      Compares package versions in factory & next against release-monitoring.org.")
    print(f"  {BOLD}--forward, -f{RESET}      Identifies which next branches can be cleanly forwarded to factory.")
    print("\nOptions:")
    print(f"  {BOLD}-h, --help{RESET}         Show this help message and exit.")


def main():
    if "-h" in sys.argv or "--help" in sys.argv:
        print_help()
        sys.exit(0)
    
    repos = sorted([
        d for d in os.listdir('.') 
        if os.path.isdir(d) and os.path.exists(os.path.join(d, '.git'))
    ])
    
    show_versions = "--version" in sys.argv or "-v" in sys.argv
    show_forward = "--forward" in sys.argv or "-f" in sys.argv
    show_todo = "--todo" in sys.argv
    
    if show_versions:
        run_version_check(repos)
    elif show_forward:
        run_forward_check(repos)
    else:
        run_sync_check(repos)

if __name__ == '__main__':
    try:
        main()
    except BrokenPipeError:
        # Handle BrokenPipeError gracefully when piping output to commands like head or less
        try:
            sys.stdout.close()
        except Exception:
            pass
        try:
            sys.stderr.close()
        except Exception:
            pass
        sys.exit(0)
