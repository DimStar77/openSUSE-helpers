#!/usr/bin/env python3
"""
osc_review_helper.py

A command-line utility to assist in reviewing openSUSE submit requests against
openSUSE packaging guidelines. It queries pending reviews for a configurable
reviewer group, inspects request diffs, executes automated compliance checks,
and posts review comments.

Usage:
  ./osc_review_helper.py list
  ./osc_review_helper.py show <request_id>
  ./osc_review_helper.py verify <request_id>
  ./osc_review_helper.py comment <request_id> <message>
"""

import argparse
import difflib
import re
import sys
from typing import Dict, List, Optional, Tuple, Any

import osc.core
from osc.core import HTTPError


def setup_osc() -> str:
    """
    Initializes osc configuration and returns the active API URL.
    """
    try:
        osc.core.conf.get_config()
        apiurl = osc.core.conf.config.get('apiurl')
        if not apiurl:
            print("Error: Could not retrieve 'apiurl' from osc configuration.", file=sys.stderr)
            sys.exit(1)
        return apiurl
    except Exception as e:
        print(f"Error initializing osc configuration: {e}", file=sys.stderr)
        print("Please ensure your osc is configured properly (e.g. ~/.config/osc/oscrc exists).", file=sys.stderr)
        sys.exit(1)


def get_pending_reviews(apiurl: str, group: str, target_project: str) -> List[Any]:
    """
    Retrieves pending reviews for a given reviewer group and filters them
    by target project.
    """
    try:
        # get_review_list returns open requests needing review by the group
        requests = osc.core.get_review_list(apiurl, bygroup=group)
        filtered = []
        for req in requests:
            # A request is relevant if any of its submit actions target the specified project
            for action in req.actions:
                if action.type == 'submit' and action.tgt_project == target_project:
                    filtered.append(req)
                    break
        return filtered
    except HTTPError as e:
        print(f"Error fetching reviews from OBS: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error fetching reviews: {e}", file=sys.stderr)
        sys.exit(1)


def parse_request_diff(diff_str: str) -> Dict[str, Dict[str, List[str]]]:
    """
    Parses a unified diff string and groups lines by target file.
    Returns:
      Dict mapping filename to a dict with:
        'added': List of raw added lines (without leading '+')
        'raw': List of all lines in that file's diff section
    """
    files_diff: Dict[str, Dict[str, List[str]]] = {}
    current_file = None
    
    for line in diff_str.splitlines():
        if line.startswith('+++ '):
            # Format is typically "+++ b/filename" or "+++ filename"
            parts = line.split()
            if len(parts) >= 2:
                filename = parts[1]
                if filename.startswith('b/'):
                    filename = filename[2:]
                current_file = filename
                files_diff[current_file] = {'added': [], 'raw': []}
        
        if current_file:
            files_diff[current_file]['raw'].append(line)
            if line.startswith('+') and not line.startswith('+++'):
                files_diff[current_file]['added'].append(line[1:])
                
    return files_diff


def analyze_spec_diff(filename: str, added_lines: List[str]) -> List[str]:
    """
    Surgically checks added lines in a spec file against key openSUSE guidelines.
    Returns a list of clear warning messages with references.
    """
    warnings = []
    has_clean = False
    has_buildroot = False
    has_defattr = False
    has_hardcoded_bin = False
    has_hardcoded_etc = False
    has_hardcoded_usr = False
    has_license_tag = False
    license_val = ""
    
    for line in added_lines:
        stripped = line.strip()
        
        # 1. Obsolete sections
        if stripped.startswith('%clean'):
            has_clean = True
        if stripped.startswith('BuildRoot:'):
            has_buildroot = True
            
        # 2. Obsolete macros in %files
        if stripped.startswith('%defattr'):
            has_defattr = True
            
        # 3. Hardcoded system paths
        if '/usr/bin/' in stripped or '/usr/bin ' in stripped:
            has_hardcoded_bin = True
        if '/etc/' in stripped or '/etc ' in stripped:
            has_hardcoded_etc = True
        if '/usr/' in stripped and not any(macro in stripped for macro in ['%{_usr}', '%{_prefix}', '%{_bindir}', '%{_datadir}', '%{_libdir}', '%{_includedir}', '%{_docdir}']):
            # Simple heuristic: warn if hardcoded /usr/share or similar is used directly
            if any(path in stripped for path in ['/usr/share/', '/usr/include/', '/usr/sbin/']):
                has_hardcoded_usr = True
                
        # 4. License tag checking
        if stripped.startswith('License:'):
            has_license_tag = True
            parts = stripped.split(':', 1)
            if len(parts) > 1:
                license_val = parts[1].strip()

    if has_clean:
        warnings.append(
            "The '%clean' section and 'rm -rf %{buildroot}' are obsolete in modern openSUSE spec files and should be removed "
            "(ref: openSUSE Spec File Guidelines)."
        )
    if has_buildroot:
        warnings.append(
            "The 'BuildRoot:' tag is obsolete in modern openSUSE spec files and should be removed "
            "(ref: openSUSE Spec File Guidelines)."
        )
    if has_defattr:
        warnings.append(
            "The '%defattr' macro is obsolete in modern %files sections and should be removed "
            "(ref: openSUSE Spec File Guidelines)."
        )
    if has_hardcoded_bin:
        warnings.append(
            "Found hardcoded '/usr/bin' path. Use the '%{_bindir}' macro instead to ensure portability "
            "(ref: openSUSE Packaging Guidelines)."
        )
    if has_hardcoded_etc:
        warnings.append(
            "Found hardcoded '/etc' path. Use the '%{_sysconfdir}' macro instead to ensure portability "
            "(ref: openSUSE Packaging Guidelines)."
        )
    if has_hardcoded_usr:
        warnings.append(
            "Found hardcoded system path under /usr (e.g. /usr/share, /usr/include). Use appropriate macros like "
            "'%{_datadir}', '%{_includedir}', or '%{_sbindir}' instead (ref: openSUSE Packaging Guidelines)."
        )
        
    if has_license_tag:
        # Look for legacy, non-SPDX or deprecated license format strings
        deprecated_lics = ['GPLv2', 'GPLv3', 'LGPLv2', 'LGPLv3', 'BSD-3-Clause-like', 'MIT license', 'GPL 2', 'GPL 3']
        if any(dep in license_val for dep in deprecated_lics):
            warnings.append(
                f"The declared License '{license_val}' might be using deprecated or non-SPDX identifiers. "
                "Ensure all licenses conform strictly to the SPDX Standard "
                "(e.g., use 'GPL-2.0-only', 'GPL-3.0-or-later', 'MIT', 'Apache-2.0') "
                "(ref: openSUSE Licensing Guidelines)."
            )
            
    return warnings


def analyze_changes_diff(filename: str, added_lines: List[str]) -> List[str]:
    """
    Verifies that added .changes lines include a standard formatted header.
    """
    warnings = []
    has_header = False
    
    # Match standard openSUSE changes header format:
    # Wed Jul  8 17:18:31 UTC 2026 - Jehu Marcos Herrera Puentes <jehuherrerap@hotmail.com>
    import re
    header_pattern = re.compile(
        r'^[*-]?\s*[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d+\s+\d{2}:\d{2}:\d{2}\s+UTC\s+\d{4}\s+-\s+.*<.*@.*>'
    )
    
    for line in added_lines:
        if header_pattern.match(line.strip()):
            has_header = True
            break
            
    if not has_header and added_lines:
        warnings.append(
            "The added '.changes' entries do not appear to contain a standard openSUSE changes header line "
            "(e.g., 'Wed Jul 23 12:00:00 UTC 2026 - Name <email>'). Every change must be properly attributed "
            "(ref: openSUSE Changes Files Guidelines)."
        )
        
    return warnings


def run_automated_checks(diff_str: str) -> Dict[str, List[str]]:
    """
    Runs compliance checks on the full unified diff of a request.
    Returns:
      Dict mapping filename to a list of warnings. A special key 'global' holds
      overall request-level warnings.
    """
    files_diff = parse_request_diff(diff_str)
    all_warnings: Dict[str, List[str]] = {}
    
    # 1. Global check: Does the diff include a .changes file?
    changes_modified = any(fn.endswith('.changes') for fn in files_diff)
    if not changes_modified:
        all_warnings['global'] = [
            "No '.changes' file was modified in this request. Every submit request against openSUSE:Factory "
            "must include a corresponding '.changes' entry to document the change "
            "(ref: openSUSE Packaging Guidelines)."
        ]
        
    # 2. Extract added/dropped patches from spec files and collect changes text
    added_patches = []
    dropped_patches = []
    changes_added_text = ""
    
    for filename, diff_data in files_diff.items():
        if filename.endswith('.spec'):
            for line in diff_data['added']:
                match = re.search(r'^\s*Patch\d*:\s*(\S+\.patch)', line, re.IGNORECASE)
                if match:
                    added_patches.append(match.group(1))
            for line in diff_data['raw']:
                if line.startswith('-') and not line.startswith('---'):
                    match = re.search(r'^\s*Patch\d*:\s*(\S+\.patch)', line[1:], re.IGNORECASE)
                    if match:
                        dropped_patches.append(match.group(1))
        elif filename.endswith('.changes'):
            changes_added_text += "\n".join(diff_data['added'])

    # Check if added patches are mentioned in the changes file
    for patch_name in added_patches:
        if patch_name not in changes_added_text:
            if 'global' not in all_warnings:
                all_warnings['global'] = []
            all_warnings['global'].append(
                f"Patch '{patch_name}' was added to the spec file but is not mentioned in the '.changes' file. "
                f"The patch lifecycle guidelines require all patch additions, modifications, and removals "
                f"to be explicitly documented in the changes file, including the patch file name "
                f"(ref: openSUSE:Packaging Patches guidelines - Patch life cycle)."
            )

    # Check if dropped patches are mentioned in the changes file
    for patch_name in dropped_patches:
        if patch_name in added_patches:
            continue  # ignore renames/rebases in the same submit request
        if patch_name not in changes_added_text:
            if 'global' not in all_warnings:
                all_warnings['global'] = []
            all_warnings['global'].append(
                f"Patch '{patch_name}' was removed from the spec file but is not mentioned in the '.changes' file. "
                f"The patch lifecycle guidelines require all patch additions, modifications, and removals "
                f"to be explicitly documented in the changes file, including the patch file name "
                f"(ref: openSUSE:Packaging Patches guidelines - Patch life cycle)."
            )

    # 3. File-by-file checks
    for filename, diff_data in files_diff.items():
        file_warnings = []
        if filename.endswith('.spec'):
            file_warnings.extend(analyze_spec_diff(filename, diff_data['added']))
        elif filename.endswith('.changes'):
            file_warnings.extend(analyze_changes_diff(filename, diff_data['added']))
            
        if file_warnings:
            all_warnings[filename] = file_warnings
            
    return all_warnings


def has_gemini_comment(apiurl: str, reqid: str) -> bool:
    """
    Checks if a request already has a comment containing the keyword 'gemini'.
    This prevents duplicate or redundant commenting on runs.
    """
    try:
        root = osc.core.get_comments(apiurl, 'request', reqid)
        for comment in root:
            text = comment.text or ""
            if "gemini" in text.lower():
                return True
        return False
    except Exception:
        return False


def get_colored_diff(diff_str: str) -> str:
    """
    Surgically colors a unified diff string with ANSI escape codes if output is a TTY.
    """
    if not sys.stdout.isatty():
        return diff_str

    color_lines = []
    for line in diff_str.splitlines():
        if line.startswith('+') and not line.startswith('+++'):
            color_lines.append(f"\033[32m{line}\033[0m")  # Green
        elif line.startswith('-') and not line.startswith('---'):
            color_lines.append(f"\033[31m{line}\033[0m")  # Red
        elif line.startswith('@@'):
            color_lines.append(f"\033[36m{line}\033[0m")  # Cyan
        elif line.startswith('Index:') or line.startswith('===') or line.startswith('---') or line.startswith('+++'):
            color_lines.append(f"\033[1m{line}\033[0m")    # Bold
        else:
            color_lines.append(line)
    return "\n".join(color_lines)


def cmd_list(args: argparse.Namespace, apiurl: str) -> None:
    """
    Lists pending reviews matching group and target project.
    """
    print(f"Scanning for pending reviews against group '{args.group}' for project '{args.project}'...")
    requests = get_pending_reviews(apiurl, args.group, args.project)
    
    if not requests:
        print(f"No pending reviews found for group '{args.group}' targeting '{args.project}'.")
        return

    print(f"\nFound {len(requests)} pending review request(s):")
    print(f"{'ID':<10} | {'Package':<30} | {'Creator':<15} | {'Created At'}")
    print("-" * 75)
    for req in requests:
        # Find the package name from submit action
        pkg = "Unknown"
        for act in req.actions:
            if act.type == 'submit' and act.tgt_project == args.project:
                pkg = act.tgt_package
                break
        
        # Get creation time from state or first history
        created_at = "Unknown"
        if req.statehistory:
            created_at = req.statehistory[0].when
        elif req.reviews:
            created_at = req.reviews[0].when
            
        print(f"{req.id:<10} | {pkg:<30} | {req.creator:<15} | {created_at}")


def cmd_show(args: argparse.Namespace, apiurl: str) -> None:
    """
    Shows request metadata, description, reviews, and the request diff.
    """
    try:
        req = osc.core.get_request(apiurl, args.request_id)
    except HTTPError as e:
        if e.code == 404:
            print(f"Error: Request ID '{args.request_id}' not found.", file=sys.stderr)
        else:
            print(f"Error fetching request details: {e}", file=sys.stderr)
        sys.exit(1)

    print("=" * 80)
    print(f"Request ID:  {req.id}")
    print(f"Creator:     {req.creator}")
    print(f"State:       {req.state}")
    print("-" * 80)
    print("Description:")
    print(req.description or "(No description)")
    print("-" * 80)
    
    # Show active reviews
    if req.reviews:
        print("Reviews:")
        for r in req.reviews:
            reviewer = f"User: {r.by_user}" if r.by_user else f"Group: {r.by_group}"
            if r.by_project:
                reviewer = f"Project: {r.by_project}"
            print(f"  - {reviewer:<45} [{r.state}]")
        print("-" * 80)

    print("Fetching diff...")
    try:
        diff_bytes = osc.core.request_diff(apiurl, args.request_id)
        diff_str = diff_bytes.decode('utf-8', errors='replace')
        print(get_colored_diff(diff_str))
    except Exception as e:
        print(f"Error fetching diff: {e}", file=sys.stderr)


def cmd_verify(args: argparse.Namespace, apiurl: str) -> None:
    """
    Runs automated guidelines compliance checks on a request and prints a report.
    """
    print(f"Running automated compliance checks for Request {args.request_id}...")
    try:
        diff_bytes = osc.core.request_diff(apiurl, args.request_id)
        diff_str = diff_bytes.decode('utf-8', errors='replace')
    except Exception as e:
        print(f"Error fetching diff for Request {args.request_id}: {e}", file=sys.stderr)
        sys.exit(1)

    warnings = run_automated_checks(diff_str)
    
    if not warnings:
        print("\n\033[32m[PASS]\033[0m No obvious packaging guideline violations detected in the diff.")
        print("This submission is clean and suitable for acceptance.")
        return

    print(f"\n\033[31m[WARNINGS FOUND]\033[0m Request {args.request_id} has packaging guideline violations:")
    print("=" * 80)
    
    if 'global' in warnings:
        print("\nGlobal / Request-Level Warnings:")
        for w in warnings['global']:
            print(f"  * {w}")
            
    for fn, file_warns in warnings.items():
        if fn == 'global':
            continue
        print(f"\nIn File: {fn}")
        for w in file_warns:
            print(f"  * {w}")
    print("=" * 80)


def cmd_comment(args: argparse.Namespace, apiurl: str) -> None:
    """
    Posts a review comment on a request.
    """
    comment_text = args.message
    # If the user specified a file path, read from it
    try:
        with open(comment_text, 'r', encoding='utf-8') as f:
            comment_text = f.read()
    except (OSError, IOError):
        pass  # Treat as plain text comment

    # Check for existing comments to prevent double-posting
    if has_gemini_comment(apiurl, args.request_id):
        print(f"Notice: A review comment involving 'gemini' was already posted on Request {args.request_id}. Skipping.")
        return

    print(f"Posting comment to Request {args.request_id}...")
    try:
        osc.core.create_comment(apiurl, 'request', comment_text, args.request_id)
        print("Comment successfully posted!")
    except HTTPError as e:
        print(f"Error posting comment: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error posting comment: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_accept(args: argparse.Namespace, apiurl: str) -> None:
    """
    Accepts a pending review for the specified group on a request, optionally with a message.
    """
    message_text = args.message
    # If the user specified a file path, read from it
    try:
        with open(message_text, 'r', encoding='utf-8') as f:
            message_text = f.read()
    except (OSError, IOError):
        pass  # Treat as plain text

    print(f"Accepting review for Request {args.request_id} by group '{args.group}'...")
    try:
        osc.core.change_review_state(
            apiurl,
            args.request_id,
            'accepted',
            by_group=args.group,
            message=message_text
        )
        print("Review successfully accepted!")
    except HTTPError as e:
        print(f"Error accepting review: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error accepting review: {e}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="openSUSE OBS Submit Request Guideline Review Assistant"
    )
    
    # Global options
    parser.add_argument(
        '--group',
        default='opensuse-review-team',
        help="Reviewer group to query (default: 'opensuse-review-team')"
    )
    parser.add_argument(
        '--project',
        default='openSUSE:Factory',
        help="Target project of submit requests (default: 'openSUSE:Factory')"
    )

    subparsers = parser.add_subparsers(
        dest='command',
        required=True,
        metavar="command"
    )

    # 'list' command
    subparsers.add_parser(
        'list',
        help="List pending review requests for the configured group and project"
    )

    # 'show' command
    show_parser = subparsers.add_parser(
        'show',
        help="Show details, metadata, and diff of a specific request"
    )
    show_parser.add_argument(
        'request_id',
        help="The numeric OBS request ID to view"
    )

    # 'verify' command
    verify_parser = subparsers.add_parser(
        'verify',
        help="Run automated packaging guideline compliance checks on a request"
    )
    verify_parser.add_argument(
        'request_id',
        help="The numeric OBS request ID to verify"
    )

    # 'comment' command
    comment_parser = subparsers.add_parser(
        'comment',
        help="Add a comment to an OBS request"
    )
    comment_parser.add_argument(
        'request_id',
        help="The numeric OBS request ID to comment on"
    )
    comment_parser.add_argument(
        'message',
        help="The comment text or a file path containing the comment text"
    )

    # 'accept' command
    accept_parser = subparsers.add_parser(
        'accept',
        help="Accept a pending review on an OBS request"
    )
    accept_parser.add_argument(
        'request_id',
        help="The numeric OBS request ID to accept the review for"
    )
    accept_parser.add_argument(
        'message',
        nargs='?',
        default='accepted by gemini',
        help="The optional review message (default: 'accepted by gemini')"
    )

    args = parser.parse_args()

    # Setup osc and run subcommand
    apiurl = setup_osc()

    if args.command == 'list':
        cmd_list(args, apiurl)
    elif args.command == 'show':
        cmd_show(args, apiurl)
    elif args.command == 'verify':
        cmd_verify(args, apiurl)
    elif args.command == 'comment':
        cmd_comment(args, apiurl)
    elif args.command == 'accept':
        cmd_accept(args, apiurl)


if __name__ == '__main__':
    main()
