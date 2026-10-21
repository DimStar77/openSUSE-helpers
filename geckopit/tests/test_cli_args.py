#!/usr/bin/env python3
"""
Unit tests for geckopit-cli command-line option validation.
Verifies that unrecognized options trigger errors with helpful typo suggestions,
preventing unintended fallback to default synchronization/dashboard actions.
"""

import os
import sys
import unittest
import importlib.util
from importlib.machinery import SourceFileLoader

import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
GECKOPIT_DIR = os.path.dirname(TESTS_DIR)
HELPERS_DIR = os.path.join(GECKOPIT_DIR, "helpers")

for p in [GECKOPIT_DIR, HELPERS_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

cli_path = os.path.join(GECKOPIT_DIR, "geckopit-cli")
loader = SourceFileLoader("geckopit_cli", cli_path)
spec = importlib.util.spec_from_file_location("geckopit_cli", cli_path, loader=loader)
geckopit_cli = importlib.util.module_from_spec(spec)
sys.modules["geckopit_cli"] = geckopit_cli
loader.exec_module(geckopit_cli)


class TestCliArgs(unittest.TestCase):

    def test_valid_options(self):
        """Tests that valid options pass validation with None returned."""
        valid_cases = [
            [],
            ["gnome-shell"],
            ["--check-patches"],
            ["--check-patches", "--verbose"],
            ["--check-patches", "--sle"],
            ["--rebase-patch"],
            ["--rebase-patch=fix.patch"],
            ["--rebase-patch", "fix.patch", "--shell"],
            ["--sync"],
            ["-s"],
            ["--upgrade"],
            ["-u", "51.1"],
            ["--upgrade=51.1"],
            ["--branch=next"],
            ["-b", "factory"],
            ["--jobs=8"],
            ["-j", "4"],
            ["gnome-shell", "--commit", "--no-edit"],
            ["--check-pool"],
            ["--pending-sr"],
            ["--submit-pool"],
            ["libnotify", "--submit-pool", "--dry-run"],
            ["--submit-pool", "--all", "--yes"],
            ["--submit-pool", "-m", "Update version"],
        ]
        for args in valid_cases:
            err = geckopit_cli.validate_cli_options(args)
            self.assertIsNone(err, f"Valid args {args} should not return an error: {err}")

    def test_unrecognized_option_suggestion_prefix(self):
        """Tests that prefix abbreviations like --c produce an error suggesting matching options."""
        err = geckopit_cli.validate_cli_options(["--c"])
        self.assertIsNotNone(err)
        self.assertIn("Unrecognized option '--c'", err)
        self.assertIn("'--commit'", err)
        self.assertIn("'--check-patches'", err)

        err_com = geckopit_cli.validate_cli_options(["--com"])
        self.assertIsNotNone(err_com)
        self.assertIn("Did you mean '--commit'?", err_com)

    def test_unrecognized_option_suggestion_fuzzy(self):
        """Tests fuzzy matching suggestions for typos."""
        err = geckopit_cli.validate_cli_options(["--check-patchess"])
        self.assertIsNotNone(err)
        self.assertIn("Unrecognized option '--check-patchess'", err)
        self.assertIn("Did you mean '--check-patches'?", err)

        err_commit = geckopit_cli.validate_cli_options(["--comit"])
        self.assertIsNotNone(err_commit)
        self.assertIn("Unrecognized option '--comit'", err_commit)
        self.assertIn("Did you mean '--commit'?", err_commit)

    def test_unknown_option_without_suggestion(self):
        """Tests completely unrecognized options without suggestions."""
        err = geckopit_cli.validate_cli_options(["--unknown-option-xyz"])
        self.assertIsNotNone(err)
        self.assertIn("Unrecognized option '--unknown-option-xyz'", err)
        self.assertIn("See 'geckopit-cli --help'", err)


if __name__ == '__main__':
    unittest.main()
