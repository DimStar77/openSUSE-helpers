#!/usr/bin/env python3
import os
import sys
import unittest
import unittest.mock as mock
import subprocess

# Ensure helpers are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'helpers'))
import sync_backend as sb

class TestWorktreeTracking(unittest.TestCase):

    def test_nonexistent_path(self):
        res = sb.check_worktree_status("/nonexistent/path/to/repo")
        self.assertFalse(res["exists"])
        self.assertEqual(res["ahead"], 0)
        self.assertEqual(res["behind"], 0)
        self.assertFalse(res["dirty"])
        self.assertFalse(res["untracked"])

    @mock.patch("sync_backend.run_tracked")
    @mock.patch("os.path.exists", return_value=True)
    def test_clean_worktree(self, mock_exists, mock_run):
        porcelain_output = (
            "# branch.oid 1234567890abcdef1234567890abcdef12345678\n"
            "# branch.head next\n"
            "# branch.upstream origin/next\n"
            "# branch.ab +0 -0\n"
        )
        mock_proc = mock.MagicMock()
        mock_proc.stdout = porcelain_output
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc

        res = sb.check_worktree_status("/fake/repo", "next")
        self.assertTrue(res["exists"])
        self.assertEqual(res["head"], "next")
        self.assertEqual(res["upstream"], "origin/next")
        self.assertEqual(res["ahead"], 0)
        self.assertEqual(res["behind"], 0)
        self.assertFalse(res["dirty"])
        self.assertFalse(res["untracked"])

    @mock.patch("sync_backend.run_tracked")
    @mock.patch("os.path.exists", return_value=True)
    def test_behind_and_dirty_worktree(self, mock_exists, mock_run):
        porcelain_output = (
            "# branch.oid 44c56302eec5b2eb9b9a5bc70b740946e3be6119\n"
            "# branch.head next\n"
            "# branch.upstream origin/next\n"
            "# branch.ab +0 -2\n"
            "1 .M N... 100644 100644 100644 aabbcc aabbcc flatpak.spec\n"
            "? untracked_archive.tar.xz\n"
        )
        mock_proc = mock.MagicMock()
        mock_proc.stdout = porcelain_output
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc

        res = sb.check_worktree_status("/fake/repo", "next")
        self.assertTrue(res["exists"])
        self.assertEqual(res["head"], "next")
        self.assertEqual(res["ahead"], 0)
        self.assertEqual(res["behind"], 2)
        self.assertTrue(res["dirty"])
        self.assertTrue(res["untracked"])

    @mock.patch("sync_backend.run_tracked")
    @mock.patch("os.path.exists", return_value=True)
    def test_ahead_worktree(self, mock_exists, mock_run):
        porcelain_output = (
            "# branch.oid 44c56302eec5b2eb9b9a5bc70b740946e3be6119\n"
            "# branch.head factory\n"
            "# branch.upstream origin/factory\n"
            "# branch.ab +3 -0\n"
        )
        mock_proc = mock.MagicMock()
        mock_proc.stdout = porcelain_output
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc

        res = sb.check_worktree_status("/fake/repo", "factory")
        self.assertTrue(res["exists"])
        self.assertEqual(res["ahead"], 3)
        self.assertEqual(res["behind"], 0)
        self.assertFalse(res["dirty"])

    @mock.patch("sync_backend.run_tracked")
    @mock.patch("os.path.exists", return_value=True)
    def test_pull_worktree_success(self, mock_exists, mock_run):
        mock_proc = mock.MagicMock()
        mock_proc.stdout = "Updating 1234..5678\nFast-forward\n"
        mock_proc.stderr = ""
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc

        ok, msg = sb.pull_worktree("/fake/repo", remote="origin", branch="next")
        self.assertTrue(ok)
        self.assertIn("Fast-forward", msg)

    @mock.patch("sync_backend.run_tracked")
    @mock.patch("os.path.exists", return_value=True)
    def test_pull_worktree_failure(self, mock_exists, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(1, ['git', 'pull'], stderr="error: Your local changes would be overwritten")

        ok, msg = sb.pull_worktree("/fake/repo", remote="origin", branch="next")
        self.assertFalse(ok)
        self.assertIn("Your local changes would be overwritten", msg)

    @mock.patch("sync_backend.run_tracked")
    @mock.patch("os.path.exists", return_value=True)
    def test_push_worktree_success(self, mock_exists, mock_run):
        mock_proc = mock.MagicMock()
        mock_proc.stdout = "Everything up-to-date\n"
        mock_proc.stderr = ""
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc

        ok, msg = sb.push_worktree("/fake/repo", remote="origin", branch="next")
        self.assertTrue(ok)
        self.assertIn("Everything up-to-date", msg)

    @mock.patch("sync_backend.run_tracked")
    @mock.patch("os.path.exists", return_value=True)
    def test_push_worktree_failure(self, mock_exists, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(1, ['git', 'push'], stderr="fatal: [rejected] (non-fast-forward)")

        ok, msg = sb.push_worktree("/fake/repo", remote="origin", branch="next")
        self.assertFalse(ok)
        self.assertIn("rejected", msg)

    def test_is_package_dir_detection(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            # Empty folder without .git is not a package dir
            self.assertFalse(sb.is_package_dir(tmpdir))

            # Folder with .git but no packaging files is not a package dir
            os.makedirs(os.path.join(tmpdir, '.git'))
            self.assertFalse(sb.is_package_dir(tmpdir))

            # Adding a .spec file makes it a package dir
            with open(os.path.join(tmpdir, 'pkg.spec'), 'w') as f: f.write('Name: pkg\n')
            self.assertTrue(sb.is_package_dir(tmpdir))

    def test_get_package_name_from_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            # From .spec file
            with open(os.path.join(tmpdir, 'mypkg.spec'), 'w') as f: f.write('Name: mypkg\n')
            self.assertEqual(sb.get_package_name_from_dir(tmpdir), 'mypkg')

            # Without spec, fallback to basename
            os.remove(os.path.join(tmpdir, 'mypkg.spec'))
            self.assertEqual(sb.get_package_name_from_dir(tmpdir), os.path.basename(tmpdir))

    def test_get_local_package_version(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            # None when empty
            self.assertIsNone(sb.get_local_package_version(tmpdir))

            # From .spec
            with open(os.path.join(tmpdir, 'pkg.spec'), 'w') as f: f.write('Version: 1.19.0\n')
            self.assertEqual(sb.get_local_package_version(tmpdir), '1.19.0')

            # .obsinfo takes precedence
            with open(os.path.join(tmpdir, 'pkg.obsinfo'), 'w') as f: f.write('version: 1.19.1\n')
            self.assertEqual(sb.get_local_package_version(tmpdir), '1.19.1')

    def test_load_geckopit_profile_config(self):
        conf = sb.load_geckopit_profile_config()
        self.assertIn('stable_branch', conf)
        self.assertIn('unstable_branch', conf)
        self.assertEqual(conf.get('stable_branch'), 'factory')

if __name__ == '__main__':
    unittest.main()
