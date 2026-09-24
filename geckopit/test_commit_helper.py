#!/usr/bin/env python3
import os
import sys
import tempfile
import unittest
import unittest.mock as mock
import subprocess

# Ensure helpers are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'helpers'))
import commit_helper as ch

class TestCommitHelper(unittest.TestCase):

    def test_sanitize_trailing_whitespaces(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            spec_path = os.path.join(tmpdir, "pkg.spec")
            with open(spec_path, "w") as f:
                f.write("Name: test   \nVersion: 1.0 \t\nClean line\n")

            sanitized = ch.sanitize_trailing_whitespaces(tmpdir)
            self.assertEqual(sanitized, ["pkg.spec"])

            with open(spec_path, "r") as f:
                content = f.read()

            self.assertEqual(content, "Name: test\nVersion: 1.0\nClean line\n")

    def test_extract_latest_changes_entry(self):
        sample_changes = """-------------------------------------------------------------------
Tue Sep 22 13:05:11 UTC 2026 - Bjørn Lie <bjorn.lie@gmail.com>

- Update to version 1.18.3:
  + Bug fixes:
    - Fix regressions in 1.18.2

-------------------------------------------------------------------
Thu Aug 27 20:12:00 UTC 2026 - Bjørn Lie <bjorn.lie@gmail.com>

- Update to version 1.18.2:
  + Old release
"""
        entry = ch.extract_latest_changes_entry(sample_changes)
        expected = """- Update to version 1.18.3:
  + Bug fixes:
    - Fix regressions in 1.18.2"""
        self.assertEqual(entry, expected)

    def test_format_commit_message_version_update(self):
        entry = """- Update to version 1.18.3:
  + Bug fixes:
    - Fix regressions in 1.18.2
  + Updated translations."""
        subject, body = ch.format_commit_message_from_entry(entry)
        self.assertEqual(subject, "Update to version 1.18.3")
        expected_body = """  + Bug fixes:
    - Fix regressions in 1.18.2
  + Updated translations."""
        self.assertEqual(body, expected_body)

    def test_format_commit_message_patch_drop(self):
        entry = "- Drop e5c2018d.patch: fixed upstream."
        subject, body = ch.format_commit_message_from_entry(entry)
        self.assertEqual(subject, "Drop e5c2018d.patch: fixed upstream.")
        self.assertEqual(body, "")

    @mock.patch("subprocess.run")
    def test_stage_package_files(self, mock_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a new tarball
            with open(os.path.join(tmpdir, "pkg-2.0.tar.xz"), "w") as f: f.write("tar")

            mock_proc = mock.MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = "pkg-2.0.tar.xz\n"
            mock_run.return_value = mock_proc

            logs = []
            ok, msg = ch.stage_package_files(tmpdir, on_log=logs.append)
            self.assertTrue(ok)
            calls = [c[0][0] for c in mock_run.call_args_list]
            self.assertIn(["git", "-C", tmpdir, "add", "-u", "."], calls)
            self.assertIn(["git", "-C", tmpdir, "add", "pkg-2.0.tar.xz"], calls)

    @mock.patch("subprocess.run")
    def test_execute_package_commit_non_interactive(self, mock_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            changes_path = os.path.join(tmpdir, "pkg.changes")
            with open(changes_path, "w") as f:
                f.write("""-------------------------------------------------------------------
Wed Sep 23 16:00:00 UTC 2026 - User <user@example.com>

- Update to version 2.0:
  + New features

-------------------------------------------------------------------
""")
            # Mock git diff --cached returning pkg.changes
            mock_diff = mock.MagicMock()
            mock_diff.returncode = 0
            mock_diff.stdout = "pkg.changes\n"

            mock_commit = mock.MagicMock()
            mock_commit.returncode = 0

            def fake_run(cmd, *args, **kwargs):
                if "diff" in cmd:
                    return mock_diff
                return mock_commit

            mock_run.side_effect = fake_run

            logs = []
            ok, msg = ch.execute_package_commit(tmpdir, interactive=False, on_log=logs.append)
            self.assertTrue(ok)
            self.assertIn("Update to version 2.0", msg)

    def test_execute_package_commit_osc_rejection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, ".osc"))
            ok, msg = ch.execute_package_commit(tmpdir)
            self.assertFalse(ok)
            self.assertIn("managed by osc", msg)

if __name__ == '__main__':
    unittest.main()
