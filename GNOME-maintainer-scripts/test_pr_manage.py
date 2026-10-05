#!/usr/bin/env python3
"""
Unit tests for pr_manage.py.
Covers token parsing, title generation, exact package matching,
and the duplicate package collision guard during select and combine.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import pr_manage as pm
import tempfile
from pathlib import Path
_TEST_TMP_DIR = tempfile.TemporaryDirectory()
pm.CONFIG_PATH = Path(_TEST_TMP_DIR.name) / "test-pr-manage.json"



class TestPrManageModelsAndUtils(unittest.TestCase):
    def test_package_token(self):
        t1 = pm.PackageToken("GNOME", "gtk4", 18)
        t2 = pm.PackageToken("GNOME", "GTK4", 18)
        t3 = pm.PackageToken("GNOME", "gtk4", 19)

        self.assertEqual(t1.key, "gtk4")
        self.assertEqual(t2.key, "gtk4")
        self.assertEqual(t1, t2)
        self.assertNotEqual(t1, t3)
        self.assertEqual(hash(t1), hash(t2))

    def test_extract_tokens(self):
        body = """
This is a forwarded pull request by AutoGits Workflow Bot
referencing the following pull request(s):

PR: GNOME/accountsservice!5
  PR: GNOME/adwaita-icon-theme!8
Some random line
PR: openSUSE/package!99

###  ManualMergeProject enabled.
"""
        tokens = pm.extract_tokens(body)
        self.assertEqual(len(tokens), 3)
        self.assertEqual(tokens[0].package, "accountsservice")
        self.assertEqual(tokens[0].pr_number, 5)
        self.assertEqual(tokens[1].package, "adwaita-icon-theme")
        self.assertEqual(tokens[1].pr_number, 8)
        self.assertEqual(tokens[2].owner, "openSUSE")
        self.assertEqual(tokens[2].package, "package")
        self.assertEqual(tokens[2].pr_number, 99)



    def test_get_host_package_from_pr(self):
        pr_head = {"head": {"ref": "PR_gdk-pixbuf#7"}, "body": ""}
        self.assertEqual(pm.get_host_package_from_pr(pr_head), "gdk-pixbuf")

        pr_body = {"head": {"ref": "master"}, "body": "PR: GNOME/zenity!3"}
        self.assertEqual(pm.get_host_package_from_pr(pr_body), "zenity")

        pr_empty = {"head": {"ref": ""}, "body": ""}
        self.assertIsNone(pm.get_host_package_from_pr(pr_empty))

    def test_find_open_pr_for_package_exact_vs_substring(self):
        prs = [
            {"number": 1, "head": {"ref": "PR_gtk3#6"}, "title": "Forwarded PRs: gtk3", "body": "PR: GNOME/gtk3!6"},
            {"number": 2, "head": {"ref": "PR_gtk4#18"}, "title": "Forwarded PRs: gtk4", "body": "PR: GNOME/gtk4!18"},
            {"number": 3, "head": {"ref": "PR_gtk#1"}, "title": "Forwarded PRs: gtk", "body": "PR: GNOME/gtk!1"},
        ]

        # Searching for 'gtk' must find PR #3, NOT #1 (gtk3) or #2 (gtk4)
        m = pm.find_open_pr_for_package(prs, "gtk")
        self.assertIsNotNone(m)
        self.assertEqual(m["number"], 3)

        # Searching for 'gtk4' must find PR #2
        m4 = pm.find_open_pr_for_package(prs, "gtk4")
        self.assertIsNotNone(m4)
        self.assertEqual(m4["number"], 2)


class TestPrManageCollisionGuard(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_client.repo = "GNOME/_ObsPrj"

    def test_select_duplicate_host_package_skipped(self):
        # Target PR 100 has host 'zenity' and peer 'glib2'
        self.mock_client.get_pr.return_value = {
            "number": 100,
            "base": {"ref": "factory"},
            "head": {"ref": "PR_zenity#10"},
            "title": "Forwarded PRs: glib2, zenity",
            "body": "PR: GNOME/zenity!10\nPR: GNOME/glib2!5\n"
        }

        # Attempt to select 'zenity' (already host)
        pm.action_select(self.mock_client, 100, ["zenity"])

        # No update_pr calls should happen because zenity was skipped as duplicate
        self.mock_client.update_pr.assert_not_called()

    def test_select_duplicate_peer_package_skipped(self):
        self.mock_client.get_pr.side_effect = lambda pid: {
            "number": pid,
            "base": {"ref": "factory"},
            "head": {"ref": f"PR_{'zenity' if pid == 100 else 'glib2'}#{pid}"},
            "title": "Forwarded PRs: glib2, zenity" if pid == 100 else "Forwarded PRs: glib2",
            "body": "PR: GNOME/zenity!10\nPR: GNOME/glib2!5\n" if pid == 100 else "PR: GNOME/glib2!6\n"
        }
        self.mock_client.get_all_open_prs.return_value = [
            {"number": 200, "head": {"ref": "PR_glib2#6"}, "title": "Forwarded PRs: glib2", "body": "PR: GNOME/glib2!6"}
        ]

        # Attempt to select glib2 into PR 100 (which already tracks glib2!5)
        pm.action_select(self.mock_client, 100, ["glib2"])

        # Must reject glib2 and never call update_pr
        self.mock_client.update_pr.assert_not_called()

    def test_select_mixed_duplicate_and_clean_package(self):
        def get_pr_mock(pid):
            if pid == 100:
                return {
                    "number": 100,
                    "base": {"ref": "factory"},
                    "head": {"ref": "PR_zenity#10"},
                    "title": "Forwarded PRs: zenity",
                    "body": "PR: GNOME/zenity!10\n"
                }
            elif pid == 200:
                return {
                    "number": 200,
                    "base": {"ref": "factory"},
                    "head": {"ref": "PR_zenity#11"},
                    "title": "Forwarded PRs: zenity",
                    "body": "PR: GNOME/zenity!11\n"
                }
            elif pid == 300:
                return {
                    "number": 300,
                    "base": {"ref": "factory"},
                    "head": {"ref": "PR_gjs#2"},
                    "title": "Forwarded PRs: gjs",
                    "body": "PR: GNOME/gjs!2\n"
                }

        self.mock_client.get_pr.side_effect = get_pr_mock
        self.mock_client.get_all_open_prs.return_value = [
            {"number": 200, "head": {"ref": "PR_zenity#11"}, "title": "Forwarded PRs: zenity", "body": "PR: GNOME/zenity!11"},
            {"number": 300, "head": {"ref": "PR_gjs#2"}, "title": "Forwarded PRs: gjs", "body": "PR: GNOME/gjs!2"}
        ]

        # Select both 'zenity' (duplicate) and 'gjs' (clean)
        pm.action_select(self.mock_client, 100, ["zenity", "gjs"])

        # Target PR 100 should be updated with ONLY gjs added; title is preserved!
        self.mock_client.update_pr.assert_any_call(
            100,
            body="PR: GNOME/zenity!10\nPR: GNOME/gjs!2"
        )
        # Only PR 300 should be closed, PR 200 must NOT be closed
        self.mock_client.update_pr.assert_any_call(300, state="closed")
        calls = [c[0] for c in self.mock_client.update_pr.call_args_list]
        self.assertNotIn((200,), calls)

    def test_combine_collision_guard_aborts_on_duplicate(self):
        target_pr = {
            "number": 100,
            "base": {"ref": "factory"},
            "head": {"ref": "PR_zenity#10"},
            "title": "Forwarded PRs: gtk4, zenity",
            "body": "PR: GNOME/zenity!10\nPR: GNOME/gtk4!18\n"
        }
        source_pr = {
            "number": 200,
            "base": {"ref": "factory"},
            "head": {"ref": "PR_gtk4#19"},
            "title": "Forwarded PRs: gjs, gtk4",
            "body": "PR: GNOME/gtk4!19\nPR: GNOME/gjs!3\n"
        }

        self.mock_client.get_pr.side_effect = lambda pid: target_pr if pid == 100 else source_pr

        # Attempt to combine: gtk4 is in both!
        with self.assertRaises(SystemExit) as cm:
            pm.action_combine(self.mock_client, 100, 200)

        self.assertEqual(cm.exception.code, 1)
        self.mock_client.update_pr.assert_not_called()

    def test_combine_success_no_duplicates(self):
        target_pr = {
            "number": 100,
            "base": {"ref": "factory"},
            "head": {"ref": "PR_zenity#10"},
            "title": "Forwarded PRs: zenity",
            "body": "PR: GNOME/zenity!10\n"
        }
        source_pr = {
            "number": 200,
            "base": {"ref": "factory"},
            "head": {"ref": "PR_gtk4#18"},
            "title": "Forwarded PRs: gjs, gtk4",
            "body": "PR: GNOME/gtk4!18\nPR: GNOME/gjs!3\n"
        }

        self.mock_client.get_pr.side_effect = lambda pid: target_pr if pid == 100 else source_pr

        pm.action_combine(self.mock_client, 100, 200)

        # Target should be updated with new tokens only; title is preserved!
        self.mock_client.update_pr.assert_any_call(
            100,
            body="PR: GNOME/zenity!10\nPR: GNOME/gtk4!18\nPR: GNOME/gjs!3"
        )
        self.mock_client.update_pr.assert_any_call(200, state="closed")

    def test_unselect_host_package_blocked(self):
        target_pr = {
            "number": 100,
            "base": {"ref": "factory"},
            "head": {"ref": "PR_zenity#10"},
            "title": "Forwarded PRs: gjs, zenity",
            "body": "PR: GNOME/zenity!10\nPR: GNOME/gjs!3\n"
        }
        self.mock_client.get_pr.return_value = target_pr

        with self.assertRaises(SystemExit) as cm:
            pm.action_unselect(self.mock_client, 100, "zenity")

        self.assertEqual(cm.exception.code, 1)
        self.mock_client.update_pr.assert_not_called()


    def test_custom_title_preserved_during_grouping(self):
        # Staging manager gave PR 100 a custom name: "GNOME 51.0"
        target_pr = {
            "number": 100,
            "base": {"ref": "factory"},
            "head": {"ref": "PR_zenity#10"},
            "title": "GNOME 51.0",
            "body": "PR: GNOME/zenity!10\n"
        }
        source_pr = {
            "number": 200,
            "base": {"ref": "factory"},
            "head": {"ref": "PR_gjs#2"},
            "title": "Forwarded PRs: gjs",
            "body": "PR: GNOME/gjs!2\n"
        }

        self.mock_client.get_pr.side_effect = lambda pid: target_pr if pid == 100 else source_pr

        pm.action_combine(self.mock_client, 100, 200)

        # Ensure update_pr was called with body only, never passing title!
        update_call = [c for c in self.mock_client.update_pr.call_args_list if c[0] == (100,)][0]
        self.assertNotIn("title", update_call[1])
        self.assertIn("body", update_call[1])


if __name__ == "__main__":
    unittest.main()

import staging_service as ss


class TestStagingService(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_client.repo = "GNOME/_ObsPrj"
        self.mock_client.is_read_only = False
        self.service = ss.StagingService(self.mock_client)

    def test_fetch_all_separates_groups_and_ungrouped(self):
        self.mock_client.get_all_open_prs.return_value = [{"number": 100}, {"number": 200}]
        self.mock_client.get_pr.side_effect = lambda pid: {
            "number": pid,
            "base": {"ref": "factory"},
            "head": {"ref": f"PR_pkg{pid}#1"},
            "title": f"PR {pid}",
            "body": "PR: GNOME/a!1\nPR: GNOME/b!2" if pid == 100 else "PR: GNOME/c!3"
        }

        all_prs, ungrouped = self.service.fetch_all()
        # Column 1 includes both multi-package groups and single-package forwards!
        self.assertEqual(len(all_prs), 2)
        self.assertEqual(all_prs[0].pr_id, 100)
        self.assertTrue(all_prs[0].is_group)
        self.assertEqual(all_prs[0].member_count, 2)

        self.assertEqual(all_prs[1].pr_id, 200)
        self.assertFalse(all_prs[1].is_group)
        self.assertEqual(all_prs[1].member_count, 1)

        # Column 3 has standalone queue
        self.assertEqual(len(ungrouped), 1)
        self.assertEqual(ungrouped[0].pr_id, 200)

    def test_service_add_package_collision_guard(self):
        self.mock_client.get_pr.return_value = {
            "number": 100,
            "base": {"ref": "factory"},
            "head": {"ref": "PR_zenity#1"},
            "title": "GNOME 51.0",
            "body": "PR: GNOME/zenity!1\nPR: GNOME/gtk4!18"
        }

        res = self.service.add_package_to_group(100, "gtk4")
        self.assertFalse(res.success)
        self.assertTrue("already exists" in res.message or "Duplicates" in res.message)
        self.mock_client.update_pr.assert_not_called()

    def test_service_combine_collision_guard(self):
        self.mock_client.get_pr.side_effect = lambda pid: {
            "number": pid,
            "base": {"ref": "factory"},
            "head": {"ref": f"PR_p{pid}#1"},
            "title": f"PR {pid}",
            "body": "PR: GNOME/zenity!1\nPR: GNOME/gtk4!18" if pid == 100 else "PR: GNOME/gtk4!19\nPR: GNOME/gjs!2"
        }

        res = self.service.combine_groups(100, 200)
        self.assertFalse(res.success)
        self.assertIn("Collision", res.message)
        self.mock_client.update_pr.assert_not_called()

    def test_service_batch_add_packages(self):
        def mock_get(pid):
            if pid == 100:
                return {"number": 100, "base": {"ref": "factory"}, "head": {"ref": "PR_zenity#1"}, "title": "PR 100", "body": "PR: GNOME/zenity!1\n"}
            elif pid == 200:
                return {"number": 200, "base": {"ref": "factory"}, "head": {"ref": "PR_gjs#1"}, "title": "PR 200", "body": "PR: GNOME/gjs!1\n"}
            elif pid == 300:
                return {"number": 300, "base": {"ref": "factory"}, "head": {"ref": "PR_gtk4#1"}, "title": "PR 300", "body": "PR: GNOME/gtk4!1\n"}
            return {}
        self.mock_client.get_pr.side_effect = mock_get
        self.mock_client.get_all_open_prs.return_value = [
            {"number": 200, "head": {"ref": "PR_gjs#1"}, "title": "Forwarded PRs: gjs", "body": "PR: GNOME/gjs!1\n"},
            {"number": 300, "head": {"ref": "PR_gtk4#1"}, "title": "Forwarded PRs: gtk4", "body": "PR: GNOME/gtk4!1\n"},
        ]

        res = self.service.add_packages_to_group(100, ["gjs", "gtk4"])
        self.assertTrue(res.success)
        self.assertIn("Added 2 package(s)", res.message)
        # Verify update_pr was called on target 100
        target_update = [c for c in self.mock_client.update_pr.call_args_list if c[0] == (100,)][0]
        self.assertIn("PR: GNOME/gjs!1", target_update[1]["body"])
        self.assertIn("PR: GNOME/gtk4!1", target_update[1]["body"])

    def test_service_move_packages_between_groups(self):
        source_pr = {
            "number": 100,
            "base": {"ref": "factory"},
            "head": {"ref": "PR_zenity#1"},
            "title": "Group A",
            "body": "PR: GNOME/zenity!1\nPR: GNOME/gjs!2\nPR: GNOME/gtk4!3\n"
        }
        target_pr = {
            "number": 200,
            "base": {"ref": "factory"},
            "head": {"ref": "PR_mutter#1"},
            "title": "Group B",
            "body": "PR: GNOME/mutter!1\n"
        }
        self.mock_client.get_pr.side_effect = lambda pid: source_pr if pid == 100 else target_pr

        # Move 'gjs' from 100 to 200
        res = self.service.move_packages_between_groups(100, 200, ["gjs"])
        self.assertTrue(res.success)
        self.assertIn("Successfully moved 1 package(s)", res.message)

        # Check source PR 100 body no longer has gjs
        source_call = [c for c in self.mock_client.update_pr.call_args_list if c[0] == (100,)][0]
        self.assertNotIn("PR: GNOME/gjs!2", source_call[1]["body"])
        self.assertIn("PR: GNOME/gtk4!3", source_call[1]["body"])

        # Check target PR 200 body now has gjs
        target_call = [c for c in self.mock_client.update_pr.call_args_list if c[0] == (200,)][0]
        self.assertIn("PR: GNOME/gjs!2", target_call[1]["body"])

    def test_service_move_packages_collision_guard(self):
        source_pr = {
            "number": 100,
            "base": {"ref": "factory"},
            "head": {"ref": "PR_zenity#1"},
            "title": "Group A",
            "body": "PR: GNOME/zenity!1\nPR: GNOME/gjs!2\n"
        }
        target_pr = {
            "number": 200,
            "base": {"ref": "factory"},
            "head": {"ref": "PR_mutter#1"},
            "title": "Group B",
            "body": "PR: GNOME/mutter!1\nPR: GNOME/gjs!3\n"
        }
        self.mock_client.get_pr.side_effect = lambda pid: source_pr if pid == 100 else target_pr

        # Move 'gjs' from 100 to 200, but 200 already has gjs!
        res = self.service.move_packages_between_groups(100, 200, ["gjs"])
        self.assertFalse(res.success)
        self.assertIn("Collision", res.message)
        self.mock_client.update_pr.assert_not_called()

import pr_manage_tui as pt


class TestStagingTUI(unittest.TestCase):
    def setUp(self):
        self.mock_service = MagicMock()
        self.tui = pt.StagingTUI(self.mock_service)

    def test_tui_filtering(self):
        g1 = ss.StagingGroup(1, "webkitgtk", "factory", "PR_webkitgtk#1", "webkitgtk")
        g2 = ss.StagingGroup(2, "Komikku", "factory", "PR_Komikku#1", "Komikku")
        self.tui.ungrouped = [g1, g2]

        # No filter
        self.assertEqual(len(self.tui.get_filtered_ungrouped()), 2)

        # Filter 'webkit'
        self.tui.ungrouped_search = "webkit"
        filtered = self.tui.get_filtered_ungrouped()
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].pr_id, 1)

    def test_tui_cursor_navigation(self):
        self.tui.groups = [ss.StagingGroup(1, "G1", "factory", "", "host1"), ss.StagingGroup(2, "G2", "factory", "", "host2")]
        self.tui.active_col = 0
        self.tui.group_idx = 0

        self.tui.move_cursor(1)
        self.assertEqual(self.tui.group_idx, 1)

        # Clamping
        self.tui.move_cursor(5)
        self.assertEqual(self.tui.group_idx, 1)

        self.tui.move_cursor(-1)
        self.assertEqual(self.tui.group_idx, 0)



    def test_tui_contextual_multi_select(self):
        # Column 1 (Members)
        g = ss.StagingGroup(1, "Group 1", "factory", "PR_zenity#1", "zenity", [pm.PackageToken("GNOME", "gjs", 2)])
        self.tui.groups = [g]
        self.tui.group_idx = 0
        self.tui.active_col = 1
        self.tui.member_idx = 0

        # Press space on member
        self.tui.handle_toggle_selection()
        self.assertIn("gjs", self.tui.selected_members)
        # Toggle off
        self.tui.handle_toggle_selection()
        self.assertNotIn("gjs", self.tui.selected_members)

        # Column 2 (Ungrouped)
        u1 = ss.StagingGroup(200, "Komikku", "factory", "PR_Komikku#1", "Komikku")
        self.tui.ungrouped = [u1]
        self.tui.active_col = 2
        self.tui.ungrouped_idx = 0

        # Press space on ungrouped
        self.tui.handle_toggle_selection()
        self.assertIn(200, self.tui.selected_ungrouped)
        # Toggle off
        self.tui.handle_toggle_selection()
        self.assertNotIn(200, self.tui.selected_ungrouped)

        # Column 0 (Groups): space should not modify selections
        self.tui.active_col = 0
        self.tui.handle_toggle_selection()
        self.assertEqual(len(self.tui.selected_members), 0)
        self.assertEqual(len(self.tui.selected_ungrouped), 0)

    def test_tui_jump_home_and_end(self):
        self.tui.groups = [
            ss.StagingGroup(1, "G1", "factory", "", "h1"),
            ss.StagingGroup(2, "G2", "factory", "", "h2"),
            ss.StagingGroup(3, "G3", "factory", "", "h3"),
        ]
        self.tui.active_col = 0
        self.tui.group_idx = 0

        # Jump End in Groups
        self.tui.jump_end()
        self.assertEqual(self.tui.group_idx, 2)

        # Jump Home in Groups
        self.tui.jump_home()
        self.assertEqual(self.tui.group_idx, 0)

        # Jump in Members
        g = ss.StagingGroup(1, "G1", "factory", "", "h1", [
            pm.PackageToken("GNOME", "a", 1),
            pm.PackageToken("GNOME", "b", 2),
            pm.PackageToken("GNOME", "c", 3)
        ])
        self.tui.groups = [g]
        self.tui.active_col = 1
        self.tui.jump_end()
        self.assertEqual(self.tui.member_idx, 2)
        self.tui.jump_home()
        self.assertEqual(self.tui.member_idx, 0)

    def test_tui_accept_single_pr_forward(self):
        # Column 1 has a single PR forward: #1080 webkitgtk
        single_pr = ss.StagingGroup(1080, "Forwarded PRs: webkitgtk", "factory", "PR_webkitgtk#27", "webkitgtk", html_url="url")
        self.tui.groups = [single_pr]
        self.tui.group_idx = 0
        self.tui.active_col = 0

        # Mock prompt_confirm to return True
        self.tui.prompt_confirm = MagicMock(return_value=True)
        self.tui.draw = MagicMock()
        self.mock_service.accept_group.return_value = ss.OperationResult(True, "Successfully commented 'merge ok'")

        mock_stdscr = MagicMock()
        self.tui.handle_accept_group(mock_stdscr)

        self.mock_service.accept_group.assert_called_with(1080)
        self.assertIn("Successfully commented 'merge ok'", self.tui.status_msg)


    def test_tui_mark_groups_and_batch_combine(self):
        g1 = ss.StagingGroup(100, "Group 1", "factory", "PR_zenity#1", "zenity", [pm.PackageToken("GNOME", "zenity", 1)])
        g2 = ss.StagingGroup(200, "Group 2", "factory", "PR_gtk4#1", "gtk4", [pm.PackageToken("GNOME", "gtk4", 18)])
        g3 = ss.StagingGroup(300, "Group 3", "factory", "PR_gjs#1", "gjs", [pm.PackageToken("GNOME", "gjs", 2)])
        self.tui.groups = [g1, g2, g3]
        self.tui.active_col = 0

        # Mark g1
        self.tui.group_idx = 0
        self.tui.handle_toggle_selection()
        self.assertIn(100, self.tui.selected_groups)

        # Mark g2
        self.tui.group_idx = 1
        self.tui.handle_toggle_selection()
        self.assertIn(200, self.tui.selected_groups)

        # Mock target selection modal to select g1 as anchor
        self.tui.select_combine_target_modal = MagicMock(return_value=g1)
        self.tui.draw = MagicMock()
        self.tui.service.client.is_read_only = False
        self.mock_service.combine_multiple_groups.return_value = ss.OperationResult(True, "Combined 1 PR into #100")

        mock_stdscr = MagicMock()
        self.tui.handle_combine_groups(mock_stdscr)

        # Confirm combine_multiple_groups called with target=100, sources=[200]
        self.mock_service.combine_multiple_groups.assert_called_with(100, [200])
        self.assertEqual(len(self.tui.selected_groups), 0)


class TestMultiWorkspaceAwareness(unittest.TestCase):
    def test_resolve_repository_env(self):
        with patch.dict(os.environ, {"GITEA_REPO": "KDE/_ObsPrj"}):
            self.assertEqual(pm.resolve_repository(), "KDE/_ObsPrj")

        with patch.dict(os.environ, {"GITEA_REPO": "openSUSE/Factory"}):
            self.assertEqual(pm.resolve_repository(), "openSUSE/Factory")

    def test_gitea_client_splits_owner_and_repo_name(self):
        client = pm.GiteaClient(repo="openSUSE/Factory")
        self.assertEqual(client.owner, "openSUSE")
        self.assertEqual(client.repo_name, "Factory")

        client_kde = pm.GiteaClient(repo="KDE/_ObsPrj")
        self.assertEqual(client_kde.owner, "KDE")
        self.assertEqual(client_kde.repo_name, "_ObsPrj")

    def test_find_forwarded_child_id_dynamic_repo_name(self):
        client = pm.GiteaClient(repo="openSUSE/Factory")
        # Mock request for child PR timeline
        timeline_events = [
            {"ref_issue": {"repository": {"name": "Factory"}, "number": 1234}},
            {"ref_issue": {"repository": {"name": "other_repo"}, "number": 5678}},
        ]
        client.request = MagicMock(return_value=timeline_events)

        child_id = pm.find_forwarded_child_id(client, "openSUSE/bash", "42")
        self.assertEqual(child_id, 1234)


class TestOrphanAudit(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_client.repo = "GNOME/_ObsPrj"
        self.mock_client.owner = "GNOME"
        self.mock_client.repo_name = "_ObsPrj"
        self.mock_client.is_read_only = False
        self.service = ss.StagingService(self.mock_client)

    def test_audit_orphans_detects_untracked_pr(self):
        # Open PRs on _ObsPrj: only tracks zenity!1
        self.mock_client.get_all_open_prs.return_value = [
            {"number": 100, "head": {"ref": "PR_zenity#1"}, "body": "PR: GNOME/zenity!1\n"}
        ]

        # Issues/PRs across GNOME org: zenity!1 (tracked) and gtk4!18 (untracked)
        self.mock_client.request.side_effect = lambda ep: [
            {"number": 1, "repository": {"name": "zenity"}},
            {"number": 18, "repository": {"name": "gtk4"}, "title": "GNOME 51.0"}
        ] if "issues/search" in ep else {
            "number": 18, "title": "GNOME 51.0", "base": {"ref": "factory"},
            "head": {"ref": "next"}, "user": {"login": "dimstar"}, "html_url": "url"
        }

        orphans = self.service.audit_orphans(filter_branch="factory")
        self.assertEqual(len(orphans), 1)
        self.assertEqual(orphans[0].package, "gtk4")
        self.assertEqual(orphans[0].pr_number, 18)
        self.assertEqual(orphans[0].author, "dimstar")

    def test_reopen_orphan_forward_pr(self):
        orphan = ss.OrphanPackagePR("gtk4", 18, "title", "user", "factory", "next", "url", forward_pr_id=999)
        res = self.service.reopen_orphan_forward_pr(orphan)
        self.assertTrue(res.success)
        self.mock_client.update_pr.assert_called_with(999, state="open")

    def test_adopt_orphan_into_group(self):
        self.mock_client.get_pr.return_value = {
            "number": 100, "base": {"ref": "factory"}, "head": {"ref": "PR_zenity#1"},
            "title": "Group", "body": "PR: GNOME/zenity!1\n"
        }
        orphan = ss.OrphanPackagePR("gtk4", 18, "title", "user", "factory", "next", "url", forward_pr_id=999, forward_pr_state="closed")
        res = self.service.adopt_orphan_into_group(100, orphan)
        self.assertTrue(res.success)
        target_call = [c for c in self.mock_client.update_pr.call_args_list if c[0] == (100,)][0]
        self.assertIn("PR: GNOME/gtk4!18", target_call[1]["body"])


    def test_service_combine_multiple_groups(self):
        target_pr = {"number": 100, "base": {"ref": "factory"}, "head": {"ref": "PR_zenity#1"}, "title": "Target", "body": "PR: GNOME/zenity!1\n"}
        source_200 = {"number": 200, "base": {"ref": "factory"}, "head": {"ref": "PR_gtk4#1"}, "title": "Src 200", "body": "PR: GNOME/gtk4!18\n"}
        source_300 = {"number": 300, "base": {"ref": "factory"}, "head": {"ref": "PR_gjs#1"}, "title": "Src 300", "body": "PR: GNOME/gjs!2\n"}

        def get_pr_mock(pid):
            if pid == 100: return target_pr
            if pid == 200: return source_200
            if pid == 300: return source_300
            return {}

        self.mock_client.get_pr.side_effect = get_pr_mock

        res = self.service.combine_multiple_groups(100, [200, 300])
        self.assertTrue(res.success)
        self.assertIn("Successfully combined 2 PR(s)", res.message)

        target_call = [c for c in self.mock_client.update_pr.call_args_list if c[0] == (100,)][0]
        self.assertIn("PR: GNOME/gtk4!18", target_call[1]["body"])
        self.assertIn("PR: GNOME/gjs!2", target_call[1]["body"])

    def test_service_combine_multiple_groups_inter_source_collision(self):
        target_pr = {"number": 100, "base": {"ref": "factory"}, "head": {"ref": "PR_zenity#1"}, "title": "Target", "body": "PR: GNOME/zenity!1\n"}
        source_200 = {"number": 200, "base": {"ref": "factory"}, "head": {"ref": "PR_gtk4#1"}, "title": "Src 200", "body": "PR: GNOME/gtk4!18\n"}
        source_300 = {"number": 300, "base": {"ref": "factory"}, "head": {"ref": "PR_gtk4#2"}, "title": "Src 300", "body": "PR: GNOME/gtk4!19\n"}

        def get_pr_mock(pid):
            if pid == 100: return target_pr
            if pid == 200: return source_200
            if pid == 300: return source_300
            return {}

        self.mock_client.get_pr.side_effect = get_pr_mock

        res = self.service.combine_multiple_groups(100, [200, 300])
        self.assertFalse(res.success)
        self.assertIn("Collision", res.message)
        self.mock_client.update_pr.assert_not_called()



class TestNewTUIFeatures(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_client.repo = "GNOME/_ObsPrj"
        self.mock_client.owner = "GNOME"
        self.mock_client.base_url = "https://src.opensuse.org"
        self.mock_client.token = "token"
        self.mock_client.is_read_only = False
        self.service = ss.StagingService(self.mock_client)
        self.tui = pt.StagingTUI(self.service)

    def test_rename_group(self):
        res = self.service.rename_group(100, "GNOME 51.0 Final")
        self.assertTrue(res.success)
        self.mock_client.update_pr.assert_called_with(100, title="GNOME 51.0 Final")

    def test_rename_group_empty_rejected(self):
        res = self.service.rename_group(100, "   ")
        self.assertFalse(res.success)
        self.assertIn("cannot be empty", res.message)

    def test_get_pr_diff(self):
        mock_res = MagicMock()
        mock_res.status_code = 200
        mock_res.text = "diff --git a/foo.changes b/foo.changes\n+New changes"
        self.mock_client.session.get.return_value = mock_res

        diff = self.service.get_pr_diff("GNOME", "zenity", 10)
        self.assertIn("+New changes", diff)

    @patch("subprocess.run")
    def test_get_obs_build_status_detects_failures(self, mock_run):
        xml_output = """<resultlist>
  <result project="GNOME:Factory:PullRequest:100" repository="openSUSE_Factory" arch="x86_64" code="failed" state="failed">
    <status package="zenity" code="succeeded"/>
    <status package="mutter" code="failed"/>
    <status package="libdex" code="unresolvable"/>
  </result>
</resultlist>"""
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = xml_output
        mock_run.return_value = mock_res

        status = self.service.get_obs_build_status(100, "factory")
        self.assertEqual(status["status"], "failed")
        self.assertIn("mutter", status["failed_pkgs"])
        self.assertIn("libdex", status["failed_pkgs"])
        self.assertEqual(status["succeeded"], 1)
        self.assertEqual(status["total"], 3)

    def test_tui_bulk_selection_tools(self):
        g1 = ss.StagingGroup(1, "G1", "factory", "", "p1")
        g2 = ss.StagingGroup(2, "G2", "factory", "", "p2")
        self.tui.groups = [g1, g2]
        self.tui.active_col = 0

        # Select all
        self.tui.handle_select_all()
        self.assertEqual(self.tui.selected_groups, {1, 2})

        # Invert
        self.tui.handle_toggle_selection() # deselect group 1 (group_idx=0)
        self.assertEqual(self.tui.selected_groups, {2})
        self.tui.handle_invert_selection()
        self.assertEqual(self.tui.selected_groups, {1})

        # Clear
        self.tui.handle_clear_selection()
        self.assertEqual(len(self.tui.selected_groups), 0)

    def test_accept_group_warns_when_obs_fails(self):
        target = ss.StagingGroup(100, "GNOME 51.0", "factory", "PR_zenity#1", "zenity")
        self.tui.groups = [target]
        self.tui.group_idx = 0
        self.tui.active_col = 0

        # Mock OBS status to failed
        self.tui.obs_status_cache[100] = {"status": "failed", "failed_pkgs": ["mutter", "libdex"]}

        # Mock prompt_confirm
        mock_prompt = MagicMock(return_value=True)
        self.tui.prompt_confirm = mock_prompt
        self.tui.draw = MagicMock()
        self.mock_client.add_comment = MagicMock()

        mock_stdscr = MagicMock()
        self.tui.handle_accept_group(mock_stdscr)

        # Assert warning prefix was shown in confirm prompt
        prompt_arg = mock_prompt.call_args[0][1]
        self.assertIn("OBS FAILING", prompt_arg)
        self.assertIn("mutter", prompt_arg)


    def test_tui_async_obs_loading(self):
        import time

        self.service.get_obs_build_status = MagicMock(return_value={"status": "succeeded", "total": 10})

        # Initial call returns loading immediately
        status = self.tui.get_cached_obs_status(555, "factory")
        self.assertEqual(status.get("status"), "loading")

        # Wait briefly for background thread to populate cache
        for _ in range(50):
            if 555 in self.tui.obs_status_cache:
                break
            time.sleep(0.02)

        self.assertIn(555, self.tui.obs_status_cache)
        self.assertEqual(self.tui.obs_status_cache[555].get("status"), "succeeded")


class TestPermissionsAndDynamicWorkspaces(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_client.repo = "openSUSE/Factory"
        self.mock_client.owner = "openSUSE"
        self.mock_client.repo_name = "Factory"
        self.service = ss.StagingService(self.mock_client)

    def test_client_read_only_permissions(self):
        client = pm.GiteaClient(repo="openSUSE/Factory")
        client.get_repo_info = MagicMock(return_value={
            "permissions": {"admin": False, "push": False, "pull": True}
        })

        self.assertFalse(client.has_push_access)
        self.assertFalse(client.has_admin_access)
        self.assertTrue(client.is_read_only)

    def test_mutations_blocked_in_read_only_mode(self):
        self.mock_client.is_read_only = True
        self.mock_client.get_pr.return_value = {
            "number": 100, "base": {"ref": "factory"}, "head": {"ref": "PR_p#1"},
            "title": "PR", "body": "PR: GNOME/foo!1\n"
        }

        res_add = self.service.add_packages_to_group(100, ["foo"])
        self.assertFalse(res_add.success)
        self.assertIn("Permission Denied", res_add.message)

        res_rem = self.service.remove_packages_from_group(100, ["foo"])
        self.assertFalse(res_rem.success)
        self.assertIn("Permission Denied", res_rem.message)

        res_comb = self.service.combine_multiple_groups(100, [200])
        self.assertFalse(res_comb.success)
        self.assertIn("Permission Denied", res_comb.message)

        res_ren = self.service.rename_group(100, "New Title")
        self.assertFalse(res_ren.success)
        self.assertIn("Permission Denied", res_ren.message)

        self.mock_client.update_pr.assert_not_called()

    def test_discover_valid_workspaces_filters_nonexistent_repos(self):
        # Candidate KDE/_ObsPrj returns None (404), GNOME/_ObsPrj returns valid info
        def mock_get_info(repo):
            if "KDE" in repo:
                return None
            elif "GNOME" in repo:
                return {"permissions": {"admin": True, "push": True, "pull": True}}
            elif "Factory" in repo:
                return {"permissions": {"admin": False, "push": False, "pull": True}}
            return None

        self.mock_client.get_repo_info = mock_get_info
        self.mock_client.request.return_value = [{"username": "GNOME"}, {"username": "KDE"}]

        workspaces = self.service.discover_valid_workspaces()
        repo_names = [w[0] for w in workspaces]

        self.assertIn("GNOME/_ObsPrj", repo_names)
        self.assertIn("openSUSE/Factory", repo_names)
        self.assertNotIn("KDE/_ObsPrj", repo_names)

    def test_config_save_and_load(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            tmp_cfg = Path(td) / "pr-manage.json"
            with patch("pr_manage.CONFIG_PATH", tmp_cfg):
                # Save
                pm.save_config(active_workspace="openSUSE/Factory", add_workspace="GNOME/_ObsPrj")

                # Load
                cfg = pm.load_config()
                self.assertEqual(cfg.get("active_workspace"), "openSUSE/Factory")
                self.assertIn("openSUSE/Factory", cfg.get("workspaces", []))
                self.assertIn("GNOME/_ObsPrj", cfg.get("workspaces", []))

    def test_resolve_repository_uses_active_workspace_from_config(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("subprocess.run") as mock_sub:
                mock_sub.return_value = MagicMock(returncode=1)
                with patch("pr_manage.load_config", return_value={"active_workspace": "openSUSE/Factory"}):
                    # Mock workflow.config check
                    with patch("builtins.open", side_effect=FileNotFoundError):
                        resolved = pm.resolve_repository()
                        self.assertEqual(resolved, "openSUSE/Factory")

    def test_is_staging_metaproject_validation(self):
        from pathlib import Path
        # Non-staging github repo
        self.assertFalse(pm.is_staging_metaproject(
            Path("/some/path"), "git@github.com:DimStar77/openSUSE-helpers.git", "DimStar77/openSUSE-helpers"
        ))

        # Legitimate Gitea staging repos
        self.assertTrue(pm.is_staging_metaproject(
            Path("/some/path"), "gitea@src.opensuse.org:/GNOME/_ObsPrj", "GNOME/_ObsPrj"
        ))
        self.assertTrue(pm.is_staging_metaproject(
            Path("/some/path"), "https://src.opensuse.org/openSUSE/Factory.git", "openSUSE/Factory"
        ))

    def test_is_valid_workspace_slug(self):
        self.assertFalse(pm.is_valid_workspace_slug("DimStar77/openSUSE-helpers"))
        self.assertFalse(pm.is_valid_workspace_slug("something_without_slash"))
        self.assertTrue(pm.is_valid_workspace_slug("GNOME/_ObsPrj"))
        self.assertTrue(pm.is_valid_workspace_slug("openSUSE/Factory"))
