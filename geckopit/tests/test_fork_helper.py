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


if __name__ == '__main__':
    unittest.main()
