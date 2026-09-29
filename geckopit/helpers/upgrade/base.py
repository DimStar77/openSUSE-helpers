#!/usr/bin/env python3
"""
Base package upgrade helper interface and data models.
Supports extensible strategies for various packaging patterns (OBS SCM, PyPI, Cargo, etc.).
"""

import abc
import os
import re
import shlex
import subprocess
import tempfile
import urllib.parse
from typing import Optional, Dict, Any, Callable, Set, List

_rpm_devnull = None

def _silence_rpm_logging():
    global _rpm_devnull
    try:
        import rpm
        if _rpm_devnull is None or _rpm_devnull.closed:
            _rpm_devnull = open(os.devnull, "w", encoding="utf-8")
        rpm.setLogFile(_rpm_devnull)
    except Exception:
        pass

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
        diff_files: Optional[Dict[str, str]] = None,
        has_retrospective_news: bool = False,
        has_obscpio_warning: bool = False,
        removed_files: Optional[List[str]] = None
    ):
        self.success = success
        self.message = message
        self.package_name = package_name
        self.old_version = old_version
        self.new_version = new_version
        self.old_revision = old_revision
        self.new_revision = new_revision
        self.diff_files = diff_files or {}
        self.has_retrospective_news = has_retrospective_news
        self.has_obscpio_warning = has_obscpio_warning
        self.removed_files = removed_files or []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "message": self.message,
            "package_name": self.package_name,
            "old_version": self.old_version,
            "new_version": self.new_version,
            "old_revision": self.old_revision,
            "new_revision": self.new_revision,
            "diff_files": self.diff_files,
            "has_retrospective_news": self.has_retrospective_news,
            "has_obscpio_warning": self.has_obscpio_warning,
            "removed_files": self.removed_files
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

    def ensure_gitignore_pattern(self, pattern: str = "osc-collab.*") -> bool:
        """Ensures a pattern (e.g. osc-collab.*) is present in .gitignore so review files are ignored."""
        gitignore_path = os.path.join(self.package_dir, ".gitignore")
        if os.path.isfile(gitignore_path):
            try:
                with open(gitignore_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                regex = r"^\s*" + re.escape(pattern).replace(r"\*", r".*") + r"\s*$"
                if not re.search(regex, content, re.MULTILINE):
                    with open(gitignore_path, "a", encoding="utf-8") as f:
                        if content and not content.endswith("\n"):
                            f.write("\n")
                        f.write(f"{pattern}\n")
                    return True
            except OSError:
                pass
        return False

    @classmethod
    def get_command_line(cls, target_revision: Optional[str] = None) -> str:
        """Returns the CLI command line string to run this helper in a terminal."""
        if target_revision:
            return f"geckopit-upgrade {shlex.quote(target_revision)}"
        return "geckopit-upgrade"

    @classmethod
    def get_referenced_files(
        cls,
        spec_path: Optional[str] = None,
        spec_content: Optional[str] = None
    ) -> Set[str]:
        """
        Extracts the set of source and patch filenames referenced by a .spec file.
        Uses librpm (ts.parseSpec) if available, falling back to regex and macro expansion.
        """
        referenced: Set[str] = set()

        temp_spec = None
        target_path = spec_path
        if not target_path and spec_content:
            try:
                tf = tempfile.NamedTemporaryFile("w", suffix=".spec", delete=False, encoding="utf-8")
                tf.write(spec_content)
                tf.close()
                temp_spec = tf.name
                target_path = temp_spec
            except Exception:
                pass

        if target_path and os.path.isfile(target_path):
            try:
                import rpm
                _silence_rpm_logging()
                ts = rpm.TransactionSet()
                parsed_spec = ts.parseSpec(target_path)
                for item in parsed_spec.sources:
                    url_or_path = item[0]
                    parsed = urllib.parse.urlparse(url_or_path)
                    fname = os.path.basename(parsed.path) if parsed.path else os.path.basename(url_or_path)
                    if fname and not fname.startswith("%"):
                        referenced.add(fname)
            except Exception:
                pass
            finally:
                if temp_spec and os.path.isfile(temp_spec):
                    try:
                        os.remove(temp_spec)
                    except OSError:
                        pass

        if referenced:
            return referenced

        # Fallback to regex and macro expansion
        content = spec_content
        if content is None and spec_path and os.path.isfile(spec_path):
            try:
                with open(spec_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except OSError:
                return referenced

        if content:
            referenced = cls._extract_referenced_files_fallback(content)

        return referenced

    @classmethod
    def _extract_referenced_files_fallback(cls, content: str) -> Set[str]:
        macros: Dict[str, str] = {}
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("#"):
                continue
            m_def = re.match(r"^%(?:define|global)\s+([a-zA-Z0-9_]+)(?:\([^\)]*\))?\s+(.*)$", line)
            if m_def:
                macros[m_def.group(1)] = m_def.group(2).strip()
                continue
            m_tag = re.match(r"^(Name|Version|Release):\s*(.*)$", line, re.IGNORECASE)
            if m_tag:
                macros[m_tag.group(1).lower()] = m_tag.group(2).strip()

        # Expand macros against each other
        for _ in range(5):
            changed = False
            for k in list(macros.keys()):
                val = macros[k]
                for ok, ov in macros.items():
                    if k == ok:
                        continue
                    pattern = r"%\{?" + re.escape(ok) + r"\}?"
                    new_val = re.sub(pattern, lambda m, repl=ov: repl, val, flags=re.IGNORECASE)
                    if new_val != val:
                        macros[k] = new_val
                        val = new_val
                        changed = True
            if not changed:
                break

        files: Set[str] = set()
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("#"):
                continue
            m_src = re.match(r"^(?:Source\d*|Patch\d*)\s*:\s*(\S+)", line, re.IGNORECASE)
            if m_src:
                raw = m_src.group(1).strip()
                val = raw
                for _ in range(5):
                    ch = False
                    for k, v in macros.items():
                        pat = r"%\{?" + re.escape(k) + r"\}?"
                        nv = re.sub(pat, lambda m, repl=v: repl, val, flags=re.IGNORECASE)
                        if nv != val:
                            val = nv
                            ch = True
                    if not ch:
                        break
                parsed = urllib.parse.urlparse(val)
                fname = os.path.basename(parsed.path) if parsed.path else os.path.basename(val)
                if fname and not fname.startswith("%"):
                    files.add(fname)

        return files

    def clean_obsolete_files(
        self,
        obsolete_files: Any,
        on_log: Optional[Callable[[str], None]] = None
    ) -> List[str]:
        """
        Cleans obsolete files no longer referenced by the package.
        Safely removes them from disk and informs osc if within an osc checkout.
        Guards against deleting packaging metadata (.spec, .changes, _service) and hidden files.
        Enforces strict path confinement to prevent directory traversal or absolute path deletion.
        """
        log = on_log or (lambda msg: None)
        removed = []
        is_osc = os.path.isdir(os.path.join(self.package_dir, ".osc"))
        norm_pkg_dir = os.path.normpath(self.package_dir)

        for fname in sorted(obsolete_files):
            # Guard against directory traversal, absolute paths, or non-string input
            if not isinstance(fname, str) or not fname:
                continue
            if os.path.basename(fname) != fname or "/" in fname or "\\" in fname:
                continue
            if fname.startswith(".") or fname.endswith((".spec", ".changes", "_service")) or fname == "_constraints":
                continue

            full_path = os.path.normpath(os.path.join(self.package_dir, fname))
            if os.path.dirname(full_path) != norm_pkg_dir:
                continue

            if not os.path.isfile(full_path) and not os.path.islink(full_path):
                continue

            if is_osc:
                try:
                    subprocess.run(
                        ["osc", "rm", "-f", fname],
                        cwd=self.package_dir,
                        capture_output=True,
                        check=False
                    )
                except Exception:
                    pass

            if os.path.isfile(full_path):
                try:
                    os.remove(full_path)
                    removed.append(fname)
                    log(f"Removed obsolete file: {fname}")
                except OSError as e:
                    log(f"Warning: Could not remove obsolete file {fname}: {e}")
            elif fname not in removed:
                removed.append(fname)
                log(f"Removed obsolete file: {fname}")

        return removed
