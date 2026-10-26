#!/usr/bin/env python3
import os
import sys
import tempfile
import unittest

import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
GECKOPIT_DIR = os.path.dirname(TESTS_DIR)
HELPERS_DIR = os.path.join(GECKOPIT_DIR, "helpers")

for p in [GECKOPIT_DIR, HELPERS_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

from upgrade.changelog import (
    clean_trailing_issue_ref,
    build_changelog_from_items,
    wrap_bullet,
    format_changelog_entry,
    check_retrospective_news_changes,
    extract_appstream_notes,
    APPSTREAM_XML_REGEX,
    is_date_line,
)

class TestChangelogEngine(unittest.TestCase):
    def test_clean_trailing_issue_ref(self):
        from upgrade.changelog import clean_trailing_issue_ref
        self.assertEqual(clean_trailing_issue_ref('Fix bug (#6705, #6678, #6677)'), 'Fix bug')
        self.assertEqual(clean_trailing_issue_ref('Fix bug (#6710) (#6715)'), 'Fix bug')
        self.assertEqual(clean_trailing_issue_ref('Fix bug (bgo#123456)'), 'Fix bug')
        # Preserve CVE identifiers
        self.assertEqual(clean_trailing_issue_ref('bubblewrap 0.12.0 (CVE-2026-87766)'), 'bubblewrap 0.12.0 (CVE-2026-87766)')

    def test_build_changelog_from_items(self):
        items = [
            (1, "Add support for GNOME 47"),
            (1, "Fix memory leak on shutdown")
        ]
        result = build_changelog_from_items(items, "47.0", dropped_patches=["fix.patch"], has_translations=True)
        lines = result.splitlines()
        self.assertEqual(lines[0], "- Update to version 47.0:")
        self.assertEqual(lines[1], "  * Add support for GNOME 47")
        self.assertEqual(lines[2], "  * Fix memory leak on shutdown")
        self.assertEqual(lines[3], "  * Updated translations.")
        self.assertEqual(lines[4], "- Drop fix.patch: fixed upstream.")

    def test_wrap_bullet_levels(self):
        # Level 0
        l0 = wrap_bullet("Update to version 51.0:", level=0)
        self.assertTrue(l0.startswith("- Update to version 51.0:"))

        # Level 1
        l1 = wrap_bullet("Don't duplicate locale keyboard layout", level=1)
        self.assertTrue(l1.startswith("  * Don't duplicate locale keyboard layout"))

        # Level 2 with wrap at 79 chars (default CHANGELOG_WRAP_WIDTH)
        long_txt = "Fix several issues related to presentation of ARIA tree and treegrid in web engine."
        l2 = wrap_bullet(long_txt, level=2)
        lines = l2.splitlines()
        self.assertTrue(lines[0].startswith("    + Fix several issues related to presentation of ARIA tree and treegrid in"))
        self.assertTrue(lines[1].startswith("      web engine."))
        for line in lines:
            self.assertLessEqual(len(line), 79)

        # Level 3
        l3 = wrap_bullet("Detailed sub-sub item", level=3)
        self.assertTrue(l3.startswith("      - Detailed sub-sub item"))

    def test_format_changelog_entry_flat_list_and_dropped_patch(self):
        sample_diff = '''
+++ b/NEWS
+51.0
+====
+* Don't duplicate locale keyboard layout
+* Fix activating network items in quick settings
+* Improve lock/login screen styling
+* Cancel mount password dialogs when locking screen
+* Validate serilized image data before creating pixbuf
+* Fixed crash
+* Plugged leaks
+* Misc. bug fixes and cleanups
+
+Translations:
+* Bulgarian (Shopov)
+* Spanish (Mustieles)
'''
        result = format_changelog_entry(sample_diff, "51.0", dropped_patches=["e5c2018d.patch"])
        expected = """- Update to version 51.0:
  * Don't duplicate locale keyboard layout
  * Fix activating network items in quick settings
  * Improve lock/login screen styling
  * Cancel mount password dialogs when locking screen
  * Validate serilized image data before creating pixbuf
  * Fixed crash
  * Plugged leaks
  * Misc. bug fixes and cleanups
  * Updated translations.
- Drop e5c2018d.patch: fixed upstream."""
        self.assertEqual(result.strip(), expected.strip())

    def test_format_changelog_entry_nested_categories(self):
        sample_diff = '''
+51.0
+====
+
+Web:
+ * Fix combining lines incorrectly due to Gecko scaling bug.
+ * Fix missing "leaving blockquote" announcement.
+
+General:
+ * Fix on-the-fly changes related to the non-global voice set.
+ * Fix not speaking a newly-shown terminal line after scrolling.
+
+Translations:
+ * Bulgarian
'''
        result = format_changelog_entry(sample_diff, "51.0")
        lines = result.splitlines()
        self.assertEqual(lines[0], "- Update to version 51.0:")
        self.assertEqual(lines[1], "  * Web:")
        self.assertEqual(lines[2], "    + Fix combining lines incorrectly due to Gecko scaling bug.")
        self.assertEqual(lines[3], '    + Fix missing "leaving blockquote" announcement.')
        self.assertEqual(lines[4], "  * General:")
        self.assertEqual(lines[5], "    + Fix on-the-fly changes related to the non-global voice set.")
        self.assertEqual(lines[6], "    + Fix not speaking a newly-shown terminal line after scrolling.")
        self.assertEqual(lines[7], "  * Updated translations.")

    def test_format_changelog_entry_news_in_header(self):
        sample_diff = """
+++ b/NEWS
+News in 299.8.0, 2026-10-15
+---------------------------
+* Translation updates.
+
+News in 299.8.alpha, 2026-08-22
+-------------------------------
+* GtkSourceFileLoader: code refactorings.
"""
        result = format_changelog_entry(sample_diff, "299.8.0")
        expected = """- Update to version 299.8.0:
  * Updated translations."""
        self.assertEqual(result.strip(), expected.strip())
        self.assertNotIn("News in 299.8.0", result)

    def test_format_changelog_entry_various_release_headers(self):
        # 1. New in <version>
        diff1 = """
+++ b/NEWS
+New in 51.1 - 6 October 2026
+----------------------------
+* Fixed race condition in worker
"""
        res1 = format_changelog_entry(diff1, "51.1")
        self.assertNotIn("New in 51.1", res1)
        self.assertIn("* Fixed race condition in worker", res1)

        # 2. PackageName <version>
        diff2 = """
+++ b/NEWS
+Evolution 3.62.1 2026-10-09
+---------------------------
+* Address book sync improvements
"""
        res2 = format_changelog_entry(diff2, "3.62.1")
        self.assertNotIn("Evolution 3.62.1", res2)
        self.assertIn("* Address book sync improvements", res2)

    def test_format_changelog_entry_markdown_history_header(self):
        diff = """--- a/HISTORY.md
+++ b/HISTORY.md
@@ -1,3 +1,14 @@
+# History of versions #
+
+* Version 51.0.9 (2026-10-11)
+  * Updated french translation
+  * Updated Occitan translation
+  * Drop dead X11 code (Thanks to Marco Trevisan)
+  * Added CI checks
+  * Added new open/close emblems to stacks
+  * Don't show for a brief moment a window icon in Dash (Marco Trevisan)
+  * Use org.freedesktop.FileManager1 interface to open folders
+  * Use sandboxing by default in .appimage files
+
 * Version 51.0.8 (2026-09-09)
"""
        res = format_changelog_entry(diff, "51.0.9")
        self.assertIn("- Update to version 51.0.9:", res)
        self.assertNotIn("History of versions", res)
        self.assertNotIn("Version 51.0.9", res)
        self.assertNotIn("Version 51.0.8", res)
        self.assertIn("  * Drop dead X11 code (Thanks to Marco Trevisan)", res)
        self.assertIn("  * Added CI checks", res)
        self.assertIn("  * Updated translations.", res)
        self.assertNotIn("Updated french translation", res)

    def test_format_changelog_entry_keep_a_changelog_markdown(self):
        diff = """--- a/CHANGELOG.md
+++ b/CHANGELOG.md
@@ -1,5 +1,12 @@
 # Changelog

+## [1.2.0] - 2026-10-20
+### Added
+- New user onboarding flow
+- Support for dark mode
+
 ## [1.1.0] - 2026-08-15
"""
        res = format_changelog_entry(diff, "1.2.0")
        self.assertIn("- Update to version 1.2.0:", res)
        self.assertNotIn("Changelog", res)
        self.assertIn("  * Added:", res)
        self.assertIn("    + New user onboarding flow", res)
        self.assertIn("    + Support for dark mode", res)

    def test_check_retrospective_news_changes(self):
        from upgrade.changelog import check_retrospective_news_changes
        diff_no_retro = '--- NEWS.old\n+++ NEWS.new\n@@ -1,5 +1,10 @@\n+Changes in 1.1\n+==============\n+* Feature A\n+\n Changes in 1.0\n'
        self.assertFalse(check_retrospective_news_changes(diff_no_retro))

        diff_with_retro = '--- NEWS.old\n+++ NEWS.new\n@@ -1,15 +1,20 @@\n+Changes in 1.1\n+==============\n+* Feature A\n+\n Changes in 1.0\n ==============\n * Bug fix\n+* (CVE-2026-1234)\n'
        self.assertTrue(check_retrospective_news_changes(diff_with_retro))

    def test_format_changelog_entry_empty_diff(self):
        res = format_changelog_entry("", "2.1.8")
        self.assertEqual(res.strip(), "- Update to version 2.1.8: No changelog found.")

    def test_format_changelog_entry_empty_diff_with_dropped_patches(self):
        res = format_changelog_entry("", "2.1.8", dropped_patches=["fix.patch"])
        expected = """- Update to version 2.1.8: No changelog found.
- Drop fix.patch: fixed upstream."""
        self.assertEqual(res.strip(), expected)

    def test_extract_appstream_notes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            xml_path = os.path.join(tmpdir, "test.metainfo.xml")
            with open(xml_path, "w") as f:
                f.write("""<?xml version="1.0" encoding="UTF-8"?>
<component>
  <releases>
    <release version="2.0">
      <description>
        <p>New features:</p>
        <ul>
          <li>First cool feature</li>
          <li>Second cool feature</li>
        </ul>
      </description>
    </release>
  </releases>
</component>""")
            notes = extract_appstream_notes(xml_path, version="2.0")
            self.assertIsNotNone(notes)
            self.assertIn("New features:", notes)
            self.assertIn("* First cool feature", notes)

            # Test formatting
            diff_lines = ["--- a/test.xml", "+++ b/test.xml", "@@ -0,0 +1,5 @@"]
            diff_lines += ["+" + l for l in notes.splitlines()]
            formatted = format_changelog_entry("\n".join(diff_lines), "2.0")
            self.assertIn("- Update to version 2.0:", formatted)
            self.assertIn("* New features:", formatted)
            self.assertIn("+ First cool feature", formatted)

    def test_appstream_xml_regex(self):
        """Tests that APPSTREAM_XML_REGEX matches .xml, .xml.in, and .xml.in.in variants."""
        valid_filenames = [
            "org.gnome.Baobab.metainfo.xml",
            "org.gnome.Baobab.appdata.xml",
            "info.febvre.Komikku.metainfo.xml.in",
            "info.febvre.Komikku.metainfo.xml.in.in",
            "data/info.febvre.Komikku.metainfo.xml.in.in",
            "data/org.example.App.appdata.xml.in.in",
            "metainfo.xml",
            "appdata.xml.in",
        ]
        for f in valid_filenames:
            self.assertTrue(bool(APPSTREAM_XML_REGEX.search(f)), f"Failed to match valid: {f}")

        invalid_filenames = [
            "info.febvre.Komikku.metainfo.xml.bak",
            "info.febvre.Komikku.metainfo.xml.patch",
            "info.febvre.Komikku.metainfo.xml.in.orig",
            "NEWS",
            "meson.build",
            "metainfo.xml.txt",
        ]
        for f in invalid_filenames:
            self.assertFalse(bool(APPSTREAM_XML_REGEX.search(f)), f"Incorrectly matched invalid: {f}")

    def test_extract_appstream_notes_double_in_and_fluff(self):
        """Tests extraction from metainfo.xml.in.in with trailing fluff paragraph and translation lines."""
        with tempfile.TemporaryDirectory() as tmpdir:
            xml_path = os.path.join(tmpdir, "info.febvre.Komikku.metainfo.xml.in.in")
            with open(xml_path, "w", encoding="utf-8") as f:
                f.write("""<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop-application">
  <releases>
    <release version="51.1.0" date="2026-10-03">
      <description translate="no">
        <ul>
          <li>[Card] Tracking: Added warning message when sync fails</li>
          <li>[Reader] Fixed selection coordinate calculation</li>
          <li>[L10n] Updated French, Indonesian and Korean translations</li>
        </ul>
        <p>Happy reading.</p>
      </description>
    </release>
  </releases>
</component>""")
            notes = extract_appstream_notes(xml_path, version="51.1.0")
            self.assertIsNotNone(notes)
            self.assertIn("* [Card] Tracking:", notes)
            self.assertNotIn("Happy reading", notes)

            # Test changelog formatting
            diff_lines = ["--- a/test.xml", "+++ b/test.xml", "@@ -0,0 +5 @@"]
            diff_lines += ["+" + l for l in notes.splitlines()]
            formatted = format_changelog_entry("\n".join(diff_lines), "51.1.0")
            self.assertIn("- Update to version 51.1.0:", formatted)
            self.assertIn("[Card] Tracking: Added warning message when sync fails", formatted)
            self.assertIn("[Reader] Fixed selection coordinate calculation", formatted)
            self.assertIn("Updated translations.", formatted)
            self.assertNotIn("Happy reading", formatted)


    def test_is_date_line(self):
        valid_dates = [
            "Thursday, October 22, 2026",
            "Monday, October 5, 2026",
            "Thursday, October 26th, 2017",
            "Monday, October 23nd, 2017",
            "Friday, October the 13th, 2017",
            "Tuesday, Sep 03, 2012",
            "Released October 2026",
            "Released February 2026",
            "Released: 2026-10-22",
            "Released on October 22, 2026",
            "Release date: October 22, 2026",
            "Date: 2026-10-22",
            "October 22, 2026",
            "22 October 2026",
            "22nd October 2026",
            "22-Oct-2026",
            "2026-10-22",
            "22.10.2026",
            "10/22/2026",
            "(2026-10-22)",
            "[October 22, 2026]",
            "Thu Oct 22 06:45:47 UTC 2026",
            "2026-10-22 14:30:00",
            "2026-10-22T14:30:00Z",
            "2026-09-29  Lars Windolf <lars.windolf@gmx.de>",
        ]
        for d in valid_dates:
            self.assertTrue(is_date_line(d, d.strip()), f"Failed to match date: {d}")

        invalid_dates = [
            "Web:",
            "General:",
            "Support for the new tables and formats",
            "* Thursday, October 22, 2026",
            "- 2026-10-22: Release notes",
            "Fixed bug in October 2026 where something broke",
        ]
        for inv in invalid_dates:
            self.assertFalse(is_date_line(inv, inv.strip()), f"Incorrectly matched non-date: {inv}")

    def test_format_changelog_entry_harfbuzz_multiline_header(self):
        sample_diff = """
+++ b/NEWS
+Overview of changes leading to 15.0.0
+Thursday, October 22, 2026
+=====================================
+- Support for the new tables and formats in the ISO Open Font Format (OFF) 5th
+  edition is now enabled by default:
+  * New tables and formats for 24-bit glyph indices (a.k.a. beyond-64k): the
+    `GLYF`, `LOCA`, `GVAR`, `MAXP`, `HHEA`, `HMTX`, `VHEA`, and `VMTX` tables
+  * Cubic curves in `GLYF` outlines.
+  * The `DMAP` table, supported since 14.6.0.
+- Support instancing axes not referenced by retained `VARC` data.
+- Rewrite the number parser without Ragel, fixing various edge cases.

Overview of changes leading to 14.6.0
Monday, October 5, 2026
=====================================
- Fix shaping failures caused by out-of-range glyph IDs.
"""
        result = format_changelog_entry(sample_diff, "15.0.0")
        lines = result.splitlines()
        self.assertEqual(lines[0], "- Update to version 15.0.0:")
        # Date and release header must be completely stripped
        self.assertNotIn("Thursday, October 22, 2026", result)
        self.assertNotIn("Overview of changes", result)
        self.assertNotIn("Fix shaping failures", result)
        # Main bullets should start with level 1 indentation (* )
        self.assertEqual(lines[1], "  * Support for the new tables and formats in the ISO Open Font Format (OFF)")
        # Nested sub-bullets should be indented with (+ )
        self.assertTrue(any("    + New tables and formats for 24-bit glyph indices" in l for l in lines))
        self.assertTrue(any("    + Cubic curves in `GLYF` outlines." in l for l in lines))
        self.assertTrue(any("    + The `DMAP` table, supported since 14.6.0." in l for l in lines))
        # Sibling bullets should be level 1 (* )
        self.assertTrue(any("  * Support instancing axes not referenced by retained `VARC` data." in l for l in lines))
        self.assertTrue(any("  * Rewrite the number parser without Ragel, fixing various edge cases." in l for l in lines))

    def test_format_changelog_entry_released_month_year_header(self):
        sample_diff = """
+++ b/NEWS
+feedbackd 0.8.10
+----------------
+Released October 2026
+* Project moved to https://gitlab.freedesktop.org/feedbackd/
+* Allow Phosh to use the important hint (for e.g. cellbroadcasts)
+* Add pmi8998-haptics
+"""
        result = format_changelog_entry(sample_diff, "0.8.10")
        self.assertNotIn("feedbackd 0.8.10", result)
        self.assertNotIn("Released October 2026", result)
        lines = result.splitlines()
        self.assertEqual(lines[0], "- Update to version 0.8.10:")
        self.assertEqual(lines[1], "  * Project moved to https://gitlab.freedesktop.org/feedbackd/")
        self.assertEqual(lines[2], "  * Allow Phosh to use the important hint (for e.g. cellbroadcasts)")
        self.assertEqual(lines[3], "  * Add pmi8998-haptics")


if __name__ == '__main__':
    unittest.main()
