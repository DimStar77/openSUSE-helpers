"""
Geckopit Test Suite.
Automatically configures sys.path so tests can import geckopit and its helper engines.
"""

import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_GECKOPIT_DIR = os.path.dirname(_TESTS_DIR)
_HELPERS_DIR = os.path.join(_GECKOPIT_DIR, "helpers")

for _p in [_GECKOPIT_DIR, _HELPERS_DIR]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
