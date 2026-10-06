#!/usr/bin/env python3
"""
openSUSE Staging Group PR Manager - Graphical User Interface (GUI).

A modern GTK4 / Libadwaita desktop application for managing forwarded PRs on _ObsPrj / Factory:
- Split layout: Groups Sidebar & Dual-Column Staging Workspace
- Real-time OBS staging build status indicators with failure guards
- Interactive Diff & Changelog Viewer using GtkSourceView 5
- Multi-workspace switcher with dynamic permission badges
- High-throughput batch Add, Remove, Move, and Combine operations
- Out-of-Sync Orphan PR detection and 1-click remediation
"""

import sys
import os
import subprocess
from pathlib import Path
from typing import Optional, List, Dict, Set, Tuple
from concurrent.futures import ThreadPoolExecutor

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
try:
    gi.require_version("GtkSource", "5")
    from gi.repository import GtkSource
    HAS_GTKSOURCE = True
except Exception:
    HAS_GTKSOURCE = False

from gi.repository import Gtk, Adw, GLib, Gio, Gdk

import pr_manage as pm
import staging_service as ss


def apply_source_view_style_scheme(buffer):
    """Dynamically applies GtkSourceView style schemes based on system dark/light preference."""
    if not HAS_GTKSOURCE or not buffer or not hasattr(buffer, "set_style_scheme"):
        return
    style_manager = Adw.StyleManager.get_default()
    is_dark = style_manager.get_dark()

    scheme_manager = GtkSource.StyleSchemeManager.get_default()
    schemes_to_try = ("Adwaita-dark", "classic-dark", "oblivion") if is_dark else ("Adwaita", "classic", "tango")
    for sid in schemes_to_try:
        scheme = scheme_manager.get_scheme(sid)
        if scheme:
            buffer.set_style_scheme(scheme)
            break


CSS_STYLING = """
row.obs-succeeded {
    background-color: alpha(@success_color, 0.16);
    border-left: 5px solid @success_color;
}
row.obs-succeeded:hover {
    background-color: alpha(@success_color, 0.24);
}
row.obs-succeeded:selected {
    background-color: alpha(@success_color, 0.32);
}

row.obs-building {
    background-color: alpha(@accent_color, 0.16);
    border-left: 5px solid @accent_color;
}
row.obs-building:hover {
    background-color: alpha(@accent_color, 0.24);
}
row.obs-building:selected {
    background-color: alpha(@accent_color, 0.32);
}

row.obs-failed {
    background-color: alpha(@destructive_color, 0.16);
    border-left: 5px solid @destructive_color;
}
row.obs-failed:hover {
    background-color: alpha(@destructive_color, 0.24);
}
row.obs-failed:selected {
    background-color: alpha(@destructive_color, 0.32);
}

row.obs-succeeded, row.obs-building, row.obs-failed {
    transition: background-color 200ms ease-in-out, border-color 200ms ease-in-out;
}
"""

def init_gui_styling():
    try:
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS_STYLING.encode("utf-8"))
        display = Gdk.Display.get_default()
        if display:
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
    except Exception:
        pass


class DiffViewerWindow(Adw.Window):
    """Interactive modal pager displaying unified git diff with GtkSourceView syntax highlighting."""

    def __init__(self, parent_window, owner: str, package: str, pr_num: int, diff_text: str):
        super().__init__(transient_for=parent_window, modal=True)
        self.set_title(f"Diff: {owner}/{package}!{pr_num}")
        self.set_default_size(900, 680)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(main_box)

        # HeaderBar
        header = Adw.HeaderBar()
        main_box.append(header)

        btn_copy = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy Unified Diff to Clipboard")
        btn_copy.connect("clicked", lambda b: self.copy_diff_to_clipboard(diff_text))
        header.pack_end(btn_copy)

        # Scrolled text / source view
        scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        main_box.append(scroller)

        if HAS_GTKSOURCE:
            self.source_buffer = GtkSource.Buffer()
            lm = GtkSource.LanguageManager.get_default()
            lang = lm.get_language("diff")
            if lang:
                self.source_buffer.set_language(lang)
            self.source_buffer.set_text(diff_text)

            # Synchronize GtkSourceView theme with system dark/light mode preference
            apply_source_view_style_scheme(self.source_buffer)
            self.style_manager = Adw.StyleManager.get_default()
            self.theme_handler_id = self.style_manager.connect(
                "notify::dark", lambda sm, pspec: apply_source_view_style_scheme(self.source_buffer)
            )

            source_view = GtkSource.View(
                buffer=self.source_buffer,
                show_line_numbers=True,
                monospace=True,
                editable=False,
                cursor_visible=False
            )
            scroller.set_child(source_view)

            def on_close_request(win):
                if hasattr(self, "theme_handler_id") and hasattr(self, "style_manager"):
                    try:
                        self.style_manager.disconnect(self.theme_handler_id)
                    except Exception:
                        pass
                return False

            self.connect("close-request", on_close_request)
        else:
            text_view = Gtk.TextView(monospace=True, editable=False, cursor_visible=False)
            text_view.get_buffer().set_text(diff_text)
            scroller.set_child(text_view)

    def copy_diff_to_clipboard(self, diff_text: str):
        clipboard = Gdk.Display.get_default().get_clipboard()
        clipboard.set(diff_text)


class OrphanAuditDialog(Adw.Window):
    """Interactive modal window for triaging out-of-sync / orphan PRs."""

    def __init__(self, parent_window, service: ss.StagingService, target_pr_id: Optional[int], on_refresh_callback):
        super().__init__(transient_for=parent_window, modal=True)
        self.service = service
        self.target_pr_id = target_pr_id
        self.on_refresh = on_refresh_callback
        self.set_title("Out-of-Sync Package PRs Audit")
        self.set_default_size(840, 560)

        self.selected_orphans: Set[int] = set() # indexes
        self.orphans: List[ss.OrphanPackagePR] = []

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        main_box.set_margin_top(12)
        main_box.set_margin_bottom(12)
        main_box.set_margin_start(16)
        main_box.set_margin_end(16)
        self.set_content(main_box)

        # HeaderBar
        header = Adw.HeaderBar()
        main_box.prepend(header)

        # Action Buttons on bottom
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        btn_box.set_halign(Gtk.Align.END)
        main_box.append(btn_box)

        self.btn_reopen = Gtk.Button(label="Reopen Child PRs", css_classes=["suggested-action"])
        self.btn_reopen.connect("clicked", self.on_reopen_clicked)
        btn_box.append(self.btn_reopen)

        self.btn_adopt = Gtk.Button(label=f"Adopt into #{target_pr_id}" if target_pr_id else "Adopt into Group")
        self.btn_adopt.set_sensitive(bool(target_pr_id))
        self.btn_adopt.connect("clicked", self.on_adopt_clicked)
        btn_box.append(self.btn_adopt)

        btn_close = Gtk.Button(label="Close")
        btn_close.connect("clicked", lambda b: self.close())
        btn_box.append(btn_close)

        # Scrolled List
        self.scroller = Gtk.ScrolledWindow(vexpand=True)
        self.list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list_box.add_css_class("boxed-list")
        self.scroller.set_child(self.list_box)
        main_box.append(self.scroller)

        self.load_orphans()

    def load_orphans(self):
        def worker():
            orphans = self.service.audit_orphans()
            GLib.idle_add(self.populate_orphans, orphans)

        ThreadPoolExecutor(max_workers=1).submit(worker)

    def populate_orphans(self, orphans: List[ss.OrphanPackagePR]):
        self.orphans = orphans
        # Clear list box
        while True:
            row = self.list_box.get_row_at_index(0)
            if not row:
                break
            self.list_box.remove(row)

        if not orphans:
            status = Adw.StatusPage(
                icon_name="emblem-ok-symbolic",
                title="All Package PRs Tracked",
                description="Zero out-of-sync PRs found! Every open package PR has an active forward PR in staging."
            )
            self.scroller.set_child(status)
            self.btn_reopen.set_sensitive(False)
            self.btn_adopt.set_sensitive(False)
            return

        for idx, o in enumerate(orphans):
            row = Adw.ActionRow()
            fwd_txt = f"Fwd: #{o.forward_pr_id} ({o.forward_pr_state})" if o.forward_pr_id else "No forward PR"
            row.set_title(f"{o.package} !{o.pr_number}  ➔  [{o.base_branch}]")
            row.set_subtitle(f"Author: {o.author}  •  {fwd_txt}  •  {o.title}")

            chk = Gtk.CheckButton()
            chk.connect("toggled", self.on_chk_toggled, idx)
            row.add_prefix(chk)

            self.list_box.append(row)

    def on_chk_toggled(self, chk, idx: int):
        if chk.get_active():
            self.selected_orphans.add(idx)
        else:
            self.selected_orphans.discard(idx)

    def on_reopen_clicked(self, btn):
        targets = [self.orphans[i] for i in (self.selected_orphans if self.selected_orphans else range(len(self.orphans)))]
        for o in targets:
            self.service.reopen_orphan_forward_pr(o)
        self.on_refresh()
        self.close()

    def on_adopt_clicked(self, btn):
        if not self.target_pr_id:
            return
        targets = [self.orphans[i] for i in (self.selected_orphans if self.selected_orphans else range(len(self.orphans)))]
        for o in targets:
            self.service.adopt_orphan_into_group(self.target_pr_id, o)
        self.on_refresh()
        self.close()


class StagingGuiWindow(Adw.ApplicationWindow):
    """Primary desktop workspace window for the Staging Group Manager."""

    def __init__(self, app, service: ss.StagingService):
        super().__init__(application=app, title="openSUSE Staging PR Manager")
        self.service = service
        self.set_default_size(1220, 800)

        init_gui_styling()

        # State
        self.groups: List[ss.StagingGroup] = []
        self.ungrouped: List[ss.StagingGroup] = []
        self.selected_group: Optional[ss.StagingGroup] = None
        self.selected_groups_marked: Set[int] = set()
        self.selected_members_marked: Set[str] = set()
        self.selected_ungrouped_marked: Set[int] = set()
        self.obs_status_cache: Dict[int, Dict] = {}
        self.pending_obs_queries: Set[int] = set()
        self.group_rows: Dict[int, Adw.ActionRow] = {}
        self.executor = ThreadPoolExecutor(max_workers=5)

        # Toast Overlay & Outer layout
        self.toast_overlay = Adw.ToastOverlay()
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.toast_overlay.set_child(main_box)
        self.set_content(self.toast_overlay)

        # HeaderBar
        self.header = Adw.HeaderBar()
        main_box.append(self.header)

        # Workspace Menu Button
        self.btn_workspace = Gtk.MenuButton()
        self.update_workspace_menu()
        self.header.pack_start(self.btn_workspace)

        # Branch Dropdown
        self.drop_branch = Gtk.DropDown.new_from_strings(["all", "factory", "next"])
        self.drop_branch.connect("notify::selected", lambda d, p: self.refresh_data())
        self.header.pack_start(self.drop_branch)

        # Orphan Audit Button
        self.btn_audit = Gtk.Button(label="⚠️ Audit Orphans", tooltip_text="Scan for package PRs without active staging forward PRs")
        self.btn_audit.connect("clicked", self.on_audit_clicked)
        self.header.pack_end(self.btn_audit)

        # Refresh Button with Spinner
        self.btn_refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh from Gitea and OBS")
        self.btn_refresh.connect("clicked", lambda b: self.refresh_data())
        self.header.pack_end(self.btn_refresh)

        self.spinner = Gtk.Spinner()
        self.header.pack_end(self.spinner)

        # Read-Only Banner
        self.banner = Adw.Banner(button_label="Dismiss")
        self.banner.connect("button-clicked", lambda b: self.banner.set_revealed(False))
        main_box.append(self.banner)

        # Main Paned (Sidebar + Workspace)
        self.paned_main = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned_main.set_position(380)
        main_box.append(self.paned_main)

        # Left Panel (Sidebar: Groups & Forwards)
        self.build_sidebar()

        # Right Panel (Work Area)
        self.build_work_area()

        # Initial Load
        self.refresh_data()

    def update_workspace_menu(self):
        """Constructs workspace dropdown menu showing permissions and active repo."""
        repo = self.service.repo
        perm_lbl = "Admin" if self.service.client.has_admin_access else ("Maintainer" if self.service.client.has_push_access else "Read-Only")
        self.btn_workspace.set_label(f"{repo} [{perm_lbl}] ▾")

        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        popover.set_child(box)

        lbl = Gtk.Label(label="Switch Staging Workspace", css_classes=["heading"])
        box.append(lbl)

        # Populate discovered workspaces
        def populate_ws(workspaces):
            for name, w_lbl, _ in workspaces:
                btn = Gtk.Button(label=f"{name} [{w_lbl}]")
                btn.set_has_frame(False)
                btn.connect("clicked", self.on_switch_workspace_clicked, name, popover)
                box.append(btn)

            btn_custom = Gtk.Button(label="+ Enter Custom Repository...")
            btn_custom.set_has_frame(False)
            btn_custom.connect("clicked", self.on_custom_workspace_clicked, popover)
            box.append(btn_custom)

        self.executor.submit(lambda: GLib.idle_add(populate_ws, self.service.discover_valid_workspaces()))
        self.btn_workspace.set_popover(popover)

    def on_switch_workspace_clicked(self, btn, repo_name: str, popover):
        popover.popdown()
        if repo_name != self.service.repo:
            self.service.client.set_repo(repo_name, persist=True)
            self.service.record_recent_workspace(repo_name)
            self.update_workspace_menu()
            self.refresh_data()

    def on_custom_workspace_clicked(self, btn, popover):
        popover.popdown()
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Switch Repository",
            body="Enter the Gitea repository slug (e.g. 'openSUSE/Factory' or 'filesystems/_ObsPrj'):"
        )
        entry = Gtk.Entry()
        entry.set_placeholder_text("owner/repo")
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("switch", "Switch")
        dialog.set_response_appearance("switch", Adw.ResponseAppearance.SUGGESTED)

        def on_response(d, resp):
            if resp == "switch":
                val = entry.get_text().strip()
                if val:
                    info = self.service.client.get_repo_info(val)
                    if not info:
                        self.show_toast(f"Repository '{val}' does not exist on Gitea.")
                        return
                    self.on_switch_workspace_clicked(None, val, popover)

        dialog.connect("response", on_response)
        dialog.present()

    def build_sidebar(self):
        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sidebar_box.set_margin_top(8)
        sidebar_box.set_margin_bottom(8)
        sidebar_box.set_margin_start(8)
        sidebar_box.set_margin_end(8)
        self.paned_main.set_start_child(sidebar_box)

        # Header controls inside sidebar
        self.search_groups = Gtk.SearchEntry(hexpand=True)
        self.search_groups.connect("search-changed", lambda s: self.populate_groups_list())
        sidebar_box.append(self.search_groups)

        # Scrolled List Box
        scroller = Gtk.ScrolledWindow(vexpand=True)
        self.list_groups = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.list_groups.add_css_class("navigation-sidebar")
        self.list_groups.connect("row-selected", self.on_group_row_selected)
        scroller.set_child(self.list_groups)
        sidebar_box.append(scroller)

        # Dedicated Batch Action Bar below sidebar
        self.box_sidebar_batch = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.box_sidebar_batch.set_visible(False)
        sidebar_box.append(self.box_sidebar_batch)

        self.btn_combine_marked = Gtk.Button(label="⎘ Combine", hexpand=True, tooltip_text="Batch combine marked groups")
        self.btn_combine_marked.connect("clicked", self.on_batch_combine_clicked)
        self.box_sidebar_batch.append(self.btn_combine_marked)

        self.btn_accept_marked = Gtk.Button(label="✓ Accept", hexpand=True, css_classes=["suggested-action"], tooltip_text="Batch accept marked PRs with 'merge ok'")
        self.btn_accept_marked.connect("clicked", self.on_batch_accept_clicked)
        self.box_sidebar_batch.append(self.btn_accept_marked)

        self.btn_clear_marked = Gtk.Button(icon_name="edit-clear-symbolic", tooltip_text="Deselect all marked groups")
        self.btn_clear_marked.connect("clicked", lambda b: self.clear_groups_marked())
        self.box_sidebar_batch.append(self.btn_clear_marked)

    def clear_groups_marked(self):
        self.selected_groups_marked.clear()
        self.update_sidebar_batch_bar()
        self.populate_groups_list()

    def update_sidebar_batch_bar(self):
        cnt = len(self.selected_groups_marked)
        is_ro = self.service.client.is_read_only
        if cnt > 0:
            self.box_sidebar_batch.set_visible(True)
            self.btn_combine_marked.set_label(f"⎘ Combine ({cnt})")
            self.btn_combine_marked.set_sensitive(cnt >= 2 and not is_ro)
            self.btn_accept_marked.set_label(f"✓ Accept ({cnt})")
            self.btn_accept_marked.set_sensitive(not is_ro)
        else:
            self.box_sidebar_batch.set_visible(False)

    def build_work_area(self):
        self.work_area_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.work_area_box.set_margin_top(8)
        self.work_area_box.set_margin_bottom(8)
        self.work_area_box.set_margin_start(8)
        self.work_area_box.set_margin_end(8)
        self.paned_main.set_end_child(self.work_area_box)

        # Detail Header Card (Title, Badges, OBS status, Actions)
        self.card_header = Adw.PreferencesGroup()
        self.work_area_box.append(self.card_header)

        self.row_title = Adw.ActionRow(title="Select a group to manage")
        self.card_header.add(self.row_title)

        # Group Action Buttons in top card
        self.btn_accept = Gtk.Button(label="✓ Accept (merge ok)", css_classes=["suggested-action"])
        self.btn_accept.connect("clicked", self.on_accept_clicked)
        self.row_title.add_suffix(self.btn_accept)

        self.btn_rename = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text="Rename PR Title")
        self.btn_rename.connect("clicked", self.on_rename_clicked)
        self.row_title.add_suffix(self.btn_rename)

        self.btn_disintegrate = Gtk.Button(label="Disintegrate", css_classes=["destructive-action"])
        self.btn_disintegrate.connect("clicked", self.on_disintegrate_clicked)
        self.row_title.add_suffix(self.btn_disintegrate)

        self.btn_browser = Gtk.Button(icon_name="web-browser-symbolic", tooltip_text="Open on Gitea")
        self.btn_browser.connect("clicked", self.on_open_browser_clicked)
        self.row_title.add_suffix(self.btn_browser)

        # Dual Work Paned (Members vs Ungrouped)
        self.paned_columns = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned_columns.set_position(440)
        self.paned_columns.set_vexpand(True)
        self.work_area_box.append(self.paned_columns)

        # Column 2: Group Members
        self.build_members_subpanel()

        # Column 3: Ungrouped Queue
        self.build_ungrouped_subpanel()

    def build_members_subpanel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_end(6)
        self.paned_columns.set_start_child(box)

        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.append(header_box)

        self.lbl_members_title = Gtk.Label(label="Group Members (0)", css_classes=["heading"])
        header_box.append(self.lbl_members_title)

        self.btn_remove_marked = Gtk.Button(label="Remove Marked", css_classes=["destructive-action"])
        self.btn_remove_marked.connect("clicked", self.on_remove_marked_clicked)
        header_box.append(self.btn_remove_marked)

        self.search_members = Gtk.SearchEntry(hexpand=True)
        self.search_members.connect("search-changed", lambda s: self.populate_members_list())
        box.append(self.search_members)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        self.list_members = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list_members.add_css_class("boxed-list")
        scroller.set_child(self.list_members)
        box.append(scroller)

    def build_ungrouped_subpanel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(6)
        self.paned_columns.set_end_child(box)

        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.append(header_box)

        self.lbl_ungrouped_title = Gtk.Label(label="Ungrouped Queue (0)", css_classes=["heading"])
        header_box.append(self.lbl_ungrouped_title)

        self.btn_add_marked = Gtk.Button(label="Add Marked", css_classes=["suggested-action"])
        self.btn_add_marked.connect("clicked", self.on_add_marked_clicked)
        header_box.append(self.btn_add_marked)

        self.search_ungrouped = Gtk.SearchEntry(hexpand=True)
        self.search_ungrouped.connect("search-changed", lambda s: self.populate_ungrouped_list())
        box.append(self.search_ungrouped)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        self.list_ungrouped = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list_ungrouped.add_css_class("boxed-list")
        scroller.set_child(self.list_ungrouped)
        box.append(scroller)

    def refresh_data(self):
        """Asynchronously refreshes staging queues and groups from Gitea."""
        self.obs_status_cache.clear()
        self.pending_obs_queries.clear()
        self.spinner.start()
        self.btn_refresh.set_sensitive(False)

        branch_val = self.drop_branch.get_selected_item().get_string()
        branch_filter = None if branch_val == "all" else branch_val

        # Update Read-Only banner
        if self.service.client.is_read_only:
            self.banner.set_title(f"Read-Only Mode: You have read access on '{self.service.repo}'. Modifications disabled.")
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)

        def worker():
            try:
                groups, ungrouped = self.service.fetch_all(filter_branch=branch_filter)
                GLib.idle_add(self.on_data_loaded, groups, ungrouped)
            except Exception as e:
                GLib.idle_add(self.on_load_error, str(e))

        self.executor.submit(worker)

    def on_data_loaded(self, groups: List[ss.StagingGroup], ungrouped: List[ss.StagingGroup]):
        self.groups = groups
        self.ungrouped = ungrouped
        self.spinner.stop()
        self.btn_refresh.set_sensitive(True)

        self.populate_groups_list()
        self.populate_ungrouped_list()

        # Restore or update selection
        if self.selected_group:
            matching = next((g for g in self.groups if g.pr_id == self.selected_group.pr_id), None)
            self.select_group(matching or (self.groups[0] if self.groups else None))
        elif self.groups:
            self.select_group(self.groups[0])
        else:
            self.select_group(None)

    def on_load_error(self, err_msg: str):
        self.spinner.stop()
        self.btn_refresh.set_sensitive(True)
        toast = Adw.Toast(title=f"Error loading PRs: {err_msg}")
        self.show_toast(f"Error loading PRs: {err_msg}")

    def populate_groups_list(self):
        self.group_rows.clear()
        # Clear list box
        while True:
            row = self.list_groups.get_row_at_index(0)
            if not row:
                break
            self.list_groups.remove(row)

        filter_q = self.search_groups.get_text().lower()

        for g in self.groups:
            if filter_q and (filter_q not in g.title.lower() and filter_q not in str(g.pr_id) and (not g.host_package or filter_q not in g.host_package.lower())):
                continue

            row = Adw.ActionRow()
            chk = Gtk.CheckButton()
            chk.set_active(g.pr_id in self.selected_groups_marked)
            def on_chk_toggle(c, gid=g.pr_id):
                if c.get_active():
                    self.selected_groups_marked.add(gid)
                else:
                    self.selected_groups_marked.discard(gid)
                self.update_sidebar_batch_bar()
            chk.connect("toggled", on_chk_toggle)
            row.add_prefix(chk)

            conflict_tag = "⚠️ " if g.mergeable is False else ""
            icon = "📦" if g.is_group else "📄"
            title_txt = f"{conflict_tag}{icon} #{g.pr_id}  [{g.branch}]  ({g.member_count}p)"
            row.set_title(title_txt)
            row.set_subtitle(g.title)
            row.pr_obj = g

            # Status pill on row suffix
            badge = Gtk.Label(label="⚪ Standby", css_classes=["dim-label", "caption"])
            badge.set_valign(Gtk.Align.CENTER)
            row.add_suffix(badge)
            row.obs_badge = badge

            self.group_rows[g.pr_id] = row
            self.list_groups.append(row)

            # Apply cached OBS status if already available
            if g.pr_id in self.obs_status_cache:
                self.apply_row_obs_styling(g.pr_id, self.obs_status_cache[g.pr_id])

        self.trigger_sidebar_obs_queries()

    def trigger_sidebar_obs_queries(self):
        """Asynchronously queries OBS build status for groups in sidebar."""
        for g in self.groups:
            if g.pr_id in self.obs_status_cache:
                self.apply_row_obs_styling(g.pr_id, self.obs_status_cache[g.pr_id])
                continue

            if g.pr_id in self.pending_obs_queries:
                continue

            self.pending_obs_queries.add(g.pr_id)

            def worker(gid=g.pr_id, branch=g.branch):
                try:
                    obs = self.service.get_obs_build_status(gid, branch)
                except Exception as e:
                    obs = {"status": "none", "error": str(e)}
                self.obs_status_cache[gid] = obs
                self.pending_obs_queries.discard(gid)
                GLib.idle_add(self.apply_row_obs_styling, gid, obs)

            self.executor.submit(worker)

    def apply_row_obs_styling(self, pr_id: int, obs: Dict):
        """Applies green, light blue, or red styling to sidebar row based on OBS build results."""
        row = self.group_rows.get(pr_id)
        if not row:
            return

        row.remove_css_class("obs-succeeded")
        row.remove_css_class("obs-building")
        row.remove_css_class("obs-failed")

        st = obs.get("status", "none")
        if st == "succeeded":
            row.add_css_class("obs-succeeded")
            pill_text = f"🟢 Built ({obs.get('total', 0)}p)"
        elif st == "building":
            row.add_css_class("obs-building")
            pill_text = f"🔵 Building ({obs.get('succeeded', 0)}/{obs.get('total', 0)}p)"
        elif st == "failed":
            row.add_css_class("obs-failed")
            failed_archs = obs.get("failed_archs", [])
            arch_str = f" [{','.join(failed_archs)}]" if failed_archs else ""
            pill_text = f"🔴 Failed ({len(obs.get('failed_pkgs', []))}p{arch_str})"
        else:
            pill_text = "⚪ Standby"

        if hasattr(row, "obs_badge"):
            row.obs_badge.set_label(pill_text)

    def on_group_row_selected(self, listbox, row):
        if row and hasattr(row, "pr_obj"):
            self.select_group(row.pr_obj)

    def select_group(self, group: Optional[ss.StagingGroup]):
        self.selected_group = group
        self.selected_members_marked.clear()

        if not group:
            self.row_title.set_title("No group selected")
            self.row_title.set_subtitle("")
            self.btn_accept.set_sensitive(False)
            self.btn_rename.set_sensitive(False)
            self.btn_disintegrate.set_sensitive(False)
            self.btn_browser.set_sensitive(False)
            self.populate_members_list()
            return

        is_ro = self.service.client.is_read_only
        is_conflicted = (group.mergeable is False)
        self.btn_accept.set_label(f"✓ Accept #{group.pr_id}")
        if is_conflicted:
            self.btn_accept.set_tooltip_text(f"Blocked: PR #{group.pr_id} has Git merge conflicts with '{group.branch}'")
            self.btn_accept.set_sensitive(False)
        else:
            self.btn_accept.set_tooltip_text(f"Submit 'merge ok' for active PR #{group.pr_id} ('{group.title}')")
            self.btn_accept.set_sensitive(not is_ro)
        self.btn_rename.set_sensitive(not is_ro)
        self.btn_disintegrate.set_sensitive(not is_ro and group.is_group)
        self.btn_browser.set_sensitive(True)

        host_str = f"Host: ★ {group.host_package}" if group.host_package else ""
        self.row_title.set_title(f"#{group.pr_id}  [{group.branch}]  {group.title}")
        self.row_title.set_subtitle(f"{host_str}  •  {group.member_count} package(s) tracked")

        # Async query OBS status (or use cache)
        if group.pr_id in self.obs_status_cache:
            self.update_obs_badge(group.pr_id, self.obs_status_cache[group.pr_id])
        else:
            self.row_title.set_subtitle(f"{host_str}  •  OBS: ⚡ Checking build status...")
            def query_obs():
                obs = self.service.get_obs_build_status(group.pr_id, group.branch)
                self.obs_status_cache[group.pr_id] = obs
                GLib.idle_add(self.update_obs_badge, group.pr_id, obs)
                GLib.idle_add(self.apply_row_obs_styling, group.pr_id, obs)

            self.executor.submit(query_obs)
        self.populate_members_list()

    def update_obs_badge(self, pr_id: int, obs: Dict):
        if not self.selected_group or self.selected_group.pr_id != pr_id:
            return
        host_str = f"Host: ★ {self.selected_group.host_package}" if self.selected_group.host_package else ""
        st = obs.get("status", "none")
        if st == "failed":
            obs_txt = f"OBS: 🔴 Failed ({len(obs.get('failed_pkgs', []))} pkgs)"
        elif st == "building":
            obs_txt = f"OBS: 🟡 Building ({obs.get('succeeded', 0)}/{obs.get('total', 0)})"
        elif st == "succeeded":
            obs_txt = f"OBS: 🟢 Built ({obs.get('total', 0)} pkgs)"
        else:
            obs_txt = "OBS: ⚪ Standby"

        self.row_title.set_subtitle(f"{host_str}  •  {obs_txt}  •  {self.selected_group.member_count} pkgs")

    def populate_members_list(self):
        while True:
            row = self.list_members.get_row_at_index(0)
            if not row:
                break
            self.list_members.remove(row)

        if not self.selected_group:
            self.lbl_members_title.set_label("Group Members (0)")
            return

        members = sorted(self.selected_group.tokens, key=lambda t: t.package.lower())
        self.lbl_members_title.set_label(f"Group Members ({len(members)})")
        filter_q = self.search_members.get_text().lower()

        is_ro = self.service.client.is_read_only

        for tok in members:
            if filter_q and (filter_q not in tok.package.lower() and filter_q not in str(tok.pr_number)):
                continue

            is_host = bool(self.selected_group.host_package and tok.key == self.selected_group.host_package.lower())
            sub_txt = f"Host Package ★  •  Upstream PR: !{tok.pr_number}" if is_host else f"Upstream PR: !{tok.pr_number}"
            row = Adw.ActionRow(title=tok.package, subtitle=sub_txt)

            # Checkbox: disabled for host package
            chk = Gtk.CheckButton()
            if is_host:
                chk.set_sensitive(False)
                chk.set_tooltip_text("Host package cannot be removed from group")
            else:
                chk.set_active(tok.key in self.selected_members_marked)
                chk.connect("toggled", lambda c, k=tok.key: self.selected_members_marked.add(k) if c.get_active() else self.selected_members_marked.discard(k))
            row.add_prefix(chk)

            # Per-row actions: Diff
            btn_diff = Gtk.Button(icon_name="text-x-generic-symbolic", tooltip_text="Inspect Changelog Diff")
            btn_diff.connect("clicked", lambda b, o=tok.owner, p=tok.package, n=tok.pr_number: self.open_diff_viewer(o, p, n))
            row.add_suffix(btn_diff)

            # Only peer members get a remove 'x' button (host cannot be removed)
            if not is_host:
                btn_remove = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Remove from Group", css_classes=["flat"])
                btn_remove.set_sensitive(not is_ro)
                btn_remove.connect("clicked", lambda b, p=tok.package: self.on_remove_single_member(p))
                row.add_suffix(btn_remove)
            else:
                badge_host = Gtk.Label(label="★ Host", css_classes=["dim-label", "caption"])
                badge_host.set_valign(Gtk.Align.CENTER)
                row.add_suffix(badge_host)

            self.list_members.append(row)

    def populate_ungrouped_list(self):
        while True:
            row = self.list_ungrouped.get_row_at_index(0)
            if not row:
                break
            self.list_ungrouped.remove(row)

        filter_q = self.search_ungrouped.get_text().lower()
        visible_ungrouped = [
            u for u in self.ungrouped
            if not self.selected_group or u.pr_id != self.selected_group.pr_id
        ]

        self.lbl_ungrouped_title.set_label(f"Ungrouped Queue ({len(visible_ungrouped)})")
        is_ro = self.service.client.is_read_only

        for u in visible_ungrouped:
            pkg_name = u.host_package or u.title.replace("Forwarded PRs: ", "").strip()
            pr_token = f"!{u.tokens[0].pr_number}" if u.tokens else ""

            if filter_q and (filter_q not in pkg_name.lower() and filter_q not in str(u.pr_id)):
                continue

            row = Adw.ActionRow(title=f"#{u.pr_id}  [{u.branch}]  {pkg_name}", subtitle=f"Upstream PR: {pr_token}")

            chk = Gtk.CheckButton()
            chk.set_active(u.pr_id in self.selected_ungrouped_marked)
            chk.connect("toggled", lambda c, uid=u.pr_id: self.selected_ungrouped_marked.add(uid) if c.get_active() else self.selected_ungrouped_marked.discard(uid))
            row.add_prefix(chk)

            # Per-row actions: Diff, Add
            owner = u.tokens[0].owner if u.tokens else self.service.owner
            pr_num = u.tokens[0].pr_number if u.tokens else u.pr_id

            btn_diff = Gtk.Button(icon_name="text-x-generic-symbolic", tooltip_text="Inspect Changelog Diff")
            btn_diff.connect("clicked", lambda b, o=owner, p=pkg_name, n=pr_num: self.open_diff_viewer(o, p, n))
            row.add_suffix(btn_diff)

            btn_add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add into Active Group", css_classes=["suggested-action"])
            btn_add.set_sensitive(not is_ro and bool(self.selected_group))
            btn_add.connect("clicked", lambda b, p=pkg_name: self.on_add_single_package(p))
            row.add_suffix(btn_add)

            self.list_ungrouped.append(row)

    def open_diff_viewer(self, owner: str, package: str, pr_num: int):
        def worker():
            diff = self.service.get_pr_diff(owner, package, pr_num)
            GLib.idle_add(lambda: DiffViewerWindow(self, owner, package, pr_num, diff).present())
        self.executor.submit(worker)

    def on_add_single_package(self, package_name: str):
        if not self.selected_group:
            return
        res = self.service.add_package_to_group(self.selected_group.pr_id, package_name)
        self.show_operation_result(res)
        if res.success:
            self.refresh_data()

    def on_add_marked_clicked(self, btn):
        if not self.selected_group or not self.selected_ungrouped_marked:
            return
        id_map = {u.pr_id: (u.host_package or u.title.replace("Forwarded PRs: ", "").strip()) for u in self.ungrouped}
        pkgs = [id_map[uid] for uid in self.selected_ungrouped_marked if uid in id_map]
        res = self.service.add_packages_to_group(self.selected_group.pr_id, pkgs)
        self.show_operation_result(res)
        self.selected_ungrouped_marked.clear()
        if res.success:
            self.refresh_data()

    def on_remove_single_member(self, package_name: str):
        if not self.selected_group:
            return
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=f"Remove {package_name}?",
            body=f"Remove '{package_name}' from group #{self.selected_group.pr_id}? This will reopen its original forwarded pull request."
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(d, resp):
            if resp == "remove":
                res = self.service.remove_package_from_group(self.selected_group.pr_id, package_name)
                self.show_operation_result(res)
                if res.success:
                    self.refresh_data()

        dialog.connect("response", on_response)
        dialog.present()

    def on_remove_marked_clicked(self, btn):
        if not self.selected_group or not self.selected_members_marked:
            return
        pkgs = list(self.selected_members_marked)
        sample = ", ".join(pkgs[:3]) + (f" and {len(pkgs) - 3} more" if len(pkgs) > 3 else "")
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=f"Remove {len(pkgs)} package(s)?",
            body=f"Remove {len(pkgs)} marked package(s) ({sample}) from group #{self.selected_group.pr_id}? Their original forwarded pull requests will be reopened."
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", f"Remove ({len(pkgs)})")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(d, resp):
            if resp == "remove":
                res = self.service.remove_packages_from_group(self.selected_group.pr_id, pkgs)
                self.show_operation_result(res)
                self.selected_members_marked.clear()
                if res.success:
                    self.refresh_data()

        dialog.connect("response", on_response)
        dialog.present()

    def on_batch_combine_clicked(self, btn):
        if len(self.selected_groups_marked) < 2:
            self.show_toast("Select at least 2 groups with checkboxes to batch combine.")
            return

        marked_groups = [g for g in self.groups if g.pr_id in self.selected_groups_marked]
        branches = {g.branch for g in marked_groups}
        if len(branches) > 1:
            self.show_toast(f"Branch mismatch: cannot combine across branches ({', '.join(branches)})")
            return

        # Smart Anchor Heuristic:
        # Priority 1: Currently active group (if marked)
        # Priority 2: Multi-package group / highest member_count
        # Priority 3: Newest PR ID
        def anchor_score(g: ss.StagingGroup):
            is_active = bool(self.selected_group and g.pr_id == self.selected_group.pr_id)
            return (1 if is_active else 0, g.member_count, g.pr_id)

        marked_groups.sort(key=anchor_score, reverse=True)

        def format_anchor_label(g: ss.StagingGroup):
            active_tag = " ★ Active" if (self.selected_group and g.pr_id == self.selected_group.pr_id) else ""
            type_tag = f"Group ({g.member_count}p)" if g.is_group else "Single PR"
            return f"#{g.pr_id} [{g.branch}] {type_tag}{active_tag} - {g.title[:35]}"

        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Select Target (Anchor) Group",
            body="Choose which PR will stay open and absorb the other selected PRs:"
        )

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        drop = Gtk.DropDown.new_from_strings([format_anchor_label(g) for g in marked_groups])
        drop.set_selected(0)
        content_box.append(drop)

        lbl_summary = Gtk.Label(css_classes=["caption", "dim-label"], wrap=True)
        lbl_summary.set_xalign(0.0)
        content_box.append(lbl_summary)

        def update_summary():
            idx = drop.get_selected()
            if 0 <= idx < len(marked_groups):
                target_g = marked_groups[idx]
                sources = [g for g in marked_groups if g.pr_id != target_g.pr_id]
                source_names = ", ".join(f"#{s.pr_id} ({s.host_package or s.title[:15]})" for s in sources)
                lbl_summary.set_text(
                    f"PR #{target_g.pr_id} will absorb {len(sources)} PR(s): {source_names}\n"
                    "(The absorbed PRs will be closed automatically)."
                )

        drop.connect("notify::selected", lambda d, p: update_summary())
        update_summary()

        dialog.set_extra_child(content_box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("combine", "Combine into Anchor")
        dialog.set_response_appearance("combine", Adw.ResponseAppearance.SUGGESTED)

        def on_response(d, resp):
            if resp == "combine":
                target_idx = drop.get_selected()
                target_g = marked_groups[target_idx]
                sources = [g.pr_id for g in marked_groups if g.pr_id != target_g.pr_id]
                res = self.service.combine_multiple_groups(target_g.pr_id, sources)
                self.show_operation_result(res)
                self.clear_groups_marked()
                if res.success:
                    self.refresh_data()

        dialog.connect("response", on_response)
        dialog.present()

    def on_rename_clicked(self, btn):
        if not self.selected_group:
            return
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Rename Group Title",
            body=f"Enter new title for PR #{self.selected_group.pr_id}:"
        )
        entry = Gtk.Entry(text=self.selected_group.title)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("rename", "Rename")
        dialog.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)

        def on_response(d, resp):
            if resp == "rename":
                new_t = entry.get_text().strip()
                if new_t:
                    res = self.service.rename_group(self.selected_group.pr_id, new_t)
                    self.show_operation_result(res)
                    if res.success:
                        self.selected_group.title = new_t
                        self.select_group(self.selected_group)

        dialog.connect("response", on_response)
        dialog.present()

    def on_batch_accept_clicked(self, btn):
        if not self.selected_groups_marked:
            return
        marked_groups = [g for g in self.groups if g.pr_id in self.selected_groups_marked]
        if not marked_groups:
            return

        conflicted = [f"#{g.pr_id}" for g in marked_groups if g.mergeable is False]
        if conflicted:
            self.show_toast(f"❌ Blocked: Marked PR(s) {', '.join(conflicted)} have Git merge conflicts!")
            return

        failed_prs = []
        for g in marked_groups:
            obs = self.service.get_obs_build_status(g.pr_id, g.branch)
            if obs.get("status") == "failed":
                fpkgs = ", ".join(obs.get("failed_pkgs", [])[:3])
                failed_prs.append(f"#{g.pr_id} ({fpkgs})")

        warn_txt = ""
        if failed_prs:
            fail_str = ", ".join(failed_prs)
            warn_txt = f"\n\n⚠️ WARNING: OBS staging builds are FAILING on: {fail_str}!"

        ids_str = ", ".join(f"#{g.pr_id}" for g in marked_groups)
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Batch Signal Staging Merge Approval",
            body=f"Submit 'merge ok' for {len(marked_groups)} marked staging PR(s) ({ids_str})?{warn_txt}"
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("merge", f"Submit 'merge ok' ({len(marked_groups)})")
        dialog.set_response_appearance("merge", Adw.ResponseAppearance.SUGGESTED)

        def on_response(d, resp):
            if resp == "merge":
                count = 0
                for g in marked_groups:
                    res = self.service.accept_group(g.pr_id)
                    if res.success:
                        count += 1
                self.show_toast(f"Successfully submitted 'merge ok' on {count} PR(s).")
                self.clear_groups_marked()

        dialog.connect("response", on_response)
        dialog.present()

    def on_accept_clicked(self, btn):
        if not self.selected_group:
            return

        if self.selected_group.mergeable is False:
            self.show_toast(f"❌ Blocked: PR #{self.selected_group.pr_id} has Git merge conflicts with '{self.selected_group.branch}'!")
            return
        # Pre-Merge OBS Failure Guard
        obs = self.service.get_obs_build_status(self.selected_group.pr_id, self.selected_group.branch)
        warn_txt = ""
        if obs.get("status") == "failed":
            failed_str = ", ".join(obs.get("failed_pkgs", [])[:3])
            warn_txt = f"\n\n⚠️ WARNING: OBS staging build is FAILING ({failed_str})!"

        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Signal Staging Merge Approval",
            body=f"Submit 'merge ok' for PR #{self.selected_group.pr_id} ('{self.selected_group.title}')?{warn_txt}"
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("merge", "Submit 'merge ok'")
        dialog.set_response_appearance("merge", Adw.ResponseAppearance.SUGGESTED)

        def on_response(d, resp):
            if resp == "merge":
                res = self.service.accept_group(self.selected_group.pr_id)
                self.show_operation_result(res)

        dialog.connect("response", on_response)
        dialog.present()

    def on_disintegrate_clicked(self, btn):
        if not self.selected_group:
            return
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Disintegrate Staging Group",
            body=f"Break down #{self.selected_group.pr_id} back into individual standalone PRs? All child PRs will be reopened."
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("disint", "Disintegrate")
        dialog.set_response_appearance("disint", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(d, resp):
            if resp == "disint":
                res = self.service.disintegrate_group(self.selected_group.pr_id)
                self.show_operation_result(res)
                if res.success:
                    self.refresh_data()

        dialog.connect("response", on_response)
        dialog.present()

    def on_open_browser_clicked(self, btn):
        if self.selected_group and self.selected_group.html_url:
            subprocess.Popen(["xdg-open", self.selected_group.html_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def on_audit_clicked(self, btn):
        target_id = self.selected_group.pr_id if self.selected_group else None
        OrphanAuditDialog(self, self.service, target_id, self.refresh_data).present()

    def show_operation_result(self, res: ss.OperationResult):
        toast = Adw.Toast.new(res.message)
        self.toast_overlay.add_toast(toast)

    def show_toast(self, message: str):
        toast = Adw.Toast.new(message)
        self.toast_overlay.add_toast(toast)


class StagingGuiApp(Adw.Application):
    def __init__(self, service: Optional[ss.StagingService] = None):
        super().__init__(application_id="org.opensuse.StagingManager", flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.service = service or ss.StagingService()

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = StagingGuiWindow(self, self.service)
        win.present()


def run_gui(client: Optional[pm.GiteaClient] = None):
    # In GUI mode, if no explicit repo was passed, load the user's active_workspace from ~/.config/pr-manage.json
    if client is None:
        cfg = pm.load_config()
        configured_repo = cfg.get("active_workspace") or pm.DEFAULT_REPO
        client = pm.GiteaClient(repo=configured_repo)
    app = StagingGuiApp(service=ss.StagingService(client=client))
    app.run([sys.argv[0]])


if __name__ == "__main__":
    run_gui()
