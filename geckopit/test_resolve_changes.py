#!/usr/bin/env python3
import os
import sys
import unittest
import importlib.machinery

# Load resolve-changes
rc_path = os.path.join(os.path.dirname(__file__), '..', 'GNOME-maintainer-scripts', 'resolve-changes')
loader = importlib.machinery.SourceFileLoader("resolve_changes", rc_path)
rc = loader.load_module()

class TestResolveChanges(unittest.TestCase):

    def setUp(self):
        self.stage1 = """-------------------------------------------------------------------
Wed Sep 23 11:25:28 UTC 2026 - Bjørn Lie <bjorn.lie@gmail.com>

- Shared ancestor fix.

-------------------------------------------------------------------
Thu Sep 17 14:11:40 UTC 2026 - Bjørn Lie <bjorn.lie@gmail.com>

- Update to version 2.54.0:
  + Feature A.
  + Feature B.
"""
        # Ours has an added 2.54.1, an added patch, and an amended date on 2.54.0
        self.stage2 = """-------------------------------------------------------------------
Fri Oct  2 14:55:25 UTC 2026 - Dominique Leuenberger <dimstar@opensuse.org>

- Update to version 2.54.1.

-------------------------------------------------------------------
Sun Sep 27 18:38:01 UTC 2026 - Michael Gorse <mgorse@suse.com>

- Add patch for s390x.

-------------------------------------------------------------------
Wed Sep 23 11:25:28 UTC 2026 - Bjørn Lie <bjorn.lie@gmail.com>

- Shared ancestor fix.

-------------------------------------------------------------------
Thu Sep 17 17:05:54 UTC 2026 - Bjørn Lie <bjorn.lie@gmail.com>

- Update to version 2.54.0:
  + Feature A.
  + Feature B.
"""
        # Theirs has a new patch entry from a contributor
        self.stage3 = """-------------------------------------------------------------------
Mon Sep 28 10:20:51 UTC 2026 - Andreas Schwab <schwab@suse.de>

- Fix build for riscv64.

-------------------------------------------------------------------
Wed Sep 23 11:25:28 UTC 2026 - Bjørn Lie <bjorn.lie@gmail.com>

- Shared ancestor fix.

-------------------------------------------------------------------
Thu Sep 17 14:11:40 UTC 2026 - Bjørn Lie <bjorn.lie@gmail.com>

- Update to version 2.54.0:
  + Feature A.
  + Feature B.
"""

    def test_cherry_pick_places_incoming_on_top_and_redates_theirs(self):
        """In cherry-pick mode, the incoming PR entry must sit at the top, redated to now."""
        merged = rc.merge_changes_structural(self.stage1, self.stage2, self.stage3, is_cherry_pick=True)
        entries = rc.parse_entries_list(merged)

        # Top entry should be Andreas Schwab's entry
        self.assertIn("Andreas Schwab <schwab@suse.de>", entries[0][0])
        self.assertIn("Fix build for riscv64", entries[0][1])

        # Second entry should be Dominique's 2.54.1 entry with UNTOUCHED date
        self.assertIn("Dominique Leuenberger <dimstar@opensuse.org>", entries[1][0])
        self.assertIn("Fri Oct  2 14:55:25 UTC 2026", entries[1][0])
        self.assertIn("Update to version 2.54.1", entries[1][1])

        # Third entry should be Michael Gorse's patch with UNTOUCHED date
        self.assertIn("Michael Gorse <mgorse@suse.com>", entries[2][0])
        self.assertIn("Sun Sep 27 18:38:01 UTC 2026", entries[2][0])

    def test_body_aware_matching_prevents_duplication(self):
        """Even though 2.54.0 date was amended in stage2, it must not be duplicated."""
        merged = rc.merge_changes_structural(self.stage1, self.stage2, self.stage3, is_cherry_pick=True)
        entries = rc.parse_entries_list(merged)

        # Count occurrences of 2.54.0 in entries
        occurrences = [e for e in entries if "Update to version 2.54.0:" in e[1]]
        self.assertEqual(len(occurrences), 1, "2.54.0 entry was duplicated!")
        # And it should preserve the updated timestamp from stage2 (17:05:54)
        self.assertIn("Thu Sep 17 17:05:54 UTC 2026", occurrences[0][0])

    def test_branch_merge_keeps_ours_on_top_and_redates_ours(self):
        """In branch merge mode (factory -> next), next entries sit on top and are redated."""
        merged = rc.merge_changes_structural(self.stage1, self.stage2, self.stage3, is_cherry_pick=False)
        entries = rc.parse_entries_list(merged)

        # Top entry should be Dominique's entry
        self.assertIn("Dominique Leuenberger <dimstar@opensuse.org>", entries[0][0])
        # Next entry should be Michael Gorse
        self.assertIn("Michael Gorse <mgorse@suse.com>", entries[1][0])
        # Followed by Andreas Schwab from factory
        self.assertIn("Andreas Schwab <schwab@suse.de>", entries[2][0])
        self.assertIn("Mon Sep 28 10:20:51 UTC 2026", entries[2][0])

    def test_deduplicate_identical_cherry_picks(self):
        """If stage3 already contains an entry identical to an entry on stage2, it is omitted."""
        stage3_with_duplicate = """-------------------------------------------------------------------
Sun Sep 27 18:38:01 UTC 2026 - Michael Gorse <mgorse@suse.com>

- Add patch for s390x.

""" + self.stage1
        merged = rc.merge_changes_structural(self.stage1, self.stage2, stage3_with_duplicate, is_cherry_pick=True)
        entries = rc.parse_entries_list(merged)
        s390x_entries = [e for e in entries if "Add patch for s390x." in e[1]]
        self.assertEqual(len(s390x_entries), 1)


    def test_chronologically_sorted_incoming_not_redated(self):
        """When incoming entry has a timestamp newer than ours, original date must be preserved."""
        stage3_newer = """-------------------------------------------------------------------
Sat Oct  3 10:20:51 UTC 2026 - Andreas Schwab <schwab@suse.de>

- Fix build for riscv64.

""" + self.stage1
        merged = rc.merge_changes_structural(self.stage1, self.stage2, stage3_newer, is_cherry_pick=True)
        entries = rc.parse_entries_list(merged)
        self.assertIn("Andreas Schwab <schwab@suse.de>", entries[0][0])
        self.assertIn("Sat Oct  3 10:20:51 UTC 2026", entries[0][0], "Original incoming date was modified!")

    def test_chronologically_sorted_branch_merge_not_redated(self):
        """When branch merge entries on next are already newer than incoming entries, no dates are altered."""
        # stage2 has Dominique (Oct 2), Michael (Sep 27)
        # stage3_older has Contributor (Sep 25) which is older than Michael (Sep 27) but newer than Bjørn (Sep 23)
        stage3_older = """-------------------------------------------------------------------
Fri Sep 25 12:00:00 UTC 2026 - Contributor <contrib@example.com>

- Stable bugfix from factory.

""" + self.stage1
        merged = rc.merge_changes_structural(self.stage1, self.stage2, stage3_older, is_cherry_pick=False)
        entries = rc.parse_entries_list(merged)
        # Check that Dominique, Michael, and Contributor all preserve their exact timestamps
        self.assertIn("Dominique Leuenberger <dimstar@opensuse.org>", entries[0][0])
        self.assertIn("Fri Oct  2 14:55:25 UTC 2026", entries[0][0])
        self.assertIn("Michael Gorse <mgorse@suse.com>", entries[1][0])
        self.assertIn("Sun Sep 27 18:38:01 UTC 2026", entries[1][0])
        self.assertIn("Contributor <contrib@example.com>", entries[2][0])
        self.assertIn("Fri Sep 25 12:00:00 UTC 2026", entries[2][0])

if __name__ == '__main__':
    unittest.main()
