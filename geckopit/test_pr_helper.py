#!/usr/bin/env python3
import os
import sys
import unittest
import tempfile
import subprocess
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'helpers'))
from pr_helper import check_spec_collisions, get_pr_commits, merge_pull_request

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

if __name__ == '__main__':
    unittest.main()
