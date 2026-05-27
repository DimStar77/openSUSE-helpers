#!/usr/bin/env python3
import os
import ssl
import json
import re
import sys
import urllib.request
import yaml
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from colorama import init, Fore, Style

# Initialize colorama for clean cross-platform terminal sequences
init(autoreset=True)

REPO = "GNOME/_ObsPrj"

# ---------------------------------------------------------------------
# CONFIGURATION & CREDENTIALS PARSER (Robust YAML)
# ---------------------------------------------------------------------
def get_gitea_credentials(login_name: str = "OBS"):
    """Uses pyyaml to safely extract the URL and token for the specified profile."""
    config_path = Path.home() / ".config" / "tea" / "config.yml"
    if not config_path.exists():
        print(f"{Fore.RED}Error: tea configuration not found at {config_path}")
        print("Please run 'tea login add' first.")
        sys.exit(1)

    try:
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)
        
        logins = config.get("logins", [])
        
        # Strategy 1: Look for the explicit profile name (like OBS)
        for login in logins:
            if login.get("name") == login_name:
                return login.get("url").rstrip("/"), login.get("token")
                
        # Strategy 2: Fallback to whatever profile is marked as default
        for login in logins:
            if login.get("default") is True:
                return login.get("url").rstrip("/"), login.get("token")
                
    except Exception as e:
        print(f"{Fore.RED}Error reading YAML configuration: {e}")
        sys.exit(1)
        
    print(f"{Fore.RED}Error: Could not find login profile matching '{login_name}' or a 'default' profile in tea config.")
    sys.exit(1)


# ---------------------------------------------------------------------
# NATIVE GITEA API CLIENT
# ---------------------------------------------------------------------
class GiteaClient:
    def __init__(self):
        self.base_url, self.token = get_gitea_credentials()
        
    def request(self, endpoint: str, method: str = "GET", data: dict = None):
        """Dispatches an authenticated HTTP request to the Gitea API."""
        url = f"{self.base_url}/api/v1/{endpoint.lstrip('/')}"
        headers = {
            "Authorization": f"token {self.token}",
            "Content-Type": "application/json"
        }
        
        payload = json.dumps(data).encode("utf-8") if data else None
        req = urllib.request.Request(url, headers=headers, method=method, data=payload)
        
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        try:
            with urllib.request.urlopen(req, context=ctx) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            error_msg = e.read().decode("utf-8")
            print(f"{Fore.RED}API Error ({e.code}): {error_msg}")
            sys.exit(1)


# ---------------------------------------------------------------------
# WORKFLOW UTILITIES
# ---------------------------------------------------------------------
client = GiteaClient()

def get_pr_details(pr_id: int) -> dict:
    return client.request(f"repos/{REPO}/pulls/{pr_id}")

def extract_tokens(body: str) -> list[str]:
    if not body:
        return []
    return re.findall(r"^PR: GNOME/[\w-]+!\d+", body, re.MULTILINE)

def get_branch_color(branch_name: str) -> str:
    """Returns distinct colors for release lines to ease visual sorting."""
    if branch_name == "factory":
        return Fore.GREEN
    if branch_name == "next":
        return Fore.CYAN
    return Fore.YELLOW


# ---------------------------------------------------------------------
# ACTIONS LAYERS
# ---------------------------------------------------------------------
def action_list(filter_branch: str = None):
    if filter_branch:
        print(f"=== Fetching active open PRs targeting '{get_branch_color(filter_branch)}{filter_branch}{Style.RESET_ALL}'... ===")
    else:
        print("=== Fetching all active open PRs... ===")

    summaries = client.request(f"repos/{REPO}/pulls?state=open")
    if not summaries:
        print(f"{Fore.YELLOW}No open PRs found.")
        return

    print("=== Resolving target branches and hosts in parallel... ===")
    pr_ids = [int(p["number"]) for p in summaries]
    
    with ThreadPoolExecutor(max_workers=10) as executor:
        records = list(executor.map(get_pr_details, pr_ids))

    if filter_branch:
        records = [r for r in records if r["base"]["ref"] == filter_branch]
        records.sort(key=lambda x: x["number"])
    else:
        records.sort(key=lambda x: (x["base"]["ref"], x["number"]))

    print(f"\n{Style.BRIGHT}{'INDEX':<8} {'TARGET BRANCH':<15} {'PACKAGE / TITLE'}")
    print(f"{'-'*5:<8} {'-'*13:<15} {'-'*15}")
    
    for r in records:
        branch = r["base"]["ref"]
        b_color = get_branch_color(branch)
        raw_title = r["title"].replace("Forwarded PRs: ", "")
        
        # Extract the absolute host package from the immutable head reference string
        # Example: "PR_atkmm1_6#1" -> "atkmm1_6"
        head_ref = r.get("head", {}).get("ref", "")
        if head_ref and head_ref.startswith("PR_"):
            host_package = head_ref.replace("PR_", "").split("#")[0]
            
            # Tokenize all packages listed in the title
            packages = [p.strip() for p in raw_title.split(",")]
            
            if host_package in packages:
                packages.remove(host_package)
                host_str = f"{Fore.YELLOW}{Style.BRIGHT}★ {host_package}{Style.RESET_ALL}"
                
                if packages:
                    peer_str = f" (+ {', '.join(packages)})"
                    clean_title = f"{host_str}{Style.RESET_ALL}{peer_str}"
                else:
                    clean_title = host_str
            else:
                # Fallback if the host package naming doesn't exactly align with the title string
                clean_title = f"{Fore.YELLOW}{Style.BRIGHT}★ {host_package}{Style.RESET_ALL} (+ {raw_title})"
        else:
            clean_title = raw_title

        print(f"{Fore.WHITE}{r['number']:<8} {b_color}{branch:<15} {Style.RESET_ALL}{clean_title}")

def action_select(target_id: int, source_packages: list[str]):
    target = get_pr_details(target_id)
    target_branch = target["base"]["ref"]
    b_color = get_branch_color(target_branch)
    
    print(f"Target PR #{target_id} points to branch: {b_color}{target_branch}")
    print("------------------------------------------------------")

    open_prs = client.request(f"repos/{REPO}/pulls?state=open")
    new_tokens = []
    ids_to_close = []

    for package in source_packages:
        print(f"Processing package: '{Fore.BLUE}{package}{Style.RESET_ALL}'...")
        match = next((p for p in open_prs if package in p["title"]), None)
        
        if not match:
            print(f"--> {Fore.RED}ERROR: Could not find an open PR for '{package}'. Skipping!")
            continue

        source = get_pr_details(int(match["number"]))
        source_branch = source["base"]["ref"]

        if target_branch != source_branch:
            print(f"--> {Fore.RED}CRITICAL BRANCH MISMATCH for '{package}'! Target: {target_branch}, Source: {source_branch}. Skipping.")
            continue

        tokens = extract_tokens(source["body"])
        if not tokens:
            print(f"--> {Fore.RED}ERROR: No valid reference token found in PR #{source['number']}. Skipping.")
            continue

        print(f"--> Staging reference line: {Fore.LIGHTBLACK_EX}{tokens[0]}")
        new_tokens.append(tokens[0])
        ids_to_close.append(source["number"])

    if not new_tokens:
        print(f"{Fore.YELLOW}No valid packages were processed.")
        return

    body_lines = (target["body"] or "").splitlines()
    last_pr_idx = max((i for i, line in enumerate(body_lines) if line.startswith("PR: GNOME/")), default=-1)

    if last_pr_idx != -1:
        for tok in reversed(new_tokens):
            body_lines.insert(last_pr_idx + 1, tok)
    else:
        body_lines.extend([""] + new_tokens)

    client.request(f"repos/{REPO}/pulls/{target_id}", method="PATCH", data={"body": "\n".join(body_lines)})
    
    for close_id in ids_to_close:
        client.request(f"repos/{REPO}/pulls/{close_id}", method="PATCH", data={"state": "closed"})
    print(f"=== {Fore.GREEN}Done! Packages selected into group #{target_id} ===")


def action_unselect(target_id: int, package: str):
    target = get_pr_details(target_id)
    all_tokens = extract_tokens(target["body"])

    if not all_tokens:
        print(f"{Fore.RED}Error: PR #{target_id} has no token trackers.")
        return

    host_package = all_tokens[0].split("/")[1].split("!")[0]
    if package == host_package:
        print(f"{Fore.RED}!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print(f"{Fore.RED}CRITICAL WARNING: Target package '{package}' is the group host.")
        print(f"{Fore.YELLOW}To tear down this stack safely, use: pr-manage disintegrate {target_id}")
        print(f"{Fore.RED}!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        sys.exit(1)

    target_token_line = next((t for t in all_tokens if f"PR: GNOME/{package}!" in t), None)
    if not target_token_line:
        print(f"{Fore.RED}Error: Package '{package}' tracking line missing in PR #{target_id}.")
        return

    sub_repo = target_token_line.split()[1].split("!")[0]
    sub_id = target_token_line.split("!")[1]

    new_body = "\n".join([line for line in target["body"].splitlines() if not line.startswith(f"PR: GNOME/{package}!")])
    client.request(f"repos/{REPO}/pulls/{target_id}", method="PATCH", data={"body": new_body})

    print(f"=== Reopening original package PR #{sub_id} in {sub_repo} ===")
    client.request(f"repos/{sub_repo}/pulls/{sub_id}", method="PATCH", data={"state": "open"})
    print(f"=== {Fore.GREEN}Done! Successfully unselected and restored '{package}' ===")


def action_combine(target_id: int, source_id: int):
    target = get_pr_details(target_id)
    source = get_pr_details(source_id)

    if target["base"]["ref"] != source["base"]["ref"]:
        print(f"{Fore.RED}CRITICAL ERROR: Branch mismatch! Target: {target['base']['ref']}, Source: {source['base']['ref']}.")
        sys.exit(1)

    source_tokens = extract_tokens(source["body"])
    if not source_tokens:
        print(f"{Fore.RED}Error: No tracking tokens found in source PR #{source_id}.")
        return

    body_lines = (target["body"] or "").splitlines()
    last_pr_idx = max((i for i, line in enumerate(body_lines) if line.startswith("PR: GNOME/")), default=-1)

    if last_pr_idx != -1:
        for tok in reversed(source_tokens):
            body_lines.insert(last_pr_idx + 1, tok)
    else:
        body_lines.extend([""] + source_tokens)

    client.request(f"repos/{REPO}/pulls/{target_id}", method="PATCH", data={"body": "\n".join(body_lines)})
    client.request(f"repos/{REPO}/pulls/{source_id}", method="PATCH", data={"state": "closed"})
    print(f"{Fore.GREEN}Done! Group PR #{source_id} combined into #{target_id}.")


def action_disintegrate(target_id: int):
    target = get_pr_details(target_id)
    all_tokens = extract_tokens(target["body"])

    if len(all_tokens) <= 1:
        print(f"{Fore.YELLOW}This PR doesn't track any nested peer groups. Already standalone.")
        return

    peer_tokens = all_tokens[1:]
    print("Reopening all sub-package PRs in parallel...")

    def reopen_pr(tok_line: str):
        sub_repo = tok_line.split()[1].split("!")[0]
        sub_id = tok_line.split("!")[1]
        print(f"--> Reopening #{sub_id} in {sub_repo}...")
        client.request(f"repos/{sub_repo}/pulls/{sub_id}", method="PATCH", data={"state": "open"})

    with ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(reopen_pr, peer_tokens))

    cleaned_lines = [line for line in target["body"].splitlines() if not any(p in line for p in peer_tokens)]
    client.request(f"repos/{REPO}/pulls/{target_id}", method="PATCH", data={"body": "\n".join(cleaned_lines)})
    print(f"=== {Fore.GREEN}Disintegration Complete! Reset #{target_id} back to single track. ===")


def action_accept(target_id: int):
    print(f"=== Signaling Staging Approval for Group PR #{target_id} ===")
    client.request(f"repos/{REPO}/issues/{target_id}/comments", method="POST", data={"body": "merge ok"})
    print(f"{Fore.GREEN}Successfully commented 'merge ok' on PR #{target_id}.")
    print("The openSUSE staging workflow bots have been notified to process the merge.")


# ---------------------------------------------------------------------
# MAIN EXECUTOR CLI FRONTEND
# ---------------------------------------------------------------------
def main():
    if len(sys.argv) < 2:
        print(f"{Style.BRIGHT}Usage:")
        print("  pr-manage list [factory|next]")
        print("  pr-manage select <target-pr-id> <package1> [...]")
        print("  pr-manage unselect <target-pr-id> <package-name>")
        print("  pr-manage combine <target-pr-id> <source-pr-id>")
        print("  pr-manage disintegrate <target-pr-id>")
        print("  pr-manage accept <target-pr-id>")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "list":
        branch = sys.argv[2] if len(sys.argv) > 2 else None
        action_list(branch)
    else:
        if len(sys.argv) < 3:
            print(f"{Fore.RED}Error: Command '{cmd}' requires a target PR ID.")
            sys.exit(1)
        try:
            target_id = int(sys.argv[2])
        except ValueError:
            print(f"{Fore.RED}Error: Target PR ID must be an integer.")
            sys.exit(1)

        if cmd == "select":
            action_select(target_id, sys.argv[3:])
        elif cmd == "unselect":
            if len(sys.argv) < 4:
                print(f"{Fore.RED}Error: Missing package argument.")
                sys.exit(1)
            action_unselect(target_id, sys.argv[3])
        elif cmd == "combine":
            if len(sys.argv) < 4:
                print(f"{Fore.RED}Error: Missing source PR ID.")
                sys.exit(1)
            action_combine(target_id, int(sys.argv[3]))
        elif cmd == "disintegrate":
            action_disintegrate(target_id)
        elif cmd == "accept":
            action_accept(target_id)
        else:
            print(f"{Fore.RED}Unknown command: {cmd}")
            sys.exit(1)

if __name__ == "__main__":
    main()
