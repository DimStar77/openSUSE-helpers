#!/usr/bin/env python3
"""
OBS Source Service Upgrade Helper (obs_scm / _service).
Python reimplementation and modernization of obs_scm-update.sh.
Features automated changelog formatting at 67 chars, spec version bumping,
and automated merged patch detection and dropping.
"""

import glob
import os
import re
import shlex
import subprocess
from typing import Optional, Callable, Dict, Tuple, List

from .base import BaseUpgradeHelper, UpgradeResult
from .changelog import (
    CHANGELOG_WRAP_WIDTH,
    wrap_bullet,
    format_changelog_entry,
    remove_patch_from_spec
)

class ObsScmUpgradeHelper(BaseUpgradeHelper):
    """
    Upgrade helper for packages managed with OBS source service (_service)
    using obs_scm / tar_scm.
    """
    name = "obs_scm"
    description = "OBS Source Service (obs_scm / _service)"

    @classmethod
    def can_handle(cls, package_dir: str) -> bool:
        service_file = os.path.join(package_dir, "_service")
        return os.path.isfile(service_file)

    @classmethod
    def get_current_revision(cls, package_dir: str) -> Optional[str]:
        service_file = os.path.join(package_dir, "_service")
        if not os.path.isfile(service_file):
            return None

        # XML parsing first
        import xml.etree.ElementTree as ET
        try:
            tree = ET.parse(service_file)
            root = tree.getroot()
            for service in root.findall("service"):
                if service.get("name") in ("obs_scm", "tar_scm"):
                    versionformat = None
                    for param in service.findall("param"):
                        if param.get("name") == "versionformat":
                            versionformat = param.text
                    if versionformat is not None and versionformat.strip() == "0.gitmodule":
                        continue
                    for param in service.findall("param"):
                        if param.get("name") == "revision":
                            return param.text.strip() if param.text else None
        except Exception:
            pass

        # Regex fallback
        try:
            with open(service_file, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            match = re.search(r'<param\s+name=["\']revision["\']>([^<]+)</param>', content)
            if match:
                return match.group(1).strip()
        except Exception:
            pass
        return None

    def get_package_name(self) -> str:
        service_file = os.path.join(self.package_dir, "_service")
        if os.path.isfile(service_file):
            try:
                with open(service_file, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                match = re.search(r'<param\s+name=["\']url["\']>([^<]+)</param>', content)
                if match:
                    url = match.group(1).strip()
                    url = re.sub(r'\.git/?$', '', url)
                    name = url.rstrip('/').split('/')[-1]
                    if name:
                        return name
            except Exception:
                pass

        base = os.path.basename(self.package_dir)
        try:
            if os.path.isfile(os.path.join(self.package_dir, f"{base}.spec")):
                return base
            specs = [f[:-5] for f in os.listdir(self.package_dir) if f.endswith(".spec")]
            if specs:
                return specs[0]
        except Exception:
            pass

        return os.path.basename(self.package_dir)

    def get_obsinfo_metadata(self, pkg_name: str) -> Tuple[Optional[str], Optional[str]]:
        """Returns (version, commit) from <pkg_name>.obsinfo."""
        obsinfo_file = os.path.join(self.package_dir, f"{pkg_name}.obsinfo")
        if not os.path.isfile(obsinfo_file):
            return None, None

        version = None
        commit = None
        try:
            with open(obsinfo_file, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line_s = line.strip()
                    if line_s.startswith("version:"):
                        version = line_s.split(":", 1)[1].strip()
                    elif line_s.startswith("commit:"):
                        commit = line_s.split(":", 1)[1].strip()
        except Exception:
            pass
        return version, commit

    def clean_stale_archives(self, on_log: Optional[Callable[[str], None]] = None) -> List[str]:
        """Removes *.obscpio and *.tar.xz archives prior to running service."""
        removed = []
        for pattern in ["*.obscpio", "*.tar.xz"]:
            for fpath in glob.glob(os.path.join(self.package_dir, pattern)):
                try:
                    os.remove(fpath)
                    removed.append(os.path.basename(fpath))
                except OSError:
                    pass
        if removed and on_log:
            on_log(f"Cleaned stale archives: {', '.join(removed)}")
        return removed

    def update_service_revision(self, new_revision: str, on_log: Optional[Callable[[str], None]] = None) -> bool:
        """Updates the <param name="revision"> tag in _service cleanly."""
        service_file = os.path.join(self.package_dir, "_service")
        if not os.path.isfile(service_file):
            return False

        with open(service_file, "r", encoding="utf-8") as f:
            content = f.read()

        pattern = r'(<param\s+name=["\']revision["\']>)[^<]*(</param>)'
        if not re.search(pattern, content):
            if on_log:
                on_log("Warning: <param name=\"revision\"> not found in _service")
            return False

        new_content, count = re.subn(pattern, rf'\g<1>{new_revision}\g<2>', content, count=1)
        if count > 0:
            with open(service_file, "w", encoding="utf-8") as f:
                f.write(new_content)
            if on_log:
                on_log(f"Updated revision in _service to '{new_revision}'")
            return True
        return False

    def clean_duplicate_obscpio_if_manual(self, on_log: Optional[Callable[[str], None]] = None) -> List[str]:
        """If tar mode='manual', delete *.obscpio so only *.tar.xz is checked into repo."""
        service_file = os.path.join(self.package_dir, "_service")
        if not os.path.isfile(service_file):
            return []

        with open(service_file, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        if re.search(r'["\']tar["\'].*["\']manual["\']', content) or re.search(r'name=["\']tar["\']\s+mode=["\']manual["\']', content):
            removed = []
            for fpath in glob.glob(os.path.join(self.package_dir, "*.obscpio")):
                try:
                    os.remove(fpath)
                    removed.append(os.path.basename(fpath))
                except OSError:
                    pass
            if removed and on_log:
                on_log(f"Cleaned intermediate obscpio (tar is manual): {', '.join(removed)}")
            return removed
        return []

    def find_upstream_changelog_target(self, upstream_repo: str, old_rev: Optional[str] = None) -> str:
        """
        Discovers the upstream release notes file.
        Prioritizes condensed, release-targeted files (NEWS*) over commit-dump logs (ChangeLog*).
        If multiple exist and old_rev is provided, prioritizes the highest-precedence file
        that was actually modified in this release.
        """
        candidates = [
            "NEWS", "NEWS.md", "NEWS.rst", "NEWS.txt",
            "RELEASES.md", "releasenotes.txt",
            "CHANGELOG.md", "CHANGELOG.rst", "ChangeLog"
        ]

        # 1. If old_rev is available, check which candidates actually changed in git
        if old_rev:
            try:
                res = subprocess.run(
                    ["git", "-C", upstream_repo, "diff", "--no-ext-diff", "--name-only", f"{old_rev}..HEAD", "--"] + candidates,
                    capture_output=True,
                    text=True,
                    check=False
                )
                if res.returncode == 0:
                    changed_files = set(res.stdout.splitlines())
                    for c in candidates:
                        if c in changed_files:
                            return c
            except Exception:
                pass

        # 2. Fallback to existence priority
        for c in candidates:
            if os.path.isfile(os.path.join(upstream_repo, c)):
                return c

        return "NEWS"

    def extract_git_diffs(
        self,
        pkg_name: str,
        old_rev: str,
        on_log: Optional[Callable[[str], None]] = None
    ) -> Dict[str, str]:
        """Extracts upstream git diffs between old_rev and HEAD for NEWS/ChangeLog and meson build files."""
        diff_files = {}
        upstream_repo = os.path.join(self.package_dir, pkg_name)
        if not (os.path.isdir(upstream_repo) and os.path.exists(os.path.join(upstream_repo, ".git"))):
            return diff_files

        # Auto-detect upstream changelog filename (evaluating diff activity against old_rev)
        changelog_target = self.find_upstream_changelog_target(upstream_repo, old_rev=old_rev)

        targets = [
            (changelog_target, "osc-collab.NEWS"),
            ("meson.build", "osc-collab.meson"),
            ("meson_options.txt", "osc-collab.meson_options")
        ]

        for git_target, out_filename in targets:
            try:
                res = subprocess.run(
                    ["git", "-C", upstream_repo, "diff", "--no-ext-diff", f"{old_rev}..HEAD", "--", git_target],
                    capture_output=True,
                    text=True,
                    check=False
                )
                if res.returncode == 0:
                    out_path = os.path.join(self.package_dir, out_filename)
                    with open(out_path, "w", encoding="utf-8") as f:
                        f.write(res.stdout)
                    diff_files[out_filename] = res.stdout
            except Exception as e:
                if on_log:
                    on_log(f"Notice: Could not extract diff for {git_target}: {e}")

        if diff_files and on_log:
            on_log(f"Extracted upstream diffs: {', '.join(diff_files.keys())}")

        return diff_files

    def audit_and_drop_merged_patches(
        self,
        pkg_name: str,
        on_log: Optional[Callable[[str], None]] = None
    ) -> List[str]:
        """
        Scans package directory for *.patch files.
        Checks if each patch is merged into upstream HEAD (by commit SHA or reverse apply).
        If merged, deletes patch file, cleans spec file, and returns dropped patch list.
        """
        dropped_patches = []
        upstream_repo = os.path.join(self.package_dir, pkg_name)
        if not (os.path.isdir(upstream_repo) and os.path.exists(os.path.join(upstream_repo, ".git"))):
            return dropped_patches

        patch_files = glob.glob(os.path.join(self.package_dir, "*.patch"))
        if not patch_files:
            return dropped_patches

        for patch_path in sorted(patch_files):
            patch_name = os.path.basename(patch_path)
            is_merged = False

            # Check 1: Commit SHA in patch filename (e.g. e5c2018d.patch or 0001-...)
            sha_match = re.match(r'^([a-f0-9]{7,40})\.patch$', patch_name, re.IGNORECASE)
            if sha_match:
                sha = sha_match.group(1)
                try:
                    res = subprocess.run(
                        ["git", "-C", upstream_repo, "merge-base", "--is-ancestor", sha, "HEAD"],
                        capture_output=True,
                        check=False
                    )
                    if res.returncode == 0:
                        is_merged = True
                except Exception:
                    pass

            # Check 2: Test if patch applies cleanly in reverse to upstream HEAD
            if not is_merged:
                for p_num in ["-p1", "-p0"]:
                    try:
                        res = subprocess.run(
                            ["git", "-C", upstream_repo, "apply", "--check", "--reverse", p_num, patch_path],
                            capture_output=True,
                            check=False
                        )
                        if res.returncode == 0:
                            is_merged = True
                            break
                    except Exception:
                        pass

            if is_merged:
                dropped_patches.append(patch_name)
                if on_log:
                    on_log(f"Detected merged patch: {patch_name} (carried upstream)")

                # Delete patch file (from git or osc if tracked, otherwise unlink)
                try:
                    subprocess.run(
                        ["git", "-C", self.package_dir, "rm", "-f", patch_name],
                        capture_output=True,
                        check=False
                    )
                except Exception:
                    pass
                if os.path.isdir(os.path.join(self.package_dir, ".osc")):
                    try:
                        subprocess.run(
                            ["osc", "rm", "-f", patch_name],
                            cwd=self.package_dir,
                            capture_output=True,
                            check=False
                        )
                    except Exception:
                        pass
                if os.path.exists(patch_path):
                    try:
                        os.remove(patch_path)
                    except OSError:
                        pass

                # Clean spec file reference
                for sf in os.listdir(self.package_dir):
                    if sf.endswith(".spec"):
                        spec_file = os.path.join(self.package_dir, sf)
                        try:
                            with open(spec_file, "r", encoding="utf-8") as f:
                                orig_spec = f.read()
                            clean_spec = remove_patch_from_spec(orig_spec, patch_name)
                            if clean_spec != orig_spec:
                                with open(spec_file, "w", encoding="utf-8") as f:
                                    f.write(clean_spec)
                                if on_log:
                                    on_log(f"Removed '{patch_name}' declaration from {sf}")
                        except Exception:
                            pass

        return dropped_patches

    def update_spec_version(
        self,
        new_version: str,
        on_log: Optional[Callable[[str], None]] = None
    ) -> Optional[str]:
        """Updates Version: tag in *.spec to new_version."""
        for sf in os.listdir(self.package_dir):
            if sf.endswith(".spec"):
                spec_file = os.path.join(self.package_dir, sf)
                try:
                    with open(spec_file, "r", encoding="utf-8") as f:
                        content = f.read()
                    new_content = re.sub(
                        r'^(Version:\s*)\S+',
                        rf'\g<1>{new_version}',
                        content,
                        flags=re.MULTILINE,
                        count=1
                    )
                    if new_content != content:
                        with open(spec_file, "w", encoding="utf-8") as f:
                            f.write(new_content)
                        if on_log:
                            on_log(f"Updated 'Version:' in {sf} to {new_version}")
                        return sf
                except Exception:
                    pass
        return None

    def update_changelog_via_osc(
        self,
        new_version: str,
        diff_text: str = "",
        dropped_patches: Optional[List[str]] = None,
        on_log: Optional[Callable[[str], None]] = None
    ) -> bool:
        """Formats 67-column changelog entry and records it via non-interactive 'osc vc -F'."""
        changelog_content = format_changelog_entry(
            diff_text,
            new_version,
            dropped_patches=dropped_patches,
            width=CHANGELOG_WRAP_WIDTH
        )

        tmp_news = os.path.join(self.package_dir, ".NEWS")
        try:
            with open(tmp_news, "w", encoding="utf-8") as f:
                f.write(changelog_content + "\n")

            res = subprocess.run(
                ["osc", "vc", "-F", ".NEWS"],
                cwd=self.package_dir,
                capture_output=True,
                text=True,
                check=False
            )
            if res.returncode == 0:
                if on_log:
                    on_log(f"Recorded formatted changelog for version {new_version} via 'osc vc'")
                return True
            else:
                err = (res.stderr or res.stdout).strip()
                if on_log:
                    on_log(f"Warning: 'osc vc' returned code {res.returncode}: {err}")
                return False
        finally:
            if os.path.isfile(tmp_news):
                os.remove(tmp_news)

    def execute_upgrade(
        self,
        target_revision: Optional[str] = None,
        dry_run: bool = False,
        on_log: Optional[Callable[[str], None]] = None
    ) -> UpgradeResult:
        log = on_log or (lambda msg: None)

        if not self.can_handle(self.package_dir):
            return UpgradeResult(
                success=False,
                message=f"No _service file found in {self.package_dir}"
            )

        rev = target_revision.strip() if target_revision else "@PARENT_TAG@"
        pkg_name = self.get_package_name()
        old_ver, old_rev = self.get_obsinfo_metadata(pkg_name)

        log(f"Starting OBS SCM update for '{pkg_name}' (Target Revision: '{rev}')")

        if dry_run:
            log(f"[DRY-RUN] Would update _service revision to '{rev}' and run 'osc service mr'")
            return UpgradeResult(
                success=True,
                message="[DRY-RUN] Validation completed",
                package_name=pkg_name,
                old_version=old_ver,
                old_revision=old_rev
            )

        # 1. Clean stale archives
        self.clean_stale_archives(on_log=log)

        # 2. Update _service revision
        if not self.update_service_revision(rev, on_log=log):
            return UpgradeResult(
                success=False,
                message="Failed to update <param name=\"revision\"> in _service",
                package_name=pkg_name
            )

        # 3. Execute osc service mr
        log("Executing 'osc service mr' (fetching upstream repository & generating archive)...")
        try:
            res = subprocess.run(
                ["osc", "service", "mr"],
                cwd=self.package_dir,
                capture_output=True,
                text=True,
                check=True
            )
            if on_log and res.stdout:
                on_log(res.stdout.strip())
        except subprocess.CalledProcessError as e:
            err = (e.stderr or e.stdout or str(e)).strip()
            log(f"Error running 'osc service mr': {err}")
            return UpgradeResult(
                success=False,
                message=f"'osc service mr' failed: {err}",
                package_name=pkg_name,
                old_version=old_ver,
                old_revision=old_rev
            )
        except FileNotFoundError:
            return UpgradeResult(
                success=False,
                message="'osc' binary not found on system PATH",
                package_name=pkg_name
            )

        # 4. Extract new metadata from .obsinfo
        new_ver, new_rev = self.get_obsinfo_metadata(pkg_name)
        log(f"New package state: version={new_ver or 'unknown'}, commit={new_rev[:8] if new_rev else 'unknown'}")

        # 5. Extract upstream diffs (NEWS/ChangeLog, meson.build, meson_options.txt)
        diff_files = {}
        if old_rev:
            diff_files = self.extract_git_diffs(pkg_name, old_rev, on_log=log)

        # 6. Source duplication hygiene
        self.clean_duplicate_obscpio_if_manual(on_log=log)

        # 7. Audit and drop merged patches
        dropped_patches = self.audit_and_drop_merged_patches(pkg_name, on_log=log)

        # 8. Update Version: in *.spec
        if new_ver:
            self.update_spec_version(new_ver, on_log=log)

        # 9. Update changelog via non-interactive osc vc
        news_diff = diff_files.get("osc-collab.NEWS", "")
        has_retro = False
        if news_diff:
            has_retro = check_retrospective_news_changes(news_diff)
            if has_retro:
                log("⚠️  Notice: Upstream NEWS diff contains additions to older release sections (e.g. historical CVE/GHSA annotations). Inspect 'osc-collab.NEWS' if past .changes entries should be updated.")
        else:
            log("⚠️  Notice: No upstream NEWS/changelog diff found. Please review upstream release notes.")

        if new_ver and (old_rev != new_rev or not old_rev):
            self.update_changelog_via_osc(
                new_ver,
                diff_text=news_diff,
                dropped_patches=dropped_patches,
                on_log=log
            )

        log(f"Successfully upgraded {pkg_name} to {new_ver or rev}!")
        return UpgradeResult(
            success=True,
            message=f"Successfully upgraded {pkg_name} to version {new_ver or rev}",
            package_name=pkg_name,
            old_version=old_ver,
            new_version=new_ver,
            old_revision=old_rev,
            new_revision=new_rev,
            diff_files=diff_files,
            has_retrospective_news=has_retro
        )
