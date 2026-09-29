"""
Unit tests for Meson Dependency Drift Auditor
"""

import unittest
import tempfile
import os
import sys

HELPERS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "helpers")
if HELPERS_DIR not in sys.path:
    sys.path.insert(0, HELPERS_DIR)

import meson_drift


class TestMesonDriftAuditor(unittest.TestCase):

    def test_parse_meson_dependencies_simple(self):
        content = """
        dependency('gtk4', version: '>= 4.16.0')
        dependency('libadwaita-1', version: '>= 1.6.0', required: true)
        dependency('vulkan', version: '>= 1.3', required: false)
        dependency('json-glib-1.0', '>= 1.6.0')
        """
        deps = meson_drift.parse_meson_dependencies(content)
        self.assertIn("gtk4", deps)
        self.assertEqual(deps["gtk4"], (">=", "4.16.0", True))
        self.assertIn("libadwaita-1", deps)
        self.assertEqual(deps["libadwaita-1"], (">=", "1.6.0", True))
        self.assertIn("vulkan", deps)
        self.assertEqual(deps["vulkan"], (">=", "1.3", False))
        self.assertIn("json-glib-1.0", deps)
        self.assertEqual(deps["json-glib-1.0"], (">=", "1.6.0", True))

    def test_parse_meson_dependencies_concatenation(self):
        content = """
        glib_ver = '2.82.0'
        dependency('glib-2.0', version: '>= ' + glib_ver)
        """
        deps = meson_drift.parse_meson_dependencies(content)
        self.assertIn("glib-2.0", deps)
        self.assertEqual(deps["glib-2.0"], (">=", "2.82.0", True))

    def test_parse_meson_dependencies_array(self):
        content = """
        dependency('cairo', version: ['>= 1.16.0', '< 2.0.0'])
        """
        deps = meson_drift.parse_meson_dependencies(content)
        self.assertIn("cairo", deps)
        self.assertEqual(deps["cairo"], (">=", "1.16.0", True))

    def test_parse_meson_dependencies_unified_diff(self):
        diff = """--- meson.old
+++ meson.new
@@ -5,3 +5,3 @@
-gtk_ver = '>= 4.14.0'
+gtk_ver = '>= 4.16.0'
-dependency('gtk4', version: gtk_ver)
+dependency('gtk4', version: gtk_ver)
+dependency('soup-3.0', version: '>= 3.4.0')
"""
        deps = meson_drift.parse_meson_dependencies(diff)
        self.assertIn("gtk4", deps)
        self.assertEqual(deps["gtk4"][1], "4.16.0")
        self.assertIn("soup-3.0", deps)
        self.assertEqual(deps["soup-3.0"][1], "3.4.0")

    def test_parse_spec_build_requires(self):
        spec = """
        Name: mypkg
        Version: 1.0.0
        Release: 0
        %define min_gtk 4.14.0
        BuildRequires:  pkgconfig(gtk4) >= %{min_gtk}
        BuildRequires:  pkgconfig(libadwaita-1) >= 1.5.0
        BuildRequires:  pkgconfig(gio-2.0)
        BuildRequires:  libxml2-devel >= 2.10.0
        """
        spec_deps = meson_drift.parse_spec_build_requires(spec)
        self.assertIn("gtk4", spec_deps)
        self.assertEqual(spec_deps["gtk4"][1], "4.14.0")
        self.assertIn("libadwaita-1", spec_deps)
        self.assertEqual(spec_deps["libadwaita-1"][1], "1.5.0")
        self.assertIn("gio-2.0", spec_deps)
        self.assertEqual(spec_deps["gio-2.0"][1], "")
        self.assertIn("libxml2-devel", spec_deps)
        self.assertEqual(spec_deps["libxml2-devel"][1], "2.10.0")

    def test_audit_meson_drift_bump_and_missing(self):
        meson_code = """
        dependency('gtk4', version: '>= 4.16.0')
        dependency('libadwaita-1', version: '>= 1.5.0')
        dependency('gio-2.0', version: '>= 2.80.0')
        dependency('libxml-2.0', version: '>= 2.12.0')
        dependency('json-glib-1.0', version: '>= 1.6.0')
        """
        spec_code = """
        Name: testpkg
        Version: 1.0.0
        Release: 0
        BuildRequires:  pkgconfig(gtk4) >= 4.14.0
        BuildRequires:  pkgconfig(libadwaita-1) >= 1.5.0
        BuildRequires:  pkgconfig(gio-2.0)
        BuildRequires:  libxml2-devel >= 2.10.0
        """
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "testpkg.spec"), "w", encoding="utf-8") as f:
                f.write(spec_code)

            # Test default bumps_only=True (zero false positives from unconfigured options)
            drifts = meson_drift.audit_meson_drift(td, meson_content=meson_code)
            drift_types = {d["package"]: d["type"] for d in drifts}

            # gtk4 bumped from 4.14.0 to 4.16.0
            self.assertIn("gtk4", drift_types)
            self.assertEqual(drift_types["gtk4"], "bump")

            # libadwaita-1 is identical (1.5.0 vs 1.5.0) -> no drift
            self.assertNotIn("libadwaita-1", drift_types)

            # gio-2.0 was unversioned in spec -> excluded in bumps_only mode to prevent noise
            self.assertNotIn("gio-2.0", drift_types)

            # libxml-2.0 mapped to libxml2-devel bumped from 2.10.0 to 2.12.0
            self.assertIn("libxml-2.0", drift_types)
            self.assertEqual(drift_types["libxml-2.0"], "bump")

            # json-glib-1.0 is missing from spec -> excluded in bumps_only mode to prevent false positives from -Dqt=false etc.
            self.assertNotIn("json-glib-1.0", drift_types)

            summary = meson_drift.format_drift_short_summary(drifts)
            self.assertIn("2 version bumps", summary)

            # Test bumps_only=False (comprehensive mode)
            all_drifts = meson_drift.audit_meson_drift(td, meson_content=meson_code, bumps_only=False)
            all_drift_types = {d["package"]: d["type"] for d in all_drifts}
            self.assertIn("gio-2.0", all_drift_types)
            self.assertIn("json-glib-1.0", all_drift_types)


    def test_fix_meson_drift_and_changelog(self):
        meson_code = """
        dependency('gtk4', version: '>= 4.16.0')
        dependency('libadwaita-1', version: '>= 1.8.alpha')
        """
        spec_code = """Name: testpkg
Version: 1.0.0
Release: 0
%define min_adw 1.6.alpha
BuildRequires:  pkgconfig(gtk4) >= 4.14.0
BuildRequires:  pkgconfig(libadwaita-1) >= %{min_adw}
"""
        with tempfile.TemporaryDirectory() as td:
            spec_file = os.path.join(td, "testpkg.spec")
            changes_file = os.path.join(td, "testpkg.changes")
            with open(spec_file, "w", encoding="utf-8") as f:
                f.write(spec_code)
            with open(changes_file, "w", encoding="utf-8") as f:
                f.write("-------------------------------------------------------------------\nWed Sep 23 12:00:00 UTC 2026 - test@opensuse.org\n\n- Initial packaging.\n\n")

            with open(os.path.join(td, "osc-collab.meson"), "w", encoding="utf-8") as f:
                f.write(meson_code)

            # Audit before fix: 2 bumps
            drifts = meson_drift.audit_meson_drift(td)
            self.assertEqual(len(drifts), 2)

            # Apply fixes
            fixed = meson_drift.fix_meson_drift(td)
            self.assertEqual(len(fixed), 2)

            # Read back spec and verify
            with open(spec_file, "r", encoding="utf-8") as f:
                new_spec = f.read()
            self.assertIn("BuildRequires:  pkgconfig(gtk4) >= 4.16.0", new_spec)
            self.assertIn("%define min_adw 1.8.alpha", new_spec)

            # Verify 0 drifts remaining
            remaining = meson_drift.audit_meson_drift(td)
            self.assertEqual(len(remaining), 0)

            # Record changelog
            ok = meson_drift.record_drift_changelog(td, fixed)
            self.assertTrue(ok)
            with open(changes_file, "r", encoding="utf-8") as f:
                new_changes = f.read()
            self.assertIn("- Update version dependencies according to meson.build.", new_changes)

if __name__ == "__main__":
    unittest.main()
