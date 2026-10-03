#!/usr/bin/env python3
import os
import sys
import unittest
import tempfile
import tarfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'helpers'))
from patch_checker import get_spec_patches, check_patches_sequential, run_check_patches_cli

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

if __name__ == '__main__':
    unittest.main()
