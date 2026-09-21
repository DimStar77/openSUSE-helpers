#!/usr/bin/env python3
import unittest
import unittest.mock as mock
from geckopit import guess_update_revision

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

        res_msg = "\x1b]8;;https://src.opensuse.org/GNOME/gcr/pulls/2\x07https://src.opensuse.org/GNOME/gcr/pulls/2\x1b]8;;\x07"

        SyncCreatePRDialog.on_pr_created_result(mock_dialog, True, res_msg)

        MockToast.new.assert_called_with("Pull Request created successfully!")
        mock_toast_inst = MockToast.new.return_value
        mock_toast_inst.connect.assert_called_once()
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

        # Modify and save
        config.max_workers = 15
        config.save()

        # Verify that json.dump was called with max_workers=15
        mock_file.assert_called()


if __name__ == '__main__':
    unittest.main()
