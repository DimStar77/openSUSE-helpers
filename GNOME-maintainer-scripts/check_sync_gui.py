#!/usr/bin/env python3
"""
GNOME Sync Dashboard - GTK4 / Libadwaita GUI
Interactive dashboard to explore downstream repository sync status,
upstream updates, and next-to-factory commit forwarding with inline diff viewer.
"""

import os
import sys
import subprocess
import concurrent.futures
import webbrowser

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, GObject, Gdk

try:
    gi.require_version('GtkSource', '5')
    from gi.repository import GtkSource
except ValueError:
    try:
        gi.require_version('GtkSource', '4')
        from gi.repository import GtkSource
    except ValueError:
        GtkSource = None

# Make sure we can import sync_backend from helpers/ by resolving symlinks
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), 'helpers'))
import sync_backend as sb

class SyncRow(Adw.ActionRow):
    """Custom row holding package data for the Sync tab."""
    def __init__(self, package_name, data):
        super().__init__()
        self.package_name = package_name
        self.data = data
        self.set_title(package_name)
        self.set_activatable(True) # Ensure row responds to activation
        
        # Build status label
        pool_status = data.get("pool_status", "unknown")
        pool_ahead = data.get("pool_ahead", 0)
        pool_behind = data.get("pool_behind", 0)
        next_status = data.get("next_status", "unknown")
        next_ahead = data.get("next_ahead", 0)
        next_behind = data.get("next_behind", 0)

        # Detail text
        actions = []
        if pool_behind > 0:
            actions.append("Pull Pool")
        if pool_ahead > 0:
            actions.append("Submit Pool")
        if next_behind > 0:
            actions.append("Merge Next")
            
        action_text = " & ".join(actions) if actions else "In Sync"
        self.set_subtitle(f"Action: {action_text}   (Double-click to view Diff)")
        
        # Build badges/pills as suffixes
        badge_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        
        # Stage 1 Badge
        s1_label = Gtk.Label()
        if pool_behind > 0 and pool_ahead > 0:
            s1_label.set_markup(f"<span foreground='red'>Diverged (B:{pool_behind}/A:{pool_ahead})</span>")
        elif pool_behind > 0:
            s1_label.set_markup(f"<span foreground='orange'>Behind {pool_behind}</span>")
        elif pool_ahead > 0:
            s1_label.set_markup(f"<span foreground='cyan'>Ahead {pool_ahead}</span>")
        elif pool_status == "Not in Pool":
            s1_label.set_markup("<span foreground='yellow'>Not in Pool</span>")
        else:
            s1_label.set_markup("<span foreground='green'>Stage 1 OK</span>")
        badge_box.append(s1_label)
        
        # Separator
        sep = Gtk.Label(label="|")
        badge_box.append(sep)
        
        # Stage 2 Badge
        s2_label = Gtk.Label()
        if next_behind > 0 and next_ahead > 0:
            s2_label.set_markup(f"<span foreground='red'>Diverged (B:{next_behind}/A:{next_ahead})</span>")
        elif next_behind > 0:
            s2_label.set_markup(f"<span foreground='orange'>Behind {next_behind}</span>")
        elif next_ahead > 0:
            s2_label.set_markup(f"<span foreground='green'>Ahead {next_ahead} (ok)</span>")
        elif next_status == "No next branch":
            s2_label.set_markup("<span>No next branch</span>")
        else:
            s2_label.set_markup("<span foreground='green'>Stage 2 OK</span>")
        badge_box.append(s2_label)
        
        self.add_suffix(badge_box)


class VersionRow(Adw.ActionRow):
    """Custom row holding package data for the Versions tab."""
    def __init__(self, package_name, data):
        super().__init__()
        self.package_name = package_name
        self.data = data
        self.set_title(package_name)
        
        factory_ver = data.get("factory_ver", "N/A")
        next_ver = data.get("next_ver", "—")
        upstream_stable = data.get("upstream_stable", "N/A")
        upstream_latest = data.get("upstream_latest", "—")
        
        self.set_subtitle(
            f"Local: [F: {factory_ver} | N: {next_ver}]   "
            f"Upstream: [S: {upstream_stable} | L: {upstream_latest}]"
        )
        
        # Suffix action buttons
        suffix_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        
        # Copy Version Button
        copy_btn = Gtk.Button.new_from_icon_name("edit-copy-symbolic")
        copy_btn.set_tooltip_text("Copy Upstream Stable Version")
        copy_btn.connect("clicked", self.on_copy_clicked, upstream_stable)
        suffix_box.append(copy_btn)
        
        # Open Upstream Page Button
        web_btn = Gtk.Button.new_from_icon_name("web-browser-symbolic")
        web_btn.set_tooltip_text("Open Release Monitoring Page")
        web_btn.connect("clicked", self.on_web_clicked, package_name)
        suffix_box.append(web_btn)
        
        self.add_suffix(suffix_box)

    def on_copy_clicked(self, btn, version):
        clipboard = Gdk.Display.get_default().get_clipboard()
        clipboard.set(version)

    def on_web_clicked(self, btn, package_name):
        url = f"https://release-monitoring.org/packages/?name={package_name}"
        webbrowser.open(url)


class ForwardRow(Adw.ActionRow):
    """Custom row holding package data for the Forwarding sidebar."""
    def __init__(self, package_name, sync_data, pr_data):
        super().__init__()
        self.package_name = package_name
        self.sync_data = sync_data
        self.pr_data = pr_data
        
        self.next_ahead = sync_data.get("next_ahead", 0)
        self.next_behind = sync_data.get("next_behind", 0)
        self.has_pr = pr_data.get("has_pr", False)
        self.pr_number = pr_data.get("number", None)
        self.pr_url = pr_data.get("url", None)
        
        # Build layout
        self.set_title(package_name)
        self.set_subtitle(f"Commits Ahead: {self.next_ahead} | Behind: {self.next_behind}")
        
        # PR Status Suffix Badge
        pr_label = Gtk.Label()
        if self.has_pr:
            pr_label.set_markup(f"<span foreground='green' weight='bold'>PR #{self.pr_number}</span>")
        else:
            pr_label.set_markup("<span foreground='red'>No PR</span>")
            
        self.add_suffix(pr_label)


class SyncDiffDialog(Gtk.Window):
    """Modal dialog displaying syntax-highlighted git diffs for a selected sync state."""
    def __init__(self, parent, package_name, data):
        super().__init__(transient_for=parent, modal=True, title=f"Sync Diff - {package_name}")
        self.set_default_size(840, 600)
        
        self.package_name = package_name
        self.data = data
        self.parent = parent
        
        # Main layout
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        
        # Header area
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header.set_margin_start(18)
        header.set_margin_end(18)
        header.set_margin_top(12)
        header.set_margin_bottom(12)
        
        self.title_label = Gtk.Label()
        self.title_label.set_halign(Gtk.Align.START)
        self.title_label.set_hexpand(True)
        self.title_label.set_markup(f"<span size='large' weight='bold'>Loading diff for {package_name}...</span>")
        header.append(self.title_label)
        
        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", lambda btn: self.destroy())
        header.append(close_btn)
        
        box.append(header)
        
        # Scrolled view for source buffer
        scroll = Gtk.ScrolledWindow()
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        
        if GtkSource:
            lang_manager = GtkSource.LanguageManager.get_default()
            lang = lang_manager.get_language('diff')
            self.buffer = GtkSource.Buffer()
            self.buffer.set_language(lang)
            self.buffer.set_highlight_syntax(True)
            self.view = GtkSource.View(buffer=self.buffer)
            self.view.set_show_line_numbers(True)
            self.view.set_highlight_current_line(True)
        else:
            self.buffer = Gtk.TextBuffer()
            self.view = Gtk.TextView(buffer=self.buffer)
            
        self.view.set_monospace(True)
        self.view.set_editable(False)
        scroll.set_child(self.view)
        box.append(scroll)
        
        self.set_child(box)
        
        # Start background load
        self.buffer.set_text("Connecting to pool & loading differences...")
        parent.executor.submit(self.load_diff_data)

    def load_diff_data(self):
        pool_status = self.data.get("pool_status", "unknown")
        pool_ahead = self.data.get("pool_ahead", 0)
        pool_behind = self.data.get("pool_behind", 0)
        next_behind = self.data.get("next_behind", 0)
        
        diff_text = ""
        comparison_desc = ""
        repo_path = os.path.join('.', self.package_name)
        
        try:
            if pool_behind > 0:
                comparison_desc = f"Pool (Upstream) vs local Factory  [Behind by {pool_behind} commits]"
                # Fetch pool/factory first to ensure FETCH_HEAD is correct
                gitea_name = sb.get_gitea_repo_name(repo_path, self.package_name)
                pool_url = f"https://src.opensuse.org/pool/{gitea_name}.git"
                subprocess.run(
                    ['git', '-C', repo_path, 'fetch', '--quiet', pool_url, 'factory'],
                    check=True, capture_output=True
                )
                res = subprocess.run(
                    ['git', '-C', repo_path, 'diff', 'origin/factory...FETCH_HEAD'],
                    check=True, capture_output=True, text=True
                )
                diff_text = res.stdout
                if not diff_text.strip():
                    diff_text = "No differences in spec files or sources detected."
            elif pool_ahead > 0:
                comparison_desc = f"local Factory vs Pool (Upstream)  [Ahead by {pool_ahead} commits]"
                gitea_name = sb.get_gitea_repo_name(repo_path, self.package_name)
                pool_url = f"https://src.opensuse.org/pool/{gitea_name}.git"
                subprocess.run(
                    ['git', '-C', repo_path, 'fetch', '--quiet', pool_url, 'factory'],
                    check=True, capture_output=True
                )
                res = subprocess.run(
                    ['git', '-C', repo_path, 'diff', 'FETCH_HEAD...origin/factory'],
                    check=True, capture_output=True, text=True
                )
                diff_text = res.stdout
                if not diff_text.strip():
                    diff_text = "No differences in spec files or sources detected."
            elif next_behind > 0:
                comparison_desc = f"local Next vs local Factory  [Next behind by {next_behind} commits]"
                res = subprocess.run(
                    ['git', '-C', repo_path, 'diff', 'origin/next...origin/factory'],
                    check=True, capture_output=True, text=True
                )
                diff_text = res.stdout
                if not diff_text.strip():
                    diff_text = "No differences in spec files or sources detected."
            else:
                comparison_desc = "Repository Sync Details"
                diff_text = "Package is fully in sync between Pool, Factory, and Next."
        except Exception as e:
            diff_text = f"Error performing git diff: {str(e)}"
            
        GLib.idle_add(self.update_ui, comparison_desc, diff_text)

    def update_ui(self, desc, text):
        self.title_label.set_markup(f"<span size='large' weight='bold'>{desc}</span>")
        self.buffer.set_text(text)


class SyncWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="GNOME Sync Dashboard")
        self.set_default_size(1100, 720)
        
        # Core data
        self.repos = sorted([
            d for d in os.listdir('.')
            if os.path.isdir(d) and os.path.exists(os.path.join(d, '.git'))
        ])
        
        # Background workers
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=50)
        
        # UI components
        self.stack = Adw.ViewStack()
        
        self.build_sync_view()
        self.build_version_view()
        self.build_forward_view()
        
        # View Switcher
        self.header_bar = Adw.HeaderBar()
        self.view_switcher = Adw.ViewSwitcher(stack=self.stack)
        self.header_bar.set_title_widget(self.view_switcher)
        
        # Top-level container
        self.toolbar_view = Adw.ToolbarView()
        self.toolbar_view.add_top_bar(self.header_bar)
        self.toolbar_view.set_content(self.stack)
        self.set_content(self.toolbar_view)
        
        # Kick off background loading
        self.refresh_all()

    def refresh_all(self):
        self.start_sync_scan()
        self.start_version_scan()
        self.start_forwarding_scan()

    # --- TAB 1: SYNC VIEW ---
    def build_sync_view(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        
        # Control bar
        control_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        control_bar.set_margin_start(18)
        control_bar.set_margin_end(18)
        control_bar.set_margin_top(12)
        control_bar.set_margin_bottom(6)
        
        # Search Entry
        self.sync_search = Gtk.SearchEntry()
        self.sync_search.set_hexpand(True)
        self.sync_search.connect("search-changed", self.on_sync_search_changed)
        control_bar.append(self.sync_search)
        
        # Filter Toggle (Only Needs Action)
        self.sync_filter_toggle = Gtk.CheckButton(label="Only Needs Action")
        self.sync_filter_toggle.set_active(True)
        self.sync_filter_toggle.connect("toggled", lambda cb: self.sync_list_box.invalidate_filter())
        control_bar.append(self.sync_filter_toggle)
        
        # Refresh Button
        refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh_btn.connect("clicked", lambda btn: self.start_sync_scan())
        control_bar.append(refresh_btn)
        
        # Spinner/Progress
        self.sync_spinner = Gtk.Spinner()
        control_bar.append(self.sync_spinner)
        
        self.sync_progress_label = Gtk.Label(label="Ready")
        control_bar.append(self.sync_progress_label)
        
        box.append(control_bar)
        
        # List box container
        scroll = Gtk.ScrolledWindow()
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        
        self.sync_list_box = Gtk.ListBox()
        self.sync_list_box.set_filter_func(self.sync_filter_func)
        self.sync_list_box.connect("row-activated", self.on_sync_row_activated)
        scroll.set_child(self.sync_list_box)
        box.append(scroll)
        
        # Add to stack
        self.stack.add_titled_with_icon(
            box, "sync", "Repository Sync", "folder-download-symbolic"
        )

    def sync_filter_func(self, row):
        # 1. Search text filter
        search_text = self.sync_search.get_text().lower()
        if search_text and search_text not in row.package_name.lower():
            return False
            
        # 2. Needs action checkbox filter
        only_needs_action = self.sync_filter_toggle.get_active()
        if only_needs_action and not row.data.get("needs_action", False):
            return False
            
        return True

    def on_sync_search_changed(self, entry):
        self.sync_list_box.invalidate_filter()

    def start_sync_scan(self):
        # Clear previous rows
        while True:
            row = self.sync_list_box.get_row_at_index(0)
            if not row:
                break
            self.sync_list_box.remove(row)
            
        self.sync_spinner.start()
        self.sync_completed_count = 0
        self.sync_progress_label.set_text(f"Scanning 0/{len(self.repos)}...")
        
        # Queue all repos in background thread pool
        for repo in self.repos:
            self.executor.submit(self.run_bg_sync, repo)

    def run_bg_sync(self, repo):
        name, data = sb.check_repo_sync(repo)
        GLib.idle_add(self.add_sync_result, name, data)

    def add_sync_result(self, name, data):
        self.sync_completed_count += 1
        self.sync_progress_label.set_text(f"Scanning {self.sync_completed_count}/{len(self.repos)}...")
        
        if data.get("status") == "success":
            row = SyncRow(name, data)
            self.sync_list_box.append(row)
            
        if self.sync_completed_count == len(self.repos):
            self.sync_spinner.stop()
            self.sync_progress_label.set_text("Scan Completed")

    def on_sync_row_activated(self, list_box, row):
        if not row:
            return
        dialog = SyncDiffDialog(self, row.package_name, row.data)
        dialog.present()


    # --- TAB 2: VERSION VIEW ---
    def build_version_view(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        
        control_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        control_bar.set_margin_start(18)
        control_bar.set_margin_end(18)
        control_bar.set_margin_top(12)
        control_bar.set_margin_bottom(6)
        
        # Search Entry
        self.ver_search = Gtk.SearchEntry()
        self.ver_search.set_hexpand(True)
        self.ver_search.connect("search-changed", lambda entry: self.ver_list_box.invalidate_filter())
        control_bar.append(self.ver_search)
        
        # Filter Toggle (Only Needs Update)
        self.ver_filter_toggle = Gtk.CheckButton(label="Only Needs Update")
        self.ver_filter_toggle.set_active(True)
        self.ver_filter_toggle.connect("toggled", lambda cb: self.ver_list_box.invalidate_filter())
        control_bar.append(self.ver_filter_toggle)
        
        # Refresh Button
        refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh_btn.connect("clicked", lambda btn: self.start_version_scan())
        control_bar.append(refresh_btn)
        
        # Spinner/Progress
        self.ver_spinner = Gtk.Spinner()
        control_bar.append(self.ver_spinner)
        
        self.ver_progress_label = Gtk.Label(label="Ready")
        control_bar.append(self.ver_progress_label)
        
        box.append(control_bar)
        
        # List box container
        scroll = Gtk.ScrolledWindow()
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        
        self.ver_list_box = Gtk.ListBox()
        self.ver_list_box.set_filter_func(self.ver_filter_func)
        scroll.set_child(self.ver_list_box)
        box.append(scroll)
        
        # Add to stack
        self.stack.add_titled_with_icon(
            box, "versions", "Upstream Updates", "software-update-available-symbolic"
        )

    def ver_filter_func(self, row):
        # 1. Search text filter
        search_text = self.ver_search.get_text().lower()
        if search_text and search_text not in row.package_name.lower():
            return False
            
        # 2. Needs update checkbox filter
        only_needs_update = self.ver_filter_toggle.get_active()
        if only_needs_update and not row.data.get("needs_update", False):
            return False
            
        return True

    def start_version_scan(self):
        while True:
            row = self.ver_list_box.get_row_at_index(0)
            if not row:
                break
            self.ver_list_box.remove(row)
            
        self.ver_spinner.start()
        self.ver_completed_count = 0
        self.ver_progress_label.set_text(f"Scanning 0/{len(self.repos)}...")
        
        # Queue all repos in background (30 concurrent workers to avoid release-monitoring rate limits)
        for repo in self.repos:
            self.executor.submit(self.run_bg_version, repo)

    def run_bg_version(self, repo):
        name, data = sb.check_repo_version(repo)
        GLib.idle_add(self.add_version_result, name, data)

    def add_version_result(self, name, data):
        self.ver_completed_count += 1
        self.ver_progress_label.set_text(f"Scanning {self.ver_completed_count}/{len(self.repos)}...")
        
        if data.get("status") == "success":
            row = VersionRow(name, data)
            self.ver_list_box.append(row)
            
        if self.ver_completed_count == len(self.repos):
            self.ver_spinner.stop()
            self.ver_progress_label.set_text("Scan Completed")


    # --- TAB 3: FORWARDING VIEW ---
    def build_forward_view(self):
        # Main container split horizontally
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(340) # Width of sidebar
        
        # Left Panel (Sidebar)
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar.set_margin_start(12)
        sidebar.set_margin_end(12)
        sidebar.set_margin_top(12)
        sidebar.set_margin_bottom(12)
        
        # Sidebar Controls
        sidebar_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        sidebar_controls.set_margin_bottom(12)
        
        self.fwd_search = Gtk.SearchEntry()
        self.fwd_search.set_hexpand(True)
        self.fwd_search.connect("search-changed", lambda entry: self.fwd_list_box.invalidate_filter())
        sidebar_controls.append(self.fwd_search)
        
        self.fwd_filter_toggle = Gtk.CheckButton(label="No PR")
        self.fwd_filter_toggle.set_active(False)
        self.fwd_filter_toggle.connect("toggled", lambda cb: self.fwd_list_box.invalidate_filter())
        sidebar_controls.append(self.fwd_filter_toggle)
        
        sidebar.append(sidebar_controls)
        
        # Sidebar List
        sidebar_scroll = Gtk.ScrolledWindow()
        sidebar_scroll.set_hexpand(True)
        sidebar_scroll.set_vexpand(True)
        
        self.fwd_list_box = Gtk.ListBox()
        self.fwd_list_box.set_filter_func(self.fwd_filter_func)
        self.fwd_list_box.connect("row-selected", self.on_forward_row_selected)
        sidebar_scroll.set_child(self.fwd_list_box)
        sidebar.append(sidebar_scroll)
        
        paned.set_start_child(sidebar)
        
        # Right Panel (Detail Area)
        self.detail_stack = Adw.ViewStack()
        
        # Page 1: Empty Page
        self.empty_page = Adw.StatusPage()
        self.empty_page.set_title("Select a Package")
        self.empty_page.set_description("Choose a package from the sidebar to review commits and its file diff.")
        self.empty_page.set_icon_name("open-menu-symbolic")
        self.detail_stack.add_named(self.empty_page, "empty")
        
        # Page 2: Diff Page
        diff_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        
        # Diff Header Bar
        diff_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        diff_header.set_margin_start(18)
        diff_header.set_margin_end(18)
        diff_header.set_margin_top(12)
        diff_header.set_margin_bottom(12)
        
        self.diff_title_label = Gtk.Label(label="No Package Selected")
        self.diff_title_label.set_hexpand(True)
        self.diff_title_label.set_halign(Gtk.Align.START)
        self.diff_title_label.set_markup("<span size='large' weight='bold'>No Package Selected</span>")
        diff_header.append(self.diff_title_label)
        
        # Open PR Button
        self.pr_btn = Gtk.Button(label="Create Pull Request")
        self.pr_btn.set_sensitive(False)
        self.pr_btn.connect("clicked", self.on_create_pr_clicked)
        diff_header.append(self.pr_btn)
        
        diff_box.append(diff_header)
        
        # Diff Viewer (using GtkSourceView if available)
        diff_scroll = Gtk.ScrolledWindow()
        diff_scroll.set_hexpand(True)
        diff_scroll.set_vexpand(True)
        
        if GtkSource:
            lang_manager = GtkSource.LanguageManager.get_default()
            self.diff_lang = lang_manager.get_language('diff')
            self.diff_buffer = GtkSource.Buffer()
            self.diff_buffer.set_language(self.diff_lang)
            self.diff_buffer.set_highlight_syntax(True)
            self.diff_view = GtkSource.View(buffer=self.diff_buffer)
            self.diff_view.set_show_line_numbers(True)
            self.diff_view.set_highlight_current_line(True)
            self.diff_view.set_editable(False)
        else:
            self.diff_buffer = Gtk.TextBuffer()
            self.diff_view = Gtk.TextView(buffer=self.diff_buffer)
            self.diff_view.set_editable(False)
            
        self.diff_view.set_monospace(True)
        diff_scroll.set_child(self.diff_view)
        diff_box.append(diff_scroll)
        
        self.detail_stack.add_named(diff_box, "diff")
        
        paned.set_end_child(self.detail_stack)
        
        # Add tab to main view stack
        self.stack.add_titled_with_icon(
            paned, "forwarding", "Forward & PR", "mail-send-receive-symbolic"
        )

    def fwd_filter_func(self, row):
        # 1. Search text filter
        search_text = self.fwd_search.get_text().lower()
        if search_text and search_text not in row.package_name.lower():
            return False
            
        # 2. "No PR" filter toggle
        only_no_pr = self.fwd_filter_toggle.get_active()
        if only_no_pr and row.has_pr:
            return False
            
        return True

    def start_forwarding_scan(self):
        while True:
            row = self.fwd_list_box.get_row_at_index(0)
            if not row:
                break
            self.fwd_list_box.remove(row)
            
        # Select empty state page by default
        self.detail_stack.set_visible_child(self.empty_page)
        
        # Forward scan fetches sync status, and if ahead > 0, fetches Gitea PR status
        for repo in self.repos:
            self.executor.submit(self.run_bg_forward, repo)

    def run_bg_forward(self, repo):
        # 1. Check sync status
        _, sync_data = sb.check_repo_sync(repo)
        if sync_data.get("status") == "success" and sync_data.get("next_status") != "No next branch":
            next_ahead = sync_data.get("next_ahead", 0)
            if next_ahead > 0:
                # 2. Check Gitea PR status (only for those actually ahead)
                _, pr_data = sb.check_repo_pr(repo)
                GLib.idle_add(self.add_forward_result, repo, sync_data, pr_data)

    def add_forward_result(self, name, sync_data, pr_data):
        row = ForwardRow(name, sync_data, pr_data)
        self.fwd_list_box.append(row)

    def on_forward_row_selected(self, list_box, row):
        if not row:
            self.detail_stack.set_visible_child(self.empty_page)
            self.pr_btn.set_sensitive(False)
            return
            
        self.detail_stack.set_visible_child_name("diff")
        self.pr_btn.set_sensitive(True)
        self.current_selected_package = row.package_name
        self.diff_title_label.set_markup(
            f"<span size='large' weight='bold'>Diff for {row.package_name}</span>   "
            f"<span size='small' foreground='gray'>({row.next_ahead} commits ahead)</span>"
        )
        
        # Load diff text asynchronously
        self.diff_buffer.set_text("Loading diff...")
        self.executor.submit(self.run_bg_diff, row.package_name)

    def run_bg_diff(self, package_name):
        diff_text = sb.get_git_diff(package_name)
        GLib.idle_add(self.update_diff_text, diff_text)

    def update_diff_text(self, text):
        self.diff_buffer.set_text(text)

    def on_create_pr_clicked(self, btn):
        if hasattr(self, "current_selected_package"):
            url = sb.get_gitea_pr_url(self.current_selected_package)
            webbrowser.open(url)


class SyncApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="org.opensuse.gnome.sync_dashboard")
        
    def do_activate(self):
        win = SyncWindow(self)
        win.present()

if __name__ == '__main__':
    app = SyncApp()
    sys.argv = [sys.argv[0]]  # Strip extra args to prevent GTK app parsing issues
    sys.exit(app.run(sys.argv))
