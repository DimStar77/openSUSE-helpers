#!/usr/bin/env python3
"""
openSUSE Workspace Downstream Checker CLI
CLI Frontend relying on sync_backend.py for core logic.
"""

import os
import sys
import concurrent.futures

# Make sure we can import sync_backend from helpers/ by resolving symlinks
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), 'helpers'))
import sync_backend as sb

def run_sync_check(repos):
    total = len(repos)
    print(f"Checking {total} packages in parallel (50 threads)...", file=sys.stderr)

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        futures = {executor.submit(sb.check_repo_sync, repo): repo for repo in repos}
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
                stage1_str = f"{sb.RED}❌ Diverged (Behind {data['pool_behind']}, Ahead {data['pool_ahead']}){sb.RESET}"
            elif data["pool_behind"] > 0:
                stage1_str = f"{sb.RED}📥 **Behind by {data['pool_behind']}** (needs pull){sb.RESET}"
            elif data["pool_ahead"] > 0:
                stage1_str = f"{sb.CYAN}📤 **Ahead by {data['pool_ahead']}** (needs submit){sb.RESET}"
            elif data["pool_status"] == "Not in Pool":
                stage1_str = f"{sb.YELLOW}❓ Not in Pool{sb.RESET}"
            else:
                stage1_str = f"{sb.GREEN}✅ In Sync{sb.RESET}"

            # Stage 2 format
            if data["next_status"] == "No next branch":
                stage2_str = "—"
            elif data["next_behind"] > 0 and data["next_ahead"] > 0:
                stage2_str = f"{sb.RED}❌ Diverged (Behind {data['next_behind']}, Ahead {data['next_ahead']}){sb.RESET}"
            elif data["next_behind"] > 0:
                stage2_str = f"{sb.RED}📥 **Behind by {data['next_behind']}** (needs merge){sb.RESET}"
            elif data["next_ahead"] > 0:
                stage2_str = f"{sb.GREEN}✅ In Sync (+{data['next_ahead']} next commits){sb.RESET}"
            else:
                stage2_str = f"{sb.GREEN}✅ In Sync{sb.RESET}"

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

    w1 = max(sb.visual_len(headers[0]), max(sb.visual_len(r[0]) for r in rows) if rows else 0)
    w2 = max(sb.visual_len(headers[1]), max(sb.visual_len(r[1]) for r in rows) if rows else 0)
    w3 = max(sb.visual_len(headers[2]), max(sb.visual_len(r[2]) for r in rows) if rows else 0)
    w4 = max(sb.visual_len(headers[3]), max(sb.visual_len(r[3]) for r in rows) if rows else 0)

    print(f"Total Packages: {total} | Packages with 'next' branch: {has_next_count}\n")
    print(f"### 🔄 Downstream Sync Table ({sb.BOLD}Action Required{sb.RESET})\n")
    print(f"| {sb.pad_left(headers[0], w1)} | {sb.pad_left(headers[1], w2)} | {sb.pad_left(headers[2], w3)} | {sb.pad_left(headers[3], w4)} |")
    print(f"| {'-'*w1} | {'-'*w2} | {'-'*w3} | {'-'*w4} |")

    for r in rows:
        print(f"| {sb.pad_left(r[0], w1)} | {sb.pad_left(r[1], w2)} | {sb.pad_left(r[2], w3)} | {sb.pad_left(r[3], w4)} |")

    print(f"\n*Total packages requiring sync action: {len(rows)}*")
    print(f"\n💡 {sb.BOLD}Tip:{sb.RESET} Try './check_sync.py --version' to check upstream releases, or '--forward' to find forwardable next branches.")


def run_version_check(repos, branch=None):
    total = len(repos)
    # Be gentle with release-monitoring.org by using 30 parallel workers
    if branch:
        print(f"Checking {branch} versions for {total} packages against release-monitoring.org (30 threads)...", file=sys.stderr)
    else:
        print(f"Checking versions for {total} packages against release-monitoring.org (30 threads)...", file=sys.stderr)

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as executor:
        futures = {executor.submit(sb.check_repo_version, repo, branch): repo for repo in repos}
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
            if branch == "factory":
                # Only factory columns
                if data["factory_ver"] != data["upstream_stable"]:
                    f_local = f"{sb.RED}{data['factory_ver']}{sb.RESET}"
                    f_up = f"{sb.GREEN}{data['upstream_stable']}{sb.RESET}"
                else:
                    f_local = f"{sb.GREEN}{data['factory_ver']}{sb.RESET}"
                    f_up = f"{sb.GREEN}{data['upstream_stable']}{sb.RESET}"
                rows.append((repo, f_local, f_up))

            elif branch == "next":
                # Only next columns
                if data["next_ver"] != "—" and data["next_ver"] != data["upstream_latest"]:
                    n_local = f"{sb.RED}{data['next_ver']}{sb.RESET}"
                    n_up = f"{sb.GREEN}{data['upstream_latest']}{sb.RESET}"
                else:
                    n_local = f"{sb.GREEN}{data['next_ver']}{sb.RESET}" if data["next_ver"] != "—" else "—"
                    n_up = f"{sb.GREEN}{data['upstream_latest']}{sb.RESET}" if data["upstream_latest"] != "—" else "—"
                rows.append((repo, n_local, n_up))

            else:
                # Default (both branches)
                # Format factory columns
                if data["factory_ver"] != data["upstream_stable"]:
                    f_local = f"{sb.RED}{data['factory_ver']}{sb.RESET}"
                    f_up = f"{sb.GREEN}{data['upstream_stable']}{sb.RESET}"
                else:
                    f_local = f"{sb.GREEN}{data['factory_ver']}{sb.RESET}"
                    f_up = f"{sb.GREEN}{data['upstream_stable']}{sb.RESET}"

                # Format next columns
                if data["next_ver"] != "—" and data["next_ver"] != data["upstream_latest"]:
                    n_local = f"{sb.RED}{data['next_ver']}{sb.RESET}"
                    n_up = f"{sb.GREEN}{data['upstream_latest']}{sb.RESET}"
                else:
                    # If they match or there's no next branch
                    n_local = f"{sb.GREEN}{data['next_ver']}{sb.RESET}" if data["next_ver"] != "—" else "—"
                    n_up = f"{sb.GREEN}{data['upstream_latest']}{sb.RESET}" if data["upstream_latest"] != "—" else "—"

                rows.append((
                    repo,
                    f_local,
                    f_up,
                    n_local,
                    n_up
                ))
        else:
            up_to_date_count += 1

    if branch == "factory":
        headers = [
            "Package",
            "Local (factory)",
            "Upstream (stable)"
        ]
    elif branch == "next":
        headers = [
            "Package",
            "Local (next)",
            "Upstream (unstable)"
        ]
    else:
        headers = [
            "Package",
            "Local (factory)",
            "Upstream (stable)",
            "Local (next)",
            "Upstream (unstable)"
        ]

    widths = []
    for i, h in enumerate(headers):
        w = max(sb.visual_len(h), max(sb.visual_len(r[i]) for r in rows) if rows else 0)
        widths.append(w)

    print(f"Total Checked: {total} | Up to date: {up_to_date_count} | API errors: {api_error_count} | Missing specs: {missing_spec_count}\n")
    title_branch = f" ({branch})" if branch else ""
    print(f"### 📦 Upstream Version Sync Table{title_branch} ({sb.BOLD}Updates Available{sb.RESET})\n")

    header_str = " | ".join(sb.pad_left(headers[i], widths[i]) for i in range(len(headers)))
    sep_str = " | ".join('-'*widths[i] for i in range(len(headers)))
    print(f"| {header_str} |")
    print(f"| {sep_str} |")

    for r in rows:
        row_str = " | ".join(sb.pad_left(r[i], widths[i]) for i in range(len(headers)))
        print(f"| {row_str} |")

    print(f"\n*Total packages that could use an update: {len(rows)}*")


def run_forward_check(repos, show_pr=False, only_no_pr=False):
    total = len(repos)
    print(f"Checking {total} packages for forwardable next branches (50 threads)...", file=sys.stderr)

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        futures = {executor.submit(sb.check_repo_sync, repo): repo for repo in repos}
        completed = 0
        for future in concurrent.futures.as_completed(futures):
            repo_name, data = future.result()
            results[repo_name] = data
            completed += 1
            if completed % 50 == 0 or completed == total:
                print(f"Progress: {completed}/{total} checked...", end='\r', file=sys.stderr)
    print("", file=sys.stderr)

    # Filter to packages with next branches that are ahead
    forward_candidates = []
    for repo in sorted(results.keys()):
        data = results[repo]
        if data["status"] != "success":
            continue
        if data["next_status"] != "No next branch" and data["next_status"] != "N/A":
            next_ahead = data["next_ahead"]
            if next_ahead > 0:
                forward_candidates.append(repo)

    # Check Gitea PRs in parallel if requested (or required by only_no_pr)
    pr_status = {}
    if (show_pr or only_no_pr) and forward_candidates:
        print(f"Checking Gitea pull requests for {len(forward_candidates)} candidates (20 threads)...", file=sys.stderr)
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            pr_futures = {executor.submit(sb.check_repo_pr, repo): repo for repo in forward_candidates}
            completed_prs = 0
            for future in concurrent.futures.as_completed(pr_futures):
                repo_name, pr_data = future.result()
                pr_status[repo_name] = pr_data
                completed_prs += 1
                if completed_prs % 10 == 0 or completed_prs == len(forward_candidates):
                    print(f"PR Progress: {completed_prs}/{len(forward_candidates)} checked...", end='\r', file=sys.stderr)
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
                pr_info = pr_status.get(repo, {"has_pr": False, "url": None, "number": None})

                # If filtering for only no PR, skip repositories that have a pending PR!
                if only_no_pr and pr_info["has_pr"]:
                    continue

                if next_behind == 0:
                    status_str = f"{sb.GREEN}✅ Ready (Clean Forward){sb.RESET}"
                    ready_count += 1
                else:
                    status_str = f"{sb.YELLOW}⚠️ Needs Merge first (Behind {next_behind}){sb.RESET}"
                    needs_merge_count += 1

                if show_pr or only_no_pr:
                    # Determine PR Status display
                    if pr_info["has_pr"]:
                        pr_display = f"{sb.GREEN}Yes (PR #{pr_info['number']}){sb.RESET}"
                    else:
                        pr_display = f"{sb.RED}No{sb.RESET}"

                    rows.append((
                        repo,
                        f"{sb.GREEN}{next_ahead}{sb.RESET}",
                        f"{sb.RED}{next_behind}{sb.RESET}" if next_behind > 0 else f"{sb.GREEN}0{sb.RESET}",
                        status_str,
                        pr_display
                    ))
                else:
                    rows.append((
                        repo,
                        f"{sb.GREEN}{next_ahead}{sb.RESET}",
                        f"{sb.RED}{next_behind}{sb.RESET}" if next_behind > 0 else f"{sb.GREEN}0{sb.RESET}",
                        status_str
                    ))

    if show_pr or only_no_pr:
        headers = [
            "Package",
            "Commits Ahead (on next)",
            "Commits Behind (from factory)",
            "Forward Status",
            "PR Pending?"
        ]
    else:
        headers = [
            "Package",
            "Commits Ahead (on next)",
            "Commits Behind (from factory)",
            "Forward Status"
        ]

    widths = []
    for i, h in enumerate(headers):
        w = max(sb.visual_len(h), max(sb.visual_len(r[i]) for r in rows) if rows else 0)
        widths.append(w)

    print(f"Total Packages: {total} | Ready to Forward: {ready_count} | Needs Merge: {needs_merge_count}\n")
    print(f"### 🔀 Next-to-Factory Forwarding Table\n")

    header_str = " | ".join(sb.pad_left(headers[i], widths[i]) for i in range(len(headers)))
    sep_str = " | ".join('-'*widths[i] for i in range(len(headers)))
    print(f"| {header_str} |")
    print(f"| {sep_str} |")

    for r in rows:
        row_str = " | ".join(sb.pad_left(r[i], widths[i]) for i in range(len(headers)))
        print(f"| {row_str} |")

    print(f"\n*Total packages displayed: {len(rows)}*")


def print_help():
    print(f"{sb.BOLD}openSUSE Workspace Downstream Checker{sb.RESET}")
    print("\nAn optimized parallel dashboard for openSUSE package maintainers.")
    print("\nUsage:")
    print("  ./check_sync.py [options]")
    print("\nModes (choose exactly one):")
    print(f"  {sb.BOLD}(default){sb.RESET}          Checks downstream git sync status (pool ➔ devel:factory ➔ devel:next).")
    print(f"  {sb.BOLD}--todo{sb.RESET}             Alias for the default sync checking mode.")
    print(f"  {sb.BOLD}--version, -v [branch]{sb.RESET}")
    print("                     Compares package versions in factory & next against release-monitoring.org.")
    print("                     Optional branch: 'factory' or 'next' to filter output.")
    print(f"  {sb.BOLD}--forward, -f{sb.RESET}      Identifies which next branches can be cleanly forwarded to factory.")
    print("\nOptions:")
    print(f"  {sb.BOLD}-p, --pr{sb.RESET}           Include Gitea pull request pending status in forwarding mode.")
    print(f"  {sb.BOLD}--no-pr{sb.RESET}            In forwarding mode, only show packages without a pending pull request.")
    print(f"  {sb.BOLD}-b, --branch <branch>{sb.RESET}")
    print("                     Limit version checks to a specific branch ('factory' or 'next').")
    print(f"  {sb.BOLD}-h, --help{sb.RESET}         Show this help message and exit.")


def main():
    if "-h" in sys.argv or "--help" in sys.argv:
        print_help()
        sys.exit(0)

    repos = sorted([
        d for d in os.listdir('.')
        if os.path.isdir(d) and os.path.exists(os.path.join(d, '.git'))
    ])

    show_versions = False
    version_branch = None

    # Check for --version or -v with equal sign, e.g. --version=next
    for arg in sys.argv:
        if arg.startswith("--version=") or arg.startswith("-v="):
            show_versions = True
            val = arg.split("=", 1)[1].lower()
            if val in ("next", "factory"):
                version_branch = val

    # Check for standalone --version or -v
    if not show_versions:
        if "--version" in sys.argv or "-v" in sys.argv:
            show_versions = True

    # Check for --branch or -b
    for opt in ("--branch", "-b"):
        if opt in sys.argv:
            idx = sys.argv.index(opt)
            if idx + 1 < len(sys.argv):
                val = sys.argv[idx + 1].lower()
                if val in ("next", "factory"):
                    version_branch = val

    # Check if 'next' or 'factory' is in sys.argv directly following -v/--version
    if show_versions and not version_branch:
        for opt in ("--version", "-v"):
            if opt in sys.argv:
                idx = sys.argv.index(opt)
                if idx + 1 < len(sys.argv):
                    val = sys.argv[idx + 1].lower()
                    if val in ("next", "factory"):
                        version_branch = val

    show_forward = "--forward" in sys.argv or "-f" in sys.argv
    show_todo = "--todo" in sys.argv
    show_no_pr = "--no-pr" in sys.argv
    show_pr = "--pr" in sys.argv or "-p" in sys.argv

    if show_versions:
        run_version_check(repos, version_branch)
    elif show_forward:
        run_forward_check(repos, show_pr, show_no_pr)
    else:
        run_sync_check(repos)

    sys.stdout.flush()
    sys.stderr.flush()

if __name__ == '__main__':
    try:
        main()
    except BrokenPipeError:
        # Handle BrokenPipeError gracefully when piping output to commands like head or less
        # Use os._exit(0) to bypass Python's high-level interpreter shutdown stream flushing.
        import os
        os._exit(0)
