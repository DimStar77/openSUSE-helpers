#!/usr/bin/env python3
import os
import sys
import unittest
import unittest.mock as mock

import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
GECKOPIT_DIR = os.path.dirname(TESTS_DIR)
HELPERS_DIR = os.path.join(GECKOPIT_DIR, "helpers")

for p in [GECKOPIT_DIR, HELPERS_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

import sync_backend as sb
from geckopit import SyncCreatePRDialog

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
