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

from geckopit import guess_update_revision, compare_versions, is_version_newer, is_version_equal

class TestVersionGuessing(unittest.TestCase):
    def test_simdutf_prefix_v(self):
        # 1. Prefix 'v' versioning (simdutf style)
        guessed, err = guess_update_revision("v9.1.2", "9.2.0")
        self.assertEqual(guessed, "v9.2.0")
        self.assertIsNone(err)

    def test_appstream_glib_underscores(self):
        # 2. Underscores versioning (appstream-glib style)
        guessed, err = guess_update_revision("appstream_glib_0_8_4", "0.8.5")
        self.assertEqual(guessed, "appstream_glib_0_8_5")
        self.assertIsNone(err)

    def test_gupnp_tools_prefix_dots(self):
        # 3. Dots versioning with complex prefix (gupnp-tools style)
        guessed, err = guess_update_revision("gupnp-tools-0.12.4", "0.12.5")
        self.assertEqual(guessed, "gupnp-tools-0.12.5")
        self.assertIsNone(err)

    def test_spiel_prefix_underscores(self):
        # 4. Underscores with uppercase prefix (spiel style)
        guessed, err = guess_update_revision("SPIEL_1_0_4", "1.0.5")
        self.assertEqual(guessed, "SPIEL_1_0_5")
        self.assertIsNone(err)

    def test_gnome_shell_normal(self):
        # 5. Standard version with dots only (gnome-shell style)
        guessed, err = guess_update_revision("51.0", "51.1")
        self.assertEqual(guessed, "51.1")
        self.assertIsNone(err)

    def test_vocalis_hash(self):
        # 6. Specific git commit SHA (vocalis style)
        guessed, err = guess_update_revision("aa0c00a9cb037e78a6ed2ec30dc2256aa4bcdabb", "1.0.0")
        self.assertIsNone(guessed)
        self.assertIn("Git commit SHA", err)

    def test_static_branch(self):
        # 7. Static branch (master style)
        guessed, err = guess_update_revision("master", "1.0.0")
        self.assertIsNone(guessed)
        self.assertIn("static development branch", err)

    def test_target_underscores_local_dots(self):
        # 8. Target has underscores but current revision uses dots (libiptcdata style)
        guessed, err = guess_update_revision("1.0.4", "1_0_5")
        self.assertEqual(guessed, "1.0.5")
        self.assertIsNone(err)

    def test_prefix_and_underscores(self):
        # 9. Current revision has prefix and underscores (release_1_0_4 style)
        guessed, err = guess_update_revision("release_1_0_4", "1.0.5")
        self.assertEqual(guessed, "release_1_0_5")
        self.assertIsNone(err)

    def test_libgedit_gtksourceview_prerelease_to_stable(self):
        # 10. Upgrading from alpha to stable release must not append old '.alpha' suffix
        guessed, err = guess_update_revision("299.8.alpha", "299.8.0")
        self.assertEqual(guessed, "299.8.0")
        self.assertIsNone(err)

        # Also when upgrading between pre-releases
        guessed, err = guess_update_revision("299.8.alpha", "299.8.beta")
        self.assertEqual(guessed, "299.8.beta")
        self.assertIsNone(err)

    def test_single_part_prerelease(self):
        # 11. Single-number with pre-release qualifier (gedit & gnome-extensions-app style)
        guessed, err = guess_update_revision("51.alpha", "51.0")
        self.assertEqual(guessed, "51.0")
        self.assertIsNone(err)

        guessed, err = guess_update_revision("51.alpha1", "51.0")
        self.assertEqual(guessed, "51.0")
        self.assertIsNone(err)

        guessed, err = guess_update_revision("v51.alpha", "51.0")
        self.assertEqual(guessed, "v51.0")
        self.assertIsNone(err)

    def test_four_part_version_no_spurious_suffix(self):
        # 12. 4-part versions must not append trailing 4th sub-minor component to new target
        guessed, err = guess_update_revision("0.21.8.2", "0.22.0")
        self.assertEqual(guessed, "0.22.0")
        self.assertIsNone(err)

        guessed, err = guess_update_revision("refs/tags/0.9.7.2", "0.9.8")
        self.assertEqual(guessed, "refs/tags/0.9.8")
        self.assertIsNone(err)

    def test_refs_tags_prefix(self):
        # 13. refs/tags/ prefix with and without leading 'v'
        guessed, err = guess_update_revision("refs/tags/v3.7.0", "3.7.1")
        self.assertEqual(guessed, "refs/tags/v3.7.1")
        self.assertIsNone(err)

        guessed, err = guess_update_revision("refs/tags/3.3.0", "3.3.1")
        self.assertEqual(guessed, "refs/tags/3.3.1")
        self.assertIsNone(err)

    def test_target_version_prefix_deduplication(self):
        # 14. Target version already starting with 'v' must not produce 'vv1.2.4'
        guessed, err = guess_update_revision("v1.2.3", "v1.2.4")
        self.assertEqual(guessed, "v1.2.4")
        self.assertIsNone(err)

        # When local revision had no 'v', target 'v' is stripped to match local style
        guessed, err = guess_update_revision("1.2.3", "v1.2.4")
        self.assertEqual(guessed, "1.2.4")
        self.assertIsNone(err)

    def test_single_number_and_calver(self):
        # 15. Pure integer versions (gnome-radio style) and CalVer dates (mobile-broadband-provider-info style)
        guessed, err = guess_update_revision("128", "129")
        self.assertEqual(guessed, "129")
        self.assertIsNone(err)

        guessed, err = guess_update_revision("v128", "129")
        self.assertEqual(guessed, "v129")
        self.assertIsNone(err)

        guessed, err = guess_update_revision("20240407", "20241019")
        self.assertEqual(guessed, "20241019")
        self.assertIsNone(err)

class TestSemanticVersionComparison(unittest.TestCase):
    def test_underscore_and_dot_equality(self):
        # 1.0.5 and 1_0_5 are semantically identical under RPM rules
        self.assertTrue(is_version_equal("1.0.5", "1_0_5"))
        self.assertTrue(is_version_equal("1_0_5", "1.0.5"))
        self.assertFalse(is_version_newer("1_0_5", "1.0.5"))
        self.assertFalse(is_version_newer("1.0.5", "1_0_5"))

    def test_genuine_updates(self):
        # True newer versions must be flagged regardless of separator style
        self.assertTrue(is_version_newer("1.0.6", "1.0.5"))
        self.assertTrue(is_version_newer("1_0_6", "1.0.5"))
        self.assertTrue(is_version_newer("1.0.6", "1_0_5"))
        self.assertTrue(is_version_newer("v1.0.6", "1.0.5"))
        self.assertTrue(is_version_newer("V1.0.6", "1.0.5"))

    def test_git_snapshot_comparisons(self):
        # Git snapshots (e.g. 1.0.0+git) are post-release increments and not older than 1.0.0
        self.assertFalse(is_version_newer("1.0.0", "1.0.0+git"))
        self.assertFalse(is_version_newer("1.0.0", "1.0.0+git42"))
        # An actual next release is newer than the snapshot
        self.assertTrue(is_version_newer("1.0.1", "1.0.0+git42"))

    def test_lagging_upstream_and_pre_releases(self):
        # dasher: upstream stable 4_11_0 is older than local 5.0.0+199
        self.assertFalse(is_version_newer("4_11_0", "5.0.0+199"))
        # dasher: upstream beta 5_0_0_beta is older than post-release snapshot 5.0.0+199
        self.assertFalse(is_version_newer("5_0_0_beta", "5.0.0+199"))

    def test_downstream_branding_git_snapshot(self):
        # gnome-tour: local has downstream branding and git snapshot (50.0.openSUSE+git20260413.334ffbd)
        tour_local = "50.0.openSUSE+git20260413.334ffbd"
        # Upstream 50.0 is not newer than our post-50.0 branded snapshot
        self.assertFalse(is_version_newer("50.0", tour_local))
        # An actual next release (50.1) is newer and properly triggers an update
        self.assertTrue(is_version_newer("50.1", tour_local))

    def test_placeholders_and_empty_values(self):
        self.assertFalse(is_version_newer("1.0.5", "—"))
        self.assertFalse(is_version_newer("—", "1.0.5"))
        self.assertFalse(is_version_newer("1.0.5", "N/A"))
        self.assertFalse(is_version_newer("N/A", "1.0.5"))
        self.assertFalse(is_version_newer("", ""))
        self.assertFalse(is_version_newer(None, None))


if __name__ == '__main__':
    unittest.main()
