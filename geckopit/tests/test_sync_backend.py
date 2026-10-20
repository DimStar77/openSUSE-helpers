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
from sync_backend import has_git_ref

class TestSyncBackend(unittest.TestCase):
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

            # 4. SSH / transport connection reset or rate-limiting -> Fetch failed (NOT Not in Pool)
            def fake_run_conn_reset(cmd, *args, **kwargs):
                if 'fetch' in cmd and 'origin' in cmd:
                    return mock.Mock(stdout="", returncode=0)
                elif 'rev-parse' in cmd:
                    return mock.Mock(stdout="hash", returncode=0)
                elif 'fetch' in cmd and 'src.opensuse.org/pool' in cmd[5]:
                    raise subprocess.CalledProcessError(
                        128, cmd,
                        stderr="kex_exchange_identification: read: Connection reset by peer\nfatal: Could not read from remote repository.\n"
                    )
                elif 'rev-list' in cmd:
                    return mock.Mock(stdout="0 0\n", returncode=0)
                return mock.Mock(stdout="", returncode=0)

            mock_run.side_effect = fake_run_conn_reset
            _, data = sb.check_repo_sync('mypkg')
            self.assertEqual(data['pool_status'], 'Fetch failed')
            self.assertTrue(data['needs_action'])

    @mock.patch("sync_backend.get_http_session")
    @mock.patch("sync_backend.get_gitea_owner_and_repo", return_value=("GNOME", "baobab"))
    @mock.patch("sync_backend.get_gitea_token", return_value=None)
    def test_check_repo_pr_multi_pr_support(self, mock_tok, mock_owner, mock_session):
        import sync_backend as sb
        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {
                "number": 12,
                "title": "Update to version 51.0",
                "html_url": "https://src.opensuse.org/GNOME/baobab/pulls/12",
                "user": {"login": "contributor"},
                "base": {"ref": "factory"},
                "head": {"ref": "update-51", "repo": {"full_name": "contributor/baobab"}},
                "created_at": "2026-10-20T10:00:00Z"
            },
            {
                "number": 15,
                "title": "Forward next to factory: baobab",
                "html_url": "https://src.opensuse.org/GNOME/baobab/pulls/15",
                "user": {"login": "dimstar"},
                "base": {"ref": "factory"},
                "head": {"ref": "next"},
                "created_at": "2026-10-20T12:00:00Z"
            }
        ]
        mock_session.return_value.get.return_value = mock_resp

        repo, pr_info = sb.check_repo_pr("baobab", stable_branch="factory", unstable_branch="next")
        self.assertEqual(repo, "baobab")
        self.assertTrue(pr_info["has_pr"])
        self.assertTrue(pr_info["has_forward_pr"])
        self.assertEqual(pr_info["count"], 2)
        self.assertEqual(pr_info["number"], 15)
        self.assertEqual(pr_info["url"], "https://src.opensuse.org/GNOME/baobab/pulls/15")
        self.assertEqual(len(pr_info["prs"]), 2)
        self.assertEqual(pr_info["prs"][0]["number"], 12)
        self.assertEqual(pr_info["prs"][0]["author"], "contributor")
        self.assertFalse(pr_info["prs"][0]["is_forward"])
        self.assertEqual(pr_info["prs"][1]["number"], 15)
        self.assertTrue(pr_info["prs"][1]["is_forward"])

        # Case 2: Only contributor PR exists (no forward PR)
        mock_resp.json.return_value = [
            {
                "number": 42,
                "title": "Fix build on riscv64",
                "html_url": "https://src.opensuse.org/GNOME/baobab/pulls/42",
                "user": {"login": "schwab"},
                "base": {"ref": "factory"},
                "head": {"ref": "riscv-fix"},
                "created_at": "2026-10-20T14:00:00Z"
            }
        ]
        _, pr_info2 = sb.check_repo_pr("baobab", stable_branch="factory", unstable_branch="next")
        self.assertTrue(pr_info2["has_pr"])
        self.assertFalse(pr_info2["has_forward_pr"])
        self.assertEqual(pr_info2["count"], 1)
        self.assertEqual(pr_info2["number"], 42)
        self.assertEqual(pr_info2["prs"][0]["author"], "schwab")

    def test_get_http_session_singleton_and_pooling(self):
        import sync_backend as sb
        s1 = sb.get_http_session()
        s2 = sb.get_http_session()
        self.assertIs(s1, s2)
        adapter = s1.adapters.get("https://")
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter._pool_connections, 35)

    def test_has_git_ref_loose_and_packed(self):
        import os
        import tempfile
        import sync_backend as sb

        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Non-existent ref
            self.assertFalse(sb.has_git_ref(tmpdir, "refs/remotes/origin/factory"))

            # 2. Loose ref
            loose_dir = os.path.join(tmpdir, ".git", "refs", "remotes", "origin")
            os.makedirs(loose_dir, exist_ok=True)
            loose_file = os.path.join(loose_dir, "factory")
            with open(loose_file, "w") as f:
                f.write("1111222233334444555566667777888899990000\n")

            self.assertTrue(sb.has_git_ref(tmpdir, "refs/remotes/origin/factory"))
            self.assertFalse(sb.has_git_ref(tmpdir, "refs/remotes/origin/next"))

            # 3. Packed ref
            packed_file = os.path.join(tmpdir, ".git", "packed-refs")
            with open(packed_file, "w") as f:
                f.write("# pack-refs with: peeled-tags\n")
                f.write("aaaabbbbccccddddeeeeffff0000111122223333 refs/remotes/origin/next\n")

            self.assertTrue(sb.has_git_ref(tmpdir, "refs/remotes/origin/next"))

    @mock.patch("sync_backend.run_tracked")
    @mock.patch("sync_backend.get_http_session")
    def test_check_repo_version_skip_fetch(self, mock_get_session, mock_run):
        import os
        import tempfile
        import sync_backend as sb

        mock_session = mock.Mock()
        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"items": [{"name": "testpkg", "stable_version": "1.0", "version": "1.1"}]}
        mock_session.get.return_value = mock_resp
        mock_get_session.return_value = mock_session

        with tempfile.TemporaryDirectory() as tmpdir:
            spec_file = os.path.join(tmpdir, "testpkg.spec")
            with open(spec_file, "w") as f:
                f.write("Name: testpkg\nVersion: 1.0\n")

            # With skip_fetch=True: git fetch origin should NEVER be invoked
            mock_run.reset_mock()
            mock_run.return_value = mock.Mock(stdout="Version: 1.0\n", returncode=0)
            sb.check_repo_version("testpkg", workspace_path=os.path.dirname(tmpdir), skip_fetch=True)

            for call in mock_run.call_args_list:
                cmd = call[0][0]
                self.assertFalse("fetch" in cmd and "origin" in cmd)

            # With skip_fetch=False: git fetch origin MUST be invoked
            mock_run.reset_mock()
            mock_run.return_value = mock.Mock(stdout="Version: 1.0\n", returncode=0)
            sb.check_repo_version("testpkg", workspace_path=os.path.dirname(tmpdir), skip_fetch=False)

            fetch_called = any("fetch" in call[0][0] and "origin" in call[0][0] for call in mock_run.call_args_list)
            self.assertTrue(fetch_called)

    def test_setup_ssh_multiplexing(self):
        import os
        import sync_backend as sb
        sb.setup_ssh_multiplexing()
        self.assertIn("GIT_SSH_COMMAND", os.environ)
        self.assertIn("ControlMaster=auto", os.environ["GIT_SSH_COMMAND"])
        self.assertIn("ControlPath=", os.environ["GIT_SSH_COMMAND"])

    def test_get_gitea_token(self):
        import os
        import sync_backend as sb
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            conf_file = os.path.join(tmpdir, "config.yml")
            with open(conf_file, "w") as f:
                f.write("logins:\n  - name: test\n    token: my_secret_token_12345\n")

            with mock.patch("os.path.expanduser", return_value=conf_file):
                token = sb.get_gitea_token()
                self.assertEqual(token, "my_secret_token_12345")

    @mock.patch("sync_backend.has_git_ref", return_value=True)
    @mock.patch("sync_backend.run_tracked")
    def test_check_repo_sync_known_not_in_pool(self, mock_run, mock_ref):
        import sync_backend as sb
        mock_run.return_value = mock.Mock(stdout="", returncode=0)

        # When known_not_in_pool contains 'pkg1', pool fetch should NOT be executed
        _, data = sb.check_repo_sync("pkg1", known_not_in_pool={"pkg1"})
        self.assertEqual(data["pool_status"], "Not in Pool")
        # Ensure git fetch against pool_url was never called
        for call in mock_run.call_args_list:
            cmd = call[0][0]
            self.assertFalse("fetch" in cmd and "src.opensuse.org/pool" in cmd[5])


if __name__ == '__main__':
    unittest.main()
