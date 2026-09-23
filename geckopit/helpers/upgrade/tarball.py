#!/usr/bin/env python3
"""
Direct Tarball / Spec Package Upgrade Engine.
Handles packages whose sources are downloaded via 'osc service mr download_files'
and declared in the package .spec file rather than managed via 'obs_scm' _service files.
"""

import difflib
import glob
import io
import os
import re
import shlex
import subprocess
import tarfile
from typing import Optional, Dict, List, Tuple, Callable

from .base import BaseUpgradeHelper, UpgradeResult
from .changelog import format_changelog_entry, remove_patch_from_spec, check_retrospective_news_changes

ARCHIVE_EXTENSIONS = (
    ".tar.xz",
    ".tar.gz",
    ".tar.bz2",
    ".tar.zst",
    ".tar",
    ".obscpio",
    ".zip",
)

NEWS_CANDIDATES = [
    "NEWS",
    "ChangeLog",
    "CHANGELOG.md",
    "CHANGELOG",
    "RELEASES.md",
    "RELEASE_NOTES.md",
]

BUILD_CANDIDATES = [
    "meson.build",
    "meson_options.txt",
    "CMakeLists.txt",
    "configure.ac",
]


class TarballUpgradeHelper(BaseUpgradeHelper):
    name: str = "tarball"
    description: str = "Direct Tarball / Spec (download_files / .spec)"

    @classmethod
    def can_handle(cls, package_dir: str) -> bool:
        """
        Can handle if:
          1. A .spec file exists
          2. No _service file with obs_scm / tar_scm exists
        """
        if not os.path.isdir(package_dir):
            return False

        specs = glob.glob(os.path.join(package_dir, "*.spec"))
        if not specs:
            return False

        service_path = os.path.join(package_dir, "_service")
        if os.path.isfile(service_path):
            try:
                with open(service_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                if "obs_scm" in content or "tar_scm" in content:
                    return False
            except OSError:
                pass

        return True

    @classmethod
    def get_current_revision(cls, package_dir: str) -> Optional[str]:
        """Extracts the active Version string from the package .spec file."""
        specs = glob.glob(os.path.join(package_dir, "*.spec"))
        if not specs:
            return None
        try:
            with open(specs[0], "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = re.match(r"^Version:\s*(\S+)", line, re.IGNORECASE)
                    if m:
                        return m.group(1).strip()
        except OSError:
            pass
        return None

    def find_spec_file(self) -> Optional[str]:
        specs = glob.glob(os.path.join(self.package_dir, "*.spec"))
        return specs[0] if specs else None

    def find_existing_archives(self) -> List[str]:
        """Finds existing source archive files directly in the package directory."""
        archives = []
        for f in os.listdir(self.package_dir):
            if any(f.endswith(ext) for ext in ARCHIVE_EXTENSIONS):
                # Ensure it's not a temporary review or diff file
                if not f.startswith("osc-collab.") and not f.startswith("."):
                    full_p = os.path.join(self.package_dir, f)
                    if os.path.isfile(full_p):
                        archives.append(full_p)
        return sorted(archives)

    @classmethod
    def extract_member_content(
        cls,
        archive_path: str,
        candidate_name: str,
        package_dir: Optional[str] = None
    ) -> Optional[str]:
        """
        Extracts text content of a named member from a tarball archive without unpacking to disk.
        Seamlessly streams contents through 'git lfs smudge' if the archive on disk is a Git-LFS pointer.
        """
        pkg_dir = package_dir or os.path.dirname(archive_path)

        # 1. Check if archive is a Git LFS pointer
        is_lfs = False
        try:
            with open(archive_path, "rb") as fh:
                header = fh.read(100)
                if header.startswith(b"version https://git-lfs.github.com/spec/"):
                    is_lfs = True
        except Exception:
            pass

        fileobj = None
        if is_lfs:
            try:
                res = subprocess.run(
                    ["git", "-C", pkg_dir, "lfs", "smudge"],
                    input=open(archive_path, "rb").read(),
                    capture_output=True,
                    check=True
                )
                if res.stdout:
                    fileobj = io.BytesIO(res.stdout)
            except Exception:
                fileobj = None

        # 2. Open tar archive (from memory buffer or disk file)
        try:
            if fileobj:
                tf = tarfile.open(fileobj=fileobj, mode="r:*")
            else:
                tf = tarfile.open(archive_path, "r:*")

            with tf:
                for member in tf.getmembers():
                    if member.name.endswith("/" + candidate_name) or member.name == candidate_name:
                        fh = tf.extractfile(member)
                        if fh:
                            return fh.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        return None

    def execute_upgrade(
        self,
        target_revision: Optional[str] = None,
        dry_run: bool = False,
        on_log: Optional[Callable[[str], None]] = None
    ) -> UpgradeResult:
        log = on_log or (lambda msg: None)

        spec_file = self.find_spec_file()
        if not spec_file:
            return UpgradeResult(False, "No .spec file found in package directory")

        spec_basename = os.path.basename(spec_file)
        package_name = os.path.splitext(spec_basename)[0]

        with open(spec_file, "r", encoding="utf-8", errors="replace") as f:
            original_spec_content = f.read()

        current_ver_match = re.search(r"^Version:\s*(\S+)", original_spec_content, re.MULTILINE | re.IGNORECASE)
        if not current_ver_match:
            return UpgradeResult(False, f"Could not determine current Version from {spec_basename}")
        current_version = current_ver_match.group(1).strip()

        # Target version resolution
        target_version = None
        if target_revision and target_revision != "@PARENT_TAG@":
            # Clean any leading 'v' e.g. 'v0.1.9' -> '0.1.9'
            target_version = re.sub(r"^v(?=\d)", "", target_revision.strip())
        else:
            # Query release-monitoring if available
            try:
                import sync_backend as sb
                parent_dir = os.path.dirname(self.package_dir)
                _, ver_info = sb.check_repo_version(package_name, None, workspace_path=parent_dir)
                cand = ver_info.get("upstream_latest") or ver_info.get("upstream_stable")
                if cand and cand not in ("—", "N/A"):
                    target_version = re.sub(r"^v(?=\d)", "", cand.strip())
            except Exception:
                pass

        if not target_version:
            return UpgradeResult(False, f"No target version specified for direct tarball package '{package_name}'")

        log(f"Starting Tarball update for '{package_name}' ({current_version} ➔ {target_version})")

        existing_archives = self.find_existing_archives()
        old_archive_path = None
        for a in existing_archives:
            if current_version in os.path.basename(a):
                old_archive_path = a
                break
        if not old_archive_path and existing_archives:
            old_archive_path = existing_archives[0]

        if dry_run:
            log(f"[DRY-RUN] Would update Version in {spec_basename}: {current_version} ➔ {target_version}")
            log(f"[DRY-RUN] Would run 'osc service mr download_files' to retrieve new source archive")
            return UpgradeResult(
                True,
                f"[DRY-RUN] Validation completed for {package_name} (target: {target_version})",
                package_name=package_name,
                old_version=current_version,
                new_version=target_version
            )

        # 1. Bump Version in .spec
        new_spec_content = re.sub(
            r"^(Version:\s*)\S+",
            r"\g<1>" + target_version,
            original_spec_content,
            count=1,
            flags=re.MULTILINE | re.IGNORECASE
        )
        with open(spec_file, "w", encoding="utf-8") as f:
            f.write(new_spec_content)
        log(f"Updated {spec_basename} Version: {current_version} ➔ {target_version}")

        # 2. Run 'osc service mr download_files'
        log("Executing 'osc service mr download_files' (downloading upstream source)...")
        try:
            res = subprocess.run(
                ["osc", "service", "mr", "download_files"],
                cwd=self.package_dir,
                capture_output=True,
                text=True,
                check=True
            )
            for line in res.stdout.splitlines():
                if "curl" in line or "download_files" in line:
                    log(f"  {line.strip()}")
        except subprocess.CalledProcessError as e:
            # Revert spec on failure
            with open(spec_file, "w", encoding="utf-8") as f:
                f.write(original_spec_content)
            err_msg = (e.stderr or e.stdout or "Failed running download_files service").strip()
            return UpgradeResult(False, f"download_files failed: {err_msg}", package_name=package_name)

        # 3. Verify new archive was downloaded
        current_archives = self.find_existing_archives()
        new_archive_path = None
        for a in current_archives:
            if target_version in os.path.basename(a):
                new_archive_path = a
                break

        if not new_archive_path:
            # Revert spec
            with open(spec_file, "w", encoding="utf-8") as f:
                f.write(original_spec_content)
            return UpgradeResult(False, f"New source archive for {target_version} was not found after download_files", package_name=package_name)

        log(f"Downloaded new source archive: {os.path.basename(new_archive_path)}")

        # 4. Extract and Diff Review Files (NEWS, meson.build, etc.)
        diff_files: Dict[str, str] = {}
        news_diff = None

        if old_archive_path and os.path.isfile(old_archive_path):
            log(f"Diffing upstream changes: {os.path.basename(old_archive_path)} ➔ {os.path.basename(new_archive_path)}...")

            # A. Diff NEWS / ChangeLog
            for nc in NEWS_CANDIDATES:
                old_txt = self.extract_member_content(old_archive_path, nc, package_dir=self.package_dir)
                new_txt = self.extract_member_content(new_archive_path, nc, package_dir=self.package_dir)
                if old_txt is not None and new_txt is not None:
                    diff = difflib.unified_diff(
                        old_txt.splitlines(keepends=True),
                        new_txt.splitlines(keepends=True),
                        fromfile=f"{nc}.old",
                        tofile=f"{nc}.new"
                    )
                    news_diff = "".join(diff)
                    collab_path = os.path.join(self.package_dir, "osc-collab.NEWS")
                    with open(collab_path, "w", encoding="utf-8") as fh:
                        fh.write(news_diff)
                    diff_files["osc-collab.NEWS"] = collab_path
                    log(f"Extracted release notes diff: osc-collab.NEWS ({nc})")
                    break

            # B. Diff Build files (meson.build, meson_options.txt, etc.)
            for bc in BUILD_CANDIDATES:
                old_b = self.extract_member_content(old_archive_path, bc, package_dir=self.package_dir)
                new_b = self.extract_member_content(new_archive_path, bc, package_dir=self.package_dir)
                if old_b is not None and new_b is not None:
                    diff_b = "".join(difflib.unified_diff(
                        old_b.splitlines(keepends=True),
                        new_b.splitlines(keepends=True),
                        fromfile=f"{bc}.old",
                        tofile=f"{bc}.new"
                    ))
                    # Map filename e.g. meson.build -> osc-collab.meson
                    clean_bc = bc.replace(".build", "").replace(".txt", "")
                    collab_b_path = os.path.join(self.package_dir, f"osc-collab.{clean_bc}")
                    with open(collab_b_path, "w", encoding="utf-8") as fh:
                        fh.write(diff_b)
                    diff_files[f"osc-collab.{clean_bc}"] = collab_b_path
                    log(f"Extracted build file diff: osc-collab.{clean_bc}")

        # 5. Format and Record .changes Entry via 'osc vc -F'
        has_retro = False
        if news_diff:
            has_retro = check_retrospective_news_changes(news_diff)
            if has_retro:
                log("⚠️  Notice: Upstream NEWS diff contains additions to older release sections (e.g. historical CVE/GHSA annotations). Inspect 'osc-collab.NEWS' if past .changes entries should be updated.")
            formatted_entry = format_changelog_entry(news_diff, target_version)
        else:
            formatted_entry = f"- Update to version {target_version}."

        tmp_news = os.path.join(self.package_dir, ".NEWS")
        try:
            with open(tmp_news, "w", encoding="utf-8") as fh:
                fh.write(formatted_entry.rstrip() + "\n")
            log("Recording changes entry using 'osc vc -F'...")
            subprocess.run(
                ["osc", "vc", "-F", tmp_news],
                cwd=self.package_dir,
                capture_output=True,
                check=True
            )
        except subprocess.CalledProcessError as e:
            log(f"Warning: 'osc vc -F' failed: {e.stderr.decode() if e.stderr else e}")
        finally:
            if os.path.isfile(tmp_news):
                os.remove(tmp_news)

        # 6. Clean stale old archive
        if old_archive_path and os.path.isfile(old_archive_path) and old_archive_path != new_archive_path:
            old_name = os.path.basename(old_archive_path)
            try:
                os.remove(old_archive_path)
                log(f"Removed stale archive: {old_name}")
            except OSError:
                pass

        log(f"Successfully upgraded {package_name} to {target_version}!")
        return UpgradeResult(
            success=True,
            message=f"Successfully upgraded {package_name} to version {target_version}",
            package_name=package_name,
            old_version=current_version,
            new_version=target_version,
            diff_files=diff_files,
            has_retrospective_news=has_retro
        )
