#!/usr/bin/env python3
"""
osc_clean_devel.py

A command-line tool to manage and correct package metadata in openSUSE Factory and other projects.
Specifically, it identifies and removes incorrect '<devel .../>' tags from package metadata.

Usage:
  ./osc_clean_devel.py list
  ./osc_clean_devel.py drop <package>
  ./osc_clean_devel.py drop-all
"""

import argparse
import concurrent.futures
import difflib
import sys
import xml.etree.ElementTree as ET
from typing import List, Optional, Tuple

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


def search_packages_with_devel(apiurl: str, project: str, devel_project: str, all_devel: bool = False) -> List[str]:
    """
    Queries OBS using XPath search to find all packages in a project
    that have a <devel project="..."/> element.
    If all_devel is True, matches any devel tag. Otherwise, filters by devel_project.
    """
    if all_devel:
        xpath_query = f"@project='{project}' and devel/@project = devel/@project"
    else:
        xpath_query = f"@project='{project}' and devel/@project='{devel_project}'"
        
    url = osc.core.makeurl(apiurl, ['search', 'package', 'id'], {'match': xpath_query})
    
    try:
        response_bytes = osc.core.http_GET(url).read()
        root = ET.fromstring(response_bytes)
        package_names = [pkg.get('name') for pkg in root.findall('package') if pkg.get('name')]
        return sorted(package_names)
    except HTTPError as e:
        print(f"Error querying OBS search API: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error parsing search results: {e}", file=sys.stderr)
        sys.exit(1)


def remove_child_preserving_format(parent: ET.Element, child: ET.Element) -> None:
    """
    Removes a child element from a parent element, propagating the child's
    tail whitespace to maintain clean XML indentation and newlines.
    """
    children = list(parent)
    try:
        idx = children.index(child)
    except ValueError:
        return

    if idx > 0:
        prev = children[idx - 1]
        if child.tail:
            prev.tail = child.tail
    else:
        if child.tail:
            parent.text = child.tail
            
    parent.remove(child)


def modify_package_meta(xml_str: str, devel_project: str, all_devel: bool = False) -> Tuple[str, bool, int]:
    """
    Parses package metadata, removes `<devel>` elements, and serializes
    the modified XML back to a string while preserving format.
    
    Returns:
      (modified_xml_str, was_modified, removed_count)
    """
    try:
        root = ET.fromstring(xml_str)
    except Exception as e:
        print(f"Error parsing package metadata XML: {e}", file=sys.stderr)
        return xml_str, False, 0

    removed_count = 0
    # Create a list copy to safely iterate and remove during loop
    for child in list(root):
        if child.tag == 'devel':
            if all_devel or (child.get('project') == devel_project):
                remove_child_preserving_format(root, child)
                removed_count += 1

    if removed_count > 0:
        modified_xml = ET.tostring(root, encoding='utf-8').decode('utf-8')
        return modified_xml, True, removed_count

    return xml_str, False, 0


def get_colored_diff(old_str: str, new_str: str, filename: str) -> str:
    """
    Generates a unified diff between old and new strings, colorized with ANSI codes
    if stdout is a TTY.
    """
    old_lines = old_str.splitlines(keepends=True)
    new_lines = new_str.splitlines(keepends=True)
    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{filename}", tofile=f"b/{filename}"
    ))

    if not diff_lines:
        return ""

    if not sys.stdout.isatty():
        return "".join(diff_lines)

    color_diff = []
    for line in diff_lines:
        if line.endswith('\n'):
            stripped_line = line[:-1]
            suffix = '\n'
        else:
            stripped_line = line
            suffix = ''

        if line.startswith('+') and not line.startswith('+++'):
            color_diff.append(f"\033[32m{stripped_line}\033[0m{suffix}")  # Green for additions
        elif line.startswith('-') and not line.startswith('---'):
            color_diff.append(f"\033[31m{stripped_line}\033[0m{suffix}")  # Red for deletions
        elif line.startswith('@@'):
            color_diff.append(f"\033[36m{stripped_line}\033[0m{suffix}")  # Cyan for location/headers
        else:
            color_diff.append(line)
    return "".join(color_diff)


def confirm_prompt(prompt: str) -> bool:
    """
    Asks the user for a Yes/No confirmation.
    """
    try:
        response = input(f"{prompt} [y/N]: ").strip().lower()
        return response in ('y', 'yes')
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        sys.exit(0)


def process_package(
    apiurl: str,
    project: str,
    package: str,
    devel_project: str,
    all_devel: bool,
    dry_run: bool,
    force_yes: bool,
    interactive: bool = True
) -> bool:
    """
    Retrieves, parses, modifies (removes devel project tag), and saves the package metadata.
    Returns True if successfully modified, False otherwise.
    """
    print(f"\nProcessing package: {project}/{package}...")
    try:
        # Fetch current package metadata
        lines_bytes = osc.core.show_package_meta(apiurl, project, package)
        old_xml = b"".join(lines_bytes).decode('utf-8')
    except HTTPError as e:
        if e.code == 404:
            print(f"Error: Package '{package}' not found in project '{project}'.", file=sys.stderr)
        else:
            print(f"Error fetching metadata for '{package}': {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"Unexpected error fetching metadata for '{package}': {e}", file=sys.stderr)
        return False

    new_xml, modified, removed_count = modify_package_meta(old_xml, devel_project, all_devel)

    if not modified:
        if all_devel:
            print(f"No <devel/> tag found in '{package}' metadata.")
        else:
            print(f"No <devel project=\"{devel_project}\".../> tag found in '{package}' metadata.")
        return False

    # Show what changes will be applied
    diff_text = get_colored_diff(old_xml, new_xml, f"{package}.xml")
    if diff_text:
        print("Proposed changes:")
        print(diff_text)
    else:
        print("Metadata was modified but no text diff was generated (whitespace changes only).")

    if dry_run:
        print(f"[Dry-run] Would have removed {removed_count} devel tag(s) from package '{package}'.")
        return True

    # Ask for confirmation if in interactive mode and force_yes is not set
    if interactive and not force_yes:
        if not confirm_prompt(f"Apply these changes to package '{package}'?"):
            print("Skipping.")
            return False

    print(f"Saving metadata for package '{package}' back to OBS...")
    try:
        osc.core.edit_meta(
            metatype='pkg',
            path_args=(project, package),
            data=[new_xml],
            change_is_required=False,
            apiurl=apiurl
        )
        print(f"Successfully removed {removed_count} devel tag(s) from '{package}'.")
        return True
    except HTTPError as e:
        print(f"Error saving metadata to OBS: {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"Unexpected error saving metadata: {e}", file=sys.stderr)
        return False


def cmd_list(args: argparse.Namespace, apiurl: str) -> None:
    """
    Handles 'list' subcommand.
    """
    if args.all_devel:
        print(f"Searching for packages in '{args.project}' with ANY devel project tag...")
    else:
        print(f"Searching for packages in '{args.project}' with devel project '{args.devel_project}'...")
        
    packages = search_packages_with_devel(apiurl, args.project, args.devel_project, args.all_devel)
    
    if not packages:
        if args.all_devel:
            print(f"No packages found in '{args.project}' with any devel project tag.")
        else:
            print(f"No packages found in '{args.project}' with devel project '{args.devel_project}'.")
        return

    print(f"\nFound {len(packages)} package(s):")
    if args.all_devel:
        print("Retrieving associated devel projects...", end="", flush=True)
        
        def fetch_devel(pkg):
            try:
                lines_bytes = osc.core.show_package_meta(apiurl, args.project, pkg)
                xml_str = b"".join(lines_bytes).decode('utf-8')
                root = ET.fromstring(xml_str)
                projects = [child.get('project') for child in root if child.tag == 'devel' and child.get('project')]
                return pkg, projects
            except Exception:
                return pkg, []
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=30) as executor:
            # Maintain sorted order
            results = list(executor.map(fetch_devel, packages))
            
        print("\r" + " " * 42 + "\r", end="", flush=True)  # Clear the loading line
        
        for pkg, projects in results:
            if projects:
                proj_str = ", ".join(projects)
                print(f"  - {pkg} [devel project: {proj_str}]")
            else:
                print(f"  - {pkg}")
    else:
        for pkg in packages:
            print(f"  - {pkg}")


def cmd_drop(args: argparse.Namespace, apiurl: str) -> None:
    """
    Handles 'drop' subcommand.
    """
    process_package(
        apiurl=apiurl,
        project=args.project,
        package=args.package,
        devel_project=args.devel_project,
        all_devel=args.all_devel,
        dry_run=args.dry_run,
        force_yes=args.yes,
        interactive=True
    )


def cmd_drop_all(args: argparse.Namespace, apiurl: str) -> None:
    """
    Handles 'drop-all' subcommand.
    """
    if args.all_devel:
        print(f"Scanning '{args.project}' for packages with ANY devel project tag...")
    else:
        print(f"Scanning '{args.project}' for packages with devel project '{args.devel_project}'...")
        
    packages = search_packages_with_devel(apiurl, args.project, args.devel_project, args.all_devel)

    if not packages:
        print("No packages found to clean up.")
        return

    if args.all_devel:
        print(f"\nIdentified {len(packages)} package(s) with ANY devel project tag:")
    else:
        print(f"\nIdentified {len(packages)} package(s) with devel project '{args.devel_project}':")
        
    for pkg in packages:
        print(f"  - {pkg}")

    if args.dry_run:
        print("\n--- Dry-Run Mode Active ---")
        for pkg in packages:
            process_package(
                apiurl=apiurl,
                project=args.project,
                package=pkg,
                devel_project=args.devel_project,
                all_devel=args.all_devel,
                dry_run=True,
                force_yes=True,
                interactive=False
            )
        print("\n[Dry-run] Completed scan. No changes were saved.")
        return

    # Prompt before processing all
    if not args.yes:
        print(f"\nThis action will modify the metadata of {len(packages)} package(s) on the OBS server.")
        if not confirm_prompt("Do you want to proceed and process these packages?"):
            print("Aborted.")
            sys.exit(0)

    # Let the user choose whether to confirm each package individually or process all automatically
    confirm_each = False
    if not args.yes and len(packages) > 1:
        confirm_each = confirm_prompt("Do you want to confirm the diff for each package individually?")

    success_count = 0
    for pkg in packages:
        success = process_package(
            apiurl=apiurl,
            project=args.project,
            package=pkg,
            devel_project=args.devel_project,
            all_devel=args.all_devel,
            dry_run=False,
            force_yes=args.yes or (not confirm_each),
            interactive=confirm_each
        )
        if success:
            success_count += 1

    print(f"\nFinished processing. Successfully cleaned up {success_count} / {len(packages)} package(s).")


def main() -> None:
    description = """
================================================================================
  OBS <devel> Tag Cleanup Tool (osc_clean_devel)
================================================================================
A command-line tool to manage, correct, and safely clean up incorrect '<devel .../>' 
metadata elements from package metadata in openSUSE Factory and other OBS projects.

Features:
  * Automatic config loading via osc python bindings (keyrings, ~/.oscrc).
  * High-performance XPath-based scanning on the OBS server.
  * Preserves XML layout, comments, indents, and trailing whitespaces perfectly.
  * Interactive unified diff displays with automatic ANSI colorization on TTYs.
  * Complete safety dry-run capability for single-package and batch operations.
"""

    epilog = """
Usage Examples:

  1. Find packages in openSUSE:Factory pointing to incorrect 'GNOME:Apps' devel project:
     $ %(prog)s list

  2. List all packages in openSUSE:Factory:RISCV containing ANY devel project tag:
     $ %(prog)s --project openSUSE:Factory:RISCV --all-devel list

  3. Safely dry-run removing the 'GNOME:Apps' devel tag from the 'wike' package (shows unified diff):
     $ %(prog)s --dry-run drop wike

  4. Perform the correction on a single package with interactive confirmation:
     $ %(prog)s drop wike

  5. Interactively clean up 'GNOME:Apps' tags from all matching packages in openSUSE:Factory:
     $ %(prog)s drop-all

  6. Automatically remove all devel tags in openSUSE:Factory:RISCV without confirmation:
     $ %(prog)s --project openSUSE:Factory:RISCV --all-devel -y drop-all
"""

    parser = argparse.ArgumentParser(
        description=description,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    # Grouping options for a cleaner layout
    global_group = parser.add_argument_group("Global Configuration Options")
    global_group.add_argument(
        '--project',
        default='openSUSE:Factory',
        help="The target OBS project to scan/modify (default: 'openSUSE:Factory')"
    )
    global_group.add_argument(
        '--devel-project',
        default='GNOME:Apps',
        help="The name of the incorrect devel project attribute to match and remove (default: 'GNOME:Apps')"
    )
    global_group.add_argument(
        '--all-devel',
        action='store_true',
        help="Wildcard mode: match and remove any <devel> tag regardless of its target project"
    )
    global_group.add_argument(
        '--dry-run',
        action='store_true',
        help="Perform a trial run; displays unified diffs of proposed changes without saving back to OBS"
    )
    global_group.add_argument(
        '-y', '--yes',
        action='store_true',
        help="Bypass all interactive confirmation prompts and apply modifications immediately"
    )

    subparsers = parser.add_subparsers(
        dest='command',
        required=True,
        metavar="command",
        help="The cleanup action to perform"
    )

    # 'list' command
    subparsers.add_parser(
        'list',
        help="List all packages in the target project that have matching devel project tags"
    )

    # 'drop' command
    drop_parser = subparsers.add_parser(
        'drop',
        help="Remove matching devel project tags from a single package metadata"
    )
    drop_parser.add_argument(
        'package',
        help="The name of the package to clean up"
    )

    # 'drop-all' command
    subparsers.add_parser(
        'drop-all',
        help="Remove matching devel project tags from all identified packages in the project"
    )

    args = parser.parse_args()

    # Initialize osc configurations
    apiurl = setup_osc()

    # Execute the requested command
    if args.command == 'list':
        cmd_list(args, apiurl)
    elif args.command == 'drop':
        cmd_drop(args, apiurl)
    elif args.command == 'drop-all':
        cmd_drop_all(args, apiurl)


if __name__ == '__main__':
    try:
        main()
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except OSError:
            pass
        sys.exit(0)
