#!/usr/bin/env python3
import os
import sys
import unittest
import tempfile
import subprocess
from unittest.mock import patch

import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
GECKOPIT_DIR = os.path.dirname(TESTS_DIR)
HELPERS_DIR = os.path.join(GECKOPIT_DIR, "helpers")

for p in [GECKOPIT_DIR, HELPERS_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

from pr_helper import check_spec_collisions, get_pr_commits, merge_pull_request, is_changes_chronologically_sorted, fix_changes_chronological_order, parse_changelog_date, redate_entry_header

class TestPrHelper(unittest.TestCase):

    def test_check_spec_collisions_detects_duplicate_patch(self):
        """Detects when two patches share the same Patch<N>: number."""
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".spec", delete=False) as f:
            f.write("""Name: testpkg
Version: 1.0
Patch0: first.patch
Patch2: webkitgtk-skia-s390x.patch
Patch2: riscv-platformenable.patch
""")
            spec_path = f.name

        try:
            cols = check_spec_collisions(spec_path)
            self.assertEqual(len(cols), 1)
            self.assertIn("Duplicate Patch2:", cols[0])
            self.assertIn("riscv-platformenable.patch", cols[0])
            self.assertIn("webkitgtk-skia-s390x.patch", cols[0])
        finally:
            os.unlink(spec_path)

    def test_check_spec_collisions_clean_spec(self):
        """Returns empty list when spec patch and source numbers are unique."""
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".spec", delete=False) as f:
            f.write("""Name: testpkg
Version: 1.0
Source0: testpkg-1.0.tar.xz
Patch0: first.patch
Patch1: second.patch
Patch2: third.patch
""")
            spec_path = f.name

        try:
            cols = check_spec_collisions(spec_path)
            self.assertEqual(len(cols), 0)
        finally:
            os.unlink(spec_path)

    def test_get_pr_commits_chronological_order(self):
        """Tests that get_pr_commits correctly identifies multi-commit PR chains in order."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "init", "-b", "main"], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=tmpdir, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmpdir, check=True)

            # Initial commit
            with open(os.path.join(tmpdir, "file.txt"), "w") as f:
                f.write("base")
            subprocess.run(["git", "add", "file.txt"], cwd=tmpdir, check=True)
            subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=tmpdir, check=True)

            # Feature branch with 2 commits
            subprocess.run(["git", "checkout", "-b", "feature"], cwd=tmpdir, check=True, capture_output=True)
            with open(os.path.join(tmpdir, "f1.txt"), "w") as f:
                f.write("1")
            subprocess.run(["git", "add", "f1.txt"], cwd=tmpdir, check=True)
            subprocess.run(["git", "commit", "-m", "Commit 1"], cwd=tmpdir, check=True)
            c1 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmpdir, text=True).strip()

            with open(os.path.join(tmpdir, "f2.txt"), "w") as f:
                f.write("2")
            subprocess.run(["git", "add", "f2.txt"], cwd=tmpdir, check=True)
            subprocess.run(["git", "commit", "-m", "Commit 2"], cwd=tmpdir, check=True)
            c2 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmpdir, text=True).strip()

            # Run get_pr_commits
            commits = get_pr_commits(tmpdir, c2)
            self.assertEqual(commits, [c1, c2])

    def test_merge_pull_request_squashes_multiple_commits(self):
        """Tests that merge_pull_request with squash=True collapses multiple commits into 1 atomic commit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "init", "-b", "main"], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=tmpdir, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmpdir, check=True)

            # Initial commit
            with open(os.path.join(tmpdir, "file.txt"), "w") as f:
                f.write("base")
            subprocess.run(["git", "add", "file.txt"], cwd=tmpdir, check=True)
            subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=tmpdir, check=True)

            # Feature branch with 2 commits
            subprocess.run(["git", "checkout", "-b", "feature"], cwd=tmpdir, check=True, capture_output=True)
            with open(os.path.join(tmpdir, "f1.txt"), "w") as f:
                f.write("1")
            subprocess.run(["git", "add", "f1.txt"], cwd=tmpdir, check=True)
            subprocess.run(["git", "commit", "-m", "Fix part 1"], cwd=tmpdir, check=True)
            c1 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmpdir, text=True).strip()

            with open(os.path.join(tmpdir, "f2.txt"), "w") as f:
                f.write("2")
            subprocess.run(["git", "add", "f2.txt"], cwd=tmpdir, check=True)
            subprocess.run(["git", "commit", "-m", "Fix part 2"], cwd=tmpdir, check=True)
            c2 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmpdir, text=True).strip()

            # Switch back to main
            subprocess.run(["git", "checkout", "main"], cwd=tmpdir, check=True, capture_output=True)

            mock_info = {
                "pr_id": "99",
                "commit_sha": c2,
                "author": "Contributor <contrib@example.com>",
                "date": "Sat Oct 3 12:00:00 2026",
                "subject": "Add great features",
                "full_msg": "Add great features",
                "files": ["f1.txt", "f2.txt"],
                "commits": [c1, c2]
            }

            with patch("pr_helper.get_pr_info", return_value=mock_info):
                success = merge_pull_request(tmpdir, "99", auto_commit=True, squash=True)
                self.assertTrue(success)

            # Verify that main only has 2 commits total: Initial commit and the 1 squashed commit
            log_commits = subprocess.check_output(["git", "rev-list", "HEAD"], cwd=tmpdir, text=True).splitlines()
            self.assertEqual(len(log_commits), 2)

            # Verify commit author and message
            last_author = subprocess.check_output(["git", "log", "-1", "--pretty=format:%an <%ae>"], cwd=tmpdir, text=True).strip()
            self.assertEqual(last_author, "Contributor <contrib@example.com>")

            last_msg = subprocess.check_output(["git", "log", "-1", "--pretty=format:%B"], cwd=tmpdir, text=True).strip()
            self.assertIn("Add great features (PR #99)", last_msg)
            self.assertIn("Squashed commits from PR #99:", last_msg)
            self.assertIn("- Fix part 1", last_msg)
            self.assertIn("- Fix part 2", last_msg)

            # Both files exist
            self.assertTrue(os.path.isfile(os.path.join(tmpdir, "f1.txt")))
            self.assertTrue(os.path.isfile(os.path.join(tmpdir, "f2.txt")))


    def test_merge_pull_request_fast_forward_preserves_commit_and_signatures(self):
        """Tests that when HEAD is an ancestor of PR, merge-pr fast-forwards preserving commit hash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "init", "-b", "factory"], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=tmpdir, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmpdir, check=True)

            with open(os.path.join(tmpdir, "pkg.spec"), "w") as f:
                f.write("Name: pkg\nVersion: 1.0\n")
            subprocess.run(["git", "add", "."], cwd=tmpdir, check=True)
            subprocess.run(["git", "commit", "-m", "Initial commit in factory"], cwd=tmpdir, check=True)
            c1 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmpdir, text=True).strip()

            subprocess.run(["git", "checkout", "-b", "next"], cwd=tmpdir, check=True, capture_output=True)
            with open(os.path.join(tmpdir, "pkg.spec"), "w") as f:
                f.write("Name: pkg\nVersion: 1.1\n")
            subprocess.run(["git", "add", "."], cwd=tmpdir, check=True)
            subprocess.run(["git", "commit", "-m", "Update to 1.1 in next"], cwd=tmpdir, check=True)
            c2 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmpdir, text=True).strip()

            # Switch back to factory
            subprocess.run(["git", "checkout", "factory"], cwd=tmpdir, check=True, capture_output=True)

            mock_info = {
                "pr_id": "42",
                "commit_sha": c2,
                "author": "Next Maintainer <next@example.com>",
                "date": "Fri Oct 2 12:00:00 2026",
                "subject": "Update to 1.1 in next",
                "full_msg": "Update to 1.1 in next",
                "files": ["pkg.spec"],
                "commits": [c2]
            }

            with patch("pr_helper.get_pr_info", return_value=mock_info):
                success = merge_pull_request(tmpdir, "42", auto_commit=True, squash=False)
                self.assertTrue(success)

            # Factory HEAD must equal c2 exactly (no new commit object created!)
            current_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmpdir, text=True).strip()
            self.assertEqual(current_head, c2)

            # Total commits on factory must be exactly 2
            log_commits = subprocess.check_output(["git", "rev-list", "HEAD"], cwd=tmpdir, text=True).splitlines()
            self.assertEqual(len(log_commits), 2)
            self.assertEqual(log_commits[0], c2)
            self.assertEqual(log_commits[1], c1)

    def test_merge_pull_request_fast_forward_no_commit_stages_changes(self):
        """Tests that merge_pull_request with auto_commit=False stages changes without advancing HEAD."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "init", "-b", "factory"], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=tmpdir, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmpdir, check=True)

            with open(os.path.join(tmpdir, "pkg.spec"), "w") as f:
                f.write("Name: pkg\nVersion: 1.0\n")
            subprocess.run(["git", "add", "."], cwd=tmpdir, check=True)
            subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=tmpdir, check=True)
            c1 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmpdir, text=True).strip()

            subprocess.run(["git", "checkout", "-b", "feature"], cwd=tmpdir, check=True, capture_output=True)
            with open(os.path.join(tmpdir, "pkg.spec"), "w") as f:
                f.write("Name: pkg\nVersion: 1.1\n")
            subprocess.run(["git", "add", "."], cwd=tmpdir, check=True)
            subprocess.run(["git", "commit", "-m", "Bump to 1.1"], cwd=tmpdir, check=True)
            c2 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmpdir, text=True).strip()

            subprocess.run(["git", "checkout", "factory"], cwd=tmpdir, check=True, capture_output=True)

            mock_info = {
                "pr_id": "43",
                "commit_sha": c2,
                "author": "Contributor <contrib@example.com>",
                "date": "Fri Oct 2 12:00:00 2026",
                "subject": "Bump to 1.1",
                "full_msg": "Bump to 1.1",
                "files": ["pkg.spec"],
                "commits": [c2]
            }

            with patch("pr_helper.get_pr_info", return_value=mock_info):
                success = merge_pull_request(tmpdir, "43", auto_commit=False, squash=False)
                self.assertTrue(success)

            # Factory HEAD must still be c1
            current_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmpdir, text=True).strip()
            self.assertEqual(current_head, c1)

            # Changes must be staged in the index
            status = subprocess.check_output(["git", "status", "--porcelain"], cwd=tmpdir, text=True).strip()
            self.assertIn("M  pkg.spec", status)

    def test_merge_pull_request_diverged_clean_merge_preserves_pr_commit(self):
        """Tests that non-fast-forward clean merges create a merge commit preserving PR commit as Parent 2."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "init", "-b", "factory"], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=tmpdir, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmpdir, check=True)

            with open(os.path.join(tmpdir, "f1.txt"), "w") as f:
                f.write("base")
            subprocess.run(["git", "add", "."], cwd=tmpdir, check=True)
            subprocess.run(["git", "commit", "-m", "C1"], cwd=tmpdir, check=True)
            c1 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmpdir, text=True).strip()

            # Feature branch
            subprocess.run(["git", "checkout", "-b", "feature"], cwd=tmpdir, check=True, capture_output=True)
            with open(os.path.join(tmpdir, "f2.txt"), "w") as f:
                f.write("feature file")
            subprocess.run(["git", "add", "."], cwd=tmpdir, check=True)
            subprocess.run(["git", "commit", "-m", "Feature commit"], cwd=tmpdir, check=True)
            c_feature = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmpdir, text=True).strip()

            # Switch to factory and make independent commit
            subprocess.run(["git", "checkout", "factory"], cwd=tmpdir, check=True, capture_output=True)
            with open(os.path.join(tmpdir, "f3.txt"), "w") as f:
                f.write("factory file")
            subprocess.run(["git", "add", "."], cwd=tmpdir, check=True)
            subprocess.run(["git", "commit", "-m", "Factory commit"], cwd=tmpdir, check=True)
            c_factory = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmpdir, text=True).strip()

            mock_info = {
                "pr_id": "55",
                "commit_sha": c_feature,
                "author": "Contributor <contrib@example.com>",
                "date": "Fri Oct 2 12:00:00 2026",
                "subject": "Feature commit",
                "full_msg": "Feature commit",
                "files": ["f2.txt"],
                "commits": [c_feature]
            }

            with patch("pr_helper.get_pr_info", return_value=mock_info):
                success = merge_pull_request(tmpdir, "55", auto_commit=True, squash=False)
                self.assertTrue(success)

            # Verify HEAD has 2 parents: Parent 1 is c_factory, Parent 2 is c_feature
            parents = subprocess.check_output(["git", "log", "-1", "--pretty=format:%P"], cwd=tmpdir, text=True).strip().split()
            self.assertEqual(len(parents), 2)
            self.assertEqual(parents[0], c_factory)
            self.assertEqual(parents[1], c_feature)

    def test_is_changes_chronologically_sorted_and_inserted(self):
        """Tests chronological verification, including entries inserted in the middle."""
        # Sorted
        sorted_changes = """-------------------------------------------------------------------
Tue Oct  6 12:00:00 UTC 2026 - A <a@example.com>

- Top

-------------------------------------------------------------------
Sun Oct  4 12:00:00 UTC 2026 - B <b@example.com>

- Middle

-------------------------------------------------------------------
Fri Oct  2 12:00:00 UTC 2026 - C <c@example.com>

- Bottom
"""
        self.assertTrue(is_changes_chronologically_sorted(sorted_changes))

        # Unsorted
        unsorted_changes = """-------------------------------------------------------------------
Fri Oct  2 12:00:00 UTC 2026 - A <a@example.com>

- Out of order older entry

-------------------------------------------------------------------
Tue Oct  6 12:00:00 UTC 2026 - B <b@example.com>

- Newer entry below
"""
        self.assertFalse(is_changes_chronologically_sorted(unsorted_changes))

    def test_fix_changes_chronological_order(self):
        """Tests that fix_changes_chronological_order redates out-of-order entries."""
        unsorted = """-------------------------------------------------------------------
Fri Oct  2 12:00:00 UTC 2026 - A <a@example.com>

- Out of order older entry

-------------------------------------------------------------------
Tue Oct  6 12:00:00 UTC 2026 - B <b@example.com>

- Newer entry below
"""
        fixed = fix_changes_chronological_order(unsorted)
        self.assertTrue(is_changes_chronologically_sorted(fixed))
        self.assertIn("Newer entry below", fixed)


    def test_parse_and_redate_changelog_date_locale_independence(self):
        """Tests that parse_changelog_date and redate_entry_header remain correct regardless of system locale."""
        import locale
        orig_locale = locale.getlocale(locale.LC_TIME)
        try:
            for loc in ['de_CH.UTF-8', 'de_DE.UTF-8', 'fr_FR.UTF-8', 'C']:
                try:
                    locale.setlocale(locale.LC_ALL, loc)
                    break
                except Exception:
                    continue

            header_utc = "Fri Oct  2 12:00:00 UTC 2026 - Contributor <contrib@example.com>"
            dt = parse_changelog_date(header_utc)
            self.assertIsNotNone(dt)
            self.assertEqual(dt.year, 2026)
            self.assertEqual(dt.month, 10)
            self.assertEqual(dt.day, 2)
            self.assertEqual(dt.hour, 12)

            header_cest = "Wed Sep 30 18:51:33 CEST 2026 - Maintainer <maint@example.com>"
            dt_cest = parse_changelog_date(header_cest)
            self.assertIsNotNone(dt_cest)
            self.assertEqual(dt_cest.year, 2026)
            self.assertEqual(dt_cest.month, 9)
            self.assertEqual(dt_cest.day, 30)

            redated = redate_entry_header(header_utc, 1790942400)
            self.assertIn("Oct", redated)
            self.assertIn("Contributor <contrib@example.com>", redated)
        finally:
            try:
                locale.setlocale(locale.LC_ALL, orig_locale)
            except Exception:
                pass

    def test_resolve_pr_target_auto_detection_and_collision(self):
        """Verifies resolve_pr_target correctly auto-detects, handles prefixes, and halts on ambiguity."""
        from unittest.mock import patch, MagicMock
        from helpers.pr_helper import resolve_pr_target

        with tempfile.TemporaryDirectory() as td:
            pkg_dir = os.path.join(td, 'vlc')
            os.makedirs(os.path.join(pkg_dir, '.git'), exist_ok=True)

            with patch('helpers.pr_helper.sb.get_gitea_owner_and_repo', return_value=('dimstar', 'vlc')), \
                 patch('helpers.pr_helper.sb.get_upstream_gitea_target', return_value=('pool', 'vlc')):

                # 1. Explicit pool#4
                cid, src, lbl = resolve_pr_target(pkg_dir, 'pool#4')
                self.assertEqual(cid, '4')
                self.assertIn('pool/vlc.git', src)
                self.assertEqual(lbl, 'pool/vlc')

                # 2. Explicit origin#3
                cid, src, lbl = resolve_pr_target(pkg_dir, 'origin#3')
                self.assertEqual(cid, '3')
                self.assertEqual(src, 'origin')
                self.assertEqual(lbl, 'dimstar/vlc')

                # 3. Auto-detection: PR only on pool
                mock_session = MagicMock()
                def fake_get(url, *args, **kwargs):
                    m = MagicMock()
                    if 'pool/vlc/pulls/4' in url:
                        m.status_code = 200
                    else:
                        m.status_code = 404
                    return m

                mock_session.get.side_effect = fake_get
                with patch('helpers.pr_helper.sb.get_http_session', return_value=mock_session):
                    cid, src, lbl = resolve_pr_target(pkg_dir, '4')
                    self.assertEqual(cid, '4')
                    self.assertEqual(lbl, 'pool/vlc')

                # 4. Ambiguity: PR on both
                mock_session2 = MagicMock()
                mock_resp = MagicMock(status_code=200)
                mock_session2.get.return_value = mock_resp
                with patch('helpers.pr_helper.sb.get_http_session', return_value=mock_session2):
                    cid, src, lbl = resolve_pr_target(pkg_dir, '99')
                    self.assertIsNone(cid)
                    self.assertIsNone(src)

    def test_list_pull_requests_dual_repo_grouping(self):
        """Verifies list_pull_requests queries and groups PRs from both origin and upstream parent."""
        import io
        from unittest.mock import patch, MagicMock
        from helpers.pr_helper import list_pull_requests

        with tempfile.TemporaryDirectory() as td:
            pkg_dir = os.path.join(td, 'vlc')
            os.makedirs(os.path.join(pkg_dir, '.git'), exist_ok=True)

            with patch('helpers.pr_helper.sb.get_gitea_owner_and_repo', return_value=('dimstar', 'vlc')), \
                 patch('helpers.pr_helper.sb.get_upstream_gitea_target', return_value=('pool', 'vlc')), \
                 patch('helpers.pr_helper.get_obsprj_forwarded_prs', return_value=({}, [])):

                mock_session = MagicMock()
                def fake_get(url, *args, **kwargs):
                    m = MagicMock()
                    m.status_code = 200
                    if 'dimstar/vlc/pulls' in url:
                        m.json.return_value = [{
                            'number': 1, 'title': 'Local PR', 'user': {'username': 'alice'},
                            'base': {'ref': 'factory'}, 'html_url': 'https://src.opensuse.org/dimstar/vlc/pulls/1'
                        }]
                    elif 'pool/vlc/pulls' in url:
                        m.json.return_value = [{
                            'number': 4, 'title': 'Pool PR', 'user': {'username': 'bob'},
                            'base': {'ref': 'leap-16.0'}, 'html_url': 'https://src.opensuse.org/pool/vlc/pulls/4'
                        }]
                    return m

                mock_session.get.side_effect = fake_get
                with patch('helpers.pr_helper.sb.get_http_session', return_value=mock_session):
                    buf = io.StringIO()
                    with patch('sys.stdout', buf):
                        ok = list_pull_requests(target_pkg_dir=pkg_dir)
                    self.assertTrue(ok)
                    out = buf.getvalue()
                    self.assertIn('DIRECT PRs TO FORK (dimstar/vlc)', out)
                    self.assertIn('CENTRAL UPSTREAM PRs (pool/vlc)', out)
                    self.assertIn('Local PR', out)
                    self.assertIn('Pool PR', out)
                    self.assertIn('geckopit-cli --merge-pr 4', out)

if __name__ == '__main__':

    unittest.main()
