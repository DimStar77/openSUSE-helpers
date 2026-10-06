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

            # gio-2.0 was unversioned in spec -> detected as unversioned by default
            self.assertIn("gio-2.0", drift_types)
            self.assertEqual(drift_types["gio-2.0"], "unversioned")

            # libxml-2.0 mapped to libxml2-devel bumped from 2.10.0 to 2.12.0
            self.assertIn("libxml-2.0", drift_types)
            self.assertEqual(drift_types["libxml-2.0"], "bump")

            # json-glib-1.0 is missing from spec -> excluded in bumps_only mode to prevent false positives from -Dqt=false etc.
            self.assertNotIn("json-glib-1.0", drift_types)

            summary = meson_drift.format_drift_short_summary(drifts)
            self.assertIn("2 version bumps", summary)
            self.assertIn("1 unversioned", summary)

            # Test include_unversioned=False
            strict_bumps = meson_drift.audit_meson_drift(td, meson_content=meson_code, bumps_only=True, include_unversioned=False)
            strict_drift_types = {d["package"]: d["type"] for d in strict_bumps}
            self.assertNotIn("gio-2.0", strict_drift_types)
            self.assertIn("gtk4", strict_drift_types)

            # Test bumps_only=False (comprehensive mode, includes missing)
            all_drifts = meson_drift.audit_meson_drift(td, meson_content=meson_code, bumps_only=False)
            all_drift_types = {d["package"]: d["type"] for d in all_drifts}
            self.assertIn("gio-2.0", all_drift_types)
            self.assertIn("json-glib-1.0", all_drift_types)

    def test_audit_meson_drift_obs_scm_clone_subdir(self):
        service_code = """<services>
  <service name="obs_scm">
    <param name="url">https://gitlab.gnome.org/GNOME/zenity.git</param>
    <param name="scm">git</param>
    <param name="versionformat">@PARENT_TAG@</param>
  </service>
</services>"""
        spec_code = """Name: zenity
Version: 4.2.0
Release: 0
BuildRequires:  pkgconfig(gtk4) >= 4.14.0
BuildRequires:  pkgconfig(libadwaita-1)
"""
        meson_code = """
        dependency('gtk4', version: '>= 4.14.0')
        dependency('libadwaita-1', version: '>= 1.2')
        """
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "_service"), "w", encoding="utf-8") as f:
                f.write(service_code)
            with open(os.path.join(td, "zenity.spec"), "w", encoding="utf-8") as f:
                f.write(spec_code)

            clone_dir = os.path.join(td, "zenity")
            os.makedirs(clone_dir)
            with open(os.path.join(clone_dir, "meson.build"), "w", encoding="utf-8") as f:
                f.write(meson_code)

            # Verify audit detects libadwaita-1 unversioned from zenity/meson.build
            drifts = meson_drift.audit_meson_drift(td)
            self.assertEqual(len(drifts), 1)
            self.assertEqual(drifts[0]["package"], "libadwaita-1")
            self.assertEqual(drifts[0]["type"], "unversioned")
            self.assertEqual(drifts[0]["upstream_version"], "1.2")

            # Verify fix_meson_drift updates zenity.spec
            fixed = meson_drift.fix_meson_drift(td)
            self.assertEqual(len(fixed), 1)
            with open(os.path.join(td, "zenity.spec"), "r", encoding="utf-8") as f:
                new_spec = f.read()
            self.assertIn("BuildRequires:  pkgconfig(libadwaita-1) >= 1.2\n", new_spec)
            self.assertNotIn("\x01", new_spec)


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

            with open(os.path.join(td, "meson.build"), "w", encoding="utf-8") as f:
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

    def test_audit_ignores_osc_collab_diff(self):
        """Verifies audit does not get misled by partial diff hunks in osc-collab.meson."""
        spec_code = """Name: testpkg
Version: 1.0.1
Release: 0
BuildRequires:  pkgconfig(gtk4) >= 4.14.0
BuildRequires:  pkgconfig(libadwaita-1) >= 1.5.0
"""
        full_meson = """
        dependency('gtk4', version: '>= 4.16.0')
        dependency('libadwaita-1', version: '>= 1.5.0')
"""
        # osc-collab.meson only has version bump hunk, omitting unchanged dependency lines
        diff_hunk = """--- meson.build.old
+++ meson.build
@@ -1,3 +1,3 @@
 project('testpkg', 'c',
-  version: '1.0.0'
+  version: '1.0.1'
 )
"""
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "testpkg.spec"), "w", encoding="utf-8") as f:
                f.write(spec_code)
            with open(os.path.join(td, "meson.build"), "w", encoding="utf-8") as f:
                f.write(full_meson)
            with open(os.path.join(td, "osc-collab.meson"), "w", encoding="utf-8") as f:
                f.write(diff_hunk)

            drifts = meson_drift.audit_meson_drift(td)
            self.assertEqual(len(drifts), 1)
            self.assertEqual(drifts[0]["package"], "gtk4")
            self.assertEqual(drifts[0]["upstream_version"], "4.16.0")

    def test_detect_package_build_system(self):
        # 1. RPM 4.20+ Declarative BuildSystem tag
        self.assertEqual(meson_drift.detect_package_build_system("Name: foo\nBuildSystem: meson\n"), "meson")
        self.assertEqual(meson_drift.detect_package_build_system("Name: foo\nBuildSystem: cmake\n"), "cmake")
        self.assertEqual(meson_drift.detect_package_build_system("Name: foo\nBuildSystem: autotools\n"), "autotools")

        # 2. Build invocation macros
        self.assertEqual(meson_drift.detect_package_build_system("Name: foo\n%build\n%meson\n%meson_build\n"), "meson")
        self.assertEqual(meson_drift.detect_package_build_system("Name: foo\n%build\n%cmake\n%cmake_build\n"), "cmake")
        self.assertEqual(meson_drift.detect_package_build_system("Name: foo\n%build\n%configure\nmake\n"), "autotools")

        # 3. Build isolation: when spec has %meson, ignore any legacy autotools/cmake files
        spec_multi = """Name: multi
BuildRequires: meson
%build
%meson
%meson_build
"""
        self.assertEqual(meson_drift.detect_package_build_system(spec_multi), "meson")

    def test_cmake_dependency_drift_and_spec_fix(self):
        cmake_code = """
        set(soup_minimum_version 3.1.1)
        pkg_check_modules(PLATFORM REQUIRED libsoup-3.0>=${soup_minimum_version})
        """
        spec_code = """Name: cmakepkg
Version: 1.0.0
Release: 0
BuildRequires:  pkgconfig(libsoup-3.0) >= 2.58
%build
%cmake
%cmake_build
"""
        with tempfile.TemporaryDirectory() as td:
            spec_file = os.path.join(td, "cmakepkg.spec")
            changes_file = os.path.join(td, "cmakepkg.changes")
            with open(spec_file, "w", encoding="utf-8") as f:
                f.write(spec_code)
            with open(changes_file, "w", encoding="utf-8") as f:
                f.write("-------------------------------------------------------------------\nWed Sep 23 12:00:00 UTC 2026 - test@opensuse.org\n\n- Initial packaging.\n\n")
            with open(os.path.join(td, "CMakeLists.txt"), "w", encoding="utf-8") as f:
                f.write(cmake_code)

            # Audit
            drifts = meson_drift.audit_meson_drift(td)
            self.assertEqual(len(drifts), 1)
            self.assertEqual(drifts[0]["package"], "libsoup-3.0")
            self.assertEqual(drifts[0]["type"], "bump")
            self.assertEqual(drifts[0]["upstream_version"], "3.1.1")
            self.assertEqual(drifts[0]["build_system"], "cmake")

            # Verify report header
            report = meson_drift.format_drift_cli_report(drifts)
            self.assertIn("CMake Dependency Drift Detected", report)

            # Fix
            fixed = meson_drift.fix_meson_drift(td)
            self.assertEqual(len(fixed), 1)
            with open(spec_file, "r", encoding="utf-8") as f:
                new_spec = f.read()
            self.assertIn("BuildRequires:  pkgconfig(libsoup-3.0) >= 3.1.1", new_spec)

            # Record changelog
            ok = meson_drift.record_drift_changelog(td, fixed)
            self.assertTrue(ok)
            with open(changes_file, "r", encoding="utf-8") as f:
                new_changes = f.read()
            self.assertIn("- Update version dependencies according to CMakeLists.txt.", new_changes)

    def test_autotools_dependency_drift_and_spec_fix(self):
        conf_code = """
        m4_define([atk_req_ver], [1.29.2])
        GLIB_REQ=2.50.0
        PKG_CHECK_MODULES(FOO, [
            glib-2.0 >= $GLIB_REQ
            atk >= atk_req_ver
            unknown >= unresolvable_variable
        ])
        """
        spec_code = """Name: autopkg
Version: 1.0.0
Release: 0
BuildRequires:  pkgconfig(glib-2.0)
BuildRequires:  atk-devel
BuildRequires:  unknown
%build
%configure
make %{?_smp_mflags}
"""
        with tempfile.TemporaryDirectory() as td:
            spec_file = os.path.join(td, "autopkg.spec")
            changes_file = os.path.join(td, "autopkg.changes")
            with open(spec_file, "w", encoding="utf-8") as f:
                f.write(spec_code)
            with open(changes_file, "w", encoding="utf-8") as f:
                f.write("-------------------------------------------------------------------\nWed Sep 23 12:00:00 UTC 2026 - test@opensuse.org\n\n- Initial packaging.\n\n")
            with open(os.path.join(td, "configure.ac"), "w", encoding="utf-8") as f:
                f.write(conf_code)

            # Audit: glib-2.0 ($GLIB_REQ) and atk (bareword m4 atk_req_ver), unresolvable_variable is ignored
            drifts = meson_drift.audit_meson_drift(td)
            self.assertEqual(len(drifts), 2)
            d_pkgs = {d["package"]: d for d in drifts}
            self.assertIn("glib-2.0", d_pkgs)
            self.assertEqual(d_pkgs["glib-2.0"]["upstream_version"], "2.50.0")
            self.assertIn("atk", d_pkgs)
            self.assertEqual(d_pkgs["atk"]["spec_name"], "atk-devel")
            self.assertEqual(d_pkgs["atk"]["upstream_version"], "1.29.2")

            # Verify unresolvable_variable was not accepted as a version
            self.assertNotIn("unknown", d_pkgs)

            # Verify report header
            report = meson_drift.format_drift_cli_report(drifts)
            self.assertIn("Autotools Dependency Drift Detected", report)

            # Fix
            fixed = meson_drift.fix_meson_drift(td)
            self.assertEqual(len(fixed), 2)
            with open(spec_file, "r", encoding="utf-8") as f:
                new_spec = f.read()
            self.assertIn("BuildRequires:  pkgconfig(glib-2.0) >= 2.50.0", new_spec)
            self.assertIn("BuildRequires:  atk-devel >= 1.29.2", new_spec)
            self.assertIn("BuildRequires:  unknown", new_spec)

            # Record changelog
            ok = meson_drift.record_drift_changelog(td, fixed)
            self.assertTrue(ok)
            with open(changes_file, "r", encoding="utf-8") as f:
                new_changes = f.read()
            self.assertIn("- Update version dependencies according to configure.ac.", new_changes)


    def test_python_dependency_drift_and_spec_fix(self):
        pyproject_code = """[build-system]
build-backend = "mesonpy"
requires = ["meson-python>=0.12.1", "pygobject>=2.90.1"]

[project]
name = "PyAtspi"
dependencies = [
    "pygobject>=2.90.1"
]
"""
        spec_code = """Name: python-testatspi
Version: 1.0.0
Release: 0
BuildRequires:  %{python_module meson-python}
BuildRequires:  %{python_module gobject >= 2.90.1}
BuildRequires:  python-rpm-macros
%build
%pyproject_wheel
%install
%pyproject_install
"""
        with tempfile.TemporaryDirectory() as td:
            spec_file = os.path.join(td, "python-testatspi.spec")
            changes_file = os.path.join(td, "python-testatspi.changes")
            with open(spec_file, "w", encoding="utf-8") as f:
                f.write(spec_code)
            with open(changes_file, "w", encoding="utf-8") as f:
                f.write("-------------------------------------------------------------------\nWed Sep 23 12:00:00 UTC 2026 - test@opensuse.org\n\n- Initial packaging.\n\n")
            with open(os.path.join(td, "pyproject.toml"), "w", encoding="utf-8") as f:
                f.write(pyproject_code)

            # Audit
            drifts = meson_drift.audit_meson_drift(td)
            self.assertEqual(len(drifts), 1)
            self.assertEqual(drifts[0]["package"], "meson-python")
            self.assertEqual(drifts[0]["type"], "unversioned")
            self.assertEqual(drifts[0]["upstream_version"], "0.12.1")
            self.assertEqual(drifts[0]["build_system"], "python")

            # Report
            report = meson_drift.format_drift_cli_report(drifts)
            self.assertIn("Python (pyproject.toml) Dependency Drift Detected", report)

            # Fix
            fixed = meson_drift.fix_meson_drift(td)
            self.assertEqual(len(fixed), 1)
            with open(spec_file, "r", encoding="utf-8") as f:
                new_spec = f.read()
            self.assertIn("BuildRequires:  %{python_module meson-python >= 0.12.1}", new_spec)

            # Changelog
            ok = meson_drift.record_drift_changelog(td, fixed)
            self.assertTrue(ok)
            with open(changes_file, "r", encoding="utf-8") as f:
                new_changes = f.read()
            self.assertIn("- Update version dependencies according to pyproject.toml.", new_changes)

    def test_python_poetry_dependency_drift(self):
        pyproject_code = """[tool.poetry]
name = "pygtkspellcheck"
version = "5.0.4"

[tool.poetry.dependencies]
python = "^3.7"
pyenchant = "^3.0"
PyGObject = "^3.42.1"

[build-system]
requires = ["poetry_core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
"""
        spec_code = """Name: python-spell
Version: 5.0.4
Release: 0
BuildRequires:  %{python_module poetry-core >= 0.9.0}
BuildRequires:  %{python_module pyenchant >= 2.0.0}
BuildRequires:  python-rpm-macros
%build
%pyproject_wheel
"""
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "python-spell.spec"), "w", encoding="utf-8") as f:
                f.write(spec_code)
            with open(os.path.join(td, "pyproject.toml"), "w", encoding="utf-8") as f:
                f.write(pyproject_code)

            drifts = meson_drift.audit_meson_drift(td)
            self.assertEqual(len(drifts), 2)
            pkgs = {d["package"]: d for d in drifts}
            self.assertIn("poetry-core", pkgs)
            self.assertEqual(pkgs["poetry-core"]["type"], "bump")
            self.assertEqual(pkgs["poetry-core"]["upstream_version"], "1.0.0")
            self.assertIn("pyenchant", pkgs)
            self.assertEqual(pkgs["pyenchant"]["type"], "bump")
            self.assertEqual(pkgs["pyenchant"]["upstream_version"], "3.0")

    def test_hybrid_meson_python_package(self):
        meson_code = "dependency('glib-2.0', version: '>= 2.40.0')"
        pyproject_code = """[build-system]
requires = ["meson-python>=0.14.0"]
"""
        spec_code = """Name: python-hybrid
Version: 1.0.0
Release: 0
BuildRequires:  %{python_module meson-python}
BuildRequires:  pkgconfig(glib-2.0) >= 2.36.0
%build
%meson
%meson_build
%pyproject_wheel
"""
        with tempfile.TemporaryDirectory() as td:
            spec_file = os.path.join(td, "python-hybrid.spec")
            changes_file = os.path.join(td, "python-hybrid.changes")
            with open(spec_file, "w", encoding="utf-8") as f:
                f.write(spec_code)
            with open(changes_file, "w", encoding="utf-8") as f:
                f.write("-------------------------------------------------------------------\nWed Sep 23 12:00:00 UTC 2026 - test@opensuse.org\n\n- Initial packaging.\n\n")
            with open(os.path.join(td, "meson.build"), "w", encoding="utf-8") as f:
                f.write(meson_code)
            with open(os.path.join(td, "pyproject.toml"), "w", encoding="utf-8") as f:
                f.write(pyproject_code)

            drifts = meson_drift.audit_meson_drift(td)
            self.assertEqual(len(drifts), 2)
            report = meson_drift.format_drift_cli_report(drifts)
            self.assertIn("Meson & Python (pyproject.toml) Dependency Drift Detected", report)

            fixed = meson_drift.fix_meson_drift(td)
            self.assertEqual(len(fixed), 2)
            with open(spec_file, "r", encoding="utf-8") as f:
                new_spec = f.read()
            self.assertIn("BuildRequires:  %{python_module meson-python >= 0.14.0}", new_spec)
            self.assertIn("BuildRequires:  pkgconfig(glib-2.0) >= 2.40.0", new_spec)

            ok = meson_drift.record_drift_changelog(td, fixed)
            self.assertTrue(ok)
            with open(changes_file, "r", encoding="utf-8") as f:
                new_changes = f.read()
            self.assertIn("- Update version dependencies according to meson.build and pyproject.toml.", new_changes)



    def test_parse_meson_dependencies_numeric_format_variables(self):
        """Tests that integer variables and multi-variable .format() calls resolve to semantic versions."""
        content = """
        glib_major_req = 2
        glib_minor_req = 57
        glib_micro_req = 2
        glib_req = '>= @0@.@1@.@2@'.format(glib_major_req, glib_minor_req, glib_micro_req)

        dependency('glib-2.0', version: glib_req)
        dependency('gmodule-2.0', version: glib_req)
        """
        deps = meson_drift.parse_meson_dependencies(content)
        self.assertIn("glib-2.0", deps)
        self.assertEqual(deps["glib-2.0"], (">=", "2.57.2", True))
        self.assertIn("gmodule-2.0", deps)
        self.assertEqual(deps["gmodule-2.0"], (">=", "2.57.2", True))

    def test_audit_and_fix_unversioned_dependency_with_numeric_format(self):
        """Tests that audit and fix properly resolve and update unversioned BuildRequires from formatted variables."""
        spec_code = """Name: gtk3
Version: 3.24.52
BuildSystem: meson
BuildRequires:  pkgconfig(glib-2.0) >= 2.57.2
BuildRequires:  pkgconfig(gmodule-2.0)
"""
        meson_code = """
        glib_major_req = 2
        glib_minor_req = 57
        glib_micro_req = 2
        glib_req = '>= @0@.@1@.@2@'.format(glib_major_req, glib_minor_req, glib_micro_req)
        dependency('glib-2.0', version: glib_req)
        dependency('gmodule-2.0', version: glib_req)
        """
        with tempfile.TemporaryDirectory() as td:
            spec_file = os.path.join(td, "gtk3.spec")
            changes_file = os.path.join(td, "gtk3.changes")
            with open(spec_file, "w", encoding="utf-8") as f:
                f.write(spec_code)
            with open(changes_file, "w", encoding="utf-8") as f:
                f.write("-------------------------------------------------------------------\nWed Sep 23 12:00:00 UTC 2026 - test@opensuse.org\n\n- Initial packaging.\n\n")
            with open(os.path.join(td, "meson.build"), "w", encoding="utf-8") as f:
                f.write(meson_code)

            drifts = meson_drift.audit_meson_drift(td)
            self.assertEqual(len(drifts), 1)
            self.assertEqual(drifts[0]["package"], "gmodule-2.0")
            self.assertEqual(drifts[0]["upstream_version"], "2.57.2")
            self.assertEqual(drifts[0]["type"], "unversioned")

            fixed = meson_drift.fix_meson_drift(td)
            self.assertEqual(len(fixed), 1)
            with open(spec_file, "r", encoding="utf-8") as f:
                new_spec = f.read()
            self.assertIn("BuildRequires:  pkgconfig(gmodule-2.0) >= 2.57.2", new_spec)

            remaining = meson_drift.audit_meson_drift(td)
            self.assertEqual(len(remaining), 0)


if __name__ == "__main__":
    unittest.main()
