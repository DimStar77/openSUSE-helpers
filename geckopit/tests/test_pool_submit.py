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

            # 2. Dirty worktree informs user but does not block
            with patch("subprocess.run") as mock_run:
                def side_effect_dirty(cmd, **kwargs):
                    if "status" in cmd:
                        return MagicMock(stdout=" M test.spec\n", returncode=0)
                    if "rev-list" in cmd:
                        return MagicMock(stdout="1", returncode=0)
                    return MagicMock(stdout="", returncode=0)
                mock_run.side_effect = side_effect_dirty
                with patch("pool_submit.get_package_pool_submission_status", return_value={
                    "status": "pending",
                    "request": {"id": "12345", "state": "review", "source_project": "GNOME:Factory"}
                }):
                    ok, msg = pool_submit.submit_package_to_pool("pkg", tmpdir)
                    self.assertFalse(ok)
                    self.assertIn("request #12345", msg)

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

            # 5. Git managed target (scmsync) transitions to Gitea PR route
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
                        ok, msg = pool_submit.submit_package_to_pool("pkg", tmpdir, dry_run=True)
                        self.assertTrue(ok)
                        self.assertIn("Git-managed", msg)
                        self.assertIn("Pull Request", msg)
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


    def test_submit_package_to_pool_osc(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            osc_dir = os.path.join(tmpdir, ".osc")
            os.makedirs(osc_dir)
            with open(os.path.join(osc_dir, "_project"), "w") as f:
                f.write("graphics\n")
            with open(os.path.join(osc_dir, "_package"), "w") as f:
                f.write("shotwell\n")
            with open(os.path.join(tmpdir, "shotwell.changes"), "w") as f:
                f.write("-------------------------------------------------------------------\n- Update to 33.0\n")

            # 1. Clean check with no differences -> already in sync
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(stdout="", returncode=0)
                ok, msg = pool_submit.submit_package_to_pool("shotwell", tmpdir)
                self.assertFalse(ok)
                self.assertIn("already in sync", msg)

            # 2. Dry run with differences -> triggers osc sr
            with patch("subprocess.run") as mock_run:
                def side_effect(cmd, **kwargs):
                    if "rdiff" in cmd:
                        return MagicMock(stdout="diff content", returncode=0)
                    return MagicMock(stdout="", returncode=0)
                mock_run.side_effect = side_effect
                ok, msg = pool_submit.submit_package_to_pool("shotwell", tmpdir, dry_run=True)
                self.assertTrue(ok)
                self.assertIn("Would execute: osc sr graphics shotwell openSUSE:Factory", msg)

    def test_has_real_obs_diff(self):
        # 1. Diff with only _scmsync.obsinfo and build.specials.obscpio -> False
        meta_diff = """Index: _scmsync.obsinfo
===================================================================
--- _scmsync.obsinfo (revision 1)
+++ _scmsync.obsinfo (deleted)
Index: build.specials.obscpio
===================================================================
Binary file build.specials.obscpio deleted
"""
        with patch("subprocess.run", return_value=MagicMock(stdout=meta_diff, returncode=0)):
            has_diff, _ = pool_submit.has_real_obs_diff("devel:prj", "pkg")
            self.assertFalse(has_diff)

        # 2. Diff with spec or changes -> True
        real_diff = """Index: pkg.changes
===================================================================
--- pkg.changes
+++ pkg.changes
@@ -1 +1,3 @@
+- Update to 1.0
"""
        with patch("subprocess.run", return_value=MagicMock(stdout=real_diff, returncode=0)):
            has_diff, _ = pool_submit.has_real_obs_diff("devel:prj", "pkg")
            self.assertTrue(has_diff)

    def test_parse_scmsync_and_git_remote(self):
        # Test scmsync parsing
        h, r, b = pool_submit.parse_scmsync("https://src.opensuse.org/dimstar/vlc#factory")
        self.assertEqual(h, "src.opensuse.org")
        self.assertEqual(r, "dimstar/vlc")
        self.assertEqual(b, "factory")

        h2, r2, b2 = pool_submit.parse_scmsync("https://src.opensuse.org/GNOME/mutter?trackingbranch=factory#abc1234")
        self.assertEqual(h2, "src.opensuse.org")
        self.assertEqual(r2, "GNOME/mutter")
        self.assertEqual(b2, "factory")

        # Test remote url parsing
        lh1, lr1 = pool_submit.parse_git_remote_url("gitea@src.opensuse.org:dimstar/vlc.git")
        self.assertEqual(lh1, "src.opensuse.org")
        self.assertEqual(lr1, "dimstar/vlc")

        lh2, lr2 = pool_submit.parse_git_remote_url("https://src.opensuse.org/GNOME/mutter")
        self.assertEqual(lh2, "src.opensuse.org")
        self.assertEqual(lr2, "GNOME/mutter")

    def test_verify_scmsync_provenance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create .git marker
            os.makedirs(os.path.join(tmpdir, ".git"))

            pkg_meta_xml = """<package name="vlc" project="multimedia:libs">
  <scmsync>https://src.opensuse.org/dimstar/vlc#factory</scmsync>
</package>"""
            # 1. Matching provenance
            def side_effect(cmd, **kwargs):
                if "meta" in cmd:
                    return MagicMock(stdout=pkg_meta_xml, returncode=0)
                if "remote" in cmd:
                    return MagicMock(stdout="gitea@src.opensuse.org:dimstar/vlc.git\n", returncode=0)
                if "rev-parse" in cmd:
                    return MagicMock(stdout="factory\n", returncode=0)
                return MagicMock(stdout="", returncode=0)

            with patch("subprocess.run", side_effect=side_effect):
                ok, msg = pool_submit.verify_scmsync_provenance(tmpdir, "multimedia:libs", "vlc")
                self.assertTrue(ok)
                self.assertIn("verified", msg.lower())

            # 2. Mismatching remote (fork)
            def side_effect_fork(cmd, **kwargs):
                if "meta" in cmd:
                    return MagicMock(stdout=pkg_meta_xml, returncode=0)
                if "remote" in cmd:
                    return MagicMock(stdout="gitea@src.opensuse.org:someoneelse/vlc.git\n", returncode=0)
                if "rev-parse" in cmd:
                    return MagicMock(stdout="factory\n", returncode=0)
                return MagicMock(stdout="", returncode=0)

            with patch.dict(pool_submit._SCMSYNC_CACHE, clear=True):
                with patch("subprocess.run", side_effect=side_effect_fork):
                    ok, msg = pool_submit.verify_scmsync_provenance(tmpdir, "multimedia:libs", "vlc")
                    self.assertFalse(ok)
                    self.assertIn("does not match current repository", msg)

    def test_get_obs_scmsync_commit(self):
        log_out = """----------------------------------------------------------------------------
r367 | unknown | 2026-10-22 14:14:00 | 6341df4d71d5bcfef3f4adb4394a2231 | 3.0.24 |

[info=30589ec2c0f9ee84102a0c24c4c9a9985cdf72d9e9c95463a7d8e62944293146]
"""
        with patch("subprocess.run", return_value=MagicMock(stdout=log_out, returncode=0)):
            commit = pool_submit.get_obs_scmsync_commit("multimedia:libs", "vlc")
            self.assertEqual(commit, "30589ec2c0f9ee84102a0c24c4c9a9985cdf72d9e9c95463a7d8e62944293146")

    def test_is_accepted_sr_already_synced_with_obs_diff(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sr = {
                "id": "1386327",
                "state": "accepted",
                "source_project": "multimedia:libs",
                "description": ""
            }
            # When real differences exist in OBS between multimedia:libs and openSUSE:Factory
            with patch("pool_submit.has_real_obs_diff", return_value=(True, "diff text")):
                synced = pool_submit.is_accepted_sr_already_synced(tmpdir, sr, stable_branch="factory")
                self.assertTrue(synced)

            # When no differences exist
            with patch("pool_submit.has_real_obs_diff", return_value=(False, "")):
                synced = pool_submit.is_accepted_sr_already_synced(tmpdir, sr, stable_branch="factory")
                self.assertFalse(synced)

    def test_get_git_obs_meta_project(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Without .git -> None
            self.assertIsNone(pool_submit.get_git_obs_meta_project(tmpdir, branch="factory"))

            # 2. With .git and meta.json
            obs_dir = os.path.join(tmpdir, ".git", "obs", "factory")
            os.makedirs(obs_dir)
            with open(os.path.join(obs_dir, "meta.json"), "w") as f:
                json.dump({"project": "multimedia:libs"}, f)

            proj = pool_submit.get_git_obs_meta_project(tmpdir, branch="factory")
            self.assertEqual(proj, "multimedia:libs")

            # 3. Branch without meta.json -> None
            self.assertIsNone(pool_submit.get_git_obs_meta_project(tmpdir, branch="leap-16.1"))

    def test_resolve_source_obs_project_git_obs_meta(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            obs_dir = os.path.join(tmpdir, ".git", "obs", "factory")
            os.makedirs(obs_dir)
            with open(os.path.join(obs_dir, "meta.json"), "w") as f:
                json.dump({"project": "multimedia:libs"}, f)

            proj = pool_submit.resolve_source_obs_project(tmpdir, branch="factory")
            self.assertEqual(proj, "multimedia:libs")

    def test_check_pool_stream_sync(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            def side_effect(cmd, **kwargs):
                if "fetch" in cmd:
                    return MagicMock(returncode=0)
                if "rev-list" in cmd:
                    return MagicMock(stdout="0\n", returncode=0)
                return MagicMock(stdout="", returncode=0)

            with patch("subprocess.run", side_effect=side_effect):
                res = pool_submit.check_pool_stream_sync(tmpdir, branch="leap-16.1", pkg_name="vlc")
                self.assertTrue(res["exists"])
                self.assertEqual(res["ahead"], 0)
                self.assertEqual(res["behind"], 0)

    def test_submit_package_to_pool_stream_creates_pr(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, ".git"))
            def side_effect(cmd, **kwargs):
                if "rev-parse" in cmd and "--abbrev-ref" in cmd:
                    return MagicMock(stdout="leap-16.1\n", returncode=0)
                if "rev-list" in cmd:
                    return MagicMock(stdout="2\n", returncode=0)
                if "remote" in cmd:
                    return MagicMock(stdout="gitea@src.opensuse.org:dimstar/pkg.git\n", returncode=0)
                return MagicMock(stdout="", returncode=0)

            with patch("subprocess.run", side_effect=side_effect):
                with patch("pool_submit.check_pool_stream_sync", return_value={"exists": True, "ahead": 2, "behind": 0}):
                    ok, msg = pool_submit.submit_package_to_pool("pkg", tmpdir, dry_run=True)
                    self.assertTrue(ok)
                    self.assertIn("Pull Request", msg)
                    self.assertIn("pool/pkg:leap-16.1", msg)
    def test_submit_package_to_pool_branch_mismatch_halts_first(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, ".git"))
            # When active branch is my-feature and devel project tracks factory
            def side_effect(cmd, **kwargs):
                if "rev-parse" in cmd and "--abbrev-ref" in cmd:
                    return MagicMock(stdout="my-feature\n", returncode=0)
                if "status" in cmd:
                    return MagicMock(stdout="", returncode=0)
                if "meta" in cmd:
                    return MagicMock(stdout="<package name=\"pkg\" project=\"multimedia:libs\"><scmsync>https://src.opensuse.org/dimstar/pkg#factory</scmsync></package>", returncode=0)
                if "remote" in cmd:
                    return MagicMock(stdout="gitea@src.opensuse.org:dimstar/pkg.git\n", returncode=0)
                return MagicMock(stdout="", returncode=0)

            with patch.dict(pool_submit._SCMSYNC_CACHE, clear=True):
                with patch("pool_submit.check_pool_stream_sync", return_value={"exists": False}):
                    with patch("subprocess.run", side_effect=side_effect):
                        with patch("pool_submit.resolve_source_obs_project", return_value="multimedia:libs"):
                            ok, msg = pool_submit.submit_package_to_pool("pkg", tmpdir)
                            self.assertFalse(ok)
                            self.assertIn("synchronizes branch 'factory', which does not match current branch 'my-feature'", msg)

    def test_get_source_package_srcmd5(self):
        xml_out = "<directory name=\"pkg\" rev=\"10\" srcmd5=\"abc123456789\" />"
        with patch("subprocess.run", return_value=MagicMock(stdout=xml_out, returncode=0)):
            md5 = pool_submit.get_source_package_srcmd5("devel:prj", "pkg")
            self.assertEqual(md5, "abc123456789")

    def test_get_package_pool_submission_status_outdated_pending_sr(self):
        pending = {
            "vlc": [{
                "id": "1387152",
                "state": "review",
                "source_project": "multimedia:libs",
                "source_package": "vlc",
                "source_rev": "old_md5_111"
            }]
        }
        # 1. When current srcmd5 in OBS matches source_rev -> pending (up-to-date)
        with patch("pool_submit.get_source_package_srcmd5", return_value="old_md5_111"):
            st = pool_submit.get_package_pool_submission_status("vlc", pending_requests=pending)
            self.assertEqual(st["status"], "pending")
            self.assertFalse(st["is_outdated"])

        # 2. When current srcmd5 in OBS is newer -> pending_outdated
        with patch("pool_submit.get_source_package_srcmd5", return_value="new_md5_222"):
            st = pool_submit.get_package_pool_submission_status("vlc", pending_requests=pending)
            self.assertEqual(st["status"], "pending_outdated")
            self.assertTrue(st["is_outdated"])

    def test_submit_package_to_pool_supersede_outdated_sr(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, ".git"))
            def side_effect(cmd, **kwargs):
                if "rev-parse" in cmd and "--abbrev-ref" in cmd:
                    return MagicMock(stdout="factory\n", returncode=0)
                if "rev-list" in cmd:
                    return MagicMock(stdout="1\n", returncode=0)
                if "meta" in cmd:
                    return MagicMock(stdout="<package name=\"pkg\" project=\"multimedia:libs\"><scmsync>https://src.opensuse.org/dimstar/pkg#factory</scmsync></package>", returncode=0)
                if "remote" in cmd:
                    return MagicMock(stdout="gitea@src.opensuse.org:dimstar/pkg.git\n", returncode=0)
                return MagicMock(stdout="", returncode=0)

            outdated_status = {
                "status": "pending_outdated",
                "request_id": "1387152",
                "request": {"id": "1387152", "state": "review", "source_project": "multimedia:libs", "source_rev": "old_md5"}
            }

            with patch.dict(pool_submit._SCMSYNC_CACHE, clear=True):
                with patch("subprocess.run", side_effect=side_effect):
                    with patch("pool_submit.resolve_source_obs_project", return_value="multimedia:libs"):
                        with patch("pool_submit.get_package_pool_submission_status", return_value=outdated_status):
                            with patch("pool_submit.has_real_obs_diff", return_value=(True, "diff")):
                                ok, msg = pool_submit.submit_package_to_pool("pkg", tmpdir, dry_run=True)
                                self.assertTrue(ok)
                                self.assertIn("Would supersede pending SR #1387152", msg)
                                self.assertIn("-s 1387152 --yes", msg)

    def test_get_package_pool_submission_status_devel_project(self):
        """Verifies discovering a submit request targeting a devel project (e.g. M17N) from a source project."""
        from unittest.mock import patch, MagicMock
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, ".osc"))
            with open(os.path.join(tmpdir, ".osc", "_project"), "w") as f:
                f.write("home:dimstar:Factory\n")

            mock_xml = """<collection matches="1">
  <request id="1393059" creator="dimstar">
    <action type="submit">
      <source project="home:dimstar:Factory" package="ibus" rev="7"/>
      <target project="M17N" package="ibus"/>
    </action>
    <state name="new" who="dimstar" when="2026-10-27T11:18:52"/>
  </request>
</collection>"""

            def fake_run(cmd, *args, **kwargs):
                m = MagicMock()
                m.returncode = 0
                if "search/request" in cmd[2]:
                    m.stdout = mock_xml
                elif "api" in cmd and "/source/home:dimstar:Factory/ibus" in cmd[2]:
                    m.stdout = '<directory srcmd5="7"/>'
                else:
                    m.stdout = ""
                return m

            with patch("subprocess.run", side_effect=fake_run):
                st = pool_submit.get_package_pool_submission_status(
                    "ibus",
                    target_project=None,
                    pkg_dir=tmpdir
                )
                self.assertEqual(st["status"], "pending")
                self.assertEqual(st["request_id"], "1393059")
                self.assertEqual(st["target_project"], "M17N")
                self.assertEqual(st["source_project"], "home:dimstar:Factory")

    def test_revision_matching_short_rev_and_linkinfo_md5(self):
        """Verifies that short integer revs (e.g. '7') and linkinfo expanded MD5s are not marked outdated."""
        from unittest.mock import patch, MagicMock

        # 1. Short integer rev (e.g. rev="7") vs long srcmd5
        dir_xml_short = '<directory name="ibus" rev="7" vrev="7" srcmd5="d8dfd5c3e1922120496aa8244466807c" />'
        with patch("subprocess.run", return_value=MagicMock(stdout=dir_xml_short, returncode=0)):
            revs = pool_submit.get_source_package_revisions("home:dimstar:Factory", "ibus", force_refresh=True)
            self.assertIn("7", revs)
            self.assertIn("d8dfd5c3e1922120496aa8244466807c", revs)

        # 2. Linkinfo package with expanded xsrcmd5
        dir_xml_link = """<directory name="000package-groups" rev="2" srcmd5="8f34f6a4320cb80591c599118fbbcc54">
  <linkinfo project="openSUSE:Factory" package="000package-groups" xsrcmd5="6271b0b263fc89c65cd110511c4b22a8" lsrcmd5="8f34f6a4320cb80591c599118fbbcc54"/>
</directory>"""
        with patch("subprocess.run", return_value=MagicMock(stdout=dir_xml_link, returncode=0)):
            revs = pool_submit.get_source_package_revisions("home:dimstar:Factory", "000package-groups", force_refresh=True)
            self.assertIn("6271b0b263fc89c65cd110511c4b22a8", revs)
            self.assertIn("2", revs)
            self.assertIn("8f34f6a4320cb80591c599118fbbcc54", revs)

if __name__ == "__main__":


    unittest.main()
