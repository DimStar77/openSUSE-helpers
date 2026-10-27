#!/usr/bin/env python3
import os
import sys
import tempfile
import unittest
import unittest.mock as mock
import subprocess

import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
GECKOPIT_DIR = os.path.dirname(TESTS_DIR)
HELPERS_DIR = os.path.join(GECKOPIT_DIR, "helpers")

for p in [GECKOPIT_DIR, HELPERS_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

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

    @mock.patch("subprocess.run")
    def test_stage_package_files_obscpio_buildtime(self, mock_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            service_path = os.path.join(tmpdir, "_service")
            # Test mode before name: mode="buildtime" name="tar"
            with open(service_path, "w") as f:
                f.write('''<services>
  <service mode="buildtime" name="tar" />
</services>''')
            obscpio_path = os.path.join(tmpdir, "pkg-1.0.obscpio")
            with open(obscpio_path, "w") as f:
                f.write("obscpio content")

            mock_proc = mock.MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = ""
            mock_run.return_value = mock_proc

            logs = []
            ok, msg = ch.stage_package_files(tmpdir, on_log=logs.append)
            self.assertTrue(ok)
            calls = [c[0][0] for c in mock_run.call_args_list]
            self.assertIn(["git", "-C", tmpdir, "add", "-f", "pkg-1.0.obscpio"], calls)

    @mock.patch("subprocess.run")
    def test_stage_package_files_obscpio_manual_not_staged(self, mock_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            service_path = os.path.join(tmpdir, "_service")
            # When tar mode is manual (regardless of attribute order), obscpio must NOT be staged
            with open(service_path, "w") as f:
                f.write('''<services>
  <service mode="manual" name="obs_scm"/>
  <service mode="manual" name="tar"/>
</services>''')
            obscpio_path = os.path.join(tmpdir, "feedbackd-0.8.10.obscpio")
            with open(obscpio_path, "w") as f:
                f.write("obscpio content")

            mock_proc = mock.MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = ""
            mock_run.return_value = mock_proc

            logs = []
            ok, msg = ch.stage_package_files(tmpdir, on_log=logs.append)
            self.assertTrue(ok)
            calls = [c[0][0] for c in mock_run.call_args_list]
            self.assertFalse(any("feedbackd-0.8.10.obscpio" in str(c) for c in calls))

    @mock.patch("subprocess.run")
    def test_execute_package_commit_metadata_package_without_changes(self, mock_run):
        """Verifies committing a metadata package without a .changes file uses staged files fallback."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, 'groups.yml'), 'w') as f:
                f.write('patterns: [base]\n')

            def fake_run(cmd, *args, **kwargs):
                cmd_str = ' '.join(str(c) for c in cmd)
                m = mock.MagicMock()
                m.returncode = 0
                if 'diff --cached --name-only' in cmd_str:
                    m.stdout = 'groups.yml\n'
                elif 'diff --name-only' in cmd_str:
                    m.stdout = ''
                elif 'ls-files' in cmd_str:
                    m.stdout = ''
                else:
                    m.stdout = ''
                return m

            mock_run.side_effect = fake_run

            logs = []
            ok, msg = ch.execute_package_commit(tmpdir, interactive=False, on_log=logs.append)
            self.assertTrue(ok)
            self.assertIn('committed', msg.lower())
            self.assertTrue(any('No .changes file found' in log for log in logs))

if __name__ == '__main__':

    unittest.main()
