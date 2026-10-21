#!/usr/bin/env python3
"""
Unit tests for helpers/pool_submit.py:
Central Pool Submit Helper and Tree-Wide Pending SR Auditor.
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock
import tempfile
import subprocess
import json

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
GECKOPIT_DIR = os.path.dirname(TESTS_DIR)
HELPERS_DIR = os.path.join(GECKOPIT_DIR, "helpers")

for p in [GECKOPIT_DIR, HELPERS_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

import pool_submit


class TestPoolSubmit(unittest.TestCase):

    def test_get_obs_env(self):
        with patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/test-bus"}):
            env = pool_submit.get_obs_env()
            self.assertEqual(env.get("DBUS_SESSION_BUS_ADDRESS"), "unix:path=/tmp/test-bus")

        with patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": ""}, clear=True):
            with patch("os.path.exists", return_value=True):
                env = pool_submit.get_obs_env()
                self.assertTrue(env.get("DBUS_SESSION_BUS_ADDRESS", "").startswith("unix:path="))

    def test_is_project_git_managed(self):
        # 1. Project with scmsync
        xml_with_scmsync = """<project name="GNOME:Factory">
  <scmsync>https://src.opensuse.org/GNOME/_ObsPrj#factory</scmsync>
</project>"""
        mock_res = MagicMock(stdout=xml_with_scmsync, returncode=0)
        with patch("subprocess.run", return_value=mock_res):
            res = pool_submit.is_project_git_managed("GNOME:Factory", force_refresh=True)
            self.assertTrue(res["is_git"])
            self.assertEqual(res["scmsync_url"], "https://src.opensuse.org/GNOME/_ObsPrj#factory")

        # 2. Project without scmsync
        xml_without_scmsync = """<project name="openSUSE:Factory">
  <title>The openSUSE Target Distribution</title>
</project>"""
        mock_res_no = MagicMock(stdout=xml_without_scmsync, returncode=0)
        with patch("subprocess.run", return_value=mock_res_no):
            res = pool_submit.is_project_git_managed("openSUSE:Factory", force_refresh=True)
            self.assertFalse(res["is_git"])
            self.assertIsNone(res["scmsync_url"])

    def test_parse_obs_requests_collection(self):
        sample_xml = """<collection matches="2">
  <request id="1001" creator="packager1">
    <action type="submit">
      <source project="GNOME:Factory" package="mutter" rev="abc1234"/>
      <target project="openSUSE:Factory" package="mutter"/>
    </action>
    <state name="review" who="staging_bot" when="2026-10-20T10:00:00"/>
    <description>Update mutter to 51.1</description>
  </request>
  <request id="1002" creator="packager2">
    <action type="submit">
      <source project="devel:languages:python" package="python-foo" rev="def5678"/>
      <target project="openSUSE:Factory" package="python-foo"/>
      <acceptinfo rev="10" srcmd5="md5src" osrcmd5="md5orig"/>
    </action>
    <state name="accepted" who="release_mgr" when="2026-10-20T12:00:00"/>
    <description>Update python-foo</description>
  </request>
</collection>"""
        parsed = pool_submit.parse_obs_requests_collection(sample_xml, target_project="openSUSE:Factory")
        self.assertIn("mutter", parsed)
        self.assertEqual(len(parsed["mutter"]), 1)
        self.assertEqual(parsed["mutter"][0]["id"], "1001")
        self.assertEqual(parsed["mutter"][0]["state"], "review")
        self.assertEqual(parsed["mutter"][0]["source_project"], "GNOME:Factory")
        self.assertEqual(parsed["mutter"][0]["description"], "Update mutter to 51.1")

        self.assertIn("python-foo", parsed)
        self.assertEqual(parsed["python-foo"][0]["id"], "1002")
        self.assertEqual(parsed["python-foo"][0]["state"], "accepted")
        self.assertIsNotNone(parsed["python-foo"][0]["acceptinfo"])

    def test_get_package_pool_submission_status(self):
        pending = {
            "mutter": [{"id": "1001", "state": "review", "source_project": "GNOME:Factory"}]
        }
        accepted = {
            "simdutf": [{"id": "999", "state": "accepted", "source_project": "GNOME:Factory", "state_when": "2026-10-18T10:00:00"}]
        }

        # 1. Pending request
        st_mutter = pool_submit.get_package_pool_submission_status("mutter", pending_requests=pending, accepted_requests=accepted)
        self.assertEqual(st_mutter["status"], "pending")
        self.assertEqual(st_mutter["request_id"], "1001")

        # 2. Accepted request pending sync
        st_simdutf = pool_submit.get_package_pool_submission_status("simdutf", pending_requests=pending, accepted_requests=accepted)
        self.assertEqual(st_simdutf["status"], "accepted_pending_sync")
        self.assertEqual(st_simdutf["request_id"], "999")

        # 3. Unsubmitted
        st_other = pool_submit.get_package_pool_submission_status("other", pending_requests=pending, accepted_requests=accepted)
        self.assertEqual(st_other["status"], "unsubmitted")

    def test_is_accepted_sr_already_synced(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sr = {
                "id": "1234",
                "state": "accepted",
                "description": "- Update to version 2.0\n  * Fix bug abc"
            }

            # 1. When git log pool_ref finds the subject -> True
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(stdout="abc1234 Update to version 2.0", returncode=0)
                synced = pool_submit.is_accepted_sr_already_synced(tmpdir, sr, stable_branch="factory")
                self.assertTrue(synced)

            # 2. When not found in pool_ref -> False
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(stdout="", returncode=0)
                synced = pool_submit.is_accepted_sr_already_synced(tmpdir, sr, stable_branch="factory")
                self.assertFalse(synced)

    def test_resolve_source_obs_project(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Fallback default
            self.assertEqual(pool_submit.resolve_source_obs_project(tmpdir, default="GNOME:Factory"), "GNOME:Factory")

            # 2. .osc/_project
            osc_dir = os.path.join(tmpdir, ".osc")
            os.makedirs(osc_dir)
            with open(os.path.join(osc_dir, "_project"), "w") as f:
                f.write("multimedia:libs\n")
            self.assertEqual(pool_submit.resolve_source_obs_project(tmpdir), "multimedia:libs")

            # 3. staging.config in parent
            subpkg_dir = os.path.join(tmpdir, "mypkg")
            os.makedirs(subpkg_dir)
            with open(os.path.join(tmpdir, "staging.config"), "w") as f:
                json.dump({"ObsProject": "X11:Wayland"}, f)
            self.assertEqual(pool_submit.resolve_source_obs_project(subpkg_dir), "X11:Wayland")

    def test_extract_pool_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Mock git commands in tmpdir
            diff_output = """diff --git a/test.changes b/test.changes
--- a/test.changes
+++ b/test.changes
@@ -1,3 +1,6 @@
+-------------------------------------------------------------------
+Wed Oct 21 08:00:00 UTC 2026 - test@example.com
+
+- Update to version 1.2.3:
+  * Fixed bug foo
+  * Added feature bar
"""
            with patch("subprocess.run") as mock_run:
                # Return success for rev-parse and diff
                def run_side_effect(cmd, **kwargs):
                    if "rev-parse" in cmd:
                        return MagicMock(returncode=0)
                    if "diff" in cmd:
                        return MagicMock(stdout=diff_output, returncode=0)
                    return MagicMock(stdout="", returncode=0)
                mock_run.side_effect = run_side_effect

                changes = pool_submit.extract_pool_changes(tmpdir, stable_branch="factory")
                self.assertIn("Update to version 1.2.3:", changes)
                self.assertIn("Fixed bug foo", changes)
                self.assertNotIn("Wed Oct 21", changes)

    def test_submit_package_to_pool_preflight_guards(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Directory does not exist
            ok, msg = pool_submit.submit_package_to_pool("pkg", os.path.join(tmpdir, "nonexistent"))
            self.assertFalse(ok)
            self.assertIn("does not exist", msg)

            # 2. Dirty worktree
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(stdout=" M test.spec\n", returncode=0)
                ok, msg = pool_submit.submit_package_to_pool("pkg", tmpdir)
                self.assertFalse(ok)
                self.assertIn("uncommitted changes", msg)

            # 3. Pending SR guard
            with patch("subprocess.run") as mock_run:
                def side_effect(cmd, **kwargs):
                    if "status" in cmd:
                        return MagicMock(stdout="", returncode=0)
                    if "rev-list" in cmd:
                        if "origin/factory..HEAD" in cmd:
                            return MagicMock(stdout="0", returncode=0)
                        return MagicMock(stdout="1", returncode=0)
                    return MagicMock(stdout="", returncode=0)
                mock_run.side_effect = side_effect
                with patch("pool_submit.get_package_pool_submission_status", return_value={
                    "status": "pending",
                    "request": {"id": "12345", "state": "review", "source_project": "GNOME:Factory"}
                }):
                    ok, msg = pool_submit.submit_package_to_pool("pkg", tmpdir)
                    self.assertFalse(ok)
                    self.assertIn("request #12345", msg)
                    self.assertIn("already pending", msg)

            # 4. Accepted SR guard
            with patch("subprocess.run") as mock_run:
                def side_effect_acc(cmd, **kwargs):
                    if "status" in cmd:
                        return MagicMock(stdout="", returncode=0)
                    if "rev-list" in cmd:
                        if "origin/factory..HEAD" in cmd:
                            return MagicMock(stdout="0", returncode=0)
                        return MagicMock(stdout="1", returncode=0)
                    return MagicMock(stdout="", returncode=0)
                mock_run.side_effect = side_effect_acc
                with patch("pool_submit.get_package_pool_submission_status", return_value={
                    "status": "accepted_pending_sync",
                    "request": {"id": "12346", "state": "accepted", "accepted_when": "yesterday"}
                }):
                    ok, msg = pool_submit.submit_package_to_pool("pkg", tmpdir)
                    self.assertFalse(ok)
                    self.assertIn("request #12346 was already accepted", msg)

            # 5. Git managed target (scmsync)
            with patch("subprocess.run") as mock_run:
                def side_effect_git(cmd, **kwargs):
                    if "status" in cmd:
                        return MagicMock(stdout="", returncode=0)
                    if "rev-list" in cmd:
                        if "origin/factory..HEAD" in cmd:
                            return MagicMock(stdout="0", returncode=0)
                        return MagicMock(stdout="1", returncode=0)
                    return MagicMock(stdout="", returncode=0)
                mock_run.side_effect = side_effect_git
                with patch("pool_submit.get_package_pool_submission_status", return_value={"status": "unsubmitted"}):
                    with patch("pool_submit.is_project_git_managed", return_value={"is_git": True, "scmsync_url": "https://src.opensuse.org/foo"}):
                        ok, msg = pool_submit.submit_package_to_pool("pkg", tmpdir)
                        self.assertFalse(ok)
                        self.assertIn("Git-managed", msg)
                        self.assertIn("Gitea Pull Request", msg)

            # 6. Dry run success
            with patch("subprocess.run") as mock_run:
                def side_effect_dry(cmd, **kwargs):
                    if "status" in cmd:
                        return MagicMock(stdout="", returncode=0)
                    if "rev-list" in cmd:
                        if "origin/factory..HEAD" in cmd:
                            return MagicMock(stdout="0", returncode=0)
                        return MagicMock(stdout="1", returncode=0)
                    return MagicMock(stdout="", returncode=0)
                mock_run.side_effect = side_effect_dry
                with patch("pool_submit.get_package_pool_submission_status", return_value={"status": "unsubmitted"}):
                    with patch("pool_submit.is_project_git_managed", return_value={"is_git": False}):
                        with patch("pool_submit.extract_pool_changes", return_value="Update to 1.0"):
                            with patch("pool_submit.resolve_source_obs_project", return_value="GNOME:Factory"):
                                ok, msg = pool_submit.submit_package_to_pool("pkg", tmpdir, dry_run=True)
                                self.assertTrue(ok)
                                self.assertIn("[DRY-RUN] Would execute: osc sr GNOME:Factory pkg openSUSE:Factory", msg)


if __name__ == "__main__":
    unittest.main()
