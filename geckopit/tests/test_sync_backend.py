#!/usr/bin/env python3
import os
import sys
import unittest
import tempfile
import unittest.mock as mock
import subprocess
import json
import threading
import time

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


    def test_discover_workspace_packages(self):
        import tempfile
        import sync_backend as sb

        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Superproject with .gitmodules
            sp_dir = os.path.join(tmpdir, "superproject")
            os.makedirs(sp_dir)
            with open(os.path.join(sp_dir, ".gitmodules"), "w") as f:
                f.write("[submodule \"pkgA\"]\n\tpath = pkgA\n[submodule \"pkgB\"]\n\tpath = pkgB\n")

            found_sp = sb.discover_workspace_packages(sp_dir)
            self.assertEqual(found_sp, ["pkgA", "pkgB"])

            # 1b. Regression test: .gitmodules with 'path' in package name and relative URL
            with open(os.path.join(sp_dir, ".gitmodules"), "w") as f:
                f.write('[submodule "telepathy-farstream"]\n\tpath = telepathy-farstream\n\turl = ../telepathy-farstream\n')
            found_tele = sb.discover_workspace_packages(sp_dir)
            self.assertEqual(found_tele, ["telepathy-farstream"])

            # 2. Categorized portfolio tree (depth <= 2)
            pf_dir = os.path.join(tmpdir, "portfolio")
            p_vlc = os.path.join(pf_dir, "dimstar", "vlc")
            p_toycars = os.path.join(pf_dir, "games", "toycars")
            os.makedirs(p_vlc)
            os.makedirs(p_toycars)

            with open(os.path.join(p_vlc, "vlc.spec"), "w") as f:
                f.write("Name: vlc\nVersion: 3.0\n")
            with open(os.path.join(p_toycars, "toycars.spec"), "w") as f:
                f.write("Name: toycars\nVersion: 0.3\n")

            found_pf = sb.discover_workspace_packages(pf_dir)
            self.assertEqual(found_pf, ["dimstar/vlc", "games/toycars"])

    def test_check_repo_sync_osc_checkout(self):
        import tempfile
        import sync_backend as sb

        with tempfile.TemporaryDirectory() as tmpdir:
            pkg_dir = os.path.join(tmpdir, "games", "toycars")
            osc_dir = os.path.join(pkg_dir, ".osc")
            os.makedirs(osc_dir)

            with open(os.path.join(osc_dir, "_project"), "w") as f:
                f.write("games\n")
            with open(os.path.join(osc_dir, "_package"), "w") as f:
                f.write("toycars\n")
            with open(os.path.join(pkg_dir, "toycars.spec"), "w") as f:
                f.write("Name: toycars\nVersion: 0.3.10\n")

            name, data = sb.check_repo_sync("games/toycars", workspace_path=tmpdir)
            self.assertEqual(data["status"], "success")
            self.assertTrue(data.get("is_osc"))
            self.assertEqual(data.get("osc_project"), "games")
            self.assertEqual(data.get("osc_package"), "toycars")
            self.assertEqual(data.get("pool_status"), "OSC Managed")

    def test_check_worktree_status_osc_checkout(self):
        import tempfile
        import sync_backend as sb

        with tempfile.TemporaryDirectory() as tmpdir:
            pkg_dir = os.path.join(tmpdir, "toycars")
            osc_dir = os.path.join(pkg_dir, ".osc")
            os.makedirs(osc_dir)
            with open(os.path.join(osc_dir, "_project"), "w") as f:
                f.write("games\n")
            with open(os.path.join(pkg_dir, "toycars.spec"), "w") as f:
                f.write("Name: toycars\n")

            wt = sb.check_worktree_status(pkg_dir)
            self.assertTrue(wt["exists"])
            self.assertTrue(wt.get("is_osc"))
            self.assertEqual(wt["head"], "osc")
            self.assertEqual(wt["upstream"], "OBS")

    def test_check_repo_version_local_spec_fallback(self):
        import tempfile
        import sync_backend as sb

        with tempfile.TemporaryDirectory() as tmpdir:
            pkg_dir = os.path.join(tmpdir, "graphics", "shotwell")
            os.makedirs(pkg_dir)
            with open(os.path.join(pkg_dir, "shotwell.spec"), "w") as f:
                f.write("Name: shotwell\nVersion: 33.0\n")

            with mock.patch("sync_backend.run_tracked", side_effect=Exception("No git")):
                with mock.patch("sync_backend.get_http_session") as mock_sess:
                    mock_resp = mock.Mock()
                    mock_resp.status_code = 200
                    mock_resp.json.return_value = {"items": [{"name": "shotwell", "version": "0.32.17"}]}
                    mock_sess.return_value.get.return_value = mock_resp

                    name, ver = sb.check_repo_version("graphics/shotwell", workspace_path=tmpdir, skip_fetch=True)
                    self.assertEqual(ver["status"], "success")
                    self.assertEqual(ver["factory_ver"], "33.0")
                    self.assertEqual(ver["upstream_stable"], "0.32.17")

    def test_autodetect_worktree_pairing(self):
        import tempfile
        import sync_backend as sb

        with tempfile.TemporaryDirectory() as tmpdir:
            main_repo = os.path.join(tmpdir, "main")
            wt_repo = os.path.join(tmpdir, "main_next")
            os.makedirs(os.path.join(main_repo, ".git", "worktrees", "main_next"))
            os.makedirs(wt_repo)

            with open(os.path.join(main_repo, ".git", "HEAD"), "w") as f:
                f.write("ref: refs/heads/factory\n")

            with open(os.path.join(main_repo, ".git", "worktrees", "main_next", "gitdir"), "w") as f:
                f.write(f"{wt_repo}/.git\n")
            with open(os.path.join(main_repo, ".git", "worktrees", "main_next", "HEAD"), "w") as f:
                f.write("ref: refs/heads/next\n")

            with open(os.path.join(wt_repo, ".git"), "w") as f:
                f.write(f"gitdir: {main_repo}/.git/worktrees/main_next\n")

            res_main = sb.autodetect_worktree_pairing(main_repo)
            self.assertIsNotNone(res_main)
            self.assertEqual(res_main["stable_path"], main_repo)
            self.assertEqual(res_main["unstable_path"], wt_repo)
            self.assertEqual(res_main["stable_branch"], "factory")
            self.assertEqual(res_main["unstable_branch"], "next")

            res_wt = sb.autodetect_worktree_pairing(wt_repo)
            self.assertIsNotNone(res_wt)
            self.assertEqual(res_wt["stable_path"], main_repo)
            self.assertEqual(res_wt["unstable_path"], wt_repo)

    def test_get_upstream_gitea_target(self):
        import sync_backend as sb
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Local .git/config has remote upstream pointing to GNOME/baobab.git
            git_dir = os.path.join(tmpdir, ".git")
            os.makedirs(git_dir)
            with open(os.path.join(git_dir, "config"), "w") as f:
                f.write("[remote \"upstream\"]\n\turl = git@src.opensuse.org:GNOME/baobab.git\n")

            sb._GITEA_PARENT_CACHE.clear()
            target = sb.get_upstream_gitea_target(tmpdir, "bob", "baobab")
            self.assertEqual(target, ("GNOME", "baobab"))

        # 2. Gitea API fork parent (e.g. KDE/dolphin)
        sb._GITEA_PARENT_CACHE.clear()
        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "fork": True,
            "parent": {"full_name": "KDE/dolphin"}
        }
        with mock.patch("sync_backend.get_http_session") as mock_sess:
            mock_sess.return_value.get.return_value = mock_resp
            target = sb.get_upstream_gitea_target("/nonexistent", "alice", "dolphin")
            self.assertEqual(target, ("KDE", "dolphin"))

        # 3. Fallback to pool/pkg when not a fork
        sb._GITEA_PARENT_CACHE.clear()
        mock_resp_nofork = mock.Mock()
        mock_resp_nofork.status_code = 200
        mock_resp_nofork.json.return_value = {"fork": False}
        with mock.patch("sync_backend.get_http_session") as mock_sess:
            mock_sess.return_value.get.return_value = mock_resp_nofork
            target = sb.get_upstream_gitea_target("/nonexistent", "someone", "mypkg")
            self.assertEqual(target, ("pool", "mypkg"))

    def test_get_gitea_owner_and_repo_namespace_fallback(self):
        import helpers.sync_backend as sb
        # Namespace in default_name without git config falls back to the namespace
        owner, repo = sb.get_gitea_owner_and_repo("/nonexistent", "dimstar/vlc")
        self.assertEqual(owner, "dimstar")
        self.assertEqual(repo, "vlc")

        owner2, repo2 = sb.get_gitea_owner_and_repo("/nonexistent", "multimedia:libs/gstreamer")
        self.assertEqual(owner2, "multimedia:libs")
        self.assertEqual(repo2, "gstreamer")

        # Flat package name without slash falls back to GNOME
        owner3, repo3 = sb.get_gitea_owner_and_repo("/nonexistent", "baobab")
        self.assertEqual(owner3, "GNOME")
        self.assertEqual(repo3, "baobab")

    @mock.patch("sync_backend.get_http_session")
    def test_check_repo_version_ignored_versions_stable_and_unstable(self, mock_get_sess):
        import sync_backend as sb
        import tempfile
        import os

        mock_sess = mock.Mock()
        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "items": [{"name": "lasem", "stable_version": "0.6.0", "version": "0.6.0"}]
        }
        mock_sess.get.return_value = mock_resp
        mock_get_sess.return_value = mock_sess

        with tempfile.TemporaryDirectory() as tmpdir:
            pkg_dir = os.path.join(tmpdir, "lasem")
            os.makedirs(pkg_dir)
            spec_path = os.path.join(pkg_dir, "lasem.spec")
            with open(spec_path, "w") as f:
                f.write("Name: lasem\nVersion: 0.4.3\n")

            with mock.patch("sync_backend.run_tracked", return_value=mock.Mock(stdout="Version: 0.4.3\n", returncode=0)):
                # Case 1: 0.6.0 is ignored -> neither stable nor unstable should trigger needs_update
                _, ver_ignored = sb.check_repo_version(
                    "lasem",
                    stable_branch="factory",
                    unstable_branch="next",
                    workspace_path=tmpdir,
                    ignored_versions={"lasem": "0.6.0"},
                    skip_fetch=True
                )
                self.assertFalse(ver_ignored["stable_needs_update"])
                self.assertFalse(ver_ignored["unstable_needs_update"])
                self.assertFalse(ver_ignored["needs_update"])
                self.assertTrue(ver_ignored["is_ignored_stable"])
                self.assertTrue(ver_ignored["is_ignored_unstable"])

                # Case 2: 0.5.0 is ignored but upstream is 0.6.0 -> needs_update MUST trigger
                _, ver_not_ignored = sb.check_repo_version(
                    "lasem",
                    stable_branch="factory",
                    unstable_branch="next",
                    workspace_path=tmpdir,
                    ignored_versions={"lasem": "0.5.0"},
                    skip_fetch=True
                )
                self.assertTrue(ver_not_ignored["stable_needs_update"])
                self.assertTrue(ver_not_ignored["needs_update"])
                self.assertFalse(ver_not_ignored["is_ignored_stable"])

    @mock.patch("sync_backend.get_http_session")
    def test_check_repo_version_anitya_project_maintenance_fallback(self, mock_get_sess):
        import sync_backend as sb
        import tempfile
        import os

        # Package endpoint returns 0.6.0 as stable
        mock_pkg_resp = mock.Mock()
        mock_pkg_resp.status_code = 200
        mock_pkg_resp.json.return_value = {
            "items": [{"name": "lasem", "project": "lasem", "stable_version": "0.6.0", "version": "0.6.0"}]
        }

        # Project endpoint returns historical releases including maintenance 0.4.5
        mock_prj_resp = mock.Mock()
        mock_prj_resp.status_code = 200
        mock_prj_resp.json.return_value = {
            "items": [{
                "name": "lasem",
                "stable_versions": ["0.6.0", "0.4.5", "0.4.4", "0.4.3"],
                "versions": ["0.6.0", "0.4.5", "0.4.4", "0.4.3"]
            }]
        }

        def fake_get(url, **kwargs):
            if "projects" in url:
                return mock_prj_resp
            return mock_pkg_resp

        mock_sess = mock.Mock()
        mock_sess.get.side_effect = fake_get
        mock_get_sess.return_value = mock_sess

        with tempfile.TemporaryDirectory() as tmpdir:
            pkg_dir = os.path.join(tmpdir, "lasem")
            os.makedirs(pkg_dir)
            with open(os.path.join(pkg_dir, "lasem.spec"), "w") as f:
                f.write("Name: lasem\nVersion: 0.4.4\n")

            with mock.patch("sync_backend.run_tracked", return_value=mock.Mock(stdout="Version: 0.4.4\n", returncode=0)):
                # Case: 0.6.0 is ignored -> fallback MUST discover 0.4.5 and trigger needs_update!
                _, ver = sb.check_repo_version(
                    "lasem",
                    stable_branch="factory",
                    unstable_branch="next",
                    workspace_path=tmpdir,
                    ignored_versions={"lasem": "0.6.0"},
                    skip_fetch=True
                )
                self.assertEqual(ver["upstream_stable"], "0.4.5")
                self.assertTrue(ver["stable_needs_update"])
                self.assertTrue(ver["needs_update"])
                self.assertFalse(ver["is_ignored_stable"])

    def test_is_version_ignored_multi_version_support(self):
        import sync_backend as sb
        self.assertTrue(sb.is_version_ignored("0.6.0", "0.6.0", "lasem"))
        self.assertTrue(sb.is_version_ignored("0.6.0", "0.6.0, 0.5.2", "lasem"))
        self.assertTrue(sb.is_version_ignored("0.5.2", "0.6.0, 0.5.2", "lasem"))
        self.assertTrue(sb.is_version_ignored("0.6.0", ["0.6.0", "0.5.2"], "lasem"))
        self.assertFalse(sb.is_version_ignored("0.4.5", "0.6.0, 0.5.2", "lasem"))
        self.assertFalse(sb.is_version_ignored("0.4.5", None, "lasem"))

    def test_prune_superseded_ignored_versions(self):
        import sync_backend as sb

        # Case 1: Dual stream - both factory and next surpass ignored -> pruned
        new_d1, pruned1 = sb.prune_superseded_ignored_versions("lasem", "0.7.0", "0.7.0", "next", {"lasem": "0.6.0"})
        self.assertEqual(pruned1, ["0.6.0"])
        self.assertEqual(new_d1, {})

        # Case 2: Dual stream - only unstable surpasses -> NOT pruned
        new_d2, pruned2 = sb.prune_superseded_ignored_versions("lasem", "0.4.4", "0.7.0", "next", {"lasem": "0.6.0"})
        self.assertEqual(pruned2, [])
        self.assertEqual(new_d2, {"lasem": "0.6.0"})

        # Case 3: Dual stream - only stable surpasses -> NOT pruned
        new_d3, pruned3 = sb.prune_superseded_ignored_versions("lasem", "0.7.0", "0.4.4", "next", {"lasem": "0.6.0"})
        self.assertEqual(pruned3, [])
        self.assertEqual(new_d3, {"lasem": "0.6.0"})

        # Case 4: Single stream (flat org) - stable surpasses -> pruned
        new_d4, pruned4 = sb.prune_superseded_ignored_versions("lasem", "0.7.0", None, None, {"lasem": "0.6.0"})
        self.assertEqual(pruned4, ["0.6.0"])
        self.assertEqual(new_d4, {})

        # Case 5: Multi-version - 0.5.2 surpassed, 0.6.0 retained
        new_d5, pruned5 = sb.prune_superseded_ignored_versions("lasem", "0.5.5", "0.5.5", "next", {"lasem": "0.5.2, 0.6.0"})
        self.assertEqual(pruned5, ["0.5.2"])
        self.assertEqual(new_d5, {"lasem": "0.6.0"})

    def test_resolve_stream_candidate_successive_ignores(self):
        import sync_backend as sb
        vers = ['0.99.11', '0.99.10', '0.99.9', '0.99.8', '0.24.2']

        # Baseline: none ignored -> suggests 0.99.11
        t_ver, is_ign, needs_up = sb.resolve_stream_candidate('0.99.11', '0.24.2', None, vers, 'telepathy-glib')
        self.assertEqual(t_ver, '0.99.11')
        self.assertFalse(is_ign)
        self.assertTrue(needs_up)

        # Ignore 0.99.11 -> suggests 0.99.10
        t_ver, is_ign, needs_up = sb.resolve_stream_candidate('0.99.11', '0.24.2', '0.99.11', vers, 'telepathy-glib')
        self.assertEqual(t_ver, '0.99.10')
        self.assertFalse(is_ign)
        self.assertTrue(needs_up)

        # Ignore 0.99.10 as well -> suggests 0.99.9
        t_ver, is_ign, needs_up = sb.resolve_stream_candidate('0.99.11', '0.24.2', '0.99.11, 0.99.10', vers, 'telepathy-glib')
        self.assertEqual(t_ver, '0.99.9')
        self.assertFalse(is_ign)
        self.assertTrue(needs_up)

        # Ignore all newer versions -> reports 0.99.11 as ignored, needs_update False
        t_ver, is_ign, needs_up = sb.resolve_stream_candidate('0.99.11', '0.24.2', '0.99.11, 0.99.10, 0.99.9, 0.99.8', vers, 'telepathy-glib')
        self.assertEqual(t_ver, '0.99.11')
        self.assertTrue(is_ign)
        self.assertFalse(needs_up)

        # Unignore 0.99.10 -> brings back 0.99.10
        t_ver, is_ign, needs_up = sb.resolve_stream_candidate('0.99.11', '0.24.2', '0.99.11, 0.99.9, 0.99.8', vers, 'telepathy-glib')
        self.assertEqual(t_ver, '0.99.10')
        self.assertFalse(is_ign)
        self.assertTrue(needs_up)

    def test_resolve_best_upstream_versions_dual_and_single_stream(self):
        import sync_backend as sb

        # Dual stream: stable is 0.24.2 (up-to-date), unstable has 0.99.x
        res = sb.resolve_best_upstream_versions(
            raw_upstream_stable='0.24.2',
            raw_upstream_latest='0.99.11',
            factory_ver='0.24.2',
            next_ver='0.24.2',
            ignored_ver='0.99.11, 0.99.10',
            repo_name='telepathy-glib',
            stable_versions=['0.24.2'],
            all_versions=['0.99.11', '0.99.10', '0.99.9', '0.99.8', '0.24.2'],
            unstable_branch='next'
        )
        self.assertEqual(res['upstream_stable'], '0.24.2')
        self.assertFalse(res['stable_needs_update'])
        self.assertEqual(res['upstream_latest'], '0.99.9')
        self.assertTrue(res['unstable_needs_update'])
        self.assertFalse(res['is_ignored_unstable'])
        self.assertTrue(res['needs_update'])

        # Single stream: stable_versions empty, all_versions has maintenance releases
        res_single = sb.resolve_best_upstream_versions(
            raw_upstream_stable='0.6.0',
            raw_upstream_latest='0.6.0',
            factory_ver='0.4.4',
            next_ver=None,
            ignored_ver='0.6.0',
            repo_name='lasem',
            stable_versions=[],
            all_versions=['0.6.0', '0.4.5', '0.4.4', '0.4.3'],
            unstable_branch=None
        )
        self.assertEqual(res_single['upstream_stable'], '0.4.5')
        self.assertTrue(res_single['stable_needs_update'])
        self.assertFalse(res_single['is_ignored_stable'])
        self.assertFalse(res_single['unstable_needs_update'])
        self.assertTrue(res_single['needs_update'])

    def test_metadata_package_support(self):
        """Tests pure metadata package discovery, name resolution, and spec-less version handling."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Package with .osc directory alone (classic OBS checkout of metadata package)
            osc_pkg_dir = os.path.join(tmpdir, '000package-groups')
            os.makedirs(os.path.join(osc_pkg_dir, '.osc'))
            with open(os.path.join(osc_pkg_dir, '.osc', '_package'), 'w') as f:
                f.write('000package-groups\n')
            with open(os.path.join(osc_pkg_dir, 'groups.yml'), 'w') as f:
                f.write('patterns:\n  - base\n')
            with open(os.path.join(osc_pkg_dir, 'openSUSE.product.in'), 'w') as f:
                f.write('<product/>\n')

            self.assertTrue(sb.is_package_dir(osc_pkg_dir))
            self.assertEqual(sb.get_package_name_from_dir(osc_pkg_dir), '000package-groups')

            # Version check must succeed gracefully with is_metadata=True and no spec
            name, ver_data = sb.check_repo_version(
                '000package-groups',
                workspace_path=tmpdir,
                skip_fetch=True
            )
            self.assertEqual(name, '000package-groups')
            self.assertEqual(ver_data['status'], 'success')
            self.assertTrue(ver_data.get('is_metadata'))
            self.assertEqual(ver_data['factory_ver'], 'N/A')
            self.assertFalse(ver_data['needs_update'])

            # 2. Package with .kiwi appliance definition
            kiwi_dir = os.path.join(tmpdir, 'jeos-appliance')
            os.makedirs(kiwi_dir)
            with open(os.path.join(kiwi_dir, 'jeos.kiwi'), 'w') as f:
                f.write('<image name="jeos"><version>2.5.0</version></image>\n')

            self.assertTrue(sb.is_package_dir(kiwi_dir))
            self.assertEqual(sb.get_local_package_version(kiwi_dir), '2.5.0')
            _, kiwi_ver = sb.check_repo_version(
                'jeos-appliance',
                workspace_path=tmpdir,
                skip_fetch=True
            )
            self.assertEqual(kiwi_ver['status'], 'success')
            self.assertTrue(kiwi_ver.get('is_metadata'))
            self.assertEqual(kiwi_ver['factory_ver'], '2.5.0')

            # 3. Workspace package discovery finds both metadata packages
            discovered = sb.discover_workspace_packages(tmpdir)
            self.assertIn('000package-groups', discovered)
            self.assertIn('jeos-appliance', discovered)

    def test_has_git_ref_prefix_candidates(self):
        """Verifies has_git_ref resolves refs both with and without explicit refs/ prefixes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = os.path.join(tmpdir, "pkg")
            os.makedirs(repo_dir)
            subprocess.run(["git", "init", repo_dir], capture_output=True, check=True)
            # Create dummy remote ref in packed-refs or loose
            git_dir = os.path.join(repo_dir, ".git")
            os.makedirs(os.path.join(git_dir, "refs", "remotes", "origin"), exist_ok=True)
            with open(os.path.join(git_dir, "refs", "remotes", "origin", "factory"), "w") as f:
                f.write("0123456789abcdef0123456789abcdef01234567\n")

            self.assertTrue(sb.has_git_ref(repo_dir, "refs/remotes/origin/factory"))
            self.assertTrue(sb.has_git_ref(repo_dir, "origin/factory"))
            self.assertFalse(sb.has_git_ref(repo_dir, "origin/nonexistent"))

    def test_check_repo_sync_compare_ref_prefers_stable_branch(self):
        """Verifies check_repo_sync compares stable branch against pool rather than falling back to @{u}."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = os.path.join(tmpdir, "pkg")
            os.makedirs(repo_dir)
            subprocess.run(["git", "init", repo_dir], capture_output=True, check=True)
            git_dir = os.path.join(repo_dir, ".git")
            os.makedirs(os.path.join(git_dir, "refs", "remotes", "origin"), exist_ok=True)
            os.makedirs(os.path.join(git_dir, "refs", "pool"), exist_ok=True)

            # Create an initial commit
            test_file = os.path.join(repo_dir, "test.txt")
            with open(test_file, "w") as f:
                f.write("initial\n")
            subprocess.run(["git", "-C", repo_dir, "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", repo_dir, "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", repo_dir, "add", "."], check=True)
            subprocess.run(["git", "-C", repo_dir, "commit", "-m", "initial"], check=True)

            rev = subprocess.run(["git", "-C", repo_dir, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

            # Set refs/remotes/origin/factory and refs/pool/factory to same commit
            with open(os.path.join(git_dir, "refs", "remotes", "origin", "factory"), "w") as f:
                f.write(f"{rev}\n")
            with open(os.path.join(git_dir, "refs", "pool", "factory"), "w") as f:
                f.write(f"{rev}\n")

            # Mock check_repo_sync network fetch to no-op
            with mock.patch("helpers.sync_backend.run_tracked") as mock_rt:
                # Provide rev-list count returning "0 0" for compare_ref
                def fake_rt(cmd, *args, **kwargs):
                    m = mock.MagicMock(returncode=0, stdout="0	0\n", stderr="")
                    return m
                mock_rt.side_effect = fake_rt
                name, data = sb.check_repo_sync("pkg", stable_branch="factory", unstable_branch="next", workspace_path=tmpdir, known_not_in_pool=set())
                self.assertEqual(data["status"], "success")
                self.assertEqual(data["pool_ahead"], 0)
                self.assertEqual(data["pool_behind"], 0)

    def test_get_workspace_profile_for_path_boundary(self):
        """Verifies get_workspace_profile_for_path correctly detects inside vs outside workspace."""
        with tempfile.TemporaryDirectory() as td:
            cfg_file = os.path.join(td, "geckopit.json")
            cfg = {
                "active_workspace": "GNOME",
                "workspaces": {
                    "GNOME": {
                        "stable_path": os.path.join(td, "GNOME"),
                        "stable_branch": "factory",
                        "unstable_path": os.path.join(td, "GNOME:Next"),
                        "unstable_branch": "next"
                    }
                }
            }
            with open(cfg_file, "w") as f:
                json.dump(cfg, f)

            p_in = os.path.join(td, "GNOME:Next", "pkg1")
            p_out = os.path.join(td, "random", "pkg2")
            os.makedirs(p_in, exist_ok=True)
            os.makedirs(p_out, exist_ok=True)

            ws_in, prof_in = sb.get_workspace_profile_for_path(p_in, config_file=cfg_file)
            self.assertEqual(ws_in, "GNOME")
            self.assertEqual(prof_in.get("unstable_branch"), "next")

            ws_out, prof_out = sb.get_workspace_profile_for_path(p_out, config_file=cfg_file)
            self.assertIsNone(ws_out)
            self.assertIsNone(prof_out)

    def test_update_profile_cache_for_package_writes_and_skips(self):
        """Verifies update_profile_cache_for_package writes cache for workspace packages and skips others."""
        with tempfile.TemporaryDirectory() as td:
            cfg_file = os.path.join(td, "geckopit.json")
            cache_dir = os.path.join(td, "cache")
            cfg = {
                "active_workspace": "GNOME",
                "workspaces": {
                    "GNOME": {
                        "stable_path": os.path.join(td, "GNOME"),
                        "stable_branch": "factory",
                        "unstable_path": os.path.join(td, "GNOME:Next"),
                        "unstable_branch": "next"
                    }
                }
            }
            with open(cfg_file, "w") as f:
                json.dump(cfg, f)

            pkg_in = os.path.join(td, "GNOME:Next", "testpkg")
            pkg_out = os.path.join(td, "outside", "randompkg")
            os.makedirs(pkg_in, exist_ok=True)
            os.makedirs(pkg_out, exist_ok=True)

            fake_data = {
                "repo_name": "testpkg",
                "pkg_dir": pkg_in,
                "active_branch": "next",
                "sync": {"status": "success", "pool_status": "In Sync", "pool_ahead": 0},
                "version": {"status": "success", "needs_update": False},
                "pr": {"has_pr": True, "count": 1},
                "worktree": {"ahead": 0, "behind": 0, "dirty": False}
            }

            # 1. Update inside workspace
            ok = sb.update_profile_cache_for_package(pkg_in, package_data=fake_data, cache_dir=cache_dir, config_file=cfg_file)
            self.assertTrue(ok)
            cache_file = os.path.join(cache_dir, "cache_GNOME.json")
            self.assertTrue(os.path.exists(cache_file))
            with open(cache_file) as f:
                c = json.load(f)
            self.assertIn("testpkg", c["package_data"])
            self.assertEqual(c["package_data"]["testpkg"]["pr"]["count"], 1)
            self.assertEqual(c["package_data"]["testpkg"]["worktree"]["unstable"]["dirty"], False)

            # 2. Update outside workspace
            ok_out = sb.update_profile_cache_for_package(pkg_out, package_data=fake_data, cache_dir=cache_dir, config_file=cfg_file)
            self.assertFalse(ok_out)

    def test_profile_cache_lock_mutual_exclusion(self):
        """Verifies profile_cache_lock provides mutual exclusion across concurrent workers."""
        with tempfile.TemporaryDirectory() as td:
            cache_file = os.path.join(td, "cache_GNOME.json")
            events = []

            def worker_holding_lock():
                with sb.profile_cache_lock(cache_file):
                    events.append("worker1_acquired")
                    time.sleep(0.1)
                    events.append("worker1_released")

            def worker_waiting():
                time.sleep(0.02)
                with sb.profile_cache_lock(cache_file):
                    events.append("worker2_acquired")

            t1 = threading.Thread(target=worker_holding_lock)
            t2 = threading.Thread(target=worker_waiting)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            self.assertEqual(events, ["worker1_acquired", "worker1_released", "worker2_acquired"])

if __name__ == '__main__':

    unittest.main()
