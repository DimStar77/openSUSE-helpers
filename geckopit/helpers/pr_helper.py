#!/usr/bin/env python3
"""
Helper for fetching and merging contributor Gitea Pull Requests onto HEAD.
Preserves original commit hashes, attribution, and signatures via fast-forward
when HEAD is an ancestor of the PR (such as forwarding next into factory).
For non-fast-forward merges, performs a clean merge preserving PR commits
in the history graph, validating that .changes remains chronologically sorted,
and only redating if chronological ordering is broken.
Supports squashing multi-commit PRs into atomic commits when explicitly requested.
Includes a spec collision guard to safely leave changes staged when patch conflicts are detected.
"""

import glob
import os
import re
import sys
import time
import datetime
import subprocess
from typing import Optional, Dict, Any, List, Tuple

# Ensure terminal colors if available
BOLD = "\x1b[1m"
CYAN = "\x1b[36m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
RESET = "\x1b[0m"

def check_spec_collisions_content(content: str) -> List[str]:
    """
    Scans .spec file content for duplicate Patch<N> or Source<N> declarations that
    may indicate an upstream/branch merge collision needing maintainer review.
    """
    collisions = []
    seen_tags = {}
    re_tag = re.compile(r'^\s*(Patch|Source)(\d+)\s*:\s*(.+)$', re.IGNORECASE)

    for idx, line in enumerate(content.splitlines(), 1):
        m = re_tag.match(line)
        if m:
            tag_type = m.group(1).capitalize()
            tag_num = m.group(2)
            tag_name = f"{tag_type}{tag_num}"
            val = m.group(3).strip()
            if tag_name in seen_tags:
                prev_idx, prev_val = seen_tags[tag_name]
                collisions.append(
                    f"Duplicate {tag_name}: on line {idx} ('{val}') conflicts with line {prev_idx} ('{prev_val}')"
                )
            else:
                seen_tags[tag_name] = (idx, val)

    return collisions

def check_spec_collisions(spec_path: str) -> List[str]:
    """Scans a .spec file path for duplicate Patch<N> or Source<N> declarations."""
    if not os.path.isfile(spec_path):
        return []
    try:
        with open(spec_path, 'r', encoding='utf-8', errors='ignore') as f:
            return check_spec_collisions_content(f.read())
    except Exception:
        return []

def parse_changelog_date(header: str) -> Optional[datetime.datetime]:
    """Parses standard openSUSE .changes header timestamp into a datetime object."""
    parts = header.split(" - ", 1)
    if len(parts) < 2:
        parts = header.split("- ", 1)
    date_str = parts[0].strip()
    clean_date = re.sub(r'\s+', ' ', date_str)
    tokens = clean_date.split()
    if len(tokens) >= 5:
        try:
            if len(tokens) == 6:
                no_tz = f"{tokens[0]} {tokens[1]} {tokens[2]} {tokens[3]} {tokens[5]}"
            else:
                no_tz = " ".join(tokens[:5])
            return datetime.datetime.strptime(no_tz, "%a %b %d %H:%M:%S %Y")
        except Exception:
            pass
    try:
        return datetime.datetime.strptime(clean_date[:10], "%Y-%m-%d")
    except Exception:
        pass
    return None

def is_changes_chronologically_sorted(content: str) -> bool:
    """
    Returns True if all parsed changelog entries in content are in
    valid non-increasing (reverse-chronological) order from top to bottom.
    """
    raw_entries = content.split("-------------------------------------------------------------------\n")
    dates = []
    for raw in raw_entries:
        raw_str = raw.strip()
        if not raw_str:
            continue
        header = raw_str.splitlines()[0].strip()
        dt = parse_changelog_date(header)
        if dt:
            dates.append(dt)
    for i in range(len(dates) - 1):
        if dates[i] < dates[i + 1]:
            return False
    return True

def redate_entry_header(header: str, timestamp_sec: float) -> str:
    """Replaces the entry timestamp in header with formatted system time."""
    struct_time = time.localtime(timestamp_sec)
    new_dt_str = time.strftime('%a %b %e %H:%M:%S %Z %Y', struct_time)
    parts = header.split(" - ", 1)
    if len(parts) == 2:
        return f"{new_dt_str} - {parts[1]}"
    parts = header.split("- ", 1)
    if len(parts) == 2:
        return f"{new_dt_str} - {parts[1]}"
    return f"{new_dt_str} - anonymous"

def fix_changes_chronological_order(content: str) -> str:
    """
    Redates out-of-order entries in a changelog file to current system time,
    preserving the rest of the file untouched.
    """
    raw_entries = content.split("-------------------------------------------------------------------\n")
    entries = []
    for raw in raw_entries:
        raw_str = raw.strip()
        if not raw_str:
            continue
        lines = raw_str.splitlines()
        header = lines[0].strip() if lines else ""
        entries.append((header, raw_str))

    now_ts = time.time()
    for idx in range(len(entries) - 1):
        dt_curr = parse_changelog_date(entries[idx][0])
        dt_next = parse_changelog_date(entries[idx + 1][0])
        if dt_curr and dt_next and dt_curr < dt_next:
            new_hdr = redate_entry_header(entries[idx][0], now_ts - idx)
            lines = entries[idx][1].splitlines()
            if lines:
                lines[0] = new_hdr
                new_raw = "\n".join(lines)
            else:
                new_raw = new_hdr
            entries[idx] = (new_hdr, new_raw)

    separator = "-------------------------------------------------------------------\n"
    return separator + separator.join(e[1] + "\n\n" for e in entries)

def get_pr_commits(package_dir: str, target_commit: str) -> List[str]:
    """Discovers all commits belonging to the PR in chronological order by finding the closest merge base."""
    candidates = [
        "origin/factory", "origin/next", "origin/master", "origin/main",
        "factory", "next", "master", "main", "HEAD"
    ]
    best_commits = None
    for ref in candidates:
        try:
            mb = subprocess.check_output(
                ["git", "-C", package_dir, "merge-base", target_commit, ref],
                text=True, stderr=subprocess.DEVNULL
            ).strip()
            if not mb or mb == target_commit:
                continue
            revs = subprocess.check_output(
                ["git", "-C", package_dir, "rev-list", "--reverse", f"{mb}..{target_commit}"],
                text=True, stderr=subprocess.DEVNULL
            ).splitlines()
            if revs:
                if best_commits is None or len(revs) < len(best_commits):
                    best_commits = revs
        except Exception:
            pass

    return best_commits if best_commits else [target_commit]

def get_pr_info(package_dir: str, pr_id: str) -> Optional[Dict[str, Any]]:
    """Fetches refs/pull/<pr_id>/head from origin and returns commit metadata."""
    try:
        # 1. Fetch the pull request ref from origin
        res = subprocess.run(
            ["git", "-C", package_dir, "fetch", "origin", f"pull/{pr_id}/head"],
            capture_output=True,
            text=True
        )
        if res.returncode != 0:
            # Fallback to full refspec
            res = subprocess.run(
                ["git", "-C", package_dir, "fetch", "origin", f"refs/pull/{pr_id}/head"],
                capture_output=True,
                text=True
            )
            if res.returncode != 0:
                print(f"{RED}[Error]{RESET} Failed to fetch pull request #{pr_id} from origin:\n{res.stderr}", file=sys.stderr)
                return None

        # 2. Extract commit metadata from FETCH_HEAD
        commit_sha = subprocess.check_output(
            ["git", "-C", package_dir, "rev-parse", "FETCH_HEAD"],
            text=True
        ).strip()
        author = subprocess.check_output(
            ["git", "-C", package_dir, "log", "-1", "--pretty=format:%an <%ae>", "FETCH_HEAD"],
            text=True
        ).strip()
        date = subprocess.check_output(
            ["git", "-C", package_dir, "log", "-1", "--pretty=format:%ad", "FETCH_HEAD"],
            text=True
        ).strip()
        subject = subprocess.check_output(
            ["git", "-C", package_dir, "log", "-1", "--pretty=format:%s", "FETCH_HEAD"],
            text=True
        ).strip()
        full_msg = subprocess.check_output(
            ["git", "-C", package_dir, "log", "-1", "--pretty=format:%B", "FETCH_HEAD"],
            text=True
        ).strip()
        files = subprocess.check_output(
            ["git", "-C", package_dir, "diff-tree", "--no-commit-id", "--name-only", "-r", "FETCH_HEAD"],
            text=True
        ).splitlines()

        commits = get_pr_commits(package_dir, commit_sha)

        return {
            "pr_id": pr_id,
            "commit_sha": commit_sha,
            "author": author,
            "date": date,
            "subject": subject,
            "full_msg": full_msg,
            "files": files,
            "commits": commits,
        }
    except Exception as e:
        print(f"{RED}[Error]{RESET} Exception while inspecting PR #{pr_id}: {e}", file=sys.stderr)
        return None

def merge_pull_request(
    package_dir: str,
    pr_id: str,
    auto_commit: bool = True,
    squash: bool = False
) -> bool:
    """
    Applies and merges a contributor pull request onto current branch HEAD.
    Preserves original commit hashes, attribution, and signatures via fast-forward
    when HEAD is an ancestor of the PR (such as forwarding next into factory).
    For non-fast-forward merges, performs a clean merge preserving PR commits
    in the history graph, validating that .changes remains chronologically sorted,
    and only redating if chronological ordering is broken.
    When squash=True, multiple PR commits are squashed into a single atomic commit.
    """
    pkg_dir = os.path.abspath(package_dir)
    pkg_name = os.path.basename(pkg_dir)

    # 1. Verify working directory is a git repository
    try:
        is_git = subprocess.check_output(
            ["git", "-C", pkg_dir, "rev-parse", "--is-inside-work-tree"],
            text=True, stderr=subprocess.DEVNULL
        ).strip()
        if is_git != "true":
            print(f"{RED}[Error]{RESET} {pkg_dir} is not a valid git repository.", file=sys.stderr)
            return False
    except Exception:
        print(f"{RED}[Error]{RESET} {pkg_dir} is not a valid git repository.", file=sys.stderr)
        return False

    # 2. Check for dirty working tree
    status_out = subprocess.check_output(
        ["git", "-C", pkg_dir, "status", "--porcelain"],
        text=True
    ).strip()
    if status_out:
        print(f"{RED}[Error]{RESET} Working tree is dirty. Please commit or stash changes before merging a PR.", file=sys.stderr)
        return False

    # 3. Fetch PR info
    print(f"🔄 Fetching pull request #{pr_id} from origin for {CYAN}{pkg_name}{RESET}...")
    pr_info = get_pr_info(pkg_dir, pr_id)
    if not pr_info:
        return False

    commits = pr_info.get("commits", [pr_info["commit_sha"]])
    commit_count = len(commits)
    target_sha = pr_info["commit_sha"]

    print(f"{BOLD}{'='*72}{RESET}")
    mode_label = " (SQUASH)" if (squash and commit_count > 1) else ""
    print(f"📥 {BOLD}GECKOPIT PR MERGE{mode_label}: {CYAN}{pkg_name}{RESET} (PR #{pr_id})")
    print(f"{BOLD}{'='*72}{RESET}")
    print(f"  Head Commit: {target_sha[:10]}")
    print(f"  Author:      {pr_info['author']}")
    print(f"  Date:        {pr_info['date']}")
    print(f"  Subject:     {pr_info['subject']}")
    if commit_count > 1:
        print(f"  Commits:     {commit_count} commits in PR ({', '.join(c[:7] for c in commits)})")
    print(f"  Files:       {', '.join(pr_info['files'])}")
    print(f"{BOLD}{'='*72}{RESET}")

    current_head = subprocess.check_output(
        ["git", "-C", pkg_dir, "rev-parse", "HEAD"],
        text=True
    ).strip()

    try:
        merge_base = subprocess.check_output(
            ["git", "-C", pkg_dir, "merge-base", "HEAD", target_sha],
            text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        merge_base = ""

    is_fast_forward = (merge_base == current_head)

    # 4. Fast-forward merge when HEAD is an ancestor and squash is not requested
    if is_fast_forward and not squash:
        # Check spec collisions in target commit
        spec_collisions = []
        spec_files = glob.glob(os.path.join(pkg_dir, "*.spec"))
        for sf in spec_files:
            rel_sf = os.path.relpath(sf, pkg_dir)
            try:
                spec_content = subprocess.check_output(
                    ["git", "-C", pkg_dir, "show", f"{target_sha}:{rel_sf}"],
                    text=True, stderr=subprocess.DEVNULL
                )
                cols = check_spec_collisions_content(spec_content)
                if cols:
                    spec_collisions.extend([(os.path.basename(sf), c) for c in cols])
            except Exception:
                pass

        if spec_collisions:
            print(f"\n{YELLOW}⚠️  Detected potential spec collision(s) needing maintainer adjustment:{RESET}")
            for sf_name, col_msg in spec_collisions:
                print(f"   • [{sf_name}] {col_msg}")
            # Stage changes with merge --no-ff --no-commit so maintainer can review
            subprocess.run(["git", "-C", pkg_dir, "merge", "--no-ff", "--no-commit", target_sha], capture_output=True)
            print(f"\n{CYAN}ℹ️  Changes left staged without auto-committing.{RESET}")
            print(f"   After adjusting the spec file, complete the commit with:")
            print(f"     {BOLD}git commit -c FETCH_HEAD{RESET}")
            return True

        if auto_commit:
            ff_res = subprocess.run(
                ["git", "-C", pkg_dir, "merge", "--ff-only", target_sha],
                capture_output=True,
                text=True
            )
            if ff_res.returncode == 0:
                print(f"{GREEN}🚀 Successfully fast-forwarded to PR #{pr_id} ({target_sha[:7]})!{RESET}")
                print(f"   Preserved original commit hashes, attribution, and signatures without alterations.")
                log_out = subprocess.check_output(
                    ["git", "-C", pkg_dir, "--no-pager", "log", "-1", "--oneline"],
                    text=True
                ).strip()
                print(f"   {BOLD}{log_out}{RESET}")
                return True
            else:
                print(f"{RED}❌ Fast-forward merge failed:{RESET}\n{ff_res.stderr}", file=sys.stderr)
                return False
        else:
            subprocess.run(["git", "-C", pkg_dir, "merge", "--no-ff", "--no-commit", target_sha], check=True)
            print(f"ℹ️  Changes from PR #{pr_id} staged with '--no-commit'. Review staged diff and commit when ready.")
            return True

    # 5. Non-fast-forward 3-way merge
    elif not squash:
        merge_res = subprocess.run(
            ["git", "-C", pkg_dir, "merge", "--no-commit", target_sha],
            capture_output=True,
            text=True
        )
        if merge_res.returncode != 0:
            conflicts = subprocess.check_output(
                ["git", "-C", pkg_dir, "diff", "--name-only", "--diff-filter=U"],
                text=True
            ).splitlines()
            if conflicts:
                print(f"{RED}❌ Conflict encountered during merge of PR #{pr_id}:{RESET} {', '.join(conflicts)}", file=sys.stderr)
                print("   The merge driver resolved changelog/spec where possible.", file=sys.stderr)
                print(f"   Please inspect and resolve remaining conflicts, then commit with:", file=sys.stderr)
                print(f"     git commit", file=sys.stderr)
                return False
            else:
                print(f"{RED}❌ Merge failed for PR #{pr_id}:{RESET}\n{merge_res.stderr}", file=sys.stderr)
                return False

        # Inspect resulting .changes file for chronological ordering
        changes_files = glob.glob(os.path.join(pkg_dir, "*.changes"))
        for cf in changes_files:
            try:
                with open(cf, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                if not is_changes_chronologically_sorted(content):
                    print(f"🕒 Adjusting out-of-order changelog timestamp in {os.path.basename(cf)}...", file=sys.stderr)
                    fixed_content = fix_changes_chronological_order(content)
                    with open(cf, "w", encoding="utf-8") as f:
                        f.write(fixed_content)
                    subprocess.run(["git", "-C", pkg_dir, "add", cf], check=True)
                else:
                    print(f"✅ {os.path.basename(cf)} is chronologically sorted. Preserving original timestamps.")
            except Exception as e:
                print(f"{YELLOW}⚠️ Could not check changelog ordering: {e}{RESET}", file=sys.stderr)

        # Spec collision check
        spec_collisions = []
        spec_files = glob.glob(os.path.join(pkg_dir, "*.spec"))
        for sf in spec_files:
            cols = check_spec_collisions(sf)
            if cols:
                spec_collisions.extend([(os.path.basename(sf), c) for c in cols])

        if spec_collisions:
            print(f"\n{YELLOW}⚠️  Detected potential spec collision(s) needing maintainer adjustment:{RESET}")
            for sf_name, col_msg in spec_collisions:
                print(f"   • [{sf_name}] {col_msg}")
            print(f"\n{CYAN}ℹ️  Changes left staged without auto-committing.{RESET}")
            print(f"   After adjusting the spec file, complete the commit with:")
            print(f"     {BOLD}git commit{RESET}")
            return True

        if auto_commit:
            commit_msg = f"Merge pull request #{pr_id} from {pr_info['author']}: {pr_info['subject']}"
            commit_res = subprocess.run(
                ["git", "-C", pkg_dir, "commit", "-m", commit_msg],
                capture_output=True,
                text=True
            )
            if commit_res.returncode == 0:
                print(f"{GREEN}🚀 Successfully merged PR #{pr_id} into HEAD (merge commit)!{RESET}")
                print(f"   Original PR commit {target_sha[:7]} preserved in history graph.")
                log_out = subprocess.check_output(
                    ["git", "-C", pkg_dir, "--no-pager", "log", "-1", "--oneline"],
                    text=True
                ).strip()
                print(f"   {BOLD}{log_out}{RESET}")
                return True
            else:
                print(f"{YELLOW}⚠️  Files are staged, but automated commit returned non-zero:{RESET}\n{commit_res.stderr}", file=sys.stderr)
                return True
        else:
            print(f"ℹ️  Changes from PR #{pr_id} staged with '--no-commit'. Review staged diff and commit when ready.")
            return True

    # 6. Squashed PR mode (when squash=True is explicitly passed)
    else:
        env = dict(os.environ)
        for c_sha in commits:
            cp_res = subprocess.run(
                ["git", "-C", pkg_dir, "cherry-pick", "--no-commit", c_sha],
                env=env,
                capture_output=True,
                text=True
            )
            if cp_res.returncode != 0:
                conflicts = subprocess.check_output(
                    ["git", "-C", pkg_dir, "diff", "--name-only", "--diff-filter=U"],
                    text=True
                ).splitlines()
                if conflicts:
                    print(f"{RED}❌ Conflict encountered during cherry-pick of {c_sha[:7]}:{RESET} {', '.join(conflicts)}", file=sys.stderr)
                    return False
                else:
                    print(f"{RED}❌ Cherry-pick failed for {c_sha[:7]}:{RESET}\n{cp_res.stderr}", file=sys.stderr)
                    return False

        # Inspect resulting .changes file
        changes_files = glob.glob(os.path.join(pkg_dir, "*.changes"))
        for cf in changes_files:
            try:
                with open(cf, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                if not is_changes_chronologically_sorted(content):
                    print(f"🕒 Adjusting out-of-order changelog timestamp in {os.path.basename(cf)}...", file=sys.stderr)
                    fixed_content = fix_changes_chronological_order(content)
                    with open(cf, "w", encoding="utf-8") as f:
                        f.write(fixed_content)
                    subprocess.run(["git", "-C", pkg_dir, "add", cf], check=True)
                else:
                    print(f"✅ {os.path.basename(cf)} is chronologically sorted. Preserving original timestamps.")
            except Exception as e:
                pass

        # Spec collision check
        spec_collisions = []
        spec_files = glob.glob(os.path.join(pkg_dir, "*.spec"))
        for sf in spec_files:
            cols = check_spec_collisions(sf)
            if cols:
                spec_collisions.extend([(os.path.basename(sf), c) for c in cols])

        if spec_collisions:
            print(f"\n{YELLOW}⚠️  Detected potential spec collision(s) needing maintainer adjustment:{RESET}")
            for sf_name, col_msg in spec_collisions:
                print(f"   • [{sf_name}] {col_msg}")
            print(f"\n{CYAN}ℹ️  Changes left staged without auto-committing.{RESET}")
            return True

        if auto_commit:
            subject = pr_info["subject"]
            if f"#{pr_id}" not in subject:
                subject = f"{subject} (PR #{pr_id})"

            bullet_lines = []
            for c_sha in commits:
                c_subj = subprocess.check_output(
                    ["git", "-C", pkg_dir, "log", "-1", "--pretty=format:%s", c_sha],
                    text=True
                ).strip()
                bullet_lines.append(f"- {c_subj}")

            commit_msg = f"{subject}\n\nSquashed commits from PR #{pr_id}:\n" + "\n".join(bullet_lines)
            commit_res = subprocess.run(
                ["git", "-C", pkg_dir, "commit", f"--author={pr_info['author']}", "-m", commit_msg],
                capture_output=True,
                text=True
            )
            if commit_res.returncode == 0:
                print(f"{GREEN}🚀 Successfully merged and squashed {commit_count} commits from PR #{pr_id} on top of HEAD!{RESET}")
                log_out = subprocess.check_output(
                    ["git", "-C", pkg_dir, "--no-pager", "log", "-1", "--oneline"],
                    text=True
                ).strip()
                print(f"   {BOLD}{log_out}{RESET}")
                return True
            else:
                print(f"{YELLOW}⚠️  Files are staged, but automated commit returned non-zero:{RESET}\n{commit_res.stderr}", file=sys.stderr)
                return True
        else:
            print(f"ℹ️  Changes squashed and staged with '--no-commit'. Review staged diff and commit when ready.")
            return True


try:
    import sync_backend as sb
except ImportError:
    from . import sync_backend as sb

def get_obsprj_forwarded_prs(workspace_path: str = ".", owner: str = "GNOME"):
    """
    Queries open pull requests on the superproject ({owner}/_ObsPrj) and maps
    referenced package PRs to their forwarded superproject PR status.
    """
    token = sb.get_gitea_token()
    headers = {'User-Agent': 'curl/8.0.1'}
    if token:
        headers['Authorization'] = f'token {token}'

    url = f"https://src.opensuse.org/api/v1/repos/{owner}/_ObsPrj/pulls?state=open"
    forwarded_map = {}
    obsprj_prs = []
    try:
        res = sb.get_http_session().get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            obsprj_prs = res.json()
    except Exception:
        pass

    re_pkg_pr = re.compile(r'PR:\s*([^/]+)/([^!]+)!(\d+)')
    for pr in obsprj_prs:
        num = pr.get("number")
        title = pr.get("title")
        target_branch = pr.get("base", {}).get("ref")
        url = pr.get("html_url")
        body = pr.get("body", "")

        referenced = re_pkg_pr.findall(body)
        is_grouped = len(referenced) > 1
        group_size = len(referenced)

        for p_owner, pkg, pkg_pr_num in referenced:
            key = (pkg.lower(), int(pkg_pr_num))
            forwarded_map[key] = {
                "obsprj_pr": num,
                "obsprj_title": title,
                "obsprj_target": target_branch,
                "obsprj_url": url,
                "is_grouped": is_grouped,
                "group_size": group_size,
                "group_pkgs": [p[1] for p in referenced] if is_grouped else []
            }
    return forwarded_map, obsprj_prs

def list_pull_requests(target_pkg_dir: Optional[str] = None, workspace_path: str = ".") -> bool:
    """
    Lists open pull requests with target branches and openSUSE superproject (_ObsPrj)
    forwarded PR status (single vs grouped).
    In single package mode, prints detailed PR information with quick merge command.
    In workspace mode, prints an overview table of forwarded PRs on _ObsPrj.
    """
    token = sb.get_gitea_token()
    headers = {'User-Agent': 'curl/8.0.1'}
    if token:
        headers['Authorization'] = f'token {token}'

    if target_pkg_dir and os.path.isdir(target_pkg_dir):
        pkg_dir = os.path.abspath(target_pkg_dir)
        pkg_name = os.path.basename(pkg_dir)
        owner, gitea_name = sb.get_gitea_owner_and_repo(pkg_dir, pkg_name)

        super_owner, _ = sb.get_gitea_owner_and_repo(workspace_path, "_ObsPrj")
        if not super_owner:
            super_owner = owner

        print(f"🔄 Querying open pull requests for {CYAN}{pkg_name}{RESET} on {owner}/{gitea_name}...")
        url = f"https://src.opensuse.org/api/v1/repos/{owner}/{gitea_name}/pulls?state=open"
        prs = []
        try:
            res = sb.get_http_session().get(url, headers=headers, timeout=10)
            if res.status_code == 200:
                prs = res.json()
        except Exception as e:
            print(f"{RED}[Error]{RESET} Failed to query pull requests from Gitea: {e}", file=sys.stderr)
            return False

        forwarded_map, _ = get_obsprj_forwarded_prs(workspace_path, super_owner)

        if not prs:
            print(f"\n{CYAN}ℹ️  No open pull requests found for {pkg_name} on {owner}/{gitea_name}.{RESET}\n")
            return True

        print(f"\n{BOLD}{'='*80}{RESET}")
        print(f"📋 {BOLD}OPEN PULL REQUESTS: {CYAN}{pkg_name}{RESET} ({owner}/{gitea_name})")
        print(f"{BOLD}{'='*80}{RESET}")

        for pr in prs:
            num = pr.get("number")
            title = pr.get("title")
            author = pr.get("user", {}).get("username") or pr.get("user", {}).get("login")
            base = pr.get("base", {}).get("ref")
            head_repo = pr.get("head", {}).get("repo", {}).get("full_name") or ""
            head_ref = pr.get("head", {}).get("ref") or ""
            source_str = f"{head_repo}:{head_ref}" if head_repo else head_ref
            created = pr.get("created_at", "")[:19].replace("T", " ")
            url = pr.get("html_url")

            fwd = forwarded_map.get((pkg_name.lower(), num))
            if fwd:
                if fwd["is_grouped"]:
                    fwd_str = f"{YELLOW}{super_owner}/_ObsPrj#{fwd['obsprj_pr']} ➔ {fwd['obsprj_target']} [Group of {fwd['group_size']} packages]{RESET}"
                else:
                    fwd_str = f"{GREEN}{super_owner}/_ObsPrj#{fwd['obsprj_pr']} ➔ {fwd['obsprj_target']} [Single Package Forwarded]{RESET}"
            else:
                fwd_str = f"{CYAN}None (Direct Branch or Contributor PR){RESET}"

            print(f"  {BOLD}PR #{num}:{RESET} {title}")
            print(f"    • Author:       {author}")
            print(f"    • Target:       {BOLD}{base}{RESET}  (<= {source_str})")
            print(f"    • Created:      {created}")
            print(f"    • Forwarded PR: {fwd_str}")
            print(f"    • URL:          {url}")
            print(f"    • Quick Merge:  {CYAN}geckopit-cli --merge-pr {num}{RESET}")
            print()

        print(f"{BOLD}{'='*80}{RESET}\n")
        return True

    else:
        # Workspace Mode
        super_owner, _ = sb.get_gitea_owner_and_repo(workspace_path, "_ObsPrj")
        if not super_owner:
            super_owner = "GNOME"

        print(f"🔄 Querying open forwarded pull requests for superproject {CYAN}{super_owner}/_ObsPrj{RESET}...")
        forwarded_map, obs_prs = get_obsprj_forwarded_prs(workspace_path, super_owner)

        if not obs_prs:
            print(f"\n{CYAN}ℹ️  No open forwarded pull requests found on {super_owner}/_ObsPrj.{RESET}\n")
            return True

        print(f"\n{BOLD}{'='*80}{RESET}")
        print(f"📋 {BOLD}OPEN FORWARDED PULL REQUESTS: {CYAN}{super_owner}/_ObsPrj{RESET}")
        print(f"{BOLD}{'='*80}{RESET}")
        print(f"  {'PR #':<8} {'TARGET':<10} {'TYPE':<22} {'PACKAGES / TITLE'}")
        print(f"  {'-'*76}")

        re_pkg_pr = re.compile(r'PR:\s*([^/]+)/([^!]+)!(\d+)')
        total_referenced = 0

        for pr in obs_prs:
            num = f"#{pr.get('number')}"
            target = pr.get('base', {}).get('ref', '')
            body = pr.get('body', '')
            title = pr.get('title', '')
            referenced = re_pkg_pr.findall(body)
            pkgs = [p[1] for p in referenced]
            total_referenced += len(pkgs)

            if len(pkgs) > 1:
                ptype = f"{YELLOW}Group of {len(pkgs)} pkgs{RESET}"
                pkg_summary = ", ".join(pkgs[:4]) + f" (+{len(pkgs)-4} more)" if len(pkgs) > 4 else ", ".join(pkgs)
            elif len(pkgs) == 1:
                ptype = f"{GREEN}Single Package{RESET}"
                pkg_summary = pkgs[0]
            else:
                ptype = f"{CYAN}Superproject{RESET}"
                pkg_summary = title[:40]

            print(f"  {BOLD}{num:<8}{RESET} {target:<10} {ptype:<31} {pkg_summary}")

        print(f"{BOLD}{'='*80}{RESET}")
        print(f"  Total: {len(obs_prs)} forwarded PRs on _ObsPrj representing {total_referenced} package updates in staging.\n")
        return True
