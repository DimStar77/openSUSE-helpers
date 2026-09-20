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
import json
from pathlib import Path

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

def clean_version(version_str, repo_name=None):
    """Normalize version string by leveraging our centralized sync_backend custom overrides."""
    return sb.clean_version(version_str, repo_name)

def apply_source_view_style_scheme(buffer):
    """Dynamically applies GtkSourceView style schemes based on system dark/light preference."""
    if not GtkSource or not buffer or not hasattr(buffer, "set_style_scheme"):
        return
    style_manager = Adw.StyleManager.get_default()
    is_dark = style_manager.get_dark()
    scheme_id = "oblivion" if is_dark else "classic"

    scheme_manager = GtkSource.StyleSchemeManager.get_default()
    scheme = scheme_manager.get_scheme(scheme_id)
    if scheme:
        buffer.set_style_scheme(scheme)

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

            if factory_ver != "N/A" and upstream_stable != "N/A" and clean_version(factory_ver, self.package_name) != clean_version(upstream_stable, self.package_name):
                subtitle_parts.append("Stable Update")
                self.badges_box.append(self.create_badge("Stable 🔺", "green"))

            if next_ver != "—" and upstream_latest != "—" and clean_version(next_ver, self.package_name) != clean_version(upstream_latest, self.package_name):
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


class SyncCreatePRDialog(Gtk.Window):
    """Modal dialog to prefill, review branch changes, and programmatically create a Gitea PR."""
    def __init__(self, parent, package_name):
        super().__init__(transient_for=parent, modal=True, title=f"Create Pull Request - {package_name}")
        self.set_default_size(840, 680)

        self.package_name = package_name
        self.parent = parent
        self.is_destroyed = False

        self.connect("destroy", self.on_destroy)

        # Main layout
        main_layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        main_layout.set_margin_top(12)
        main_layout.set_margin_bottom(12)
        main_layout.set_margin_start(18)
        main_layout.set_margin_end(18)

        # Header Title
        title_lbl = Gtk.Label(halign=Gtk.Align.START)
        title_lbl.set_markup(f"<span size='large' weight='bold'>Prefill Pull Request for {package_name}</span>")
        main_layout.append(title_lbl)

        # Grid for prefilled branch mapping & PR Title
        grid = Gtk.Grid(column_spacing=18, row_spacing=12)
        grid.set_margin_top(6)
        grid.set_margin_bottom(6)

        # 1. Source Branch (Head)
        src_lbl = Gtk.Label(halign=Gtk.Align.START)
        src_lbl.set_markup("<span weight='bold'>Source Branch (Head):</span>")
        src_val = Gtk.Label(label=self.parent.unstable_b, halign=Gtk.Align.START)
        grid.attach(src_lbl, 0, 0, 1, 1)
        grid.attach(src_val, 1, 0, 1, 1)

        # 2. Target Branch (Base)
        tgt_lbl = Gtk.Label(halign=Gtk.Align.START)
        tgt_lbl.set_markup("<span weight='bold'>Target Branch (Base):</span>")
        tgt_val = Gtk.Label(label=self.parent.stable_b, halign=Gtk.Align.START)
        grid.attach(tgt_lbl, 2, 0, 1, 1)
        grid.attach(tgt_val, 3, 0, 1, 1)

        # 3. PR Title
        title_input_lbl = Gtk.Label(halign=Gtk.Align.START)
        title_input_lbl.set_markup("<span weight='bold'>PR Title:</span>")
        self.title_entry = Gtk.Entry()
        self.title_entry.set_hexpand(True)
        self.title_entry.set_text(f"Forward {self.parent.unstable_b} to {self.parent.stable_b}: {package_name}")
        grid.attach(title_input_lbl, 0, 1, 1, 1)
        grid.attach(self.title_entry, 1, 1, 3, 1)

        # 4. PR Description
        desc_input_lbl = Gtk.Label(halign=Gtk.Align.START)
        desc_input_lbl.set_markup("<span weight='bold'>Description:</span>")

        self.desc_buffer = Gtk.TextBuffer()
        self.desc_buffer.set_text(f"Automated {self.parent.unstable_b}-to-{self.parent.stable_b} branch forwarding for {package_name} via Geckopit.")
        self.desc_view = Gtk.TextView(buffer=self.desc_buffer)
        self.desc_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)

        desc_scroll = Gtk.ScrolledWindow()
        desc_scroll.set_child(self.desc_view)
        desc_scroll.set_size_request(-1, 80)
        grid.attach(desc_input_lbl, 0, 2, 1, 1)
        grid.attach(desc_scroll, 1, 2, 3, 1)

        main_layout.append(grid)

        # Diff View Label
        diff_lbl = Gtk.Label(halign=Gtk.Align.START)
        diff_lbl.set_markup("<span size='medium' weight='bold'>Review Branch Changes (Diff):</span>")
        main_layout.append(diff_lbl)

        # Diff View Scrolled Window
        diff_scroll = Gtk.ScrolledWindow()
        diff_scroll.set_hexpand(True)
        diff_scroll.set_vexpand(True)

        if GtkSource:
            lang_manager = GtkSource.LanguageManager.get_default()
            lang = lang_manager.get_language('diff')
            self.diff_buffer = GtkSource.Buffer()
            self.diff_buffer.set_language(lang)
            self.diff_buffer.set_highlight_syntax(True)
            self.diff_view = GtkSource.View(buffer=self.diff_buffer)
            self.diff_view.set_show_line_numbers(True)
            self.diff_view.set_highlight_current_line(True)
        else:
            self.diff_buffer = Gtk.TextBuffer()
            self.diff_view = Gtk.TextView(buffer=self.diff_buffer)

        self.diff_view.set_monospace(True)
        self.diff_view.set_editable(False)
        diff_scroll.set_child(self.diff_view)
        main_layout.append(diff_scroll)

        # Synchronize GtkSourceView theme with system dark/light mode preference!
        style_manager = Adw.StyleManager.get_default()
        style_manager.connect("notify::dark", lambda sm, pspec: apply_source_view_style_scheme(self.diff_buffer))
        apply_source_view_style_scheme(self.diff_buffer)

        # Footer Button Action Bar
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        footer.set_halign(Gtk.Align.END)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda btn: self.destroy())
        footer.append(cancel_btn)

        self.create_btn = Gtk.Button(label="Create Pull Request")
        self.create_btn.add_css_class("suggested-action")
        self.create_btn.connect("clicked", self.on_create_pr_clicked)
        footer.append(self.create_btn)

        main_layout.append(footer)
        self.set_child(main_layout)

        # Start loading diff asynchronously on a priority thread to bypass the background executor queue!
        self.diff_buffer.set_text("Connecting to pool & loading differences...")
        threading.Thread(target=self.load_diff_data, daemon=True).start()

    def on_destroy(self, widget):
        self.is_destroyed = True

    def load_diff_data(self):
        diff_text = sb.get_git_diff(self.package_name, stable_branch=self.parent.stable_b, unstable_branch=self.parent.unstable_b, workspace_path=self.parent.stable_p)
        GLib.idle_add(self.update_diff_text, diff_text)

    def update_diff_text(self, text):
        if not self.is_destroyed:
            self.diff_buffer.set_text(text)

    def on_create_pr_clicked(self, btn):
        title = self.title_entry.get_text().strip()

        start_iter = self.desc_buffer.get_start_iter()
        end_iter = self.desc_buffer.get_end_iter()
        description = self.desc_buffer.get_text(start_iter, end_iter, True).strip()

        if not title:
            toast = Adw.Toast.new("Pull Request title cannot be empty.")
            self.parent.toast_overlay.add_toast(toast)
            return

        self.create_btn.set_sensitive(False)
        self.create_btn.set_label("Creating PR...")
        self.title_entry.set_sensitive(False)
        self.desc_view.set_sensitive(False)

        # Final pre-submit double-check to prevent race conditions (run on priority thread to bypass background queue)
        threading.Thread(target=self.run_bg_pre_submit_check, args=(title, description), daemon=True).start()

    def run_bg_pre_submit_check(self, title, description):
        _, pr_data = sb.check_repo_pr(self.package_name, stable_branch=self.parent.stable_b, unstable_branch=self.parent.unstable_b, workspace_path=self.parent.stable_p)
        GLib.idle_add(self.on_pre_submit_check_result, pr_data, title, description)

    def on_pre_submit_check_result(self, pr_data, title, description):
        if self.is_destroyed:
            return

        if pr_data.get("has_pr"):
            # A PR was created in the meantime by someone else!
            toast = Adw.Toast.new("A Pull Request has just been created by someone else!")
            pr_url = pr_data.get("url")
            if pr_url:
                toast.set_button_label("Open PR")
                toast.connect("button-clicked", lambda t, u: webbrowser.open(u), pr_url)
            self.parent.toast_overlay.add_toast(toast)
            self.destroy()
            self.parent.refresh_single_package(self.package_name)
        else:
            # No existing PR, proceed to submit (run on priority thread to bypass background queue)
            threading.Thread(target=self.run_bg_create_pr, args=(title, description), daemon=True).start()

    def run_bg_create_pr(self, title, description):
        success, res_msg = sb.create_gitea_pr(self.package_name, title, description, stable_branch=self.parent.stable_b, unstable_branch=self.parent.unstable_b, workspace_path=self.parent.stable_p)
        GLib.idle_add(self.on_pr_created_result, success, res_msg)

    def on_pr_created_result(self, success, res_msg):
        if self.is_destroyed:
            return

        if success:
            toast = Adw.Toast.new("Pull Request created successfully!")
            
            # Clean ANSI escape sequences and OSC 8 hyperlinks to prevent duplicate/invalid URLs
            clean_msg = res_msg
            # 1. Clean OSC 8 hyperlink wrapper and keep only the anchor/target URL
            clean_msg = re.sub(r'\x1b\]8;;([^\x07]*)\x07(.*?)\x1b\]8;;\x07', r'\2', clean_msg)
            # 2. Clean CSI color sequences (e.g. \x1b[32m)
            clean_msg = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', clean_msg)
            # 3. Clean any other trailing non-printable control sequences like bell or esc
            clean_msg = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', clean_msg)

            url_match = re.search(r'https?://[^\s]+', clean_msg)
            if url_match:
                pr_url = url_match.group(0)
                toast.set_button_label("Open PR")
                toast.connect("button-clicked", lambda t, u: webbrowser.open(u), pr_url)

            self.parent.toast_overlay.add_toast(toast)
            self.destroy()
            self.parent.refresh_single_package(self.package_name)
        else:
            self.create_btn.set_sensitive(True)
            self.create_btn.set_label("Create Pull Request")
            self.title_entry.set_sensitive(True)
            self.desc_view.set_sensitive(True)

            toast = Adw.Toast.new(f"Failed to create PR: {res_msg}")
            self.parent.toast_overlay.add_toast(toast)


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

        # Synchronize GtkSourceView theme with system dark/light mode preference!
        style_manager = Adw.StyleManager.get_default()
        style_manager.connect("notify::dark", lambda sm, pspec: apply_source_view_style_scheme(self.buffer))
        apply_source_view_style_scheme(self.buffer)

        self.set_child(box)

        # Load diff text asynchronously on a priority thread to bypass the background queue!
        threading.Thread(target=self.load_diff_data, daemon=True).start()

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


        if not self.is_destroyed:
            GLib.idle_add(self.update_ui, comparison_desc, diff_text)

    def update_ui(self, desc, text):
        if not self.is_destroyed:
            self.title_label.set_markup(f"<span size='large' weight='bold'>{desc}</span>")
            self.buffer.set_text(text)


class WorkspaceConfig:
    def __init__(self):
        self.active_workspace = "Default"
        self.workspaces = {
            "Default": {
                "stable_path": "",
                "stable_branch": "factory",
                "unstable_path": "",
                "unstable_branch": "next"
            }
        }
        self.load()

    def load(self):
        path = Path.home() / ".config" / "geckopit.json"
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.active_workspace = data.get("active_workspace", "Default")
                    self.workspaces = data.get("workspaces", self.workspaces)
            except Exception:
                pass

    def save(self):
        path = Path.home() / ".config" / "geckopit.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({
                    "active_workspace": self.active_workspace,
                    "workspaces": self.workspaces
                }, f, indent=2)
        except Exception:
            pass

    def get_active_profile(self):
        return self.workspaces.get(self.active_workspace, {
            "stable_path": "",
            "stable_branch": "factory",
            "unstable_path": "",
            "unstable_branch": "next"
        })

    def autodetect_and_migrate(self):
        active_prof = self.get_active_profile()
        if active_prof.get("stable_path"):
            return

        cwd = os.path.abspath('.')
        parent_dir, current_folder_name = os.path.split(cwd)

        is_gnome_style = False
        stable_guess = ""
        unstable_guess = ""

        if current_folder_name == "GNOME":
            stable_guess = cwd
            unstable_sibling = os.path.join(parent_dir, "GNOME:Next")
            if os.path.exists(unstable_sibling):
                unstable_guess = unstable_sibling
            is_gnome_style = True
        elif current_folder_name == "GNOME:Next":
            unstable_guess = cwd
            stable_sibling = os.path.join(parent_dir, "GNOME")
            if os.path.exists(stable_sibling):
                stable_guess = stable_sibling
            is_gnome_style = True

        if is_gnome_style:
            # Autodetection succeeded! Overwrite generic "Default" so we have a clean GNOME profile
            self.workspaces = {
                "GNOME": {
                    "stable_path": stable_guess,
                    "stable_branch": "factory",
                    "unstable_path": unstable_guess if unstable_guess else None,
                    "unstable_branch": "next" if unstable_guess else None
                }
            }
            self.active_workspace = "GNOME"
            self.save()
            return

        has_sub_repos = any(os.path.isdir(d) and os.path.exists(os.path.join(d, '.git')) for d in os.listdir('.'))
        if has_sub_repos:
            # Autodetection succeeded for local sub-repos! Overwrite generic "Default"
            folder_name = current_folder_name
            self.workspaces = {
                folder_name: {
                    "stable_path": cwd,
                    "stable_branch": "factory",
                    "unstable_path": None,
                    "unstable_branch": None
                }
            }
            self.active_workspace = folder_name
            self.save()


def detect_git_branch(path):
    """Parses .git/HEAD in pure Python to detect the active git branch name without subprocess overhead."""
    if not path or not os.path.exists(path):
        return None
    try:
        git_dir = os.path.join(path, '.git')
        if os.path.isfile(git_dir):
            with open(git_dir, 'r') as f:
                line = f.read().strip()
            if line.startswith('gitdir:'):
                git_dir = line.split(':', 1)[1].strip()
                if not os.path.isabs(git_dir):
                    git_dir = os.path.abspath(os.path.join(path, git_dir))

        head_path = os.path.join(git_dir, 'HEAD')
        if os.path.exists(head_path):
            with open(head_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            if content.startswith('ref:'):
                ref_part = content.split('ref:', 1)[1].strip()
                return ref_part.split('/')[-1]
            elif len(content) == 40 and all(c in "0123456789abcdefABCDEF" for c in content):
                # Detached HEAD state: return short 7-character commit SHA-1 hash!
                return content[:7]
    except Exception:
        pass
    return None


class WorkspaceManagerDialog(Gtk.Window):
    def __init__(self, parent_window, config, callback_on_save):
        super().__init__(transient_for=parent_window, modal=True, title="Workspace Manager Settings")
        self.set_default_size(550, 480)
        self.config = config
        self.parent_window = parent_window
        self.callback_on_save = callback_on_save
        self.ws_names = sorted(self.config.workspaces.keys())

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        main_box.set_margin_start(18)
        main_box.set_margin_end(18)
        main_box.set_margin_top(18)
        main_box.set_margin_bottom(18)

        title_lbl = Gtk.Label(halign=Gtk.Align.START)
        title_lbl.set_markup("<span size='large' weight='bold'>Manage Workspace Profiles</span>")
        main_box.append(title_lbl)

        selector_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        lbl_sel = Gtk.Label(label="Selected Profile:", halign=Gtk.Align.START)
        selector_box.append(lbl_sel)

        # DropDown for profiles list
        self.profile_combo = Gtk.DropDown.new_from_strings(self.ws_names)
        if self.config.active_workspace in self.ws_names:
            idx = self.ws_names.index(self.config.active_workspace)
            self.profile_combo.set_selected(idx)
        self.profile_combo.connect("notify::selected", self.on_profile_selection_changed)
        selector_box.append(self.profile_combo)

        add_btn = Gtk.Button(label="➕ Add New")
        add_btn.connect("clicked", self.on_add_profile_clicked)
        selector_box.append(add_btn)

        self.del_btn = Gtk.Button(label="🗑️ Delete")
        self.del_btn.connect("clicked", self.on_delete_profile_clicked)
        selector_box.append(self.del_btn)

        main_box.append(selector_box)
        main_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        self.grid = Gtk.Grid(column_spacing=18, row_spacing=12)
        self.grid.set_margin_top(6)
        self.grid.set_margin_bottom(6)

        lbl_name = Gtk.Label(label="Profile Name:", halign=Gtk.Align.START)
        self.name_entry = Gtk.Entry()
        self.grid.attach(lbl_name, 0, 0, 1, 1)
        self.grid.attach(self.name_entry, 1, 0, 2, 1)

        lbl_stable_path = Gtk.Label(label="Stable Path:", halign=Gtk.Align.START)
        self.stable_path_entry = Gtk.Entry()
        self.stable_path_entry.set_hexpand(True)
        btn_browse_stable = Gtk.Button(label="📁 Browse")
        btn_browse_stable.connect("clicked", self.on_browse_clicked, self.stable_path_entry, True)
        self.grid.attach(lbl_stable_path, 0, 1, 1, 1)
        self.grid.attach(self.stable_path_entry, 1, 1, 1, 1)
        self.grid.attach(btn_browse_stable, 2, 1, 1, 1)

        lbl_stable_br = Gtk.Label(label="Stable Branch:", halign=Gtk.Align.START)
        self.stable_br_entry = Gtk.Entry()
        self.grid.attach(lbl_stable_br, 0, 2, 1, 1)
        self.grid.attach(self.stable_br_entry, 1, 2, 2, 1)

        lbl_unstable_path = Gtk.Label(label="Unstable Path (Opt):", halign=Gtk.Align.START)
        self.unstable_path_entry = Gtk.Entry()
        self.unstable_path_entry.set_hexpand(True)
        btn_browse_unstable = Gtk.Button(label="📁 Browse")
        btn_browse_unstable.connect("clicked", self.on_browse_clicked, self.unstable_path_entry, False)
        self.grid.attach(lbl_unstable_path, 0, 3, 1, 1)
        self.grid.attach(self.unstable_path_entry, 1, 3, 1, 1)
        self.grid.attach(btn_browse_unstable, 2, 3, 1, 1)

        lbl_unstable_br = Gtk.Label(label="Unstable Branch:", halign=Gtk.Align.START)
        self.unstable_br_entry = Gtk.Entry()
        self.grid.attach(lbl_unstable_br, 0, 4, 1, 1)
        self.grid.attach(self.unstable_br_entry, 1, 4, 2, 1)

        main_box.append(self.grid)

        spacer = Gtk.Label()
        spacer.set_vexpand(True)
        main_box.append(spacer)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        footer.set_halign(Gtk.Align.END)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda btn: self.destroy())
        footer.append(cancel_btn)

        save_btn = Gtk.Button(label="💾 Save Profile")
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", self.on_save_clicked)
        footer.append(save_btn)

        main_box.append(footer)
        self.set_child(main_box)

        self.load_profile_to_fields(self.config.active_workspace)

    def load_profile_to_fields(self, ws_name):
        prof = self.config.workspaces.get(ws_name, {})
        self.name_entry.set_text(ws_name)
        self.stable_path_entry.set_text(prof.get("stable_path", "") or "")
        self.stable_br_entry.set_text(prof.get("stable_branch", "factory") or "factory")
        self.unstable_path_entry.set_text(prof.get("unstable_path", "") or "")
        self.unstable_br_entry.set_text(prof.get("unstable_branch", "next") or "next")
        self.del_btn.set_sensitive(len(self.config.workspaces) > 1)

    def on_profile_selection_changed(self, dropdown, pspec):
        active_idx = dropdown.get_selected()
        if 0 <= active_idx < len(self.ws_names):
            ws_name = self.ws_names[active_idx]
            self.load_profile_to_fields(ws_name)

    def on_browse_clicked(self, btn, entry, is_stable=True):
        dialog = Gtk.FileChooserNative(
            title="Select Workspace Folder",
            transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER
        )
        def on_response(native_dialog, response_id):
            if response_id == Gtk.ResponseType.ACCEPT:
                import warnings
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", category=DeprecationWarning)
                    path = native_dialog.get_file().get_path()
                entry.set_text(path)

                # Auto-detect git branch
                guessed_br = detect_git_branch(path)
                if guessed_br:
                    if is_stable:
                        self.stable_br_entry.set_text(guessed_br)
                    else:
                        self.unstable_br_entry.set_text(guessed_br)

                # Pre-populate profile name if stable path was picked and profile name is empty or default
                if is_stable:
                    curr_name = self.name_entry.get_text().strip()
                    if not curr_name or curr_name in ("New Workspace", "New Profile"):
                        folder_name = os.path.basename(path)
                        self.name_entry.set_text(folder_name)

                    # Auto-detect linked git worktree for the unstable path if exactly one active worktree exists!
                    try:
                        git_dir = os.path.join(path, '.git')
                        if os.path.isfile(git_dir):
                            with open(git_dir, 'r') as f:
                                line = f.read().strip()
                            if line.startswith('gitdir:'):
                                real_git_dir = line.split(':', 1)[1].strip()
                                git_dir = os.path.dirname(os.path.dirname(real_git_dir))
                                if not os.path.isabs(git_dir):
                                    git_dir = os.path.abspath(os.path.join(path, git_dir))

                        worktrees_dir = os.path.join(git_dir, 'worktrees')
                        if os.path.exists(worktrees_dir) and os.path.isdir(worktrees_dir):
                            valid_worktrees = []
                            for wt_name in os.listdir(worktrees_dir):
                                wt_path = os.path.join(worktrees_dir, wt_name)
                                gitdir_file = os.path.join(wt_path, 'gitdir')
                                head_file = os.path.join(wt_path, 'HEAD')

                                if os.path.exists(gitdir_file) and os.path.exists(head_file):
                                    with open(gitdir_file, 'r', encoding='utf-8') as f:
                                        wt_gitdir = f.read().strip()

                                    wt_target_dir = os.path.dirname(wt_gitdir)
                                    if os.path.exists(wt_target_dir) and os.path.isdir(wt_target_dir):
                                        with open(head_file, 'r', encoding='utf-8') as f:
                                            wt_head = f.read().strip()
                                        wt_branch = None
                                        if wt_head.startswith('ref:'):
                                            wt_branch = wt_head.split('ref:', 1)[1].strip().split('/')[-1]
                                        elif len(wt_head) == 40:
                                            wt_branch = wt_head[:7]

                                        valid_worktrees.append((wt_target_dir, wt_branch))

                            if len(valid_worktrees) == 1:
                                wt_dir, wt_br = valid_worktrees[0]
                                self.unstable_path_entry.set_text(wt_dir)
                                if wt_br:
                                    self.unstable_br_entry.set_text(wt_br)

                                # Present a gorgeous floating toast notification
                                toast = Adw.Toast.new("💡 Linked Git worktree detected! Unstable path and branches auto-filled.")
                                self.parent_window.toast_overlay.add_toast(toast)
                    except Exception:
                        pass

            native_dialog.destroy()
        dialog.connect("response", on_response)
        dialog.show()

    def on_add_profile_clicked(self, btn):
        self.name_entry.set_text("New Profile")
        self.stable_path_entry.set_text("")
        self.stable_br_entry.set_text("")
        self.unstable_path_entry.set_text("")
        self.unstable_br_entry.set_text("")
        self.name_entry.grab_focus()

    def on_delete_profile_clicked(self, btn):
        active_idx = self.profile_combo.get_selected()
        if 0 <= active_idx < len(self.ws_names):
            ws_name = self.ws_names[active_idx]
            if len(self.config.workspaces) > 1:
                del self.config.workspaces[ws_name]
                self.ws_names = sorted(self.config.workspaces.keys())
                self.config.active_workspace = self.ws_names[0]
                self.config.save()

                # Rebuild dropdown model
                model = Gtk.StringList.new(self.ws_names)
                self.profile_combo.set_model(model)
                if self.config.active_workspace in self.ws_names:
                    idx = self.ws_names.index(self.config.active_workspace)
                    self.profile_combo.set_selected(idx)

    def on_save_clicked(self, btn):
        ws_name = self.name_entry.get_text().strip()
        stable_p = self.stable_path_entry.get_text().strip()
        stable_b = self.stable_br_entry.get_text().strip() or "factory"
        unstable_p = self.unstable_path_entry.get_text().strip() or None
        unstable_b = self.unstable_br_entry.get_text().strip() or None

        if not ws_name:
            return
        if not stable_p or not os.path.exists(stable_p):
            toast = Adw.Toast.new("Stable Path cannot be empty and must exist on disk.")
            self.parent_window.toast_overlay.add_toast(toast)
            return

        if not unstable_p:
            unstable_p = None
            unstable_b = None

        self.config.workspaces[ws_name] = {
            "stable_path": stable_p,
            "stable_branch": stable_b,
            "unstable_path": unstable_p,
            "unstable_branch": unstable_b
        }

        # If they renamed a profile, delete the old one
        active_idx = self.profile_combo.get_selected()
        old_ws_name = self.ws_names[active_idx] if 0 <= active_idx < len(self.ws_names) else None
        if old_ws_name and old_ws_name != ws_name:
            if old_ws_name in self.config.workspaces:
                del self.config.workspaces[old_ws_name]

        self.config.active_workspace = ws_name
        self.config.save()
        self.destroy()
        self.callback_on_save()


class SyncWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Geckopit")
        self.set_default_size(1250, 780)
        self.set_size_request(950, 620) # Prevent GTK Paned measurement warning at startup
        # Set window icon natively from our custom SVG vector icon!
        icon_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "org.opensuse.geckopit.svg")
        if os.path.exists(icon_path):
            try:
                icon_theme = Gtk.IconTheme.get_for_display(self.get_display())
                icon_theme.add_search_path(os.path.dirname(icon_path))
                self.set_icon_name("org.opensuse.geckopit")
            except Exception:
                self.set_icon_name("preferences-system-network")
        else:
            self.set_icon_name("preferences-system-network")

        # Load workspace configuration and run autodetection
        self.config = WorkspaceConfig()
        self.config.autodetect_and_migrate()

        # Branch states
        active_prof = self.config.get_active_profile()
        self.stable_p = active_prof.get("stable_path", ".") or "."
        self.stable_b = active_prof.get("stable_branch", "factory") or "factory"
        self.unstable_b = active_prof.get("unstable_branch", "next") or "next"
        self.ignored_unstable_versions = active_prof.get("ignored_unstable_versions", {})

        # Core data
        self.repos = []
        self.package_data = {}
        self.load_workspace_repositories()

        # Background workers configured with daemon threads so they terminate on exit
        self.executor = DaemonThreadPoolExecutor(max_workers=50)

        # Open tab registry for active monitoring and deduplication
        self.terminal_tabs = []

        # Load user's preferred monospace font dynamically from GNOME GSettings
        self.monospace_font = self.get_system_monospace_font()

        # Initialize filter timeout and state registries
        self.filter_timeout_id = 0
        self.last_diff_package = None
        self.current_selected_package = None
        self.refreshed_packages = set()
        self.refreshed_sync_packages = set()
        self.refreshed_version_packages = set()

        # Global key event controller for application-wide shortcuts (Ctrl+F / Slash)
        window_key_controller = Gtk.EventControllerKey.new()
        window_key_controller.connect("key-pressed", self.on_window_key_pressed)
        self.add_controller(window_key_controller)

        # Build Sidebar
        self.sidebar_box = self.build_sidebar()

        # Build Detail Pane
        self.detail_pane = self.build_detail_pane()

        # Horizontal split pane: Left is Sidebar, Right is Detail Pane
        self.horizontal_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.horizontal_paned.set_start_child(self.sidebar_box)
        self.horizontal_paned.set_end_child(self.detail_pane)

        # Header Bar Workspace Selector and Settings Button
        self.header_bar = Adw.HeaderBar()

        # Left: Workspace profile dropdown picker (using modern Gtk.DropDown!)
        self.header_combo = Gtk.DropDown()
        self.header_combo.connect("notify::selected", self.on_header_profile_changed)
        self.update_header_profile_combo()
        self.header_bar.pack_start(self.header_combo)

        # Left: Settings/Workspace Manager Button
        settings_btn = Gtk.Button.new_from_icon_name("emblem-system-symbolic")
        settings_btn.set_tooltip_text("Workspace Profile Manager Settings")
        settings_btn.connect("clicked", self.on_settings_clicked)
        self.header_bar.pack_start(settings_btn)

        # Center Title
        title_lbl = Gtk.Label()
        title_lbl.set_markup("<span weight='bold'>Geckopit</span>")
        self.header_bar.set_title_widget(title_lbl)

        # Apply profile configuration UI states dynamically on startup
        self.apply_active_profile_ui()

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

        # If no stable path is configured, trigger the onboarding setup dialog immediately!
        if not active_prof.get("stable_path"):
            GLib.idle_add(lambda: self.on_settings_clicked(None))

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

    def load_workspace_repositories(self):
        active_prof = self.config.get_active_profile()
        stable_p = active_prof.get("stable_path", "")
        unstable_p = active_prof.get("unstable_path", "")

        factory_repos = set()
        next_repos = set()

        def is_git_repo(path):
            if not os.path.exists(path) or not os.path.isdir(path):
                return False
            git_sub = os.path.join(path, ".git")
            return os.path.exists(git_sub)

        if stable_p and os.path.exists(stable_p):
            try:
                factory_repos = {d for d in os.listdir(stable_p) if is_git_repo(os.path.join(stable_p, d))}
            except Exception:
                pass

        if unstable_p and os.path.exists(unstable_p):
            try:
                next_repos = {d for d in os.listdir(unstable_p) if is_git_repo(os.path.join(unstable_p, d))}
            except Exception:
                pass

        self.repos = sorted(list(factory_repos | next_repos))

        # Re-initialize package data dictionary
        self.package_data = {
            repo: {
                "sync": {},
                "version": {},
                "pr": {}
            } for repo in self.repos
        }
        self.load_profile_cache()

    def get_cache_path(self):
        # Sanitize workspace profile name to only allow safe alphanumeric/dash characters,
        # preventing path-traversal or directory bugs on names with slashes or colons.
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in self.config.active_workspace)
        return Path.home() / ".cache" / "geckopit" / f"cache_{safe_name}.json"

    def load_profile_cache(self):
        cache_path = self.get_cache_path()
        if cache_path.exists():
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    cached_pkg_data = data.get("package_data", {})
                    for pkg_name, pkg_val in cached_pkg_data.items():
                        if pkg_name in self.package_data:
                            self.package_data[pkg_name] = pkg_val
            except Exception:
                pass

    def save_profile_cache(self):
        cache_path = self.get_cache_path()
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({
                    "profile": self.config.active_workspace,
                    "scanned_at": time.time(),
                    "package_data": self.package_data
                }, f, indent=2)
        except Exception:
            pass

    def apply_active_profile_ui(self):
        active_prof = self.config.get_active_profile()
        unstable_p = active_prof.get("unstable_path")

        # Update window title to show active workspace profile name
        self.set_title(f"Geckopit - Profile: {self.config.active_workspace}")

        # If it is a Single-Pipeline workspace, hide Unstable Card completely
        is_dual = unstable_p is not None and os.path.exists(unstable_p)
        self.unstable_card.set_visible(is_dual)

        # Hide unstable filter buttons in sidebar
        self.filter_unstable.set_visible(is_dual)
        self.filter_forwarding.set_visible(is_dual)

        # Hide diff perspective dropdown selector if single pipeline
        self.diff_selector.set_visible(is_dual)
        if not is_dual:
            # Fallback perspective selection to factory_pool strictly
            self.diff_selector.set_selected(1) # Index 1 is factory_pool
        else:
            self.diff_selector.set_selected(0) # Index 0 is next_factory (default)

    def update_header_profile_combo(self):
        ws_names = sorted(self.config.workspaces.keys())
        model = Gtk.StringList.new(ws_names)

        # Set is_reloading to True to block signals during data rebuild
        self.is_reloading = True
        self.header_combo.set_model(model)

        active_ws = self.config.active_workspace
        if active_ws in ws_names:
            idx = ws_names.index(active_ws)
            self.header_combo.set_selected(idx)
        self.is_reloading = False

    def on_header_profile_changed(self, dropdown, pspec):
        if getattr(self, "is_reloading", False):
            return

        active_idx = dropdown.get_selected()
        ws_names = sorted(self.config.workspaces.keys())
        if 0 <= active_idx < len(ws_names):
            ws_name = ws_names[active_idx]
            if ws_name != self.config.active_workspace:
                self.config.active_workspace = ws_name
                self.config.save()
                self.reload_workspace()

    def on_settings_clicked(self, btn):
        dialog = WorkspaceManagerDialog(self, self.config, self.reload_workspace)
        dialog.present()

    def reload_workspace(self):
        # Update stable and unstable branch maps
        active_prof = self.config.get_active_profile()
        self.stable_p = active_prof.get("stable_path", ".") or "."
        self.stable_b = active_prof.get("stable_branch", "factory") or "factory"
        self.unstable_b = active_prof.get("unstable_branch", "next") or "next"
        self.ignored_unstable_versions = active_prof.get("ignored_unstable_versions", {})

        # Clear session refreshed state registry
        self.refreshed_packages = set()
        self.refreshed_sync_packages = set()
        self.refreshed_version_packages = set()

        # Reload repositories
        self.load_workspace_repositories()

        # Update sidebar list row entries dynamically
        self.populate_sidebar_rows()

        # Update header-combo selection list
        self.update_header_profile_combo()

        # Update dynamic layouts (single vs dual pipeline)
        self.apply_active_profile_ui()

        # Clear active selected details
        self.detail_stack.set_visible_child_name("empty")
        self.current_selected_package = None

        # Re-trigger background scans
        self.refresh_all()

    def get_mapped_worktree_path(self, package_name, target_branch):
        active_prof = self.config.get_active_profile()
        stable_p = active_prof.get("stable_path")
        unstable_p = active_prof.get("unstable_path")

        if target_branch == "factory" and stable_p:
            path = os.path.join(stable_p, package_name)
            if os.path.exists(path):
                return path
        elif target_branch == "next" and unstable_p:
            path = os.path.join(unstable_p, package_name)
            if os.path.exists(path):
                return path

        fallback_p = stable_p if stable_p else unstable_p
        if fallback_p:
            return os.path.join(fallback_p, package_name)
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

    def populate_sidebar_rows(self):
        # Clear existing rows first
        while True:
            row = self.master_list_box.get_row_at_index(0)
            if not row:
                break
            self.master_list_box.remove(row)

        self.package_rows = {}
        for repo in self.repos:
            row = PackageRow(repo, self)
            self.master_list_box.append(row)
            self.package_rows[repo] = row

    def refresh_all(self):
        # Ensure rows are fully populated
        if not getattr(self, "package_rows", None):
            self.populate_sidebar_rows()

        self.start_sync_scan()
        self.start_version_scan()
        if self.unstable_b:
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
        name, data = sb.check_repo_sync(repo, stable_branch=self.stable_b, unstable_branch=self.unstable_b, workspace_path=self.stable_p)
        GLib.idle_add(self.update_sync_row_single, name, data)

    def update_sync_row_single(self, name, data):
        if data.get("status") == "success":
            self.package_data[name]["sync"] = data
            self.refreshed_sync_packages.add(name)
            self.update_row_ui(name)
            self.check_and_mark_package_refreshed(name)

    def run_bg_version_single(self, repo):
        name, data = sb.check_repo_version(repo, stable_branch=self.stable_b, unstable_branch=self.unstable_b, workspace_path=self.stable_p, ignored_unstable_versions=self.ignored_unstable_versions)
        GLib.idle_add(self.update_version_row_single, name, data)

    def update_version_row_single(self, name, data):
        if data.get("status") == "success":
            self.package_data[name]["version"] = data
            self.refreshed_version_packages.add(name)
            self.update_row_ui(name)
            self.check_and_mark_package_refreshed(name)

    def run_bg_forward_single(self, repo):
        _, sync_data = sb.check_repo_sync(repo, stable_branch=self.stable_b, unstable_branch=self.unstable_b, workspace_path=self.stable_p)
        pr_data = {"has_pr": False}
        if self.unstable_b and sync_data.get("status") == "success" and sync_data.get("next_status") != "No next branch":
            next_ahead = sync_data.get("next_ahead", 0)
            if next_ahead > 0:
                _, pr_data = sb.check_repo_pr(repo, stable_branch=self.stable_b, unstable_branch=self.unstable_b, workspace_path=self.stable_p)
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
        stable_needs_action = (factory_ver != "N/A" and upstream_stable != "N/A" and clean_version(factory_ver, row.package_name) != clean_version(upstream_stable, row.package_name))

        # C. Unstable/Next Tracking state
        next_ver = ver.get("next_ver", "—")
        upstream_latest = ver.get("upstream_latest", "—")
        is_unstable_track = (next_ver != "—")

        # We defensively check if this found unstable update matches our active profile's ignore list!
        ignored_ver = getattr(self, "ignored_unstable_versions", {}).get(row.package_name)
        is_ignored_unstable = (ignored_ver and clean_version(upstream_latest, row.package_name) == clean_version(ignored_ver, row.package_name))
        unstable_needs_action = (next_ver != "—" and upstream_latest != "—" and clean_version(next_ver, row.package_name) != clean_version(upstream_latest, row.package_name) and not is_ignored_unstable)

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

        # Attach gesture to capture Ctrl+Alt+Right Click for hidden ignore context menu!
        self.unstable_ver_gesture = Gtk.GestureClick.new()
        self.unstable_ver_gesture.set_button(0) # Capture all buttons (left/right/middle)
        self.unstable_ver_gesture.connect("released", self.on_unstable_ver_clicked)
        self.unstable_ver_lbl.add_controller(self.unstable_ver_gesture)

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

        # Synchronize GtkSourceView theme with system dark/light mode preference!
        style_manager = Adw.StyleManager.get_default()
        style_manager.connect("notify::dark", lambda sm, pspec: apply_source_view_style_scheme(self.diff_buffer))
        apply_source_view_style_scheme(self.diff_buffer)

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

        # Bypasses the slow background thread pool queue to load the diff instantly!
        threading.Thread(target=self.run_bg_diff_perspective, args=(package_name, perspective), daemon=True).start()

    def run_bg_diff_perspective(self, package_name, perspective):
        if perspective == "next_factory":
            diff_text = sb.get_git_diff(package_name, stable_branch=self.stable_b, unstable_branch=self.unstable_b, workspace_path=self.stable_p)
        else:
            diff_text = ""
            repo_path = self.get_mapped_worktree_path(package_name, "factory")
            try:
                gitea_name = sb.get_gitea_repo_name(repo_path, package_name)
                pool_url = f"https://src.opensuse.org/pool/{gitea_name}.git"

                # Bypasses slow, redundant network fetches if the package was already refreshed in the current session!
                was_fetched = package_name in getattr(self, "refreshed_sync_packages", set())
                if not was_fetched:
                    sb.run_tracked(
                        ['git', '-C', repo_path, 'fetch', '--quiet', pool_url, self.stable_b],
                        check=True, capture_output=True
                    )

                res = sb.run_tracked(
                    ['git', '-C', repo_path, 'diff', f'FETCH_HEAD...origin/{self.stable_b}'],
                    check=True, capture_output=True, text=True
                )
                diff_text = res.stdout
                if not diff_text.strip():
                    diff_text = f"No differences in spec files or sources detected between local Stable ({self.stable_b}) and remote Pool."
            except Exception as e:
                diff_text = f"Error performing git diff (Factory vs Pool): {str(e)}"

        GLib.idle_add(self.update_diff_text, diff_text)

    def on_pull_pool_clicked(self, btn):
        if getattr(self, "current_selected_package", None):
            repo_path = self.get_mapped_worktree_path(self.current_selected_package, "factory")
            gitea_name = sb.get_gitea_repo_name(repo_path, self.current_selected_package)
            pool_url = f"https://src.opensuse.org/pool/{gitea_name}.git"
            command = f"git fetch {pool_url} {self.stable_b} && git merge FETCH_HEAD"
            self.allocate_terminal(self.current_selected_package, "factory", command)

    def on_catchup_merge_clicked(self, btn):
        if getattr(self, "current_selected_package", None):
            command = f"git fetch origin && git merge origin/{self.stable_b} --no-edit"
            self.allocate_terminal(self.current_selected_package, "next", command)

    # --- THE CORE EVENT HANDLERS & LOADERS ---

    def on_unstable_ver_clicked(self, gesture, n_press, x, y):
        # Retrieve event modifier and button states defensively
        state = gesture.get_current_event_state()
        button = gesture.get_current_button()

        ctrl_pressed = (state & Gdk.ModifierType.CONTROL_MASK) != 0
        alt_pressed = (state & Gdk.ModifierType.ALT_MASK) != 0

        # Ctrl + Alt + Right Click (Button 3) trigger validation
        if ctrl_pressed and alt_pressed and button == 3:
            package_name = getattr(self, "current_selected_package", None)
            if not package_name:
                return

            pkg_data = self.package_data.get(package_name, {})
            ver = pkg_data.get("version") or {}
            upstream_latest = ver.get("upstream_latest")
            if not upstream_latest or upstream_latest == "—":
                return

            # Construct contextual, non-cluttering Popover menu
            popover = Gtk.Popover()
            popover.set_parent(self.unstable_ver_lbl)

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.set_margin_start(8)
            box.set_margin_end(8)
            box.set_margin_top(8)
            box.set_margin_bottom(8)

            ignored_ver = self.ignored_unstable_versions.get(package_name)
            is_ignored = (ignored_ver and clean_version(upstream_latest, package_name) == clean_version(ignored_ver, package_name))

            if is_ignored:
                btn = Gtk.Button(label=f"🔄 Unignore Version {upstream_latest}")
                btn.connect("clicked", self.on_toggle_ignore_clicked, popover, package_name, upstream_latest, False)
            else:
                btn = Gtk.Button(label=f"🚫 Ignore Version {upstream_latest}")
                btn.connect("clicked", self.on_toggle_ignore_clicked, popover, package_name, upstream_latest, True)

            box.append(btn)
            popover.set_child(box)
            popover.popup()

    def on_toggle_ignore_clicked(self, btn, popover, package_name, version, should_ignore):
        popover.popdown()

        active_prof = self.config.get_active_profile()
        if "ignored_unstable_versions" not in active_prof:
            active_prof["ignored_unstable_versions"] = {}

        if should_ignore:
            active_prof["ignored_unstable_versions"][package_name] = version
            toast_text = f"🚫 Version {version} is now ignored for {package_name}."
        else:
            if package_name in active_prof["ignored_unstable_versions"]:
                del active_prof["ignored_unstable_versions"][package_name]
            toast_text = f"🔄 Version {version} is no longer ignored for {package_name}."

        # 1. Save config to disk immediately!
        self.config.save()

        # 2. Reload branch state mappings
        self.ignored_unstable_versions = active_prof.get("ignored_unstable_versions", {})

        # 3. Update memory/results cache state immediately in-place
        pkg_data = self.package_data.get(package_name, {})
        ver_data = pkg_data.get("version") or {}
        next_ver = ver_data.get("next_ver", "—")
        factory_ver = ver_data.get("factory_ver", "N/A")
        upstream_stable = ver_data.get("upstream_stable", "N/A")

        # Recalculate needs_update
        needs_update = False
        if factory_ver != "N/A" and upstream_stable != "N/A" and clean_version(factory_ver, package_name) != clean_version(upstream_stable, package_name):
            needs_update = True
        if next_ver != "—" and clean_version(next_ver, package_name) != clean_version(version, package_name) and not should_ignore:
            needs_update = True

        ver_data["needs_update"] = needs_update

        # 4. Save profile cache so state is preserved across launches instantly
        self.save_profile_cache()

        # 5. Redraw row UI and detail pane instantly!
        self.update_row_ui(package_name)
        self.load_package_detail(package_name)

        # 6. Re-evaluate sidebar list filters in-place!
        self.master_list_box.invalidate_filter()

        # 7. Display floating confirmation toast
        toast = Adw.Toast.new(toast_text)
        self.toast_overlay.add_toast(toast)

    def on_window_key_pressed(self, controller, keyval, keycode, state):
        # 1. Defensive Guard: Never steal key inputs from active VTE terminal tabs!
        focused_widget = self.get_focus()
        if focused_widget and focused_widget.get_name().startswith("Vte"):
            return False

        # 2. Defensive Guard: Never steal inputs if already editing an entry or PR description
        if focused_widget and (focused_widget.get_name().startswith("GtkEntry") or focused_widget.get_name().startswith("GtkTextView")):
            return False

        # 3. Detect Ctrl + F or Slash (/) triggers
        ctrl_pressed = (state & Gdk.ModifierType.CONTROL_MASK) != 0
        if (ctrl_pressed and keyval == Gdk.KEY_f) or keyval == Gdk.KEY_slash:
            self.sidebar_search.grab_focus()
            return True # Consume key event so '/' isn't typed into the focused search entry

        return False

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
        legend_title.set_markup(f"<span weight='bold' size='medium'>ℹ️ Geckopit SCM Workflow Legend ({self.config.active_workspace})</span>")
        legend_box.append(legend_title)

        stable_b = self.stable_b
        unstable_b = self.unstable_b or "unstable"

        # Build dynamic text based on whether unstable branch exists (Dual vs Single pipeline)
        if self.unstable_b:
            pipeline_text = (
                f"<span weight='bold'>Unstable Pipeline Sync ({unstable_b} ➔ {stable_b}):</span> Monitors alignment between unstable development and stable branch.\n"
                f"   • <span foreground='orange' weight='bold'>Behind {stable_b}</span>: Unstable branch is missing stable commits—merge {stable_b} ➔ {unstable_b}.\n"
                f"   • <span foreground='green' weight='bold'>Ahead of {stable_b}</span>: Unstable branch carries new commits (ready for PR).\n\n"
            )
        else:
            pipeline_text = ""

        legend_desc = Gtk.Label(halign=Gtk.Align.START)
        legend_desc.set_justify(Gtk.Justification.LEFT)
        legend_desc.set_wrap(True)
        legend_desc.set_markup(
            f"<span weight='bold'>Gitea Pool Sync (local ➔ pool):</span> Monitors alignment between local SCM checkouts and the central package pool.\n"
            f"   • <span foreground='orange' weight='bold'>Behind Pool</span>: Upstream changes exist in pool—pull them to catch up.\n"
            f"   • <span foreground='cyan' weight='bold'>Ahead of Pool</span>: Local {stable_b} branch carries commits not yet submitted to the central pool.\n\n"
            f"{pipeline_text}"
            "<span foreground='gray' size='small'>Double-click any sidebar package row to review its code differences.</span>"
        )
        legend_box.append(legend_desc)

        popover.set_child(legend_box)
        popover.popup()

    def on_package_row_selected(self, list_box, row):
        if not row:
            self.detail_stack.set_visible_child(self.empty_page)
            self.current_selected_package = None
            return
        package_name = row.package_name
        self.load_package_detail(package_name)

        # Trigger an instant, prioritized priority-thread scan for the newly selected package exactly once on manual selection!
        # Bypasses the thread pool and avoids infinite recursive updates by never triggering from result callbacks.
        if package_name not in getattr(self, "refreshed_packages", set()):
            self.refresh_single_package_priority(package_name)

    def refresh_single_package_priority(self, pkg_name):
        """Spawns direct, prioritized background threads to bypass the saturated thread pool queue and refresh the selected package instantly."""
        if not self.repos or pkg_name not in self.repos:
            return

        # Start direct priority daemon threads to bypass self.executor queue fanning!
        threading.Thread(target=self.run_bg_sync_single, args=(pkg_name,), daemon=True).start()
        threading.Thread(target=self.run_bg_version_single, args=(pkg_name,), daemon=True).start()
        if self.unstable_b:
            threading.Thread(target=self.run_bg_forward_single, args=(pkg_name,), daemon=True).start()

    def load_package_detail(self, package_name):
        self.current_selected_package = package_name
        self.detail_stack.set_visible_child_name("detail")

        # Visual indicator for cached details
        is_fresh = package_name in getattr(self, "refreshed_packages", set())
        title_suffix = "" if is_fresh else " <span size='small' style='italic' foreground='gray' weight='normal'>(cached)</span>"
        self.detail_title_label.set_markup(f"<span size='large' weight='bold'>{package_name}</span>{title_suffix}")

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
            if factory_ver != "N/A" and upstream_stable != "N/A" and clean_version(factory_ver, package_name) != clean_version(upstream_stable, package_name):
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

        # Check if this unstable version is ignored in active profile configs
        ignored_ver = getattr(self, "ignored_unstable_versions", {}).get(package_name)
        is_ignored_unstable = (ignored_ver and clean_version(upstream_latest, package_name) == clean_version(ignored_ver, package_name))

        if not ver:
            self.unstable_ver_lbl.set_text("Loading...")
            self.update_next_btn.set_sensitive(False)
            self.update_next_btn.set_label("Run SCM Update")
        else:
            if next_ver != "—" and upstream_latest != "—" and clean_version(next_ver, package_name) != clean_version(upstream_latest, package_name):
                if is_ignored_unstable:
                    self.unstable_ver_lbl.set_markup(f"<span weight='bold'>{next_ver}</span> ➔ <span weight='bold' foreground='gray' style='italic'>{upstream_latest} (Ignored)</span>")
                    self.update_next_btn.set_sensitive(False)
                    self.update_next_btn.set_label("Ignored Unstable Update")
                else:
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

        # Dynamically update terminal button labels to match configured branch names!
        self.open_term_fac_btn.set_label(f"🖥️ Terminal ({self.stable_b})")
        if self.unstable_b:
            self.open_term_next_btn.set_label(f"🖥️ Terminal ({self.unstable_b})")

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
                webbrowser.open(pr_url)
            else:
                dialog = SyncCreatePRDialog(self, self.current_selected_package)
                dialog.present()

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
        name, data = sb.check_repo_sync(repo, stable_branch=self.stable_b, unstable_branch=self.unstable_b, workspace_path=self.stable_p)
        GLib.idle_add(self.add_sync_result, name, data)

    def check_and_mark_package_refreshed(self, name):
        if name in self.refreshed_sync_packages and name in self.refreshed_version_packages:
            if name not in self.refreshed_packages:
                self.refreshed_packages.add(name)
                self.save_profile_cache()
                if self.current_selected_package == name:
                    self.load_package_detail(name)

    def add_sync_result(self, name, data):
        self.sync_completed_count += 1

        if data.get("status") == "success":
            self.package_data[name]["sync"] = data
            self.refreshed_sync_packages.add(name)
            self.update_row_ui(name)
            self.check_and_mark_package_refreshed(name)

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
        name, data = sb.check_repo_version(repo, stable_branch=self.stable_b, unstable_branch=self.unstable_b, workspace_path=self.stable_p, ignored_unstable_versions=self.ignored_unstable_versions)
        GLib.idle_add(self.add_version_result, name, data)

    def add_version_result(self, name, data):
        self.ver_completed_count += 1

        if data.get("status") == "success":
            self.package_data[name]["version"] = data
            self.refreshed_version_packages.add(name)
            self.update_row_ui(name)
            self.check_and_mark_package_refreshed(name)

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
        _, sync_data = sb.check_repo_sync(repo, stable_branch=self.stable_b, unstable_branch=self.unstable_b, workspace_path=self.stable_p)
        pr_data = {"has_pr": False}
        if self.unstable_b and sync_data.get("status") == "success" and sync_data.get("next_status") != "No next branch":
            next_ahead = sync_data.get("next_ahead", 0)
            if next_ahead > 0:
                _, pr_data = sb.check_repo_pr(repo, stable_branch=self.stable_b, unstable_branch=self.unstable_b, workspace_path=self.stable_p)
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
    GLib.set_prgname("Geckopit")
    GLib.set_application_name("Geckopit")
    app = SyncApp()
    sys.argv = [sys.argv[0]]  # Strip extra args to prevent GTK app parsing issues
    sys.exit(app.run(sys.argv))
