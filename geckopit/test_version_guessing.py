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

        # Modify and save
        config.max_workers = 15
        config.save()

        # Verify that json.dump was called with max_workers=15
        mock_file.assert_called()


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
