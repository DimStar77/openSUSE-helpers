#!/usr/bin/env python3
import os
import sys
import tempfile
import unittest
import unittest.mock as mock
import subprocess

# Ensure helpers are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'helpers'))
from upgrade import (
    BaseUpgradeHelper,
    UpgradeResult,
    ObsScmUpgradeHelper,
    TarballUpgradeHelper,
    get_upgrade_helper,
    list_upgrade_helpers,
    register_upgrade_helper
)
from upgrade.changelog import (
    wrap_bullet,
    format_changelog_entry,
    build_changelog_from_items,
    remove_patch_from_spec,
    CHANGELOG_WRAP_WIDTH,
    extract_appstream_notes,
    APPSTREAM_XML_REGEX
)

class TestUpgradeHelperArchitecture(unittest.TestCase):

    def test_upgrade_result_model(self):
        res = UpgradeResult(
            success=True,
            message="Upgraded successfully",
            package_name="baobab",
            old_version="49.0",
            new_version="50.0",
            old_revision="aabbcc11",
            new_revision="ddeeff22",
            diff_files={"osc-collab.NEWS": "+ Version 50.0\n"}
        )
        self.assertTrue(res.success)
        self.assertEqual(res.package_name, "baobab")
        self.assertEqual(res.new_version, "50.0")
        d = res.to_dict()
        self.assertEqual(d["old_version"], "49.0")
        self.assertIn("osc-collab.NEWS", d["diff_files"])

    def test_registry_discovery(self):
        helpers = list_upgrade_helpers()
        self.assertIn(ObsScmUpgradeHelper, helpers)

    def test_obs_scm_can_handle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertFalse(ObsScmUpgradeHelper.can_handle(tmpdir))
            helper = get_upgrade_helper(tmpdir)
            self.assertIsNone(helper)

            # Create _service file
            service_path = os.path.join(tmpdir, "_service")
            with open(service_path, "w") as f:
                f.write("<services/>")

            self.assertTrue(ObsScmUpgradeHelper.can_handle(tmpdir))
            helper = get_upgrade_helper(tmpdir)
            self.assertIsNotNone(helper)
            self.assertIsInstance(helper, ObsScmUpgradeHelper)

    def test_obs_scm_get_current_revision(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service_path = os.path.join(tmpdir, "_service")
            with open(service_path, "w") as f:
                f.write('''<services>
  <service name="obs_scm">
    <param name="url">https://gitlab.gnome.org/GNOME/baobab.git</param>
    <param name="revision">50.0</param>
  </service>
</services>''')

            rev = ObsScmUpgradeHelper.get_current_revision(tmpdir)
            self.assertEqual(rev, "50.0")

    def test_obs_scm_get_package_name_from_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service_path = os.path.join(tmpdir, "_service")
            with open(service_path, "w") as f:
                f.write('''<services>
  <service name="obs_scm">
    <param name="url">https://gitlab.gnome.org/GNOME/gnome-calculator.git</param>
    <param name="revision">50.0</param>
  </service>
</services>''')

            helper = ObsScmUpgradeHelper(tmpdir)
            self.assertEqual(helper.get_package_name(), "gnome-calculator")

    def test_obs_scm_update_service_revision(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service_path = os.path.join(tmpdir, "_service")
            original_xml = '''<?xml version="1.0"?>
<services>
  <!-- Comment preserving -->
  <service name="obs_scm" mode="manual">
    <param name="url">https://gitlab.gnome.org/GNOME/baobab.git</param>
    <param name="revision">49.0</param>
    <param name="versionformat">@PARENT_TAG@</param>
  </service>
</services>'''
            with open(service_path, "w") as f:
                f.write(original_xml)

            helper = ObsScmUpgradeHelper(tmpdir)
            ok = helper.update_service_revision("50.0")
            self.assertTrue(ok)

            with open(service_path, "r") as f:
                updated_xml = f.read()

            self.assertIn('<param name="revision">50.0</param>', updated_xml)
            self.assertIn('<!-- Comment preserving -->', updated_xml)

    def test_clean_duplicate_obscpio_if_manual(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service_path = os.path.join(tmpdir, "_service")
            with open(service_path, "w") as f:
                f.write('''<services>
  <service name="tar" mode="manual"/>
</services>''')
            obscpio = os.path.join(tmpdir, "pkg.obscpio")
            with open(obscpio, "w") as f: f.write("dummy")

            helper = ObsScmUpgradeHelper(tmpdir)
            removed = helper.clean_duplicate_obscpio_if_manual()
            self.assertEqual(removed, ["pkg.obscpio"])
            self.assertFalse(os.path.exists(obscpio))

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

    def test_remove_patch_from_spec(self):
        spec_content = '''Name: gnome-shell
Version: 50.0
Patch1: fix-cursor.patch
# PATCH-FIX-UPSTREAM
# https://gitlab.gnome.org/GNOME/gnome-shell/-/commit/e5c2018d
Patch2: https://gitlab.gnome.org/GNOME/gnome-shell/-/commit/e5c2018d.patch
Patch100: no-gnome-tour.patch
'''
        cleaned = remove_patch_from_spec(spec_content, "e5c2018d.patch")
        self.assertNotIn("e5c2018d.patch", cleaned)
        self.assertNotIn("PATCH-FIX-UPSTREAM", cleaned)
        self.assertIn("Patch1: fix-cursor.patch", cleaned)
        self.assertIn("Patch100: no-gnome-tour.patch", cleaned)

    def test_update_spec_version(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            spec_path = os.path.join(tmpdir, "baobab.spec")
            with open(spec_path, "w") as f:
                f.write('''Name: baobab
Version:        50.0
Release:        0
''')
            helper = ObsScmUpgradeHelper(tmpdir)
            sf = helper.update_spec_version("51.0")
            self.assertEqual(sf, "baobab.spec")

            with open(spec_path, "r") as f:
                updated = f.read()
            self.assertIn("Version:        51.0", updated)

    def test_find_upstream_changelog_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            helper = ObsScmUpgradeHelper(tmpdir)
            # Default fallback when none exist
            self.assertEqual(helper.find_upstream_changelog_target(tmpdir), "NEWS")

            # If both ChangeLog and NEWS exist on disk, NEWS takes precedence
            cl_path = os.path.join(tmpdir, "ChangeLog")
            with open(cl_path, "w") as f: f.write("test")
            news_path = os.path.join(tmpdir, "NEWS")
            with open(news_path, "w") as f: f.write("test")
            self.assertEqual(helper.find_upstream_changelog_target(tmpdir), "NEWS")

            # With NEWS.md taking precedence over ChangeLog
            os.remove(news_path)
            news_md = os.path.join(tmpdir, "NEWS.md")
            with open(news_md, "w") as f: f.write("test")
            self.assertEqual(helper.find_upstream_changelog_target(tmpdir), "NEWS.md")

    def test_find_upstream_changelog_target_git_activity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Initialize a real git repo
            subprocess.run(["git", "init", tmpdir], capture_output=True, check=True)
            subprocess.run(["git", "-C", tmpdir, "config", "user.name", "test"], capture_output=True, check=True)
            subprocess.run(["git", "-C", tmpdir, "config", "user.email", "test@example.com"], capture_output=True, check=True)

            # Both NEWS and ChangeLog exist in commit 1
            with open(os.path.join(tmpdir, "NEWS"), "w") as f: f.write("v1\n")
            with open(os.path.join(tmpdir, "ChangeLog"), "w") as f: f.write("v1\n")
            subprocess.run(["git", "-C", tmpdir, "add", "."], capture_output=True, check=True)
            subprocess.run(["git", "-C", tmpdir, "commit", "-m", "init"], capture_output=True, check=True)
            old_rev = subprocess.run(["git", "-C", tmpdir, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

            # Commit 2: Only ChangeLog was updated in this release
            with open(os.path.join(tmpdir, "ChangeLog"), "a") as f: f.write("v2 changes\n")
            subprocess.run(["git", "-C", tmpdir, "add", "."], capture_output=True, check=True)
            subprocess.run(["git", "-C", tmpdir, "commit", "-m", "update"], capture_output=True, check=True)

            helper = ObsScmUpgradeHelper(tmpdir)
            # When old_rev is checked, ChangeLog is detected because it had active changes in this release
            self.assertEqual(helper.find_upstream_changelog_target(tmpdir, old_rev=old_rev), "ChangeLog")

    @mock.patch("subprocess.run")
    def test_audit_and_drop_merged_patches(self, mock_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            upstream_repo = os.path.join(tmpdir, "baobab")
            os.makedirs(os.path.join(upstream_repo, ".git"))

            patch_file = os.path.join(tmpdir, "e5c2018d.patch")
            with open(patch_file, "w") as f:
                f.write("diff --git a/a b/b\n")

            spec_file = os.path.join(tmpdir, "baobab.spec")
            with open(spec_file, "w") as f:
                f.write("Patch0: e5c2018d.patch\n")

            mock_proc = mock.MagicMock()
            mock_proc.returncode = 0 # Simulate merge-base returns 0 (is ancestor)
            mock_run.return_value = mock_proc

            helper = ObsScmUpgradeHelper(tmpdir)
            dropped = helper.audit_and_drop_merged_patches("baobab")

            self.assertEqual(dropped, ["e5c2018d.patch"])
            self.assertFalse(os.path.exists(patch_file))
            with open(spec_file, "r") as f:
                self.assertNotIn("e5c2018d.patch", f.read())

    def test_check_retrospective_news_changes(self):
        from upgrade.changelog import check_retrospective_news_changes
        diff_no_retro = '--- NEWS.old\n+++ NEWS.new\n@@ -1,5 +1,10 @@\n+Changes in 1.1\n+==============\n+* Feature A\n+\n Changes in 1.0\n'
        self.assertFalse(check_retrospective_news_changes(diff_no_retro))

        diff_with_retro = '--- NEWS.old\n+++ NEWS.new\n@@ -1,15 +1,20 @@\n+Changes in 1.1\n+==============\n+* Feature A\n+\n Changes in 1.0\n ==============\n * Bug fix\n+* (CVE-2026-1234)\n'
        self.assertTrue(check_retrospective_news_changes(diff_with_retro))

    def test_parse_lfs_pointer(self):
        from upgrade.tarball import parse_lfs_pointer
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write('version https://git-lfs.github.com/spec/v1\noid sha256:b6630bd24f8161b0e2546d2acbb014a3b3249f5c0d75f2a863ade898b9034d3d\nsize 45932\n')
            tmp_path = tf.name

        try:
            is_lfs, oid, size = parse_lfs_pointer(tmp_path)
            self.assertTrue(is_lfs)
            self.assertEqual(oid, "b6630bd24f8161b0e2546d2acbb014a3b3249f5c0d75f2a863ade898b9034d3d")
            self.assertEqual(size, 45932)
        finally:
            os.remove(tmp_path)

    def test_lfs_giant_archive_size_guard(self):
        from upgrade.tarball import TarballUpgradeHelper, MAX_LFS_SMUDGE_SIZE
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(f"version https://git-lfs.github.com/spec/v1\noid sha256:0000000000000000000000000000000000000000000000000000000000000000\nsize {MAX_LFS_SMUDGE_SIZE + 1000}\n")
            tmp_path = tf.name

        try:
            res = TarballUpgradeHelper.extract_member_content(tmp_path, "NEWS")
            self.assertIsNone(res)
        finally:
            os.remove(tmp_path)

    def test_tarball_helper_can_handle(self):
        from upgrade.tarball import TarballUpgradeHelper
        with tempfile.TemporaryDirectory() as tmpdir:
            # No spec -> False
            self.assertFalse(TarballUpgradeHelper.can_handle(tmpdir))

            # Spec exists -> True
            spec_path = os.path.join(tmpdir, 'pkg.spec')
            with open(spec_path, 'w') as f: f.write('Version: 1.0\n')
            self.assertTrue(TarballUpgradeHelper.can_handle(tmpdir))

            # If _service has obs_scm -> False
            srv_path = os.path.join(tmpdir, '_service')
            with open(srv_path, 'w') as f: f.write('<service name="obs_scm"/>\n')
            self.assertFalse(TarballUpgradeHelper.can_handle(tmpdir))

    def test_tarball_extract_member_content(self):
        from upgrade.tarball import TarballUpgradeHelper
        import tarfile, io
        with tempfile.TemporaryDirectory() as tmpdir:
            tar_path = os.path.join(tmpdir, 'pkg-1.0.tar.xz')
            with tarfile.open(tar_path, 'w:xz') as tf:
                data = b'Changes in 1.0\n'
                ti = tarfile.TarInfo(name='pkg-1.0/NEWS')
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))

            txt = TarballUpgradeHelper.extract_member_content(tar_path, 'NEWS', package_dir=tmpdir)
            self.assertEqual(txt, 'Changes in 1.0\n')

    def test_tarball_execute_upgrade_dry_run(self):
        from upgrade.tarball import TarballUpgradeHelper
        with tempfile.TemporaryDirectory() as tmpdir:
            spec_path = os.path.join(tmpdir, 'pkg.spec')
            with open(spec_path, 'w') as f: f.write('Name: pkg\nVersion: 1.0\n')
            helper = TarballUpgradeHelper(tmpdir)
            res = helper.execute_upgrade(target_revision='1.1', dry_run=True)
            self.assertTrue(res.success)
            self.assertIn('[DRY-RUN]', res.message)
            self.assertEqual(res.old_version, '1.0')
            self.assertEqual(res.new_version, '1.1')

    def test_format_changelog_entry_empty_diff(self):
        res = format_changelog_entry("", "2.1.8")
        self.assertEqual(res.strip(), "- Update to version 2.1.8.")

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

    def test_obs_scm_metainfo_double_in(self):
        """Tests ObsScmUpgradeHelper changelog target discovery and extraction on metainfo.xml.in.in."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service_path = os.path.join(tmpdir, "_service")
            with open(service_path, "w") as f:
                f.write('''<services>
  <service name="obs_scm" mode="manual">
    <param name="url">https://codeberg.org/valos/Komikku</param>
    <param name="scm">git</param>
    <param name="revision">v51.1.0</param>
    <param name="filename">Komikku</param>
  </service>
</services>''')
            with open(os.path.join(tmpdir, "Komikku.spec"), "w") as f:
                f.write("Name: Komikku\n")

            clone_dir = os.path.join(tmpdir, "Komikku")
            data_dir = os.path.join(clone_dir, "data")
            os.makedirs(os.path.join(clone_dir, ".git"))
            os.makedirs(data_dir)

            xml_path = os.path.join(data_dir, "info.febvre.Komikku.metainfo.xml.in.in")
            with open(xml_path, "w", encoding="utf-8") as f:
                f.write('''<?xml version="1.0" encoding="UTF-8"?>
<component>
  <releases>
    <release version="51.1.0">
      <description>
        <ul>
          <li>Double in extension test note</li>
        </ul>
      </description>
    </release>
  </releases>
</component>''')

            helper = ObsScmUpgradeHelper(tmpdir)
            target = helper.find_upstream_changelog_target(clone_dir)
            self.assertEqual(target, os.path.relpath(xml_path, clone_dir))

            diffs = helper.extract_git_diffs(pkg_name="Komikku", new_ver="51.1.0")
            self.assertIn("osc-collab.NEWS", diffs)
            self.assertIn("Double in extension test note", diffs["osc-collab.NEWS"])

    def test_obs_scm_get_obsinfo_path_and_metadata_mismatch(self):
        """Tests case where repo URL is Junction.git, but obsinfo on disk is junction.obsinfo."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service_path = os.path.join(tmpdir, "_service")
            with open(service_path, "w") as f:
                f.write('''<services>
  <service name="obs_scm" mode="manual">
    <param name="url">https://github.com/sonnyp/Junction.git</param>
    <param name="scm">git</param>
    <param name="revision">v1.12</param>
    <param name="filename">junction</param>
  </service>
</services>''')
            obsinfo_path = os.path.join(tmpdir, "junction.obsinfo")
            with open(obsinfo_path, "w") as f:
                f.write('''name: junction
version: 1.12
mtime: 1768265974
commit: 22cc7bade6a0d63b5ebeb3ab58f57f71ce1a26a1
''')
            spec_path = os.path.join(tmpdir, "junction.spec")
            with open(spec_path, "w") as f:
                f.write("Name: junction\nVersion: 1.12\n")

            helper = ObsScmUpgradeHelper(tmpdir)
            self.assertEqual(helper.get_package_name(), "junction")
            found_obs = helper.get_obsinfo_path()
            self.assertEqual(found_obs, obsinfo_path)

            ver, rev = helper.get_obsinfo_metadata()
            self.assertEqual(ver, "1.12")
            self.assertEqual(rev, "22cc7bade6a0d63b5ebeb3ab58f57f71ce1a26a1")

            # Calling with uppercase 'Junction' still finds junction.obsinfo
            ver_u, rev_u = helper.get_obsinfo_metadata("Junction")
            self.assertEqual(ver_u, "1.12")
            self.assertEqual(rev_u, "22cc7bade6a0d63b5ebeb3ab58f57f71ce1a26a1")

    def test_obs_scm_get_upstream_repo_dir(self):
        """Tests discovery of cloned upstream directory matching URL basename case."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service_path = os.path.join(tmpdir, "_service")
            with open(service_path, "w") as f:
                f.write('''<services>
  <service name="obs_scm">
    <param name="url">https://github.com/sonnyp/Junction.git</param>
  </service>
</services>''')
            # Package name is lowercase junction
            with open(os.path.join(tmpdir, "junction.spec"), "w") as f:
                f.write("Name: junction\n")

            # Upstream clone directory is uppercase Junction
            clone_dir = os.path.join(tmpdir, "Junction")
            os.makedirs(os.path.join(clone_dir, ".git"))

            helper = ObsScmUpgradeHelper(tmpdir)
            found_repo = helper.get_upstream_repo_dir("junction")
            self.assertEqual(found_repo, clone_dir)

    def test_obs_scm_extract_appstream_diff(self):
        """Tests AppStream release notes extraction without requiring old_rev."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service_path = os.path.join(tmpdir, "_service")
            with open(service_path, "w") as f:
                f.write('''<services>
  <service name="obs_scm">
    <param name="url">https://github.com/sonnyp/Junction.git</param>
  </service>
</services>''')
            with open(os.path.join(tmpdir, "junction.spec"), "w") as f:
                f.write("Name: junction\n")

            clone_dir = os.path.join(tmpdir, "Junction")
            data_dir = os.path.join(clone_dir, "data")
            os.makedirs(os.path.join(clone_dir, ".git"))
            os.makedirs(data_dir)

            xml_path = os.path.join(data_dir, "re.sonny.Junction.metainfo.xml")
            with open(xml_path, "w") as f:
                f.write('''<?xml version="1.0" encoding="UTF-8"?>
<component>
  <releases>
    <release version="1.13">
      <description>
        <ul>
          <li>Use GNOME 51</li>
          <li>Follow system color scheme</li>
        </ul>
      </description>
    </release>
  </releases>
</component>''')

            helper = ObsScmUpgradeHelper(tmpdir)
            diffs = helper.extract_git_diffs(pkg_name="junction", new_ver="1.13")
            self.assertIn("osc-collab.NEWS", diffs)
            self.assertIn("Use GNOME 51", diffs["osc-collab.NEWS"])
            self.assertIn("Follow system color scheme", diffs["osc-collab.NEWS"])

    def test_ensure_gitignore_pattern(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            gi_path = os.path.join(tmpdir, ".gitignore")
            with open(gi_path, "w") as f:
                f.write("*.obscpio\n")

            helper = ObsScmUpgradeHelper(tmpdir)
            added = helper.ensure_gitignore_pattern("osc-collab.*")
            self.assertTrue(added)

            with open(gi_path, "r") as f:
                content = f.read()
            self.assertIn("osc-collab.*", content)

            # Second call should not duplicate
            added_again = helper.ensure_gitignore_pattern("osc-collab.*")
            self.assertFalse(added_again)
            with open(gi_path, "r") as f:
                content_after = f.read()
            self.assertEqual(content, content_after)

    def test_obs_scm_is_git_managed_and_uses_obscpio(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service_path = os.path.join(tmpdir, "_service")
            with open(service_path, "w") as f:
                f.write('''<services>
  <service name="tar" mode="buildtime" />
</services>''')
            helper = ObsScmUpgradeHelper(tmpdir)
            self.assertTrue(helper.uses_obscpio())

            # Legacy osc checkout (has .osc and no .git) is not git managed
            os.makedirs(os.path.join(tmpdir, ".osc"))
            self.assertFalse(helper.is_git_managed())

            # Service with manual tar does not use obscpio
            with open(service_path, "w") as f:
                f.write('''<services>
  <service name="tar" mode="manual" />
</services>''')
            self.assertFalse(helper.uses_obscpio())


    def test_get_referenced_files_librpm(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            spec_path = os.path.join(tmpdir, "testpkg.spec")
            with open(spec_path, "w") as f:
                f.write("""Name:           testpkg
Version:        1.2.0
Release:        0
Summary:        Test summary
License:        GPL-2.0
Source0:        https://example.com/releases/%{name}-%{version}.tar.xz
Source1:        https://example.com/releases/%{name}-%{version}.tar.xz.asc
Source2:        testpkg.keyring
Patch0:         fix-build.patch
%description
Test package
""")
            refs = BaseUpgradeHelper.get_referenced_files(spec_path=spec_path)
            self.assertIn("testpkg-1.2.0.tar.xz", refs)
            self.assertIn("testpkg-1.2.0.tar.xz.asc", refs)
            self.assertIn("testpkg.keyring", refs)
            self.assertIn("fix-build.patch", refs)

    def test_get_referenced_files_fallback(self):
        spec_content = """%define rname myapp
%global subver 3.4.1
Name:           %{rname}
Version:        %{subver}
Source0:        https://example.com/%{name}-%{version}.tar.xz
Source1:        https://example.com/%{name}-%{version}.tar.xz.sig
Source2:        myapp.keyring
# Source99:     commented.tar.gz
"""
        refs = BaseUpgradeHelper._extract_referenced_files_fallback(spec_content)
        self.assertIn("myapp-3.4.1.tar.xz", refs)
        self.assertIn("myapp-3.4.1.tar.xz.sig", refs)
        self.assertIn("myapp.keyring", refs)
        self.assertNotIn("commented.tar.gz", refs)

    def test_clean_obsolete_files_path_traversal_prevention(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            external_file = os.path.join(tmpdir, "secret.txt")
            with open(external_file, "w") as f:
                f.write("sensitive data")

            pkg_dir = os.path.join(tmpdir, "pkg")
            os.makedirs(pkg_dir)

            valid_obsolete = os.path.join(pkg_dir, "old-1.0.tar.xz")
            with open(valid_obsolete, "w") as f:
                f.write("obsolete content")

            helper = TarballUpgradeHelper(pkg_dir)
            malicious_inputs = {
                "../secret.txt",
                "../../secret.txt",
                external_file,
                "/etc/shadow",
                "subdir/nested.tar.gz",
                "old-1.0.tar.xz",
            }
            logs = []
            removed = helper.clean_obsolete_files(malicious_inputs, on_log=logs.append)

            # Valid obsolete file inside pkg_dir was removed
            self.assertFalse(os.path.exists(valid_obsolete))
            self.assertEqual(removed, ["old-1.0.tar.xz"])

            # External file must remain completely untouched
            self.assertTrue(os.path.exists(external_file))
            with open(external_file, "r") as f:
                self.assertEqual(f.read(), "sensitive data")

    def test_clean_obsolete_files_guards(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            files_to_create = [
                "pkg-1.0.tar.xz",
                "pkg-1.0.tar.xz.asc",
                "pkg.keyring",
                "pkg.spec",
                "pkg.changes",
                "_service",
                "_constraints",
                ".gitignore"
            ]
            for fname in files_to_create:
                with open(os.path.join(tmpdir, fname), "w") as f:
                    f.write("content")

            helper = TarballUpgradeHelper(tmpdir)
            obsolete_candidates = {
                "pkg-1.0.tar.xz",
                "pkg-1.0.tar.xz.asc",
                "pkg.spec",
                "pkg.changes",
                "_service",
                "_constraints",
                ".gitignore",
                "nonexistent-file.tar.gz"
            }
            logs = []
            removed = helper.clean_obsolete_files(obsolete_candidates, on_log=logs.append)

            # Obsolete files removed
            self.assertFalse(os.path.exists(os.path.join(tmpdir, "pkg-1.0.tar.xz")))
            self.assertFalse(os.path.exists(os.path.join(tmpdir, "pkg-1.0.tar.xz.asc")))
            self.assertEqual(sorted(removed), ["pkg-1.0.tar.xz", "pkg-1.0.tar.xz.asc"])

            # Protected packaging files must remain intact
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "pkg.keyring")))
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "pkg.spec")))
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "pkg.changes")))
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "_service")))
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "_constraints")))
            self.assertTrue(os.path.exists(os.path.join(tmpdir, ".gitignore")))

    def test_tarball_audit_and_drop_merged_patches(self):
        import tarfile, io
        with tempfile.TemporaryDirectory() as tmpdir:
            spec_path = os.path.join(tmpdir, "pkg.spec")
            with open(spec_path, "w") as f:
                f.write("""Name:           pkg
Version:        1.0
Release:        0
Summary:        Test
License:        GPL-2.0
Source0:        pkg-%{version}.tar.gz
Patch0:         fix.patch
Patch1:         unmerged.patch
%description
Test
""")
            # Create patch files
            fix_patch = os.path.join(tmpdir, "fix.patch")
            with open(fix_patch, "w") as f:
                f.write("""--- a/src/foo.c
+++ b/src/foo.c
@@ -1,3 +1,3 @@
 int main() {
-    return 0;
+    return 1;
 }
""")
            unmerged_patch = os.path.join(tmpdir, "unmerged.patch")
            with open(unmerged_patch, "w") as f:
                f.write("""--- a/src/bar.c
+++ b/src/bar.c
@@ -1,3 +1,3 @@
 int bar() {
-    return 10;
+    return 20;
 }
""")

            # Create new archive where fix.patch is incorporated, but unmerged.patch is not
            new_tar = os.path.join(tmpdir, "pkg-1.1.tar.gz")
            with tarfile.open(new_tar, "w:gz") as tf:
                data_foo = b"int main() \n    return 1;\n}\n"
                ti_foo = tarfile.TarInfo("pkg-1.1/src/foo.c")
                ti_foo.size = len(data_foo)
                tf.addfile(ti_foo, io.BytesIO(data_foo))

                data_bar = b"int bar() \n    return 10;\n}\n"
                ti_bar = tarfile.TarInfo("pkg-1.1/src/bar.c")
                ti_bar.size = len(data_bar)
                tf.addfile(ti_bar, io.BytesIO(data_bar))

            helper = TarballUpgradeHelper(tmpdir)
            logs = []
            dropped = helper.audit_and_drop_merged_patches(new_tar, on_log=logs.append)

            # fix.patch was merged and dropped
            self.assertEqual(dropped, ["fix.patch"])
            self.assertFalse(os.path.exists(fix_patch))

            # unmerged.patch remains
            self.assertTrue(os.path.exists(unmerged_patch))

            # spec file reference for fix.patch removed, unmerged.patch retained
            with open(spec_path, "r") as f:
                spec_after = f.read()
            self.assertNotIn("fix.patch", spec_after)
            self.assertIn("unmerged.patch", spec_after)

    def test_tarball_diff_meson_options_json(self):
        import tarfile, io
        with tempfile.TemporaryDirectory() as tmpdir:
            old_tar = os.path.join(tmpdir, "pkg-1.0.tar.gz")
            with tarfile.open(old_tar, "w:gz") as tf:
                data = b'{"feature": {"type": "boolean", "value": false}}'
                ti = tarfile.TarInfo("pkg-1.0/meson_options.json")
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))

            new_tar = os.path.join(tmpdir, "pkg-1.1.tar.gz")
            with tarfile.open(new_tar, "w:gz") as tf:
                data = b'{"feature": {"type": "boolean", "value": true}}'
                ti = tarfile.TarInfo("pkg-1.1/meson_options.json")
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))

            helper = TarballUpgradeHelper(tmpdir)
            old_b = helper.extract_member_content(old_tar, "meson_options.json")
            new_b = helper.extract_member_content(new_tar, "meson_options.json")
            self.assertIsNotNone(old_b)
            self.assertIsNotNone(new_b)
            self.assertIn('"value": false', old_b)
            self.assertIn('"value": true', new_b)

    @mock.patch("subprocess.run")
    def test_tarball_upgrade_safe_temp_news_symlink_protection(self, mock_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            spec_path = os.path.join(tmpdir, "pkg.spec")
            with open(spec_path, "w") as f:
                f.write("Name: pkg\nVersion: 1.0.0\nRelease: 0\nSummary: test\nLicense: GPL-2.0\nSource0: pkg-%{version}.tar.xz\n")
            with open(os.path.join(tmpdir, "pkg-1.0.0.tar.xz"), "w") as f:
                f.write("old")

            # Create sensitive target and a .NEWS symlink pointing to it
            sensitive_target = os.path.join(tmpdir, "sensitive.txt")
            with open(sensitive_target, "w") as f:
                f.write("original sensitive data")
            symlink_news = os.path.join(tmpdir, ".NEWS")
            os.symlink(sensitive_target, symlink_news)

            captured_osc_vc_args = []
            def fake_run(cmd, *args, **kwargs):
                if "download_files" in cmd:
                    with open(os.path.join(tmpdir, "pkg-1.0.1.tar.xz"), "w") as f:
                        f.write("new")
                if "vc" in cmd and "-F" in cmd:
                    captured_osc_vc_args.append(cmd)
                res = mock.MagicMock()
                res.returncode = 0
                res.stdout = ""
                return res

            mock_run.side_effect = fake_run

            helper = TarballUpgradeHelper(tmpdir)
            res = helper.execute_upgrade(target_revision="1.0.1")
            self.assertTrue(res.success)

            # Sensitive target must not have been overwritten via symlink
            with open(sensitive_target, "r") as f:
                self.assertEqual(f.read(), "original sensitive data")

            # Check that osc vc was called with an unpredictable .NEWS-* file, not static .NEWS
            self.assertTrue(len(captured_osc_vc_args) > 0)
            vc_cmd = captured_osc_vc_args[0]
            f_idx = vc_cmd.index("-F")
            passed_filename = vc_cmd[f_idx + 1]
            self.assertTrue(passed_filename.startswith(".NEWS-"))
            self.assertNotEqual(passed_filename, ".NEWS")

    @mock.patch("subprocess.run")
    def test_tarball_upgrade_cleans_obsolete_signature_and_archive(self, mock_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            spec_path = os.path.join(tmpdir, "AppStream.spec")
            with open(spec_path, "w") as f:
                f.write("""Name:           AppStream
Version:        1.2.0
Release:        0
Summary:        AppStream tools
License:        LGPL-2.1-or-later
Source0:        https://example.com/AppStream-%{version}.tar.xz
Source1:        https://example.com/AppStream-%{version}.tar.xz.asc
Source2:        AppStream.keyring
%description
AppStream description
""")
            # Create old source archive and asc
            old_tar = os.path.join(tmpdir, "AppStream-1.2.0.tar.xz")
            old_asc = os.path.join(tmpdir, "AppStream-1.2.0.tar.xz.asc")
            keyring = os.path.join(tmpdir, "AppStream.keyring")
            with open(old_tar, "w") as f: f.write("old archive")
            with open(old_asc, "w") as f: f.write("old asc")
            with open(keyring, "w") as f: f.write("keyring")

            helper = TarballUpgradeHelper(tmpdir)

            def fake_run(cmd, *args, **kwargs):
                # When download_files runs, pretend it downloaded new archive and asc
                if "download_files" in cmd:
                    with open(os.path.join(tmpdir, "AppStream-1.2.1.tar.xz"), "w") as f:
                        f.write("new archive")
                    with open(os.path.join(tmpdir, "AppStream-1.2.1.tar.xz.asc"), "w") as f:
                        f.write("new asc")
                res = mock.MagicMock()
                res.returncode = 0
                res.stdout = ""
                return res

            mock_run.side_effect = fake_run

            logs = []
            result = helper.execute_upgrade(target_revision="1.2.1", on_log=logs.append)
            self.assertTrue(result.success)

            # Old files removed
            self.assertFalse(os.path.exists(old_tar))
            self.assertFalse(os.path.exists(old_asc))

            # New files present
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "AppStream-1.2.1.tar.xz")))
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "AppStream-1.2.1.tar.xz.asc")))
            self.assertTrue(os.path.exists(keyring))

            self.assertIn("AppStream-1.2.0.tar.xz", result.removed_files)
            self.assertIn("AppStream-1.2.0.tar.xz.asc", result.removed_files)

    def test_tarball_upgrade_dry_run_reports_obsolete_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            spec_path = os.path.join(tmpdir, "AppStream.spec")
            with open(spec_path, "w") as f:
                f.write("""Name:           AppStream
Version:        1.2.0
Release:        0
Summary:        AppStream tools
License:        LGPL-2.1-or-later
Source0:        https://example.com/AppStream-%{version}.tar.xz
Source1:        https://example.com/AppStream-%{version}.tar.xz.asc
Source2:        AppStream.keyring
%description
AppStream description
""")
            old_tar = os.path.join(tmpdir, "AppStream-1.2.0.tar.xz")
            old_asc = os.path.join(tmpdir, "AppStream-1.2.0.tar.xz.asc")
            with open(old_tar, "w") as f: f.write("old archive")
            with open(old_asc, "w") as f: f.write("old asc")

            helper = TarballUpgradeHelper(tmpdir)
            logs = []
            res = helper.execute_upgrade(target_revision="1.2.1", dry_run=True, on_log=logs.append)
            self.assertTrue(res.success)

            # In dry-run, files must NOT be deleted
            self.assertTrue(os.path.exists(old_tar))
            self.assertTrue(os.path.exists(old_asc))

            self.assertIn("AppStream-1.2.0.tar.xz", res.removed_files)
            self.assertIn("AppStream-1.2.0.tar.xz.asc", res.removed_files)
            self.assertTrue(any("Would remove obsolete file: AppStream-1.2.0.tar.xz" in l for l in logs))
            self.assertTrue(any("Would remove obsolete file: AppStream-1.2.0.tar.xz.asc" in l for l in logs))

    def test_bash_completion_syntax_and_flag(self):
        comp_path = os.path.join(os.path.dirname(__file__), "completion", "geckopit.bash")
        self.assertTrue(os.path.isfile(comp_path))

        # 1. Test bash syntax validation via bash -n
        res_syntax = subprocess.run(["bash", "-n", comp_path], capture_output=True, text=True)
        self.assertEqual(res_syntax.returncode, 0, f"Bash completion syntax error: {res_syntax.stderr}")

        # 2. Test geckopit-cli --bash-completion flag
        cli_path = os.path.join(os.path.dirname(__file__), "geckopit-cli")
        res_cli = subprocess.run([sys.executable, cli_path, "--bash-completion"], capture_output=True, text=True)
        self.assertEqual(res_cli.returncode, 0)
        self.assertIn("complete -F _geckopit_completion geckopit-cli", res_cli.stdout)
        self.assertIn("_geckopit_get_packages", res_cli.stdout)
        self.assertIn("_geckopit_get_upgrade_targets", res_cli.stdout)

if __name__ == '__main__':
    unittest.main()
