#!/usr/bin/env python3
import os
import sys
import subprocess
import unittest

import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
GECKOPIT_DIR = os.path.dirname(TESTS_DIR)
HELPERS_DIR = os.path.join(GECKOPIT_DIR, "helpers")

for p in [GECKOPIT_DIR, HELPERS_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

class TestBashCompletion(unittest.TestCase):
    def test_bash_completion_syntax_and_flag(self):
        comp_path = os.path.join(GECKOPIT_DIR, "completion", "geckopit.bash")
        self.assertTrue(os.path.isfile(comp_path))

        # 1. Test bash syntax validation via bash -n
        res_syntax = subprocess.run(["bash", "-n", comp_path], capture_output=True, text=True)
        self.assertEqual(res_syntax.returncode, 0, f"Bash completion syntax error: {res_syntax.stderr}")

        # 2. Test geckopit-cli --bash-completion flag
        cli_path = os.path.join(GECKOPIT_DIR, "geckopit-cli")
        res_cli = subprocess.run([sys.executable, cli_path, "--bash-completion"], capture_output=True, text=True)
        self.assertEqual(res_cli.returncode, 0)
        self.assertIn("complete -F _geckopit_completion geckopit-cli", res_cli.stdout)
        self.assertIn("_geckopit_get_packages", res_cli.stdout)
        self.assertIn("_geckopit_get_upgrade_targets", res_cli.stdout)
        self.assertIn("_geckopit_get_ignored_versions", res_cli.stdout)
        self.assertIn("--ignore", res_cli.stdout)
        self.assertIn("--unignore", res_cli.stdout)
        self.assertIn("--list-ignored", res_cli.stdout)


if __name__ == '__main__':
    unittest.main()
