#!/usr/bin/env python3
"""
openSUSE Staging Group PR Manager - Terminal User Interface (TUI).

A responsive, keyboard-driven curses interface for managing forwarded PRs on _ObsPrj:
- Column 1: Staging Groups & Standalone Forwards (with live OBS build status)
- Column 2: Selected Group Members (with Host ★, multi-select [✓], and upstream PR #)
- Column 3: Standalone / Ungrouped Staging Queue (with multi-select [✓] and search)

Keybindings:
  [Tab] / [1-3]      Switch active column
  [Home] / [End]     Jump to top / bottom of active list (or [g] / [G])
  [Enter] / [i]      Inspect highlighted package: view PR details & syntax-colored diff
  [Space]            Toggle multi-selection mark ([✓]) on highlighted item
  [*]                Select all visible items in active column
  [_]                Deselect all items in active column
  [I]                Invert selection in active column
  [a] / [A]          Add marked (or highlighted) ungrouped packages into active group
  [d] / [D] / [u]    Delete/unselect marked (or highlighted) packages from group
  [v] / [m]          Move marked package(s) directly to another group
  [e]                Rename active group PR title in Gitea
  [c]                Combine marked groups in Column 1 (or prompt for single combine)
  [o]                Approve active group for merge ('merge ok') with OBS failure guard
  [O] / [!]          Audit out-of-sync / orphan PRs (with 1-click Reopen or Adopt)
  [w] / [x]          Open highlighted PR in web browser (xdg-open)
  [W]                Switch metaproject workspace (GNOME, KDE, Factory, custom)
  [f] / [b]          Cycle branch filter (all / factory / next)
  [/]                Search / filter ungrouped queue
  [r]                Refresh live state from Gitea & OBS
  [?]                Help dialog
  [q]                Quit
"""

import sys
import os
import re
import curses
import subprocess
from typing import Optional, List, Tuple, Set, Dict
from concurrent.futures import ThreadPoolExecutor

import pr_manage as pm
import staging_service as ss


class StagingTUI:
    def __init__(self, service: Optional[ss.StagingService] = None):
        self.service = service or ss.StagingService()
        self.groups: List[ss.StagingGroup] = []
        self.ungrouped: List[ss.StagingGroup] = []

        # Navigation State
        self.active_col = 0  # 0: Groups, 1: Members, 2: Ungrouped
        self.group_idx = 0
        self.member_idx = 0
        self.ungrouped_idx = 0

        # Multi-Selection Sets
        self.selected_groups: Set[int] = set()        # stores marked PR IDs in Column 1
        self.selected_members: Set[str] = set()       # stores member tok.key in Column 2
        self.selected_ungrouped: Set[int] = set()     # stores ungrouped PR IDs in Column 3

        # View Filters
        self.branch_filter_idx = 0
        self.branch_filters = ["all", "factory", "next"]
        self.ungrouped_search = ""

        # OBS Build Status Cache & Asynchronous Background Executor
        self.obs_status_cache: Dict[int, Dict] = {}
        self.pending_obs_queries: Set[int] = set()
        self.obs_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="obs_status")

        # Status & Notifications
        self.status_msg = "Ready. Press [?] for help, [r] to refresh."
        self.status_color = 1  # 1: Normal, 2: Success, 3: Cyan, 4: Warning, 5: Error
        self.loading = False

    def load_data(self, stdscr: Optional[curses.window] = None, message: Optional[str] = None):
        """Fetches data from Gitea via the StagingService, rendering an immediate splash or toast."""
        self.loading = True
        if stdscr:
            if not self.groups and not self.ungrouped:
                self.draw_splash(stdscr, message or f"Target: {self.service.repo}")
            else:
                self.status_msg = message or "Refreshing live staging queue from Gitea..."
                self.status_color = 4
                self.draw(stdscr)

        current_branch = self.branch_filters[self.branch_filter_idx]
        try:
            self.groups, self.ungrouped = self.service.fetch_all(filter_branch=current_branch)
            self.status_msg = f"Fetched {len(self.groups)} staging PR(s) and {len(self.ungrouped)} ungrouped item(s)."
            self.status_color = 2
        except Exception as e:
            self.status_msg = f"Error fetching PRs: {e}"
            self.status_color = 5
        finally:
            self.loading = False

        # Clean selection sets that no longer exist
        self.selected_members.clear()
        valid_group_ids = {g.pr_id for g in self.groups}
        self.selected_groups = {gid for gid in self.selected_groups if gid in valid_group_ids}
        valid_ungrouped_ids = {u.pr_id for u in self.ungrouped}
        self.selected_ungrouped = {uid for uid in self.selected_ungrouped if uid in valid_ungrouped_ids}

        # Clear OBS status cache and pending queries so it refreshes for active view
        self.obs_status_cache.clear()
        self.pending_obs_queries.clear()

        # Clamp indices
        if self.groups:
            self.group_idx = max(0, min(self.group_idx, len(self.groups) - 1))
        else:
            self.group_idx = 0

        self._clamp_indices()

    def _clamp_indices(self):
        current_members = self.get_current_group_members()
        if current_members:
            self.member_idx = max(0, min(self.member_idx, len(current_members) - 1))
        else:
            self.member_idx = 0

        filtered_ungrouped = self.get_filtered_ungrouped()
        if filtered_ungrouped:
            self.ungrouped_idx = max(0, min(self.ungrouped_idx, len(filtered_ungrouped) - 1))
        else:
            self.ungrouped_idx = 0

    def get_current_group(self) -> Optional[ss.StagingGroup]:
        if 0 <= self.group_idx < len(self.groups):
            return self.groups[self.group_idx]
        return None

    def get_current_group_members(self) -> List[pm.PackageToken]:
        group = self.get_current_group()
        if group:
            return group.tokens
        return []

    def get_filtered_ungrouped(self) -> List[ss.StagingGroup]:
        if not self.ungrouped_search:
            return self.ungrouped
        query = self.ungrouped_search.lower()
        return [
            u for u in self.ungrouped
            if query in u.title.lower() or (u.host_package and query in u.host_package.lower()) or query in str(u.pr_id)
        ]

    def get_cached_obs_status(self, pr_id: int, branch: str) -> Dict:
        """Asynchronously queries OBS build results in a background thread."""
        if pr_id in self.obs_status_cache:
            return self.obs_status_cache[pr_id]

        if pr_id not in self.pending_obs_queries:
            self.pending_obs_queries.add(pr_id)

            def fetch_in_background():
                try:
                    res = self.service.get_obs_build_status(pr_id, branch)
                except Exception as e:
                    res = {"status": "none", "error": str(e)}
                self.obs_status_cache[pr_id] = res
                self.pending_obs_queries.discard(pr_id)

            self.obs_executor.submit(fetch_in_background)

        return {"status": "loading"}

    def run(self, stdscr):
        curses.curs_set(0)
        stdscr.timeout(100)

        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_WHITE, -1)
        curses.init_pair(2, curses.COLOR_GREEN, -1)
        curses.init_pair(3, curses.COLOR_CYAN, -1)
        curses.init_pair(4, curses.COLOR_YELLOW, -1)
        curses.init_pair(5, curses.COLOR_RED, -1)
        curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(7, curses.COLOR_BLACK, curses.COLOR_WHITE)

        self.load_data(stdscr=stdscr, message=f"Target: {self.service.repo}")

        while True:
            self.draw(stdscr)
            try:
                ch = stdscr.getch()
            except KeyboardInterrupt:
                break

            if ch == -1:
                continue

            if ch in (ord("q"), ord("Q")):
                break
            elif ch in (curses.KEY_RESIZE,):
                continue
            elif ch in (ord("	"),):
                self.active_col = (self.active_col + 1) % 3
            elif ch in (curses.KEY_BTAB, ord("`")):
                self.active_col = (self.active_col - 1) % 3
            elif ch in (ord("1"),):
                self.active_col = 0
            elif ch in (ord("2"),):
                self.active_col = 1
            elif ch in (ord("3"),):
                self.active_col = 2
            elif ch in (curses.KEY_UP, ord("k"), ord("K")):
                self.move_cursor(-1)
            elif ch in (curses.KEY_DOWN, ord("j"), ord("J")):
                self.move_cursor(1)
            elif ch in (curses.KEY_PPAGE,):
                self.move_cursor(-8)
            elif ch in (curses.KEY_NPAGE,):
                self.move_cursor(8)
            elif ch in (curses.KEY_HOME, ord("g")):
                self.jump_home()
            elif ch in (curses.KEY_END, ord("G"), getattr(curses, "KEY_LL", 360)):
                self.jump_end()
            elif ch in (10, 10, curses.KEY_ENTER, ord("i")):
                self.handle_inspect(stdscr)
            elif ch in (ord("l"), ord("L")):
                self.handle_view_build_log(stdscr)
            elif ch == ord(" "):
                self.handle_toggle_selection()
            elif ch == ord("*"):
                self.handle_select_all()
            elif ch == ord("_"):
                self.handle_clear_selection()
            elif ch in (ord("I"),):
                self.handle_invert_selection()
            elif ch in (ord("a"), ord("A")):
                self.handle_add_packages(stdscr)
            elif ch in (ord("d"), ord("D"), ord("u"), ord("U"), curses.KEY_DC, ord("x")):
                self.handle_unselect_packages(stdscr)
            elif ch in (ord("v"), ord("V")):
                self.handle_move_packages(stdscr)
            elif ch in (ord("e"),):
                self.handle_rename_group(stdscr)
            elif ch in (ord("c"), ord("C")):
                self.handle_combine_groups(stdscr)
            elif ch in (ord("o"),):
                self.handle_accept_group(stdscr)
            elif ch in (ord("O"), ord("!")):
                self.handle_audit_orphans(stdscr)
            elif ch in (ord("w"),):
                self.handle_open_browser()
            elif ch in (ord("W"),):
                self.handle_switch_workspace(stdscr)
            elif ch in (ord("f"), ord("b")):
                self.branch_filter_idx = (self.branch_filter_idx + 1) % len(self.branch_filters)
                self.load_data(stdscr=stdscr, message="Switching branch filter...")
            elif ch in (ord("r"), ord("R")):
                self.load_data(stdscr=stdscr, message="Refreshing live staging queue...")
            elif ch == ord("/"):
                self.prompt_search(stdscr)
            elif ch in (ord("?"), ord("h")):
                self.show_help_modal(stdscr)

    def move_cursor(self, delta: int):
        if self.active_col == 0:
            if self.groups:
                self.group_idx = max(0, min(self.group_idx + delta, len(self.groups) - 1))
                self.member_idx = 0
                self.selected_members.clear()
        elif self.active_col == 1:
            members = self.get_current_group_members()
            if members:
                self.member_idx = max(0, min(self.member_idx + delta, len(members) - 1))
        elif self.active_col == 2:
            ungrouped = self.get_filtered_ungrouped()
            if ungrouped:
                self.ungrouped_idx = max(0, min(self.ungrouped_idx + delta, len(ungrouped) - 1))

    def jump_home(self):
        if self.active_col == 0:
            self.group_idx = 0
            self.member_idx = 0
        elif self.active_col == 1:
            self.member_idx = 0
        elif self.active_col == 2:
            self.ungrouped_idx = 0

    def jump_end(self):
        if self.active_col == 0:
            if self.groups:
                self.group_idx = len(self.groups) - 1
                self.member_idx = 0
        elif self.active_col == 1:
            members = self.get_current_group_members()
            if members:
                self.member_idx = len(members) - 1
        elif self.active_col == 2:
            ungrouped = self.get_filtered_ungrouped()
            if ungrouped:
                self.ungrouped_idx = len(ungrouped) - 1

    def handle_toggle_selection(self):
        if self.active_col == 0:
            if self.groups and 0 <= self.group_idx < len(self.groups):
                g = self.groups[self.group_idx]
                if g.pr_id in self.selected_groups:
                    self.selected_groups.remove(g.pr_id)
                    self.status_msg = f"Unmarked PR #{g.pr_id} ({len(self.selected_groups)} marked)."
                else:
                    self.selected_groups.add(g.pr_id)
                    self.status_msg = f"Marked PR #{g.pr_id} for batch combine ({len(self.selected_groups)} marked)."
                self.status_color = 1
        elif self.active_col == 1:
            members = self.get_current_group_members()
            if members and 0 <= self.member_idx < len(members):
                tok = members[self.member_idx]
                target = self.get_current_group()
                if target and target.host_package and tok.key == target.host_package.lower():
                    self.status_msg = f"Host package '{tok.package}' cannot be removed from group (use 'd' to disintegrate)."
                    self.status_color = 3
                    return
                if tok.key in self.selected_members:
                    self.selected_members.remove(tok.key)
                    self.status_msg = f"Unmarked '{tok.package}' ({len(self.selected_members)} marked)."
                else:
                    self.selected_members.add(tok.key)
                    self.status_msg = f"Marked '{tok.package}' for batch action ({len(self.selected_members)} marked)."
                self.status_color = 1
        elif self.active_col == 2:
            ungrouped_list = self.get_filtered_ungrouped()
            if ungrouped_list and 0 <= self.ungrouped_idx < len(ungrouped_list):
                item = ungrouped_list[self.ungrouped_idx]
                pkg_name = item.host_package or item.title.replace("Forwarded PRs: ", "").strip()
                if item.pr_id in self.selected_ungrouped:
                    self.selected_ungrouped.remove(item.pr_id)
                    self.status_msg = f"Unmarked '{pkg_name}' ({len(self.selected_ungrouped)} marked)."
                else:
                    self.selected_ungrouped.add(item.pr_id)
                    self.status_msg = f"Marked '{pkg_name}' for batch add ({len(self.selected_ungrouped)} marked)."
                self.status_color = 1

    def handle_select_all(self):
        """[*] Marks all visible items in the active column."""
        if self.active_col == 0:
            self.selected_groups = {g.pr_id for g in self.groups}
            self.status_msg = f"Marked all {len(self.selected_groups)} PR(s) in Column 1."
        elif self.active_col == 1:
            members = self.get_current_group_members()
            self.selected_members = {t.key for t in members}
            self.status_msg = f"Marked all {len(self.selected_members)} member(s) in group."
        elif self.active_col == 2:
            filtered = self.get_filtered_ungrouped()
            self.selected_ungrouped = {u.pr_id for u in filtered}
            self.status_msg = f"Marked all {len(self.selected_ungrouped)} visible ungrouped PR(s)."
        self.status_color = 1

    def handle_clear_selection(self):
        """[_] Deselects all items in the active column."""
        if self.active_col == 0:
            self.selected_groups.clear()
            self.status_msg = "Cleared selection marks in Column 1."
        elif self.active_col == 1:
            self.selected_members.clear()
            self.status_msg = "Cleared member marks in Column 2."
        elif self.active_col == 2:
            self.selected_ungrouped.clear()
            self.status_msg = "Cleared selection marks in Column 3."
        self.status_color = 1

    def handle_invert_selection(self):
        """[I] Inverts selection marks in the active column."""
        if self.active_col == 0:
            all_ids = {g.pr_id for g in self.groups}
            self.selected_groups = all_ids - self.selected_groups
            self.status_msg = f"Inverted marks in Column 1 ({len(self.selected_groups)} marked)."
        elif self.active_col == 1:
            members = self.get_current_group_members()
            all_keys = {t.key for t in members}
            self.selected_members = all_keys - self.selected_members
            self.status_msg = f"Inverted member marks ({len(self.selected_members)} marked)."
        elif self.active_col == 2:
            filtered = self.get_filtered_ungrouped()
            all_ids = {u.pr_id for u in filtered}
            self.selected_ungrouped = all_ids - self.selected_ungrouped
            self.status_msg = f"Inverted ungrouped marks ({len(self.selected_ungrouped)} marked)."
        self.status_color = 1

    def handle_inspect(self, stdscr):
        """[Enter] / [i] Opens interactive diff and changelog viewer modal."""
        owner = self.service.owner
        pkg_name = ""
        pr_num = 0

        if self.active_col == 1:
            members = self.get_current_group_members()
            if members and 0 <= self.member_idx < len(members):
                tok = members[self.member_idx]
                owner = tok.owner
                pkg_name = tok.package
                pr_num = tok.pr_number
        elif self.active_col == 2:
            ungrouped_list = self.get_filtered_ungrouped()
            if ungrouped_list and 0 <= self.ungrouped_idx < len(ungrouped_list):
                u = ungrouped_list[self.ungrouped_idx]
                if u.tokens:
                    tok = u.tokens[0]
                    owner = tok.owner
                    pkg_name = tok.package
                    pr_num = tok.pr_number
                elif u.host_package:
                    pkg_name = u.host_package
                    # Extract from title or head ref
                    m = pm.HEAD_PR_REGEX.match(u.head_ref)
                    pr_num = int(m.group(2)) if m else u.pr_id
        elif self.active_col == 0:
            target = self.get_current_group()
            if target:
                if target.tokens:
                    tok = target.tokens[0]
                    owner = tok.owner
                    pkg_name = tok.package
                    pr_num = tok.pr_number
                elif target.host_package:
                    pkg_name = target.host_package
                    pr_num = target.pr_id

        if not pkg_name or not pr_num:
            self.status_msg = "No package PR to inspect."
            self.status_color = 3
            return

        self.status_msg = f"Loading diff for {owner}/{pkg_name}!{pr_num}..."
        self.status_color = 4
        self.draw(stdscr)

        diff_text = self.service.get_pr_diff(owner, pkg_name, pr_num)
        self.show_diff_viewer_modal(stdscr, owner, pkg_name, pr_num, diff_text)

    def show_diff_viewer_modal(self, stdscr, owner: str, pkg: str, pr_num: int, diff_text: str):
        """Scrollable modal pager displaying unified diff and packaging changelogs."""
        h, w = stdscr.getmaxyx()
        modal_w = min(96, w - 4)
        modal_h = min(28, h - 4)
        start_y = max(1, (h - modal_h) // 2)
        start_x = max(1, (w - modal_w) // 2)

        win = curses.newwin(modal_h, modal_w, start_y, start_x)
        win.keypad(True)

        lines = diff_text.splitlines() if diff_text.strip() else ["(Diff is empty or already merged)"]
        scroll_pos = 0

        while True:
            win.erase()
            win.box()
            title_str = f" Diff Inspection: {owner}/{pkg} !{pr_num} ({len(lines)} lines) "
            win.addstr(0, 2, title_str[:modal_w - 4], curses.color_pair(3) | curses.A_BOLD)

            max_display = modal_h - 3
            for i in range(max_display):
                line_idx = scroll_pos + i
                if line_idx >= len(lines):
                    break
                row_y = 1 + i
                raw_line = lines[line_idx]

                # Syntax coloring
                if raw_line.startswith("+") and not raw_line.startswith("+++"):
                    attr = curses.color_pair(2)  # Green
                elif raw_line.startswith("-") and not raw_line.startswith("---"):
                    attr = curses.color_pair(5)  # Red
                elif raw_line.startswith("@@") or raw_line.startswith("diff --git"):
                    attr = curses.color_pair(3) | curses.A_BOLD  # Cyan
                else:
                    attr = curses.color_pair(1)

                win.addstr(row_y, 2, raw_line[:modal_w - 4], attr)

            pct = int(((scroll_pos + max_display) / max(1, len(lines))) * 100)
            pct = min(100, pct)
            footer = f" [↑/↓/PgUp/PgDn] Scroll  [Home/End] Top/Bottom  [Esc/q] Close  ({pct}%) "
            win.addstr(modal_h - 1, 2, footer[:modal_w - 4], curses.color_pair(1) | curses.A_DIM)
            win.refresh()

            ch = win.getch()
            if ch in (27, ord("q"), ord("Q")):
                break
            elif ch in (curses.KEY_UP, ord("k")):
                scroll_pos = max(0, scroll_pos - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                scroll_pos = min(max(0, len(lines) - max_display), scroll_pos + 1)
            elif ch in (curses.KEY_PPAGE,):
                scroll_pos = max(0, scroll_pos - 15)
            elif ch in (curses.KEY_NPAGE,):
                scroll_pos = min(max(0, len(lines) - max_display), scroll_pos + 15)
            elif ch in (curses.KEY_HOME, ord("g")):
                scroll_pos = 0
            elif ch in (curses.KEY_END, ord("G")):
                scroll_pos = max(0, len(lines) - max_display)

    def select_package_log_modal(self, stdscr, group: ss.StagingGroup, packages: List[str], obs: Dict) -> Optional[str]:
        """Modal menu to select which package build log to view in a multi-package group."""
        h, w = stdscr.getmaxyx()
        modal_w = min(74, w - 4)
        modal_h = min(16, len(packages) + 6)
        start_y = max(1, (h - modal_h) // 2)
        start_x = max(1, (w - modal_w) // 2)

        win = curses.newwin(modal_h, modal_w, start_y, start_x)
        win.box()

        sel_idx = 0
        failed_details = obs.get("failed_details", {})

        while True:
            win.erase()
            win.box()
            title = f" Select Package Build Log (PR #{group.pr_id}) "
            win.addstr(0, max(2, (modal_w - len(title)) // 2), title[:modal_w - 4], curses.color_pair(3) | curses.A_BOLD)
            win.addstr(1, 2, "Choose package log to view:", curses.color_pair(1) | curses.A_DIM)

            for i, p in enumerate(packages):
                if 2 + i >= modal_h - 2:
                    break
                is_selected = (i == sel_idx)
                cursor = "▸ " if is_selected else "  "
                archs = failed_details.get(p, [])
                if archs:
                    label = f"{cursor}🔴 {p} [Failed: {', '.join(archs)}]"
                    attr = curses.color_pair(5) | (curses.A_BOLD if is_selected else 0)
                else:
                    label = f"{cursor}📦 {p}"
                    attr = curses.color_pair(2 if is_selected else 1) | (curses.A_BOLD if is_selected else 0)

                win.addstr(2 + i, 2, label[:modal_w - 4], attr)

            foot = " Enter: View Log  •  q/Esc: Cancel "
            win.addstr(modal_h - 2, 2, foot[:modal_w - 4], curses.color_pair(2) | curses.A_BOLD)
            win.refresh()

            ch = stdscr.getch()
            if ch in (ord("q"), ord("Q"), 27):
                return None
            elif ch in (curses.KEY_UP, ord("k")):
                sel_idx = max(0, sel_idx - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                sel_idx = min(len(packages) - 1, sel_idx + 1)
            elif ch in (10, curses.KEY_ENTER):
                return packages[sel_idx]

    def handle_view_build_log(self, stdscr):
        """[l] / [L] Opens interactive OBS build failure log viewer modal."""
        target = self.get_current_group()
        if not target:
            self.status_msg = "No group selected to view build log."
            self.status_color = 3
            return

        pkg_name = ""
        obs_status = self.get_cached_obs_status(target.pr_id, target.branch)

        if self.active_col == 1:
            members = self.get_current_group_members()
            if members and 0 <= self.member_idx < len(members):
                pkg_name = members[self.member_idx].package
            elif target.host_package:
                pkg_name = target.host_package
        elif self.active_col == 2:
            ungrouped_list = self.get_filtered_ungrouped()
            if ungrouped_list and 0 <= self.ungrouped_idx < len(ungrouped_list):
                u = ungrouped_list[self.ungrouped_idx]
                pkg_name = u.host_package or (u.tokens[0].package if u.tokens else "")
        elif self.active_col == 0:
            failed_pkgs = obs_status.get("failed_pkgs", [])
            all_pkgs = target.package_names
            if len(failed_pkgs) > 1:
                chosen = self.select_package_log_modal(stdscr, target, failed_pkgs, obs_status)
                if not chosen:
                    self.status_msg = "Log viewer cancelled."
                    self.status_color = 1
                    return
                pkg_name = chosen
            elif len(failed_pkgs) == 1:
                pkg_name = failed_pkgs[0]
            elif len(all_pkgs) > 1:
                chosen = self.select_package_log_modal(stdscr, target, all_pkgs, obs_status)
                if not chosen:
                    self.status_msg = "Log viewer cancelled."
                    self.status_color = 1
                    return
                pkg_name = chosen
            elif target.tokens:
                pkg_name = target.tokens[0].package
            elif target.host_package:
                pkg_name = target.host_package

        if not pkg_name:
            self.status_msg = "No package identified to fetch build log."
            self.status_color = 3
            return

        self.draw_splash(stdscr, f"Fetching OBS build log for '{pkg_name}' (PR #{target.pr_id})...")
        log_text = self.service.get_obs_build_log(target.pr_id, pkg_name, branch=target.branch)
        self.show_log_viewer_modal(stdscr, target.pr_id, pkg_name, log_text)

    def show_log_viewer_modal(self, stdscr, pr_id: int, pkg: str, log_text: str):
        """Scrollable modal viewer for OBS build logs with error syntax highlighting."""
        h, w = stdscr.getmaxyx()
        modal_w = min(100, w - 4)
        modal_h = min(32, h - 4)
        start_y = max(1, (h - modal_h) // 2)
        start_x = max(1, (w - modal_w) // 2)

        win = curses.newwin(modal_h, modal_w, start_y, start_x)
        win.box()

        lines = log_text.splitlines()
        scroll_pos = max(0, len(lines) - (modal_h - 4))
        max_display = modal_h - 4

        while True:
            win.erase()
            win.box()
            title = f" OBS Build Log: {pkg} (PR #{pr_id}) [{len(lines)} lines] "
            win.addstr(0, max(2, (modal_w - len(title)) // 2), title[:modal_w - 4], curses.color_pair(3) | curses.A_BOLD)

            for i in range(max_display):
                line_idx = scroll_pos + i
                if line_idx >= len(lines):
                    break
                line = lines[line_idx]

                lower_l = line.lower()
                if "error:" in lower_l or "failed" in lower_l or "fatal:" in lower_l:
                    attr = curses.color_pair(5) | curses.A_BOLD
                elif "warning:" in lower_l:
                    attr = curses.color_pair(4) | curses.A_BOLD
                elif line.startswith("==="):
                    attr = curses.color_pair(3) | curses.A_BOLD
                else:
                    attr = curses.color_pair(1)

                win.addstr(1 + i, 2, line[:modal_w - 4], attr)

            pos_pct = int(((scroll_pos + max_display) / max(1, len(lines))) * 100)
            pos_pct = min(100, pos_pct)
            foot = f" Pos: {scroll_pos + 1}/{len(lines)} ({pos_pct}%)  •  ↑/↓/PgUp/PgDn: Scroll  •  q/Esc: Close "
            win.addstr(modal_h - 2, 2, foot[:modal_w - 4], curses.color_pair(2) | curses.A_BOLD)

            win.refresh()
            ch = stdscr.getch()

            if ch in (ord("q"), ord("Q"), 27):
                break
            elif ch in (curses.KEY_UP, ord("k"), ord("K")):
                scroll_pos = max(0, scroll_pos - 1)
            elif ch in (curses.KEY_DOWN, ord("j"), ord("J")):
                scroll_pos = min(max(0, len(lines) - max_display), scroll_pos + 1)
            elif ch in (curses.KEY_PPAGE,):
                scroll_pos = max(0, scroll_pos - 15)
            elif ch in (curses.KEY_NPAGE, ord(" ")):
                scroll_pos = min(max(0, len(lines) - max_display), scroll_pos + 15)
            elif ch in (curses.KEY_HOME, ord("g")):
                scroll_pos = 0
            elif ch in (curses.KEY_END, ord("G")):
                scroll_pos = max(0, len(lines) - max_display)

    def handle_rename_group(self, stdscr):
        if self.service.client.is_read_only:
            self.status_msg = f"🔒 Permission Denied: Read-Only access on '{self.service.repo}' (maintainer push rights required)."
            self.status_color = 5
            return
        """[e] Inline prompt to rename the title of the active group in Gitea."""
        target = self.get_current_group()
        if not target:
            self.status_msg = "Select a group in Column 1 to rename."
            self.status_color = 3
            return

        curses.curs_set(1)
        h, w = stdscr.getmaxyx()
        stdscr.addstr(h - 1, 0, " " * (w - 1))
        prompt_txt = f"Rename PR #{target.pr_id} Title: "
        stdscr.addstr(h - 1, 0, prompt_txt, curses.color_pair(4) | curses.A_BOLD)
        curses.echo()
        stdscr.timeout(-1)
        new_title = stdscr.getstr().decode("utf-8").strip()
        curses.noecho()
        curses.curs_set(0)
        stdscr.timeout(100)

        if not new_title or new_title == target.title:
            self.status_msg = "Rename cancelled (empty or unchanged)."
            self.status_color = 1
            return

        self.status_msg = f"Renaming PR #{target.pr_id} to '{new_title}'..."
        self.status_color = 4
        self.draw(stdscr)

        res = self.service.rename_group(target.pr_id, new_title)
        self.status_msg = res.message
        self.status_color = 2 if res.success else 5
        if res.success:
            target.title = new_title

    def handle_open_browser(self):
        """[w] / [x] Opens the highlighted PR in the default desktop browser."""
        url = ""
        if self.active_col == 0:
            target = self.get_current_group()
            url = target.html_url if target else ""
        elif self.active_col == 1:
            members = self.get_current_group_members()
            if members and 0 <= self.member_idx < len(members):
                tok = members[self.member_idx]
                url = f"{self.service.client.base_url}/{tok.owner}/{tok.package}/pulls/{tok.pr_number}"
        elif self.active_col == 2:
            ungrouped = self.get_filtered_ungrouped()
            if ungrouped and 0 <= self.ungrouped_idx < len(ungrouped):
                url = ungrouped[self.ungrouped_idx].html_url

        if not url:
            self.status_msg = "No URL available to open."
            self.status_color = 3
            return

        try:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.status_msg = f"Opened {url} in browser."
            self.status_color = 2
        except Exception as e:
            self.status_msg = f"Failed to open browser: {e}"
            self.status_color = 5

    def handle_switch_workspace(self, stdscr):
        """[W] Modal dialog to dynamically discover and switch between valid Gitea workspaces."""
        self.status_msg = "Discovering and validating accessible workspaces on Gitea..."
        self.status_color = 4
        self.draw(stdscr)

        discovered = self.service.discover_valid_workspaces()
        items = [(name, f"{name} [{lbl}]" + (" (Current)" if name.lower() == self.service.repo.lower() else "")) for name, lbl, _ in discovered]
        items.append(("+ Custom Repository...", "+ Custom Repository..."))

        h, w = stdscr.getmaxyx()
        modal_w = min(70, w - 4)
        modal_h = min(18, len(items) + 5)
        start_y = max(1, (h - modal_h) // 2)
        start_x = max(1, (w - modal_w) // 2)

        win = curses.newwin(modal_h, modal_w, start_y, start_x)
        win.keypad(True)
        sel_idx = 0

        while True:
            win.erase()
            win.box()
            win.addstr(0, 2, " Switch Staging Workspace ", curses.color_pair(3) | curses.A_BOLD)
            perm_str = "Admin" if self.service.client.has_admin_access else ("Maintainer" if self.service.client.has_push_access else "Read-Only")
            curr_str = f"Active: {self.service.repo} [{perm_str}]"
            win.addstr(1, 2, curr_str[:modal_w - 4], curses.color_pair(1) | curses.A_DIM)

            for i, (repo_val, display_label) in enumerate(items[:modal_h - 4]):
                row_y = 2 + i
                is_sel = (i == sel_idx)
                prefix = "▸ " if is_sel else "  "
                attr = curses.color_pair(6) | curses.A_BOLD if is_sel else curses.color_pair(1)
                win.addstr(row_y, 2, f"{prefix}{display_label}"[:modal_w - 4], attr)

            win.addstr(modal_h - 2, 2, "[Enter] Select   [Esc/q] Cancel", curses.color_pair(4) | curses.A_BOLD)
            win.refresh()

            ch = win.getch()
            if ch in (27, ord("q"), ord("Q")):
                self.status_msg = "Workspace switch cancelled."
                self.status_color = 1
                return
            elif ch in (curses.KEY_UP, ord("k")):
                sel_idx = max(0, sel_idx - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                sel_idx = min(len(items) - 1, sel_idx + 1)
            elif ch in (10, 13, curses.KEY_ENTER):
                chosen_repo, _ = items[sel_idx]
                if chosen_repo == "+ Custom Repository...":
                    curses.curs_set(1)
                    stdscr.addstr(h - 1, 0, " " * (w - 1))
                    stdscr.addstr(h - 1, 0, "Enter repository (owner/repo): ", curses.color_pair(4) | curses.A_BOLD)
                    curses.echo()
                    stdscr.timeout(-1)
                    chosen_repo = stdscr.getstr().decode("utf-8").strip()
                    curses.noecho()
                    curses.curs_set(0)
                    stdscr.timeout(100)

                if chosen_repo and chosen_repo != self.service.repo:
                    # Validate repository existence on Gitea
                    info = self.service.client.get_repo_info(chosen_repo)
                    if not info:
                        self.status_msg = f"❌ Repository '{chosen_repo}' does not exist on {self.service.client.base_url}."
                        self.status_color = 5
                        return

                    self.service.client.set_repo(chosen_repo)
                    self.service.record_recent_workspace(chosen_repo)
                    self.groups.clear()
                    self.ungrouped.clear()
                    self.load_data(stdscr=stdscr, message=f"Switched workspace to {chosen_repo}...")
                return

    def handle_add_packages(self, stdscr):
        if self.service.client.is_read_only:
            self.status_msg = f"🔒 Permission Denied: Read-Only access on '{self.service.repo}' (maintainer push rights required)."
            self.status_color = 5
            return
        target = self.get_current_group()
        if not target:
            self.status_msg = "Select a target group in Column 1 first."
            self.status_color = 3
            return

        pkgs_to_add: List[str] = []

        if self.selected_ungrouped:
            id_map = {u.pr_id: (u.host_package or u.title.replace("Forwarded PRs: ", "").strip()) for u in self.ungrouped}
            pkgs_to_add = [id_map[uid] for uid in self.selected_ungrouped if uid in id_map]
        else:
            ungrouped_list = self.get_filtered_ungrouped()
            if ungrouped_list and 0 <= self.ungrouped_idx < len(ungrouped_list):
                candidate = ungrouped_list[self.ungrouped_idx]
                pkgs_to_add = [candidate.host_package or candidate.title.replace("Forwarded PRs: ", "").strip()]

        if not pkgs_to_add:
            self.status_msg = "No ungrouped packages marked or highlighted to add."
            self.status_color = 3
            return

        self.status_msg = f"Adding {len(pkgs_to_add)} package(s) into group #{target.pr_id}..."
        self.status_color = 4
        self.draw(stdscr)

        res = self.service.add_packages_to_group(target.pr_id, pkgs_to_add)
        self.status_msg = res.message
        self.status_color = 2 if res.success else 5
        self.selected_ungrouped.clear()
        if res.success:
            self.load_data(stdscr=stdscr)

    def handle_unselect_packages(self, stdscr):
        if self.service.client.is_read_only:
            self.status_msg = f"🔒 Permission Denied: Read-Only access on '{self.service.repo}' (maintainer push rights required)."
            self.status_color = 5
            return
        target = self.get_current_group()
        if not target:
            self.status_msg = "No active group selected."
            self.status_color = 3
            return

        members = self.get_current_group_members()
        if not members:
            self.status_msg = "No members in group to remove."
            self.status_color = 3
            return

        pkgs_to_remove: List[str] = []

        if self.selected_members:
            pkgs_to_remove = list(self.selected_members)
        else:
            if 0 <= self.member_idx < len(members):
                pkgs_to_remove = [members[self.member_idx].package]

        if not pkgs_to_remove:
            self.status_msg = "No members selected or highlighted to remove."
            self.status_color = 3
            return

        self.status_msg = f"Removing {len(pkgs_to_remove)} package(s) from group #{target.pr_id}..."
        self.status_color = 4
        self.draw(stdscr)

        res = self.service.remove_packages_from_group(target.pr_id, pkgs_to_remove)
        self.status_msg = res.message
        self.status_color = 2 if res.success else 5
        self.selected_members.clear()
        if res.success:
            self.load_data(stdscr=stdscr)

    def handle_move_packages(self, stdscr):
        if self.service.client.is_read_only:
            self.status_msg = f"🔒 Permission Denied: Read-Only access on '{self.service.repo}' (maintainer push rights required)."
            self.status_color = 5
            return
        current_group = self.get_current_group()
        if not current_group:
            self.status_msg = "No active source group selected."
            self.status_color = 3
            return

        members = self.get_current_group_members()
        if not members:
            self.status_msg = "No members in group to move."
            self.status_color = 3
            return

        pkgs_to_move: List[str] = []
        if self.selected_members:
            pkgs_to_move = list(self.selected_members)
        else:
            if 0 <= self.member_idx < len(members):
                pkgs_to_move = [members[self.member_idx].package]

        if not pkgs_to_move:
            self.status_msg = "No packages selected to move."
            self.status_color = 3
            return

        eligible_groups = [
            g for g in self.groups
            if g.pr_id != current_group.pr_id and g.branch == current_group.branch
        ]

        if not eligible_groups:
            self.status_msg = f"No other groups targeting '{current_group.branch}' available to move into."
            self.status_color = 3
            return

        dest_group = self.select_group_modal(stdscr, eligible_groups, pkgs_to_move)
        if not dest_group:
            self.status_msg = "Move cancelled."
            self.status_color = 1
            return

        self.status_msg = f"Moving {len(pkgs_to_move)} package(s) to #{dest_group.pr_id}..."
        self.status_color = 4
        self.draw(stdscr)

        res = self.service.move_packages_between_groups(current_group.pr_id, dest_group.pr_id, pkgs_to_move)
        self.status_msg = res.message
        self.status_color = 2 if res.success else 5
        self.selected_members.clear()
        if res.success:
            self.load_data(stdscr=stdscr)

    def select_group_modal(self, stdscr, eligible_groups: List[ss.StagingGroup], pkgs: List[str]) -> Optional[ss.StagingGroup]:
        h, w = stdscr.getmaxyx()
        modal_w = min(64, w - 4)
        modal_h = min(14, len(eligible_groups) + 6)
        start_y = max(1, (h - modal_h) // 2)
        start_x = max(1, (w - modal_w) // 2)

        win = curses.newwin(modal_h, modal_w, start_y, start_x)
        win.keypad(True)
        selected_dest_idx = 0

        while True:
            win.erase()
            win.box()
            header_str = f" Move {len(pkgs)} Package(s) to Target Group "
            win.addstr(0, 2, header_str[:modal_w - 4], curses.color_pair(3) | curses.A_BOLD)

            prompt_str = "Select Destination Group:"
            win.addstr(1, 2, prompt_str, curses.color_pair(1) | curses.A_DIM)

            for i, g in enumerate(eligible_groups[:modal_h - 4]):
                row_y = 2 + i
                is_sel = (i == selected_dest_idx)
                prefix = "▸ " if is_sel else "  "
                row_txt = f"{prefix}#{g.pr_id} [{g.branch}] ({g.member_count}p) {g.title[:25]}"
                attr = curses.color_pair(6) | curses.A_BOLD if is_sel else curses.color_pair(1)
                win.addstr(row_y, 2, row_txt[:modal_w - 4], attr)

            win.addstr(modal_h - 2, 2, "[Enter] Confirm Move   [Esc/q] Cancel", curses.color_pair(4) | curses.A_BOLD)
            win.refresh()

            ch = win.getch()
            if ch in (27, ord("q"), ord("Q")):
                return None
            elif ch in (curses.KEY_UP, ord("k")):
                selected_dest_idx = max(0, selected_dest_idx - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                selected_dest_idx = min(len(eligible_groups) - 1, selected_dest_idx + 1)
            elif ch in (10, 10, curses.KEY_ENTER):
                return eligible_groups[selected_dest_idx]

    def select_combine_target_modal(self, stdscr, marked_prs: List[ss.StagingGroup]) -> Optional[ss.StagingGroup]:
        h, w = stdscr.getmaxyx()
        modal_w = min(74, w - 4)
        modal_h = min(16, len(marked_prs) + 6)
        start_y = max(1, (h - modal_h) // 2)
        start_x = max(1, (w - modal_w) // 2)

        win = curses.newwin(modal_h, modal_w, start_y, start_x)
        win.keypad(True)

        curr_g = self.get_current_group()
        def tui_anchor_score(g: ss.StagingGroup):
            is_active = bool(curr_g and g.pr_id == curr_g.pr_id)
            return (1 if is_active else 0, g.member_count, g.pr_id)

        best_score = (-1, -1, -1)
        selected_idx = 0
        for i, g in enumerate(marked_prs):
            sc = tui_anchor_score(g)
            if sc > best_score:
                best_score = sc
                selected_idx = i

        while True:
            win.erase()
            win.box()
            header_str = f" Combine {len(marked_prs)} PRs: Select Target Group "
            win.addstr(0, 2, header_str[:modal_w - 4], curses.color_pair(3) | curses.A_BOLD)

            prompt_str = f"Select the Target Group to absorb the other {len(marked_prs) - 1} PR(s):"
            win.addstr(1, 2, prompt_str[:modal_w - 4], curses.color_pair(1) | curses.A_DIM)

            for i, g in enumerate(marked_prs[:modal_h - 4]):
                row_y = 2 + i
                is_sel = (i == selected_idx)
                prefix = "▸ " if is_sel else "  "

                badge = f"({g.member_count}p)"
                rec_str = " [Recommended Target]" if g.is_group and g.member_count == max_pkgs else ""
                row_txt = f"{prefix}#{g.pr_id} [{g.branch}] {badge} {g.title[:30]}{rec_str}"
                attr = curses.color_pair(6) | curses.A_BOLD if is_sel else curses.color_pair(1)
                win.addstr(row_y, 2, row_txt[:modal_w - 4], attr)

            win.addstr(modal_h - 2, 2, "[Enter] Confirm Target   [Esc/q] Cancel", curses.color_pair(4) | curses.A_BOLD)
            win.refresh()

            ch = win.getch()
            if ch in (27, ord("q"), ord("Q")):
                return None
            elif ch in (curses.KEY_UP, ord("k")):
                selected_idx = max(0, selected_idx - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                selected_idx = min(len(marked_prs) - 1, selected_idx + 1)
            elif ch in (10, 10, curses.KEY_ENTER):
                return marked_prs[selected_idx]

    def handle_combine_groups(self, stdscr):
        if self.service.client.is_read_only:
            self.status_msg = f"🔒 Permission Denied: Read-Only access on '{self.service.repo}' (maintainer push rights required)."
            self.status_color = 5
            return
        if len(self.selected_groups) >= 2:
            marked_prs = [g for g in self.groups if g.pr_id in self.selected_groups]

            branches = {g.branch for g in marked_prs}
            if len(branches) > 1:
                self.status_msg = f"Branch mismatch: cannot combine PRs across different branches ({', '.join(branches)})."
                self.status_color = 5
                return

            target_group = self.select_combine_target_modal(stdscr, marked_prs)
            if not target_group:
                self.status_msg = "Batch combine cancelled."
                self.status_color = 1
                return

            sources = [g.pr_id for g in marked_prs if g.pr_id != target_group.pr_id]
            self.status_msg = f"Combining {len(sources)} PR(s) into #{target_group.pr_id}..."
            self.status_color = 4
            self.draw(stdscr)

            res = self.service.combine_multiple_groups(target_group.pr_id, sources)
            self.status_msg = res.message
            self.status_color = 2 if res.success else 5
            self.selected_groups.clear()
            if res.success:
                self.load_data(stdscr=stdscr)
            return

        target = self.get_current_group()
        if not target:
            self.status_msg = "Select a target group in Column 1 first."
            self.status_color = 3
            return

        other_groups = [g for g in self.groups if g.pr_id != target.pr_id]
        if not other_groups:
            self.status_msg = f"No other groups available to combine into #{target.pr_id}."
            self.status_color = 3
            return

        source_pr_id = self.prompt_number(stdscr, f"Combine which source PR into #{target.pr_id}? Enter PR #: ")
        if not source_pr_id:
            self.status_msg = "Combine cancelled."
            self.status_color = 1
            return

        self.status_msg = f"Combining PR #{source_pr_id} into #{target.pr_id}..."
        self.status_color = 4
        self.draw(stdscr)

        res = self.service.combine_groups(target.pr_id, source_pr_id)
        self.status_msg = res.message
        self.status_color = 2 if res.success else 5
        if res.success:
            self.load_data(stdscr=stdscr)

    def handle_accept_group(self, stdscr):
        """[o] Submits 'merge ok' with pre-merge OBS failure guard."""
        if self.active_col == 0 and len(self.selected_groups) > 1:
            marked_groups = [g for g in self.groups if g.pr_id in self.selected_groups]

            # Pre-flight check: Git merge conflicts
            conflicted_prs = [f"#{g.pr_id}" for g in marked_groups if g.mergeable is False]
            if conflicted_prs:
                self.status_msg = f"❌ Blocked: Marked PR(s) {', '.join(conflicted_prs)} have Git merge conflicts!"
                self.status_color = 5
                return

            failed_prs = []
            for g in marked_groups:
                st = self.get_cached_obs_status(g.pr_id, g.branch)
                if st.get("status") == "failed":
                    failed_prs.append(f"#{g.pr_id}")

            fail_list = ", ".join(failed_prs)
            warn_prefix = f"⚠️ OBS FAILING on {fail_list}! " if failed_prs else ""
            ids_str = ", ".join(f"#{g.pr_id}" for g in marked_groups)
            confirm = self.prompt_confirm(
                stdscr, f"{warn_prefix}Approve {len(marked_groups)} marked PRs ({ids_str}) with 'merge ok'?"
            )
            if not confirm:
                self.status_msg = "Batch accept cancelled."
                self.status_color = 1
                return

            self.status_msg = f"Submitting 'merge ok' for {len(marked_groups)} PRs..."
            self.status_color = 4
            self.draw(stdscr)

            count = 0
            for g in marked_groups:
                res = self.service.accept_group(g.pr_id)
                if res.success:
                    count += 1
            self.status_msg = f"Successfully approved {count} marked PR(s) with 'merge ok'."
            self.status_color = 2
            self.selected_groups.clear()
            return

        if self.active_col == 2:
            ungrouped_list = self.get_filtered_ungrouped()
            if not ungrouped_list or not (0 <= self.ungrouped_idx < len(ungrouped_list)):
                self.status_msg = "No PR selected in Column 3 to accept."
                self.status_color = 3
                return
            target = ungrouped_list[self.ungrouped_idx]
        else:
            target = self.get_current_group()

        if not target:
            self.status_msg = "No PR selected to accept."
            self.status_color = 3
            return

        # Pre-Merge Safety Guard 1: Check Git Merge Conflict first!
        if target.mergeable is False:
            self.status_msg = f"❌ Blocked: PR #{target.pr_id} has Git merge conflicts with base branch '{target.branch}'!"
            self.status_color = 5
            return

        # Pre-Merge Safety Guard 2: Multi-Architecture OBS Build Status!
        obs_status = self.get_cached_obs_status(target.pr_id, target.branch)
        warn_prefix = ""
        if obs_status.get("status") == "failed":
            failed_details = obs_status.get("failed_details", {})
            if failed_details:
                failed_items = [f"{pkg} [{','.join(archs)}]" for pkg, archs in failed_details.items()]
                failed_str = ", ".join(failed_items[:3])
            else:
                failed_str = ", ".join(obs_status.get("failed_pkgs", [])[:3])
            warn_prefix = f"⚠️ OBS FAILING ({failed_str})! "

        label = f"group #{target.pr_id}" if target.is_group else f"single PR #{target.pr_id}"
        confirm = self.prompt_confirm(stdscr, f"{warn_prefix}Approve {label} ('{target.title[:25]}') with 'merge ok'?")
        if not confirm:
            self.status_msg = "Accept cancelled."
            self.status_color = 1
            return

        self.status_msg = f"Submitting 'merge ok' for #{target.pr_id}..."
        self.status_color = 4
        self.draw(stdscr)

        res = self.service.accept_group(target.pr_id)
        self.status_msg = res.message
        self.status_color = 2 if res.success else 5

    def handle_audit_orphans(self, stdscr):
        self.status_msg = f"Auditing open package PRs in {self.service.owner}..."
        self.status_color = 4
        self.draw(stdscr)

        current_branch = self.branch_filters[self.branch_filter_idx]
        orphans = self.service.audit_orphans(filter_branch=current_branch)

        if not orphans:
            self.status_msg = f"✅ Audit clean: 100% of open package PRs in {self.service.owner} are tracked in staging!"
            self.status_color = 2
            return

        self.show_orphan_audit_modal(stdscr, orphans)

    def show_orphan_audit_modal(self, stdscr, orphans: List[ss.OrphanPackagePR]):
        h, w = stdscr.getmaxyx()
        modal_w = min(84, w - 4)
        modal_h = min(18, len(orphans) + 6)
        start_y = max(1, (h - modal_h) // 2)
        start_x = max(1, (w - modal_w) // 2)

        win = curses.newwin(modal_h, modal_w, start_y, start_x)
        win.keypad(True)
        selected_idx = 0
        marked_orphans: Set[int] = set()

        target = self.get_current_group()
        target_name = f"#{target.pr_id}" if target else "active group"

        while True:
            win.erase()
            win.box()
            header_str = f" ⚠️ Out-of-Sync PRs Audit ({len(orphans)} untracked) "
            win.addstr(0, 2, header_str[:modal_w - 4], curses.color_pair(5) | curses.A_BOLD)

            prompt_str = f"Target Group: {target_name}  •  [Space] Mark  [R] Reopen Fwd  [A] Adopt  [Esc] Close"
            win.addstr(1, 2, prompt_str[:modal_w - 4], curses.color_pair(1) | curses.A_DIM)

            for i, o in enumerate(orphans[:modal_h - 4]):
                row_y = 2 + i
                is_cursor = (i == selected_idx)
                is_chk = (i in marked_orphans)

                cursor_p = "▸ " if is_cursor else "  "
                chk_box = "[✓] " if is_chk else "[ ] "
                fwd_str = f"Fwd: #{o.forward_pr_id} ({o.forward_pr_state})" if o.forward_pr_id else "No Fwd PR"
                row_txt = f"{cursor_p}{chk_box}{o.package} !{o.pr_number} ({o.author}) ➔ {o.base_branch} [{fwd_str}]"

                if is_cursor:
                    attr = curses.color_pair(6) | curses.A_BOLD
                elif is_chk:
                    attr = curses.color_pair(2) | curses.A_BOLD
                else:
                    attr = curses.color_pair(1)

                win.addstr(row_y, 2, row_txt[:modal_w - 4], attr)

            win.addstr(modal_h - 2, 2, "[R] Reopen Fwd PR   [A] Adopt into Group   [Esc/q] Close", curses.color_pair(4) | curses.A_BOLD)
            win.refresh()

            ch = win.getch()
            if ch in (27, ord("q"), ord("Q")):
                break
            elif ch in (curses.KEY_UP, ord("k")):
                selected_idx = max(0, selected_idx - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                selected_idx = min(len(orphans) - 1, selected_idx + 1)
            elif ch == ord(" "):
                if selected_idx in marked_orphans:
                    marked_orphans.remove(selected_idx)
                else:
                    marked_orphans.add(selected_idx)
            elif ch in (ord("r"), ord("R")):
                targets = [orphans[i] for i in (marked_orphans if marked_orphans else [selected_idx])]
                reopened = 0
                for o in targets:
                    res = self.service.reopen_orphan_forward_pr(o)
                    if res.success:
                        reopened += 1
                self.status_msg = f"Reopened {reopened} forwarded child PR(s) on {self.service.repo}."
                self.status_color = 2
                self.load_data(stdscr=stdscr)
                break
            elif ch in (ord("a"), ord("A")):
                if not target:
                    self.status_msg = "No active target group selected to adopt into."
                    self.status_color = 3
                    break
                targets = [orphans[i] for i in (marked_orphans if marked_orphans else [selected_idx])]
                adopted = 0
                for o in targets:
                    res = self.service.adopt_orphan_into_group(target.pr_id, o)
                    if res.success:
                        adopted += 1
                self.status_msg = f"Adopted {adopted} orphan package(s) into group #{target.pr_id}."
                self.status_color = 2
                self.load_data(stdscr=stdscr)
                break

    def draw_splash(self, stdscr, message: str = "Connecting to src.opensuse.org..."):
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        top_bar = f" openSUSE Staging Group Manager  •  Repo: {self.service.repo}"
        stdscr.addstr(0, 0, top_bar[:w - 1], curses.color_pair(3) | curses.A_BOLD)

        box_w = min(68, w - 4)
        box_h = 7
        start_y = max(1, (h - box_h) // 2)
        start_x = max(0, (w - box_w) // 2)

        win = curses.newwin(box_h, box_w, start_y, start_x)
        win.box()
        win.addstr(0, 2, " Loading Staging Queue ", curses.color_pair(3) | curses.A_BOLD)
        win.addstr(2, 4, "⚡ Connecting to Gitea API...", curses.color_pair(4) | curses.A_BOLD)
        win.addstr(3, 4, message[:box_w - 8], curses.color_pair(1))
        win.addstr(4, 4, "Fetching open PRs and staging group topologies...", curses.color_pair(1) | curses.A_DIM)
        win.refresh()

        stdscr.addstr(h - 1, 0, "Please wait, contacting src.opensuse.org...", curses.color_pair(1) | curses.A_DIM)
        stdscr.refresh()

    def prompt_search(self, stdscr):
        curses.curs_set(1)
        h, w = stdscr.getmaxyx()
        stdscr.addstr(h - 1, 0, " " * (w - 1))
        stdscr.addstr(h - 1, 0, "Search Ungrouped: ", curses.color_pair(3) | curses.A_BOLD)
        curses.echo()
        stdscr.timeout(-1)
        query = stdscr.getstr().decode("utf-8").strip()
        curses.noecho()
        curses.curs_set(0)
        stdscr.timeout(100)

        self.ungrouped_search = query
        self.ungrouped_idx = 0
        self.status_msg = f"Filtered ungrouped queue by '{query}'." if query else "Cleared search filter."
        self.status_color = 1

    def prompt_number(self, stdscr, prompt: str) -> Optional[int]:
        curses.curs_set(1)
        h, w = stdscr.getmaxyx()
        stdscr.addstr(h - 1, 0, " " * (w - 1))
        stdscr.addstr(h - 1, 0, prompt, curses.color_pair(4) | curses.A_BOLD)
        curses.echo()
        stdscr.timeout(-1)
        val = stdscr.getstr().decode("utf-8").strip()
        curses.noecho()
        curses.curs_set(0)
        stdscr.timeout(100)

        try:
            return int(val)
        except ValueError:
            return None

    def prompt_confirm(self, stdscr, prompt: str) -> bool:
        h, w = stdscr.getmaxyx()
        stdscr.addstr(h - 1, 0, " " * (w - 1))
        stdscr.addstr(h - 1, 0, f"{prompt} [y/N]: ", curses.color_pair(4) | curses.A_BOLD)
        stdscr.timeout(-1)
        ch = stdscr.getch()
        stdscr.timeout(100)
        return ch in (ord("y"), ord("Y"))

    def show_help_modal(self, stdscr):
        h, w = stdscr.getmaxyx()
        modal_w = min(74, w - 4)
        modal_h = min(22, h - 2)
        start_y = max(1, (h - modal_h) // 2)
        start_x = max(1, (w - modal_w) // 2)

        win = curses.newwin(modal_h, modal_w, start_y, start_x)
        win.box()
        win.addstr(0, 2, " openSUSE Staging PR Manager - Help ", curses.color_pair(3) | curses.A_BOLD)

        help_lines = [
            "Navigation:",
            "  Tab / 1, 2, 3       Switch active column (Groups / Members / Queue)",
            "  Up / Down / j / k   Move selection cursor",
            "  Home / End / g / G  Jump to top / bottom of active list",
            "",
            "Inspection & Selection:",
            "  Enter / i           Inspect package PR details & unified changes diff",
            "  l / L               View OBS build failure log for highlighted package/group",
            "  Space               Toggle [✓] multi-select mark on highlighted item",
            "  *                   Mark all visible items in active column",
            "  _                   Deselect all marks in active column",
            "  I                   Invert selection in active column",
            "",
            "Grouping & Modification:",
            "  A / a               Add marked (or highlighted) package(s) into group",
            "  D / d / u           Remove marked (or highlighted) package(s) from group",
            "  v / m               Move marked package(s) directly to another group",
            "  e                   Rename active group PR title in Gitea",
            "  c / C               Batch combine marked groups in Col 1 into target",
            "",
            "Approval & Utilities:",
            "  o                   Approve group for staging merge ('merge ok')",
            "  O / !               Audit out-of-sync / orphan PRs (with Reopen or Adopt)",
            "  w / x               Open highlighted PR in web browser (xdg-open)",
            "  W                   Switch metaproject workspace (GNOME, KDE, Factory)",
            "  f / b               Cycle branch filter (all / factory / next)",
            "  /                   Search / filter in ungrouped queue",
            "  r                   Refresh live state from Gitea & OBS",
            "  q                   Quit application",
        ]

        for i, line in enumerate(help_lines[:modal_h - 2]):
            win.addstr(i + 1, 2, line[:modal_w - 4])

        win.refresh()
        stdscr.timeout(-1)
        stdscr.getch()
        stdscr.timeout(100)

    def draw(self, stdscr):
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        if h < 12 or w < 74:
            stdscr.addstr(0, 0, "Terminal too small. Please enlarge to at least 74x12.")
            stdscr.refresh()
            return

        # Top Bar with Permission Indicator
        branch_str = f"Branch: [{self.branch_filters[self.branch_filter_idx].upper()}]"
        if self.service.client.is_read_only:
            perm_badge = "[🔒 READ-ONLY]"
        elif self.service.client.has_admin_access:
            perm_badge = "[🟢 ADMIN]"
        else:
            perm_badge = "[🟢 MAINTAINER]"

        repo_str = f"Repo: {self.service.repo} {perm_badge}"
        top_bar = f" openSUSE Staging Group Manager  •  {repo_str}  •  {branch_str}"
        top_attr = curses.color_pair(4 if self.service.client.is_read_only else 3) | curses.A_BOLD
        stdscr.addstr(0, 0, top_bar[:w - 1], top_attr)
        help_hint = "[W] Switch [?] Help [q] Quit "
        if w > len(top_bar) + len(help_hint):
            stdscr.addstr(0, w - len(help_hint), help_hint, curses.color_pair(1) | curses.A_DIM)

        # Calculate Column Widths
        body_h = h - 3
        col1_w = max(28, int(w * 0.30))
        col2_w = max(34, int(w * 0.38))
        col3_w = w - col1_w - col2_w

        # Draw Columns
        self.draw_groups_column(stdscr, 1, 0, body_h, col1_w)
        self.draw_members_column(stdscr, 1, col1_w, body_h, col2_w)
        self.draw_ungrouped_column(stdscr, 1, col1_w + col2_w, body_h, col3_w)

        # Bottom Status Bar
        status_color = curses.color_pair(self.status_color)
        if self.status_color == 5:
            status_color |= curses.A_BOLD

        status_prefix = "⚡ " if self.loading else "▸ "
        full_status = f"{status_prefix}{self.status_msg}"
        stdscr.addstr(h - 2, 0, full_status[:w - 1], status_color)

        # Bottom Keybindings Guide
        cmd_guide = "[Enter] Diff [l] Log [Space] Mark [*] All [A] Add [D] Rem [v] Move [e] Rename [c] Comb [o] OK"
        stdscr.addstr(h - 1, 0, cmd_guide[:w - 1], curses.color_pair(1) | curses.A_DIM)

        stdscr.refresh()

    def draw_groups_column(self, stdscr, y: int, x: int, h: int, w: int):
        is_active = (self.active_col == 0)
        border_attr = curses.color_pair(3) | curses.A_BOLD if is_active else curses.color_pair(1) | curses.A_DIM

        marked_badge = f" [{len(self.selected_groups)} marked]" if self.selected_groups else ""
        title = f" 1. FORWARDS & GROUPS ({len(self.groups)}){marked_badge} "
        self._draw_box(stdscr, y, x, h, w, title, border_attr)

        max_items = h - 2
        scroll_start = max(0, self.group_idx - (max_items // 2))

        for idx, g in enumerate(self.groups[scroll_start:scroll_start + max_items]):
            actual_idx = scroll_start + idx
            is_selected = (actual_idx == self.group_idx)
            is_marked = (g.pr_id in self.selected_groups)
            row_y = y + 1 + idx

            cursor_prefix = "▸ " if is_selected else "  "
            check_box = "[✓] " if is_marked else "[ ] "

            # Query cached OBS build status (async, non-blocking)
            obs_st = self.get_cached_obs_status(g.pr_id, g.branch)
            status_code = obs_st.get("status", "none")
            if status_code == "succeeded":
                obs_badge = "🟢 "
                row_color = curses.color_pair(2) # Green
            elif status_code == "building":
                obs_badge = "🔵 "
                row_color = curses.color_pair(4) # Light Blue / Cyan
            elif status_code == "failed":
                obs_badge = "🔴 "
                row_color = curses.color_pair(5) # Red
            else:
                obs_badge = ""
                row_color = curses.color_pair(1) # Default Grey

            conflict_tag = "⚠️ " if g.mergeable is False else ""
            if g.is_group:
                badge = f"({g.member_count}p)"
                summary_str = f"{conflict_tag}{obs_badge}#{g.pr_id} [{g.branch}] {badge} {g.title}"
            else:
                pkg = g.host_package or g.title.replace("Forwarded PRs: ", "").strip()
                summary_str = f"{conflict_tag}{obs_badge}#{g.pr_id} [{g.branch}] {pkg}"

            line_str = f"{cursor_prefix}{check_box}{summary_str:<{w - 6}}"

            if is_selected:
                attr = curses.color_pair(6 if is_active else 7) | curses.A_BOLD
            elif is_marked:
                attr = curses.color_pair(2) | curses.A_BOLD
            else:
                attr = row_color

            stdscr.addstr(row_y, x + 1, line_str[:w - 2], attr)

    def draw_members_column(self, stdscr, y: int, x: int, h: int, w: int):
        is_active = (self.active_col == 1)
        border_attr = curses.color_pair(3) | curses.A_BOLD if is_active else curses.color_pair(1) | curses.A_DIM

        target = self.get_current_group()
        marked_badge = f" [{len(self.selected_members)} marked]" if self.selected_members else ""
        count_str = f" ({target.member_count})" if target else ""
        title = f" 2. MEMBERS{count_str}{marked_badge} "
        self._draw_box(stdscr, y, x, h, w, title, border_attr)

        if not target:
            stdscr.addstr(y + 2, x + 2, "No group selected.", curses.color_pair(1) | curses.A_DIM)
            return

        # Query OBS status for active target
        obs_status = self.get_cached_obs_status(target.pr_id, target.branch)
        obs_state = obs_status.get("status", "none")
        if obs_state == "failed":
            failed_cnt = len(obs_status.get("failed_pkgs", []))
            obs_txt = f"OBS: 🔴 Failed ({failed_cnt} pkgs)"
            obs_attr = curses.color_pair(5) | curses.A_BOLD
        elif obs_state == "building":
            obs_txt = f"OBS: 🟡 Building ({obs_status.get('succeeded', 0)}/{obs_status.get('total', 0)})"
            obs_attr = curses.color_pair(4) | curses.A_BOLD
        elif obs_state == "succeeded":
            obs_txt = f"OBS: 🟢 Built ({obs_status.get('total', 0)} pkgs)"
            obs_attr = curses.color_pair(2) | curses.A_BOLD
        elif obs_state == "loading":
            obs_txt = "OBS: ⚡ Checking..."
            obs_attr = curses.color_pair(3) | curses.A_DIM
        else:
            obs_txt = "OBS: ⚪ Standby"
            obs_attr = curses.color_pair(1) | curses.A_DIM

        conflict_str = "  •  ⚠️ GIT CONFLICT" if target.mergeable is False else ""
        info_line1 = f"Target: #{target.pr_id} [{target.branch}]  •  {obs_txt}{conflict_str}"
        info_line2 = f"Title:  {target.title}"
        host_str = f"Host:   ★ {target.host_package or 'None'}"

        stdscr.addstr(y + 1, x + 2, info_line1[:w - 4], curses.color_pair(3) | curses.A_BOLD)
        stdscr.addstr(y + 2, x + 2, info_line2[:w - 4], curses.color_pair(1))
        stdscr.addstr(y + 3, x + 2, host_str[:w - 4], curses.color_pair(4) | curses.A_BOLD)

        stdscr.addstr(y + 4, x + 1, "─" * (w - 2), curses.color_pair(1) | curses.A_DIM)

        members = target.tokens
        members_y_start = y + 5
        max_items = h - 6
        scroll_start = max(0, self.member_idx - (max_items // 2))

        if not members and target.host_package:
            cursor_prefix = "▸ " if is_active else "  "
            item_str = f"{cursor_prefix}{target.host_package} (host package)"
            stdscr.addstr(members_y_start, x + 2, item_str[:w - 4], curses.color_pair(4) | curses.A_BOLD)
        else:
            for idx, tok in enumerate(members[scroll_start:scroll_start + max_items]):
                actual_idx = scroll_start + idx
                is_selected = (actual_idx == self.member_idx)
                is_marked = (tok.key in self.selected_members)
                row_y = members_y_start + idx

                cursor_prefix = "▸ " if is_selected else "  "
                is_host = bool(target.host_package and tok.key == target.host_package.lower())
                if is_host:
                    check_box = "[★] "
                    host_tag = " (host)"
                else:
                    check_box = "[✓] " if is_marked else "[ ] "
                    host_tag = ""
                failed_archs = obs_status.get("failed_details", {}).get(tok.package, [])
                arch_tag = f" [✗ {','.join(failed_archs)}]" if failed_archs else ""
                item_str = f"{cursor_prefix}{check_box}{tok.package}{host_tag}{arch_tag} (!{tok.pr_number})"
                line_str = f"{item_str:<{w - 4}}"

                if is_selected:
                    attr = curses.color_pair(6 if is_active else 7) | curses.A_BOLD
                elif is_marked:
                    attr = curses.color_pair(2) | curses.A_BOLD
                else:
                    attr = curses.color_pair(1)

                stdscr.addstr(row_y, x + 1, line_str[:w - 2], attr)

    def draw_ungrouped_column(self, stdscr, y: int, x: int, h: int, w: int):
        is_active = (self.active_col == 2)
        border_attr = curses.color_pair(3) | curses.A_BOLD if is_active else curses.color_pair(1) | curses.A_DIM

        filtered = self.get_filtered_ungrouped()
        marked_badge = f" [{len(self.selected_ungrouped)} marked]" if self.selected_ungrouped else ""
        filter_badge = f" [/{self.ungrouped_search}]" if self.ungrouped_search else ""
        title = f" 3. UNGROUPED QUEUE ({len(filtered)}){marked_badge}{filter_badge} "
        self._draw_box(stdscr, y, x, h, w, title, border_attr)

        max_items = h - 2
        scroll_start = max(0, self.ungrouped_idx - (max_items // 2))

        for idx, u in enumerate(filtered[scroll_start:scroll_start + max_items]):
            actual_idx = scroll_start + idx
            is_selected = (actual_idx == self.ungrouped_idx)
            is_marked = (u.pr_id in self.selected_ungrouped)
            row_y = y + 1 + idx

            pkg_name = u.host_package or u.title.replace("Forwarded PRs: ", "").strip()
            pr_token_num = f"!{u.tokens[0].pr_number}" if u.tokens else ""

            cursor_prefix = "▸ " if is_selected else "  "
            check_box = "[✓] " if is_marked else "[ ] "
            item_str = f"{cursor_prefix}{check_box}#{u.pr_id} [{u.branch}] {pkg_name} {pr_token_num}"
            line_str = f"{item_str:<{w - 4}}"

            if is_selected:
                attr = curses.color_pair(6 if is_active else 7) | curses.A_BOLD
            elif is_marked:
                attr = curses.color_pair(2) | curses.A_BOLD
            else:
                attr = curses.color_pair(1)

            stdscr.addstr(row_y, x + 1, line_str[:w - 2], attr)

    def _draw_box(self, stdscr, y: int, x: int, h: int, w: int, title: str, attr: int):
        stdscr.addstr(y, x, "┌" + "─" * (w - 2) + "┐", attr)
        stdscr.addstr(y + h - 1, x, "└" + "─" * (w - 2) + "┘", attr)

        for row in range(y + 1, y + h - 1):
            stdscr.addstr(row, x, "│", attr)
            stdscr.addstr(row, x + w - 1, "│", attr)

        if title and len(title) < w - 2:
            stdscr.addstr(y, x + 2, title, attr | curses.A_BOLD)


    def shutdown(self):
        """Shuts down background executor immediately upon exit."""
        try:
            self.obs_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass


def run_tui(client: Optional[pm.GiteaClient] = None):
    app = StagingTUI(service=ss.StagingService(client=client))
    try:
        curses.wrapper(app.run)
    finally:
        app.shutdown()


if __name__ == "__main__":
    run_tui()
