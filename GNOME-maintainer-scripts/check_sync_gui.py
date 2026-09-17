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

class SyncRow(Gtk.ListBoxRow):
    """Custom row holding package sync data for the Sync tab."""
    def __init__(self, package_name, data):
        super().__init__()
        self.package_name = package_name
        self.data = data

        # Main horizontal box
        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        main_box.set_margin_start(18)
        main_box.set_margin_end(18)
        main_box.set_margin_top(12)
        main_box.set_margin_bottom(12)

        # Left side: Title and structured Grid
        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left_box.set_hexpand(True)

        # Package Title
        title_label = Gtk.Label(halign=Gtk.Align.START)
        title_label.set_markup(f"<span size='medium' weight='bold'>{package_name}</span>")
        left_box.append(title_label)

        # Structured Grid
        grid = Gtk.Grid(column_spacing=48, row_spacing=4)

        pool_status = data.get("pool_status", "unknown")
        pool_ahead = data.get("pool_ahead", 0)
        pool_behind = data.get("pool_behind", 0)
        next_status = data.get("next_status", "unknown")
        next_ahead = data.get("next_ahead", 0)
        next_behind = data.get("next_behind", 0)

        # Column 1: Stage 1 (Pool Sync)
        s1_title = Gtk.Label(halign=Gtk.Align.START)
        s1_title.set_markup("<span size='small' foreground='gray'>Pool Sync (Stage 1)</span>")
        grid.attach(s1_title, 0, 0, 1, 1)

        s1_val = Gtk.Label(halign=Gtk.Align.START)
        if pool_behind > 0 and pool_ahead > 0:
            s1_val.set_markup(f"<span weight='bold' foreground='red'>Diverged</span> <span size='small' foreground='gray'>(B:{pool_behind}/A:{pool_ahead})</span>")
        elif pool_behind > 0:
            s1_val.set_markup(f"<span weight='bold' foreground='orange'>Behind Pool by {pool_behind} commits</span>")
        elif pool_ahead > 0:
            s1_val.set_markup(f"<span weight='bold' foreground='cyan'>Ahead of Pool by {pool_ahead} commits</span>")
        elif pool_status == "Not in Pool":
            s1_val.set_markup("<span foreground='yellow'>Not in Gitea Pool</span>")
        else:
            s1_val.set_markup("<span foreground='green'>Fully In Sync</span>")
        grid.attach(s1_val, 0, 1, 1, 1)

        # Column 2: Stage 2 (Next Sync)
        s2_title = Gtk.Label(halign=Gtk.Align.START)
        s2_title.set_markup("<span size='small' foreground='gray'>Next Branch Sync (Stage 2)</span>")
        grid.attach(s2_title, 1, 0, 1, 1)

        s2_val = Gtk.Label(halign=Gtk.Align.START)
        if next_behind > 0 and next_ahead > 0:
            s2_val.set_markup(f"<span weight='bold' foreground='red'>Diverged</span> <span size='small' foreground='gray'>(B:{next_behind}/A:{next_ahead})</span>")
        elif next_behind > 0:
            s2_val.set_markup(f"<span weight='bold' foreground='orange'>Next behind Factory by {next_behind} commits</span>")
        elif next_ahead > 0:
            s2_val.set_markup(f"<span foreground='green'>Next ahead of Factory by {next_ahead} commits (ok)</span>")
        elif next_status == "No next branch".strip():
            s2_val.set_markup("<span foreground='gray'>No Next Branch</span>")
        else:
            s2_val.set_markup("<span foreground='green'>Fully In Sync</span>")
        grid.attach(s2_val, 1, 1, 1, 1)

        left_box.append(grid)
        main_box.append(left_box)

        # Right side: Icon/Action hint (Double click details)
        info_icon = Gtk.Image.new_from_icon_name("document-properties-symbolic")
        info_icon.set_tooltip_text("Double-click to view Diff")
        info_icon.set_valign(Gtk.Align.CENTER)
        main_box.append(info_icon)

        self.set_child(main_box)


class VersionRow(Gtk.ListBoxRow):
    """Custom row holding package data for the Versions tab."""
    def __init__(self, package_name, data, parent_window):
        super().__init__()
        self.package_name = package_name
        self.data = data
        self.parent_window = parent_window

        # Main horizontal box
        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        main_box.set_margin_start(18)
        main_box.set_margin_end(18)
        main_box.set_margin_top(4)
        main_box.set_margin_bottom(4)

        # Package Title
        title_label = Gtk.Label(halign=Gtk.Align.START)
        title_label.set_markup(f"<span size='medium' weight='bold'>{package_name}</span>")
        title_label.set_ellipsize(Pango.EllipsizeMode.END)
        title_label.set_hexpand(True)
        title_label.set_xalign(0.0)
        parent_window.sg_pkg.add_widget(title_label)
        main_box.append(title_label)

        factory_ver = data.get("factory_ver", "N/A")
        next_ver = data.get("next_ver", "—")
        upstream_stable = data.get("upstream_stable", "N/A")
        upstream_latest = data.get("upstream_latest", "—")

        # Column 1: Factory Value
        self.f_val = Gtk.Label(halign=Gtk.Align.START)
        if factory_ver != upstream_stable and factory_ver != "N/A" and upstream_stable != "N/A":
            self.f_val.set_markup(f"<span weight='bold' foreground='red'>{factory_ver}</span>  ➔  <span weight='bold' foreground='green'>{upstream_stable}</span>")
        else:
            self.f_val.set_markup(f"<span foreground='gray'>{factory_ver} (In Sync)</span>")
        self.f_val.set_hexpand(True)
        self.f_val.set_xalign(0.0)
        parent_window.sg_fac.add_widget(self.f_val)
        main_box.append(self.f_val)

        # Column 2: Next Value
        self.n_val = Gtk.Label(halign=Gtk.Align.START)
        if next_ver != "—" and next_ver != upstream_latest and upstream_latest != "—":
            self.n_val.set_markup(f"<span weight='bold' foreground='red'>{next_ver}</span>  ➔  <span weight='bold' foreground='green'>{upstream_latest}</span>")
        else:
            self.n_val.set_markup(f"<span foreground='gray'>{next_ver} (In Sync)</span>" if next_ver != "—" else "<span foreground='gray'>—</span>")
        self.n_val.set_hexpand(True)
        self.n_val.set_xalign(0.0)
        parent_window.sg_nxt.add_widget(self.n_val)
        main_box.append(self.n_val)

        # Right side: Suffix action buttons (vertically centered)
        suffix_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        suffix_box.set_valign(Gtk.Align.CENTER)
        suffix_box.set_halign(Gtk.Align.END)
        suffix_box.set_hexpand(False)
        parent_window.sg_act.add_widget(suffix_box)

        # Open Upstream Page Button
        web_btn = Gtk.Button.new_from_icon_name("web-browser-symbolic")
        web_btn.set_tooltip_text("Open Release Monitoring Page")
        web_btn.connect("clicked", self.on_web_clicked, package_name)
        suffix_box.append(web_btn)

        main_box.append(suffix_box)
        self.set_child(main_box)

        # Right-click gesture detector (Button 3 is right-click)
        gesture = Gtk.GestureClick.new()
        gesture.set_button(3)
        gesture.connect("released", self.on_right_click)
        self.add_controller(gesture)

    def set_branch_filter(self, filter_mode):
        # 0 = Both, 1 = Factory, 2 = Next
        if filter_mode == 0:
            self.f_val.set_visible(True)
            self.n_val.set_visible(True)
        elif filter_mode == 1:
            self.f_val.set_visible(True)
            self.n_val.set_visible(False)
        elif filter_mode == 2:
            self.f_val.set_visible(False)
            self.n_val.set_visible(True)

    def on_web_clicked(self, btn, package_name):
        # Retrieve the resolved project name from our backend results data
        project_name = self.data.get("project", package_name)
        url = f"https://release-monitoring.org/project/{project_name}/"
        webbrowser.open(url)

    def on_right_click(self, gesture, n_press, x, y):
        popover = Gtk.Popover()
        popover.set_parent(self)

        # Position at the clicked point
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        popover.set_pointing_to(rect)

        # Popover layout
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        vbox.set_margin_start(8)
        vbox.set_margin_end(8)
        vbox.set_margin_top(8)
        vbox.set_margin_bottom(8)

        # Term Options (Explicit branch-specific terminal folders)
        factory_ver = self.data.get("factory_ver", "N/A")
        next_ver = self.data.get("next_ver", "—")
        upstream_stable = self.data.get("upstream_stable", "N/A")
        upstream_latest = self.data.get("upstream_latest", "—")

        # 1. Open Terminal in Next Worktree
        term_next_btn = Gtk.Button(label="Open Terminal in Next Worktree")
        term_next_btn.set_has_frame(False)
        term_next_btn.set_halign(Gtk.Align.START)
        term_next_btn.connect("clicked", self.on_open_terminal_clicked, "next", popover)
        vbox.append(term_next_btn)

        # 2. Open Terminal in Factory Worktree
        term_fac_btn = Gtk.Button(label="Open Terminal in Factory Worktree")
        term_fac_btn.set_has_frame(False)
        term_fac_btn.set_halign(Gtk.Align.START)
        term_fac_btn.connect("clicked", self.on_open_terminal_clicked, "factory", popover)
        vbox.append(term_fac_btn)

        # Check for _service file
        pkg_dir = os.path.join('.', self.package_name)
        has_service = os.path.exists(os.path.join(pkg_dir, '_service'))

        if has_service:
            # Separator
            sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            vbox.append(sep)

            # If next needs an update
            if next_ver != "—" and next_ver != upstream_latest and upstream_latest != "—":
                next_btn = Gtk.Button(label=f"Update Next to {upstream_latest} via obs_scm-update.sh")
                next_btn.set_has_frame(False)
                next_btn.set_halign(Gtk.Align.START)
                next_btn.connect("clicked", self.on_run_update_clicked, "next", upstream_latest, popover)
                vbox.append(next_btn)

            # If factory needs an update
            if factory_ver != "N/A" and factory_ver != upstream_stable and upstream_stable != "N/A":
                fac_btn = Gtk.Button(label=f"Update Factory to {upstream_stable} via obs_scm-update.sh")
                fac_btn.set_has_frame(False)
                fac_btn.set_halign(Gtk.Align.START)
                fac_btn.connect("clicked", self.on_run_update_clicked, "factory", upstream_stable, popover)
                vbox.append(fac_btn)

        popover.set_child(vbox)
        popover.popup()

    def on_open_terminal_clicked(self, btn, branch, popover):
        popover.popdown()
        self.parent_window.allocate_terminal(self.package_name, branch)

    def on_run_update_clicked(self, btn, branch, version, popover):
        popover.popdown()

        # Try to resolve package directory and find current _service revision
        pkg_dir = os.path.join('.', self.package_name)
        current_revision = get_service_revision(pkg_dir)
        guessed_revision, confidence_err = guess_update_revision(current_revision, version)

        if guessed_revision and not confidence_err:
            # We are confident! Run the update with the guessed revision parameter
            command = f"obs_scm-update.sh {shlex.quote(guessed_revision)}"
            self.parent_window.allocate_terminal(self.package_name, branch, command)
        else:
            # Not confident! Provide a helpful ANSI-colored terminal hint and drop to interactive shell
            title_text = f"⚠️  Unable to confidently guess target revision for {self.package_name}."
            lines = [
                f"\033[1;33m{title_text}\033[0m",
            ]
            if confidence_err:
                lines.append(f"   Reason: _service {confidence_err}.")
            if current_revision:
                lines.append(f"   Current revision in _service: \033[1m{current_revision}\033[0m")
            lines.append(f"   Suggested target version:     \033[1;32m{version}\033[0m")
            lines.append("")
            lines.append("👉 \033[1mRun obs_scm-update.sh manually with your preferred parameter.\033[0m")

            # Safely chain the echos with proper shell quoting
            hint_msg = " && ".join(f"echo {shlex.quote(line)}" for line in lines)
            self.parent_window.allocate_terminal(self.package_name, branch, hint_msg)


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
        self.pr_label = Gtk.Label()
        if self.has_pr:
            self.pr_label.set_markup(f"<span foreground='green' weight='bold'>PR #{self.pr_number}</span>")
        else:
            self.pr_label.set_markup("<span foreground='red'>No PR</span>")

        self.add_suffix(self.pr_label)

    def update_pr_state(self, pr_data):
        self.has_pr = pr_data.get("has_pr", False)
        self.pr_number = pr_data.get("number")
        self.pr_url = pr_data.get("url")

        if self.has_pr:
            self.pr_label.set_markup(f"<span foreground='green' weight='bold'>PR #{self.pr_number}</span>")
        else:
            self.pr_label.set_markup("<span foreground='red'>No PR</span>")

    def update_row_data(self, sync_data, pr_data):
        """Update the row data, subtitle, and badge in-place to prevent selection loss."""
        self.sync_data = sync_data
        self.pr_data = pr_data

        self.next_ahead = sync_data.get("next_ahead", 0)
        self.next_behind = sync_data.get("next_behind", 0)
        self.has_pr = pr_data.get("has_pr", False)
        self.pr_number = pr_data.get("number", None)
        self.pr_url = pr_data.get("url", None)

        self.set_subtitle(f"Commits Ahead: {self.next_ahead} | Behind: {self.next_behind}")

        if self.has_pr:
            self.pr_label.set_markup(f"<span foreground='green' weight='bold'>PR #{self.pr_number}</span>")
        else:
            self.pr_label.set_markup("<span foreground='red'>No PR</span>")


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
        src_val = Gtk.Label(label="next", halign=Gtk.Align.START)
        grid.attach(src_lbl, 0, 0, 1, 1)
        grid.attach(src_val, 1, 0, 1, 1)

        # 2. Target Branch (Base)
        tgt_lbl = Gtk.Label(halign=Gtk.Align.START)
        tgt_lbl.set_markup("<span weight='bold'>Target Branch (Base):</span>")
        tgt_val = Gtk.Label(label="factory", halign=Gtk.Align.START)
        grid.attach(tgt_lbl, 2, 0, 1, 1)
        grid.attach(tgt_val, 3, 0, 1, 1)

        # 3. PR Title
        title_input_lbl = Gtk.Label(halign=Gtk.Align.START)
        title_input_lbl.set_markup("<span weight='bold'>PR Title:</span>")
        self.title_entry = Gtk.Entry()
        self.title_entry.set_hexpand(True)
        self.title_entry.set_text(f"Forward next to factory: {package_name}")
        grid.attach(title_input_lbl, 0, 1, 1, 1)
        grid.attach(self.title_entry, 1, 1, 3, 1)

        # 4. PR Description
        desc_input_lbl = Gtk.Label(halign=Gtk.Align.START)
        desc_input_lbl.set_markup("<span weight='bold'>Description:</span>")

        self.desc_buffer = Gtk.TextBuffer()
        self.desc_buffer.set_text(f"Automated next-to-factory branch forwarding for {package_name} via GNOME Sync Dashboard.")
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

        # Start loading diff asynchronously
        self.diff_buffer.set_text("Connecting to pool & loading differences...")
        parent.executor.submit(self.load_diff_data)

    def on_destroy(self, widget):
        self.is_destroyed = True

    def load_diff_data(self):
        diff_text = sb.get_git_diff(self.package_name)
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

        # Final pre-submit double-check to prevent race conditions
        self.parent.executor.submit(self.run_bg_pre_submit_check, title, description)

    def run_bg_pre_submit_check(self, title, description):
        _, pr_data = sb.check_repo_pr(self.package_name)
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
            # No existing PR, proceed to submit!
            self.parent.executor.submit(self.run_bg_create_pr, title, description)

    def run_bg_create_pr(self, title, description):
        success, res_msg = sb.create_gitea_pr(self.package_name, title, description)
        GLib.idle_add(self.on_pr_created_result, success, res_msg)

    def on_pr_created_result(self, success, res_msg):
        if self.is_destroyed:
            return

        if success:
            toast = Adw.Toast.new("Pull Request created successfully!")
            url_match = re.search(r'https?://[^\s]+', res_msg)
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
        self.is_destroyed = False # Blocker 2: track destroyed state defensively!

        self.connect("destroy", self.on_destroy)

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

    def on_destroy(self, widget):
        self.is_destroyed = True

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

        # Blocker 2: check if window was closed asynchronously before scheduling GMainLoop idle frame
        if not self.is_destroyed:
            GLib.idle_add(self.update_ui, comparison_desc, diff_text)

    def update_ui(self, desc, text):
        if not self.is_destroyed:
            self.title_label.set_markup(f"<span size='large' weight='bold'>{desc}</span>")
            self.buffer.set_text(text)


class SyncWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="GNOME Sync Dashboard")
        self.set_default_size(1100, 720)
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

        # UI components
        self.stack = Adw.ViewStack()

        self.build_sync_view()
        self.build_version_view()
        self.build_forward_view()

        # View Switcher
        self.header_bar = Adw.HeaderBar()
        self.view_switcher = Adw.ViewSwitcher(stack=self.stack)
        self.header_bar.set_title_widget(self.view_switcher)

        # Vertical split pane: Top is stack, Bottom is terminal drawer
        self.main_paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self.main_paned.set_position(450) # Split position
        self.main_paned.set_start_child(self.stack)

        # Collapsible Terminal Drawer
        self.terminal_drawer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.terminal_drawer.set_size_request(-1, 280)

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

        # Kick off background loading
        self.refresh_all()

    def get_system_monospace_font(self):
        """Query GNOME GSettings dynamically to load the user's monospace font preference defensively."""
        # Allan's Blocker 3: Verify GSettings schema existence defensively before instantiating to avoid noisy C-warnings
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

    def on_close_request(self, window):
        """Gracefully dismantles GLib timers, closes thread pools, and terminates shell children."""
        # 1. Cancel the active GLib timeout source to let GApplication exit gracefully!
        if hasattr(self, "timeout_id") and self.timeout_id:
            GLib.Source.remove(self.timeout_id)
            self.timeout_id = 0

        # 2. Cancel and cleanly shutdown background scanner thread pool
        self.executor.shutdown(wait=False, cancel_futures=True)

        # 3. Forcefully terminate all running terminal shell processes to prevent process leaks!
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

        # 4. Explicitly terminate python process to clean up GObject reference-cycle states cleanly
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

        # Verify both that the directory exists and contains a valid SCM git setup to ensure worktree integrity
        if os.path.exists(mapped_dir) and (os.path.exists(os.path.join(mapped_dir, '.git')) or os.path.isfile(os.path.join(mapped_dir, '.git'))):
            return mapped_dir

        # Fallback to local package directory
        return os.path.abspath(os.path.join('.', package_name))

    def is_shell_pid_active(self, shell_pid):
        """
        Scans /proc directly in pure Python without spawning subprocesses (pgrep).
        Narrow exception boundaries handles microsecond PID creation/termination safely.
        """
        if not shell_pid:
            return False
        try:
            target_ppid = str(shell_pid)
            for f in os.listdir('/proc'):
                if f.isdigit():
                    try:
                        with open(f"/proc/{f}/stat", "r") as stat_file:
                            line = stat_file.readline()
                            fields = line.split()
                            # 4th field in /proc/<pid>/stat is the PPID
                            if len(fields) >= 4 and fields[3] == target_ppid:
                                return True
                    except (FileNotFoundError, ProcessLookupError, PermissionError):
                        # Safely skip microsecond process race conditions and system permissions
                        continue
                    except Exception:
                        pass
        except Exception:
            pass
        return False

    def allocate_terminal(self, pkg_name, target_branch, command=None):
        """
        Deduplicates terminal tabs.
        If an IDLE terminal tab already exists for this package/branch combo, switches focus to it.
        If it is ACTIVE/BUSY (running a command), spawns a new separate tab with an incremented counter.
        """
        self.terminal_drawer.set_visible(True)

        # Scan existing open tabs
        matching_tabs = [
            tab for tab in self.terminal_tabs
            if tab["pkg_name"] == pkg_name and tab["target_branch"] == target_branch
        ]

        # Check if any matching tab is currently idle
        for tab in matching_tabs:
            if not self.is_shell_pid_active(tab["shell_pid"]):
                # Found an idle tab! Focus it and run the command if provided
                page_num = self.notebook.page_num(tab["scroll_widget"])
                if page_num != -1:
                    self.notebook.set_current_page(page_num)
                    if command:
                        # Feed the command directly to the running shell
                        tab["terminal"].feed_child(f"{command}\n".encode('utf-8'))
                    tab["terminal"].grab_focus()
                    return

        # Otherwise, if none are idle or none exist, spawn a fresh new tab!
        suffix = ""
        if matching_tabs:
            # Add an incremented counter to distinguish parallel active tabs
            suffix = f" [{len(matching_tabs) + 1}]"

        self.show_terminal(pkg_name, target_branch, command, suffix)

    def show_terminal(self, pkg_name, target_branch, command=None, suffix=""):
        """Spawns a new VTE terminal tab inside the Gtk.Notebook drawer, supporting worktree directory resolution."""
        resolved_dir = self.get_mapped_worktree_path(pkg_name, target_branch)

        # Create a new terminal instance
        terminal = Vte.Terminal()
        terminal.set_font(Pango.FontDescription.from_string(self.monospace_font))
        terminal.set_scrollback_lines(2000)

        scroll = Gtk.ScrolledWindow()
        scroll.set_child(terminal)

        # Build tab label box
        tab_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        label_text = f"{pkg_name} ({target_branch}){suffix}"
        tab_label = Gtk.Label(label=label_text)
        tab_box.append(tab_label)

        close_tab_btn = Gtk.Button.new_from_icon_name("window-close-symbolic")
        # Apply standard GTK4/Libadwaita circular and flat visual styling classes
        close_tab_btn.add_css_class("flat")
        close_tab_btn.add_css_class("circular")
        close_tab_btn.set_tooltip_text("Close Tab")
        close_tab_btn.connect("clicked", lambda btn: self.close_terminal_tab(scroll))
        tab_box.append(close_tab_btn)

        # Append tab page
        page_index = self.notebook.append_page(scroll, tab_box)
        self.notebook.set_current_page(page_index)

        # Event controller for dynamic font size zooming (Ctrl+Plus / Ctrl+Minus / Ctrl+0)
        key_controller = Gtk.EventControllerKey.new()
        key_controller.connect("key-pressed", self.on_terminal_key_pressed, terminal)
        terminal.add_controller(key_controller)

        # Register tab inside our state tracker, keeping track of controllers to break reference cycles on destroy!
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

        # Connect child-exited signal to close the tab page automatically on 'exit'
        terminal.connect("child-exited", self.on_terminal_child_exited, scroll)

        shell = os.environ.get("SHELL", "/bin/bash")
        argv = [shell]
        if command:
            argv = [shell, "-c", f"{command}; exec {shell}"]

        # Spawn the shell and capture its PID inside our callback
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
        """Binds Ctrl+Plus (zoom in), Ctrl+Minus (zoom out), and Ctrl+0 (reset) keys. Support Shift modifiers defensively."""
        # Clean Gdk4 bitwise AND check cleanly isolates CONTROL_MASK
        is_ctrl = (state & Gdk.ModifierType.CONTROL_MASK) != 0
        if is_ctrl:
            current_scale = terminal.get_font_scale()
            # Allan's Blocker 4: Support both standard plus/equal AND Ctrl+Shift+= (which evaluates as KEY_equal with Shift layer)
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
        """Typing 'exit' or shell process terminating automatically closes the tab page safely."""
        GLib.idle_add(self.close_terminal_tab, scroll_widget)

    def on_terminal_spawned(self, terminal, pid, error, tab_state):
        if error is None:
            # Check if the tab was closed while spawn was pending!
            # If so, kill the orphaned process immediately to prevent any shell background leaks.
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
            # Blocker 5: Close dead tab page instantly and notify the user via a Toast Overlay
            scroll_widget = tab_state.get("scroll_widget")
            if scroll_widget:
                GLib.idle_add(self.close_terminal_tab, scroll_widget)

            toast = Adw.Toast.new(f"Terminal spawn failed: {error.message}")
            self.toast_overlay.add_toast(toast)

    def monitor_terminals(self):
        """Polls active terminal PIDs every 1.5 seconds, flashing state changes, reaping zombie processes, and displaying completed Toasts."""
        # Blocker 8: Force immediate GSource destruction to prevent re-registration leaks during close
        if not hasattr(self, "timeout_id") or not self.timeout_id:
            return False

        for tab in list(self.terminal_tabs):
            shell_pid = tab.get("shell_pid")
            if not shell_pid:
                continue

            # If the page was removed from the notebook, clear it from registry
            if self.notebook.page_num(tab["scroll_widget"]) == -1:
                if tab in self.terminal_tabs:
                    self.terminal_tabs.remove(tab)
                continue

            # --- NON-BLOCKING PROCESS REAPING (os.waitpid) ---
            # Resolves Python multi-threading hijacking SIGCHLD and blocking VTE child-exited emissions!
            try:
                reaped_pid, status = os.waitpid(shell_pid, os.WNOHANG)
                if reaped_pid == shell_pid:
                    # The shell has exited! Reap it from the system and close the tab instantly!
                    tab["shell_pid"] = None # Reset PID immediately to prevent duplicate triggers
                    GLib.idle_add(self.close_terminal_tab, tab["scroll_widget"])
                    continue
            except ChildProcessError:
                # Process is already reaped or gone! Close tab
                tab["shell_pid"] = None # Reset PID immediately to prevent duplicate triggers
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
                # Process started running!
                tab["was_active"] = True
                label_widget.set_text(f"{base_lbl} ⚙️")
            elif not is_active and was_active:
                # Process completed!
                tab["was_active"] = False
                label_widget.set_text(f"{base_lbl} ✅")

                # Suppress the toast if the user is already viewing the completed tab
                page_idx = self.notebook.page_num(tab["scroll_widget"])
                is_current_and_visible = (
                    self.terminal_drawer.props.visible and
                    page_idx != -1 and
                    self.notebook.get_current_page() == page_idx
                )
                if not is_current_and_visible:
                    # Dismiss any existing toast for this tab first just in case
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

        return True # Return True to keep the periodic GLib timer alive

    def on_toast_clicked(self, toast, scroll_widget):
        page_num = self.notebook.page_num(scroll_widget)
        if page_num != -1:
            self.notebook.set_current_page(page_num)
            self.terminal_drawer.set_visible(True)

    def on_notebook_switch_page(self, notebook, page, page_num):
        """When switching pages, dismiss the toast of the newly focused page."""
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

        # Clean terminal_tabs registry and destroy/unparent the GObject reference-cycle safely
        for tab in list(self.terminal_tabs):
            if tab["scroll_widget"] == page_widget:
                pkg_name = tab["pkg_name"]
                # Forcefully SIGKILL/SIGHUP the shell process if it's still alive when tab is closed manually!
                shell_pid = tab.get("shell_pid")
                if shell_pid:
                    try:
                        os.kill(shell_pid, signal.SIGHUP)
                    except Exception:
                        pass

                # Matthias's Blocker 1: Explicitly unparent, remove controllers, and destroy Vte.Terminal widget
                # to cleanly break the GObject reference-cycle and free the memory instantly!
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

                # Automatically refresh single package on terminal tab close
                self.refresh_single_package(pkg_name)
                break

        if self.notebook.get_n_pages() == 0:
            self.hide_terminal()

    def hide_terminal(self):
        self.terminal_drawer.set_visible(False)

    def refresh_all(self):
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
            idx = 0
            while True:
                row = self.sync_list_box.get_row_at_index(idx)
                if not row:
                    break
                if hasattr(row, "package_name") and row.package_name == name:
                    self.sync_list_box.remove(row)
                    new_row = SyncRow(name, data)
                    self.sync_list_box.insert(new_row, idx)
                    self.sync_list_box.invalidate_filter()
                    break
                idx += 1

    def run_bg_version_single(self, repo):
        name, data = sb.check_repo_version(repo)
        GLib.idle_add(self.update_version_row_single, name, data)

    def update_version_row_single(self, name, data):
        if data.get("status") == "success":
            idx = 0
            while True:
                row = self.ver_list_box.get_row_at_index(idx)
                if not row:
                    break
                if hasattr(row, "package_name") and row.package_name == name:
                    self.ver_list_box.remove(row)
                    new_row = VersionRow(name, data, self)
                    self.ver_list_box.insert(new_row, idx)
                    self.ver_list_box.invalidate_filter()
                    break
                idx += 1

    def run_bg_forward_single(self, repo):
        _, sync_data = sb.check_repo_sync(repo)
        pr_data = {"has_pr": False}
        if sync_data.get("status") == "success" and sync_data.get("next_status") != "No next branch":
            next_ahead = sync_data.get("next_ahead", 0)
            if next_ahead > 0:
                _, pr_data = sb.check_repo_pr(repo)
        GLib.idle_add(self.update_forward_row_single, repo, sync_data, pr_data)

    def update_forward_row_single(self, name, sync_data, pr_data):
        # Search for existing row
        idx = 0
        found_row = None
        while True:
            row = self.fwd_list_box.get_row_at_index(idx)
            if not row:
                break
            if hasattr(row, "package_name") and row.package_name == name:
                found_row = row
                break
            idx += 1

        next_ahead = sync_data.get("next_ahead", 0)

        if found_row:
            if next_ahead > 0:
                # Update in-place to avoid selection loss!
                found_row.update_row_data(sync_data, pr_data)

                # If this row is the currently selected one, we should also update the self.pr_btn label!
                selected_row = self.fwd_list_box.get_selected_row()
                if selected_row == found_row:
                    if found_row.has_pr:
                        self.pr_btn.set_label("Show Pull Request")
                    else:
                        self.pr_btn.set_label("Create Pull Request")
            else:
                # If there are no more next_ahead commits, we remove the row
                self.fwd_list_box.remove(found_row)
        elif next_ahead > 0:
            # Append new row
            new_row = ForwardRow(name, sync_data, pr_data)
            self.fwd_list_box.append(new_row)

        self.fwd_list_box.invalidate_filter()

    # --- TAB 1: SYNC VIEW ---
    def build_sync_view(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Control bar
        control_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        control_bar.set_margin_start(18)
        control_bar.set_margin_end(18)
        control_bar.set_margin_top(8)
        control_bar.set_margin_bottom(3)

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

        # Help / Legend Popover Button
        self.legend_btn = Gtk.Button.new_from_icon_name("help-about-symbolic")
        self.legend_btn.set_tooltip_text("Show Sync Workflow Legend")
        self.legend_btn.connect("clicked", self.on_legend_btn_clicked)
        control_bar.append(self.legend_btn)

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

        # Scrolled window (recovers 100% of the screen height for clean repository listings!)
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

    def on_legend_btn_clicked(self, btn):
        """Pops up a modern, elegant, and interactive workflow legend panel on demand."""
        popover = Gtk.Popover()
        popover.set_parent(btn)

        legend_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        legend_box.set_margin_start(16)
        legend_box.set_margin_end(16)
        legend_box.set_margin_top(16)
        legend_box.set_margin_bottom(16)
        legend_box.set_size_request(450, -1) # Set comfortable reading width

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
            "   • <span foreground='green' weight='bold'>Next ahead of Factory</span>: Next has additional developmental commits checked in (OK).\n\n"
            "<span foreground='gray' size='small'>Double-click any repository row to review its sync git diff in-app.</span>"
        )
        legend_box.append(legend_desc)

        popover.set_child(legend_box)
        popover.popup()

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

        if not self.repos:
            self.sync_progress_label.set_text("No packages found")
            empty_label = Gtk.Label()
            empty_label.set_markup(
                "<span size='large' weight='bold' foreground='gray'>No GNOME packages found in this directory.</span>\n"
                "<span size='small' foreground='gray'>Please run this tool from your openSUSE GNOME:Next workspace root.</span>"
            )
            empty_label.set_justify(Gtk.Justification.CENTER)
            empty_label.set_margin_top(48)
            empty_label.set_margin_bottom(48)
            self.sync_list_box.set_placeholder(empty_label)
            return

        # Set loading placeholder (prevents flashing misleading empty text on startup)
        loading_label = Gtk.Label()
        loading_label.set_markup("<span size='large' foreground='gray'>Checking downstream sync states...</span>")
        loading_label.set_margin_top(48)
        loading_label.set_margin_bottom(48)
        self.sync_list_box.set_placeholder(loading_label)

        self.sync_spinner.start()
        self.sync_completed_count = 0
        self.sync_progress_label.set_text(f"Scanning 0/{len(self.repos)}...")

        # Queue all repos in background thread pool
        for repo in self.repos:
            try:
                self.executor.submit(self.run_bg_sync, repo)
            except RuntimeError:
                # Catch RuntimeError in case executor is shutdown during search
                break

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

            # Count visible rows (rows that pass search & "only needs action" filter)
            visible_rows = 0
            row = self.sync_list_box.get_row_at_index(0)
            idx = 0
            while row:
                if self.sync_filter_func(row):
                    visible_rows += 1
                idx += 1
                row = self.sync_list_box.get_row_at_index(idx)

            if visible_rows == 0:
                empty_label = Gtk.Label()
                if not self.repos:
                    empty_label.set_markup(
                        "<span size='large' weight='bold' foreground='gray'>No GNOME packages found in this directory.</span>\n"
                        "<span size='small' foreground='gray'>Please run this tool from your openSUSE GNOME:Next workspace root.</span>"
                    )
                else:
                    empty_label.set_markup(
                        "<span size='large' weight='bold' foreground='green'>✅ All packages are fully in sync!</span>\n"
                        "<span size='small' foreground='gray'>Everything matches cleanly between pool, factory, and next.</span>"
                    )
                empty_label.set_justify(Gtk.Justification.CENTER)
                empty_label.set_margin_top(48)
                empty_label.set_margin_bottom(48)
                self.sync_list_box.set_placeholder(empty_label)

    def on_sync_row_activated(self, list_box, row):
        if not row:
            return
        dialog = SyncDiffDialog(self, row.package_name, row.data)
        dialog.present()


    # --- TAB 2: VERSION VIEW ---
    def build_version_view(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Control bar
        control_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        control_bar.set_margin_start(18)
        control_bar.set_margin_end(18)
        control_bar.set_margin_top(8)
        control_bar.set_margin_bottom(3)

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

        # Branch Filter DropDown (Both, Factory only, Next only)
        self.ver_branch_dropdown = Gtk.DropDown.new_from_strings(["Both Branches", "Factory Only", "Next Only"])
        self.ver_branch_dropdown.connect("notify::selected", self.on_ver_branch_changed)
        control_bar.append(self.ver_branch_dropdown)

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

        # SizeGroups for uniform column alignment across list
        self.sg_pkg = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        self.sg_fac = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        self.sg_nxt = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        self.sg_act = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)

        # Header Box
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        header_box.set_margin_start(18)
        header_box.set_margin_end(18)
        header_box.set_margin_top(4)
        header_box.set_margin_bottom(4)

        lbl_pkg = Gtk.Label(label="Package", halign=Gtk.Align.START)
        lbl_pkg.set_markup("<span weight='bold'>Package</span>")
        lbl_pkg.set_hexpand(True)
        lbl_pkg.set_xalign(0.0)
        self.sg_pkg.add_widget(lbl_pkg)
        header_box.append(lbl_pkg)

        self.lbl_fac = Gtk.Label(label="Factory (Stable)", halign=Gtk.Align.START)
        self.lbl_fac.set_markup("<span weight='bold'>Factory (Stable)</span>")
        self.lbl_fac.set_hexpand(True)
        self.lbl_fac.set_xalign(0.0)
        self.sg_fac.add_widget(self.lbl_fac)
        header_box.append(self.lbl_fac)

        self.lbl_nxt = Gtk.Label(label="Next (Unstable)", halign=Gtk.Align.START)
        self.lbl_nxt.set_markup("<span weight='bold'>Next (Unstable)</span>")
        self.lbl_nxt.set_hexpand(True)
        self.lbl_nxt.set_xalign(0.0)
        self.sg_nxt.add_widget(self.lbl_nxt)
        header_box.append(self.lbl_nxt)

        lbl_act = Gtk.Label(label="Actions", halign=Gtk.Align.END)
        lbl_act.set_markup("<span weight='bold'>Actions</span>")
        lbl_act.set_hexpand(False)
        lbl_act.set_xalign(1.0)
        self.sg_act.add_widget(lbl_act)
        header_box.append(lbl_act)

        box.append(header_box)
        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

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

        # 2. Branch and needs update filter
        only_needs_update = self.ver_filter_toggle.get_active()
        filter_mode = self.ver_branch_dropdown.get_selected() # 0 = Both, 1 = Factory, 2 = Next

        # Apply visibility filter to row columns on the fly!
        row.set_branch_filter(filter_mode)

        if only_needs_update:
            factory_ver = row.data.get("factory_ver", "N/A")
            next_ver = row.data.get("next_ver", "—")
            upstream_stable = row.data.get("upstream_stable", "N/A")
            upstream_latest = row.data.get("upstream_latest", "—")

            f_needs = factory_ver != upstream_stable and factory_ver != "N/A" and upstream_stable != "N/A"
            n_needs = next_ver != "—" and next_ver != upstream_latest and upstream_latest != "—"

            if filter_mode == 0:
                # Both branches: show if either needs update
                return f_needs or n_needs
            elif filter_mode == 1:
                # Factory only: show if factory needs update
                return f_needs
            elif filter_mode == 2:
                # Next only: show if next needs update
                return n_needs

        return True

    def on_ver_branch_changed(self, dropdown, pspec):
        filter_mode = self.ver_branch_dropdown.get_selected()
        if filter_mode == 0:
            self.lbl_fac.set_visible(True)
            self.lbl_nxt.set_visible(True)
        elif filter_mode == 1:
            self.lbl_fac.set_visible(True)
            self.lbl_nxt.set_visible(False)
        elif filter_mode == 2:
            self.lbl_fac.set_visible(False)
            self.lbl_nxt.set_visible(True)
        self.ver_list_box.invalidate_filter()

    def start_version_scan(self):
        while True:
            row = self.ver_list_box.get_row_at_index(0)
            if not row:
                break
            self.ver_list_box.remove(row)

        if not self.repos:
            self.ver_progress_label.set_text("No packages found")
            empty_label_ver = Gtk.Label()
            empty_label_ver.set_markup(
                "<span size='large' weight='bold' foreground='gray'>No GNOME packages found in this directory.</span>"
            )
            empty_label_ver.set_margin_top(48)
            empty_label_ver.set_margin_bottom(48)
            self.ver_list_box.set_placeholder(empty_label_ver)
            return

        # Set loading placeholder
        loading_label = Gtk.Label()
        loading_label.set_markup("<span size='large' foreground='gray'>Checking upstream updates...</span>")
        loading_label.set_margin_top(48)
        loading_label.set_margin_bottom(48)
        self.ver_list_box.set_placeholder(loading_label)

        self.ver_spinner.start()
        self.ver_completed_count = 0
        self.ver_progress_label.set_text(f"Scanning 0/{len(self.repos)}...")

        # Queue all repos in background (30 concurrent workers to avoid release-monitoring rate limits)
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
        self.ver_progress_label.set_text(f"Scanning {self.ver_completed_count}/{len(self.repos)}...")

        if data.get("status") == "success":
            # Pass our main window instance as parent_window
            row = VersionRow(name, data, self)
            self.ver_list_box.append(row)

        if self.ver_completed_count == len(self.repos):
            self.ver_spinner.stop()
            self.ver_progress_label.set_text("Scan Completed")

            visible_rows = 0
            row = self.ver_list_box.get_row_at_index(0)
            idx = 0
            while row:
                if self.ver_filter_func(row):
                    visible_rows += 1
                idx += 1
                row = self.ver_list_box.get_row_at_index(idx)

            if visible_rows == 0:
                empty_label = Gtk.Label()
                if not self.repos:
                    empty_label.set_markup(
                        "<span size='large' weight='bold' foreground='gray'>No GNOME packages found in this directory.</span>"
                    )
                else:
                    empty_label.set_markup(
                        "<span size='large' weight='bold' foreground='green'>✅ All packages are up to date!</span>\n"
                        "<span size='small' foreground='gray'>No new upstream updates available on release-monitoring.org.</span>"
                    )
                empty_label.set_justify(Gtk.Justification.CENTER)
                empty_label.set_margin_top(48)
                empty_label.set_margin_bottom(48)
                self.ver_list_box.set_placeholder(empty_label)


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

        if not self.repos:
            empty_label = Gtk.Label()
            empty_label.set_markup("<span size='small' weight='bold' foreground='gray'>No packages found.</span>")
            empty_label.set_margin_top(32)
            self.fwd_list_box.set_placeholder(empty_label)
            return

        # Set loading placeholder
        loading_label = Gtk.Label()
        loading_label.set_markup("<span size='small' foreground='gray'>Scanning unsubmitted changes...</span>")
        loading_label.set_margin_top(32)
        self.fwd_list_box.set_placeholder(loading_label)

        self.fwd_completed_count = 0

        # Forward scan fetches sync status, and if ahead > 0, fetches Gitea PR status
        for repo in self.repos:
            try:
                self.executor.submit(self.run_bg_forward, repo)
            except RuntimeError:
                break

    def run_bg_forward(self, repo):
        # 1. Check sync status
        _, sync_data = sb.check_repo_sync(repo)
        pr_data = {"has_pr": False}
        if sync_data.get("status") == "success" and sync_data.get("next_status") != "No next branch":
            next_ahead = sync_data.get("next_ahead", 0)
            if next_ahead > 0:
                # 2. Check Gitea PR status (only for those active)
                _, pr_data = sb.check_repo_pr(repo)
        GLib.idle_add(self.add_forward_result, repo, sync_data, pr_data)

    def add_forward_result(self, name, sync_data, pr_data):
        self.fwd_completed_count += 1

        next_ahead = sync_data.get("next_ahead", 0)
        if next_ahead > 0:
            row = ForwardRow(name, sync_data, pr_data)
            self.fwd_list_box.append(row)

        if self.fwd_completed_count == len(self.repos):
            # Check if any visible rows remain after filter
            visible_rows = 0
            row = self.fwd_list_box.get_row_at_index(0)
            idx = 0
            while row:
                if self.fwd_filter_func(row):
                    visible_rows += 1
                idx += 1
                row = self.fwd_list_box.get_row_at_index(idx)

            if visible_rows == 0:
                empty_label = Gtk.Label()
                if not self.repos:
                    empty_label.set_markup(
                        "<span size='small' weight='bold' foreground='gray'>No packages found.</span>"
                    )
                else:
                    empty_label.set_markup(
                        "<span size='small' weight='bold' foreground='green'>✅ No unsubmitted changes.</span>\n"
                        "<span size='x-small' foreground='gray'>All next commits are fully submitted to factory.</span>"
                    )
                empty_label.set_justify(Gtk.Justification.CENTER)
                empty_label.set_margin_top(32)
                empty_label.set_margin_bottom(32)
                self.fwd_list_box.set_placeholder(empty_label)

    def on_forward_row_selected(self, list_box, row):
        if not row:
            self.detail_stack.set_visible_child(self.empty_page)
            self.pr_btn.set_sensitive(False)
            self.pr_btn.set_label("Create Pull Request")
            return

        self.detail_stack.set_visible_child_name("diff")
        self.pr_btn.set_sensitive(True)
        self.current_selected_package = row.package_name

        if row.has_pr:
            self.pr_btn.set_label("Show Pull Request")
        else:
            self.pr_btn.set_label("Create Pull Request")

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
            row = self.fwd_list_box.get_selected_row()
            if row and row.has_pr and row.pr_url:
                webbrowser.open(row.pr_url)
                return

            # Disable button and show checking state
            self.pr_btn.set_sensitive(False)
            self.pr_btn.set_label("Checking Gitea...")

            # Run Gitea check in background
            self.executor.submit(self.run_bg_pre_create_check, self.current_selected_package)

    def run_bg_pre_create_check(self, package_name):
        _, pr_data = sb.check_repo_pr(package_name)
        GLib.idle_add(self.on_pre_create_check_result, package_name, pr_data)

    def on_pre_create_check_result(self, package_name, pr_data):
        if not hasattr(self, "current_selected_package") or self.current_selected_package != package_name:
            return

        # Restore button state
        self.pr_btn.set_sensitive(True)

        has_pr = pr_data.get("has_pr", False)
        if has_pr:
            # Update row in-place!
            row = self.fwd_list_box.get_selected_row()
            if row and row.package_name == package_name:
                row.update_pr_state(pr_data)
                self.pr_btn.set_label("Show Pull Request")

            # Show Toast
            toast = Adw.Toast.new("A Pull Request already exists on Gitea!")
            pr_url = pr_data.get("url")
            if pr_url:
                toast.set_button_label("Open PR")
                toast.connect("button-clicked", lambda t, u: webbrowser.open(u), pr_url)
            self.toast_overlay.add_toast(toast)
        else:
            self.pr_btn.set_label("Create Pull Request")
            # Open the dialog!
            dialog = SyncCreatePRDialog(self, package_name)
            dialog.present()


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
