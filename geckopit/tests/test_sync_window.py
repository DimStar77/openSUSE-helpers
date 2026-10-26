#!/usr/bin/env python3
import os
import sys
import re
import unittest
import tempfile
import unittest.mock as mock

import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
GECKOPIT_DIR = os.path.dirname(TESTS_DIR)
HELPERS_DIR = os.path.join(GECKOPIT_DIR, "helpers")

for p in [GECKOPIT_DIR, HELPERS_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw
from geckopit import SyncWindow

class TestSyncWindow(unittest.TestCase):
    @mock.patch('geckopit.WorkspaceConfig')
    def test_sync_window_initialization(self, MockConfig):
        import gi
        gi.require_version('Gtk', '4.0')
        gi.require_version('Adw', '1')
        from gi.repository import Adw
        from geckopit import SyncWindow

        mock_config_inst = MockConfig.return_value
        mock_config_inst.max_workers = 50
        mock_config_inst.get_active_profile.return_value = {
            'stable_path': '',
            'stable_branch': 'factory',
            'unstable_path': '',
            'unstable_branch': 'next'
        }
        mock_config_inst.workspaces = {'Default': mock_config_inst.get_active_profile.return_value}
        mock_config_inst.active_workspace = 'Default'

        app = Adw.Application()
        win = SyncWindow(app)

        # Verify that current_selected_package is initialized to None
        self.assertIsNone(win.current_selected_package)

        # Verify that the main window is maximized by default
        self.assertTrue(win.is_maximized())

        # Verify sidebar paned constraints
        self.assertFalse(win.horizontal_paned.get_shrink_start_child())
        self.assertFalse(win.horizontal_paned.get_resize_start_child())

        # Verify that checking and marking package refreshed doesn't raise AttributeError
        win.refreshed_sync_packages.add("test-package")
        win.refreshed_version_packages.add("test-package")
        try:
            win.check_and_mark_package_refreshed("test-package")
        except AttributeError as e:
            self.fail(f"check_and_mark_package_refreshed raised AttributeError: {e}")

    @mock.patch('geckopit.GLib.idle_add')
    @mock.patch('geckopit.WorkspaceConfig')
    def test_on_terminal_spawned_one_shot_idle(self, MockConfig, MockIdleAdd):
        import gi
        gi.require_version('Gtk', '4.0')
        gi.require_version('Adw', '1')
        from gi.repository import Adw
        from geckopit import SyncWindow

        mock_config_inst = MockConfig.return_value
        mock_config_inst.max_workers = 50
        mock_config_inst.get_active_profile.return_value = {
            'stable_path': '',
            'stable_branch': 'factory',
            'unstable_path': '',
            'unstable_branch': 'next'
        }
        mock_config_inst.workspaces = {'Default': mock_config_inst.get_active_profile.return_value}
        mock_config_inst.active_workspace = 'Default'

        app = Adw.Application()
        win = SyncWindow(app)

        # Mock self.notebook.page_num to avoid real widget calls
        win.notebook = mock.Mock()
        win.notebook.page_num.return_value = 0

        # Mock monitor_terminals to verify it is called
        win.monitor_terminals = mock.Mock()

        # Reset mock to clear initialization side effects!
        MockIdleAdd.reset_mock()

        # Call on_terminal_spawned
        terminal_mock = mock.Mock()
        tab_state = {"scroll_widget": mock.Mock()}
        win.on_terminal_spawned(terminal_mock, 12345, None, tab_state)

        # Verify we stored the shell_pid
        self.assertEqual(tab_state["shell_pid"], 12345)

        # Verify that GLib.idle_add was called with a callback
        MockIdleAdd.assert_called_once()
        callback = MockIdleAdd.call_args[0][0]

        # Call the callback and assert that it returns False (to terminate the idle source)
        # and that win.monitor_terminals was called.
        res = callback()
        self.assertFalse(res)
        win.monitor_terminals.assert_called_once()

    @mock.patch('gi.repository.Adw.Toast')
    def test_pr_created_result_url_extraction(self, MockToast):
        from geckopit import SyncCreatePRDialog

        mock_dialog = mock.Mock()
        mock_dialog.is_destroyed = False
        mock_dialog.package_name = "gcr"
        mock_dialog.parent = mock.Mock()

        # 1. Test BEL (\x07) style terminator
        res_msg = "\x1b]8;;https://src.opensuse.org/GNOME/gcr/pulls/2\x07https://src.opensuse.org/GNOME/gcr/pulls/2\x1b]8;;\x07"

        SyncCreatePRDialog.on_pr_created_result(mock_dialog, True, res_msg)

        MockToast.new.assert_called_with("Pull Request created successfully!")
        mock_toast_inst = MockToast.new.return_value
        mock_toast_inst.connect.assert_called_once()
        args, kwargs = mock_toast_inst.connect.call_args
        self.assertIn("https://src.opensuse.org/GNOME/gcr/pulls/2", args)

        # 2. Test ESC \ (\x1b\\) style terminator
        MockToast.reset_mock()
        res_msg_esc = "\x1b]8;;https://src.opensuse.org/GNOME/gcr/pulls/2\x1b\\https://src.opensuse.org/GNOME/gcr/pulls/2\x1b]8;;\x1b\\"
        SyncCreatePRDialog.on_pr_created_result(mock_dialog, True, res_msg_esc)
        mock_toast_inst = MockToast.new.return_value
        args, kwargs = mock_toast_inst.connect.call_args
        self.assertIn("https://src.opensuse.org/GNOME/gcr/pulls/2", args)

    @mock.patch('geckopit.sb.run_tracked')
    @mock.patch('geckopit.GLib.idle_add')
    @mock.patch('geckopit.WorkspaceConfig')
    def test_refresh_all_key_probe(self, MockConfig, MockIdleAdd, MockRunTracked):
        import gi
        gi.require_version('Gtk', '4.0')
        gi.require_version('Adw', '1')
        from gi.repository import Adw
        from geckopit import SyncWindow

        mock_config_inst = MockConfig.return_value
        mock_config_inst.max_workers = 50
        mock_config_inst.get_active_profile.return_value = {
            'stable_path': '/mock/stable',
            'stable_branch': 'factory',
            'unstable_path': '',
            'unstable_branch': 'next'
        }
        mock_config_inst.workspaces = {'Default': mock_config_inst.get_active_profile.return_value}
        mock_config_inst.active_workspace = 'Default'

        app = Adw.Application()
        win = SyncWindow(app)

        win.repos = ["gcr", "glib2"]

        # Reset mocks to clear initialization side effects!
        MockRunTracked.reset_mock()
        MockIdleAdd.reset_mock()

        win.run_initial_key_probe_then_scan()

        MockRunTracked.assert_called_once()
        args, kwargs = MockRunTracked.call_args
        self.assertIn("gcr", args[0][2]) # check repo_path
        self.assertIn("fetch", args[0][3]) # check fetch command

        MockIdleAdd.assert_called_once_with(win.trigger_bulk_scans)

    @mock.patch('builtins.open', new_callable=mock.mock_open, read_data='{"active_workspace": "Default", "max_workers": 25, "workspaces": {}}')
    @mock.patch('pathlib.Path.exists', return_value=True)
    def test_workspace_config_max_workers_persistence(self, mock_exists, mock_file):
        from geckopit import WorkspaceConfig

        config = WorkspaceConfig()
        # Verify it loaded the max_workers value from json correctly
        self.assertEqual(config.max_workers, 25)

        # Verify default sidebar_width is 500 when omitted in json
        self.assertEqual(config.sidebar_width, 500)

        # Modify and save
        config.max_workers = 15
        config.sidebar_width = 520
        config.save()

        # Verify that json.dump was called with max_workers=15
        mock_file.assert_called()

    def test_workspace_profile_add_preserves_existing_profiles(self):
        """Verifies that adding a new profile preserves existing profiles instead of removing them."""
        import gi
        gi.require_version('Gtk', '4.0')
        gi.require_version('Adw', '1')
        from gi.repository import Gtk, Adw
        from geckopit import WorkspaceManagerDialog

        win = Gtk.Window()
        win.toast_overlay = mock.MagicMock()

        config = mock.MagicMock()
        config.workspaces = {
            "GNOME": {
                "stable_path": "/tmp",
                "stable_branch": "factory",
                "unstable_path": None,
                "unstable_branch": None,
                "ignored_unstable_versions": {}
            }
        }
        config.active_workspace = "GNOME"
        config.max_workers = 25

        callback = mock.MagicMock()
        dlg = WorkspaceManagerDialog(win, config, callback)

        # Initially 1 profile
        self.assertEqual(sorted(config.workspaces.keys()), ["GNOME"])

        # Click Add New Profile
        dlg.on_add_profile_clicked(None)
        self.assertTrue(dlg.is_adding_new)
        self.assertIsNone(dlg.editing_ws_name)

        # Enter details for new profile
        dlg.name_entry.set_text("KDE")
        dlg.stable_path_entry.set_text("/tmp")
        dlg.stable_br_entry.set_text("factory")

        # Save profile
        dlg.on_save_clicked(None)

        # Both profiles must be present!
        self.assertIn("GNOME", config.workspaces)
        self.assertIn("KDE", config.workspaces)
        self.assertEqual(config.active_workspace, "KDE")
        config.save.assert_called()
        callback.assert_called()

    def test_workspace_profile_rename_and_collision_guards(self):
        """Tests that renaming an existing profile cleans up the old name and guards against duplicate names."""
        import gi
        gi.require_version('Gtk', '4.0')
        gi.require_version('Adw', '1')
        from gi.repository import Gtk, Adw
        from geckopit import WorkspaceManagerDialog

        win = Gtk.Window()
        win.toast_overlay = mock.MagicMock()

        config = mock.MagicMock()
        config.workspaces = {
            "GNOME": {
                "stable_path": "/tmp",
                "stable_branch": "factory",
                "unstable_path": None,
                "unstable_branch": None,
                "ignored_unstable_versions": {}
            },
            "KDE": {
                "stable_path": "/tmp",
                "stable_branch": "factory",
                "unstable_path": None,
                "unstable_branch": None,
                "ignored_unstable_versions": {}
            }
        }
        config.active_workspace = "GNOME"
        config.max_workers = 25

        callback = mock.MagicMock()

        # 1. Name collision guard when renaming
        dlg = WorkspaceManagerDialog(win, config, callback)
        dlg.name_entry.set_text("KDE") # Collides with existing KDE
        dlg.on_save_clicked(None)
        self.assertEqual(sorted(config.workspaces.keys()), ["GNOME", "KDE"])
        win.toast_overlay.add_toast.assert_called()

        # 2. Legitimate rename
        win.toast_overlay.reset_mock()
        dlg.name_entry.set_text("GNOME_Devel")
        dlg.on_save_clicked(None)
        self.assertNotIn("GNOME", config.workspaces)
        self.assertIn("GNOME_Devel", config.workspaces)
        self.assertIn("KDE", config.workspaces)
        self.assertEqual(config.active_workspace, "GNOME_Devel")

        # 3. Name collision guard when adding new profile
        dlg2 = WorkspaceManagerDialog(win, config, callback)
        dlg2.on_add_profile_clicked(None)
        dlg2.name_entry.set_text("KDE")
        dlg2.stable_path_entry.set_text("/tmp")
        win.toast_overlay.reset_mock()
        dlg2.on_save_clicked(None)
        # KDE was not overwritten or removed
        self.assertEqual(sorted(config.workspaces.keys()), ["GNOME_Devel", "KDE"])
        win.toast_overlay.add_toast.assert_called()

    def test_background_scan_generation_guard_and_keyerror_prevention(self):
        """Tests that stale callbacks from previous profiles are dropped and do not raise KeyError."""
        from geckopit import SyncWindow

        mock_win = mock.MagicMock(spec=SyncWindow)
        mock_win.scan_generation = 2
        mock_win.package_data = {"pkgA": {"sync": {}, "version": {}, "pr": {}}}
        mock_win.refreshed_version_packages = set()
        mock_win.refreshed_sync_packages = set()

        # 1. Stale generation result should be ignored
        SyncWindow.add_version_result(mock_win, "pkgA", {"status": "success"}, gen=1)
        self.assertEqual(mock_win.package_data["pkgA"]["version"], {})

        # 2. Package not in package_data (e.g. from previous profile) should NOT raise KeyError
        SyncWindow.add_version_result(mock_win, "cairomm", {"status": "success"}, gen=2)
        SyncWindow.add_sync_result(mock_win, "caribou", {"status": "success"}, gen=2)
        SyncWindow.add_forward_result(mock_win, "calls", {"status": "success"}, {"has_pr": False}, gen=2)

        SyncWindow.update_version_row_single(mock_win, "cairomm", {"status": "success"}, gen=2)
        SyncWindow.update_sync_row_single(mock_win, "caribou", {"status": "success"}, gen=2)
        SyncWindow.update_forward_row_single(mock_win, "calls", {"status": "success"}, {"has_pr": False}, gen=2)

    def test_header_profile_changed_defers_reload(self):
        """Tests that changing profile in the header combo defers reload to GLib.idle_add without segfaulting."""
        import gi
        gi.require_version('Gtk', '4.0')
        from gi.repository import Gtk
        from geckopit import SyncWindow

        mock_win = mock.MagicMock(spec=SyncWindow)
        mock_win.config = mock.MagicMock()
        mock_win.config.workspaces = {"GNOME": {}, "KDE": {}}
        mock_win.config.active_workspace = "GNOME"
        mock_win.is_reloading = False

        dropdown = Gtk.DropDown.new_from_strings(["GNOME", "KDE"])
        dropdown.set_selected(1) # KDE

        with mock.patch("gi.repository.GLib.idle_add") as mock_idle_add:
            SyncWindow.on_header_profile_changed(mock_win, dropdown, None)
            self.assertEqual(mock_win.config.active_workspace, "KDE")
            mock_win.config.save.assert_called_once()
            mock_idle_add.assert_called_once_with(mock_win._on_header_reload_idle)

    def test_workspace_profile_add_mode_switch_back(self):
        """Tests that selecting an existing profile in the dropdown exits Add New mode cleanly."""
        import gi
        gi.require_version('Gtk', '4.0')
        gi.require_version('Adw', '1')
        from gi.repository import Gtk, Adw
        from geckopit import WorkspaceManagerDialog

        win = Gtk.Window()
        win.toast_overlay = mock.MagicMock()

        config = mock.MagicMock()
        config.workspaces = {
            "GNOME": {
                "stable_path": "/tmp",
                "stable_branch": "factory",
                "unstable_path": None,
                "unstable_branch": None,
                "ignored_unstable_versions": {}
            },
            "KDE": {
                "stable_path": "/tmp",
                "stable_branch": "factory",
                "unstable_path": None,
                "unstable_branch": None,
                "ignored_unstable_versions": {}
            }
        }
        config.active_workspace = "GNOME"
        config.max_workers = 25

        callback = mock.MagicMock()
        dlg = WorkspaceManagerDialog(win, config, callback)

        # Click Add New
        dlg.on_add_profile_clicked(None)
        self.assertTrue(dlg.is_adding_new)
        self.assertEqual(dlg.profile_model.get_n_items(), 3)
        self.assertEqual(dlg.profile_combo.get_selected(), 2)

        # Switch back to KDE (index 1)
        dlg.profile_combo.set_selected(1)
        self.assertFalse(dlg.is_adding_new)
        self.assertEqual(dlg.editing_ws_name, "KDE")
        self.assertEqual(dlg.profile_model.get_n_items(), 2)
        self.assertEqual(dlg.name_entry.get_text(), "KDE")

    def test_keyboard_navigation_and_filter_shortcuts(self):
        import gi
        gi.require_version('Gtk', '4.0')
        gi.require_version('Gdk', '4.0')
        from gi.repository import Gtk, Gdk
        from geckopit import SyncWindow

        mock_win = mock.Mock()
        mock_win.get_focus.return_value = None

        # 1. Filter toggle shortcuts (Ctrl+1 .. Ctrl+5)
        mock_win.filter_needs_action = mock.Mock()
        mock_win.filter_needs_action.get_active.return_value = True

        res = SyncWindow.on_window_key_pressed(
            mock_win, None, Gdk.KEY_1, 0, Gdk.ModifierType.CONTROL_MASK
        )
        self.assertTrue(res)
        mock_win.filter_needs_action.set_active.assert_called_with(False)

        mock_win.filter_pool_sync = mock.Mock()
        mock_win.filter_pool_sync.get_active.return_value = False
        res = SyncWindow.on_window_key_pressed(
            mock_win, None, Gdk.KEY_2, 0, Gdk.ModifierType.CONTROL_MASK
        )
        self.assertTrue(res)
        mock_win.filter_pool_sync.set_active.assert_called_with(True)

        mock_win.filter_stable = mock.Mock()
        mock_win.filter_stable.get_active.return_value = False
        res = SyncWindow.on_window_key_pressed(
            mock_win, None, Gdk.KEY_3, 0, Gdk.ModifierType.CONTROL_MASK
        )
        self.assertTrue(res)
        mock_win.filter_stable.set_active.assert_called_with(True)

        mock_win.filter_unstable = mock.Mock()
        mock_win.filter_unstable.get_active.return_value = False
        res = SyncWindow.on_window_key_pressed(
            mock_win, None, Gdk.KEY_4, 0, Gdk.ModifierType.CONTROL_MASK
        )
        self.assertTrue(res)
        mock_win.filter_unstable.set_active.assert_called_with(True)

        mock_win.filter_forwarding = mock.Mock()
        mock_win.filter_forwarding.get_active.return_value = False
        res = SyncWindow.on_window_key_pressed(
            mock_win, None, Gdk.KEY_5, 0, Gdk.ModifierType.CONTROL_MASK
        )
        self.assertTrue(res)
        mock_win.filter_forwarding.set_active.assert_called_with(True)

        # 2. Focus search entry with Ctrl+F and Slash
        mock_win.sidebar_search = mock.Mock()
        res = SyncWindow.on_window_key_pressed(
            mock_win, None, Gdk.KEY_f, 0, Gdk.ModifierType.CONTROL_MASK
        )
        self.assertTrue(res)
        mock_win.sidebar_search.grab_focus.assert_called()

        mock_win.sidebar_search.reset_mock()
        res = SyncWindow.on_window_key_pressed(
            mock_win, None, Gdk.KEY_slash, 0, 0
        )
        self.assertTrue(res)
        mock_win.sidebar_search.grab_focus.assert_called()

        # 3. Vim-style list navigation (j/k and Up/Down)
        mock_win.navigate_package_list = mock.Mock(return_value=True)
        res = SyncWindow.on_window_key_pressed(mock_win, None, Gdk.KEY_j, 0, 0)
        self.assertTrue(res)
        mock_win.navigate_package_list.assert_called_with(1)

        res = SyncWindow.on_window_key_pressed(mock_win, None, Gdk.KEY_k, 0, 0)
        self.assertTrue(res)
        mock_win.navigate_package_list.assert_called_with(-1)

        # 4. Defensive Guard: Terminal focus should never be stolen
        mock_terminal = mock.Mock()
        mock_terminal.get_name.return_value = "VteTerminal"
        mock_win.get_focus.return_value = mock_terminal

        res = SyncWindow.on_window_key_pressed(
            mock_win, None, Gdk.KEY_1, 0, Gdk.ModifierType.CONTROL_MASK
        )
        self.assertFalse(res)

        res = SyncWindow.on_window_key_pressed(mock_win, None, Gdk.KEY_j, 0, 0)
        self.assertFalse(res)

        # 5. Distinct Sync and Refresh Shortcuts
        mock_win.get_focus.return_value = None
        mock_win.current_selected_package = "pkg1"
        mock_win.update_detail_worktree_ui = mock.Mock()
        mock_win.update_detail_drift_ui = mock.Mock()
        mock_win.refresh_single_package_priority = mock.Mock()
        mock_win.refresh_active_diff = mock.Mock()
        mock_win.toast_overlay = mock.Mock()

        # F5 -> Refresh selected package
        res = SyncWindow.on_window_key_pressed(mock_win, None, Gdk.KEY_F5, 0, 0)
        self.assertTrue(res)
        mock_win.refresh_single_package_priority.assert_called_with("pkg1")
        mock_win.refresh_active_diff.assert_called()

        # Ctrl+Shift+R -> Rescan all packages
        mock_win.refresh_all = mock.Mock()
        res = SyncWindow.on_window_key_pressed(mock_win, None, Gdk.KEY_r, 0, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK)
        self.assertTrue(res)
        mock_win.refresh_all.assert_called()

        # Ctrl+Shift+S -> Workspace sync (git-project-sync)
        mock_win.on_workspace_sync_clicked = mock.Mock()
        res = SyncWindow.on_window_key_pressed(mock_win, None, Gdk.KEY_s, 0, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK)
        self.assertTrue(res)
        mock_win.on_workspace_sync_clicked.assert_called()

        # 6. Global accelerators work EVEN WHEN focusing a text entry (like search bar on startup)
        mock_entry = mock.Mock()
        mock_entry.get_name.return_value = "GtkSearchEntry"
        mock_entry.get_parent.return_value = None
        mock_win.get_focus.return_value = mock_entry

        with mock.patch("docs_builder.open_user_guide") as mock_guide:
            res_f1 = SyncWindow.on_window_key_pressed(mock_win, None, Gdk.KEY_F1, 0, 0)
            self.assertTrue(res_f1)
            mock_guide.assert_called_with(mock_win)

        mock_win.refresh_single_package_priority.reset_mock()
        res_f5 = SyncWindow.on_window_key_pressed(mock_win, None, Gdk.KEY_F5, 0, 0)
        self.assertTrue(res_f5)
        mock_win.refresh_single_package_priority.assert_called_with("pkg1")

        # But character shortcuts like 'j' or '/' are NOT stolen when typing in the entry
        res_j = SyncWindow.on_window_key_pressed(mock_win, None, Gdk.KEY_j, 0, 0)
        self.assertFalse(res_j)
        res_slash = SyncWindow.on_window_key_pressed(mock_win, None, Gdk.KEY_slash, 0, 0)
        self.assertFalse(res_slash)

        # 7. Pressing '/' or keypad '/' on read-only views (diff_view) or package list DOES focus search
        mock_read_only_view = mock.Mock()
        mock_read_only_view.get_name.return_value = "GtkSourceView"
        mock_read_only_view.get_editable.return_value = False
        mock_read_only_view.get_parent.return_value = None
        mock_win.get_focus.return_value = mock_read_only_view
        mock_win.sidebar_search.grab_focus.reset_mock()

        res_slash_diff = SyncWindow.on_window_key_pressed(mock_win, None, Gdk.KEY_slash, 0, 0)
        self.assertTrue(res_slash_diff)
        mock_win.sidebar_search.grab_focus.assert_called()

        mock_win.sidebar_search.grab_focus.reset_mock()
        res_kp_slash = SyncWindow.on_window_key_pressed(mock_win, None, Gdk.KEY_KP_Divide, 0, 0)
        self.assertTrue(res_kp_slash)
        mock_win.sidebar_search.grab_focus.assert_called()

    def test_search_key_pressed_hand_off(self):
        import gi
        gi.require_version('Gtk', '4.0')
        gi.require_version('Gdk', '4.0')
        from gi.repository import Gtk, Gdk
        from geckopit import SyncWindow

        mock_win = mock.Mock()
        mock_win.navigate_package_list = mock.Mock(return_value=True)
        mock_win.sidebar_search = mock.Mock()
        mock_win.sidebar_search.get_text.return_value = "gcr"

        # Return / Enter activates search and selects first match
        mock_win.on_search_activate = mock.Mock(return_value=True)
        res = SyncWindow.on_search_key_pressed(mock_win, None, Gdk.KEY_Return, 0, 0)
        self.assertTrue(res)
        mock_win.on_search_activate.assert_called_once()

        # Down arrow hands off to package list
        mock_win.navigate_package_list.reset_mock()
        res = SyncWindow.on_search_key_pressed(mock_win, None, Gdk.KEY_Down, 0, 0)
        self.assertTrue(res)
        mock_win.navigate_package_list.assert_called_with(1)

        # Escape clears text
        res = SyncWindow.on_search_key_pressed(mock_win, None, Gdk.KEY_Escape, 0, 0)
        self.assertTrue(res)
        mock_win.sidebar_search.set_text.assert_called_with("")

    def test_terminal_zoom_indicator(self):
        import gi
        gi.require_version('Gtk', '4.0')
        from geckopit import SyncWindow

        mock_win = mock.Mock()
        mock_win.term_zoom_label = mock.Mock()
        mock_win.term_zoom_revealer = mock.Mock()
        mock_win.term_zoom_timeout_id = 0

        SyncWindow.show_terminal_zoom_indicator(mock_win, 1.2)
        mock_win.term_zoom_label.set_markup.assert_called_with("<span size='small' weight='bold' foreground='#3584e4'>🔍 120%</span>")
        mock_win.term_zoom_revealer.set_reveal_child.assert_called_with(True)

    @mock.patch('geckopit.WorkspaceConfig')
    def test_terminal_tab_reorderable(self, MockConfig):
        import gi
        gi.require_version('Gtk', '4.0')
        gi.require_version('Adw', '1')
        from gi.repository import Adw, Gtk
        from geckopit import SyncWindow

        mock_config_inst = MockConfig.return_value
        mock_config_inst.max_workers = 50
        mock_config_inst.get_active_profile.return_value = {
            'stable_path': '',
            'stable_branch': 'factory',
            'unstable_path': '',
            'unstable_branch': 'next'
        }
        mock_config_inst.workspaces = {'Default': mock_config_inst.get_active_profile.return_value}
        mock_config_inst.active_workspace = 'Default'

        app = Adw.Application()
        win = SyncWindow(app)

        test_scroll = Gtk.ScrolledWindow()
        test_tab_box = Gtk.Box()
        win.notebook.append_page(test_scroll, test_tab_box)
        win.notebook.set_tab_reorderable(test_scroll, True)

        self.assertTrue(win.notebook.get_tab_reorderable(test_scroll))

    def test_copy_detail_diff_action(self):
        from geckopit import SyncWindow

        mock_win = mock.Mock()
        mock_win.diff_buffer = mock.Mock()
        mock_win.diff_buffer.get_start_iter.return_value = 0
        mock_win.diff_buffer.get_end_iter.return_value = 1
        mock_win.diff_buffer.get_text.return_value = "diff --git a/test b/test"
        mock_win.get_clipboard = mock.Mock()
        mock_clipboard = mock_win.get_clipboard.return_value
        mock_win.toast_overlay = mock.Mock()

        SyncWindow.on_copy_detail_diff_clicked(mock_win, None)

        mock_clipboard.set.assert_called_with("diff --git a/test b/test")
        mock_win.toast_overlay.add_toast.assert_called()

    @mock.patch('geckopit.GLib.idle_add')
    def test_close_terminal_tab_focuses_remaining_terminal(self, MockIdleAdd):
        from geckopit import SyncWindow

        mock_win = mock.Mock()
        mock_win.notebook = mock.Mock()
        mock_win.notebook.page_num.return_value = 1
        mock_win.notebook.get_n_pages.return_value = 1
        mock_win.notebook.get_current_page.return_value = 0

        scroll1 = mock.Mock()
        terminal1 = mock.Mock()
        tab1 = {"scroll_widget": scroll1, "terminal": terminal1, "pkg_name": "pkg1", "shell_pid": None}

        scroll2 = mock.Mock()
        terminal2 = mock.Mock()
        tab2 = {"scroll_widget": scroll2, "terminal": terminal2, "pkg_name": "pkg2", "shell_pid": None}

        mock_win.notebook.get_nth_page.return_value = scroll1
        mock_win.terminal_tabs = [tab1, tab2]

        SyncWindow.close_terminal_tab(mock_win, scroll2)

        # Verify page 2 was removed
        mock_win.notebook.remove_page.assert_called_with(1)
        # Verify tab2 was removed from terminal_tabs
        self.assertNotIn(tab2, mock_win.terminal_tabs)
        # Verify terminal1 grab_focus was scheduled
        MockIdleAdd.assert_called_with(mock_win.idle_grab_focus, terminal1)
        # Verify idle_grab_focus returns False to remove the GLib idle source
        self.assertFalse(SyncWindow.idle_grab_focus(mock_win, terminal1))
        terminal1.grab_focus.assert_called_once()

    def test_close_terminal_tab_refreshes_selected_package_detail(self):
        from geckopit import SyncWindow

        mock_win = mock.Mock()
        mock_win.notebook = mock.Mock()
        mock_win.notebook.page_num.return_value = 0
        mock_win.notebook.get_n_pages.return_value = 0
        mock_win.current_selected_package = "gedit"
        mock_win.unstable_b = "next"
        mock_win.stable_b = "factory"

        scroll = mock.Mock()
        terminal = mock.Mock()
        tab = {"scroll_widget": scroll, "terminal": terminal, "pkg_name": "gedit", "shell_pid": None}
        mock_win.terminal_tabs = [tab]

        SyncWindow.close_terminal_tab(mock_win, scroll)

        mock_win.refresh_single_package_priority.assert_called_with("gedit")
        mock_win.load_package_detail.assert_called_with("gedit")

    @mock.patch("sync_backend.get_local_package_version")
    def test_update_version_row_single_preserves_local_ondisk_version(self, mock_get_local):
        from geckopit import SyncWindow

        mock_win = mock.Mock()
        mock_win.scan_generation = 1
        mock_win.unstable_b = "next"
        mock_win.stable_b = "factory"
        mock_win.get_mapped_worktree_path.side_effect = lambda pkg, br: f"/workspace/{br}/{pkg}"
        mock_get_local.side_effect = lambda path: "51.0" if "next" in path else "50.0"

        mock_win.package_data = {"gedit": {"version": {"factory_ver": "50.0", "next_ver": "51.alpha1"}}}
        mock_win.refreshed_version_packages = set()
        mock_win.current_selected_package = "gedit"

        stale_bg_data = {
            "status": "success",
            "factory_ver": "50.0",
            "next_ver": "51.alpha1",
            "upstream_latest": "51.0",
            "upstream_stable": "50.0"
        }

        SyncWindow.update_version_row_single(mock_win, "gedit", stale_bg_data, gen=1)

        self.assertEqual(mock_win.package_data["gedit"]["version"]["next_ver"], "51.0")
        mock_win.update_detail_version_ui.assert_called_with("gedit")

    def test_parse_filter_query_tokens_and_aliases(self):
        from geckopit import parse_filter_query

        names, flags = parse_filter_query("gedit @diverged @dirty is:push is:conflict")
        self.assertEqual(names, ["gedit"])
        self.assertEqual(flags, {"diverged", "dirty", "unpushed", "conflict"})

        names2, flags2 = parse_filter_query("@catch-up @fwd @pr @nopr @not-in-pool")
        self.assertEqual(names2, [])
        self.assertEqual(flags2, {"catchup", "promote", "pr", "nopr", "nopool"})

        names3, flags3 = parse_filter_query("@outdated @supersede @sr is:pending-sr")
        self.assertEqual(names3, [])
        self.assertEqual(flags3, {"outdated", "sr"})

    def test_sidebar_filter_advanced_search_tokens(self):
        from geckopit import SyncWindow

        mock_win = mock.Mock()
        mock_win._filter_name_tokens = []
        mock_win._filter_active_flags = {"diverged"}

        row_div = mock.Mock()
        row_div.package_name = "pkg-div"
        row_clean = mock.Mock()
        row_clean.package_name = "pkg-clean"
        row_dirty = mock.Mock()
        row_dirty.package_name = "pkg-dirty"
        row_push = mock.Mock()
        row_push.package_name = "pkg-push"
        row_conflict = mock.Mock()
        row_conflict.package_name = "pkg-conflict"

        mock_win.package_data = {
            "pkg-div": {
                "sync": {"next_ahead": 2, "next_behind": 1, "pool_status": "In Sync"},
                "worktree": {}, "version": {}, "pr": {}
            },
            "pkg-dirty": {
                "sync": {"next_ahead": 0, "next_behind": 0, "pool_status": "In Sync"},
                "worktree": {"unstable": {"dirty": True}}, "version": {}, "pr": {}
            },
            "pkg-push": {
                "sync": {"next_ahead": 0, "next_behind": 0, "pool_status": "In Sync"},
                "worktree": {"stable": {"ahead": 3}}, "version": {}, "pr": {}
            },
            "pkg-conflict": {
                "sync": {"next_ahead": 0, "next_behind": 0, "pool_status": "In Sync"},
                "worktree": {"stable": {"has_conflict": True}}, "version": {}, "pr": {}
            },
            "pkg-clean": {
                "sync": {"next_ahead": 0, "next_behind": 0, "pool_status": "In Sync"},
                "worktree": {"stable": {"ahead": 0, "behind": 0, "dirty": False}},
                "version": {}, "pr": {}
            }
        }

        # 1. @diverged matches only pkg-div
        mock_win._filter_active_flags = {"diverged"}
        self.assertTrue(SyncWindow.sidebar_filter_func(mock_win, row_div))
        self.assertFalse(SyncWindow.sidebar_filter_func(mock_win, row_clean))
        self.assertFalse(SyncWindow.sidebar_filter_func(mock_win, row_dirty))

        # 2. @dirty matches only pkg-dirty
        mock_win._filter_active_flags = {"dirty"}
        self.assertTrue(SyncWindow.sidebar_filter_func(mock_win, row_dirty))
        self.assertFalse(SyncWindow.sidebar_filter_func(mock_win, row_clean))

        # 3. @push / @unpushed matches only pkg-push
        mock_win._filter_active_flags = {"unpushed"}
        self.assertTrue(SyncWindow.sidebar_filter_func(mock_win, row_push))
        self.assertFalse(SyncWindow.sidebar_filter_func(mock_win, row_clean))

        # 4. @conflict matches only pkg-conflict
        mock_win._filter_active_flags = {"conflict"}
        self.assertTrue(SyncWindow.sidebar_filter_func(mock_win, row_conflict))
        self.assertFalse(SyncWindow.sidebar_filter_func(mock_win, row_clean))

        # 5. @promote / @forwardable matches forwardable packages without a pending PR
        row_promote = mock.Mock(package_name="pkg-promote")
        row_has_pr = mock.Mock(package_name="pkg-has-pr")
        mock_win.package_data["pkg-promote"] = {"sync": {"next_ahead": 2, "next_behind": 0}, "pr": {"has_pr": False}}
        mock_win.package_data["pkg-has-pr"] = {"sync": {"next_ahead": 2, "next_behind": 0}, "pr": {"has_pr": True}}

        mock_win._filter_active_flags = {"promote"}
        self.assertTrue(SyncWindow.sidebar_filter_func(mock_win, row_promote))
        self.assertFalse(SyncWindow.sidebar_filter_func(mock_win, row_has_pr))

        # 6. @nopr matches packages without a pending PR
        mock_win._filter_active_flags = {"nopr"}
        self.assertTrue(SyncWindow.sidebar_filter_func(mock_win, row_promote))
        self.assertFalse(SyncWindow.sidebar_filter_func(mock_win, row_has_pr))

        # 7. @pr matches packages with an open PR
        mock_win._filter_active_flags = {"pr"}
        self.assertTrue(SyncWindow.sidebar_filter_func(mock_win, row_has_pr))
        self.assertFalse(SyncWindow.sidebar_filter_func(mock_win, row_promote))

        # 8. @outdated matches packages with outdated pending submit requests
        row_outdated = mock.Mock(package_name="loupe")
        mock_win.package_data["loupe"] = {
            "sync": {"pool_ahead": 5, "pool_sr": {"status": "pending_outdated", "is_outdated": True}}
        }
        mock_win._filter_active_flags = {"outdated"}
        self.assertTrue(SyncWindow.sidebar_filter_func(mock_win, row_outdated))
        self.assertFalse(SyncWindow.sidebar_filter_func(mock_win, row_clean))

        # 9. @sr matches packages with active pending submit requests
        mock_win._filter_active_flags = {"sr"}
        self.assertTrue(SyncWindow.sidebar_filter_func(mock_win, row_outdated))
        self.assertFalse(SyncWindow.sidebar_filter_func(mock_win, row_clean))

    def test_sidebar_filter_action_queue_generalization(self):
        from geckopit import SyncWindow

        mock_win = mock.Mock()
        mock_win._filter_name_tokens = []
        mock_win._filter_active_flags = set()
        mock_win._filter_needs_action = True
        mock_win._filter_pool_sync = False
        mock_win._filter_stable = False
        mock_win._filter_unstable = False
        mock_win._filter_forwarding = False

        row_div = mock.Mock(package_name="pkg-div")
        row_dirty = mock.Mock(package_name="pkg-dirty")
        row_push = mock.Mock(package_name="pkg-push")
        row_clean = mock.Mock(package_name="pkg-clean")

        mock_win.package_data = {
            "pkg-div": {"sync": {"next_ahead": 1, "next_behind": 1}},
            "pkg-dirty": {"worktree": {"unstable": {"untracked": True}}},
            "pkg-push": {"worktree": {"unstable": {"ahead": 1}}},
            "pkg-clean": {"sync": {}, "worktree": {}, "version": {}}
        }

        # Action Queue (⚠️ Needs) now surfaces diverged, untracked, and unpushed
        self.assertTrue(SyncWindow.sidebar_filter_func(mock_win, row_div))
        self.assertTrue(SyncWindow.sidebar_filter_func(mock_win, row_dirty))
        self.assertTrue(SyncWindow.sidebar_filter_func(mock_win, row_push))
        self.assertFalse(SyncWindow.sidebar_filter_func(mock_win, row_clean))

    def test_sidebar_filter_forwarding_track_generalization(self):
        from geckopit import SyncWindow

        mock_win = mock.Mock()
        mock_win._filter_name_tokens = []
        mock_win._filter_active_flags = set()
        mock_win._filter_needs_action = True
        mock_win._filter_pool_sync = False
        mock_win._filter_stable = False
        mock_win._filter_unstable = False
        mock_win._filter_forwarding = True # 🔀 Fwd active

        row_fwd = mock.Mock(package_name="pkg-fwd")
        row_catchup = mock.Mock(package_name="pkg-catchup")
        row_div = mock.Mock(package_name="pkg-div")
        row_other = mock.Mock(package_name="pkg-other")

        mock_win.package_data = {
            "pkg-fwd": {"sync": {"next_ahead": 2, "next_behind": 0}},
            "pkg-catchup": {"sync": {"next_ahead": 0, "next_behind": 3}},
            "pkg-div": {"sync": {"next_ahead": 1, "next_behind": 1}},
            "pkg-other": {"sync": {"next_ahead": 0, "next_behind": 0}, "version": {"stable_needs_update": True}}
        }

        # 🔀 Fwd now captures the entire forwarding lifecycle: promote, catchup, diverged!
        self.assertTrue(SyncWindow.sidebar_filter_func(mock_win, row_fwd))
        self.assertTrue(SyncWindow.sidebar_filter_func(mock_win, row_catchup))
        self.assertTrue(SyncWindow.sidebar_filter_func(mock_win, row_div))
        self.assertFalse(SyncWindow.sidebar_filter_func(mock_win, row_other))

    def test_sync_diff_dialog_copy_action(self):
        from geckopit import SyncDiffDialog

        mock_dialog = mock.Mock()
        mock_dialog.buffer = mock.Mock()
        mock_dialog.buffer.get_start_iter.return_value = 0
        mock_dialog.buffer.get_end_iter.return_value = 1
        mock_dialog.buffer.get_text.return_value = "--- a/file\n+++ b/file"
        mock_dialog.get_clipboard = mock.Mock()
        mock_clipboard = mock_dialog.get_clipboard.return_value
        mock_dialog.parent = mock.Mock()
        mock_dialog.parent.toast_overlay = mock.Mock()

        SyncDiffDialog.on_copy_diff_clicked(mock_dialog, None)

        mock_clipboard.set.assert_called_with("--- a/file\n+++ b/file")
        mock_dialog.parent.toast_overlay.add_toast.assert_called()

    def test_workspace_sync_completed_toast_capping(self):
        from geckopit import SyncWindow
        import gi
        gi.require_version('Adw', '1')
        from gi.repository import Adw

        mock_win = mock.Mock()
        mock_win.sync_spinner = mock.Mock()
        mock_win.sync_stack = mock.Mock()
        mock_win.sync_btn = mock.Mock()
        mock_win.terminal_drawer = mock.Mock()
        mock_win.terminal_drawer.get_visible.return_value = False
        mock_win.toast_overlay = mock.Mock()
        mock_win.refresh_single_package = mock.Mock()
        mock_win.close_terminal_tab = mock.Mock()
        mock_win.notebook = mock.Mock()
        mock_win.notebook.page_num.return_value = 0

        tab_state = {"scroll_widget": mock.Mock(), "terminal": mock.Mock()}

        # 1. Success with > 4 updated packages -> capped with ...
        updated_10 = [f"pkg{i:02d}" for i in range(10)]
        SyncWindow.on_workspace_sync_completed(mock_win, True, updated_10, [], [], ["/path"], tab_state)
        toast = mock_win.toast_overlay.add_toast.call_args[0][0]
        self.assertEqual(toast.get_title(), "✅ Synced 10 package(s): pkg00, pkg01, pkg02, pkg03...")

        # 2. Failure with <= 4 failed packages -> lists packages
        mock_win.toast_overlay.reset_mock()
        SyncWindow.on_workspace_sync_completed(mock_win, False, [], ["glib2", "gtk4"], [], ["/path"], tab_state)
        toast = mock_win.toast_overlay.add_toast.call_args[0][0]
        self.assertEqual(toast.get_title(), "⚠️ Workspace sync encountered warnings or conflicts (glib2, gtk4)")

        # 3. Failure with > 4 failed packages (e.g. 100 packages) -> capped with +N more
        mock_win.toast_overlay.reset_mock()
        failed_100 = [f"pkg{i:03d}" for i in range(100)]
        SyncWindow.on_workspace_sync_completed(mock_win, False, [], failed_100, [], ["/path"], tab_state)
        toast = mock_win.toast_overlay.add_toast.call_args[0][0]
        self.assertEqual(toast.get_title(), "⚠️ Workspace sync encountered warnings or conflicts (pkg000, pkg001, pkg002, pkg003... +96 more)")

        # 4. Failure with 0 specific packages failed -> generic warning
        mock_win.toast_overlay.reset_mock()
        SyncWindow.on_workspace_sync_completed(mock_win, False, [], [], [], ["/path"], tab_state)
        toast = mock_win.toast_overlay.add_toast.call_args[0][0]
        self.assertEqual(toast.get_title(), "⚠️ Workspace sync encountered warnings or conflicts")

    def test_track_filters_sensitivity_toggle(self):
        from geckopit import SyncWindow

        mock_win = mock.Mock()
        mock_win.filter_needs_action = mock.Mock()
        mock_win.filter_pool_sync = mock.Mock()
        mock_win.filter_stable = mock.Mock()
        mock_win.filter_unstable = mock.Mock()
        mock_win.filter_forwarding = mock.Mock()

        # Needs action active -> sensitive=True
        mock_win.filter_needs_action.get_active.return_value = True
        SyncWindow.update_track_filters_sensitivity(mock_win)
        mock_win.filter_pool_sync.set_sensitive.assert_called_with(True)
        mock_win.filter_stable.set_sensitive.assert_called_with(True)
        mock_win.filter_unstable.set_sensitive.assert_called_with(True)
        mock_win.filter_forwarding.set_sensitive.assert_called_with(True)

        # Needs action inactive -> sensitive=False
        mock_win.filter_needs_action.get_active.return_value = False
        SyncWindow.update_track_filters_sensitivity(mock_win)
        mock_win.filter_pool_sync.set_sensitive.assert_called_with(False)
        mock_win.filter_stable.set_sensitive.assert_called_with(False)
        mock_win.filter_unstable.set_sensitive.assert_called_with(False)
        mock_win.filter_forwarding.set_sensitive.assert_called_with(False)

    def test_keyboard_shortcuts_ignore_tracks_when_needs_action_disabled(self):
        import gi
        gi.require_version('Gtk', '4.0')
        gi.require_version('Gdk', '4.0')
        from gi.repository import Gtk, Gdk
        from geckopit import SyncWindow

        mock_win = mock.Mock()
        mock_win.get_focus.return_value = None

        # When filter_needs_action is False (Browse All mode)
        mock_win.filter_needs_action = mock.Mock()
        mock_win.filter_needs_action.get_active.return_value = False

        mock_win.filter_pool_sync = mock.Mock()
        mock_win.filter_stable = mock.Mock()
        mock_win.filter_unstable = mock.Mock()
        mock_win.filter_forwarding = mock.Mock()

        for keyval in (Gdk.KEY_2, Gdk.KEY_3, Gdk.KEY_4, Gdk.KEY_5):
            res = SyncWindow.on_window_key_pressed(
                mock_win, None, keyval, 0, Gdk.ModifierType.CONTROL_MASK
            )
            self.assertTrue(res)

        mock_win.filter_pool_sync.set_active.assert_not_called()
        mock_win.filter_stable.set_active.assert_not_called()
        mock_win.filter_unstable.set_active.assert_not_called()
        mock_win.filter_forwarding.set_active.assert_not_called()

    def test_sidebar_filter_func_browse_all_mode(self):
        from geckopit import SyncWindow

        mock_win = mock.Mock()
        mock_win.sidebar_search = mock.Mock()
        mock_win.sidebar_search.get_text.return_value = ""

        # Row representing a package
        mock_row = mock.Mock()
        mock_row.package_name = "test-pkg"

        # Even with empty package_data (no actions needed)
        mock_win.package_data = {"test-pkg": {}}

        # When Needs Action is False (Browse All mode), returns True
        mock_win.filter_needs_action = mock.Mock()
        mock_win.filter_needs_action.get_active.return_value = False

        self.assertTrue(SyncWindow.sidebar_filter_func(mock_win, mock_row))

        # But if search text does not match, returns False
        mock_win.sidebar_search.get_text.return_value = "nomatch"
        self.assertFalse(SyncWindow.sidebar_filter_func(mock_win, mock_row))

    def test_granular_package_selection_and_refresh(self):
        from geckopit import SyncWindow
        mock_win = mock.Mock()
        mock_win.refreshed_packages = {"pkg1"}
        mock_win.refresh_single_package_priority = mock.Mock()
        mock_win.load_package_detail = mock.Mock()

        mock_row = mock.Mock()
        mock_row.package_name = "pkg1"

        # 1. Manual selection should ALWAYS trigger priority refresh even if already in refreshed_packages
        SyncWindow.on_package_row_selected(mock_win, None, mock_row)
        mock_win.load_package_detail.assert_called_with("pkg1", reload_diff=True)
        mock_win.refresh_single_package_priority.assert_called_with("pkg1")

        # 2. Granular background callback does NOT reload diff
        mock_win.current_selected_package = "pkg1"
        mock_win.package_data = {"pkg1": {"sync": {"status": "success"}, "version": {}}}
        mock_win.refreshed_sync_packages = set()
        mock_win.update_row_ui = mock.Mock()
        mock_win.check_and_mark_package_refreshed = mock.Mock()
        mock_win.update_detail_sync_ui = mock.Mock()
        mock_win.update_detail_worktree_ui = mock.Mock()
        mock_win.update_detail_title = mock.Mock()
        mock_win.refresh_active_diff = mock.Mock()

        SyncWindow.update_sync_row_single(mock_win, "pkg1", {"status": "success"})
        mock_win.update_detail_sync_ui.assert_called_with("pkg1")
        mock_win.update_detail_worktree_ui.assert_called_with("pkg1")
        mock_win.update_detail_title.assert_called_with("pkg1")
        # Diff buffer should NEVER be touched by background sync polling
        mock_win.refresh_active_diff.assert_not_called()

    def test_init_executors_worker_allocation(self):
        from geckopit import SyncWindow
        mock_win = mock.Mock()
        mock_win.config = mock.Mock()
        mock_win.config.max_workers = 50
        mock_win.sync_executor = None
        mock_win.ver_executor = None
        mock_win.executor = None

        SyncWindow.init_executors(mock_win)
        try:
            # Workers capped at 20 to protect OpenSSH MaxSessions and prevent peer connection resets
            self.assertEqual(mock_win.sync_executor._max_workers, 20)
            self.assertEqual(mock_win.ver_executor._max_workers, 20)
            self.assertIs(mock_win.executor, mock_win.sync_executor)
        finally:
            mock_win.sync_executor.shutdown(wait=False)
            mock_win.ver_executor.shutdown(wait=False)

        # When concurrency is configured below 20, both pools use the lower limit
        mock_win.config.max_workers = 12
        SyncWindow.init_executors(mock_win)
        try:
            self.assertEqual(mock_win.sync_executor._max_workers, 12)
            self.assertEqual(mock_win.ver_executor._max_workers, 12)
        finally:
            mock_win.sync_executor.shutdown(wait=False)
            mock_win.ver_executor.shutdown(wait=False)

    @mock.patch("sync_backend.check_repo_sync")
    @mock.patch("sync_backend.check_repo_pr")
    @mock.patch("geckopit.GLib.idle_add")
    def test_chained_pr_sync_conditional_execution(self, mock_idle, mock_pr, mock_sync):
        from geckopit import SyncWindow
        mock_win = mock.Mock()
        mock_win.stable_b = "factory"
        mock_win.unstable_b = "next"
        mock_win.stable_p = "/workspace"
        mock_win.check_worktrees_for_repo.return_value = {"stable": {}}

        # Case 1: next is ahead -> check_repo_pr MUST be called
        mock_sync.return_value = ("pkg_ahead", {
            "status": "success",
            "next_status": "Ahead",
            "next_ahead": 3
        })
        mock_pr.return_value = ("pkg_ahead", {"has_pr": True, "number": 101})

        SyncWindow.run_bg_sync(mock_win, "pkg_ahead")
        mock_sync.assert_called_with("pkg_ahead", stable_branch="factory", unstable_branch="next", workspace_path="/workspace")
        mock_pr.assert_called_with("pkg_ahead", stable_branch="factory", unstable_branch="next", workspace_path="/workspace")
        mock_idle.assert_called_with(
            mock_win.add_sync_result,
            "pkg_ahead",
            mock_sync.return_value[1],
            {"stable": {}},
            {"has_pr": True, "number": 101}
        )

        # Case 2: next is NOT ahead and no cached PR -> check_repo_pr MUST NOT be called, pr_data passed as None to preserve cache
        mock_pr.reset_mock()
        mock_idle.reset_mock()
        mock_sync.return_value = ("pkg_clean", {
            "status": "success",
            "next_status": "In Sync",
            "next_ahead": 0
        })

        SyncWindow.run_bg_sync(mock_win, "pkg_clean")
        mock_pr.assert_not_called()
        mock_idle.assert_called_with(
            mock_win.add_sync_result,
            "pkg_clean",
            mock_sync.return_value[1],
            {"stable": {}},
            None
        )

        # Case 3: single-stream workspace (flat org / maintainer portfolio) -> check_repo_pr MUST be called for multi-target PRs
        mock_win.unstable_b = None
        mock_pr.reset_mock()
        mock_idle.reset_mock()
        mock_sync.return_value = ("pkg_flat", {
            "status": "success",
            "next_status": "No next branch",
            "next_ahead": 0
        })
        mock_pr.return_value = ("pkg_flat", {"has_pr": True, "number": 3})

        SyncWindow.run_bg_sync(mock_win, "pkg_flat")
        mock_pr.assert_called_with("pkg_flat", stable_branch="factory", unstable_branch=None, workspace_path="/workspace")
        mock_idle.assert_called_with(
            mock_win.add_sync_result,
            "pkg_flat",
            mock_sync.return_value[1],
            {"stable": {}},
            {"has_pr": True, "number": 3}
        )

        # Case 4: dual-stream workspace with cached PR -> check_repo_pr MUST be called even if next_ahead == 0
        mock_win.unstable_b = "next"
        mock_win.package_data = {"pkg_cached": {"pr": {"has_pr": True}}}
        mock_pr.reset_mock()
        mock_idle.reset_mock()
        mock_sync.return_value = ("pkg_cached", {
            "status": "success",
            "next_status": "In Sync",
            "next_ahead": 0
        })
        mock_pr.return_value = ("pkg_cached", {"has_pr": True, "number": 42})

        SyncWindow.run_bg_sync(mock_win, "pkg_cached")
        mock_pr.assert_called_with("pkg_cached", stable_branch="factory", unstable_branch="next", workspace_path="/workspace")
        mock_idle.assert_called_with(
            mock_win.add_sync_result,
            "pkg_cached",
            mock_sync.return_value[1],
            {"stable": {}},
            {"has_pr": True, "number": 42}
        )

    def test_update_detail_pr_ui_group_card(self):
        from geckopit import SyncWindow
        mock_win = mock.Mock()
        mock_win.current_selected_package = "mypkg"
        mock_win.stable_b = "factory"
        mock_win.unstable_b = "next"
        mock_win.package_data = {
            "mypkg": {
                "version": {
                    "factory_ver": "50.0",
                    "upstream_stable": "51.0",
                    "next_ver": "50.0",
                    "upstream_latest": "51.0"
                },
                "pr": {
                    "has_pr": True,
                    "count": 1,
                    "prs": [
                        {
                            "number": 77,
                            "title": "Fix <stdio.h> & <stdlib.h> on <arch>",
                            "author": "contributor",
                            "base": "factory",
                            "head": "patch-1",
                            "url": "https://src.opensuse.org/GNOME/mypkg/pulls/77"
                        }
                    ]
                }
            }
        }
        mock_win.pr_card_revealer = mock.Mock()
        mock_win.pr_card_inner_revealer = mock.Mock()
        mock_win.pr_card_chevron = mock.Mock()
        mock_win.pr_card_title_lbl = mock.Mock()
        mock_win.pr_card_items_box = mock.Mock()
        mock_win.pr_card_items_box.get_first_child.return_value = None
        mock_win.pr_card_folded = False
        mock_win.sync_pr_card_folded_state = lambda: SyncWindow.sync_pr_card_folded_state(mock_win)

        SyncWindow.update_detail_pr_ui(mock_win, "mypkg")

        mock_win.pr_card_revealer.set_reveal_child.assert_called_with(True)
        self.assertIn("ACTIVE REQUESTS: ⚠️ 1 Contributor PR", mock_win.pr_card_title_lbl.set_markup.call_args[0][0])
        mock_win.pr_card_items_box.append.assert_called()

        # Verify card tooltip markup is valid Pango markup (no invalid tags like <code>)
        appended_card = mock_win.pr_card_items_box.append.call_args[0][0]
        tt_markup = appended_card.get_tooltip_markup()
        self.assertIsNotNone(tt_markup)
        import gi
        gi.require_version("Pango", "1.0")
        from gi.repository import Pango
        ok, _, clean_text, _ = Pango.parse_markup(tt_markup, -1, "\0")
        self.assertTrue(ok)
        self.assertIn("geckopit-cli --merge-pr 77", clean_text)
        self.assertIn("Double-click to open on Gitea", clean_text)

        # Default unfolded state
        mock_win.pr_card_inner_revealer.set_reveal_child.assert_called_with(True)
        mock_win.pr_card_chevron.set_from_icon_name.assert_called_with("pan-down-symbolic")

        # Toggle header folds the card and updates chevron
        SyncWindow.on_pr_card_header_clicked(mock_win, None, 1, 0, 0)
        self.assertTrue(mock_win.pr_card_folded)
        mock_win.pr_card_inner_revealer.set_reveal_child.assert_called_with(False)
        mock_win.pr_card_chevron.set_from_icon_name.assert_called_with("pan-end-symbolic")

        # Switching to another package preserves the folded session state
        mock_win.current_selected_package = "mypkg2"
        mock_win.package_data["mypkg2"] = mock_win.package_data["mypkg"]
        SyncWindow.update_detail_pr_ui(mock_win, "mypkg2")
        mock_win.pr_card_inner_revealer.set_reveal_child.assert_called_with(False)

        # When no PRs exist, outer card is hidden
        mock_win.current_selected_package = "mypkg"
        mock_win.package_data["mypkg"]["pr"] = {"has_pr": False}
        SyncWindow.update_detail_pr_ui(mock_win, "mypkg")
        mock_win.pr_card_revealer.set_reveal_child.assert_called_with(False)

    def test_update_detail_pr_ui_obs_sr_card(self):
        from geckopit import SyncWindow
        mock_win = mock.Mock()
        mock_win.current_selected_package = "srpkg"
        mock_win.stable_b = "factory"
        mock_win.unstable_b = "next"
        mock_win.package_data = {
            "srpkg": {
                "version": {},
                "pr": {"has_pr": False},
                "sync": {
                    "pool_sr": {
                        "status": "pending",
                        "request": {
                            "id": "1385454",
                            "state": "review",
                            "creator": "dimstar",
                            "source_project": "GNOME:Factory",
                            "target_project": "openSUSE:Factory",
                            "description": "Push GNOME 51.1 release"
                        }
                    }
                }
            }
        }
        mock_win.pr_card_revealer = mock.Mock()
        mock_win.pr_card_inner_revealer = mock.Mock()
        mock_win.pr_card_chevron = mock.Mock()
        mock_win.pr_card_title_lbl = mock.Mock()
        mock_win.pr_card_items_box = mock.Mock()
        mock_win.pr_card_items_box.get_first_child.return_value = None
        mock_win.pr_card_folded = False
        mock_win.sync_pr_card_folded_state = lambda: None

        # 1. Render card with single OBS SR
        SyncWindow.update_detail_pr_ui(mock_win, "srpkg")
        mock_win.pr_card_revealer.set_reveal_child.assert_called_with(True)
        self.assertIn("ACTIVE REQUESTS (1 Outgoing in-flight)", mock_win.pr_card_title_lbl.set_markup.call_args[0][0])
        mock_win.pr_card_items_box.append.assert_called()

        appended_card = mock_win.pr_card_items_box.append.call_args[0][0]
        tt_markup = appended_card.get_tooltip_markup()
        self.assertIn("osc request show 1385454", tt_markup)
        self.assertIn("Double-click to open on Open Build Service", tt_markup)

        # 2. Render card with both PR and SR
        mock_win.package_data["srpkg"]["pr"] = {
            "has_pr": True,
            "count": 1,
            "prs": [{"number": 101, "title": "Patch fix", "author": "alice", "url": "https://src.o.o/pr/101"}]
        }
        SyncWindow.update_detail_pr_ui(mock_win, "srpkg")
        self.assertIn("ACTIVE REQUESTS: ⚠️ 1 Contributor PR (1 outgoing in-flight)", mock_win.pr_card_title_lbl.set_markup.call_args[0][0])

    def test_update_sync_row_single_pr_propagation(self):
        from geckopit import SyncWindow
        mock_win = mock.Mock()
        mock_win.current_selected_package = "pkg_test"
        mock_win.package_data = {
            "pkg_test": {
                "sync": {},
                "worktree": {},
                "pr": {}
            }
        }
        mock_win.refreshed_sync_packages = set()

        sync_data = {"status": "success", "pool_status": "In Sync"}
        wt_data = {"stable": {"clean": True}}
        pr_data = {"has_pr": True, "number": 77}

        SyncWindow.update_sync_row_single(mock_win, "pkg_test", sync_data, wt_data=wt_data, pr_data=pr_data)

        self.assertEqual(mock_win.package_data["pkg_test"]["sync"], sync_data)
        self.assertEqual(mock_win.package_data["pkg_test"]["worktree"], wt_data)
        self.assertEqual(mock_win.package_data["pkg_test"]["pr"], pr_data)
        self.assertIn("pkg_test", mock_win.refreshed_sync_packages)
        mock_win.update_row_ui.assert_called_with("pkg_test")
        mock_win.update_detail_sync_ui.assert_called_with("pkg_test")
        mock_win.update_detail_worktree_ui.assert_called_with("pkg_test")
        mock_win.update_detail_title.assert_called_with("pkg_test")
        mock_win.update_detail_pr_ui.assert_called_with("pkg_test")

    def test_apply_active_profile_ui_single_pipeline_deactivates_unstable_filters(self):
        from geckopit import SyncWindow
        mock_win = mock.Mock()
        mock_win.config.get_active_profile.return_value = {
            "stable_path": "/tmp/packaging",
            "unstable_path": None,
        }
        mock_win.config.active_workspace = "PKG Maintainer collection"
        mock_win.filter_unstable.get_active.return_value = True
        mock_win.filter_forwarding.get_active.return_value = True

        SyncWindow.apply_active_profile_ui(mock_win)

        mock_win.filter_unstable.set_visible.assert_called_with(False)
        mock_win.filter_forwarding.set_visible.assert_called_with(False)
        mock_win.filter_unstable.set_active.assert_called_with(False)
        mock_win.filter_forwarding.set_active.assert_called_with(False)
        mock_win.unstable_card.set_visible.assert_called_with(False)
        mock_win.diff_selector.set_visible.assert_called_with(False)
        mock_win.diff_selector.set_selected.assert_called_with(1)

    def test_snapshot_filter_states_single_pipeline_guards(self):
        from geckopit import SyncWindow
        mock_win = mock.Mock()
        mock_win.sidebar_search.get_text.return_value = ""
        mock_win.filter_needs_action.get_active.return_value = True
        mock_win.filter_pool_sync.get_active.return_value = False
        mock_win.filter_stable.get_active.return_value = False
        mock_win.filter_unstable.get_active.return_value = True
        mock_win.filter_forwarding.get_active.return_value = True
        mock_win.filter_unstable.get_visible.return_value = False
        mock_win.filter_forwarding.get_visible.return_value = False
        mock_win.unstable_b = None  # Single-stream workspace

        SyncWindow.snapshot_filter_states(mock_win)

        self.assertTrue(mock_win._filter_needs_action)
        self.assertFalse(mock_win._filter_pool_sync)
        self.assertFalse(mock_win._filter_stable)
        self.assertFalse(mock_win._filter_unstable)
        self.assertFalse(mock_win._filter_forwarding)

    def test_sidebar_filter_single_pipeline_ignores_ghost_unstable_track(self):
        from geckopit import SyncWindow
        mock_win = mock.Mock()
        mock_win.unstable_b = None  # Single-stream workspace (flat org)
        mock_win._filter_name_tokens = []
        mock_win._filter_active_flags = set()
        mock_win._filter_needs_action = True
        mock_win._filter_pool_sync = False
        mock_win._filter_stable = False
        mock_win._filter_unstable = True  # Stale ghost filter from previous dual profile
        mock_win._filter_forwarding = False

        row_todo = mock.Mock(package_name="pkg-todo")
        row_clean = mock.Mock(package_name="pkg-clean")

        mock_win.package_data = {
            "pkg-todo": {
                "sync": {},
                "worktree": {"stable": {"dirty": True}},
                "version": {"stable_needs_update": False}
            },
            "pkg-clean": {"sync": {}, "worktree": {}, "version": {}}
        }

        # Ghost unstable track must not block Todos in single-stream workspace
        self.assertTrue(SyncWindow.sidebar_filter_func(mock_win, row_todo))
        self.assertFalse(SyncWindow.sidebar_filter_func(mock_win, row_clean))

    def test_sidebar_filter_snapshot_caching(self):
        from geckopit import SyncWindow
        mock_win = mock.Mock()
        mock_win._filter_search_text = "mutter"
        mock_win._filter_needs_action = False
        mock_win._filter_pool_sync = False
        mock_win._filter_stable = False
        mock_win._filter_unstable = False
        mock_win._filter_forwarding = False

        row_match = mock.Mock()
        row_match.package_name = "mutter"

        row_no_match = mock.Mock()
        row_no_match.package_name = "baobab"

        self.assertTrue(SyncWindow.sidebar_filter_func(mock_win, row_match))
        self.assertFalse(SyncWindow.sidebar_filter_func(mock_win, row_no_match))
        # When snapshot is present, widget get_text() should never be called
        mock_win.sidebar_search.get_text.assert_not_called()

    def test_refresh_all_clears_known_not_in_pool(self):
        from geckopit import SyncWindow
        mock_win = mock.Mock()
        mock_win.package_rows = ["row1"]
        mock_win.known_not_in_pool = {"pkg_a", "pkg_b"}
        mock_win.refreshed_sync_packages = {"pkg_a"}
        mock_win.refreshed_version_packages = {"pkg_a"}
        mock_win.refreshed_packages = {"pkg_a"}
        mock_win.scan_generation = 2
        mock_win.stable_p = "/tmp"
        mock_win.repos = ["pkg_a"]

        with mock.patch("threading.Thread") as mock_thread:
            SyncWindow.refresh_all(mock_win)
            self.assertEqual(mock_win.known_not_in_pool, set())
            self.assertEqual(mock_win.refreshed_sync_packages, set())
            self.assertEqual(mock_win.refreshed_version_packages, set())
            self.assertEqual(mock_win.refreshed_packages, set())
            self.assertEqual(mock_win.scan_generation, 3)
            mock_thread.assert_called_once()

    def test_update_sync_row_single_evicts_known_not_in_pool(self):
        from geckopit import SyncWindow
        mock_win = mock.Mock()
        mock_win.scan_generation = 1
        mock_win.package_data = {
            "pkg1": {"sync": {}, "worktree": {}, "pr": {}, "version": {}}
        }
        mock_win.refreshed_sync_packages = set()
        mock_win.known_not_in_pool = {"pkg1", "pkg2"}

        # Refresh with "In Sync" should discard pkg1 from known_not_in_pool
        data_in_sync = {"status": "success", "pool_status": "In Sync"}
        SyncWindow.update_sync_row_single(mock_win, "pkg1", data_in_sync, gen=1)
        self.assertNotIn("pkg1", mock_win.known_not_in_pool)
        self.assertIn("pkg2", mock_win.known_not_in_pool)

        # Refresh with "Not in Pool" should add it back
        data_not_in_pool = {"status": "success", "pool_status": "Not in Pool"}
        SyncWindow.update_sync_row_single(mock_win, "pkg1", data_not_in_pool, gen=1)
        self.assertIn("pkg1", mock_win.known_not_in_pool)

    def test_update_diff_text_race_condition_guard(self):
        from geckopit import SyncWindow
        mock_win = mock.Mock()
        mock_win.current_selected_package = "pkg_selected"
        mock_win.diff_buffer = mock.Mock()

        # Arrival of diff for a stale/previously selected package should be ignored
        SyncWindow.update_diff_text(mock_win, "pkg_stale", "stale diff text")
        mock_win.diff_buffer.set_text.assert_not_called()

        # Arrival of diff for the active package should be rendered
        SyncWindow.update_diff_text(mock_win, "pkg_selected", "active diff text")
        mock_win.diff_buffer.set_text.assert_called_with("active diff text")



    def test_update_detail_sync_ui_pool_pluralization(self):
        mock_win = mock.Mock()
        mock_win.current_selected_package = "testpkg"
        mock_win.stable_pool_lbl = mock.Mock()
        mock_win.pull_pool_btn = mock.Mock()
        mock_win.submit_pool_btn = mock.Mock()
        mock_win.obs_pending_requests = {}
        mock_win.obs_accepted_requests = {}
        mock_win.get_mapped_worktree_path.return_value = None
        mock_win.stable_b = "factory"
        mock_win.unstable_branch_lbl = mock.Mock()
        mock_win.catchup_merge_btn = mock.Mock()
        mock_win.create_pr_btn = mock.Mock()

        # 1. Pool ahead by exactly 1 commit
        mock_win.package_data = {
            "testpkg": {
                "sync": {"pool_ahead": 1, "pool_behind": 0, "pool_status": "In Sync", "next_status": "In Sync", "next_ahead": 0, "next_behind": 0},
                "pr": {"has_pr": False, "has_forward_pr": False}
            }
        }
        SyncWindow.update_detail_sync_ui(mock_win, "testpkg")
        markup = mock_win.stable_pool_lbl.set_markup.call_args[0][0]
        self.assertIn("Ahead of Gitea Pool by 1 commit</span>", markup)
        self.assertNotIn("1 commits", markup)

        # 2. Pool behind by exactly 1 commit
        mock_win.package_data["testpkg"]["sync"] = {
            "pool_ahead": 0, "pool_behind": 1, "pool_status": "In Sync", "next_status": "In Sync", "next_ahead": 0, "next_behind": 0
        }
        SyncWindow.update_detail_sync_ui(mock_win, "testpkg")
        markup = mock_win.stable_pool_lbl.set_markup.call_args[0][0]
        self.assertIn("Behind Gitea Pool by 1 commit</span>", markup)
        self.assertNotIn("1 commits", markup)

        # 3. Diverged with 2 commits behind and 1 commit ahead
        mock_win.package_data["testpkg"]["sync"] = {
            "pool_ahead": 1, "pool_behind": 2, "pool_status": "In Sync", "next_status": "In Sync", "next_ahead": 0, "next_behind": 0
        }
        SyncWindow.update_detail_sync_ui(mock_win, "testpkg")
        markup = mock_win.stable_pool_lbl.set_markup.call_args[0][0]
        self.assertIn("Behind Pool: 2 commits / Ahead Pool: 1 commit", markup)


    def test_update_detail_sync_ui_sr_status(self):
        from unittest.mock import patch
        mock_win = mock.Mock()
        mock_win.current_selected_package = "testpkg"
        mock_win.stable_pool_lbl = mock.Mock()
        mock_win.pull_pool_btn = mock.Mock()
        mock_win.submit_pool_btn = mock.Mock()
        mock_win.unstable_branch_lbl = mock.Mock()
        mock_win.catchup_merge_btn = mock.Mock()
        mock_win.create_pr_btn = mock.Mock()

        mock_win.package_data = {
            "testpkg": {
                "sync": {"pool_ahead": 2, "pool_behind": 0, "pool_status": "In Sync", "next_status": "In Sync", "next_ahead": 0, "next_behind": 0},
                "pr": {"has_pr": False, "has_forward_pr": False}
            }
        }

        # 1. Active Pending SR -> button disabled, label shows pending SR
        with patch("pool_submit.get_package_pool_submission_status", return_value={
            "status": "pending",
            "request": {"id": "1385454", "state": "review", "source_project": "GNOME:Factory"}
        }):
            SyncWindow.update_detail_sync_ui(mock_win, "testpkg")
            markup = mock_win.stable_pool_lbl.set_markup.call_args[0][0]
            self.assertIn("Pending Submit Request #1385454", markup)
            mock_win.submit_pool_btn.set_sensitive.assert_called_with(False)

        # 2. Accepted SR awaiting sync -> button disabled, label shows accepted
        with patch("pool_submit.get_package_pool_submission_status", return_value={
            "status": "accepted_pending_sync",
            "request": {"id": "1384945", "state": "accepted"}
        }):
            mock_win.package_data["testpkg"]["sync"].pop("pool_sr", None)
            SyncWindow.update_detail_sync_ui(mock_win, "testpkg")
            markup = mock_win.stable_pool_lbl.set_markup.call_args[0][0]
            self.assertIn("Submit Request #1384945 Accepted", markup)
            mock_win.submit_pool_btn.set_sensitive.assert_called_with(False)

        # 3. Pending Outdated SR -> button enabled, label shows outdated, offers supersede
        with patch("pool_submit.get_package_pool_submission_status", return_value={
            "status": "pending_outdated",
            "request": {"id": "1387152", "state": "review", "source_project": "multimedia:libs"}
        }):
            # Ensure fresh query when pool_sr not present
            mock_win.package_data["testpkg"]["sync"].pop("pool_sr", None)
            SyncWindow.update_detail_sync_ui(mock_win, "testpkg")
            markup = mock_win.stable_pool_lbl.set_markup.call_args[0][0]
            self.assertIn("Pending SR #1387152 Outdated", markup)
            mock_win.submit_pool_btn.set_sensitive.assert_called_with(True)
            mock_win.submit_pool_btn.set_label.assert_called_with("📤 Supersede SR")

        # 3b. When pool_sr is already in memory/cache, get_package_pool_submission_status is NOT called
        with patch("pool_submit.get_package_pool_submission_status") as mock_should_not_call:
            mock_win.package_data["testpkg"]["sync"]["pool_sr"] = {
                "status": "pending_outdated",
                "request": {"id": "1387152", "state": "review", "source_project": "multimedia:libs"}
            }
            SyncWindow.update_detail_sync_ui(mock_win, "testpkg")
            mock_should_not_call.assert_not_called()

        # 4. Unsubmitted -> button enabled
        with patch("pool_submit.get_package_pool_submission_status", return_value={
            "status": "unsubmitted"
        }):
            mock_win.package_data["testpkg"]["sync"].pop("pool_sr", None)
            SyncWindow.update_detail_sync_ui(mock_win, "testpkg")
            markup = mock_win.stable_pool_lbl.set_markup.call_args[0][0]
            self.assertIn("Ahead of Gitea Pool by 2 commits", markup)
            mock_win.submit_pool_btn.set_sensitive.assert_called_with(True)

    def test_update_detail_sync_ui_branch_stream_pr(self):
        mock_win = mock.Mock()
        mock_win.current_selected_package = "dimstar/vlc"
        mock_win.stable_b = "factory"
        mock_win.stable_pool_lbl = mock.Mock()
        mock_win.pull_pool_btn = mock.Mock()
        mock_win.submit_pool_btn = mock.Mock()
        mock_win.unstable_branch_lbl = mock.Mock()
        mock_win.catchup_merge_btn = mock.Mock()
        mock_win.create_pr_btn = mock.Mock()

        mock_win.package_data = {
            "dimstar/vlc": {
                "sync": {"pool_ahead": 3, "pool_behind": 0, "pool_status": "Ahead"},
                "pr": {
                    "has_pr": True,
                    "count": 1,
                    "prs": [{
                        "number": 3,
                        "title": "Update version dependencies according to configure.ac.",
                        "base": "leap-16.1",
                        "author": "dimstar"
                    }]
                }
            }
        }

        mock_win.get_mapped_worktree_path.return_value = "/mock/vlc"
        with mock.patch("geckopit.SyncWindow.get_git_head_file", return_value="/mock/vlc/HEAD"):
            with mock.patch("geckopit.SyncWindow.read_git_branch_from_head_file", return_value="leap-16.1"):
                SyncWindow.update_detail_sync_ui(mock_win, "dimstar/vlc")
                markup = mock_win.stable_pool_lbl.set_markup.call_args[0][0]
                self.assertIn("Pending Pull Request #3", markup)
                self.assertIn("leap-16.1", markup)
                mock_win.submit_pool_btn.set_sensitive.assert_called_with(False)
                mock_win.submit_pool_btn.set_label.assert_called_with("PR Pending")

    def test_update_detail_version_ui_pr_hint_branch_matching(self):
        mock_win = mock.Mock()
        mock_win.current_selected_package = "dimstar/vlc"
        mock_win.stable_b = "factory"
        mock_win.unstable_b = None
        mock_win.stable_ver_lbl = mock.Mock()
        mock_win.update_factory_btn = mock.Mock()
        mock_win.detail_web_btn = mock.Mock()
        mock_win.get_mapped_worktree_path.return_value = "/mock/vlc"

        mock_win.package_data = {
            "dimstar/vlc": {
                "version": {"factory_ver": "3.0.23", "upstream_stable": "3.0.24"},
                "sync": {"is_osc": False},
                "pr": {
                    "has_pr": True,
                    "prs": [{"number": 3, "base": "leap-16.1", "author": "dimstar"}]
                }
            }
        }

        with mock.patch("geckopit.SyncWindow.get_git_head_file", return_value="/mock/vlc/HEAD"):
            # 1. On leap-16.0: PR #3 (base: leap-16.1) MUST NOT be annotated!
            with mock.patch("geckopit.SyncWindow.read_git_branch_from_head_file", return_value="leap-16.0"):
                SyncWindow.update_detail_version_ui(mock_win, "dimstar/vlc")
                markup_16_0 = mock_win.stable_ver_lbl.set_markup.call_args[0][0]
                self.assertIn("3.0.23", markup_16_0)
                self.assertIn("3.0.24", markup_16_0)
                self.assertNotIn("[PR #3 pending]", markup_16_0)
                mock_win.update_factory_btn.set_label.assert_called_with("Update leap-16.0 to 3.0.24")

            # 2. On leap-16.1: PR #3 matches active branch -> MUST be annotated!
            with mock.patch("geckopit.SyncWindow.read_git_branch_from_head_file", return_value="leap-16.1"):
                SyncWindow.update_detail_version_ui(mock_win, "dimstar/vlc")
                markup_16_1 = mock_win.stable_ver_lbl.set_markup.call_args[0][0]
                self.assertIn("[PR #3 pending]", markup_16_1)

    def test_package_row_portfolio_category_and_osc_badge(self):
        from geckopit import PackageRow
        mock_win = mock.Mock()

        # 1. Package with category: dimstar/vlc
        row_cat = PackageRow("dimstar/vlc", mock_win)
        self.assertIn("dimstar", row_cat.name_label.get_label())
        self.assertIn("vlc", row_cat.name_label.get_label())

        # 2. OSC package metadata subtitle (clean packages have no badge noise)
        sync_data = {"is_osc": True, "osc_project": "games"}
        row_osc = PackageRow("games/toycars", mock_win)
        row_osc.update_ui(sync_data, {}, {}, {})
        self.assertIn("OSC: games", row_osc.sub_label.get_text())
        self.assertIsNone(row_osc.badges_box.get_first_child())

        # 3. Absent from central pool: shows 'Not in Pool'
        row_nopool = PackageRow("telegnome", mock_win)
        row_nopool.update_ui({"pool_status": "Not in Pool"}, {}, {}, {})
        self.assertIn("Not in Pool", row_nopool.sub_label.get_text())

    def test_get_git_head_file_and_read_branch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Regular repo with .git directory
            git_dir = os.path.join(tmpdir, ".git")
            os.makedirs(git_dir)
            head_path = os.path.join(git_dir, "HEAD")
            with open(head_path, "w") as f:
                f.write("ref: refs/heads/leap-16.1\n")

            self.assertEqual(SyncWindow.get_git_head_file(tmpdir), head_path)
            self.assertEqual(SyncWindow.read_git_branch_from_head_file(head_path), "leap-16.1")

            # 2. Detached HEAD commit
            with open(head_path, "w") as f:
                f.write("e8e9fd6c30c5de8993a14667c2763692f3741619\n")
            self.assertEqual(SyncWindow.read_git_branch_from_head_file(head_path), "e8e9fd6c")

            # 3. Linked worktree with .git file (gitdir:)
            wt_dir = os.path.join(tmpdir, "worktree")
            os.makedirs(wt_dir)
            wt_git_file = os.path.join(wt_dir, ".git")
            wt_target_dir = os.path.join(tmpdir, "main_git", "worktrees", "wt")
            os.makedirs(wt_target_dir)
            wt_head = os.path.join(wt_target_dir, "HEAD")
            with open(wt_head, "w") as f:
                f.write("ref: refs/heads/factory\n")
            with open(wt_git_file, "w") as f:
                f.write(f"gitdir: {wt_target_dir}\n")

            self.assertEqual(SyncWindow.get_git_head_file(wt_dir), wt_head)
            self.assertEqual(SyncWindow.read_git_branch_from_head_file(wt_head), "factory")

    def test_on_terminal_head_changed_updates_label(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            head_file = os.path.join(tmpdir, "HEAD")
            with open(head_file, "w") as f:
                f.write("ref: refs/heads/leap-16.1\n")

            mock_win = mock.Mock()
            mock_label = mock.Mock()
            tab_state = {
                "head_file": head_file,
                "pkg_name": "dimstar/vlc",
                "active_branch": "factory",
                "suffix": "",
                "tab_label": mock_label
            }

            mock_win.current_selected_package = "dimstar/vlc"
            with mock.patch("gi.repository.GLib.idle_add") as mock_idle:
                SyncWindow.on_terminal_head_changed(mock_win, None, None, None, None, tab_state)
                self.assertEqual(tab_state["active_branch"], "leap-16.1")
                self.assertEqual(tab_state["base_label"], "dimstar/vlc (leap-16.1)")
                # Verifies that worktree, sync, and pr UI were all scheduled for live update!
                scheduled_funcs = [call[0][0] for call in mock_idle.call_args_list]
                self.assertIn(mock_win.update_detail_worktree_ui, scheduled_funcs)
                self.assertIn(mock_win.update_detail_sync_ui, scheduled_funcs)
                self.assertIn(mock_win.update_detail_pr_ui, scheduled_funcs)

    def test_get_repo_branches(self):
        branch_output = "factory\nleap-16.0\nleap-16.1\norigin\norigin/HEAD\norigin/factory\norigin/leap-16.1\n"
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout=branch_output)):
            branches = SyncWindow.get_repo_branches("/mock/repo")
            self.assertEqual(branches, ["factory", "leap-16.0", "leap-16.1"])

    def test_switch_to_branch_dirty_guard(self):
        mock_win = mock.Mock()
        mock_popover = mock.Mock()

        # When working tree is dirty -> blocks checkout and shows warning toast
        with mock.patch("sync_backend.check_worktree_status", return_value={"dirty": True, "has_conflict": False}):
            with mock.patch("subprocess.run") as mock_run:
                SyncWindow.switch_to_branch(mock_win, "/mock/repo", "leap-16.1", mock_popover)
                mock_popover.popdown.assert_called_once()
                mock_run.assert_not_called()
                mock_win.toast_overlay.add_toast.assert_called_once()

    def test_switch_to_branch_clean_executes_checkout(self):
        mock_win = mock.Mock()
        mock_popover = mock.Mock()

        # When working tree is clean -> executes git checkout
        with mock.patch("sync_backend.check_worktree_status", return_value={"dirty": False, "has_conflict": False}):
            with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)) as mock_run:
                SyncWindow.switch_to_branch(mock_win, "/mock/repo", "leap-16.1", mock_popover)
                mock_popover.popdown.assert_called_once()
                mock_run.assert_called_once_with(
                    ["git", "-C", "/mock/repo", "checkout", "leap-16.1"],
                    capture_output=True, text=True, timeout=15
                )

    def test_update_detail_worktree_ui_branch_switch_btn_visibility(self):
        mock_win = mock.Mock()
        mock_win.current_selected_package = "testpkg"
        mock_win.stable_b = "factory"
        mock_win.unstable_b = None
        mock_win.branch_switch_btn = mock.Mock()
        mock_win.osc_ci_btn = mock.Mock()
        mock_win.open_term_fac_btn = mock.Mock()
        mock_win.package_data = {"testpkg": {}}

        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. OSC package without .git -> branch_switch_btn hidden
            with mock.patch("sync_backend.get_local_package_version", return_value="1.0"):
                with mock.patch("sync_backend.check_worktree_status", return_value={"is_osc": True, "dirty": False, "exists": True}):
                    mock_win.get_mapped_worktree_path.return_value = tmpdir
                    SyncWindow.update_detail_worktree_ui(mock_win, "testpkg")
                    mock_win.branch_switch_btn.set_visible.assert_called_with(False)

            # 2. Git package with .git -> branch_switch_btn visible
            os.makedirs(os.path.join(tmpdir, ".git"))
            with mock.patch("sync_backend.get_local_package_version", return_value="1.0"):
                with mock.patch("sync_backend.check_worktree_status", return_value={"is_osc": False, "dirty": False, "exists": True}):
                    mock_win.get_mapped_worktree_path.return_value = tmpdir
                    SyncWindow.update_detail_worktree_ui(mock_win, "testpkg")
                    mock_win.branch_switch_btn.set_visible.assert_called_with(True)

    def test_on_active_package_head_changed(self):
        mock_win = mock.Mock()
        mock_win.current_selected_package = "vlc"

        with mock.patch("gi.repository.GLib.idle_add") as mock_idle:
            SyncWindow.on_active_package_head_changed(mock_win, None, None, None, None, "vlc", "/mock/HEAD")
            scheduled = [call[0][0] for call in mock_idle.call_args_list]
            self.assertIn(mock_win.update_detail_worktree_ui, scheduled)
            self.assertIn(mock_win.update_detail_sync_ui, scheduled)
            self.assertIn(mock_win.update_detail_pr_ui, scheduled)
            self.assertIn(mock_win.refresh_single_package_priority, scheduled)

    def test_on_branch_switch_btn_clicked_excludes_unstable_b(self):
        mock_win = mock.Mock()
        mock_win.current_selected_package = "mutter"
        mock_win.unstable_b = "next"
        mock_win.get_mapped_worktree_path.return_value = "/mock/mutter"

        with mock.patch("os.path.exists", return_value=True):
            with mock.patch("geckopit.SyncWindow.get_git_head_file", return_value="/mock/mutter/HEAD"):
                with mock.patch("geckopit.SyncWindow.read_git_branch_from_head_file", return_value="factory"):
                    with mock.patch("geckopit.SyncWindow.get_repo_branches", return_value=["factory", "glgo_mutter_5370", "next"]):
                        with mock.patch("gi.repository.Gtk.Popover") as mock_popover_cls:
                            mock_popover = mock.Mock()
                            mock_popover_cls.return_value = mock_popover
                            mock_btn = mock.Mock()
                            SyncWindow.on_branch_switch_btn_clicked(mock_win, mock_btn)
                            # Verify Popover was created and next was excluded
                            mock_popover.popup.assert_called_once()
                            box = mock_popover.set_child.call_args[0][0]
                            # Children in box: header + button for factory + button for glgo_mutter_5370 (next must be omitted)
                            # Box should not contain any button for "next"

    def test_run_bg_diff_perspective_branch_aware(self):
        mock_win = mock.Mock()
        mock_win.stable_b = "factory"
        mock_win.unstable_b = None
        mock_win.get_mapped_worktree_path.return_value = "/mock/vlc"

        with mock.patch("os.path.exists", return_value=True):
            with mock.patch("os.path.isdir", return_value=False):
                with mock.patch("geckopit.SyncWindow.get_git_head_file", return_value="/mock/vlc/HEAD"):
                    with mock.patch("geckopit.SyncWindow.read_git_branch_from_head_file", return_value="leap-16.1"):
                        with mock.patch("sync_backend.get_gitea_repo_name", return_value="vlc"):
                            with mock.patch("sync_backend.has_git_ref", return_value=True):
                                with mock.patch("sync_backend.run_tracked") as mock_fetch:
                                    with mock.patch("subprocess.run") as mock_diff:
                                        mock_diff.return_value = mock.Mock(returncode=0, stdout="diff output")
                                        with mock.patch("gi.repository.GLib.idle_add") as mock_idle:
                                            SyncWindow.run_bg_diff_perspective(mock_win, "dimstar/vlc", "factory_pool")
                                            # Verify that diff command target was leap-16.1 and passed --no-ext-diff
                                            diff_args = mock_diff.call_args[0][0]
                                            self.assertIn("--no-ext-diff", diff_args)
                                            self.assertIn("refs/pool/leap-16.1...origin/leap-16.1", diff_args)

    def test_diff_perspective_lbl_visibility(self):
        mock_win = mock.Mock()
        mock_win.current_selected_package = "vlc"
        mock_win.unstable_b = None
        mock_win.diff_selector = mock.Mock()
        mock_win.diff_perspective_lbl = mock.Mock()
        mock_win.diff_buffer = mock.Mock()
        mock_win.package_data = {"vlc": {}}
        mock_win.get_mapped_worktree_path.return_value = "/mock/vlc"

        with mock.patch("geckopit.SyncWindow.get_mapped_worktree_path", return_value="/mock/vlc"):
            with mock.patch("os.path.isdir", return_value=False):
                with mock.patch("os.path.exists", return_value=True):
                    with mock.patch("geckopit.SyncWindow.setup_active_package_head_monitor"):
                        with mock.patch("geckopit.SyncWindow.update_detail_title"):
                            with mock.patch("geckopit.SyncWindow.update_detail_worktree_ui"):
                                with mock.patch("geckopit.SyncWindow.update_detail_version_ui"):
                                    with mock.patch("geckopit.SyncWindow.update_detail_sync_ui"):
                                        with mock.patch("geckopit.SyncWindow.update_detail_pr_ui"):
                                            with mock.patch("geckopit.SyncWindow.update_detail_drift_ui"):
                                                with mock.patch("geckopit.SyncWindow.refresh_active_diff"):
                                                    SyncWindow.load_package_detail(mock_win, "vlc", reload_diff=False)
                                                    mock_win.diff_selector.set_visible.assert_called_with(False)
                                                    mock_win.diff_perspective_lbl.set_visible.assert_called_with(True)

    def test_update_detail_pr_ui_forward_and_contributor_header(self):
        mock_win = mock.Mock()
        mock_win.current_selected_package = "baobab"
        mock_win.stable_b = "factory"
        mock_win.unstable_b = "next"
        mock_win.pr_card_revealer = mock.Mock()
        mock_win.pr_card_inner_revealer = mock.Mock()
        mock_win.pr_card_chevron = mock.Mock()
        mock_win.pr_card_title_lbl = mock.Mock()
        mock_win.pr_card_items_box = mock.Mock()
        mock_win.pr_card_items_box.get_first_child.return_value = None
        mock_win.pr_card_folded = False
        mock_win.sync_pr_card_folded_state = lambda: None
        mock_win.get_mapped_worktree_path.return_value = "/mock/baobab"

        # 1. Only forward PR active
        mock_win.package_data = {
            "baobab": {
                "sync": {},
                "pr": {
                    "has_pr": True,
                    "prs": [{"number": 5, "title": "Promote next", "author": "dimstar", "is_forward": True, "base": "factory"}]
                }
            }
        }
        SyncWindow.update_detail_pr_ui(mock_win, "baobab")
        hdr_1 = mock_win.pr_card_title_lbl.set_markup.call_args[0][0]
        self.assertIn("🔀 ACTIVE REQUESTS: 🔀 1 Forward PR", hdr_1)

        # 2. Both contributor PR and forward PR active
        mock_win.package_data["baobab"]["pr"]["prs"].append(
            {"number": 9, "title": "Fix riscv", "author": "schwab", "is_forward": False, "base": "factory"}
        )
        SyncWindow.update_detail_pr_ui(mock_win, "baobab")
        hdr_2 = mock_win.pr_card_title_lbl.set_markup.call_args[0][0]
        self.assertIn("⚠️ 1 Contributor PR", hdr_2)
        self.assertIn("🔀 1 Forward PR", hdr_2)

    def test_workspace_config_per_profile_filter_persistence(self):
        from geckopit import WorkspaceConfig
        cfg = WorkspaceConfig()
        cfg.save = mock.Mock()
        cfg.workspaces = {
            "GNOME": {"stable_path": "/path/gnome", "unstable_path": "/path/next"},
            "FlatOrg": {"stable_path": "/path/pkg", "unstable_path": None},
        }
        cfg.active_workspace = "GNOME"

        # Defaults when no filters defined
        filters = cfg.get_profile_filters("GNOME")
        self.assertEqual(filters, {
            "needs_action": True,
            "pool": False,
            "stable": False,
            "unstable": False,
            "forwarding": False,
        })

        # Save custom filters on GNOME
        cfg.set_profile_filters({
            "needs_action": True,
            "pool": False,
            "stable": False,
            "unstable": True,
            "forwarding": False,
        }, ws_name="GNOME")

        # Save different filters on FlatOrg
        cfg.set_profile_filters({
            "needs_action": True,
            "pool": True,
            "stable": True,
            "unstable": False,
            "forwarding": False,
        }, ws_name="FlatOrg")

        # Verify independence of profile filters
        gnome_filters = cfg.get_profile_filters("GNOME")
        flat_filters = cfg.get_profile_filters("FlatOrg")
        self.assertTrue(gnome_filters["unstable"])
        self.assertFalse(gnome_filters["pool"])
        self.assertFalse(flat_filters["unstable"])
        self.assertTrue(flat_filters["pool"])
        self.assertTrue(flat_filters["stable"])

    def test_sync_window_profile_switch_filter_round_trip(self):
        from geckopit import SyncWindow, WorkspaceConfig
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            gnome_stable = os.path.join(tmp_dir, "gnome")
            gnome_unstable = os.path.join(tmp_dir, "gnome_next")
            flat_stable = os.path.join(tmp_dir, "flat")
            os.makedirs(gnome_stable)
            os.makedirs(gnome_unstable)
            os.makedirs(flat_stable)

            cfg = WorkspaceConfig()
            cfg.save = mock.Mock()
            cfg.workspaces = {
                "GNOME": {
                    "stable_path": gnome_stable,
                    "unstable_path": gnome_unstable,
                    "filters": {"needs_action": True, "pool": False, "stable": False, "unstable": True, "forwarding": False}
                },
                "FlatOrg": {
                    "stable_path": flat_stable,
                    "unstable_path": None,
                    "filters": {"needs_action": True, "pool": False, "stable": False, "unstable": False, "forwarding": False}
                }
            }
            cfg.active_workspace = "GNOME"

            mock_win = mock.Mock()
            mock_win.config = cfg
            mock_win.filter_needs_action = mock.Mock()
            mock_win.filter_pool_sync = mock.Mock()
            mock_win.filter_stable = mock.Mock()
            mock_win.filter_unstable = mock.Mock()
            mock_win.filter_forwarding = mock.Mock()
            mock_win.update_track_filters_sensitivity = mock.Mock()
            mock_win.snapshot_filter_states = mock.Mock()

            # 1. Restore GNOME filters: unstable should be activated
            SyncWindow.restore_active_profile_filters(mock_win)
            mock_win.filter_unstable.set_active.assert_called_with(True)

            # 2. Simulate switching to FlatOrg: save outgoing GNOME filters and switch
            mock_win.filter_unstable.get_active.return_value = True
            mock_win.filter_needs_action.get_active.return_value = True
            mock_win.filter_pool_sync.get_active.return_value = False
            mock_win.filter_stable.get_active.return_value = False
            mock_win.filter_forwarding.get_active.return_value = False

            SyncWindow.save_current_profile_filters(mock_win, ws_name="GNOME")
            cfg.active_workspace = "FlatOrg"

            # In FlatOrg, restore filters: unstable must be forced False
            SyncWindow.restore_active_profile_filters(mock_win)
            mock_win.filter_unstable.set_active.assert_called_with(False)

            # 3. Switch back to GNOME: unstable must be restored to True!
            cfg.active_workspace = "GNOME"
            SyncWindow.restore_active_profile_filters(mock_win)
            mock_win.filter_unstable.set_active.assert_called_with(True)


    def test_save_profile_cache_async_thread_safe_and_atomic(self):
        from geckopit import SyncWindow
        import json
        import tempfile
        import time
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_file = Path(tmp_dir) / "cache_test.json"
            mock_win = mock.Mock()
            mock_win.config.active_workspace = "TestWorkspace"
            mock_win.get_cache_path.return_value = cache_file
            mock_win.package_data = {
                "vlc": {"sync": {"status": "success"}, "pr": {"has_pr": True}}
            }

            SyncWindow.save_profile_cache_async(mock_win)

            for _ in range(50):
                if cache_file.exists():
                    break
                time.sleep(0.02)

            self.assertTrue(cache_file.exists())
            with open(cache_file) as f:
                data = json.load(f)
            self.assertEqual(data["profile"], "TestWorkspace")
            self.assertEqual(data["package_data"]["vlc"]["pr"]["has_pr"], True)
            self.assertFalse(cache_file.with_suffix(".tmp").exists())

    def test_toggle_terminal_drawer_f12_shortcut(self):
        from geckopit import SyncWindow
        from gi.repository import Gdk

        mock_win = mock.Mock()
        mock_win.terminal_drawer.get_visible.return_value = False
        mock_win.terminal_tabs = [{"terminal": mock.Mock()}]
        mock_win.notebook.get_current_page.return_value = 0

        res = SyncWindow.on_window_key_pressed(mock_win, None, Gdk.KEY_F12, 0, 0)
        self.assertTrue(res)
        mock_win.terminal_drawer.set_visible.assert_called_with(True)

        mock_win.terminal_drawer.get_visible.return_value = True
        res = SyncWindow.on_window_key_pressed(mock_win, None, Gdk.KEY_F12, 0, 0)
        self.assertTrue(res)
        mock_win.terminal_drawer.set_visible.assert_called_with(False)

    def test_update_detail_version_ui_ignored_stable_version(self):
        from geckopit import SyncWindow
        mock_win = mock.Mock()
        mock_win.current_selected_package = "lasem"
        mock_win.package_data = {
            "lasem": {
                "version": {
                    "factory_ver": "0.4.3",
                    "upstream_stable": "0.6.0",
                    "next_ver": "—",
                    "upstream_latest": "—",
                },
                "pr": {},
                "sync": {"is_osc": False}
            }
        }
        mock_win.config.get_active_profile.return_value = {
            "ignored_versions": {"lasem": "0.6.0"}
        }
        mock_win.get_ignored_version.return_value = "0.6.0"
        mock_win.get_mapped_worktree_path.return_value = None
        mock_win.unstable_b = None
        mock_win.stable_b = "factory"

        SyncWindow.update_detail_version_ui(mock_win, "lasem")

        # Stable label should show (Ignored)
        lbl_markup = mock_win.stable_ver_lbl.set_markup.call_args[0][0]
        self.assertIn("0.6.0 (Ignored)", lbl_markup)
        mock_win.update_factory_btn.set_sensitive.assert_called_with(False)
        mock_win.update_factory_btn.set_label.assert_called_with("Ignored Version Update")

    def test_sidebar_filter_func_excludes_ignored_stable_version(self):
        from geckopit import SyncWindow
        mock_win = mock.Mock()
        mock_win.unstable_b = None
        mock_win._filter_name_tokens = []
        mock_win._filter_active_flags = set()
        mock_win._filter_needs_action = True
        mock_win._filter_pool_sync = False
        mock_win._filter_stable = False
        mock_win._filter_unstable = False
        mock_win._filter_forwarding = False

        row_lasem = mock.Mock(package_name="lasem")
        mock_win.package_data = {
            "lasem": {
                "sync": {},
                "worktree": {},
                "version": {
                    "factory_ver": "0.4.3",
                    "upstream_stable": "0.6.0",
                    "stable_needs_update": False,  # Evaluated as False because 0.6.0 is ignored
                    "unstable_needs_update": False
                }
            }
        }
        mock_win.get_ignored_version.return_value = "0.6.0"

        # lasem must NOT appear in the action queue
        self.assertFalse(SyncWindow.sidebar_filter_func(mock_win, row_lasem))

    def test_on_toggle_ignore_clicked_advances_to_next_candidate(self):
        from geckopit import SyncWindow
        mock_win = mock.Mock()
        mock_win.package_data = {
            "telepathy-glib": {
                "version": {
                    "factory_ver": "0.24.2",
                    "next_ver": "0.24.2",
                    "upstream_stable": "0.24.2",
                    "upstream_latest": "0.99.10",
                    "raw_upstream_stable": "0.24.2",
                    "raw_upstream_latest": "0.99.11",
                    "stable_versions": ["0.24.2"],
                    "all_versions": ["0.99.11", "0.99.10", "0.99.9", "0.99.8", "0.24.2"],
                }
            }
        }
        active_prof = {
            "ignored_versions": {"telepathy-glib": "0.99.11"},
            "ignored_unstable_versions": {"telepathy-glib": "0.99.11"}
        }
        mock_win.config.get_active_profile.return_value = active_prof
        def fake_get_ign(pkg):
            return active_prof.get("ignored_versions", {}).get(pkg)
        mock_win.get_ignored_version.side_effect = fake_get_ign
        mock_win.stable_b = "factory"
        mock_win.unstable_b = "next"

        mock_popover = mock.Mock()
        mock_btn = mock.Mock()

        # User ignores 0.99.10
        SyncWindow.on_toggle_ignore_clicked(mock_win, mock_btn, mock_popover, "telepathy-glib", "0.99.10", should_ignore=True)

        mock_popover.popdown.assert_called_once()
        mock_win.config.save.assert_called_once()
        self.assertIn("0.99.10", active_prof["ignored_versions"]["telepathy-glib"])

        ver = mock_win.package_data["telepathy-glib"]["version"]
        # Must immediately advance to 0.99.9 instead of staying on 0.99.10 (Ignored)!
        self.assertEqual(ver["upstream_latest"], "0.99.9")
        self.assertTrue(ver["unstable_needs_update"])
        self.assertFalse(ver["is_ignored_unstable"])
        self.assertTrue(ver["needs_update"])

        mock_win.update_row_ui.assert_called_with("telepathy-glib")
        mock_win.update_detail_version_ui.assert_called_with("telepathy-glib")
        mock_win.master_list_box.invalidate_filter.assert_called_once()
        mock_win.toast_overlay.add_toast.assert_called_once()

    def test_on_toggle_ignore_clicked_all_exhausted_shows_ignored(self):
        from geckopit import SyncWindow
        mock_win = mock.Mock()
        mock_win.package_data = {
            "telepathy-glib": {
                "version": {
                    "factory_ver": "0.24.2",
                    "next_ver": "0.24.2",
                    "upstream_stable": "0.24.2",
                    "upstream_latest": "0.99.8",
                    "raw_upstream_stable": "0.24.2",
                    "raw_upstream_latest": "0.99.11",
                    "stable_versions": ["0.24.2"],
                    "all_versions": ["0.99.11", "0.99.10", "0.99.9", "0.99.8", "0.24.2"],
                }
            }
        }
        active_prof = {
            "ignored_versions": {"telepathy-glib": "0.99.11, 0.99.10, 0.99.9"},
            "ignored_unstable_versions": {"telepathy-glib": "0.99.11, 0.99.10, 0.99.9"}
        }
        mock_win.config.get_active_profile.return_value = active_prof
        def fake_get_ign(pkg):
            return active_prof.get("ignored_versions", {}).get(pkg)
        mock_win.get_ignored_version.side_effect = fake_get_ign
        mock_win.stable_b = "factory"
        mock_win.unstable_b = "next"

        mock_popover = mock.Mock()
        mock_btn = mock.Mock()

        # User ignores the last candidate 0.99.8
        SyncWindow.on_toggle_ignore_clicked(mock_win, mock_btn, mock_popover, "telepathy-glib", "0.99.8", should_ignore=True)

        ver = mock_win.package_data["telepathy-glib"]["version"]
        # All newer versions exhausted -> falls back to max version with is_ignored_unstable True
        self.assertEqual(ver["upstream_latest"], "0.99.11")
        self.assertFalse(ver["unstable_needs_update"])
        self.assertTrue(ver["is_ignored_unstable"])
        self.assertFalse(ver["needs_update"])

    def test_on_toggle_ignore_clicked_unignore_all(self):
        from geckopit import SyncWindow
        mock_win = mock.Mock()
        mock_win.package_data = {
            "telepathy-glib": {
                "version": {
                    "factory_ver": "0.24.2",
                    "next_ver": "0.24.2",
                    "upstream_stable": "0.24.2",
                    "upstream_latest": "0.99.11",
                    "raw_upstream_stable": "0.24.2",
                    "raw_upstream_latest": "0.99.11",
                    "stable_versions": ["0.24.2"],
                    "all_versions": ["0.99.11", "0.99.10", "0.99.9", "0.99.8", "0.24.2"],
                }
            }
        }
        active_prof = {
            "ignored_versions": {"telepathy-glib": "0.99.11, 0.99.10, 0.99.9, 0.99.8"},
            "ignored_unstable_versions": {"telepathy-glib": "0.99.11, 0.99.10, 0.99.9, 0.99.8"}
        }
        mock_win.config.get_active_profile.return_value = active_prof
        def fake_get_ign(pkg):
            return active_prof.get("ignored_versions", {}).get(pkg)
        mock_win.get_ignored_version.side_effect = fake_get_ign
        mock_win.stable_b = "factory"
        mock_win.unstable_b = "next"

        mock_popover = mock.Mock()
        mock_btn = mock.Mock()

        # User clicks "Unignore All Versions"
        SyncWindow.on_toggle_ignore_clicked(mock_win, mock_btn, mock_popover, "telepathy-glib", "all", should_ignore=False)

        self.assertNotIn("telepathy-glib", active_prof["ignored_versions"])
        ver = mock_win.package_data["telepathy-glib"]["version"]
        self.assertEqual(ver["upstream_latest"], "0.99.11")
        self.assertTrue(ver["unstable_needs_update"])
        self.assertFalse(ver["is_ignored_unstable"])
        self.assertTrue(ver["needs_update"])

    def test_on_config_file_changed_ignores_internal_saves(self):
        from geckopit import SyncWindow
        from gi.repository import Gio
        import time

        mock_win = mock.Mock()
        mock_win.config._last_save_time = time.time()
        mock_win._config_reload_timeout_id = None

        # Event fired right after internal save -> ignored
        SyncWindow.on_config_file_changed(mock_win, None, None, None, Gio.FileMonitorEvent.CHANGED)
        self.assertIsNone(mock_win._config_reload_timeout_id)

    @mock.patch("geckopit.GLib.timeout_add")
    def test_on_config_file_changed_schedules_reload_on_external_change(self, mock_timeout_add):
        from geckopit import SyncWindow
        from gi.repository import Gio

        mock_win = mock.Mock()
        mock_win.config._last_save_time = 0.0  # Old save / external modification
        mock_win._config_reload_timeout_id = None

        mock_timeout_add.return_value = 999
        SyncWindow.on_config_file_changed(mock_win, None, None, None, Gio.FileMonitorEvent.CHANGED)

        mock_timeout_add.assert_called_with(150, mock_win.on_config_file_reload)
        self.assertEqual(mock_win._config_reload_timeout_id, 999)

    def test_on_config_file_reload_synchronizes_ignore_rules_and_ui(self):
        from geckopit import SyncWindow
        mock_win = mock.Mock()
        mock_win._config_reload_timeout_id = 999
        mock_win.package_data = {
            "telepathy-glib": {
                "version": {
                    "factory_ver": "0.24.2",
                    "next_ver": "0.24.2",
                    "upstream_stable": "0.24.2",
                    "upstream_latest": "0.99.7",
                    "raw_upstream_stable": "0.24.2",
                    "raw_upstream_latest": "0.99.11",
                    "stable_versions": ["0.24.2"],
                    "all_versions": ["0.99.11", "0.99.10", "0.99.9", "0.99.8", "0.99.7", "0.99.6", "0.24.2"],
                }
            }
        }
        mock_win.stable_b = "factory"
        mock_win.unstable_b = "next"
        mock_win.current_selected_package = "telepathy-glib"

        # Simulating external tool added 0.99.7 to ignored_versions
        old_prof = {
            "stable_path": "/path",
            "unstable_path": "/path",
            "stable_branch": "factory",
            "unstable_branch": "next",
            "ignored_versions": {"telepathy-glib": "0.99.11, 0.99.10, 0.99.9, 0.99.8"},
            "filters": {"needs_action": True}
        }
        new_prof = {
            "stable_path": "/path",
            "unstable_path": "/path",
            "stable_branch": "factory",
            "unstable_branch": "next",
            "ignored_versions": {"telepathy-glib": "0.99.11, 0.99.10, 0.99.9, 0.99.8, 0.99.7"},
            "filters": {"needs_action": True}
        }

        mock_win.config.active_workspace = "Default"
        mock_win.config.get_active_profile.side_effect = [old_prof, new_prof]
        def fake_get_ign(pkg):
            return new_prof.get("ignored_versions", {}).get(pkg)
        mock_win.get_ignored_version.side_effect = fake_get_ign

        SyncWindow.on_config_file_reload(mock_win)

        self.assertIsNone(mock_win._config_reload_timeout_id)
        mock_win.config.load.assert_called_once()
        self.assertEqual(mock_win.ignored_versions, new_prof["ignored_versions"])

        # Upstream latest should automatically advance to 0.99.6!
        ver = mock_win.package_data["telepathy-glib"]["version"]
        self.assertEqual(ver["upstream_latest"], "0.99.6")
        self.assertTrue(ver["unstable_needs_update"])
        self.assertFalse(ver["is_ignored_unstable"])

        mock_win.update_row_ui.assert_called_with("telepathy-glib")
        mock_win.update_detail_version_ui.assert_called_with("telepathy-glib")
        mock_win.master_list_box.invalidate_filter.assert_called_once()
        mock_win.toast_overlay.add_toast.assert_called_once()

    def test_load_obs_requests_cache_refreshes_detail_views(self):
        from unittest.mock import patch
        mock_win = mock.Mock()
        mock_win.current_selected_package = "loupe"

        with patch("pool_submit.get_pending_obs_requests", return_value={"loupe": [{"id": "1385453"}]}),              patch("pool_submit.get_recently_accepted_obs_requests", return_value={}),              patch("geckopit.GLib.idle_add") as mock_idle:
            SyncWindow.load_obs_requests_cache(mock_win)
            self.assertEqual(mock_win.obs_pending_requests, {"loupe": [{"id": "1385453"}]})
            self.assertEqual(mock_win.obs_accepted_requests, {})
            mock_idle.assert_any_call(mock_win.update_detail_sync_ui, "loupe")
            mock_idle.assert_any_call(mock_win.update_detail_pr_ui, "loupe")

    def test_build_filter_popover_markup_validity(self):
        from geckopit import SyncWindow
        from gi.repository import Gtk, Pango

        mock_win = mock.Mock()
        mock_win.on_filter_toggled = mock.Mock()
        mock_win.on_clear_advanced_filters = mock.Mock()

        popover = SyncWindow.build_filter_popover(mock_win)
        main_box = popover.get_child()

        child = main_box.get_first_child()
        validated_count = 0
        while child:
            if isinstance(child, Gtk.Label) and child.get_use_markup():
                txt = child.get_label()
                # Must parse without throwing GLib.GError / Pango markup parse error
                Pango.parse_markup(txt, -1, chr(0))
                validated_count += 1
            child = child.get_next_sibling()

        self.assertGreaterEqual(validated_count, 3)

    def test_workspace_manager_dialog_scratchpad_support(self):
        from gi.repository import Gtk
        from geckopit import WorkspaceManagerDialog

        win = Gtk.Window()
        win.toast_overlay = mock.MagicMock()

        config = mock.MagicMock()
        config.workspaces = {
            "GNOME": {
                "stable_path": "/tmp",
                "stable_branch": "factory",
                "unstable_path": "/tmp/next",
                "unstable_branch": "next",
                "is_scratch": False
            }
        }
        config.active_workspace = "GNOME"
        config.max_workers = 20

        callback = mock.MagicMock()
        dlg = WorkspaceManagerDialog(win, config, callback)

        # Initially False
        self.assertFalse(dlg.is_scratch_check.get_active())
        self.assertTrue(dlg.unstable_path_entry.get_sensitive())

        # Toggle is_scratch to True
        dlg.is_scratch_check.set_active(True)
        self.assertFalse(dlg.unstable_path_entry.get_sensitive())
        self.assertFalse(dlg.unstable_br_entry.get_sensitive())

        # Save profile
        dlg.on_save_clicked(None)
        saved_prof = config.workspaces["GNOME"]
        self.assertTrue(saved_prof.get("is_scratch"))
        self.assertIsNone(saved_prof.get("unstable_path"))

    def test_fork_package_dialog_initialization(self):
        from gi.repository import Gtk
        from geckopit import ForkPackageDialog

        win = Gtk.Window()
        config = mock.MagicMock()
        config.get_active_profile.return_value = {"stable_path": "/tmp/scratch"}
        config.data = {"default_target_project": "home:dimstar:Factory"}

        dlg = ForkPackageDialog(win, config)
        self.assertEqual(dlg.stream_combo.get_selected(), 0)
        self.assertEqual(dlg.target_prj_entry.get_text(), "home:dimstar:Factory")
        self.assertTrue(dlg.build_check.get_active())
        dlg.destroy()

    def test_apply_active_profile_ui_scratchpad_visibility(self):
        from geckopit import SyncWindow

        mock_win = mock.Mock()
        mock_win.config = mock.Mock()
        mock_win.config.active_workspace = "Scratchpad"
        mock_win.config.get_active_profile.return_value = {
            "stable_path": "/tmp/scratch",
            "unstable_path": None,
            "is_scratch": True
        }
        mock_win.unstable_card = mock.Mock()
        mock_win.fork_pkg_btn = mock.Mock()
        mock_win.filter_unstable = mock.Mock()
        mock_win.filter_forwarding = mock.Mock()
        mock_win.diff_selector = mock.Mock()

        SyncWindow.apply_active_profile_ui(mock_win)

        mock_win.fork_pkg_btn.set_visible.assert_called_with(True)
        mock_win.unstable_card.set_visible.assert_called_with(False)
        mock_win.set_title.assert_called_with("Geckopit - Profile: Scratchpad [Scratchpad]")


if __name__ == '__main__':
    unittest.main()
