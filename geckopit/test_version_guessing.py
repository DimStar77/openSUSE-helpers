#!/usr/bin/env python3
import unittest
import unittest.mock as mock
from geckopit import guess_update_revision, compare_versions, is_version_newer, is_version_equal

class TestVersionGuessing(unittest.TestCase):
    def test_simdutf_prefix_v(self):
        # 1. Prefix 'v' versioning (simdutf style)
        guessed, err = guess_update_revision("v9.1.2", "9.2.0")
        self.assertEqual(guessed, "v9.2.0")
        self.assertIsNone(err)

    def test_appstream_glib_underscores(self):
        # 2. Underscores versioning (appstream-glib style)
        guessed, err = guess_update_revision("appstream_glib_0_8_4", "0.8.5")
        self.assertEqual(guessed, "appstream_glib_0_8_5")
        self.assertIsNone(err)

    def test_gupnp_tools_prefix_dots(self):
        # 3. Dots versioning with complex prefix (gupnp-tools style)
        guessed, err = guess_update_revision("gupnp-tools-0.12.4", "0.12.5")
        self.assertEqual(guessed, "gupnp-tools-0.12.5")
        self.assertIsNone(err)

    def test_spiel_prefix_underscores(self):
        # 4. Underscores with uppercase prefix (spiel style)
        guessed, err = guess_update_revision("SPIEL_1_0_4", "1.0.5")
        self.assertEqual(guessed, "SPIEL_1_0_5")
        self.assertIsNone(err)

    def test_gnome_shell_normal(self):
        # 5. Standard version with dots only (gnome-shell style)
        guessed, err = guess_update_revision("51.0", "51.1")
        self.assertEqual(guessed, "51.1")
        self.assertIsNone(err)

    def test_vocalis_hash(self):
        # 6. Specific git commit SHA (vocalis style)
        guessed, err = guess_update_revision("aa0c00a9cb037e78a6ed2ec30dc2256aa4bcdabb", "1.0.0")
        self.assertIsNone(guessed)
        self.assertIn("Git commit SHA", err)

    def test_static_branch(self):
        # 7. Static branch (master style)
        guessed, err = guess_update_revision("master", "1.0.0")
        self.assertIsNone(guessed)
        self.assertIn("static development branch", err)

    def test_target_underscores_local_dots(self):
        # 8. Target has underscores but current revision uses dots (libiptcdata style)
        guessed, err = guess_update_revision("1.0.4", "1_0_5")
        self.assertEqual(guessed, "1.0.5")
        self.assertIsNone(err)

    def test_prefix_and_underscores(self):
        # 9. Current revision has prefix and underscores (release_1_0_4 style)
        guessed, err = guess_update_revision("release_1_0_4", "1.0.5")
        self.assertEqual(guessed, "release_1_0_5")
        self.assertIsNone(err)


class TestSemanticVersionComparison(unittest.TestCase):
    def test_underscore_and_dot_equality(self):
        # 1.0.5 and 1_0_5 are semantically identical under RPM rules
        self.assertTrue(is_version_equal("1.0.5", "1_0_5"))
        self.assertTrue(is_version_equal("1_0_5", "1.0.5"))
        self.assertFalse(is_version_newer("1_0_5", "1.0.5"))
        self.assertFalse(is_version_newer("1.0.5", "1_0_5"))

    def test_genuine_updates(self):
        # True newer versions must be flagged regardless of separator style
        self.assertTrue(is_version_newer("1.0.6", "1.0.5"))
        self.assertTrue(is_version_newer("1_0_6", "1.0.5"))
        self.assertTrue(is_version_newer("1.0.6", "1_0_5"))
        self.assertTrue(is_version_newer("v1.0.6", "1.0.5"))
        self.assertTrue(is_version_newer("V1.0.6", "1.0.5"))

    def test_git_snapshot_comparisons(self):
        # Git snapshots (e.g. 1.0.0+git) are post-release increments and not older than 1.0.0
        self.assertFalse(is_version_newer("1.0.0", "1.0.0+git"))
        self.assertFalse(is_version_newer("1.0.0", "1.0.0+git42"))
        # An actual next release is newer than the snapshot
        self.assertTrue(is_version_newer("1.0.1", "1.0.0+git42"))

    def test_lagging_upstream_and_pre_releases(self):
        # dasher: upstream stable 4_11_0 is older than local 5.0.0+199
        self.assertFalse(is_version_newer("4_11_0", "5.0.0+199"))
        # dasher: upstream beta 5_0_0_beta is older than post-release snapshot 5.0.0+199
        self.assertFalse(is_version_newer("5_0_0_beta", "5.0.0+199"))

    def test_downstream_branding_git_snapshot(self):
        # gnome-tour: local has downstream branding and git snapshot (50.0.openSUSE+git20260413.334ffbd)
        tour_local = "50.0.openSUSE+git20260413.334ffbd"
        # Upstream 50.0 is not newer than our post-50.0 branded snapshot
        self.assertFalse(is_version_newer("50.0", tour_local))
        # An actual next release (50.1) is newer and properly triggers an update
        self.assertTrue(is_version_newer("50.1", tour_local))

    def test_placeholders_and_empty_values(self):
        self.assertFalse(is_version_newer("1.0.5", "—"))
        self.assertFalse(is_version_newer("—", "1.0.5"))
        self.assertFalse(is_version_newer("1.0.5", "N/A"))
        self.assertFalse(is_version_newer("N/A", "1.0.5"))
        self.assertFalse(is_version_newer("", ""))
        self.assertFalse(is_version_newer(None, None))


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


    def test_check_repo_sync_pool_status_classification(self):
        import subprocess
        import sync_backend as sb

        with mock.patch('os.path.isdir', return_value=True), \
             mock.patch('sync_backend.run_tracked') as mock_run:

            # 1. Gitea 401 on non-existent repo (prompts disabled) -> Not in Pool, needs_action=False
            def fake_run_not_in_pool(cmd, *args, **kwargs):
                if 'fetch' in cmd and 'origin' in cmd:
                    return mock.Mock(stdout="", returncode=0)
                elif 'rev-parse' in cmd:
                    return mock.Mock(stdout="hash", returncode=0)
                elif 'fetch' in cmd and 'src.opensuse.org/pool' in cmd[5]:
                    raise subprocess.CalledProcessError(128, cmd, stderr="fatal: could not read Username for 'https://src.opensuse.org': terminal prompts disabled\n")
                elif 'rev-list' in cmd:
                    return mock.Mock(stdout="0 0\n", returncode=0)
                return mock.Mock(stdout="", returncode=0)

            mock_run.side_effect = fake_run_not_in_pool
            _, data = sb.check_repo_sync('mypkg')
            self.assertEqual(data['pool_status'], 'Not in Pool')
            self.assertFalse(data['needs_action'])

            # 2. Network resolution failure -> Fetch failed, needs_action=True
            def fake_run_net_error(cmd, *args, **kwargs):
                if 'fetch' in cmd and 'origin' in cmd:
                    return mock.Mock(stdout="", returncode=0)
                elif 'rev-parse' in cmd:
                    return mock.Mock(stdout="hash", returncode=0)
                elif 'fetch' in cmd and 'src.opensuse.org/pool' in cmd[5]:
                    raise subprocess.CalledProcessError(128, cmd, stderr="fatal: unable to access 'https://src.opensuse.org/pool/mypkg.git': Could not resolve host: src.opensuse.org\n")
                elif 'rev-list' in cmd:
                    return mock.Mock(stdout="0 0\n", returncode=0)
                return mock.Mock(stdout="", returncode=0)

            mock_run.side_effect = fake_run_net_error
            _, data = sb.check_repo_sync('mypkg')
            self.assertEqual(data['pool_status'], 'Fetch failed')
            self.assertTrue(data['needs_action'])

            # 3. Missing factory branch in pool -> No factory in Pool
            def fake_run_missing_ref(cmd, *args, **kwargs):
                if 'fetch' in cmd and 'origin' in cmd:
                    return mock.Mock(stdout="", returncode=0)
                elif 'rev-parse' in cmd:
                    return mock.Mock(stdout="hash", returncode=0)
                elif 'fetch' in cmd and 'src.opensuse.org/pool' in cmd[5]:
                    raise subprocess.CalledProcessError(128, cmd, stderr="fatal: couldn't find remote ref factory\n")
                elif 'rev-list' in cmd:
                    return mock.Mock(stdout="0 0\n", returncode=0)
                return mock.Mock(stdout="", returncode=0)

            mock_run.side_effect = fake_run_missing_ref
            _, data = sb.check_repo_sync('mypkg')
            self.assertEqual(data['pool_status'], 'No factory in Pool')

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


class TestPRPrefill(unittest.TestCase):
    def test_parse_changes_diff_single_entry(self):
        import sync_backend as sb
        diff_text = """diff --git a/test.changes b/test.changes
index 123..456 100644
--- a/test.changes
+++ b/test.changes
@@ -1,3 +1,10 @@
+-------------------------------------------------------------------
+Mon Sep 22 10:00:00 UTC 2026 - Maintainer <maintainer@example.com>
+
+- Update to version 2.0.0:
+  + Added support for new feature
+  + Fixed security issue
+
 -------------------------------------------------------------------
 Mon Jan 01 00:00:00 UTC 2026 - Old <old@example.com>
"""
        parsed = sb.parse_changes_diff(diff_text)
        expected = """Mon Sep 22 10:00:00 UTC 2026 - Maintainer <maintainer@example.com>

- Update to version 2.0.0:
  + Added support for new feature
  + Fixed security issue"""
        self.assertEqual(parsed, expected)

    def test_parse_changes_diff_multi_entry(self):
        import sync_backend as sb
        diff_text = """diff --git a/test.changes b/test.changes
--- a/test.changes
+++ b/test.changes
@@ -1,3 +1,15 @@
+-------------------------------------------------------------------
+Mon Sep 22 10:00:00 UTC 2026 - Maintainer <maintainer@example.com>
+
+- Update to version 2.0.0:
+  + Stable release
+
+-------------------------------------------------------------------
+Mon Sep 15 10:00:00 UTC 2026 - Maintainer <maintainer@example.com>
+
+- Update to version 2.0.0.rc1:
+  + Release candidate
+
 -------------------------------------------------------------------
"""
        parsed = sb.parse_changes_diff(diff_text)
        self.assertIn("Update to version 2.0.0:", parsed)
        self.assertIn("Update to version 2.0.0.rc1:", parsed)
        self.assertIn("-------------------------------------------------------------------", parsed)
        # Verify boundary dashes were stripped
        self.assertFalse(parsed.startswith("-------------------------------------------------------------------"))
        self.assertFalse(parsed.endswith("-------------------------------------------------------------------"))

    def test_parse_changes_diff_empty(self):
        import sync_backend as sb
        self.assertEqual(sb.parse_changes_diff(""), "")
        self.assertEqual(sb.parse_changes_diff(None), "")

    @mock.patch('sync_backend.run_tracked')
    @mock.patch('os.listdir', return_value=['mypkg.spec'])
    def test_get_pr_prefill_version_bump(self, mock_listdir, mock_run):
        import sync_backend as sb

        def fake_run(args, **kwargs):
            cmd_str = " ".join(args)
            mock_res = mock.Mock()
            if "show" in args and "factory:mypkg.spec" in cmd_str:
                mock_res.stdout = "Name: mypkg\nVersion: 1.0.0\n"
            elif "show" in args and "next:mypkg.spec" in cmd_str:
                mock_res.stdout = "Name: mypkg\nVersion: 1.1.0\n"
            elif "diff" in args and "*.changes" in cmd_str:
                mock_res.stdout = """@@ -1,3 +1,6 @@
+-------------------------------------------------------------------
+- Update to version 1.1.0:
+  + Improved performance
+"""
            else:
                mock_res.stdout = ""
            return mock_res

        mock_run.side_effect = fake_run
        title, desc = sb.get_pr_prefill_info("mypkg", stable_branch="factory", unstable_branch="next")

        self.assertEqual(title, "Update mypkg to version 1.1.0")
        self.assertIn("Update to version 1.1.0:", desc)
        self.assertIn("Improved performance", desc)

    @mock.patch('sync_backend.run_tracked')
    @mock.patch('os.listdir', return_value=['mypkg.spec'])
    def test_get_pr_prefill_single_commit(self, mock_listdir, mock_run):
        import sync_backend as sb

        def fake_run(args, **kwargs):
            cmd_str = " ".join(args)
            mock_res = mock.Mock()
            if "show" in args and "factory:mypkg.spec" in cmd_str:
                mock_res.stdout = "Name: mypkg\nVersion: 1.0.0\n"
            elif "show" in args and "next:mypkg.spec" in cmd_str:
                mock_res.stdout = "Name: mypkg\nVersion: 1.0.0\n"
            elif "log" in args:
                mock_res.stdout = "Fix build with gcc 14\n"
            elif "diff" in args and "*.changes" in cmd_str:
                mock_res.stdout = """@@ -1,3 +1,5 @@
+- Fix build with gcc 14 (bsc#12345).
+"""
            else:
                mock_res.stdout = ""
            return mock_res

        mock_run.side_effect = fake_run
        title, desc = sb.get_pr_prefill_info("mypkg", stable_branch="factory", unstable_branch="next")

        self.assertEqual(title, "Fix build with gcc 14")
        self.assertIn("- Fix build with gcc 14 (bsc#12345).", desc)

    @mock.patch('sync_backend.run_tracked')
    @mock.patch('os.listdir', return_value=['mypkg.spec'])
    def test_get_pr_prefill_multiple_commits(self, mock_listdir, mock_run):
        import sync_backend as sb

        def fake_run(args, **kwargs):
            cmd_str = " ".join(args)
            mock_res = mock.Mock()
            if "show" in args and "factory:mypkg.spec" in cmd_str:
                mock_res.stdout = "Name: mypkg\nVersion: 1.0.0\n"
            elif "show" in args and "next:mypkg.spec" in cmd_str:
                mock_res.stdout = "Name: mypkg\nVersion: 1.0.0\n"
            elif "log" in args:
                mock_res.stdout = "Fix bug A\nFix bug B\nFix bug C\n"
            else:
                mock_res.stdout = ""
            return mock_res

        mock_run.side_effect = fake_run
        title, desc = sb.get_pr_prefill_info("mypkg", stable_branch="factory", unstable_branch="next")

        self.assertEqual(title, "Fix bug A (+2 more commits)")

    def test_sync_create_pr_dialog_update_diff_and_prefill(self):
        from geckopit import SyncCreatePRDialog

        mock_dialog = mock.Mock()
        mock_dialog.is_destroyed = False
        mock_dialog.default_title = "Default Title"
        mock_dialog.default_desc = "Default Description"

        # Mock widgets
        mock_dialog.diff_buffer = mock.Mock()
        mock_dialog.title_entry = mock.Mock()
        mock_dialog.title_entry.get_text.return_value = "Default Title"

        mock_dialog.desc_buffer = mock.Mock()
        mock_dialog.desc_buffer.get_start_iter.return_value = 0
        mock_dialog.desc_buffer.get_end_iter.return_value = 1
        mock_dialog.desc_buffer.get_text.return_value = "Default Description"

        SyncCreatePRDialog.update_diff_and_prefill(
            mock_dialog,
            "diff content",
            "Update mypkg to version 2.0.0",
            "New changelog content"
        )

        mock_dialog.diff_buffer.set_text.assert_called_with("diff content")
        mock_dialog.title_entry.set_text.assert_called_with("Update mypkg to version 2.0.0")
        mock_dialog.desc_buffer.set_text.assert_called_with("New changelog content")

        # Verify that if user already typed custom input, it is NOT overwritten
        mock_dialog.title_entry.reset_mock()
        mock_dialog.desc_buffer.reset_mock()
        mock_dialog.title_entry.get_text.return_value = "User customized title"
        mock_dialog.desc_buffer.get_text.return_value = "User customized description"

        SyncCreatePRDialog.update_diff_and_prefill(
            mock_dialog,
            "new diff",
            "Another title",
            "Another desc"
        )
        mock_dialog.title_entry.set_text.assert_not_called()
        mock_dialog.desc_buffer.set_text.assert_not_called()


if __name__ == '__main__':
    unittest.main()
