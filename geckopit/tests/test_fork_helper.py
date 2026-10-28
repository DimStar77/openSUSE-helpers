#!/usr/bin/env python3
"""
Unit tests for Contributor Fork & Branch Helper (fork_helper.py).
"""

import os
import sys
import unittest
import tempfile
import json
from unittest.mock import patch, MagicMock
from pathlib import Path

# Add helpers to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../helpers')))

from helpers import fork_helper


class TestForkHelper(unittest.TestCase):

    def test_get_current_users_from_env_or_config(self):
        with patch.dict(os.environ, {'USER': 'testuser'}):
            obs_user = fork_helper.get_current_obs_user()
            self.assertTrue(bool(obs_user))
            gitea_user = fork_helper.get_current_gitea_user()
            self.assertTrue(bool(gitea_user))

    def test_get_devel_packages_map_parsing(self):
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / 'devel_packages.map'
            with open(cache_file, 'w', encoding='utf-8') as f:
                f.write("ibus M17N\nfwupd Base:System\nmutter GNOME:Factory\n")

            with patch.object(fork_helper, 'DEVEL_MAP_CACHE_TTL', 999999):
                with patch('helpers.fork_helper.Path.home', return_value=Path(td)):
                    # Clear in-memory cache
                    fork_helper._DEVEL_MAP_MEM_CACHE = None
                    (Path(td) / '.cache' / 'geckopit').mkdir(parents=True, exist_ok=True)
                    target_cache = Path(td) / '.cache' / 'geckopit' / 'devel_packages.map'
                    with open(target_cache, 'w', encoding='utf-8') as f:
                        f.write("ibus M17N\nfwupd Base:System\nmutter GNOME:Factory\n")

                    dmap = fork_helper.get_devel_packages_map()
                    self.assertEqual(dmap.get('ibus'), 'M17N')
                    self.assertEqual(dmap.get('fwupd'), 'Base:System')
                    self.assertEqual(dmap.get('mutter'), 'GNOME:Factory')

    def test_resolve_package_devel_target_leap_stream(self):
        res = fork_helper.resolve_package_devel_target('vlc', stream='leap-16.1')
        self.assertTrue(res['is_git'])
        self.assertEqual(res['git_owner'], 'pool')
        self.assertEqual(res['git_repo'], 'vlc')
        self.assertEqual(res['target_branch'], 'leap-16.1')
        self.assertEqual(res['devel_project'], 'openSUSE:Leap:16.1')

    def test_resolve_package_devel_target_factory_scmsync(self):
        with patch('helpers.fork_helper.get_devel_packages_map', return_value={'fwupd': 'Base:System'}):
            with patch('helpers.pool_submit.is_project_git_managed', return_value={
                'is_git': True,
                'scmsync_url': 'https://src.opensuse.org/dimstar/fwupd#factory'
            }):
                res = fork_helper.resolve_package_devel_target('fwupd', stream='factory')
                self.assertTrue(res['is_git'])
                self.assertEqual(res['git_owner'], 'dimstar')
                self.assertEqual(res['git_repo'], 'fwupd')
                self.assertEqual(res['target_branch'], 'factory')
                self.assertEqual(res['devel_project'], 'Base:System')

    def test_resolve_package_devel_target_factory_obs(self):
        with patch('helpers.fork_helper.get_devel_packages_map', return_value={'ibus': 'M17N'}):
            with patch('helpers.pool_submit.is_project_git_managed', return_value={'is_git': False}):
                res = fork_helper.resolve_package_devel_target('ibus', stream='factory')
                self.assertFalse(res['is_git'])
                self.assertEqual(res['devel_project'], 'M17N')
                self.assertEqual(res['devel_package'], 'ibus')

    def test_resolve_destination_directory_workspace_guard(self):
        with tempfile.TemporaryDirectory() as td:
            ws_dir = os.path.join(td, 'workspace')
            scratch_dir = os.path.join(td, 'scratch')
            os.makedirs(ws_dir, exist_ok=True)
            os.makedirs(scratch_dir, exist_ok=True)

            # 1. Inside workspace WITH scratch_dir configured -> routes out-of-tree
            cfg_data = {
                'scratch_dir': scratch_dir,
                'workspaces': {
                    'TestWS': {
                        'stable_path': ws_dir,
                        'unstable_path': None
                    }
                }
            }
            cfg_file = Path(td) / '.config' / 'geckopit.json'
            cfg_file.parent.mkdir(parents=True, exist_ok=True)
            with open(cfg_file, 'w', encoding='utf-8') as f:
                json.dump(cfg_data, f)

            with patch('helpers.fork_helper.Path.home', return_value=Path(td)):
                with patch('helpers.fork_helper.is_inside_registered_workspace', return_value=(True, 'TestWS')):
                    dest, notice = fork_helper.resolve_destination_directory('ibus')
                    self.assertIsNotNone(dest)
                    self.assertTrue(dest.startswith(scratch_dir))
                    self.assertIn('Detected active workspace', notice)

            # 2. Inside workspace WITHOUT scratch_dir configured -> fails with error
            cfg_data_no_scratch = {
                'workspaces': {
                    'TestWS': {
                        'stable_path': ws_dir,
                        'unstable_path': None
                    }
                }
            }
            with open(cfg_file, 'w', encoding='utf-8') as f:
                json.dump(cfg_data_no_scratch, f)

            with patch('helpers.fork_helper.Path.home', return_value=Path(td)):
                with patch('helpers.fork_helper.is_inside_registered_workspace', return_value=(True, 'TestWS')):
                    dest, err = fork_helper.resolve_destination_directory('ibus')
                    self.assertIsNone(dest)
                    self.assertIn('Cannot check out into current directory', err)

            # 3. Explicit destination override always wins
            custom_dest = os.path.join(td, 'custom_dest')
            dest, err = fork_helper.resolve_destination_directory('ibus', dest_dir_override=custom_dest)
            self.assertEqual(dest, os.path.abspath(custom_dest))
            self.assertIsNone(err)

    def test_resolve_target_obs_project_config(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_data = {
                'default_target_project': 'home:dimstar:Factory'
            }
            cfg_file = Path(td) / '.config' / 'geckopit.json'
            cfg_file.parent.mkdir(parents=True, exist_ok=True)
            with open(cfg_file, 'w', encoding='utf-8') as f:
                json.dump(cfg_data, f)

            with patch('helpers.fork_helper.Path.home', return_value=Path(td)):
                # Factory stream uses configured default_target_project
                tgt = fork_helper.resolve_target_obs_project('M17N', stream='factory')
                self.assertEqual(tgt, 'home:dimstar:Factory')

                # Leap stream resolves stream branch project
                with patch('helpers.fork_helper.get_current_obs_user', return_value='dimstar'):
                    tgt_leap = fork_helper.resolve_target_obs_project('openSUSE:Leap:16.1', stream='leap-16.1')
                    self.assertEqual(tgt_leap, 'home:dimstar:branches:openSUSE:Leap:16.1')

    def test_ensure_gitea_fork_self_owned(self):
        with patch('helpers.fork_helper.get_current_gitea_user', return_value='dimstar'):
            ok, target = fork_helper.ensure_gitea_fork('dimstar', 'fwupd')
            self.assertTrue(ok)
            self.assertEqual(target, 'dimstar/fwupd')

    def test_ensure_gitea_fork_external_repo_exists(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp

        with patch('helpers.fork_helper.get_current_gitea_user', return_value='dimstar'):
            with patch('helpers.sync_backend.get_http_session', return_value=mock_session):
                ok, target = fork_helper.ensure_gitea_fork('GNOME', 'mutter')
                self.assertTrue(ok)
                self.assertEqual(target, 'dimstar/mutter')

    def test_provision_obs_scmsync_package(self):
        mock_sub = MagicMock()
        mock_sub.returncode = 0
        with patch('subprocess.run', return_value=mock_sub):
            ok, msg = fork_helper.provision_obs_scmsync_package(
                'home:dimstar:Factory', 'fwupd', 'https://src.opensuse.org/dimstar/fwupd#for-factory'
            )
            self.assertTrue(ok)
            self.assertIn('Configured', msg)

    def test_fork_package_git_track_workflow(self):
        target_info = {
            'package': 'fwupd',
            'stream': 'factory',
            'devel_project': 'Base:System',
            'devel_package': 'fwupd',
            'is_git': True,
            'scmsync_url': 'https://src.opensuse.org/dimstar/fwupd#factory',
            'git_url': 'https://src.opensuse.org/dimstar/fwupd.git',
            'git_owner': 'dimstar',
            'git_repo': 'fwupd',
            'target_branch': 'factory',
        }
        with tempfile.TemporaryDirectory() as td:
            scratch_dest = os.path.join(td, 'fwupd')
            with patch('helpers.fork_helper.resolve_package_devel_target', return_value=target_info):
                with patch('helpers.fork_helper.resolve_destination_directory', return_value=(scratch_dest, None)):
                    with patch('helpers.fork_helper.resolve_target_obs_project', return_value='home:dimstar:Factory'):
                        with patch('helpers.fork_helper.get_current_gitea_user', return_value='dimstar'):
                            with patch('helpers.fork_helper.provision_obs_scmsync_package', return_value=(True, 'OK')):
                                with patch('subprocess.run') as mock_run:
                                    mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')
                                    ok, msg, path = fork_helper.fork_package('fwupd', stream='factory')
                                    self.assertTrue(ok)
                                    self.assertEqual(path, scratch_dest)

    def test_fork_package_maps_slfo_stream_at_provisioning_time(self):
        """Verifies fork_package maps leap-16.1 to slfo-1.3 if pool repo uses SLFO branches."""
        target_info = {
            'package': 'openldap2_6',
            'stream': 'leap-16.1',
            'devel_project': 'openSUSE:Leap:16.1',
            'devel_package': 'openldap2_6',
            'is_git': True,
            'scmsync_url': 'https://src.opensuse.org/pool/openldap2_6.git#leap-16.1',
            'git_url': 'https://src.opensuse.org/pool/openldap2_6.git',
            'git_owner': 'pool',
            'git_repo': 'openldap2_6',
            'target_branch': 'leap-16.1',
        }
        with tempfile.TemporaryDirectory() as td:
            scratch_dest = os.path.join(td, 'openldap2_6')
            with patch('helpers.fork_helper.resolve_package_devel_target', return_value=target_info):
                with patch('helpers.fork_helper.resolve_destination_directory', return_value=(scratch_dest, None)):
                    with patch('helpers.fork_helper.resolve_target_obs_project', return_value='home:dimstar:Factory'):
                        with patch('helpers.fork_helper.get_current_gitea_user', return_value='dimstar'):
                            with patch('helpers.fork_helper.ensure_gitea_fork', return_value=(True, 'Forked')):
                                with patch('helpers.fork_helper.provision_obs_scmsync_package', return_value=(True, 'OK')) as mock_prov:
                                    def fake_run(cmd, *args, **kwargs):
                                        m = MagicMock(returncode=0, stdout='', stderr='')
                                        if 'branch' in cmd and '-r' in cmd:
                                            m.stdout = 'upstream/slfo-1.3\nupstream/slfo-main\n'
                                        return m

                                    with patch('subprocess.run', side_effect=fake_run):
                                        logs = []
                                        ok, msg, path = fork_helper.fork_package('openldap2_6', stream='leap-16.1', on_log=logs.append)
                                        self.assertTrue(ok)
                                        self.assertTrue(any("Target branch mapped: 'leap-16.1' ➔ 'slfo-1.3'" in l for l in logs))
                                        self.assertTrue(any("Active branch: 'for-slfo-1.3' (tracking 'upstream/slfo-1.3')" in l for l in logs))
                                        mock_prov.assert_called_once()
                                        scmsync_url = mock_prov.call_args[0][2]
                                        self.assertIn('#for-slfo-1.3', scmsync_url)

    def test_fork_package_obs_track_workflow(self):
        target_info = {
            'package': 'ibus',
            'stream': 'factory',
            'devel_project': 'M17N',
            'devel_package': 'ibus',
            'is_git': False,
            'scmsync_url': '',
            'git_url': None,
            'git_owner': None,
            'git_repo': None,
            'target_branch': 'factory',
        }
        with tempfile.TemporaryDirectory() as td:
            scratch_dest = os.path.join(td, 'ibus')
            os.makedirs(scratch_dest, exist_ok=True)
            with patch('helpers.fork_helper.resolve_package_devel_target', return_value=target_info):
                with patch('helpers.fork_helper.resolve_destination_directory', return_value=(scratch_dest, None)):
                    with patch('helpers.fork_helper.resolve_target_obs_project', return_value='home:dimstar:Factory'):
                        with patch('subprocess.run') as mock_run:
                            mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')
                            ok, msg, path = fork_helper.fork_package('ibus', stream='factory')
                            self.assertTrue(ok)
                            self.assertEqual(path, scratch_dest)

    def test_submit_flyby_obs_workflow(self):
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, '.osc'), exist_ok=True)
            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout='Created request 12345', stderr='')
                ok, msg = fork_helper.submit_flyby_package(td, message='Test update')
                self.assertTrue(ok)
                self.assertIn('Submit Request', msg)


    def test_prune_package_safety_check_dirty(self):
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, '.git'), exist_ok=True)
            with patch('subprocess.run') as mock_run:
                # status --porcelain reports modified files
                mock_run.return_value = MagicMock(returncode=0, stdout=" M file.spec\n", stderr='')
                ok, msg = fork_helper.prune_package(td, force=False)
                self.assertFalse(ok)
                self.assertIn('uncommitted changes detected', msg)

    def test_prune_package_clean_workflow(self):
        with tempfile.TemporaryDirectory() as td:
            pkg_dir = os.path.join(td, 'scratch_pkg')
            os.makedirs(os.path.join(pkg_dir, '.git'), exist_ok=True)

            with patch('subprocess.run') as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')
                with patch('helpers.fork_helper.resolve_package_devel_target', return_value={'devel_project': 'Base:System'}):
                    with patch('helpers.fork_helper.resolve_target_obs_project', return_value='home:dimstar:Factory'):
                        ok, msg = fork_helper.prune_package(pkg_dir, force=True)
                        self.assertTrue(ok)
                        self.assertIn('Successfully pruned', msg)
                        self.assertFalse(os.path.exists(pkg_dir))


    def test_submit_flyby_package_git_fork_resolves_upstream_pool(self):
        """Verifies submitting from a personal fork resolves upstream pool repo and avoids self-PR."""
        from unittest.mock import patch, MagicMock
        with tempfile.TemporaryDirectory() as td:
            pkg_dir = os.path.join(td, 'vlc')
            os.makedirs(os.path.join(pkg_dir, '.git'), exist_ok=True)

            def fake_run(cmd, *args, **kwargs):
                m = MagicMock(returncode=0, stdout='', stderr='')
                if 'branch' in cmd and '--show-current' in cmd:
                    m.stdout = 'leap-16.0\n'
                elif 'rev-parse' in cmd and '@{u}' in cmd:
                    m.stdout = 'origin/leap-16.0\n'
                elif 'remote' in cmd and 'upstream' in cmd:
                    m.returncode = 1
                elif 'remote' in cmd and 'origin' in cmd:
                    m.stdout = 'gitea@src.opensuse.org:dimstar/vlc.git\n'
                return m

            with patch('subprocess.run', side_effect=fake_run):
                with patch('helpers.sync_backend.get_upstream_gitea_target', return_value=('pool', 'vlc')):
                    logs = []
                    ok, msg = fork_helper.submit_flyby_package(pkg_dir, dry_run=True, on_log=logs.append)
                    self.assertTrue(ok)
                    self.assertIn('[DRY-RUN]', msg)
                    self.assertTrue(any('pool/vlc:leap-16.0' in l for l in logs))
                    self.assertTrue(any('head: dimstar:leap-16.0' in l for l in logs))

    def test_submit_dual_branch_forward_creates_pr_against_stable_branch(self):
        """Verifies submitting from unstable branch in a dual-branch workspace targets stable branch on origin."""
        from unittest.mock import patch, MagicMock
        with tempfile.TemporaryDirectory() as td:
            pkg_dir = os.path.join(td, 'sofia-sip')
            os.makedirs(os.path.join(pkg_dir, '.git'), exist_ok=True)

            def fake_run(cmd, *args, **kwargs):
                m = MagicMock(returncode=0, stdout='', stderr='')
                if 'branch' in cmd and '--show-current' in cmd:
                    m.stdout = 'next\n'
                elif 'rev-parse' in cmd and '@{u}' in cmd:
                    m.stdout = 'origin/next\n'
                elif 'remote' in cmd and 'upstream' in cmd:
                    m.returncode = 1
                elif 'remote' in cmd and 'origin' in cmd:
                    m.stdout = 'gitea@src.opensuse.org:GNOME/sofia-sip.git\n'
                return m

            mock_prof = {
                'active_workspace': 'GNOME',
                'stable_branch': 'factory',
                'unstable_branch': 'next',
                'is_scratch': False
            }

            with patch('subprocess.run', side_effect=fake_run):
                with patch('helpers.sync_backend.load_geckopit_profile_config', return_value=mock_prof):
                    with patch('helpers.sync_backend.get_pr_prefill_info', return_value=('Update sofia-sip to version 1.13.19', '- Update to version 1.13.19')):
                        logs = []
                        ok, msg = fork_helper.submit_flyby_package(pkg_dir, dry_run=True, on_log=logs.append)
                        self.assertTrue(ok)
                        self.assertIn('[DRY-RUN]', msg)
                        self.assertTrue(any('GNOME/sofia-sip:factory' in l for l in logs))
                        self.assertTrue(any('head: next' in l for l in logs))
                        self.assertTrue(any('Title: Update sofia-sip to version 1.13.19' in l for l in logs))

    def test_submit_stable_branch_routes_to_pool_submit(self):
        """Verifies submitting from stable branch in a registered workspace delegates to pool_submit."""
        from unittest.mock import patch, MagicMock
        with tempfile.TemporaryDirectory() as td:
            pkg_dir = os.path.join(td, 'sofia-sip')
            os.makedirs(os.path.join(pkg_dir, '.git'), exist_ok=True)

            def fake_run(cmd, *args, **kwargs):
                m = MagicMock(returncode=0, stdout='', stderr='')
                if 'branch' in cmd and '--show-current' in cmd:
                    m.stdout = 'factory\n'
                elif 'remote' in cmd and 'origin' in cmd:
                    m.stdout = 'gitea@src.opensuse.org:GNOME/sofia-sip.git\n'
                elif 'remote' in cmd and 'upstream' in cmd:
                    m.returncode = 1
                return m

            mock_prof = {
                'active_workspace': 'GNOME',
                'stable_branch': 'factory',
                'unstable_branch': 'next',
                'is_scratch': False
            }

            with patch('subprocess.run', side_effect=fake_run):
                with patch('helpers.sync_backend.load_geckopit_profile_config', return_value=mock_prof):
                    with patch('helpers.pool_submit.submit_package_to_pool', return_value=(True, 'Submitted to openSUSE:Factory via osc sr.')) as mock_pool_sub:
                        ok, msg = fork_helper.submit_flyby_package(pkg_dir, dry_run=False)
                        self.assertTrue(ok)
                        self.assertIn('Submitted to openSUSE:Factory', msg)
                        mock_pool_sub.assert_called_once()
                        call_kwargs = mock_pool_sub.call_args[1]
                        self.assertEqual(call_kwargs.get('package_name'), 'sofia-sip')
                        self.assertEqual(call_kwargs.get('target_project'), 'openSUSE:Factory')
                        self.assertEqual(call_kwargs.get('stable_branch'), 'factory')

    def test_submit_flyby_package_topic_branch_maps_slfo_stream_and_avoids_unbound_error(self):
        """Verifies submitting from for-leap-16.1 topic branch maps to slfo-1.3 upstream and extracts changelog."""
        from unittest.mock import patch, MagicMock
        with tempfile.TemporaryDirectory() as td:
            pkg_dir = os.path.join(td, 'openldap2_6')
            os.makedirs(os.path.join(pkg_dir, '.git'), exist_ok=True)

            def fake_run(cmd, *args, **kwargs):
                m = MagicMock(returncode=0, stdout='', stderr='')
                if 'branch' in cmd and '--show-current' in cmd:
                    m.stdout = 'for-leap-16.1\n'
                elif 'branch' in cmd and '-r' in cmd:
                    m.stdout = 'origin/HEAD -> origin/slfo-main\norigin/slfo-1.3\nupstream/slfo-1.3\nupstream/slfo-main\n'
                elif 'remote' in cmd and 'get-url' in cmd and 'origin' in cmd:
                    m.stdout = 'gitea@src.opensuse.org:dimstar/openldap2_6.git\n'
                elif 'remote' in cmd and 'get-url' in cmd and 'upstream' in cmd:
                    m.stdout = 'https://src.opensuse.org/pool/openldap2_6.git\n'
                elif 'diff' in cmd and '*.changes' in cmd:
                    m.stdout = '@@ -1,3 +1,7 @@\n+-------------------------------------------------------------------\n+Wed Oct 28 15:00:00 UTC 2026 - maint\n+\n+- Fix openldap startup script.\n'
                elif 'log' in cmd and '-1' in cmd:
                    m.stdout = 'Fix openldap startup script\n'
                return m

            with patch('subprocess.run', side_effect=fake_run):
                logs = []
                ok, msg = fork_helper.submit_flyby_package(pkg_dir, dry_run=True, on_log=logs.append)
                self.assertTrue(ok)
                self.assertIn('[DRY-RUN]', msg)
                self.assertTrue(any("Target branch mapped: 'leap-16.1' ➔ 'slfo-1.3'" in l for l in logs))
                self.assertTrue(any("pool/openldap2_6:slfo-1.3 (head: dimstar:for-leap-16.1)" in l for l in logs))
                self.assertTrue(any("Title: Fix openldap startup script" in l for l in logs))

    def test_delete_gitea_fork_repository_guards_and_deletes(self):
        """Verifies delete_gitea_fork_repository protects non-forks, guards open PRs, and deletes clean forks."""
        from unittest.mock import patch, MagicMock

        with patch('helpers.sync_backend.get_gitea_token', return_value='test-token'):
            mock_session = MagicMock()

            # 1. Non-fork guard
            mock_resp_nonfork = MagicMock(status_code=200)
            mock_resp_nonfork.json.return_value = {'fork': False, 'name': 'repo'}
            mock_session.get.return_value = mock_resp_nonfork
            with patch('helpers.sync_backend.get_http_session', return_value=mock_session):
                ok, msg = fork_helper.delete_gitea_fork_repository('user', 'repo', force=False)
                self.assertFalse(ok)
                self.assertIn('not a fork', msg)

            # 2. Open PRs guard (without force)
            mock_resp_fork = MagicMock(status_code=200)
            mock_resp_fork.json.return_value = {'fork': True, 'name': 'repo'}
            mock_resp_pulls = MagicMock(status_code=200)
            mock_resp_pulls.json.return_value = [{'number': 1}]

            def fake_get(url, *args, **kwargs):
                if 'pulls' in url:
                    return mock_resp_pulls
                return mock_resp_fork

            mock_session.get.side_effect = fake_get
            with patch('helpers.sync_backend.get_http_session', return_value=mock_session):
                ok, msg = fork_helper.delete_gitea_fork_repository('user', 'repo', force=False)
                self.assertFalse(ok)
                self.assertIn('open Pull Request', msg)

            # 3. Clean fork deletion (no open PRs)
            mock_resp_no_pulls = MagicMock(status_code=200)
            mock_resp_no_pulls.json.return_value = []
            mock_session.get.side_effect = lambda url, *args, **kwargs: mock_resp_no_pulls if 'pulls' in url else mock_resp_fork
            mock_resp_del = MagicMock(status_code=204)
            mock_session.delete.return_value = mock_resp_del
            with patch('helpers.sync_backend.get_http_session', return_value=mock_session):
                ok, msg = fork_helper.delete_gitea_fork_repository('user', 'repo', force=False)
                self.assertTrue(ok)
                self.assertIn('Deleted personal fork', msg)
                mock_session.delete.assert_called_once()

    def test_prune_package_cleans_remotes_even_if_local_dir_missing(self):
        """Verifies prune_package deletes OBS and Gitea resources even if local directory was already deleted."""
        from unittest.mock import patch, MagicMock

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')
            with patch('helpers.fork_helper.resolve_package_devel_target', return_value={'devel_project': 'openSUSE:Factory'}):
                with patch('helpers.fork_helper.resolve_target_obs_project', return_value='home:dimstar:Factory'):
                    with patch('helpers.fork_helper.delete_gitea_fork_repository', return_value=(True, 'Deleted fork')) as mock_del_fork:
                        ok, msg = fork_helper.prune_package('nonexistent_scratch_pkg', force=True)
                        self.assertTrue(ok)
                        self.assertIn('Successfully pruned', msg)
                        mock_del_fork.assert_called_once()

if __name__ == '__main__':

    unittest.main()
