#!/usr/bin/env python3
import os
import re
import sys
import unittest
import unittest.mock as mock

import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
GECKOPIT_DIR = os.path.dirname(TESTS_DIR)
HELPERS_DIR = os.path.join(GECKOPIT_DIR, "helpers")

for p in [GECKOPIT_DIR, HELPERS_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

import docs_builder

class TestDocsBuilder(unittest.TestCase):
    def test_user_guide_html_not_drifted(self):
        import os
        import docs_builder

        repo_dir = GECKOPIT_DIR
        md_path = os.path.join(repo_dir, "USER_GUIDE.md")
        html_path = os.path.join(repo_dir, "USER_GUIDE.html")

        self.assertTrue(os.path.isfile(md_path), f"Missing {md_path}")
        self.assertTrue(os.path.isfile(html_path), f"Missing {html_path}. Run docs_builder.py to compile.")

        with open(md_path, "r", encoding="utf-8") as f:
            md_text = f.read()

        expected_html = docs_builder.render_markdown_to_html(
            md_text, title="Geckopit User Guide: Packaging Workflow Cockpit"
        )

        with open(html_path, "r", encoding="utf-8") as f:
            actual_html = f.read()

        self.assertEqual(
            actual_html,
            expected_html,
            "geckopit/USER_GUIDE.html has drifted from USER_GUIDE.md! "
            "Run 'python3 geckopit/helpers/docs_builder.py' to synchronize."
        )

    def test_user_guide_html_anchor_integrity(self):
        import os, re

        repo_dir = GECKOPIT_DIR
        html_path = os.path.join(repo_dir, "USER_GUIDE.html")
        self.assertTrue(os.path.isfile(html_path), "Missing USER_GUIDE.html")

        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()

        # Extract all internal anchor links: <a href="#target">
        anchor_links = re.findall(r'<a\s+href="#([^"]+)">', html)
        self.assertGreater(len(anchor_links), 0, "No anchor links found in USER_GUIDE.html")

        # Extract all target element IDs: id="target"
        target_ids = set(re.findall(r'\bid="([^"]+)"', html))

        broken = [link for link in anchor_links if link not in target_ids]
        self.assertEqual(
            broken, [],
            f"Found broken anchor links in USER_GUIDE.html that do not match any header ID: {broken}"
        )


if __name__ == '__main__':
    unittest.main()
