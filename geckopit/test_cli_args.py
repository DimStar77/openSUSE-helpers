#!/usr/bin/env python3
"""
Unit tests for geckopit-cli command-line option validation.
Verifies that unrecognized options trigger errors with helpful typo suggestions,
preventing unintended fallback to default synchronization/dashboard actions.
"""

import os
import sys
import unittest
from importlib.machinery import SourceFileLoader

cli_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "geckopit-cli")
geckopit_cli = SourceFileLoader("geckopit_cli", cli_path).load_module()


class TestCliArgs(unittest.TestCase):

    def test_valid_options(self):
        """Tests that valid options pass validation with None returned."""
        valid_cases = [
            [],
            ["gnome-shell"],
            ["--check-patches"],
            ["--check-patches", "--verbose"],
            ["--check-patches", "--sle"],
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
