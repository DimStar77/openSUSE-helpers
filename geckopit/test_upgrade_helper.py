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
    get_upgrade_helper,
    list_upgrade_helpers,
    register_upgrade_helper
)
from upgrade.changelog import (
    wrap_bullet,
    format_changelog_entry,
    build_changelog_from_items,
    remove_patch_from_spec,
    CHANGELOG_WRAP_WIDTH
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
        result = build_changelog_from_items(items, "47.0", dropped_patches=["fix.patch"], has_translations=True, width=67)
        lines = result.splitlines()
        self.assertEqual(lines[0], "- Update to version 47.0:")
        self.assertEqual(lines[1], "  + Add support for GNOME 47")
        self.assertEqual(lines[2], "  + Fix memory leak on shutdown")
        self.assertEqual(lines[3], "  + Updated translations.")
        self.assertEqual(lines[4], "- Drop fix.patch: fixed upstream.")

    def test_wrap_bullet_levels(self):
        # Level 0
        l0 = wrap_bullet("Update to version 51.0:", level=0, width=67)
        self.assertTrue(l0.startswith("- Update to version 51.0:"))

        # Level 1
        l1 = wrap_bullet("Don't duplicate locale keyboard layout", level=1, width=67)
        self.assertTrue(l1.startswith("  + Don't duplicate locale keyboard layout"))

        # Level 2 with wrap at 67 chars
        long_txt = "Fix several issues related to presentation of ARIA tree and treegrid in web engine."
        l2 = wrap_bullet(long_txt, level=2, width=67)
        lines = l2.splitlines()
        self.assertTrue(lines[0].startswith("    - Fix several issues related to presentation of ARIA tree and"))
        self.assertTrue(lines[1].startswith("      treegrid in web engine."))
        for line in lines:
            self.assertLessEqual(len(line), 67)

        # Level 3
        l3 = wrap_bullet("Detailed sub-sub item", level=3, width=67)
        self.assertTrue(l3.startswith("      . Detailed sub-sub item"))

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
        result = format_changelog_entry(sample_diff, "51.0", dropped_patches=["e5c2018d.patch"], width=67)
        expected = """- Update to version 51.0:
  + Don't duplicate locale keyboard layout
  + Fix activating network items in quick settings
  + Improve lock/login screen styling
  + Cancel mount password dialogs when locking screen
  + Validate serilized image data before creating pixbuf
  + Fixed crash
  + Plugged leaks
  + Misc. bug fixes and cleanups
  + Updated translations.
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
        result = format_changelog_entry(sample_diff, "51.0", width=67)
        lines = result.splitlines()
        self.assertEqual(lines[0], "- Update to version 51.0:")
        self.assertEqual(lines[1], "  + Web:")
        self.assertEqual(lines[2], "    - Fix combining lines incorrectly due to Gecko scaling bug.")
        self.assertEqual(lines[3], '    - Fix missing "leaving blockquote" announcement.')
        self.assertEqual(lines[4], "  + General:")
        self.assertEqual(lines[5], "    - Fix on-the-fly changes related to the non-global voice set.")
        self.assertEqual(lines[6], "    - Fix not speaking a newly-shown terminal line after scrolling.")
        self.assertEqual(lines[7], "  + Updated translations.")

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

if __name__ == '__main__':
    unittest.main()
