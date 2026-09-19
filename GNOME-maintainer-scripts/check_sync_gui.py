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
import threading
import signal
import re
import time

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Vte', '3.91')
gi.require_version('Pango', '1.0')
gi.require_version('Gdk', '4.0')
from gi.repository import Gtk, Adw, GLib, GObject, Gdk, Vte, Pango, Gio

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

import shlex
import xml.etree.ElementTree as ET
from typing import Optional, Tuple

# Pre-compiled regular expressions for high-performance matching and pattern analysis
RE_HEX_40 = re.compile(r'^[0-9a-fA-F]{40}$')
RE_HEX_SHORT = re.compile(r'^[0-9a-fA-F]{7,12}$')
RE_VERSION_3 = re.compile(r'(\d+)([\._])(\d+)\2(\d+)')
RE_VERSION_2 = re.compile(r'(\d+)([\._])(\d+)')

def get_service_revision(pkg_dir: str) -> Optional[str]:
    """Parse the package's local _service file to find the revision parameter for the main obs_scm service.

    Catches specific XML parsing and OS access errors to prevent silencing unrelated runtime bugs.
    """
    service_path = os.path.join(pkg_dir, '_service')
    if not os.path.exists(service_path):
        return None
    try:
        tree = ET.parse(service_path)
        root = tree.getroot()
        for service in root.findall('service'):
            if service.get('name') == 'obs_scm':
                # Check versionformat to avoid sub-gitmodules
                versionformat = None
                for param in service.findall('param'):
                    if param.get('name') == 'versionformat':
                        versionformat = param.text
                if versionformat is not None and versionformat.strip() == '0.gitmodule':
                    continue
                # Main obs_scm service! Get its revision
                for param in service.findall('param'):
                    if param.get('name') == 'revision':
                        return param.text.strip() if param.text else None
    except (ET.ParseError, PermissionError, OSError):
        pass
    return None

def guess_update_revision(current_revision: Optional[str], target_version: str) -> Tuple[Optional[str], Optional[str]]:
    """Analyze the pattern of the current revision in _service (e.g. 'v9.1.2')
    and match it to target_version to produce a guessed revision, or return a confidence message.
    """
    if not current_revision:
        return target_version, None

    current_revision = current_revision.strip()
    target_version = target_version.strip()

    # 1. Check if the current revision is a full Git commit SHA (40 hex chars)
    if RE_HEX_40.match(current_revision):
        return None, f"tracks a specific Git commit SHA ({current_revision[:8]})"

    # 2. Check if the current revision is a short Git commit SHA (7-12 hex chars)
    if RE_HEX_SHORT.match(current_revision):
        return None, f"tracks a short Git commit SHA ({current_revision})"

    # 3. Check if the current revision is a static development branch name
    if current_revision in ['master', 'main', 'stable', 'factory', 'next', 'develop', 'development', 'trunk']:
        return None, f"tracks a static development branch ('{current_revision}')"

    # 4. Try to match version patterns with dot or underscore separators
    # Check 3-part versions first (e.g. X.Y.Z or X_Y_Z)
    match = RE_VERSION_3.search(current_revision)
    if not match:
        # Check 2-part versions (e.g. X.Y or X_Y)
        match = RE_VERSION_2.search(current_revision)

    if match:
        separator = match.group(2) # '.' or '_'

        start_idx = match.start()
        end_idx = match.end()
        prefix = current_revision[:start_idx]
        suffix = current_revision[end_idx:]

        # Format the target version with the same separator
        new_ver_str = target_version.replace('.', separator)
        guessed_revision = f"{prefix}{new_ver_str}{suffix}"
        return guessed_revision, None

    # If no version-like digits and separators were found, we are not confident
    return None, f"contains an unrecognized pattern ('{current_revision}')"


class DaemonThreadPoolExecutor(concurrent.futures.ThreadPoolExecutor):
    """ThreadPoolExecutor that forces all spawned worker threads to be daemon threads."""
    def _adjust_thread_count(self):
        orig_thread = threading.Thread
        def daemon_thread(*args, **kwargs):
            kwargs['daemon'] = True
            return orig_thread(*args, **kwargs)
        threading.Thread = daemon_thread
        try:
            super()._adjust_thread_count()
        finally:
            threading.Thread = orig_thread


class PackageRow(Gtk.ListBoxRow):
    """Unified master list row representing a single package with multi-stage status badges."""
    def __init__(self, package_name, parent_window):
        super().__init__()
        self.package_name = package_name
        self.parent_window = parent_window

        # Main horizontal box
        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        main_box.set_margin_start(12)
        main_box.set_margin_end(12)
        main_box.set_margin_top(8)
        main_box.set_margin_bottom(8)

        # Left side: Text details
        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_hexpand(True)

        # Package Title
        self.name_label = Gtk.Label(halign=Gtk.Align.START)
        self.name_label.set_markup(f"<span weight='bold'>{package_name}</span>")
        self.name_label.set_ellipsize(Pango.EllipsizeMode.END)
        text_box.append(self.name_label)

        # Subtitle for status text
        self.sub_label = Gtk.Label(halign=Gtk.Align.START)
        self.sub_label.set_markup("<span size='small' foreground='gray'>Pending scan...</span>")
        self.sub_label.set_ellipsize(Pango.EllipsizeMode.END)
        text_box.append(self.sub_label)

        main_box.append(text_box)

        # Right side: Badges container
        self.badges_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.badges_box.set_valign(Gtk.Align.CENTER)
        main_box.append(self.badges_box)

        self.set_child(main_box)

    def create_badge(self, text, color):
        label = Gtk.Label()
        label.set_margin_start(6)
        label.set_margin_end(6)
        label.set_margin_top(2)
        label.set_margin_bottom(2)

        color_map = {
            "orange": "#ff9f0a",
            "yellow": "#f5c211",
            "cyan": "#00d2ff",
            "red": "#ff453a",
            "green": "#30d158",
            "purple": "#bf5af2",
            "gray": "#8e8e93"
        }
        hex_color = color_map.get(color, "gray")

        label.set_markup(f"<span size='x-small' weight='bold' foreground='black' background='{hex_color}'>  {text}  </span>")
        return label

    def update_ui(self, sync_data, version_data, pr_data):
        # Clear existing badges
        while True:
            child = self.badges_box.get_first_child()
            if not child:
                break
            self.badges_box.remove(child)

        subtitle_parts = []

        # 1. Sync & Gitea Pool state
        if sync_data:
            pool_behind = sync_data.get("pool_behind", 0)
            pool_ahead = sync_data.get("pool_ahead", 0)
            pool_status = sync_data.get("pool_status", "unknown")
            next_ahead = sync_data.get("next_ahead", 0)
            next_behind = sync_data.get("next_behind", 0)

            if pool_behind > 0 or pool_ahead > 0:
                subtitle_parts.append(f"Pool: B:{pool_behind}/A:{pool_ahead}")
                self.badges_box.append(self.create_badge("Pool", "orange"))
            elif pool_status == "Not in Pool":
                subtitle_parts.append("Not in Gitea")
                self.badges_box.append(self.create_badge("No Pool", "yellow"))

            if next_ahead > 0:
                subtitle_parts.append(f"Next: +{next_ahead}")
                self.badges_box.append(self.create_badge(f"+{next_ahead}", "cyan"))
            elif next_behind > 0:
                subtitle_parts.append(f"Next: -{next_behind}")
                self.badges_box.append(self.create_badge(f"-{next_behind}", "red"))

        # 2. Upstream Version updates
        if version_data:
            factory_ver = version_data.get("factory_ver", "N/A")
            upstream_stable = version_data.get("upstream_stable", "N/A")
            next_ver = version_data.get("next_ver", "—")
            upstream_latest = version_data.get("upstream_latest", "—")

            if factory_ver != upstream_stable and factory_ver != "N/A" and upstream_stable != "N/A":
                subtitle_parts.append("Stable Update")
                self.badges_box.append(self.create_badge("Stable 🔺", "green"))

            if next_ver != "—" and next_ver != upstream_latest and upstream_latest != "—":
                subtitle_parts.append("Unstable Update")
                self.badges_box.append(self.create_badge("Unstable 🔺", "purple"))

        # 3. Active Pull Requests
        if pr_data and pr_data.get("has_pr", False):
            pr_num = pr_data.get("number", "PR")
            subtitle_parts.append(f"PR #{pr_num}")
            self.badges_box.append(self.create_badge(f"PR #{pr_num}", "green"))

        # Apply subtitle
        if subtitle_parts:
            self.sub_label.set_markup(f"<span size='small' foreground='gray'>{' | '.join(subtitle_parts)}</span>")
        else:
            if sync_data and version_data:
                self.sub_label.set_markup("<span size='small' foreground='green'>✅ Fully In Sync &amp; Up-To-Date</span>")
            else:
                self.sub_label.set_markup("<span size='small' foreground='gray'>In Sync</span>")


class SyncDiffDialog(Gtk.Window):
    """Modal dialog displaying syntax-highlighted git diffs for a selected sync state."""
    def __init__(self, parent, package_name, data):
        super().__init__(transient_for=parent, modal=True, title=f"Sync Diff - {package_name}")
        self.set_default_size(840, 600)

        self.package_name = package_name
        self.data = data
        self.parent = parent
        self.is_destroyed = False

        self.connect("destroy", self.on_destroy)

        # Main layout
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Title/Description header
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        header.set_margin_start(18)
        header.set_margin_end(18)
        header.set_margin_top(12)
        header.set_margin_bottom(12)

        self.title_label = Gtk.Label(halign=Gtk.Align.START)
        self.title_label.set_hexpand(True)
        self.title_label.set_markup("<span size='large' weight='bold'>Loading repository diff...</span>")
        header.append(self.title_label)

        # Close button
        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", lambda btn: self.destroy())
        header.append(close_btn)

        box.append(header)

        # Text ScrolledWindow
        scroll = Gtk.ScrolledWindow()
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)

        if GtkSource:
            lang_manager = GtkSource.LanguageManager.get_default()
            self.lang = lang_manager.get_language('diff')
            self.buffer = GtkSource.Buffer()
            self.buffer.set_language(self.lang)
            self.buffer.set_highlight_syntax(True)
            self.view = GtkSource.View(buffer=self.buffer)
            self.view.set_show_line_numbers(True)
            self.view.set_highlight_current_line(True)
            self.view.set_editable(False)
        else:
            self.buffer = Gtk.TextBuffer()
            self.view = Gtk.TextView(buffer=self.buffer)
            self.view.set_editable(False)

        self.view.set_monospace(True)
        scroll.set_child(self.view)
        box.append(scroll)

        self.set_child(box)

        # Load diff text asynchronously
        self.executor = parent.executor
        self.executor.submit(self.load_diff_data)

    def on_destroy(self, widget):
        self.is_destroyed = True

    def load_diff_data(self):
        """Asynchronously retrieves local git diff based on sync states."""
        repo_path = os.path.join('.', self.package_name)
        if not os.path.exists(repo_path):
            if not self.is_destroyed:
                GLib.idle_add(self.update_ui, "Error", "Package directory not found.")
            return

        pool_status = self.data.get("pool_status", "unknown")
        pool_behind = self.data.get("pool_behind", 0)
        pool_ahead = self.data.get("pool_ahead", 0)
        next_behind = self.data.get("next_behind", 0)

        comparison_desc = "Repository Sync Details"
        diff_text = ""

        try:
            if pool_behind > 0:
                comparison_desc = f"Pool (Upstream) vs local Factory  [Behind by {pool_behind} commits]"
                gitea_name = sb.get_gitea_repo_name(repo_path, self.package_name)
                pool_url = f"https://src.opensuse.org/pool/{gitea_name}.git"
                sb.run_tracked(
                    ['git', '-C', repo_path, 'fetch', '--quiet', pool_url, 'factory'],
                    check=True, capture_output=True
                )
                res = sb.run_tracked(
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
                sb.run_tracked(
                    ['git', '-C', repo_path, 'fetch', '--quiet', pool_url, 'factory'],
                    check=True, capture_output=True
                )
                res = sb.run_tracked(
                    ['git', '-C', repo_path, 'diff', 'FETCH_HEAD...origin/factory'],
                    check=True, capture_output=True, text=True
                )
                diff_text = res.stdout
                if not diff_text.strip():
                    diff_text = "No differences in spec files or sources detected."
            elif next_behind > 0:
                comparison_desc = f"local Next vs local Factory  [Next behind by {next_behind} commits]"
                res = sb.run_tracked(
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

        if not self.is_destroyed:
            GLib.idle_add(self.update_ui, comparison_desc, diff_text)

    def update_ui(self, desc, text):
        if not self.is_destroyed:
            self.title_label.set_markup(f"<span size='large' weight='bold'>{desc}</span>")
            self.buffer.set_text(text)


class SyncWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="GNOME Sync Dashboard")
        self.set_default_size(1250, 780)
        self.set_size_request(950, 620) # Prevent GTK Paned measurement warning at startup
        self.set_icon_name("preferences-system-network")

        # Core data
        self.repos = sorted([
            d for d in os.listdir('.')
            if os.path.isdir(d) and os.path.exists(os.path.join(d, '.git'))
        ])

        # Background workers configured with daemon threads so they terminate on exit
        self.executor = DaemonThreadPoolExecutor(max_workers=50)

        # Open tab registry for active monitoring and deduplication
        self.terminal_tabs = []

        # Load user's preferred monospace font dynamically from GNOME GSettings
        self.monospace_font = self.get_system_monospace_font()

        # Initialize filter timeout and state registries
        self.filter_timeout_id = 0
        self.last_diff_package = None

        # Initialize package data dictionary
        self.package_data = {
            repo: {
                "sync": {},
                "version": {},
                "pr": {}
            } for repo in self.repos
        }

        # Build Sidebar
        self.sidebar_box = self.build_sidebar()

        # Build Detail Pane
        self.detail_pane = self.build_detail_pane()

        # Horizontal split pane: Left is Sidebar, Right is Detail Pane
        self.horizontal_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.horizontal_paned.set_start_child(self.sidebar_box)
        self.horizontal_paned.set_end_child(self.detail_pane)

        # Header Bar
        self.header_bar = Adw.HeaderBar()
        title_lbl = Gtk.Label()
        title_lbl.set_markup("<span weight='bold'>GNOME Sync Dashboard</span>")
        self.header_bar.set_title_widget(title_lbl)

        # Vertical split pane: Top is horizontal split, Bottom is terminal drawer
        self.main_paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self.main_paned.set_start_child(self.horizontal_paned)

        # Collapsible Terminal Drawer
        self.terminal_drawer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Terminal Header
        term_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        term_header.set_margin_start(18)
        term_header.set_margin_end(18)
        term_header.set_margin_top(6)
        term_header.set_margin_bottom(6)

        self.term_title_label = Gtk.Label()
        self.term_title_label.set_hexpand(True)
        self.term_title_label.set_halign(Gtk.Align.START)
        self.term_title_label.set_markup("<span weight='bold'>Terminal Console Drawer</span>")
        term_header.append(self.term_title_label)

        # Hide terminal button
        hide_btn = Gtk.Button.new_from_icon_name("window-close-symbolic")
        hide_btn.set_tooltip_text("Hide Terminal Console")
        hide_btn.connect("clicked", lambda btn: self.hide_terminal())
        term_header.append(hide_btn)

        self.terminal_drawer.append(term_header)

        # Native Vte Tabbed Notebook Widget
        self.notebook = Gtk.Notebook()
        self.notebook.set_hexpand(True)
        self.notebook.set_vexpand(True)
        self.notebook.set_scrollable(True)
        self.notebook.set_show_border(True)
        self.notebook.connect("switch-page", self.on_notebook_switch_page)
        self.terminal_drawer.append(self.notebook)

        self.main_paned.set_end_child(self.terminal_drawer)
        self.main_paned.set_resize_end_child(True)

        # Hidden by default
        self.terminal_drawer.set_visible(False)
        self.terminal_drawer.connect("notify::visible", self.on_terminal_drawer_visible_changed)

        # Top-level container
        self.toolbar_view = Adw.ToolbarView()
        self.toolbar_view.add_top_bar(self.header_bar)
        self.toolbar_view.set_content(self.main_paned)

        # Wrap everything inside an Adw.ToastOverlay for floating notifications
        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(self.toolbar_view)
        self.set_content(self.toast_overlay)

        # Stop background scan leak when window is closed
        self.connect("close-request", self.on_close_request)

        # Periodic GLib timer: checks terminal process states every 1.5 seconds
        self.timeout_id = GLib.timeout_add(1500, self.monitor_terminals)

        # Set split positions asynchronously after initial layout frames to prevent measurement warnings!
        GLib.idle_add(lambda: self.horizontal_paned.set_position(420))
        GLib.idle_add(lambda: self.main_paned.set_position(520))

        # Kick off background loading
        self.refresh_all()

    def get_system_monospace_font(self):
        """Query GNOME GSettings dynamically to load the user's monospace font preference defensively."""
        try:
            schema_source = Gio.SettingsSchemaSource.get_default()
            if schema_source and schema_source.lookup("org.gnome.desktop.interface", True) is not None:
                settings = Gio.Settings.new("org.gnome.desktop.interface")
                font_str = settings.get_string("monospace-font-name")
                if font_str:
                    return font_str
        except Exception:
            pass
        return "monospace 11"

    def on_terminal_drawer_visible_changed(self, widget, pspec):
        if hasattr(self, "diff_revealer") and self.diff_revealer:
            is_term_visible = self.terminal_drawer.get_visible()
            self.diff_revealer.set_reveal_child(not is_term_visible)

    def on_close_request(self, window):
        """Gracefully dismantles GLib timers, closes thread pools, and terminates shell children."""
        if hasattr(self, "timeout_id") and self.timeout_id:
            GLib.Source.remove(self.timeout_id)
            self.timeout_id = 0

        self.executor.shutdown(wait=False, cancel_futures=True)

        for tab in list(self.terminal_tabs):
            shell_pid = tab.get("shell_pid")
            if shell_pid:
                try:
                    os.kill(shell_pid, signal.SIGHUP)
                except Exception:
                    try:
                        os.kill(shell_pid, signal.SIGKILL)
                    except Exception:
                        pass

        os._exit(0)

    def get_mapped_worktree_path(self, package_name, target_branch):
        """Maps current project path parent GNOME:Next to GNOME for worktree builds."""
        current_dir = os.path.abspath('.')
        parent_dir, current_folder_name = os.path.split(current_dir)

        target_folder_name = current_folder_name
        if target_branch == "factory" and "GNOME:Next" in current_folder_name:
            target_folder_name = current_folder_name.replace("GNOME:Next", "GNOME")
        elif target_branch == "next" and current_folder_name == "GNOME".strip():
            target_folder_name = "GNOME:Next"

        mapped_dir = os.path.join(parent_dir, target_folder_name, package_name)

        if os.path.exists(mapped_dir) and (os.path.exists(os.path.join(mapped_dir, '.git')) or os.path.isfile(os.path.join(mapped_dir, '.git'))):
            return mapped_dir

        return os.path.abspath(os.path.join('.', package_name))

    def is_shell_pid_active(self, shell_pid):
        """Scans /proc directly in pure Python without spawning subprocesses (pgrep)."""
        if not shell_pid:
            return False
        try:
            target_ppid = str(shell_pid)
            for f in os.listdir('/proc'):
                if f.isdigit():
                    try:
                        with open(os.path.join('/proc', f, 'status'), 'r', errors='ignore') as stat_f:
                            for line in stat_f:
                                if line.startswith('PPid:'):
                                    ppid = line.split()[1]
                                    if ppid == target_ppid:
                                        return True
                    except Exception:
                        pass
        except Exception:
            pass
        return False

    def allocate_terminal(self, pkg_name, target_branch, command=None):
        """Deduplicates terminal tabs."""
        self.terminal_drawer.set_visible(True)

        matching_tabs = [
            tab for tab in self.terminal_tabs
            if tab["pkg_name"] == pkg_name and tab["target_branch"] == target_branch
        ]

        for tab in matching_tabs:
            if not self.is_shell_pid_active(tab["shell_pid"]):
                page_num = self.notebook.page_num(tab["scroll_widget"])
                if page_num != -1:
                    self.notebook.set_current_page(page_num)
                    if command:
                        tab["terminal"].feed_child(f"{command}\n".encode('utf-8'))
                    tab["terminal"].grab_focus()
                    return

        suffix = ""
        if matching_tabs:
            suffix = f" [{len(matching_tabs) + 1}]"

        self.show_terminal(pkg_name, target_branch, command, suffix)

    def show_terminal(self, pkg_name, target_branch, command=None, suffix=""):
        """Spawns a new VTE terminal tab inside the Gtk.Notebook drawer."""
        resolved_dir = self.get_mapped_worktree_path(pkg_name, target_branch)

        terminal = Vte.Terminal()
        terminal.set_font(Pango.FontDescription.from_string(self.monospace_font))
        terminal.set_scrollback_lines(2000)

        scroll = Gtk.ScrolledWindow()
        scroll.set_child(terminal)

        tab_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        label_text = f"{pkg_name} ({target_branch}){suffix}"
        tab_label = Gtk.Label(label=label_text)
        tab_box.append(tab_label)

        close_tab_btn = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close_tab_btn.add_css_class("flat")
        close_tab_btn.add_css_class("circular")
        close_tab_btn.set_tooltip_text("Close Tab")
        close_tab_btn.connect("clicked", lambda btn: self.close_terminal_tab(scroll))
        tab_box.append(close_tab_btn)

        page_index = self.notebook.append_page(scroll, tab_box)
        self.notebook.set_current_page(page_index)

        key_controller = Gtk.EventControllerKey.new()
        key_controller.connect("key-pressed", self.on_terminal_key_pressed, terminal)
        terminal.add_controller(key_controller)

        tab_state = {
            "scroll_widget": scroll,
            "terminal": terminal,
            "pkg_name": pkg_name,
            "target_branch": target_branch,
            "base_label": f"{pkg_name} ({target_branch}){suffix}",
            "tab_label": tab_label,
            "key_controller": key_controller,
            "shell_pid": None,
            "was_active": False,
            "active_toast": None
        }
        self.terminal_tabs.append(tab_state)

        terminal.connect("child-exited", self.on_terminal_child_exited, scroll)

        shell = os.environ.get("SHELL", "/bin/bash")
        argv = [shell]
        if command:
            argv = [shell, "-c", f"{command}; exec {shell}"]

        terminal.spawn_async(
            Vte.PtyFlags.DEFAULT,
            resolved_dir,
            argv,
            None,
            GLib.SpawnFlags.DEFAULT,
            None,
            None,
            -1,
            None,
            self.on_terminal_spawned,
            tab_state
        )
        terminal.grab_focus()

    def on_terminal_key_pressed(self, controller, keyval, keycode, state, terminal):
        is_ctrl = (state & Gdk.ModifierType.CONTROL_MASK) != 0
        if is_ctrl:
            current_scale = terminal.get_font_scale()
            if keyval in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
                terminal.set_font_scale(min(4.0, current_scale + 0.1))
                return True
            elif keyval in (Gdk.KEY_minus, Gdk.KEY_underscore, Gdk.KEY_KP_Subtract):
                terminal.set_font_scale(max(0.5, current_scale - 0.1))
                return True
            elif keyval in (Gdk.KEY_0, Gdk.KEY_KP_0):
                terminal.set_font_scale(1.0)
                return True
        return False

    def on_terminal_child_exited(self, terminal, status, scroll_widget):
        GLib.idle_add(self.close_terminal_tab, scroll_widget)

    def on_terminal_spawned(self, terminal, pid, error, tab_state):
        if error is None:
            scroll_widget = tab_state.get("scroll_widget")
            if scroll_widget and self.notebook.page_num(scroll_widget) == -1:
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
                return

            tab_state["shell_pid"] = pid
            GLib.idle_add(self.monitor_terminals)
        else:
            scroll_widget = tab_state.get("scroll_widget")
            if scroll_widget:
                GLib.idle_add(self.close_terminal_tab, scroll_widget)

            toast = Adw.Toast.new(f"Terminal spawn failed: {error.message}")
            self.toast_overlay.add_toast(toast)

    def monitor_terminals(self):
        if not hasattr(self, "timeout_id") or not self.timeout_id:
            return False

        for tab in list(self.terminal_tabs):
            shell_pid = tab.get("shell_pid")
            if not shell_pid:
                continue

            if self.notebook.page_num(tab["scroll_widget"]) == -1:
                if tab in self.terminal_tabs:
                    self.terminal_tabs.remove(tab)
                continue

            try:
                reaped_pid, status = os.waitpid(shell_pid, os.WNOHANG)
                if reaped_pid == shell_pid:
                    tab["shell_pid"] = None
                    GLib.idle_add(self.close_terminal_tab, tab["scroll_widget"])
                    continue
            except ChildProcessError:
                tab["shell_pid"] = None
                GLib.idle_add(self.close_terminal_tab, tab["scroll_widget"])
                continue
            except Exception:
                pass

            is_active = self.is_shell_pid_active(shell_pid)
            was_active = tab["was_active"]

            pkg_name = tab["pkg_name"]
            branch = tab["target_branch"]
            label_widget = tab["tab_label"]
            base_lbl = tab["base_label"]

            if is_active and not was_active:
                tab["was_active"] = True
                label_widget.set_text(f"{base_lbl} ⚙️")
            elif not is_active and was_active:
                tab["was_active"] = False
                label_widget.set_text(f"{base_lbl} ✅")

                page_idx = self.notebook.page_num(tab["scroll_widget"])
                is_current_and_visible = (
                    self.terminal_drawer.props.visible and
                    page_idx != -1 and
                    self.notebook.get_current_page() == page_idx
                )
                if not is_current_and_visible:
                    if tab.get("active_toast"):
                        try:
                            tab["active_toast"].dismiss()
                        except Exception:
                            pass
                        tab["active_toast"] = None

                    toast = Adw.Toast.new(f"Task completed in tab: {pkg_name} ({branch})")
                    toast.set_button_label("Focus Tab")
                    toast.connect("button-clicked", self.on_toast_clicked, tab["scroll_widget"])
                    toast.connect("dismissed", lambda t: tab.update({"active_toast": None}))
                    self.toast_overlay.add_toast(toast)
                    tab["active_toast"] = toast

        return True

    def on_toast_clicked(self, toast, scroll_widget):
        page_num = self.notebook.page_num(scroll_widget)
        if page_num != -1:
            self.notebook.set_current_page(page_num)
            self.terminal_drawer.set_visible(True)

    def on_notebook_switch_page(self, notebook, page, page_num):
        for tab in self.terminal_tabs:
            if tab["scroll_widget"] == page:
                active_toast = tab.get("active_toast")
                if active_toast:
                    try:
                        active_toast.dismiss()
                    except Exception:
                        pass
                    tab["active_toast"] = None
                break

    def close_terminal_tab(self, page_widget):
        page_num = self.notebook.page_num(page_widget)
        if page_num != -1:
            self.notebook.remove_page(page_num)

        for tab in list(self.terminal_tabs):
            if tab["scroll_widget"] == page_widget:
                pkg_name = tab["pkg_name"]
                shell_pid = tab.get("shell_pid")
                if shell_pid:
                    try:
                        os.kill(shell_pid, signal.SIGHUP)
                    except Exception:
                        pass

                try:
                    terminal = tab.get("terminal")
                    controller = tab.get("key_controller")
                    if terminal and controller:
                        terminal.remove_controller(controller)

                    if terminal:
                        terminal.destroy()

                    tab["terminal"] = None
                    tab["scroll_widget"].set_child(None)
                    tab["scroll_widget"].unparent()
                except Exception:
                    pass

                if tab in self.terminal_tabs:
                    self.terminal_tabs.remove(tab)

                self.refresh_single_package(pkg_name)
                break

        if self.notebook.get_n_pages() == 0:
            self.hide_terminal()

    def hide_terminal(self):
        self.terminal_drawer.set_visible(False)

    def refresh_all(self):
        # We pre-populate the master list box once
        if not hasattr(self, "package_rows"):
            self.package_rows = {}
            for repo in self.repos:
                row = PackageRow(repo, self)
                self.master_list_box.append(row)
                self.package_rows[repo] = row

        self.start_sync_scan()
        self.start_version_scan()
        self.start_forwarding_scan()

    def refresh_single_package(self, pkg_name):
        """Asynchronously triggers background checks for a single package and updates its row in all relevant lists."""
        if not self.repos or pkg_name not in self.repos:
            return

        try:
            self.executor.submit(self.run_bg_sync_single, pkg_name)
            self.executor.submit(self.run_bg_version_single, pkg_name)
            self.executor.submit(self.run_bg_forward_single, pkg_name)
        except RuntimeError:
            pass

    def run_bg_sync_single(self, repo):
        name, data = sb.check_repo_sync(repo)
        GLib.idle_add(self.update_sync_row_single, name, data)

    def update_sync_row_single(self, name, data):
        if data.get("status") == "success":
            self.package_data[name]["sync"] = data
            self.update_row_ui(name)
            if getattr(self, "current_selected_package", None) == name:
                self.load_package_detail(name)

    def run_bg_version_single(self, repo):
        name, data = sb.check_repo_version(repo)
        GLib.idle_add(self.update_version_row_single, name, data)

    def update_version_row_single(self, name, data):
        if data.get("status") == "success":
            self.package_data[name]["version"] = data
            self.update_row_ui(name)
            if getattr(self, "current_selected_package", None) == name:
                self.load_package_detail(name)

    def run_bg_forward_single(self, repo):
        _, sync_data = sb.check_repo_sync(repo)
        pr_data = {"has_pr": False}
        if sync_data.get("status") == "success" and sync_data.get("next_status") != "No next branch":
            next_ahead = sync_data.get("next_ahead", 0)
            if next_ahead > 0:
                _, pr_data = sb.check_repo_pr(repo)
        GLib.idle_add(self.update_forward_row_single, repo, sync_data, pr_data)

    def update_forward_row_single(self, name, sync_data, pr_data):
        if sync_data.get("status") == "success":
            self.package_data[name]["sync"] = sync_data
        self.package_data[name]["pr"] = pr_data
        self.update_row_ui(name)
        if getattr(self, "current_selected_package", None) == name:
            self.load_package_detail(name)

    def update_row_ui(self, name):
        row = self.package_rows.get(name)
        if row:
            pkg_data = self.package_data.get(name, {})
            row.update_ui(
                pkg_data.get("sync"),
                pkg_data.get("version"),
                pkg_data.get("pr")
            )
            self.queue_filter_invalidation()

    def queue_filter_invalidation(self):
        if getattr(self, "filter_timeout_id", 0) == 0:
            # Coalesce extremely rapid parallel updates into a single layout pass every 120ms
            self.filter_timeout_id = GLib.timeout_add(120, self.flush_filter_invalidation)

    def flush_filter_invalidation(self):
        self.filter_timeout_id = 0
        self.master_list_box.invalidate_filter()
        return False # Run once and terminate

    # --- THE SIDEBAR & DETAILS BUILDERS ---

    def build_sidebar(self):
        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar_box.set_size_request(420, -1)

        # Sidebar Header Bar
        sidebar_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        sidebar_header.set_margin_start(12)
        sidebar_header.set_margin_end(12)
        sidebar_header.set_margin_top(12)
        sidebar_header.set_margin_bottom(6)

        sb_title = Gtk.Label(halign=Gtk.Align.START)
        sb_title.set_hexpand(True)
        sb_title.set_markup("<span weight='bold' size='medium'>Packages</span>")
        sidebar_header.append(sb_title)

        # Legend Popover Button
        self.legend_btn = Gtk.Button.new_from_icon_name("help-about-symbolic")
        self.legend_btn.set_tooltip_text("Show Sync Workflow Legend")
        self.legend_btn.connect("clicked", self.on_legend_btn_clicked)
        sidebar_header.append(self.legend_btn)

        # Refresh All Button
        refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Refresh All Package Scans")
        refresh_btn.connect("clicked", lambda btn: self.refresh_all())
        sidebar_header.append(refresh_btn)

        # Unified scanner progress indicator row (compact and non-redundant)
        progress_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        progress_box.set_margin_start(12)
        progress_box.set_margin_end(12)
        progress_box.set_margin_bottom(6)

        self.sidebar_spinner = Gtk.Spinner()
        self.sidebar_progress_label = Gtk.Label(label="Ready")
        self.sidebar_progress_label.set_halign(Gtk.Align.START)
        self.sidebar_progress_label.set_hexpand(True)
        progress_box.append(self.sidebar_spinner)
        progress_box.append(self.sidebar_progress_label)

        # Control bar (Search + Filter dropdown)
        control_bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        control_bar.set_margin_start(12)
        control_bar.set_margin_end(12)
        control_bar.set_margin_bottom(12)

        self.sidebar_search = Gtk.SearchEntry()
        self.sidebar_search.set_placeholder_text("Search packages...")
        self.sidebar_search.connect("search-changed", lambda entry: self.master_list_box.invalidate_filter())
        control_bar.append(self.sidebar_search)

        # Track-focused filter toggle buttons (multi-select / combination logic)
        filters_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        filters_box.set_margin_top(8)
        filters_box.add_css_class("linked")  # Makes them render as a unified toolbar!

        # 1. Global "Only Needs Action" toggle button
        self.filter_needs_action = Gtk.ToggleButton(label="⚠️ Needs")
        self.filter_needs_action.set_active(True)
        self.filter_needs_action.set_tooltip_text(
            "⚠️ FILTER: NEEDS ACTION ONLY (Global Modifier)\n"
            "─────────────────────────────────────────────\n"
            "When enabled, the package list is strictly filtered to display only those\n"
            "repositories that require immediate attention (e.g. have pending upstream\n"
            "updates, unforwarded commits, or are out of sync with Gitea Pool).\n\n"
            "Toggle OFF to view all matching repositories on your active tracks regardless of action status."
        )
        self.filter_needs_action.connect("toggled", lambda btn: self.master_list_box.invalidate_filter())
        filters_box.append(self.filter_needs_action)

        # 2. Pool Sync toggle button
        self.filter_pool_sync = Gtk.ToggleButton(label="📡 Pool")
        self.filter_pool_sync.set_active(False)
        self.filter_pool_sync.set_tooltip_text(
            "📡 FILTER: GITEA POOL SYNC TRACK\n"
            "────────────────────────────────\n"
            "Filters the package checkout list to display repositories matching our Stage 1 Pool Sync.\n\n"
            "Includes repositories that are:\n"
            "• Behind Pool: Central changes exist on Gitea that need to be pulled.\n"
            "• Ahead of Pool: Local Factory checkouts have commits waiting to be pushed.\n"
            "• Not in Pool: Repositories not yet registered in Gitea's pool."
        )
        self.filter_pool_sync.connect("toggled", lambda btn: self.master_list_box.invalidate_filter())
        filters_box.append(self.filter_pool_sync)

        # 3. Stable Tracking toggle button
        self.filter_stable = Gtk.ToggleButton(label="🟢 Stable")
        self.filter_stable.set_active(False)
        self.filter_stable.set_tooltip_text(
            "🟢 FILTER: STABLE TRACK (Factory ➔ Upstream)\n"
            "────────────────────────────────────────────\n"
            "Filters the package checkout list to track GNOME's stable releases.\n\n"
            "Includes packages matching:\n"
            "• Stable Upstream: Verifies if Factory aligns with the latest stable releases\n"
            "  on release-monitoring.org (e.g., getting 45.1 to 45.2)."
        )
        self.filter_stable.connect("toggled", lambda btn: self.master_list_box.invalidate_filter())
        filters_box.append(self.filter_stable)

        # 4. Unstable Tracking toggle button
        self.filter_unstable = Gtk.ToggleButton(label="🟠 Unst.")
        self.filter_unstable.set_active(False)
        self.filter_unstable.set_tooltip_text(
            "🟠 FILTER: UNSTABLE TRACK (Next ➔ Upstream)\n"
            "───────────────────────────────────────────\n"
            "Filters the package checkout list to track unstable pre-release development.\n\n"
            "Includes packages matching:\n"
            "• Next Branch: Verifies if your local unstable branch aligns with alpha, beta, and\n"
            "  release candidates (RC) upstream (e.g., tracking GNOME 46.beta)."
        )
        self.filter_unstable.connect("toggled", lambda btn: self.master_list_box.invalidate_filter())
        filters_box.append(self.filter_unstable)

        # 5. Forwarding toggle button
        self.filter_forwarding = Gtk.ToggleButton(label="🔀 Fwd.")
        self.filter_forwarding.set_active(False)
        self.filter_forwarding.set_tooltip_text(
            "🔀 FILTER: PENDING COMMIT FORWARDING (Next ➔ Factory)\n"
            "───────────────────────────────────────────────────\n"
            "Filters the package checkout list to display GNOME unstable promotion tracks.\n\n"
            "Includes packages matching:\n"
            "• Next Ahead of Factory: Shows checkouts with unsubmitted developmental commits\n"
            "  sitting on the 'next' branch that need to be merged/cherry-picked to stable 'factory'."
        )
        self.filter_forwarding.connect("toggled", lambda btn: self.master_list_box.invalidate_filter())
        filters_box.append(self.filter_forwarding)

        control_bar.append(filters_box)

        sidebar_box.append(sidebar_header)
        sidebar_box.append(progress_box)
        sidebar_box.append(control_bar)

        # Scrolled List Box
        scroll = Gtk.ScrolledWindow()
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)

        self.master_list_box = Gtk.ListBox()
        self.master_list_box.set_filter_func(self.sidebar_filter_func)
        self.master_list_box.connect("row-selected", self.on_package_row_selected)
        scroll.set_child(self.master_list_box)

        sidebar_box.append(scroll)
        return sidebar_box

    def sidebar_filter_func(self, row):
        package_name = row.package_name

        # 1. Apply Search Text filter first
        search_text = self.sidebar_search.get_text().strip().lower()
        if search_text and search_text not in package_name.lower():
            return False

        pkg_data = self.package_data.get(package_name, {})
        sync = pkg_data.get("sync") or {}
        ver = pkg_data.get("version") or {}

        # Pre-calculate common metrics
        # A. Pool Sync state
        pool_behind = sync.get("pool_behind", 0)
        pool_ahead = sync.get("pool_ahead", 0)
        pool_status = sync.get("pool_status", "unknown")
        is_pool_track = (pool_status != "unknown")
        pool_needs_action = (pool_behind > 0) or (pool_ahead > 0) or (pool_status == "Not in Pool")

        # B. Stable Tracking state
        factory_ver = ver.get("factory_ver", "N/A")
        upstream_stable = ver.get("upstream_stable", "N/A")
        is_stable_track = (factory_ver != "N/A")
        stable_needs_action = (factory_ver != upstream_stable and factory_ver != "N/A" and upstream_stable != "N/A")

        # C. Unstable/Next Tracking state
        next_ver = ver.get("next_ver", "—")
        upstream_latest = ver.get("upstream_latest", "—")
        is_unstable_track = (next_ver != "—")
        unstable_needs_action = (next_ver != "—" and next_ver != upstream_latest and upstream_latest != "—")

        # D. Forwarding state
        next_ahead = sync.get("next_ahead", 0)
        is_forwarding_track = (next_ver != "—")
        forwarding_needs_action = (next_ahead > 0)

        # Determine if we match any of the selected tracks
        # If no tracks are selected, we treat it as matching all tracks!
        any_track_selected = (
            self.filter_pool_sync.get_active() or
            self.filter_stable.get_active() or
            self.filter_unstable.get_active() or
            self.filter_forwarding.get_active()
        )

        matches_track = False
        if not any_track_selected:
            matches_track = True
        else:
            # Check individual selected tracks
            if self.filter_pool_sync.get_active():
                if self.filter_needs_action.get_active():
                    if pool_needs_action:
                        matches_track = True
                else:
                    if is_pool_track:
                        matches_track = True

            if self.filter_stable.get_active():
                if self.filter_needs_action.get_active():
                    if stable_needs_action:
                        matches_track = True
                else:
                    if is_stable_track:
                        matches_track = True

            if self.filter_unstable.get_active():
                if self.filter_needs_action.get_active():
                    if unstable_needs_action:
                        matches_track = True
                else:
                    if is_unstable_track:
                        matches_track = True

            if self.filter_forwarding.get_active():
                if self.filter_needs_action.get_active():
                    if forwarding_needs_action:
                        matches_track = True
                else:
                    if is_forwarding_track:
                        matches_track = True

        # If no tracks are checked, but "Needs Action Only" is checked:
        # We must make sure the package has SOME action pending on ANY track!
        if not any_track_selected and self.filter_needs_action.get_active():
            has_any_action = pool_needs_action or stable_needs_action or unstable_needs_action or forwarding_needs_action
            if not has_any_action:
                return False

        return matches_track

    def build_detail_stable_card(self):
        frame = Gtk.Frame()
        frame.set_margin_start(18)
        frame.set_margin_end(18)
        frame.set_margin_top(4)
        frame.set_margin_bottom(4)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(10)
        box.set_margin_end(10)
        box.set_margin_top(8)
        box.set_margin_bottom(8)

        # Title
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lbl_stable = Gtk.Label()
        lbl_stable.set_markup("<span weight='bold' size='medium' foreground='#2ec27e'>🟢 STABLE PIPELINE (factory)</span>")
        title_box.append(lbl_stable)
        box.append(title_box)

        # Info Grid
        grid = Gtk.Grid(column_spacing=24, row_spacing=4)

        lbl_ver = Gtk.Label(label="Version Alignment:", halign=Gtk.Align.START)
        grid.attach(lbl_ver, 0, 0, 1, 1)
        self.stable_ver_lbl = Gtk.Label(label="Loading...", halign=Gtk.Align.START)
        grid.attach(self.stable_ver_lbl, 1, 0, 1, 1)

        lbl_pool = Gtk.Label(label="Pool Sync State:", halign=Gtk.Align.START)
        grid.attach(lbl_pool, 0, 1, 1, 1)
        self.stable_pool_lbl = Gtk.Label(label="Loading...", halign=Gtk.Align.START)
        grid.attach(self.stable_pool_lbl, 1, 1, 1, 1)

        box.append(grid)

        # Actions Toolbar
        actions_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        actions_box.set_margin_top(6)

        self.pull_pool_btn = Gtk.Button(label="📥 Pull Pool")
        self.pull_pool_btn.connect("clicked", self.on_pull_pool_clicked)
        actions_box.append(self.pull_pool_btn)

        self.update_factory_btn = Gtk.Button(label="⚙️ Run SCM Update")
        self.update_factory_btn.connect("clicked", self.on_detail_update_factory_clicked)
        actions_box.append(self.update_factory_btn)

        self.open_term_fac_btn = Gtk.Button(label="🖥️ Open Factory Terminal")
        self.open_term_fac_btn.connect("clicked", lambda btn: self.allocate_terminal(self.current_selected_package, "factory"))
        actions_box.append(self.open_term_fac_btn)

        box.append(actions_box)
        frame.set_child(box)
        return frame

    def build_detail_unstable_card(self):
        frame = Gtk.Frame()
        frame.set_margin_start(18)
        frame.set_margin_end(18)
        frame.set_margin_top(4)
        frame.set_margin_bottom(4)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(10)
        box.set_margin_end(10)
        box.set_margin_top(8)
        box.set_margin_bottom(8)

        # Title
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lbl_unstable = Gtk.Label()
        lbl_unstable.set_markup("<span weight='bold' size='medium' foreground='#f5c35c'>🟡 UNSTABLE PIPELINE (next)</span>")
        title_box.append(lbl_unstable)
        box.append(title_box)

        # Info Grid
        grid = Gtk.Grid(column_spacing=24, row_spacing=4)

        lbl_ver = Gtk.Label(label="Version Alignment:", halign=Gtk.Align.START)
        grid.attach(lbl_ver, 0, 0, 1, 1)
        self.unstable_ver_lbl = Gtk.Label(label="Loading...", halign=Gtk.Align.START)
        grid.attach(self.unstable_ver_lbl, 1, 0, 1, 1)

        lbl_branch = Gtk.Label(label="Branch Alignment:", halign=Gtk.Align.START)
        grid.attach(lbl_branch, 0, 1, 1, 1)
        self.unstable_branch_lbl = Gtk.Label(label="Loading...", halign=Gtk.Align.START)
        grid.attach(self.unstable_branch_lbl, 1, 1, 1, 1)

        box.append(grid)

        # Actions Toolbar
        actions_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        actions_box.set_margin_top(6)

        self.catchup_merge_btn = Gtk.Button(label="🔀 Catch-up Merge")
        self.catchup_merge_btn.connect("clicked", self.on_catchup_merge_clicked)
        actions_box.append(self.catchup_merge_btn)

        self.update_next_btn = Gtk.Button(label="⚙️ Run SCM Update")
        self.update_next_btn.connect("clicked", self.on_detail_update_next_clicked)
        actions_box.append(self.update_next_btn)

        self.open_term_next_btn = Gtk.Button(label="🖥️ Open Next Terminal")
        self.open_term_next_btn.connect("clicked", lambda btn: self.allocate_terminal(self.current_selected_package, "next"))
        actions_box.append(self.open_term_next_btn)

        self.create_pr_btn = Gtk.Button(label="📤 Create PR")
        self.create_pr_btn.connect("clicked", self.on_detail_create_pr_clicked)
        actions_box.append(self.create_pr_btn)

        box.append(actions_box)
        frame.set_child(box)
        return frame

    def build_detail_diff_panel(self):
        self.diff_revealer = Gtk.Revealer()
        self.diff_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.diff_revealer.set_reveal_child(True)
        self.diff_revealer.set_vexpand(True)
        self.diff_revealer.set_hexpand(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(18)
        box.set_margin_end(18)
        box.set_margin_top(4)
        box.set_margin_bottom(8)
        box.set_vexpand(True)

        # Diff Header Bar
        diff_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

        lbl_diff = Gtk.Label(halign=Gtk.Align.START)
        lbl_diff.set_markup("<span weight='bold'>🔍 DIFF REVIEW:</span>")
        diff_header.append(lbl_diff)

        # DropDown selector (using modern, non-deprecated Gtk.DropDown in GTK4!)
        self.diff_selector = Gtk.DropDown.new_from_strings(["Next vs. Factory (PR Prep)", "Factory vs. Gitea Pool"])
        self.diff_selector.set_selected(0)
        self.diff_selector.connect("notify::selected", self.on_diff_selector_changed)
        diff_header.append(self.diff_selector)

        # Spacer
        spacer = Gtk.Label()
        spacer.set_hexpand(True)
        diff_header.append(spacer)

        # Refresh Diff Button
        self.refresh_diff_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.refresh_diff_btn.set_tooltip_text("Refresh Diff")
        self.refresh_diff_btn.connect("clicked", self.on_refresh_diff_clicked)
        diff_header.append(self.refresh_diff_btn)

        box.append(diff_header)

        # Scrolled View
        diff_scroll = Gtk.ScrolledWindow()
        diff_scroll.set_hexpand(True)
        diff_scroll.set_vexpand(True)
        diff_scroll.set_size_request(-1, 100) # Reduce minimum height constraint to prevent GtkPaned warnings

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
        box.append(diff_scroll)

        self.diff_revealer.set_child(box)
        return self.diff_revealer

    def build_detail_pane(self):
        self.detail_stack = Adw.ViewStack()

        # Page 1: Empty Page (Custom lightweight placeholder)
        self.empty_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.empty_page.set_valign(Gtk.Align.CENTER)
        self.empty_page.set_halign(Gtk.Align.CENTER)

        empty_icon = Gtk.Image.new_from_icon_name("open-menu-symbolic")
        empty_icon.set_pixel_size(64)
        empty_icon.add_css_class("dim-label")
        self.empty_page.append(empty_icon)

        empty_title = Gtk.Label()
        empty_title.set_markup("<span size='large' weight='bold' foreground='gray'>Select a Package</span>")
        self.empty_page.append(empty_title)

        empty_desc = Gtk.Label()
        empty_desc.set_markup("<span size='small' foreground='gray'>Choose a package from the sidebar to review downstream sync, upstream updates, and forwarding.</span>")
        empty_desc.set_justify(Gtk.Justification.CENTER)
        empty_desc.set_wrap(True)
        empty_desc.set_max_width_chars(40)
        self.empty_page.append(empty_desc)

        self.detail_stack.add_named(self.empty_page, "empty")

        # Page 2: Detail Workspace page
        detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Detail Header
        detail_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        detail_header.set_margin_start(18)
        detail_header.set_margin_end(18)
        detail_header.set_margin_top(12)
        detail_header.set_margin_bottom(6)

        self.detail_title_label = Gtk.Label(halign=Gtk.Align.START)
        self.detail_title_label.set_hexpand(True)
        self.detail_title_label.set_markup("<span size='large' weight='bold'>No Package Selected</span>")
        detail_header.append(self.detail_title_label)

        # Open Release Monitoring Button
        self.detail_web_btn = Gtk.Button.new_from_icon_name("web-browser-symbolic")
        self.detail_web_btn.set_tooltip_text("Open Release Monitoring Page")
        self.detail_web_btn.connect("clicked", self.on_detail_web_clicked)
        detail_header.append(self.detail_web_btn)

        detail_box.append(detail_header)

        # Append Stable Pipeline Card
        self.stable_card = self.build_detail_stable_card()
        detail_box.append(self.stable_card)

        # Append Unstable Pipeline Card
        self.unstable_card = self.build_detail_unstable_card()
        detail_box.append(self.unstable_card)

        # Append Diff Review Panel
        self.diff_panel = self.build_detail_diff_panel()
        detail_box.append(self.diff_panel)

        self.detail_stack.add_named(detail_box, "detail")
        return self.detail_stack

    def on_diff_selector_changed(self, dropdown, pspec):
        if getattr(self, "current_selected_package", None):
            self.refresh_active_diff()

    def on_refresh_diff_clicked(self, btn):
        if getattr(self, "current_selected_package", None):
            self.refresh_active_diff()

    def refresh_active_diff(self):
        package_name = self.current_selected_package
        if not package_name:
            return

        active_idx = self.diff_selector.get_selected()
        perspective = "next_factory" if active_idx == 0 else "factory_pool"
        self.diff_buffer.set_text("Loading diff...")
        self.executor.submit(self.run_bg_diff_perspective, package_name, perspective)

    def run_bg_diff_perspective(self, package_name, perspective):
        if perspective == "next_factory":
            diff_text = sb.get_git_diff(package_name)
        else:
            diff_text = ""
            repo_path = os.path.join('.', package_name)
            try:
                gitea_name = sb.get_gitea_repo_name(repo_path, package_name)
                pool_url = f"https://src.opensuse.org/pool/{gitea_name}.git"
                sb.run_tracked(
                    ['git', '-C', repo_path, 'fetch', '--quiet', pool_url, 'factory'],
                    check=True, capture_output=True
                )
                res = sb.run_tracked(
                    ['git', '-C', repo_path, 'diff', 'FETCH_HEAD...origin/factory'],
                    check=True, capture_output=True, text=True
                )
                diff_text = res.stdout
                if not diff_text.strip():
                    diff_text = "No differences in spec files or sources detected between local Factory and remote Pool."
            except Exception as e:
                diff_text = f"Error performing git diff (Factory vs Pool): {str(e)}"

        GLib.idle_add(self.update_diff_text, diff_text)

    def on_pull_pool_clicked(self, btn):
        if getattr(self, "current_selected_package", None):
            gitea_name = sb.get_gitea_repo_name(os.path.join('.', self.current_selected_package), self.current_selected_package)
            pool_url = f"https://src.opensuse.org/pool/{gitea_name}.git"
            command = f"git fetch {pool_url} factory && git merge FETCH_HEAD"
            self.allocate_terminal(self.current_selected_package, "factory", command)

    def on_catchup_merge_clicked(self, btn):
        if getattr(self, "current_selected_package", None):
            command = "git fetch origin && git merge origin/factory --no-edit"
            self.allocate_terminal(self.current_selected_package, "next", command)

    # --- THE CORE EVENT HANDLERS & LOADERS ---

    def on_legend_btn_clicked(self, btn):
        """Pops up a modern, elegant, and interactive workflow legend panel on demand."""
        popover = Gtk.Popover()
        popover.set_parent(btn)

        legend_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        legend_box.set_margin_start(16)
        legend_box.set_margin_end(16)
        legend_box.set_margin_top(16)
        legend_box.set_margin_bottom(16)
        legend_box.set_size_request(450, -1)

        legend_title = Gtk.Label(halign=Gtk.Align.START)
        legend_title.set_markup("<span weight='bold' size='medium'>ℹ️ openSUSE GNOME Sync Workflow Legend</span>")
        legend_box.append(legend_title)

        legend_desc = Gtk.Label(halign=Gtk.Align.START)
        legend_desc.set_justify(Gtk.Justification.LEFT)
        legend_desc.set_wrap(True)
        legend_desc.set_markup(
            "<span weight='bold'>Pool Sync (Stage 1):</span> Monitors alignment between local Factory checkouts and Gitea's central package pool.\n"
            "   • <span foreground='orange' weight='bold'>Behind Pool</span>: SCM changes exist in the pool—pull them to catch up.\n"
            "   • <span foreground='cyan' weight='bold'>Ahead of Pool</span>: Local Factory has local commits not yet in the pool—submit them.\n\n"
            "<span weight='bold'>Next Branch Sync (Stage 2):</span> Monitors alignment between the unstable next track and stable factory branch.\n"
            "   • <span foreground='orange' weight='bold'>Next behind Factory</span>: Next is missing commits from Factory—merge factory ➔ next.\n"
            "   • <span foreground='green' weight='bold'>Next ahead of Factory</span>: Next has additional developmental commits checked in (OK)."
        )
        legend_box.append(legend_desc)

        popover.set_child(legend_box)
        popover.popup()

    def on_package_row_selected(self, list_box, row):
        if not row:
            self.detail_stack.set_visible_child(self.empty_page)
            self.current_selected_package = None
            return
        self.load_package_detail(row.package_name)

    def load_package_detail(self, package_name):
        self.current_selected_package = package_name
        self.detail_stack.set_visible_child_name("detail")

        self.detail_title_label.set_markup(f"<span size='large' weight='bold'>{package_name}</span>")

        pkg_data = self.package_data.get(package_name, {})
        sync = pkg_data.get("sync") or {}
        ver = pkg_data.get("version") or {}
        pr = pkg_data.get("pr") or {}

        # -------------------------------------------------------------
        # 1. POPULATE STABLE CARD
        # -------------------------------------------------------------
        factory_ver = ver.get("factory_ver", "N/A")
        upstream_stable = ver.get("upstream_stable", "N/A")

        if not ver:
            self.stable_ver_lbl.set_text("Loading...")
            self.update_factory_btn.set_sensitive(False)
            self.update_factory_btn.set_label("Run SCM Update")
            self.detail_web_btn.set_sensitive(False)
        else:
            self.detail_web_btn.set_sensitive(True)
            if factory_ver != upstream_stable and factory_ver != "N/A" and upstream_stable != "N/A":
                self.stable_ver_lbl.set_markup(f"<span weight='bold' foreground='red'>{factory_ver}</span> ➔ <span weight='bold' foreground='green'>{upstream_stable} (Update Available)</span>")
                self.update_factory_btn.set_sensitive(True)
                self.update_factory_btn.set_label(f"Update Factory to {upstream_stable}")
            else:
                self.stable_ver_lbl.set_text(f"{factory_ver} (Up-To-Date)")
                self.update_factory_btn.set_sensitive(False)
                self.update_factory_btn.set_label("Factory Up-To-Date")

        pool_status = sync.get("pool_status", "unknown")
        pool_behind = sync.get("pool_behind", 0)
        pool_ahead = sync.get("pool_ahead", 0)

        if not sync:
            self.stable_pool_lbl.set_text("Loading...")
            self.pull_pool_btn.set_sensitive(False)
        else:
            if pool_behind > 0 and pool_ahead > 0:
                p1_text = f"<span weight='bold' foreground='red'>Diverged</span> (Behind Pool: {pool_behind} / Ahead Pool: {pool_ahead} commits)"
                self.pull_pool_btn.set_sensitive(True)
            elif pool_behind > 0:
                p1_text = f"<span weight='bold' foreground='orange'>Behind Gitea Pool by {pool_behind} commits</span> (Pull needed)"
                self.pull_pool_btn.set_sensitive(True)
            elif pool_ahead > 0:
                p1_text = f"<span weight='bold' foreground='cyan'>Ahead of Gitea Pool by {pool_ahead} commits</span> (Push/PR needed)"
                self.pull_pool_btn.set_sensitive(False)
            elif pool_status == "Not in Pool":
                p1_text = "<span foreground='yellow' weight='bold'>Not in Gitea Pool</span>"
                self.pull_pool_btn.set_sensitive(False)
            else:
                p1_text = "<span foreground='green'>Fully In Sync with Gitea Pool</span>"
                self.pull_pool_btn.set_sensitive(False)
            self.stable_pool_lbl.set_markup(p1_text)

        # -------------------------------------------------------------
        # 2. POPULATE UNSTABLE CARD
        # -------------------------------------------------------------
        next_ver = ver.get("next_ver", "—")
        upstream_latest = ver.get("upstream_latest", "—")

        if not ver:
            self.unstable_ver_lbl.set_text("Loading...")
            self.update_next_btn.set_sensitive(False)
            self.update_next_btn.set_label("Run SCM Update")
        else:
            if next_ver != "—" and next_ver != upstream_latest and upstream_latest != "—":
                self.unstable_ver_lbl.set_markup(f"<span weight='bold' foreground='red'>{next_ver}</span> ➔ <span weight='bold' foreground='green'>{upstream_latest} (Update Available)</span>")
                self.update_next_btn.set_sensitive(True)
                self.update_next_btn.set_label(f"Update Next to {upstream_latest}")
            else:
                self.unstable_ver_lbl.set_text(f"{next_ver} (Up-To-Date)" if next_ver != "—" else "—")
                self.update_next_btn.set_sensitive(False)
                self.update_next_btn.set_label("Next Up-To-Date")

            if next_ver == "—":
                self.update_next_btn.set_sensitive(False)
                self.update_next_btn.set_label("No Next branch")

        next_status = sync.get("next_status", "unknown")
        next_behind = sync.get("next_behind", 0)
        next_ahead_val = sync.get("next_ahead", 0)
        has_pr = pr.get("has_pr", False)
        pr_number = pr.get("number", None)

        if not sync:
            self.unstable_branch_lbl.set_text("Loading...")
            self.catchup_merge_btn.set_sensitive(False)
            self.create_pr_btn.set_sensitive(False)
        else:
            # Catchup Merge Trigger
            if next_behind > 0:
                self.catchup_merge_btn.set_sensitive(True)
                self.catchup_merge_btn.set_label(f"🔀 Catch-up Merge ({next_behind} behind)")
            else:
                self.catchup_merge_btn.set_sensitive(False)
                self.catchup_merge_btn.set_label("🔀 Catch-up Merge")

            # PR Forwarding Trigger
            if next_ahead_val > 0:
                if has_pr:
                    branch_text = f"<span foreground='green' weight='bold'>PR #{pr_number} Active</span> | Next is ahead of Factory by {next_ahead_val} commits."
                    self.create_pr_btn.set_label(f"View PR #{pr_number}")
                else:
                    branch_text = f"<span foreground='orange' weight='bold'>Forwarding Needed</span> | Next is ahead of Factory by {next_ahead_val} commits."
                    self.create_pr_btn.set_label("Create Pull Request")
                self.create_pr_btn.set_sensitive(True)
            else:
                self.create_pr_btn.set_sensitive(False)
                self.create_pr_btn.set_label("Create PR")
                if next_status == "No next branch":
                    branch_text = "<span foreground='gray'>No Next branch present</span>"
                else:
                    branch_text = "<span foreground='green'>Next and Factory branches are fully in sync</span>"

            if next_behind > 0 and next_ahead_val > 0:
                branch_text = f"<span weight='bold' foreground='red'>Diverged</span> (Behind: {next_behind} / Ahead: {next_ahead_val} commits)"
                self.create_pr_btn.set_sensitive(False) # Must merge first!

            self.unstable_branch_lbl.set_markup(branch_text)

        # -------------------------------------------------------------
        # 3. POPULATE DIFF REVIEW
        # -------------------------------------------------------------
        if not sync:
            self.diff_buffer.set_text("")
        else:
            self.refresh_active_diff()

    def on_detail_update_next_clicked(self, btn):
        if getattr(self, "current_selected_package", None):
            ver_data = self.package_data[self.current_selected_package].get("version") or {}
            upstream_latest = ver_data.get("upstream_latest", "")
            if upstream_latest and upstream_latest != "—":
                pkg_dir = self.get_mapped_worktree_path(self.current_selected_package, "next")
                current_revision = get_service_revision(pkg_dir)
                guessed_revision, confidence_err = guess_update_revision(current_revision, upstream_latest)

                if guessed_revision and not confidence_err:
                    command = f"obs_scm-update.sh {shlex.quote(guessed_revision)}"
                    self.allocate_terminal(self.current_selected_package, "next", command)
                else:
                    title_text = f"⚠️  Unable to confidently guess target revision for {self.current_selected_package}."
                    lines = [
                        f"\033[1;33m{title_text}\033[0m",
                    ]
                    if confidence_err:
                        lines.append(f"   Reason: _service {confidence_err}.")
                    if current_revision:
                        lines.append(f"   Current revision in _service: \033[1m{current_revision}\033[0m")
                    lines.append(f"   Suggested target version:     \033[1;32m{upstream_latest}\033[0m")
                    lines.append("")
                    lines.append("👉 \033[1mRun obs_scm-update.sh manually with your preferred parameter.\033[0m")

                    hint_msg = " && ".join(f"echo {shlex.quote(line)}" for line in lines)
                    self.allocate_terminal(self.current_selected_package, "next", hint_msg)

    def on_detail_update_factory_clicked(self, btn):
        if getattr(self, "current_selected_package", None):
            ver_data = self.package_data[self.current_selected_package].get("version") or {}
            upstream_stable = ver_data.get("upstream_stable", "")
            if upstream_stable and upstream_stable != "N/A":
                pkg_dir = self.get_mapped_worktree_path(self.current_selected_package, "factory")
                current_revision = get_service_revision(pkg_dir)
                guessed_revision, confidence_err = guess_update_revision(current_revision, upstream_stable)

                if guessed_revision and not confidence_err:
                    command = f"obs_scm-update.sh {shlex.quote(guessed_revision)}"
                    self.allocate_terminal(self.current_selected_package, "factory", command)
                else:
                    title_text = f"⚠️  Unable to confidently guess target revision for {self.current_selected_package}."
                    lines = [
                        f"\033[1;33m{title_text}\033[0m",
                    ]
                    if confidence_err:
                        lines.append(f"   Reason: _service {confidence_err}.")
                    if current_revision:
                        lines.append(f"   Current revision in _service: \033[1m{current_revision}\033[0m")
                    lines.append(f"   Suggested target version:     \033[1;32m{upstream_stable}\033[0m")
                    lines.append("")
                    lines.append("👉 \033[1mRun obs_scm-update.sh manually with your preferred parameter.\033[0m")

                    hint_msg = " && ".join(f"echo {shlex.quote(line)}" for line in lines)
                    self.allocate_terminal(self.current_selected_package, "factory", hint_msg)

    def on_detail_web_clicked(self, btn):
        if getattr(self, "current_selected_package", None):
            ver_data = self.package_data[self.current_selected_package].get("version") or {}
            project_name = ver_data.get("project", self.current_selected_package)
            url = f"https://release-monitoring.org/project/{project_name}/"
            webbrowser.open(url)

    def on_detail_create_pr_clicked(self, btn):
        if getattr(self, "current_selected_package", None):
            pkg_data = self.package_data.get(self.current_selected_package, {})
            pr = pkg_data.get("pr") or {}
            pr_url = pr.get("url")
            if pr.get("has_pr") and pr_url:
                url = pr_url
            else:
                url = sb.get_gitea_pr_url(self.current_selected_package)
            webbrowser.open(url)

    def on_detail_pool_diff_clicked(self, btn):
        if getattr(self, "current_selected_package", None):
            pkg_data = self.package_data[self.current_selected_package]
            sync_data = pkg_data.get("sync") or {}
            dialog = SyncDiffDialog(self, self.current_selected_package, sync_data)
            dialog.present()

    # --- UNIFIED SCAN PROGRESS MONITORING ---

    def update_progress_ui(self):
        sync_total = len(self.repos) if self.repos else 0
        sync_done = getattr(self, "sync_completed_count", 0)
        ver_done = getattr(self, "ver_completed_count", 0)

        sync_running = sync_done < sync_total
        ver_running = ver_done < sync_total

        if sync_running or ver_running:
            self.sidebar_spinner.start()
            parts = []
            if sync_running:
                parts.append(f"Sync: {sync_done}/{sync_total}")
            else:
                parts.append("Sync: Done")

            if ver_running:
                parts.append(f"Versions: {ver_done}/{sync_total}")
            else:
                parts.append("Versions: Done")

            self.sidebar_progress_label.set_text(" | ".join(parts))
        else:
            self.sidebar_spinner.stop()
            self.sidebar_progress_label.set_text("Scan Completed")

    def start_sync_scan(self):
        if not self.repos:
            self.sidebar_progress_label.set_text("No packages found")
            return

        self.sync_completed_count = 0
        self.update_progress_ui()

        for repo in self.repos:
            try:
                self.executor.submit(self.run_bg_sync, repo)
            except RuntimeError:
                break

    def run_bg_sync(self, repo):
        name, data = sb.check_repo_sync(repo)
        GLib.idle_add(self.add_sync_result, name, data)

    def add_sync_result(self, name, data):
        self.sync_completed_count += 1

        if data.get("status") == "success":
            self.package_data[name]["sync"] = data
            self.update_row_ui(name)
            if getattr(self, "current_selected_package", None) == name:
                self.load_package_detail(name)

        self.update_progress_ui()

    def start_version_scan(self):
        if not self.repos:
            return

        self.ver_completed_count = 0
        self.update_progress_ui()

        for repo in self.repos:
            try:
                self.executor.submit(self.run_bg_version, repo)
            except RuntimeError:
                break

    def run_bg_version(self, repo):
        name, data = sb.check_repo_version(repo)
        GLib.idle_add(self.add_version_result, name, data)

    def add_version_result(self, name, data):
        self.ver_completed_count += 1

        if data.get("status") == "success":
            self.package_data[name]["version"] = data
            self.update_row_ui(name)
            if getattr(self, "current_selected_package", None) == name:
                self.load_package_detail(name)

        self.update_progress_ui()

    # --- TAB 3 SCAN METHODS ---

    def start_forwarding_scan(self):
        if not self.repos:
            return

        self.fwd_completed_count = 0
        for repo in self.repos:
            try:
                self.executor.submit(self.run_bg_forward, repo)
            except RuntimeError:
                break

    def run_bg_forward(self, repo):
        _, sync_data = sb.check_repo_sync(repo)
        pr_data = {"has_pr": False}
        if sync_data.get("status") == "success" and sync_data.get("next_status") != "No next branch":
            next_ahead = sync_data.get("next_ahead", 0)
            if next_ahead > 0:
                _, pr_data = sb.check_repo_pr(repo)
        GLib.idle_add(self.add_forward_result, repo, sync_data, pr_data)

    def add_forward_result(self, name, sync_data, pr_data):
        self.fwd_completed_count += 1

        if sync_data.get("status") == "success":
            self.package_data[name]["sync"] = sync_data
        self.package_data[name]["pr"] = pr_data
        self.update_row_ui(name)

        if getattr(self, "current_selected_package", None) == name:
            self.load_package_detail(name)

    def run_bg_diff(self, package_name):
        diff_text = sb.get_git_diff(package_name)
        GLib.idle_add(self.update_diff_text, diff_text)

    def update_diff_text(self, text):
        self.diff_buffer.set_text(text)


class SyncApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=None)

    def do_activate(self):
        win = SyncWindow(self)
        win.present()

if __name__ == '__main__':
    GLib.set_prgname("GNOME Sync Dashboard")
    GLib.set_application_name("GNOME Sync Dashboard")
    app = SyncApp()
    sys.argv = [sys.argv[0]]  # Strip extra args to prevent GTK app parsing issues
    sys.exit(app.run(sys.argv))
