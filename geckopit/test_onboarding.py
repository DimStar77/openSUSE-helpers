#!/usr/bin/env python3
"""
Unit tests for openSUSE Maintainer Environment Readiness Auditor & Onboarding Assistant.
Tests Git identity, SSH key generation, OBS credentials, Gitea configuration, and report formatting.
"""

import os
import sys
import unittest
import tempfile
import stat
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "helpers"))
import onboarding


class TestOnboarding(unittest.TestCase):

    def test_configure_and_check_git_identity(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_file = os.path.join(td, "test.gitconfig")
            # 1. Configure identity
            ok, msg = onboarding.configure_git_identity(
                "Gecko Tester", "gecko@opensuse.org", gitconfig_path=cfg_file
            )
            self.assertTrue(ok)
            self.assertIn("Gecko Tester", msg)

            # 2. Verify with mock gitconfig_path
            with patch("os.path.expanduser", return_value=cfg_file):
                res = onboarding.check_git_identity()
                self.assertTrue(res["configured"])
                self.assertEqual(res["name"], "Gecko Tester")
                self.assertEqual(res["email"], "gecko@opensuse.org")

    def test_generate_ssh_key(self):
        with tempfile.TemporaryDirectory() as td:
            key_file = os.path.join(td, "test_ed25519")
            ok, pubkey, msg = onboarding.generate_ssh_key(
                comment="test@opensuse.org", key_path=key_file
            )
            self.assertTrue(ok)
            self.assertTrue(pubkey.startswith("ssh-ed25519"))
            self.assertIn("Generated new SSH key", msg)
            self.assertTrue(os.path.isfile(key_file))
            self.assertTrue(os.path.isfile(f"{key_file}.pub"))

            # Verify 0600 permission on private key
            mode = stat.S_IMODE(os.stat(key_file).st_mode)
            self.assertEqual(mode, 0o600)

            # Running again should return existing key
            ok2, pubkey2, msg2 = onboarding.generate_ssh_key(key_path=key_file)
            self.assertTrue(ok2)
            self.assertEqual(pubkey, pubkey2)
            self.assertIn("Using existing SSH key", msg2)

    def test_configure_and_check_obs_credentials(self):
        with tempfile.TemporaryDirectory() as td:
            osc_file = os.path.join(td, "oscrc")
            ok, msg = onboarding.configure_osc_credentials(
                user="test_maintainer",
                token_or_pass="secret_token_123",
                apiurl="https://api.opensuse.org",
                oscrc_path=osc_file
            )
            self.assertTrue(ok)
            self.assertTrue(os.path.isfile(osc_file))

            # Verify 0600 permissions
            mode = stat.S_IMODE(os.stat(osc_file).st_mode)
            self.assertEqual(mode, 0o600)

            # Verify check_obs_credentials parses it
            with patch("os.path.expanduser", side_effect=lambda p: osc_file if "oscrc" in p else p):
                obs_res = onboarding.check_obs_credentials()
                self.assertTrue(obs_res["configured"])
                self.assertEqual(obs_res["user"], "test_maintainer")
                self.assertEqual(obs_res["apiurl"], "https://api.opensuse.org")

    def test_configure_and_check_gitea_login(self):
        with tempfile.TemporaryDirectory() as td:
            tea_file = os.path.join(td, "config.yml")
            ok, msg = onboarding.configure_gitea_login(
                token="gitea_api_token_abc",
                name="opensuse",
                url="https://src.opensuse.org",
                config_path=tea_file
            )
            self.assertTrue(ok)
            self.assertTrue(os.path.isfile(tea_file))

            # Verify 0600 permissions
            mode = stat.S_IMODE(os.stat(tea_file).st_mode)
            self.assertEqual(mode, 0o600)

            # Verify check_gitea_tea parses it
            with patch("os.path.expanduser", side_effect=lambda p: tea_file if "tea" in p else p):
                tea_res = onboarding.check_gitea_tea()
                self.assertTrue(tea_res["configured"])
                self.assertEqual(tea_res["url"], "https://src.opensuse.org")

    def test_check_ssh_readiness_parser(self):
        mock_out = "Hi there, testuser! You've successfully authenticated with the key named testkey, but Gitea does not provide shell access."
        mock_proc = MagicMock()
        mock_proc.stdout = mock_out
        mock_proc.stderr = ""
        mock_proc.returncode = 1

        with patch("subprocess.run", return_value=mock_proc):
            res = onboarding.check_ssh_readiness(test_connection=True)
            self.assertTrue(res["authenticated"])
            self.assertEqual(res["username"], "testuser")

    def test_format_readiness_cli_report(self):
        # 1. Fully ready state
        ready_data = {
            "all_ready": True,
            "git": {"configured": True, "name": "Jane Maintainer", "email": "jane@opensuse.org"},
            "ssh": {"configured": True, "authenticated": True, "username": "jane", "has_keys": True, "keys": ["id_ed25519.pub"]},
            "obs": {"configured": True, "user": "jane", "apiurl": "https://api.opensuse.org"},
            "tea": {"configured": True, "user": "jane", "url": "https://src.opensuse.org"}
        }
        report = onboarding.format_readiness_cli_report(ready_data)
        self.assertIn("Jane Maintainer <jane@opensuse.org>", report)
        self.assertIn("Connected to src.opensuse.org as 'jane'", report)
        self.assertIn("fully configured and ready", report)

        # 2. Incomplete state
        missing_data = {
            "all_ready": False,
            "git": {"configured": False, "name": "", "email": ""},
            "ssh": {"configured": False, "authenticated": False, "username": None, "has_keys": False, "keys": []},
            "obs": {"configured": False, "user": None, "apiurl": None},
            "tea": {"configured": False, "user": None, "url": None}
        }
        rep2 = onboarding.format_readiness_cli_report(missing_data)
        self.assertIn("Not configured", rep2)
        self.assertIn("geckopit-cli --setup", rep2)


if __name__ == "__main__":
    unittest.main()
