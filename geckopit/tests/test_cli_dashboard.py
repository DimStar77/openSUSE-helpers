#!/usr/bin/env python3
import os
import sys
import io
import unittest
from unittest.mock import patch
import importlib.util
from importlib.machinery import SourceFileLoader

import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
GECKOPIT_DIR = os.path.dirname(TESTS_DIR)
HELPERS_DIR = os.path.join(GECKOPIT_DIR, "helpers")

for p in [GECKOPIT_DIR, HELPERS_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

class TestSinglePackageDashboardFormatting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        import importlib.util
        from importlib.machinery import SourceFileLoader
        cli_path = os.path.join(GECKOPIT_DIR, "geckopit-cli")
        loader = SourceFileLoader("geckopit_cli", cli_path)
        spec = importlib.util.spec_from_file_location("geckopit_cli", cli_path, loader=loader)
        cls.cli = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.cli)

    def test_dashboard_single_forward_pr_formatting(self):
        import io
        from unittest.mock import patch

        data = {
            'repo_name': 'testpkg',
            'pkg_dir': '/path/to/testpkg',
            'active_branch': 'next',
            'stable_branch': 'factory',
            'unstable_branch': 'next',
            'worktree': {'ahead': 0, 'behind': 0, 'dirty': False, 'untracked': False},
            'sync': {'pool_ahead': 2, 'pool_behind': 0, 'pool_status': 'In Sync', 'next_ahead': 1, 'next_behind': 0, 'next_status': 'Ahead (ok)'},
            'version': {'factory_ver': '51.0.8', 'upstream_stable': '51.0.9', 'next_ver': '51.0.9', 'upstream_latest': '51.0.9', 'needs_update': False},
            'pr': {
                'has_pr': True,
                'has_forward_pr': True,
                'count': 1,
                'number': 2,
                'url': 'https://src.opensuse.org/GNOME/testpkg/pulls/2',
                'prs': [
                    {
                        'number': 2,
                        'title': 'Update testpkg to version 51.0.9',
                        'author': 'dimstar',
                        'base': 'factory',
                        'head': 'next',
                        'url': 'https://src.opensuse.org/GNOME/testpkg/pulls/2',
                        'is_forward': True
                    }
                ]
            }
        }

        buf = io.StringIO()
        with patch('sys.stdout', buf):
            self.cli.render_single_package_dashboard(data)
        out = buf.getvalue()

        # 1. Verify Branch Sync states (PR #2 pending) rather than (Ready for PR)
        self.assertIn("Ahead of factory by 1 commit (PR #2 pending)", out)
        self.assertNotIn("Ready for PR", out)

        # 2. Verify singular 'commit'
        self.assertIn("1 commit", out)
        self.assertNotIn("1 commits", out)

        # 3. Verify Active PR shows PR #2 on line 1 and URL on line 2 with no repeated sub-bullet
        self.assertIn('Active PR:     [32m🟢 PR #2[0m by @dimstar: "Update testpkg to version 51.0.9" (➔ factory) [32m[Forward][0m', out)
        self.assertIn('                    https://src.opensuse.org/GNOME/testpkg/pulls/2', out)
        self.assertNotIn('• PR #2', out)

    def test_dashboard_multi_pr_formatting(self):
        import io
        from unittest.mock import patch

        data = {
            'repo_name': 'testpkg',
            'pkg_dir': '/path/to/testpkg',
            'active_branch': 'next',
            'stable_branch': 'factory',
            'unstable_branch': 'next',
            'worktree': {'ahead': 0, 'behind': 0, 'dirty': False, 'untracked': False},
            'sync': {'pool_ahead': 0, 'pool_behind': 0, 'pool_status': 'In Sync', 'next_ahead': 1, 'next_behind': 0, 'next_status': 'Ahead (ok)'},
            'version': {'factory_ver': '51.0.9', 'upstream_stable': '51.0.9', 'next_ver': '51.0.9', 'upstream_latest': '51.0.9', 'needs_update': False},
            'pr': {
                'has_pr': True,
                'has_forward_pr': True,
                'count': 2,
                'number': 2,
                'url': 'https://src.opensuse.org/GNOME/testpkg/pulls/2',
                'prs': [
                    {
                        'number': 2,
                        'title': 'Forward next to factory',
                        'author': 'dimstar',
                        'base': 'factory',
                        'head': 'next',
                        'url': 'https://src.opensuse.org/GNOME/testpkg/pulls/2',
                        'is_forward': True
                    },
                    {
                        'number': 5,
                        'title': 'Fix riscv64 build',
                        'author': 'schwab',
                        'base': 'factory',
                        'head': 'riscv',
                        'url': 'https://src.opensuse.org/GNOME/testpkg/pulls/5',
                        'is_forward': False
                    }
                ]
            }
        }

        buf = io.StringIO()
        with patch('sys.stdout', buf):
            self.cli.render_single_package_dashboard(data)
        out = buf.getvalue()

        self.assertIn("Active PR:     [32m🟢 2 Active PRs:[0m", out)
        self.assertIn('• PR #2 by @dimstar: "Forward next to factory" (➔ factory) [32m[Forward][0m', out)
        self.assertIn('                   https://src.opensuse.org/GNOME/testpkg/pulls/2', out)
        self.assertIn('• PR #5 by @schwab: "Fix riscv64 build" (➔ factory)', out)
        self.assertIn('                   https://src.opensuse.org/GNOME/testpkg/pulls/5', out)

    def test_dashboard_no_pr_shows_ready_for_pr(self):
        import io
        from unittest.mock import patch

        data = {
            'repo_name': 'testpkg',
            'pkg_dir': '/path/to/testpkg',
            'active_branch': 'next',
            'stable_branch': 'factory',
            'unstable_branch': 'next',
            'worktree': {'ahead': 0, 'behind': 0, 'dirty': False, 'untracked': False},
            'sync': {'pool_ahead': 0, 'pool_behind': 0, 'pool_status': 'In Sync', 'next_ahead': 3, 'next_behind': 0, 'next_status': 'Ahead (ok)'},
            'version': {'factory_ver': '51.0.8', 'upstream_stable': '51.0.9', 'next_ver': '51.0.9', 'upstream_latest': '51.0.9', 'needs_update': False},
            'pr': {'has_pr': False, 'has_forward_pr': False, 'count': 0, 'prs': []}
        }

        buf = io.StringIO()
        with patch('sys.stdout', buf):
            self.cli.render_single_package_dashboard(data)
        out = buf.getvalue()

        self.assertIn("Ahead of factory by 3 commits (Ready for PR)", out)
        self.assertIn("Create Gitea Pull Request from next to factory:", out)
        self.assertIn("geckopit-cli --submit", out)
        self.assertNotIn("Active PR:", out)



    def test_dashboard_in_sync_and_pool_ahead_action(self):
        import io
        from unittest.mock import patch

        data = {
            'repo_name': 'testpkg',
            'pkg_dir': '/path/to/pkg',
            'active_branch': 'next',
            'stable_branch': 'factory',
            'unstable_branch': 'next',
            'worktree': {'ahead': 0, 'behind': 0, 'dirty': False, 'untracked': False},
            'sync': {'pool_ahead': 3, 'pool_behind': 0, 'pool_status': 'In Sync', 'next_ahead': 0, 'next_behind': 0, 'next_status': 'In Sync'},
            'version': {'factory_ver': '51.0.9', 'upstream_stable': '51.0.9', 'next_ver': '51.0.9', 'upstream_latest': '51.0.9', 'needs_update': False},
            'pr': {'has_pr': False, 'has_forward_pr': False, 'count': 0, 'prs': []}
        }

        # 1. Test unsubmitted package
        buf = io.StringIO()
        with patch('pool_submit.get_package_pool_submission_status', return_value={'status': 'unsubmitted'}):
            with patch('sys.stdout', buf):
                self.cli.render_single_package_dashboard(data)
        out = buf.getvalue()

        self.assertIn("Branch Sync:   [32m✅ In Sync[0m", out)
        self.assertNotIn("(no unforwarded commits)", out)
        self.assertNotIn("Active PR:", out)
        self.assertIn("Ahead of central pool by 3 commits (Submit needed)", out)
        self.assertIn("📤 Submit changes to central pool (openSUSE:Factory)", out)

        # 2. Test pending SR
        buf_pending = io.StringIO()
        with patch('pool_submit.get_package_pool_submission_status', return_value={
            'status': 'pending',
            'request': {'id': '1386637', 'state': 'review', 'source_project': 'GNOME:Factory'},
            'request_id': '1386637',
            'state': 'review'
        }):
            with patch('sys.stdout', buf_pending):
                self.cli.render_single_package_dashboard(data)
        out_pending = buf_pending.getvalue()
        self.assertIn("⏳ Pending SR #1386637 (review from GNOME:Factory)", out_pending)
        self.assertIn("No action needed (SR #1386637 in review in openSUSE:Factory)", out_pending)
        self.assertNotIn("Submit changes to central pool", out_pending)

        # 3. Test accepted SR awaiting sync
        buf_acc = io.StringIO()
        with patch('pool_submit.get_package_pool_submission_status', return_value={
            'status': 'accepted_pending_sync',
            'request': {'id': '1384945', 'state': 'accepted'},
            'request_id': '1384945'
        }):
            with patch('sys.stdout', buf_acc):
                self.cli.render_single_package_dashboard(data)
        out_acc = buf_acc.getvalue()
        self.assertIn("⏳ SR #1384945 accepted (awaiting central pool sync)", out_acc)
        self.assertIn("No action needed (SR #1384945 accepted, awaiting sync to openSUSE:Factory)", out_acc)
        self.assertNotIn("Submit changes to central pool", out_acc)

    def test_version_check_single_stream_table_formatting(self):
        import io
        from unittest.mock import patch

        fake_results = {
            "gstreamer-docs": {
                "status": "success",
                "factory_ver": "1.28.8",
                "next_ver": "—",
                "upstream_stable": "1.29.2",
                "upstream_latest": "1.29.2",
                "needs_update": True,
                "stable_needs_update": True,
                "unstable_needs_update": False,
            }
        }

        buf = io.StringIO()
        with patch.object(self.cli.sb, 'load_geckopit_profile_config', return_value={'unstable_path': None}):
            with patch.object(self.cli.sb, 'check_repo_version', side_effect=lambda r, *args, **kwargs: (r, fake_results[r])):
                with patch('sys.stdout', buf):
                    self.cli.run_version_check(["gstreamer-docs"])
        out = buf.getvalue()

        self.assertIn("Local Version", out)
        self.assertIn("Upstream Version", out)
        self.assertNotIn("Local (unstable)", out)
        self.assertNotIn("Upstream (unstable)", out)
        self.assertIn("gstreamer-docs", out)
        self.assertIn("1.28.8", out)
        self.assertIn("1.29.2", out)

    def test_dashboard_pure_metadata_package_formatting(self):
        """Tests single-package dashboard formatting for a spec-less metadata package."""
        import io
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmpdir:
            pkg_dir = os.path.join(tmpdir, '000package-groups')
            os.makedirs(os.path.join(pkg_dir, '.osc'))

            data = {
                'repo_name': '000package-groups',
                'pkg_dir': pkg_dir,
                'active_branch': 'factory',
                'stable_branch': 'factory',
                'unstable_branch': None,
                'worktree': {'ahead': 0, 'behind': 0, 'dirty': False, 'untracked': False, 'is_osc': True},
                'sync': {'pool_ahead': 0, 'pool_behind': 0, 'pool_status': 'OSC Managed', 'is_osc': True, 'needs_action': False},
                'version': {'status': 'success', 'is_metadata': True, 'factory_ver': 'N/A', 'upstream_stable': 'N/A', 'needs_update': False},
                'pr': {'has_pr': False, 'has_forward_pr': False, 'count': 0, 'prs': []}
            }

            buf = io.StringIO()
            with patch('sys.stdout', buf):
                self.cli.render_single_package_dashboard(data)
            out = buf.getvalue()

            self.assertIn("Pure Metadata Package (No .spec file)", out)
            self.assertNotIn("https://release-monitoring.org/project/", out)
            self.assertIn("Metadata Package (No .spec / Upstream tracking N/A)", out)
            self.assertIn("OSC checkout", out)

    def test_dashboard_osc_package_with_pending_devel_sr(self):
        """Tests rendering dashboard for an OSC package with a pending SR targeting a devel project."""
        import io
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmpdir:
            pkg_dir = os.path.join(tmpdir, 'ibus')
            os.makedirs(os.path.join(pkg_dir, '.osc'))

            sr_data = {
                'status': 'pending',
                'request_id': '1393059',
                'state': 'new',
                'source_project': 'home:dimstar:Factory',
                'target_project': 'M17N',
                'request': {'id': '1393059', 'state': 'new', 'source_project': 'home:dimstar:Factory', 'target_project': 'M17N'}
            }
            data = {
                'repo_name': 'ibus',
                'pkg_dir': pkg_dir,
                'active_branch': 'factory',
                'stable_branch': 'factory',
                'unstable_branch': None,
                'worktree': {'ahead': 0, 'behind': 0, 'dirty': False, 'untracked': False, 'is_osc': True},
                'sync': {'pool_ahead': 0, 'pool_behind': 0, 'pool_status': 'OSC Managed', 'osc_project': 'home:dimstar:Factory', 'is_osc': True, 'needs_action': False, 'pool_sr': sr_data},
                'version': {'status': 'success', 'is_metadata': False, 'factory_ver': '1.5.35', 'upstream_stable': '1.5.35', 'needs_update': False},
                'pr': {'has_pr': False, 'has_forward_pr': False, 'count': 0, 'prs': []}
            }

            buf = io.StringIO()
            with patch('sys.stdout', buf):
                self.cli.render_single_package_dashboard(data)
            out = buf.getvalue()

            self.assertIn("Pending SR #1393059", out)
            self.assertIn("M17N", out)
            self.assertIn("No action needed (SR #1393059 in new in M17N)", out)

    def test_dashboard_stream_branch_with_pending_pr(self):
        """Verifies that a stream branch with pending PR displays [PR #N pending] and avoids redundant PR actions."""
        import io
        from unittest.mock import patch

        pr_info = {
            'number': 4,
            'title': 'Update vlc for leap-16.0',
            'url': 'https://src.opensuse.org/pool/vlc/pulls/4',
            'author': 'dimstar',
            'base': 'leap-16.0',
            'head': 'leap-16.0',
            'target_repo': 'pool/vlc'
        }
        data = {
            'repo_name': 'vlc',
            'pkg_dir': '/path/to/vlc',
            'active_branch': 'leap-16.0',
            'stable_branch': 'factory',
            'unstable_branch': None,
            'worktree': {'ahead': 0, 'behind': 0, 'dirty': False, 'untracked': False, 'head': 'leap-16.0'},
            'sync': {'pool_ahead': 0, 'pool_behind': 0, 'pool_status': 'In Sync', 'is_osc': False, 'needs_action': False},
            'version': {'status': 'success', 'is_metadata': False, 'factory_ver': '3.0.24', 'upstream_stable': '3.0.24', 'needs_update': False},
            'pr': {'has_pr': True, 'has_forward_pr': False, 'count': 1, 'prs': [pr_info]}
        }

        with patch('pool_submit.check_pool_stream_sync', return_value={'exists': True, 'ahead': 410, 'behind': 0}):
            buf = io.StringIO()
            with patch('sys.stdout', buf):
                self.cli.render_single_package_dashboard(data)
            out = buf.getvalue()

            self.assertIn("Ahead of central pool (leap-16.0) by 410 commits [PR #4 pending]", out)
            self.assertNotIn("(PR needed)", out)
            self.assertNotIn("Create Gitea Pull Request to central pool", out)
            self.assertIn("No action needed (PR #4 in-flight to pool/vlc)", out)

    def test_forward_check_superproject_staging_detection(self):
        """Verifies run_forward_check detects superproject _ObsPrj PRs and differentiates staging status."""
        import io
        from unittest.mock import patch

        fake_sync_results = {
            'glycin': {'status': 'success', 'next_status': 'Ahead', 'next_ahead': 1, 'next_behind': 0},
            'flatpak': {'status': 'success', 'next_status': 'Ahead', 'next_ahead': 5, 'next_behind': 0},
        }

        forwarded_map = {
            ('glycin', 11): {'obsprj_pr': 1181, 'obsprj_target': 'factory', 'obsprj_title': 'Update glycin'},
        }

        with patch('helpers.sync_backend.get_gitea_owner_and_repo', return_value=('GNOME', '_ObsPrj')), \
             patch('helpers.pr_helper.get_obsprj_forwarded_prs', return_value=(forwarded_map, [])), \
             patch('helpers.sync_backend.check_repo_sync', side_effect=lambda r: (r, fake_sync_results[r])):

            buf = io.StringIO()
            with patch('sys.stdout', buf):
                self.cli.run_forward_check(['glycin', 'flatpak'])
            out = buf.getvalue()

            self.assertIn("Ready to Forward: 1 | In Staging: 1", out)
            self.assertIn("In Staging (PR #1181 on GNOME/_ObsPrj)", out)
            self.assertIn("Ready (Clean Forward)", out)

if __name__ == '__main__':




    unittest.main()
