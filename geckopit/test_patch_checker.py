#!/usr/bin/env python3
import os
import sys
import unittest
import tempfile
import tarfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'helpers'))
from patch_checker import get_spec_patches, get_spec_details, find_primary_source_archive, check_patches_sequential, run_check_patches_cli, rebase_patch_cli, get_default_editor, record_patch_rebase_changelog

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

    def test_unconditional_patch_tagged_sle_is_checked_in_normal_run(self):
        """Tests that an unconditionally applied patch tagged with # PATCH-*-SLE is checked as a normal blocking openSUSE patch."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tar_dir = os.path.join(tmpdir, "src")
            os.makedirs(tar_dir)
            with open(os.path.join(tar_dir, "file.txt"), "w") as f:
                f.write("base line\n")

            tar_path = os.path.join(tmpdir, "pkg-1.0.tar.xz")
            with tarfile.open(tar_path, "w:xz") as tf:
                tf.add(tar_dir, arcname="pkg-1.0")

            # Patch has conflict
            with open(os.path.join(tmpdir, "fix-bsc12345.patch"), "w") as f:
                f.write("""--- a/file.txt
+++ b/file.txt
@@ -1,1 +1,2 @@
 wrong context
+added line
""")

            # Spec tags patch with # PATCH-FIX-SLE but applies it unconditionally (Patch1 < 1000, not in %if)
            with open(os.path.join(tmpdir, "pkg.spec"), "w") as f:
                f.write("""Name: pkg
Version: 1.0
Source0: %{name}-%{version}.tar.xz
# PATCH-FIX-SLE fix-bsc12345.patch bsc#123456 -- Fix crash
Patch1: fix-bsc12345.patch

%prep
%autosetup -p1
""")

            spec_details = get_spec_details(tmpdir)
            self.assertFalse(spec_details["patches"][0]["is_sle"], "Unconditionally applied Patch1 must have is_sle=False")

            res = check_patches_sequential(tmpdir)
            self.assertFalse(res["success"], "Unconditional patch failure must halt verification as a blocking failure")
            self.assertEqual(res["failed_patch"], "fix-bsc12345.patch")
            self.assertEqual(res["sle_status"]["total"], 0, "No SLE-only patches should be registered")


    def test_rebase_patch_cli_auto_detects_and_updates_patch(self):
        """Tests that rebase_patch_cli auto-detects failing patch, runs editor resolution, and updates patch on disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = os.path.join(tmpdir, "pkg-2.0")
            os.makedirs(src_dir)
            with open(os.path.join(src_dir, "app.c"), "w") as f:
                f.write("""#include <stdio.h>

void execute_task(void) {
    int mode = 2;
    run_action();
}
""")

            tar_path = os.path.join(tmpdir, "pkg-2.0.tar.xz")
            with tarfile.open(tar_path, "w:xz") as tf:
                tf.add(src_dir, arcname="pkg-2.0")

            # Patch written against old 1.0 version (different context)
            patch_path = os.path.join(tmpdir, "disable-action.patch")
            with open(patch_path, "w") as f:
                f.write("""# Disable action for safety
# bsc#999999
--- a/app.c
+++ b/app.c
@@ -3,3 +3,3 @@
 void execute_task(void) {
-    run_action();
+    // run_action();
 }
""")

            with open(os.path.join(tmpdir, "pkg.spec"), "w") as f:
                f.write("""Name: pkg
Version: 2.0
Source0: %{name}-%{version}.tar.xz
Patch0: disable-action.patch

%prep
%autosetup -p1
""")
            changes_path = os.path.join(tmpdir, "pkg.changes")
            with open(changes_path, "w") as f:
                f.write("""-------------------------------------------------------------------
Wed Oct 14 12:00:00 UTC 2026 - maint@example.com

- Update to version 2.0.
""")

            # Initially fails
            res_before = check_patches_sequential(tmpdir)
            self.assertFalse(res_before["success"])
            self.assertEqual(res_before["failed_patch"], "disable-action.patch")

            # Create mock editor script that updates app.c to comment out run_action()
            editor_script = os.path.join(tmpdir, "mock_editor.py")
            with open(editor_script, "w") as f:
                f.write("""import sys, os
for path in sys.argv[1:]:
    if path.endswith("app.c"):
        with open(path, "r") as f:
            c = f.read()
        c = c.replace("    run_action();", "    // run_action();")
        with open(path, "w") as f:
            f.write(c)
""")

            orig_editor = os.environ.get("EDITOR")
            try:
                os.environ["EDITOR"] = f"python3 {editor_script}"
                ok = rebase_patch_cli(tmpdir)
                self.assertTrue(ok, "rebase_patch_cli should succeed after mock editor resolution")
            finally:
                if orig_editor:
                    os.environ["EDITOR"] = orig_editor
                else:
                    os.environ.pop("EDITOR", None)

            # Verification should now pass
            res_after = check_patches_sequential(tmpdir)
            self.assertTrue(res_after["success"], "All patches should apply cleanly after rebase")

            # Ensure original header/comments were preserved
            with open(patch_path, "r") as f:
                new_patch = f.read()
            self.assertIn("# Disable action for safety", new_patch)
            self.assertIn("# bsc#999999", new_patch)
            self.assertIn("// run_action();", new_patch)

            with open(changes_path, "r") as f:
                ch_content = f.read()
            self.assertIn("- Rebase disable-action.patch.", ch_content)

    def test_rebase_patch_cli_already_clean(self):
        """Tests that rebase_patch_cli returns True immediately when all patches already apply cleanly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = os.path.join(tmpdir, "pkg-1.0")
            os.makedirs(src_dir)
            with open(os.path.join(src_dir, "file.txt"), "w") as f:
                f.write("line 1\nline 2\n")
            tar_path = os.path.join(tmpdir, "pkg-1.0.tar.xz")
            with tarfile.open(tar_path, "w:xz") as tf:
                tf.add(src_dir, arcname="pkg-1.0")
            with open(os.path.join(tmpdir, "clean.patch"), "w") as f:
                f.write("""--- a/file.txt
+++ b/file.txt
@@ -1,2 +1,3 @@
 line 1
+inserted line
 line 2
""")
            with open(os.path.join(tmpdir, "pkg.spec"), "w") as f:
                f.write("""Name: pkg
Version: 1.0
Source0: %{name}-%{version}.tar.xz
Patch0: clean.patch
""")
            ok = rebase_patch_cli(tmpdir)
            self.assertTrue(ok)


    def test_get_default_editor_precedence(self):
        """Tests that get_default_editor strictly adheres to Git's 5-tier resolution precedence."""
        orig = {k: os.environ.get(k) for k in ("GIT_EDITOR", "VISUAL", "EDITOR")}
        try:
            for k in orig:
                os.environ.pop(k, None)

            # Fallback when all env vars are unset: vi or vim
            default_ed = get_default_editor()
            self.assertIn(default_ed, ("vi", "vim", "nano"))

            # $EDITOR sets fallback
            os.environ["EDITOR"] = "custom-editor"
            self.assertEqual(get_default_editor(), "custom-editor")

            # $VISUAL takes precedence over $EDITOR
            os.environ["VISUAL"] = "custom-visual"
            self.assertEqual(get_default_editor(), "custom-visual")

            # $GIT_EDITOR takes precedence over $VISUAL
            os.environ["GIT_EDITOR"] = "custom-git-editor"
            self.assertEqual(get_default_editor(), "custom-git-editor")
        finally:
            for k, v in orig.items():
                if v is not None:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)


    def test_record_patch_rebase_changelog_duplicate_guard(self):
        """Tests that record_patch_rebase_changelog does not duplicate existing rebase entries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            changes_path = os.path.join(tmpdir, "pkg.changes")
            with open(changes_path, "w") as f:
                f.write("""-------------------------------------------------------------------
Wed Oct 14 12:00:00 UTC 2026 - maint@example.com

- Update to version 2.0.
- Rebase test.patch.
""")
            ok = record_patch_rebase_changelog(tmpdir, "test.patch")
            self.assertTrue(ok)
            with open(changes_path, "r") as f:
                c = f.read()
            self.assertEqual(c.count("- Rebase test.patch."), 1, "Must not duplicate existing rebase bullet")


    def test_rebase_patch_cli_targets_and_rebases_sle_patch(self):
        """Tests that rebase_patch_cli correctly targets SLE-only patches and replays openSUSE patches first."""
        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = os.path.join(tmpdir, "pkg-2.0")
            os.makedirs(src_dir)
            with open(os.path.join(src_dir, "file.txt"), "w") as f:
                f.write("base line 1\nbase line 2\n")

            tar_path = os.path.join(tmpdir, "pkg-2.0.tar.xz")
            with tarfile.open(tar_path, "w:xz") as tf:
                tf.add(src_dir, arcname="pkg-2.0")

            # Patch0: openSUSE patch adds line
            with open(os.path.join(tmpdir, "opensuse.patch"), "w") as f:
                f.write("""--- a/file.txt
+++ b/file.txt
@@ -1,2 +1,3 @@
 base line 1
+opensuse line
 base line 2
""")

            # Patch1000: SLE patch fails against new context
            sle_patch_path = os.path.join(tmpdir, "sle.patch")
            with open(sle_patch_path, "w") as f:
                f.write("""--- a/file.txt
+++ b/file.txt
@@ -1,2 +1,3 @@
 wrong context
+sle line
 base line 2
""")

            with open(os.path.join(tmpdir, "pkg.spec"), "w") as f:
                f.write("""Name: pkg
Version: 2.0
Source0: %{name}-%{version}.tar.xz
Patch0: opensuse.patch
Patch1000: sle.patch

%prep
%autosetup -p1
%if !0%{?is_opensuse}
%autopatch -p1 -m 1000
%endif
""")

            changes_path = os.path.join(tmpdir, "pkg.changes")
            with open(changes_path, "w") as f:
                f.write("""-------------------------------------------------------------------
Wed Oct 14 12:00:00 UTC 2026 - maint@example.com

- Update to version 2.0.
""")

            # Mock editor resolves sle.patch by appending sle line after opensuse line
            editor_script = os.path.join(tmpdir, "mock_editor.py")
            with open(editor_script, "w") as f:
                f.write("""import sys
for path in sys.argv[1:]:
    if path.endswith("file.txt"):
        with open(path, "r") as f:
            c = f.read()
        c = c.replace("opensuse line" + chr(10), "opensuse line" + chr(10) + "sle line" + chr(10))
        with open(path, "w") as f:
            f.write(c)
""")

            orig_editor = os.environ.get("EDITOR")
            try:
                os.environ["EDITOR"] = f"python3 {editor_script}"
                # Rebase with --sle mode
                ok = rebase_patch_cli(tmpdir, sle_mode=True)
                self.assertTrue(ok)
            finally:
                if orig_editor:
                    os.environ["EDITOR"] = orig_editor
                else:
                    os.environ.pop("EDITOR", None)

            # Verification should now show clean SLE status
            res_after = check_patches_sequential(tmpdir)
            self.assertTrue(res_after["success"])
            self.assertEqual(res_after["sle_status"]["clean"], 1)
            self.assertEqual(res_after["sle_status"]["failed"], 0)

            # Changes file should have recorded the rebase
            with open(changes_path, "r") as f:
                c = f.read()
            self.assertIn("- Rebase sle.patch.", c)

if __name__ == '__main__':
    unittest.main()
