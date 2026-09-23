#!/usr/bin/env python3
"""
Geckopit Package Upgrade Helpers Package.
Provides registry, strategy dispatch, and changelog formatting for packaging pipelines.
"""

from typing import Optional, List, Type
from .base import BaseUpgradeHelper, UpgradeResult
from .obs_scm import ObsScmUpgradeHelper
from .tarball import TarballUpgradeHelper
from .changelog import (
    CHANGELOG_WRAP_WIDTH,
    wrap_bullet,
    format_changelog_entry,
    build_changelog_from_items,
    remove_patch_from_spec,
    check_retrospective_news_changes
)

# Registry of upgrade helpers in order of evaluation
_REGISTERED_HELPERS: List[Type[BaseUpgradeHelper]] = [
    ObsScmUpgradeHelper,
    TarballUpgradeHelper,
]

def register_upgrade_helper(helper_cls: Type[BaseUpgradeHelper]):
    """Registers an upgrade helper class in the engine registry."""
    if helper_cls not in _REGISTERED_HELPERS:
        _REGISTERED_HELPERS.append(helper_cls)

def list_upgrade_helpers() -> List[Type[BaseUpgradeHelper]]:
    """Returns a list of all registered upgrade helper classes."""
    return list(_REGISTERED_HELPERS)

def get_upgrade_helper(package_dir: str, helper_name: Optional[str] = None) -> Optional[BaseUpgradeHelper]:
    """
    Detects and returns an instantiated upgrade helper for the given package directory.
    If helper_name is specified, selects that helper class explicitly.
    """
    for helper_cls in _REGISTERED_HELPERS:
        if helper_name and helper_cls.name != helper_name:
            continue
        if helper_name or helper_cls.can_handle(package_dir):
            return helper_cls(package_dir)
    return None

__all__ = [
    "BaseUpgradeHelper",
    "UpgradeResult",
    "ObsScmUpgradeHelper",
    "TarballUpgradeHelper",
    "register_upgrade_helper",
    "list_upgrade_helpers",
    "get_upgrade_helper",
    "CHANGELOG_WRAP_WIDTH",
    "wrap_bullet",
    "format_changelog_entry",
    "build_changelog_from_items",
    "remove_patch_from_spec",
    "check_retrospective_news_changes",
]
