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
            # Create a mock .obscpio file
            obscpio = os.path.join(tmpdir, "pkg.obscpio")
            with open(obscpio, "w") as f: f.write("dummy")

            helper = ObsScmUpgradeHelper(tmpdir)
            removed = helper.clean_duplicate_obscpio_if_manual()
            self.assertEqual(removed, ["pkg.obscpio"])
            self.assertFalse(os.path.exists(obscpio))

    def test_clean_duplicate_obscpio_retained_if_buildtime(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service_path = os.path.join(tmpdir, "_service")
            with open(service_path, "w") as f:
                f.write('''<services>
  <service name="tar" mode="buildtime"/>
</services>''')
            obscpio = os.path.join(tmpdir, "pkg.obscpio")
            with open(obscpio, "w") as f: f.write("dummy")

            helper = ObsScmUpgradeHelper(tmpdir)
            removed = helper.clean_duplicate_obscpio_if_manual()
            self.assertEqual(removed, [])
            self.assertTrue(os.path.exists(obscpio))

    @mock.patch("subprocess.run")
    def test_execute_upgrade_dry_run(self, mock_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            service_path = os.path.join(tmpdir, "_service")
            with open(service_path, "w") as f:
                f.write('''<services>
  <service name="obs_scm"><param name="url">https://example.org/pkg.git</param><param name="revision">1.0</param></service>
</services>''')

            helper = ObsScmUpgradeHelper(tmpdir)
            res = helper.execute_upgrade(target_revision="2.0", dry_run=True)
            self.assertTrue(res.success)
            self.assertIn("DRY-RUN", res.message)
            mock_run.assert_not_called()

    @mock.patch("subprocess.run")
    def test_execute_upgrade_full_mocked(self, mock_run):
        with tempfile.TemporaryDirectory() as tmpdir:
            service_path = os.path.join(tmpdir, "_service")
            with open(service_path, "w") as f:
                f.write('''<services>
  <service name="obs_scm"><param name="url">https://example.org/testpkg.git</param><param name="revision">1.0</param></service>
  <service name="tar" mode="manual"/>
</services>''')

            obsinfo_path = os.path.join(tmpdir, "testpkg.obsinfo")
            with open(obsinfo_path, "w") as f:
                f.write("version: 1.0\ncommit: 111111\n")

            # Mock osc service mr and osc vc calls
            mock_proc = mock.MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = "service success"
            mock_run.return_value = mock_proc

            helper = ObsScmUpgradeHelper(tmpdir)

            # We hook update_service_revision to also update obsinfo to simulate osc service mr run
            def fake_run(*args, **kwargs):
                with open(obsinfo_path, "w") as f:
                    f.write("version: 2.0\ncommit: 222222\n")
                return mock_proc

            mock_run.side_effect = fake_run

            logs = []
            res = helper.execute_upgrade(target_revision="2.0", dry_run=False, on_log=logs.append)
            self.assertTrue(res.success)
            self.assertEqual(res.new_version, "2.0")
            self.assertEqual(res.new_revision, "222222")

if __name__ == '__main__':
    unittest.main()
