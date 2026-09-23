#!/usr/bin/env python3
"""
Base package upgrade helper interface and data models.
Supports extensible strategies for various packaging patterns (OBS SCM, PyPI, Cargo, etc.).
"""

import abc
import os
import shlex
from typing import Optional, Dict, Any, Callable

class UpgradeResult:
    """Encapsulates the outcome of a package upgrade operation."""
    def __init__(
        self,
        success: bool,
        message: str,
        package_name: str = "",
        old_version: Optional[str] = None,
        new_version: Optional[str] = None,
        old_revision: Optional[str] = None,
        new_revision: Optional[str] = None,
        diff_files: Optional[Dict[str, str]] = None
    ):
        self.success = success
        self.message = message
        self.package_name = package_name
        self.old_version = old_version
        self.new_version = new_version
        self.old_revision = old_revision
        self.new_revision = new_revision
        self.diff_files = diff_files or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "message": self.message,
            "package_name": self.package_name,
            "old_version": self.old_version,
            "new_version": self.new_version,
            "old_revision": self.old_revision,
            "new_revision": self.new_revision,
            "diff_files": self.diff_files
        }

class BaseUpgradeHelper(abc.ABC):
    """
    Abstract Base Class for package upgrade engines in openSUSE/Geckopit.
    Subclasses handle different packaging patterns (e.g. OBS SCM service, PyPI, Cargo, Git tag).
    """
    name: str = "base"
    description: str = "Base package upgrade helper"

    def __init__(self, package_dir: str):
        self.package_dir = os.path.abspath(package_dir)

    @classmethod
    @abc.abstractmethod
    def can_handle(cls, package_dir: str) -> bool:
        """Determines if this upgrade helper is suitable for the package at package_dir."""
        pass

    @classmethod
    @abc.abstractmethod
    def get_current_revision(cls, package_dir: str) -> Optional[str]:
        """Extracts the active revision or version string declared in the package."""
        pass

    @abc.abstractmethod
    def execute_upgrade(
        self,
        target_revision: Optional[str] = None,
        dry_run: bool = False,
        on_log: Optional[Callable[[str], None]] = None
    ) -> UpgradeResult:
        """
        Executes the upgrade pipeline for this package.
        """
        pass

    @classmethod
    def get_command_line(cls, target_revision: Optional[str] = None) -> str:
        """Returns the CLI command line string to run this helper in a terminal."""
        if target_revision:
            return f"geckopit-upgrade {shlex.quote(target_revision)}"
        return "geckopit-upgrade"
