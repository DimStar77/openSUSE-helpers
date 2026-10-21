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
        self.assertIn("⏳ Wait for pending SR #1386637 (review) to merge in openSUSE:Factory", out_pending)
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
        self.assertIn("⏳ Wait for accepted SR #1384945 to sync into central pool", out_acc)
        self.assertNotIn("Submit changes to central pool", out_acc)

if __name__ == '__main__':
    unittest.main()
