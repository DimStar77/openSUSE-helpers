#!/usr/bin/env python3
"""
openSUSE Staging Group PR Service Engine.

Provides headless, decoupled domain logic for managing forwarded PRs on _ObsPrj / Factory:
- StagingGroup & OrphanPackagePR data models
- Pre-flight Duplicate Package Collision Guards
- High-level operations: list, select (add), unselect (remove), combine, disintegrate, accept, rename, move
- Live OBS build status query engine
- Generalized Out-of-Sync Orphan detection and remediation
- Dynamic Gitea workspace discovery & permission enforcement
Designed to serve CLI, Curses TUI, GTK4 GUI, and Web API frontends identically.
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple, Set
from concurrent.futures import ThreadPoolExecutor

import pr_manage as pm


@dataclass
class StagingGroup:
    pr_id: int
    title: str
    branch: str
    head_ref: str
    host_package: Optional[str]
    tokens: List[pm.PackageToken] = field(default_factory=list)
    html_url: str = ""
    raw_pr: Dict = field(default_factory=dict)

    def __post_init__(self):
        if self.tokens:
            self.tokens.sort(key=lambda t: t.package.lower())

    @property
    def is_group(self) -> bool:
        """True if the PR bundles more than 1 package (or is an explicit group)."""
        return len(self.tokens) > 1

    @property
    def member_count(self) -> int:
        if self.tokens:
            return len(self.tokens)
        return 1 if self.host_package else 0

    @property
    def package_names(self) -> List[str]:
        if self.tokens:
            return [t.package for t in self.tokens]
        if self.host_package:
            return [self.host_package]
        return []

    def has_package(self, pkg_name: str) -> bool:
        target = pkg_name.strip().lower()
        if self.host_package and self.host_package.lower() == target:
            return True
        return any(t.key == target for t in self.tokens)

    @property
    def mergeable(self) -> Optional[bool]:
        """Returns True if Gitea confirms PR merges cleanly, False on Git merge conflict, None if unknown."""
        return self.raw_pr.get("mergeable")


@dataclass
class OperationResult:
    success: bool
    message: str
    details: Optional[Dict] = None


@dataclass
class OrphanPackagePR:
    package: str
    pr_number: int
    title: str
    author: str
    base_branch: str
    head_branch: str
    html_url: str
    forward_pr_id: Optional[int] = None
    forward_pr_state: str = "missing"  # "closed", "missing"
    forward_pr_merged: bool = False

    @property
    def display_str(self) -> str:
        fwd_str = f"Fwd: #{self.forward_pr_id} {self.forward_pr_state}" if self.forward_pr_id else "Fwd: missing"
        return f"{self.package} !{self.pr_number} ({self.author}) ➔ {self.base_branch} [{fwd_str}]"


class StagingService:
    def __init__(self, client: Optional[pm.GiteaClient] = None):
        self.client = client or pm.GiteaClient()

    @property
    def repo(self) -> str:
        return self.client.repo

    @property
    def owner(self) -> str:
        return self.client.owner or "GNOME"

    def fetch_all(self, filter_branch: Optional[str] = None) -> Tuple[List[StagingGroup], List[StagingGroup]]:
        """
        Fetches all open PRs on the metaproject and categorizes them:
        1. all_staging_prs: All open PRs (multi-package groups listed first, followed by single-package forwards)
        2. ungrouped: Standalone / single-package PRs eligible to be grouped
        """
        raw_prs = self.client.get_all_open_prs()
        if not raw_prs:
            return [], []

        pr_ids = [int(p["number"]) for p in raw_prs]
        with ThreadPoolExecutor(max_workers=10) as executor:
            details = list(executor.map(self.client.get_pr, pr_ids))

        groups = []
        ungrouped = []

        for p in details:
            branch = p.get("base", {}).get("ref", "")
            if filter_branch and filter_branch.lower() not in ("all", "") and branch != filter_branch:
                continue

            tokens = pm.extract_tokens(p.get("body", ""))
            host_pkg = pm.get_host_package_from_pr(p)

            group_obj = StagingGroup(
                pr_id=int(p["number"]),
                title=p.get("title", ""),
                branch=branch,
                head_ref=p.get("head", {}).get("ref", ""),
                host_package=host_pkg,
                tokens=tokens,
                html_url=p.get("html_url", ""),
                raw_pr=p
            )

            if group_obj.is_group:
                groups.append(group_obj)
            else:
                ungrouped.append(group_obj)

        groups.sort(key=lambda g: (g.branch, g.pr_id))
        ungrouped.sort(key=lambda g: (g.branch, g.host_package.lower() if g.host_package else ""))
        all_staging_prs = groups + ungrouped
        return all_staging_prs, ungrouped

    def add_package_to_group(self, target_pr_id: int, package_name: str) -> OperationResult:
        return self.add_packages_to_group(target_pr_id, [package_name])

    def add_packages_to_group(self, target_pr_id: int, package_names: List[str]) -> OperationResult:
        """
        Batch adds multiple packages into the target group:
        - Checks maintainer push permissions.
        - Pre-flight checks all packages against Duplicate Collision Guard.
        - Collects all source tokens.
        - Updates target PR body in a SINGLE PATCH request!
        - Closes all source PRs in parallel via ThreadPoolExecutor.
        """
        if getattr(self.client, "is_read_only", False) is True:
            return OperationResult(
                False,
                f"Permission Denied: You have Read-Only access on '{self.repo}' (maintainer push rights required)."
            )

        if not package_names:
            return OperationResult(False, "No packages specified to add.")

        try:
            target = self.client.get_pr(target_pr_id)
        except Exception as e:
            return OperationResult(False, f"Could not load target PR #{target_pr_id}: {e}")

        target_branch = target.get("base", {}).get("ref", "")
        target_tokens = pm.extract_tokens(target.get("body", ""))
        target_pkg_map = {t.key: t for t in target_tokens}

        host_pkg = pm.get_host_package_from_pr(target)
        if host_pkg and host_pkg.lower() not in target_pkg_map:
            target_pkg_map[host_pkg.lower()] = pm.PackageToken(self.owner, host_pkg, 0)

        open_prs = self.client.get_all_open_prs()
        new_tokens = []
        ids_to_close = []
        conflicts = []
        missing = []
        mismatched_branches = []

        seen_req = set()
        cleaned_pkgs = []
        for pkg in package_names:
            p_clean = pkg.strip()
            if p_clean and p_clean.lower() not in seen_req:
                seen_req.add(p_clean.lower())
                cleaned_pkgs.append(p_clean)

        for pkg in cleaned_pkgs:
            if pkg.lower() in target_pkg_map:
                conflicts.append(pkg)
                continue

            match = pm.find_open_pr_for_package(open_prs, pkg)
            if not match:
                missing.append(pkg)
                continue

            if int(match["number"]) == target_pr_id:
                continue

            source = self.client.get_pr(int(match["number"]))
            source_branch = source.get("base", {}).get("ref", "")
            if target_branch != source_branch:
                mismatched_branches.append(f"{pkg} ({source_branch} != {target_branch})")
                continue

            s_tokens = pm.extract_tokens(source.get("body", ""))
            if not s_tokens:
                missing.append(f"{pkg} (no tokens)")
                continue

            sub_conflict = False
            for st in s_tokens:
                if st.key in target_pkg_map:
                    conflicts.append(st.package)
                    sub_conflict = True
                    break

            if sub_conflict:
                continue

            for st in s_tokens:
                new_tokens.append(st)
                target_pkg_map[st.key] = st

            ids_to_close.append(int(source["number"]))

        if not new_tokens:
            err_parts = []
            if conflicts:
                err_parts.append(f"Duplicates: {', '.join(conflicts)}")
            if missing:
                err_parts.append(f"Not found: {', '.join(missing)}")
            if mismatched_branches:
                err_parts.append(f"Branch mismatch: {', '.join(mismatched_branches)}")
            msg = "; ".join(err_parts) if err_parts else "No valid packages could be added."
            return OperationResult(False, msg)

        body_lines = (target.get("body") or "").splitlines()
        last_pr_idx = max((i for i, line in enumerate(body_lines) if pm.TOKEN_REGEX.match(line)), default=-1)

        if last_pr_idx != -1:
            for tok in reversed(new_tokens):
                body_lines.insert(last_pr_idx + 1, tok.raw_line)
        else:
            body_lines.extend([""] + [t.raw_line for t in new_tokens])

        new_body = "\n".join(body_lines)
        self.client.update_pr(target_pr_id, body=new_body)

        def close_pr(pr_id: int):
            try:
                self.client.update_pr(pr_id, state="closed")
            except Exception:
                pass

        with ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(close_pr, ids_to_close))

        msg = f"Added {len(new_tokens)} package(s) into group #{target_pr_id}."
        if conflicts:
            msg += f" (Skipped duplicates: {', '.join(conflicts)})"
        return OperationResult(True, msg)

    def remove_package_from_group(self, target_pr_id: int, package_name: str) -> OperationResult:
        return self.remove_packages_from_group(target_pr_id, [package_name])

    def remove_packages_from_group(self, target_pr_id: int, package_names: List[str]) -> OperationResult:
        """
        Batch removes multiple packages from the target group:
        - Checks maintainer push permissions.
        - Checks that host package is not among the removed packages.
        - Strips all tokens from target PR body in a SINGLE PATCH request!
        - Resolves and reopens all child PRs in parallel via ThreadPoolExecutor.
        """
        if getattr(self.client, "is_read_only", False) is True:
            return OperationResult(
                False,
                f"Permission Denied: You have Read-Only access on '{self.repo}' (maintainer push rights required)."
            )

        if not package_names:
            return OperationResult(False, "No packages specified to remove.")

        try:
            target = self.client.get_pr(target_pr_id)
        except Exception as e:
            return OperationResult(False, f"Could not load target PR #{target_pr_id}: {e}")

        all_tokens = pm.extract_tokens(target.get("body", ""))
        if not all_tokens:
            return OperationResult(False, f"PR #{target_pr_id} has no tracked package tokens.")

        host_package = pm.get_host_package_from_pr(target)
        pkgs_to_remove = {p.strip().lower() for p in package_names}

        if host_package and host_package.lower() in pkgs_to_remove:
            pkgs_to_remove.remove(host_package.lower())
            host_warn = f" (Host '{host_package}' cannot be removed without disintegrating)"
        else:
            host_warn = ""

        if not pkgs_to_remove:
            return OperationResult(False, f"Cannot remove host package '{host_package}'. Use disintegrate instead.")

        tokens_to_remove = [t for t in all_tokens if t.key in pkgs_to_remove]
        if not tokens_to_remove:
            return OperationResult(False, f"None of the specified packages were found in group #{target_pr_id}.")

        reopened_count = 0
        def reopen_child(tok: pm.PackageToken):
            nonlocal reopened_count
            sub_repo = f"{tok.owner}/{tok.package}"
            child_id = pm.find_forwarded_child_id(self.client, sub_repo, str(tok.pr_number), exclude_pr_id=target_pr_id)
            if child_id:
                try:
                    self.client.update_pr(child_id, state="open")
                    reopened_count += 1
                except Exception:
                    pass

        with ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(reopen_child, tokens_to_remove))

        remove_lines = {t.raw_line for t in tokens_to_remove}
        body_lines = [
            line for line in target.get("body", "").splitlines()
            if not pm.TOKEN_REGEX.match(line) or line.strip() not in remove_lines
        ]
        new_body = "\n".join(body_lines)
        self.client.update_pr(target_pr_id, body=new_body)

        return OperationResult(
            True,
            f"Removed {len(tokens_to_remove)} package(s) from group #{target_pr_id} (reopened {reopened_count} child PRs).{host_warn}"
        )

    def move_packages_between_groups(self, source_pr_id: int, target_pr_id: int, package_names: List[str]) -> OperationResult:
        """
        Moves one or more packages directly from source group to target group:
        - Checks maintainer push permissions.
        - Ensures both groups point to the same branch.
        - Enforces Duplicate Package Collision Guard: none of the moving packages can exist in target group.
        - Protects the host package of source group from being moved.
        - Updates source PR body (removes tokens) in 1 PATCH call.
        - Updates target PR body (adds tokens) in 1 PATCH call.
        """
        if getattr(self.client, "is_read_only", False) is True:
            return OperationResult(
                False,
                f"Permission Denied: You have Read-Only access on '{self.repo}' (maintainer push rights required)."
            )

        if source_pr_id == target_pr_id:
            return OperationResult(False, "Source and destination groups are identical.")
        if not package_names:
            return OperationResult(False, "No packages specified to move.")

        try:
            source = self.client.get_pr(source_pr_id)
            target = self.client.get_pr(target_pr_id)
        except Exception as e:
            return OperationResult(False, f"Failed to load PRs: {e}")

        source_branch = source.get("base", {}).get("ref", "")
        target_branch = target.get("base", {}).get("ref", "")

        if source_branch != target_branch:
            return OperationResult(
                False,
                f"Branch mismatch: Source #{source_pr_id} points to '{source_branch}', but Target #{target_pr_id} points to '{target_branch}'."
            )

        source_tokens = pm.extract_tokens(source.get("body", ""))
        target_tokens = pm.extract_tokens(target.get("body", ""))
        target_pkg_map = {t.key: t for t in target_tokens}

        host_target = pm.get_host_package_from_pr(target)
        if host_target and host_target.lower() not in target_pkg_map:
            target_pkg_map[host_target.lower()] = pm.PackageToken(self.owner, host_target, 0)

        host_source = pm.get_host_package_from_pr(source)
        pkgs_to_move = {p.strip().lower() for p in package_names}

        if host_source and host_source.lower() in pkgs_to_move:
            return OperationResult(
                False,
                f"Cannot move host package '{host_source}' out of its own group #{source_pr_id}."
            )

        conflicts = [p for p in pkgs_to_move if p in target_pkg_map]
        if conflicts:
            conflict_names = ", ".join(conflicts)
            return OperationResult(
                False,
                f"Collision: Target group #{target_pr_id} already contains: {conflict_names}."
            )

        tokens_to_transfer = [t for t in source_tokens if t.key in pkgs_to_move]
        if not tokens_to_transfer:
            return OperationResult(False, f"None of the specified packages exist in source group #{source_pr_id}.")

        # 1. Update source body
        transfer_lines = {t.raw_line for t in tokens_to_transfer}
        source_body_lines = [
            line for line in source.get("body", "").splitlines()
            if not pm.TOKEN_REGEX.match(line) or line.strip() not in transfer_lines
        ]
        self.client.update_pr(source_pr_id, body="\n".join(source_body_lines))

        # 2. Update target body
        target_body_lines = (target.get("body") or "").splitlines()
        last_pr_idx = max((i for i, line in enumerate(target_body_lines) if pm.TOKEN_REGEX.match(line)), default=-1)

        if last_pr_idx != -1:
            for tok in reversed(tokens_to_transfer):
                target_body_lines.insert(last_pr_idx + 1, tok.raw_line)
        else:
            target_body_lines.extend([""] + [t.raw_line for t in tokens_to_transfer])

        self.client.update_pr(target_pr_id, body="\n".join(target_body_lines))

        moved_names = ", ".join(t.package for t in tokens_to_transfer)
        return OperationResult(
            True,
            f"Successfully moved {len(tokens_to_transfer)} package(s) ({moved_names}) from #{source_pr_id} ➔ #{target_pr_id}."
        )

    def combine_groups(self, target_pr_id: int, source_pr_id: int) -> OperationResult:
        return self.combine_multiple_groups(target_pr_id, [source_pr_id])

    def combine_multiple_groups(self, target_pr_id: int, source_pr_ids: List[int]) -> OperationResult:
        """
        Batch combines multiple source PRs into a single target PR:
        - Checks maintainer push permissions.
        - Validates that target_pr_id is not in source_pr_ids.
        - Checks branch parity across all PRs.
        - Pre-scans all PRs against the Duplicate Package Collision Guard.
        - Gathers all source tokens and updates target PR body in ONE PATCH request!
        - Closes all source PRs in parallel via ThreadPoolExecutor.
        """
        if getattr(self.client, "is_read_only", False) is True:
            return OperationResult(
                False,
                f"Permission Denied: You have Read-Only access on '{self.repo}' (maintainer push rights required)."
            )

        cleaned_sources = [sid for sid in source_pr_ids if sid != target_pr_id]
        if not cleaned_sources:
            return OperationResult(False, "No valid source PRs specified to combine.")

        try:
            target = self.client.get_pr(target_pr_id)
        except Exception as e:
            return OperationResult(False, f"Failed to load target PR #{target_pr_id}: {e}")

        target_branch = target.get("base", {}).get("ref", "")
        target_tokens = pm.extract_tokens(target.get("body", ""))
        target_pkg_map = {t.key: t for t in target_tokens}

        host_target = pm.get_host_package_from_pr(target)
        if host_target and host_target.lower() not in target_pkg_map:
            target_pkg_map[host_target.lower()] = pm.PackageToken(self.owner, host_target, 0)

        def load_source_pr(sid: int):
            try:
                return sid, self.client.get_pr(sid)
            except Exception:
                return sid, None

        with ThreadPoolExecutor(max_workers=5) as executor:
            source_results = list(executor.map(load_source_pr, cleaned_sources))

        all_new_tokens: List[pm.PackageToken] = []
        sources_to_close: List[int] = []
        branch_mismatches = []
        conflicts = []

        accumulated_pkgs: Dict[str, Tuple[int, int]] = {k: (target_pr_id, t.pr_number) for k, t in target_pkg_map.items()}

        for sid, src in source_results:
            if not src:
                continue
            s_branch = src.get("base", {}).get("ref", "")
            if s_branch != target_branch:
                branch_mismatches.append(f"#{sid} ({s_branch} != {target_branch})")
                continue

            s_tokens = pm.extract_tokens(src.get("body", ""))
            if not s_tokens:
                continue

            for tok in s_tokens:
                if tok.key in accumulated_pkgs:
                    prev_pr, prev_num = accumulated_pkgs[tok.key]
                    conflicts.append(
                        f"'{tok.package}' appears in both #{prev_pr} (!{prev_num}) and #{sid} (!{tok.pr_number})"
                    )
                else:
                    accumulated_pkgs[tok.key] = (sid, tok.pr_number)
                    all_new_tokens.append(tok)

            sources_to_close.append(sid)

        if branch_mismatches:
            return OperationResult(False, f"Branch mismatch: cannot combine different branches: {', '.join(branch_mismatches)}.")

        if conflicts:
            return OperationResult(
                False,
                f"Collision: Multiple PRs contain the same submodule: {'; '.join(conflicts)}.",
                {"conflicts": conflicts}
            )

        if not all_new_tokens:
            return OperationResult(False, "No valid tokens found across source PRs to combine.")

        body_lines = (target.get("body") or "").splitlines()
        last_pr_idx = max((i for i, line in enumerate(body_lines) if pm.TOKEN_REGEX.match(line)), default=-1)

        if last_pr_idx != -1:
            for tok in reversed(all_new_tokens):
                body_lines.insert(last_pr_idx + 1, tok.raw_line)
        else:
            body_lines.extend([""] + [t.raw_line for t in all_new_tokens])

        new_body = "\n".join(body_lines)
        self.client.update_pr(target_pr_id, body=new_body)

        def close_source(sid: int):
            try:
                self.client.update_pr(sid, state="closed")
            except Exception:
                pass

        with ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(close_source, sources_to_close))

        return OperationResult(
            True,
            f"Successfully combined {len(sources_to_close)} PR(s) ({len(all_new_tokens)} pkgs) into #{target_pr_id}."
        )

    def disintegrate_group(self, target_pr_id: int) -> OperationResult:
        """Breaks down a group, reopening all peer child PRs in parallel."""
        if getattr(self.client, "is_read_only", False) is True:
            return OperationResult(
                False,
                f"Permission Denied: You have Read-Only access on '{self.repo}' (maintainer push rights required)."
            )

        try:
            target = self.client.get_pr(target_pr_id)
        except Exception as e:
            return OperationResult(False, f"Failed to load PR #{target_pr_id}: {e}")

        all_tokens = pm.extract_tokens(target.get("body", ""))
        if len(all_tokens) <= 1:
            return OperationResult(False, f"PR #{target_pr_id} is already standalone (<= 1 package).")

        host_package = pm.get_host_package_from_pr(target)
        peer_tokens = [t for t in all_tokens if host_package and t.key != host_package.lower()]
        if not peer_tokens and len(all_tokens) > 1:
            peer_tokens = all_tokens[1:]

        def restore_child(tok: pm.PackageToken):
            sub_repo = f"{tok.owner}/{tok.package}"
            child_id = pm.find_forwarded_child_id(self.client, sub_repo, str(tok.pr_number), exclude_pr_id=target_pr_id)
            if child_id:
                self.client.update_pr(child_id, state="open")

        with ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(restore_child, peer_tokens))

        peer_lines = {t.raw_line for t in peer_tokens}
        cleaned_lines = [line for line in target.get("body", "").splitlines() if line.strip() not in peer_lines]
        new_body = "\n".join(cleaned_lines)

        self.client.update_pr(target_pr_id, body=new_body)
        return OperationResult(True, f"Disintegrated group #{target_pr_id}. Restored {len(peer_tokens)} child PRs.")

    def accept_group(self, target_pr_id: int) -> OperationResult:
        """Approves the group for staging merge by posting 'merge ok', guarded by pre-flight checks."""
        if getattr(self.client, "is_read_only", False) is True:
            return OperationResult(
                False,
                f"Permission Denied: Repository '{self.repo}' is read-only for your user. Cannot approve PR #{target_pr_id}."
            )

        pr_data = self.client.get_pr(target_pr_id)
        target_branch = pr_data.get("base", {}).get("ref", "factory") if pr_data else "factory"

        # Pre-merge safety guard 1: Gitea Git merge conflict check
        if pr_data and pr_data.get("mergeable") is False:
            return OperationResult(
                False,
                f"Pre-merge safety check failed: PR #{target_pr_id} has Git merge conflicts with base branch '{target_branch}' (mergeable is False). Please rebase or resolve conflicts before approving!"
            )

        # Pre-merge safety guard 2: Multi-Architecture OBS build status check
        obs_status = self.get_obs_build_status(target_pr_id, target_branch)
        if obs_status.get("status") == "failed":
            failed_details = obs_status.get("failed_details", {})
            if failed_details:
                failed_items = [f"{pkg} [{', '.join(archs)}]" for pkg, archs in failed_details.items()]
                failed_str = ", ".join(failed_items)
            else:
                failed_str = ", ".join(obs_status.get("failed_pkgs", []))
            return OperationResult(
                False,
                f"Pre-merge safety check failed: OBS staging project '{obs_status.get('project')}' has failing packages ({failed_str}). Merge approval blocked!"
            )

        try:
            self.client.add_comment(target_pr_id, "merge ok")
            return OperationResult(True, f"Successfully commented 'merge ok' on PR #{target_pr_id}.")
        except Exception as e:
            return OperationResult(False, f"Failed to comment on PR #{target_pr_id}: {e}")

    def rename_group(self, target_pr_id: int, new_title: str) -> OperationResult:
        """Updates the title of a group or PR in Gitea."""
        if getattr(self.client, "is_read_only", False) is True:
            return OperationResult(
                False,
                f"Permission Denied: You have Read-Only access on '{self.repo}' (maintainer push rights required)."
            )

        title_clean = new_title.strip()
        if not title_clean:
            return OperationResult(False, "PR title cannot be empty.")
        try:
            self.client.update_pr(target_pr_id, title=title_clean)
            return OperationResult(True, f"Renamed PR #{target_pr_id} to '{title_clean}'.")
        except Exception as e:
            return OperationResult(False, f"Failed to rename PR #{target_pr_id}: {e}")

    def get_pr_diff(self, owner: str, package: str, pr_num: int) -> str:
        """Fetches the unified diff of a pull request from Gitea."""
        url = f"repos/{owner}/{package}/pulls/{pr_num}.diff"
        try:
            res = self.client.session.get(
                f"{self.client.base_url}/api/v1/{url}",
                headers={"Authorization": f"token {self.client.token}", "User-Agent": "pr-manage/2.0"},
                timeout=10
            )
            if res.status_code == 200:
                return res.text
            return f"(Could not load diff: HTTP {res.status_code})"
        except Exception as e:
            return f"(Error fetching diff: {e})"

    def get_obs_build_status(self, pr_id: int, branch: str = "factory") -> Dict:
        """
        Queries the OBS build result for the staging project associated with this PR.
        Aggregates results across ALL built architectures (not just x86_64).
        Returns a dict: {
            'status': 'succeeded'|'building'|'failed'|'none',
            'project': str,
            'failed_pkgs': [...],
            'failed_details': {'pkg': ['arch1', ...]},
            'failed_archs': [...],
            'arch_summary': {...},
            'archs': [...],
            'pkg_repos': {'pkg': 'repo_name'},
            'succeeded': int,
            'building': int,
            'total': int
        }
        """
        import subprocess
        import xml.etree.ElementTree as ET

        branch_name = "Factory" if branch.lower() in ("factory", "standard") else branch
        project = f"{self.owner}:{branch_name}:PullRequest:{pr_id}"

        try:
            res = subprocess.run(
                ["osc", "api", f"/build/{project}/_result"],
                capture_output=True,
                text=True,
                timeout=4
            )
            if res.returncode != 0:
                return {
                    "status": "none", "project": project, "total": 0,
                    "failed_pkgs": [], "failed_details": {}, "failed_archs": [],
                    "arch_summary": {}, "archs": [], "pkg_repos": {},
                    "succeeded": 0, "building": 0
                }

            root = ET.fromstring(res.stdout)
            results = root.findall(".//result")
            if not results:
                return {
                    "status": "none", "project": project, "total": 0,
                    "failed_pkgs": [], "failed_details": {}, "failed_archs": [],
                    "arch_summary": {}, "archs": [], "pkg_repos": {},
                    "succeeded": 0, "building": 0
                }

            failed_by_pkg = {}
            failed_archs = set()
            building_archs = set()
            succeeded_archs = set()
            all_archs = set()
            arch_summaries = {}
            pkg_repos = {}
            total_statuses = 0
            total_succeeded = 0
            total_building = 0
            total_excluded = 0

            for r in results:
                arch = r.get("arch", "unknown")
                repo = r.get("repository", "unknown")
                r_state = r.get("state", "")
                all_archs.add(arch)

                arch_entry = arch_summaries.setdefault(
                    arch,
                    {"succeeded": 0, "failed": 0, "building": 0, "excluded": 0, "state": r_state, "failed_pkgs": [], "repository": repo}
                )

                for s in r.findall("status"):
                    code = s.get("code")
                    pkg = s.get("package")
                    if pkg and repo:
                        pkg_repos[pkg] = repo
                    total_statuses += 1

                    if code in ("failed", "unresolvable", "broken"):
                        failed_by_pkg.setdefault(pkg, []).append(arch)
                        failed_archs.add(arch)
                        arch_entry["failed"] += 1
                        if pkg not in arch_entry["failed_pkgs"]:
                            arch_entry["failed_pkgs"].append(pkg)
                    elif code in ("building", "blocked", "scheduled", "dispatching"):
                        building_archs.add(arch)
                        total_building += 1
                        arch_entry["building"] += 1
                    elif code in ("succeeded", "finished"):
                        total_succeeded += 1
                        succeeded_archs.add(arch)
                        arch_entry["succeeded"] += 1
                    elif code in ("excluded", "disabled"):
                        total_excluded += 1
                        arch_entry["excluded"] += 1

            total_active = total_statuses - total_excluded

            for pkg in failed_by_pkg:
                failed_by_pkg[pkg] = sorted(list(set(failed_by_pkg[pkg])))

            if failed_by_pkg:
                status = "failed"
            elif building_archs or total_building > 0:
                status = "building"
            elif total_succeeded == total_active and total_active > 0:
                status = "succeeded"
            else:
                status = "none" if total_active == 0 else "building"

            return {
                "status": status,
                "project": project,
                "failed_pkgs": sorted(list(failed_by_pkg.keys())),
                "failed_details": failed_by_pkg,
                "failed_archs": sorted(list(failed_archs)),
                "arch_summary": arch_summaries,
                "archs": sorted(list(all_archs)),
                "pkg_repos": pkg_repos,
                "succeeded": total_succeeded,
                "building": total_building,
                "excluded": total_excluded,
                "total": total_active,
                "total_raw": total_statuses,
            }
        except Exception as e:
            return {
                "status": "none", "project": project, "total": 0,
                "failed_pkgs": [], "failed_details": {}, "failed_archs": [],
                "arch_summary": {}, "archs": [], "pkg_repos": {},
                "succeeded": 0, "building": 0, "error": str(e)
            }

    def get_obs_build_log(
        self,
        pr_id: int,
        package: str,
        arch: Optional[str] = None,
        branch: str = "factory",
        lines: int = 250
    ) -> str:
        """
        Fetches the build log tail from OBS for a specific package in the staging project.
        Automatically resolves the target architecture (prioritizing failing archs) and repository.
        """
        import subprocess

        branch_name = "Factory" if branch.lower() in ("factory", "standard") else branch
        project = f"{self.owner}:{branch_name}:PullRequest:{pr_id}"

        obs_status = self.get_obs_build_status(pr_id, branch)
        target_arch = arch
        if not target_arch:
            if package in obs_status.get("failed_details", {}):
                target_arch = obs_status["failed_details"][package][0]
            elif obs_status.get("failed_archs"):
                target_arch = obs_status["failed_archs"][0]
            elif "x86_64" in obs_status.get("archs", []) and obs_status.get("arch_summary", {}).get("x86_64", {}).get("excluded", 0) == 0:
                target_arch = "x86_64"
            elif obs_status.get("archs"):
                non_excl = [a for a in obs_status["archs"] if obs_status.get("arch_summary", {}).get(a, {}).get("excluded", 0) == 0]
                target_arch = non_excl[0] if non_excl else obs_status["archs"][0]
            else:
                target_arch = "x86_64"

        target_repo = obs_status.get("pkg_repos", {}).get(package)
        if not target_repo:
            if target_arch in obs_status.get("arch_summary", {}):
                target_repo = obs_status["arch_summary"][target_arch].get("repository")
            if not target_repo:
                target_repo = "openSUSE_Factory" if branch_name == "Factory" else "standard"

        try:
            cmd = ["osc", "api", f"/build/{project}/{target_repo}/{target_arch}/{package}/_log"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
            if res.returncode != 0:
                return f"⚠️ Unable to retrieve OBS build log for '{package}' ({target_arch}) in '{project}':\n{res.stderr.strip()}"

            log_text = res.stdout
            log_lines = log_text.splitlines()
            if lines and len(log_lines) > lines:
                header = f"=== OBS Build Log: {project} / {package} ({target_arch}) [Showing last {lines} of {len(log_lines)} lines] ===\n\n"
                return header + "\n".join(log_lines[-lines:])
            else:
                header = f"=== OBS Build Log: {project} / {package} ({target_arch}) [{len(log_lines)} lines] ===\n\n"
                return header + log_text
        except subprocess.TimeoutExpired:
            return f"⚠️ Timeout waiting for OBS build log for '{package}' in '{project}'."
        except Exception as e:
            return f"⚠️ Exception retrieving OBS build log: {e}"

    def audit_orphans(self, filter_branch: Optional[str] = None) -> List[OrphanPackagePR]:
        """
        Scans all open package PRs in the organization and detects any that are
        NOT currently associated with an active/open forward PR on the metaproject.
        """
        open_metaproject_prs = self.client.get_all_open_prs()
        tracked_pkgs: Dict[Tuple[str, int], int] = {}
        for opr in open_metaproject_prs:
            tokens = pm.extract_tokens(opr.get("body", ""))
            for t in tokens:
                tracked_pkgs[(t.package.lower(), t.pr_number)] = opr["number"]
            m = pm.HEAD_PR_REGEX.match(opr.get("head", {}).get("ref", ""))
            if m:
                tracked_pkgs[(m.group(1).lower(), int(m.group(2)))] = opr["number"]

        url_search = f"repos/issues/search?owner={self.owner}&type=pulls&state=open&limit=100"
        issues = []
        page = 1
        while True:
            batch = self.client.request(f"{url_search}&page={page}")
            if not isinstance(batch, list) or not batch:
                break
            issues.extend(batch)
            if len(batch) < 100:
                break
            page += 1

        candidate_orphans = []
        for iss in issues:
            repo = iss.get("repository", {}).get("name", "")
            if repo.lower() == self.client.repo_name.lower():
                continue
            pr_num = iss.get("number")
            key = (repo.lower(), pr_num)
            if key not in tracked_pkgs:
                candidate_orphans.append((repo, pr_num, iss))

        if not candidate_orphans:
            return []

        def fetch_pull_details(item):
            repo, pr_num, iss = item
            try:
                p_data = self.client.request(f"repos/{self.owner}/{repo}/pulls/{pr_num}")
                return repo, pr_num, p_data
            except Exception:
                return repo, pr_num, {}

        with ThreadPoolExecutor(max_workers=10) as executor:
            pull_details = list(executor.map(fetch_pull_details, candidate_orphans))

        orphans = []
        for repo, pr_num, p_data in pull_details:
            if not p_data:
                continue
            base_ref = p_data.get("base", {}).get("ref", "")
            head_ref = p_data.get("head", {}).get("ref", "")

            if filter_branch and filter_branch.lower() not in ("all", "") and base_ref != filter_branch:
                continue

            sub_repo = f"{self.owner}/{repo}"
            child_id = pm.find_forwarded_child_id(self.client, sub_repo, str(pr_num))

            fwd_state = "closed" if child_id else "missing"
            fwd_merged = False
            if child_id:
                try:
                    c_data = self.client.request(f"repos/{self.repo}/pulls/{child_id}")
                    fwd_state = c_data.get("state", "closed")
                    fwd_merged = c_data.get("merged", False)
                except Exception:
                    pass

            orphans.append(OrphanPackagePR(
                package=repo,
                pr_number=pr_num,
                title=p_data.get("title", ""),
                author=p_data.get("user", {}).get("login", "unknown"),
                base_branch=base_ref,
                head_branch=head_ref,
                html_url=p_data.get("html_url", ""),
                forward_pr_id=child_id,
                forward_pr_state=fwd_state,
                forward_pr_merged=fwd_merged
            ))

        orphans.sort(key=lambda o: (o.base_branch, o.package.lower()))
        return orphans

    def reopen_orphan_forward_pr(self, orphan: OrphanPackagePR) -> OperationResult:
        if not orphan.forward_pr_id:
            return OperationResult(False, f"No existing forward PR recorded on {self.repo} for {orphan.package} !{orphan.pr_number}.")
        try:
            self.client.update_pr(orphan.forward_pr_id, state="open")
            return OperationResult(True, f"Reopened forward PR #{orphan.forward_pr_id} ({orphan.package} !{orphan.pr_number}) on {self.repo}.")
        except Exception as e:
            return OperationResult(False, f"Failed to reopen PR #{orphan.forward_pr_id}: {e}")

    def adopt_orphan_into_group(self, target_pr_id: int, orphan: OrphanPackagePR) -> OperationResult:
        if getattr(self.client, "is_read_only", False) is True:
            return OperationResult(
                False,
                f"Permission Denied: You have Read-Only access on '{self.repo}' (maintainer push rights required)."
            )

        try:
            target = self.client.get_pr(target_pr_id)
        except Exception as e:
            return OperationResult(False, f"Could not load target PR #{target_pr_id}: {e}")

        target_tokens = pm.extract_tokens(target.get("body", ""))
        target_pkg_map = {t.key: t for t in target_tokens}

        host_pkg = pm.get_host_package_from_pr(target)
        if host_pkg and host_pkg.lower() not in target_pkg_map:
            target_pkg_map[host_pkg.lower()] = pm.PackageToken(self.owner, host_pkg, 0)

        if orphan.package.lower() in target_pkg_map:
            existing = target_pkg_map[orphan.package.lower()]
            return OperationResult(False, f"Package '{orphan.package}' already exists in group #{target_pr_id} (!{existing.pr_number}).")

        new_tok = pm.PackageToken(self.owner, orphan.package, orphan.pr_number)

        body_lines = (target.get("body") or "").splitlines()
        last_pr_idx = max((i for i, line in enumerate(body_lines) if pm.TOKEN_REGEX.match(line)), default=-1)

        if last_pr_idx != -1:
            body_lines.insert(last_pr_idx + 1, new_tok.raw_line)
        else:
            body_lines.extend(["", new_tok.raw_line])

        self.client.update_pr(target_pr_id, body="\n".join(body_lines))

        if orphan.forward_pr_id and orphan.forward_pr_state == "open":
            try:
                self.client.update_pr(orphan.forward_pr_id, state="closed")
            except Exception:
                pass

        return OperationResult(True, f"Adopted {orphan.package} (!{orphan.pr_number}) into group #{target_pr_id}.")

    def discover_valid_workspaces(self) -> List[Tuple[str, str, Dict[str, bool]]]:
        """
        Dynamically discovers and validates available metaproject workspaces on Gitea.
        Filters out non-existent repositories (e.g. 404s like KDE/_ObsPrj) and
        returns (repo_full_name, permission_label, permissions_dict).
        """
        import json
        from pathlib import Path

        candidates: List[str] = [self.repo]

        # 1. Discover from user organizations
        try:
            orgs = self.client.request("user/orgs")
            if isinstance(orgs, list):
                for o in orgs:
                    uname = o.get("username", "")
                    if uname:
                        for sfx in ("_ObsPrj", "Factory"):
                            cand = f"{uname}/{sfx}"
                            if cand not in candidates:
                                candidates.append(cand)
        except Exception:
            pass

        # 2. Public openSUSE metaproject
        if "openSUSE/Factory" not in candidates:
            candidates.append("openSUSE/Factory")

        # 3. Read user-configured workspaces from ~/.config/pr-manage.json
        cfg = pm.load_config()
        for w in cfg.get("workspaces", []):
            if w not in candidates:
                candidates.append(w)

        # 4. Validate existence & query permissions in parallel
        def validate_candidate(c: str):
            info = self.client.get_repo_info(c)
            if not info:
                return None
            perms = info.get("permissions", {})
            if perms.get("admin"):
                lbl = "Admin"
            elif perms.get("push"):
                lbl = "Maintainer"
            else:
                lbl = "Read-Only"
            return c, lbl, perms

        with ThreadPoolExecutor(max_workers=6) as executor:
            validated = list(executor.map(validate_candidate, candidates))

        results = [v for v in validated if v is not None]

        def sort_key(item):
            name, lbl, perms = item
            is_curr = (name.lower() == self.repo.lower())
            p_score = 0 if is_curr else (1 if perms.get("push") else 2)
            return p_score, name.lower()

        results.sort(key=sort_key)
        return results

    def record_recent_workspace(self, repo_name: str):
        """Saves a successfully accessed workspace into ~/.config/pr-manage.json."""
        pm.save_config(active_workspace=repo_name, add_workspace=repo_name)
