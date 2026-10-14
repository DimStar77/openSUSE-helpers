#!/usr/bin/env python3
import os
import sys
import unittest
import tempfile
import tarfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'helpers'))
from patch_checker import get_spec_patches, get_spec_details, find_primary_source_archive, check_patches_sequential, run_check_patches_cli

class TestPatchChecker(unittest.TestCase):

    def test_get_spec_patches_order_and_macros(self):
        """Tests parsing patches in exact declaration order with %define and %name macro expansion."""
        with tempfile.TemporaryDirectory() as tmpdir:
            spec_path = os.path.join(tmpdir, "testpkg.spec")
            with open(spec_path, "w", encoding="utf-8") as f:
                f.write("""Name: testpkg
Version: 2.0
%define patchdir mypatches
Patch0: %{name}-first.patch
Patch1: second.patch
Patch2: %{patchdir}/third.patch
""")
            patches = get_spec_patches(tmpdir)
            self.assertEqual(patches, ["testpkg-first.patch", "second.patch", "third.patch"])

    def test_check_patches_sequential_chaining_and_halt_on_failure(self):
        """Tests sequential dependency chaining where patch2 depends on patch1, and halts on failure."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Create a dummy tarball with file.txt
            tar_dir = os.path.join(tmpdir, "src")
            os.makedirs(tar_dir)
            with open(os.path.join(tar_dir, "file.txt"), "w") as f:
                f.write("line 1\nline 2\nline 3\n")

            tar_path = os.path.join(tmpdir, "testpkg-1.0.tar.xz")
            with tarfile.open(tar_path, "w:xz") as tf:
                tf.add(tar_dir, arcname="testpkg-1.0")

            # 2. Patch 1: appends 'line 2.5' after line 2
            p1_content = """--- a/file.txt
+++ b/file.txt
@@ -1,3 +1,4 @@
 line 1
 line 2
+line 2.5
 line 3
"""
            p1_path = os.path.join(tmpdir, "patch1.patch")
            with open(p1_path, "w") as f:
                f.write(p1_content)

            # 3. Patch 2: depends on 'line 2.5' introduced by Patch 1!
            p2_content = """--- a/file.txt
+++ b/file.txt
@@ -2,3 +2,4 @@
 line 2
 line 2.5
+line 2.75
 line 3
"""
            p2_path = os.path.join(tmpdir, "patch2.patch")
            with open(p2_path, "w") as f:
                f.write(p2_content)

            # 4. Patch 3: invalid patch that will fail
            p3_invalid = """--- a/file.txt
+++ b/file.txt
@@ -1,2 +1,3 @@
 non-existent line
+fail
 line 3
"""
            p3_path = os.path.join(tmpdir, "patch3.patch")
            with open(p3_path, "w") as f:
                f.write(p3_invalid)

            # 5. Patch 4: valid patch that touches line 3
            p4_content = """--- a/file.txt
+++ b/file.txt
@@ -3,1 +3,2 @@
 line 3
+line 4
"""
            p4_path = os.path.join(tmpdir, "patch4.patch")
            with open(p4_path, "w") as f:
                f.write(p4_content)

            # Create spec file declaring all 4 patches
            spec_path = os.path.join(tmpdir, "testpkg.spec")
            with open(spec_path, "w") as f:
                f.write("""Name: testpkg
Version: 1.0
Source0: testpkg-1.0.tar.xz
Patch1: patch1.patch
Patch2: patch2.patch
Patch3: patch3.patch
Patch4: patch4.patch
""")

            # Run sequential check - should halt at patch3
            res = check_patches_sequential(tmpdir)
            self.assertFalse(res["success"])
            self.assertEqual(res["applied"], ["patch1.patch", "patch2.patch"])
            self.assertEqual(res["failed_patch"], "patch3.patch")
            self.assertEqual(res["skipped"], ["patch4.patch"])

            # 6. RESUME-AFTER-REBASE TEST: Fix patch 3 and re-verify without any directory teardown!
            p3_fixed = """--- a/file.txt
+++ b/file.txt
@@ -1,2 +1,3 @@
+line 0.5
 line 1
 line 2
"""
            with open(p3_path, "w") as f:
                f.write(p3_fixed)

            res_fixed = check_patches_sequential(tmpdir)
            self.assertTrue(res_fixed["success"])
            self.assertEqual(len(res_fixed["applied"]), 4)
            self.assertIsNone(res_fixed["failed_patch"])
            self.assertEqual(len(res_fixed["skipped"]), 0)

    def test_find_primary_source_archive_ignores_newer_secondary_mtime(self):
        """Tests that find_primary_source_archive selects Source0 / Name-Version even if a secondary archive has a newer mtime."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Create primary tarball with older mtime
            tar_dir = os.path.join(tmpdir, "primary_src")
            os.makedirs(tar_dir)
            with open(os.path.join(tar_dir, "file.txt"), "w") as f:
                f.write("main upstream content\n")

            primary_tar = os.path.join(tmpdir, "testpkg-1.0.tar.xz")
            with tarfile.open(primary_tar, "w:xz") as tf:
                tf.add(tar_dir, arcname="testpkg-1.0")
            os.utime(primary_tar, (1000, 1000))

            # 2. Create secondary submodule tarball with NEWER mtime
            sub_dir = os.path.join(tmpdir, "sub_src")
            os.makedirs(sub_dir)
            with open(os.path.join(sub_dir, "sub.txt"), "w") as f:
                f.write("submodule content\n")

            secondary_tar = os.path.join(tmpdir, "vendor-submodule-0.gitmodule.tar.xz")
            with tarfile.open(secondary_tar, "w:xz") as tf:
                tf.add(sub_dir, arcname="vendor-submodule-0.gitmodule")
            os.utime(secondary_tar, (5000, 5000))

            # 3. Create patch for primary source
            p_content = """--- a/file.txt
+++ b/file.txt
@@ -1,1 +1,2 @@
 main upstream content
+downstream patch
"""
            with open(os.path.join(tmpdir, "patch1.patch"), "w") as f:
                f.write(p_content)

            # 4. Create spec file
            spec_path = os.path.join(tmpdir, "testpkg.spec")
            with open(spec_path, "w") as f:
                f.write("""Name: testpkg
Version: 1.0
Source0: %{name}-%{version}.tar.xz
Source1: vendor-submodule-0.gitmodule.tar.xz
Patch1: patch1.patch
""")

            selected_archive = find_primary_source_archive(tmpdir)
            self.assertEqual(selected_archive, primary_tar)

            res = check_patches_sequential(tmpdir)
            self.assertTrue(res["success"])
            self.assertEqual(res["applied"], ["patch1.patch"])

    def test_check_patches_multi_archive_subproject_resolution(self):
        """Tests that patch targets in subprojects/ are populated from secondary archives (e.g. gitmodules)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Primary archive with main.c
            main_dir = os.path.join(tmpdir, "main_src")
            os.makedirs(main_dir)
            with open(os.path.join(main_dir, "main.c"), "w") as f:
                f.write("int main() { return 0; }\n")

            primary_tar = os.path.join(tmpdir, "myprogram-1.0.tar.xz")
            with tarfile.open(primary_tar, "w:xz") as tf:
                tf.add(main_dir, arcname="myprogram-1.0")

            # 2. Secondary gitmodule archive with subproject file
            sub_dir = os.path.join(tmpdir, "sub_src")
            os.makedirs(sub_dir)
            with open(os.path.join(sub_dir, "helper.c"), "w") as f:
                f.write("void helper() {}\n")

            sub_tar = os.path.join(tmpdir, "submodule-0.gitmodule.tar.xz")
            with tarfile.open(sub_tar, "w:xz") as tf:
                tf.add(sub_dir, arcname="submodule-0.gitmodule")

            # 3. Patch 1 for primary file
            p1_content = """--- a/main.c
+++ b/main.c
@@ -1,1 +1,2 @@
+/* patch1 */
 int main() { return 0; }
"""
            with open(os.path.join(tmpdir, "patch1.patch"), "w") as f:
                f.write(p1_content)

            # 4. Patch 2 for subproject file placed at subprojects/sub/helper.c
            p2_content = """--- a/subprojects/sub/helper.c
+++ b/subprojects/sub/helper.c
@@ -1,1 +1,2 @@
+/* patch2 */
 void helper() {}
"""
            with open(os.path.join(tmpdir, "patch2.patch"), "w") as f:
                f.write(p2_content)

            # 5. Spec file declaring both sources and both patches
            spec_path = os.path.join(tmpdir, "myprogram.spec")
            with open(spec_path, "w") as f:
                f.write("""Name: myprogram
Version: 1.0
Source0: %{name}-%{version}.tar.xz
Source1: submodule-0.gitmodule.tar.xz
Patch1: patch1.patch
Patch2: patch2.patch
""")

            res = check_patches_sequential(tmpdir)
            self.assertTrue(res["success"])
            self.assertEqual(res["applied"], ["patch1.patch", "patch2.patch"])
            self.assertIsNone(res["failed_patch"])

    def test_check_patches_sle_only_failure_is_non_blocking_info(self):
        """Tests that SLE-only patch (Patch1000+) failures are non-blocking for openSUSE and captured in sle_issues."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Primary archive with file.txt
            tar_dir = os.path.join(tmpdir, "src")
            os.makedirs(tar_dir)
            with open(os.path.join(tar_dir, "file.txt"), "w") as f:
                f.write("openSUSE line 1\nline 2\n")

            tar_path = os.path.join(tmpdir, "mytest-1.0.tar.xz")
            with tarfile.open(tar_path, "w:xz") as tf:
                tf.add(tar_dir, arcname="mytest-1.0")

            # 2. Patch 1: openSUSE patch applies cleanly
            p1_content = """--- a/file.txt
+++ b/file.txt
@@ -1,2 +1,3 @@
 openSUSE line 1
+openSUSE patch line
 line 2
"""
            with open(os.path.join(tmpdir, "opensuse.patch"), "w") as f:
                f.write(p1_content)

            # 3. Patch 1001: SLE-only patch that conflicts
            p1001_content = """--- a/file.txt
+++ b/file.txt
@@ -1,2 +1,3 @@
 non-existent line
+sle patch
 line 2
"""
            with open(os.path.join(tmpdir, "sle-feature.patch"), "w") as f:
                f.write(p1001_content)

            # 4. Spec file declaring Patch1 (openSUSE) and Patch1001 (SLE-only)
            spec_path = os.path.join(tmpdir, "mytest.spec")
            with open(spec_path, "w") as f:
                f.write("""Name: mytest
Version: 1.0
Source0: %{name}-%{version}.tar.xz
Patch1: opensuse.patch
# PATCH-FEATURE-SLE sle-feature.patch -- SLE only feature
Patch1001: sle-feature.patch
""")

            res = check_patches_sequential(tmpdir)
            self.assertTrue(res["success"], "openSUSE verification should succeed despite SLE patch conflict")
            self.assertEqual(res["applied"], ["opensuse.patch"])
            self.assertIsNone(res["failed_patch"])
            self.assertEqual(len(res["sle_issues"]), 1)
            self.assertEqual(res["sle_issues"][0]["patch"], "sle-feature.patch")
            self.assertEqual(res["sle_issues"][0]["reason"], "conflict")

            cli_ok = run_check_patches_cli(tmpdir)
            self.assertTrue(cli_ok, "CLI pre-flight check should return True for openSUSE contributors")

    def test_check_patches_distinguishes_conflict_and_missing_file(self):
        """Tests failure_reason differentiation between hunk conflict on existing file vs missing target file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Primary archive with existing.c
            tar_dir = os.path.join(tmpdir, "src")
            os.makedirs(tar_dir)
            with open(os.path.join(tar_dir, "existing.c"), "w") as f:
                f.write("int x = 1;\n")

            tar_path = os.path.join(tmpdir, "failtest-1.0.tar.xz")
            with tarfile.open(tar_path, "w:xz") as tf:
                tf.add(tar_dir, arcname="failtest-1.0")

            # 2. Patch targeting existing file but with conflict
            p_conflict = """--- a/existing.c
+++ b/existing.c
@@ -1,1 +1,2 @@
 int x = 999;
+int y = 2;
"""
            with open(os.path.join(tmpdir, "conflict.patch"), "w") as f:
                f.write(p_conflict)

            spec_path = os.path.join(tmpdir, "failtest.spec")
            with open(spec_path, "w") as f:
                f.write("""Name: failtest
Version: 1.0
Source0: %{name}-%{version}.tar.xz
Patch1: conflict.patch
""")

            res_conflict = check_patches_sequential(tmpdir)
            self.assertFalse(res_conflict["success"])
            self.assertEqual(res_conflict["failed_patch"], "conflict.patch")
            self.assertEqual(res_conflict["failure_reason"], "conflict")

            # 3. Now test missing file patch
            p_missing = """--- a/nonexistent.c
+++ b/nonexistent.c
@@ -1,1 +1,2 @@
 void func();
+void other();
"""
            with open(os.path.join(tmpdir, "missing.patch"), "w") as f:
                f.write(p_missing)

            with open(spec_path, "w") as f:
                f.write("""Name: failtest
Version: 1.0
Source0: %{name}-%{version}.tar.xz
Patch1: missing.patch
""")

            res_missing = check_patches_sequential(tmpdir)
            self.assertFalse(res_missing["success"])
            self.assertEqual(res_missing["failed_patch"], "missing.patch")
            self.assertEqual(res_missing["failure_reason"], "missing_file")

    def test_run_check_patches_cli_verbose_mode(self):
        """Tests that run_check_patches_cli with verbose=True prints detailed SLE breakdown."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tar_dir = os.path.join(tmpdir, "src")
            os.makedirs(tar_dir)
            with open(os.path.join(tar_dir, "file.txt"), "w") as f:
                f.write("line 1\nline 2\n")

            tar_path = os.path.join(tmpdir, "mytest-1.0.tar.xz")
            with tarfile.open(tar_path, "w:xz") as tf:
                tf.add(tar_dir, arcname="mytest-1.0")

            with open(os.path.join(tmpdir, "opensuse.patch"), "w") as f:
                f.write("""--- a/file.txt
+++ b/file.txt
@@ -1,2 +1,3 @@
 line 1
+opensuse line
 line 2
""")

            with open(os.path.join(tmpdir, "sle.patch"), "w") as f:
                f.write("""--- a/file.txt
+++ b/file.txt
@@ -1,2 +1,3 @@
 nonexistent line
+sle line
 line 2
""")

            with open(os.path.join(tmpdir, "mytest.spec"), "w") as f:
                f.write("""Name: mytest
Version: 1.0
Source0: %{name}-%{version}.tar.xz
Patch1: opensuse.patch
Patch1001: sle.patch
""")

            self.assertTrue(run_check_patches_cli(tmpdir, verbose=True))

if __name__ == '__main__':
    unittest.main()
