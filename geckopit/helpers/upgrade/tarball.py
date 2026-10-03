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
import tempfile
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
    "meson_options.json",
    "CMakeLists.txt",
    "configure.ac",
]


MAX_LFS_SMUDGE_SIZE = 100 * 1024 * 1024  # 100 MB safety guard for in-memory smudging


def parse_lfs_pointer(file_path: str) -> Tuple[bool, Optional[str], Optional[int]]:
    """
    Parses a file to determine if it is a Git LFS pointer.
    Returns (is_lfs, oid, size_in_bytes).
    """
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            header = fh.read(250)
            if header.startswith("version https://git-lfs.github.com/spec/"):
                m_oid = re.search(r"^oid\s+sha256:([0-9a-fA-F]{64})", header, re.MULTILINE)
                m_size = re.search(r"^size\s+(\d+)", header, re.MULTILINE)
                oid = m_oid.group(1) if m_oid else None
                size = int(m_size.group(1)) if m_size else None
                return True, oid, size
    except Exception:
        pass
    return False, None, None


def find_local_lfs_object(pkg_dir: str, oid: str) -> Optional[str]:
    """
    Checks if a Git LFS object is already cached on local disk under .git/lfs/objects/.
    """
    if not oid or len(oid) < 4:
        return None
    try:
        common_dir = subprocess.run(
            ["git", "-C", pkg_dir, "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True
        ).stdout.strip()
        if not os.path.isabs(common_dir):
            common_dir = os.path.abspath(os.path.join(pkg_dir, common_dir))
        candidate = os.path.join(common_dir, "lfs", "objects", oid[:2], oid[2:4], oid)
        if os.path.isfile(candidate):
            return candidate
    except Exception:
        pass
    return None


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
        Safely resolves Git-LFS pointers using direct local disk cache access or size-guarded smudging.
        """
        pkg_dir = package_dir or os.path.dirname(archive_path)

        # 1. Check if archive is a Git LFS pointer
        is_lfs, oid, size = parse_lfs_pointer(archive_path)

        tar_source = archive_path
        fileobj = None

        if is_lfs:
            # A. Check if the object is already stored on local disk in .git/lfs/objects/
            local_cache = find_local_lfs_object(pkg_dir, oid) if oid else None
            if local_cache:
                # Open directly from disk cache: zero RAM overhead and zero network calls!
                tar_source = local_cache
            else:
                # B. Not cached locally: enforce defensive size guard before invoking smudge
                if size and size > MAX_LFS_SMUDGE_SIZE:
                    # Giant archive (e.g. chromium, libreoffice): skip to protect memory/bandwidth
                    return None

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
                    return None

        # 2. Open tar archive (from disk file or in-memory buffer)
        try:
            if fileobj:
                tf = tarfile.open(fileobj=fileobj, mode="r:*")
            else:
                tf = tarfile.open(tar_source, mode="r:*")

            with tf:
                matches = [m for m in tf.getmembers() if m.name.endswith("/" + candidate_name) or m.name == candidate_name]
                if matches:
                    # Prefer shallowest path (root-level member)
                    matches.sort(key=lambda m: m.name.count("/"))
                    fh = tf.extractfile(matches[0])
                    if fh:
                        return fh.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        return None

    def audit_and_drop_merged_patches(
        self,
        new_archive_path: str,
        on_log: Optional[Callable[[str], None]] = None
    ) -> List[str]:
        """
        Scans package directory for *.patch files and checks if each patch
        has been merged upstream into new_archive_path using reverse patch application.
        If merged, removes patch from disk/SCM, updates .spec, and returns dropped patch list.
        """
        log = on_log or (lambda msg: None)
        dropped_patches = []

        patch_files = sorted(glob.glob(os.path.join(self.package_dir, "*.patch")))
        if not patch_files or not os.path.isfile(new_archive_path):
            return dropped_patches

        is_lfs, oid, size = parse_lfs_pointer(new_archive_path)
        tar_source = new_archive_path
        fileobj = None
        if is_lfs:
            local_cache = find_local_lfs_object(self.package_dir, oid) if oid else None
            if local_cache:
                tar_source = local_cache
            else:
                data = smudge_lfs_object_in_memory(self.package_dir, oid, size)
                if data:
                    fileobj = io.BytesIO(data)
                else:
                    return dropped_patches

        for patch_path in patch_files:
            patch_name = os.path.basename(patch_path)
            try:
                with open(patch_path, "r", encoding="utf-8", errors="replace") as pf:
                    patch_text = pf.read()
            except OSError:
                continue

            target_files = set()
            for line in patch_text.splitlines():
                m = re.match(r"^(?:---|\+\+\+)\s+(\S+)", line)
                if m:
                    raw = m.group(1).strip()
                    if raw != "/dev/null":
                        cleaned = re.sub(r"^[ab]/", "", raw)
                        target_files.add(cleaned)

            if not target_files:
                continue

            is_merged = False
            try:
                if fileobj:
                    fileobj.seek(0)
                    tf = tarfile.open(fileobj=fileobj, mode="r:*")
                else:
                    tf = tarfile.open(tar_source, mode="r:*")

                with tempfile.TemporaryDirectory() as extract_dir:
                    root_prefix = None
                    extract_kwargs = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
                    for member in tf.getmembers():
                        parts = member.name.split("/", 1)
                        rel_path = parts[1] if len(parts) > 1 else parts[0]
                        if rel_path in target_files:
                            if root_prefix is None and len(parts) > 1:
                                root_prefix = parts[0]
                            tf.extract(member, path=extract_dir, **extract_kwargs)

                    test_dir = os.path.join(extract_dir, root_prefix) if root_prefix else extract_dir

                    for p_num in ["-p1", "-p0"]:
                        res = subprocess.run(
                            ["patch", "--dry-run", "--reverse", "--forward", "--batch", p_num, "-d", test_dir, "-i", patch_path],
                            capture_output=True,
                            text=True,
                            check=False
                        )
                        if res.returncode == 0:
                            is_merged = True
                            break
                tf.close()
            except Exception as e:
                log(f"Notice: Could not audit patch {patch_name}: {e}")

            if is_merged:
                dropped_patches.append(patch_name)
                log(f"Detected merged patch: {patch_name} (carried upstream)")

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

                spec_file = self.find_spec_file()
                if spec_file:
                    try:
                        with open(spec_file, "r", encoding="utf-8") as f:
                            orig_spec = f.read()
                        clean_spec = remove_patch_from_spec(orig_spec, patch_name)
                        if clean_spec != orig_spec:
                            with open(spec_file, "w", encoding="utf-8") as f:
                                f.write(clean_spec)
                            log(f"Removed '{patch_name}' declaration from {os.path.basename(spec_file)}")
                    except Exception:
                        pass

        return dropped_patches

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

        # Extract referenced files before version bump
        old_referenced = self.get_referenced_files(spec_path=spec_file)

        # Calculate new spec content with bumped Version
        new_spec_content = re.sub(
            r"^(Version:\s*)\S+",
            r"\g<1>" + target_version,
            original_spec_content,
            count=1,
            flags=re.MULTILINE | re.IGNORECASE
        )

        new_referenced = self.get_referenced_files(spec_content=new_spec_content)
        obsolete_files = set(old_referenced - new_referenced)
        if old_archive_path and os.path.isfile(old_archive_path):
            old_name = os.path.basename(old_archive_path)
            if old_name not in new_referenced:
                obsolete_files.add(old_name)

        if dry_run:
            log(f"[DRY-RUN] Would update Version in {spec_basename}: {current_version} ➔ {target_version}")
            log(f"[DRY-RUN] Would run 'osc service mr download_files' to retrieve new source archive")
            candidate_removals = []
            for f in sorted(obsolete_files):
                if os.path.isfile(os.path.join(self.package_dir, f)):
                    log(f"[DRY-RUN] Would remove obsolete file: {f}")
                    candidate_removals.append(f)
            return UpgradeResult(
                True,
                f"[DRY-RUN] Validation completed for {package_name} (target: {target_version})",
                package_name=package_name,
                old_version=current_version,
                new_version=target_version,
                removed_files=candidate_removals
            )

        # 1. Bump Version in .spec
        with open(spec_file, "w", encoding="utf-8") as f:
            f.write(new_spec_content)
        log(f"Updated {spec_basename} Version: {current_version} ➔ {target_version}")

        disk_new_referenced = self.get_referenced_files(spec_path=spec_file)
        if disk_new_referenced:
            new_referenced = disk_new_referenced
            obsolete_files = set(old_referenced - new_referenced)
            if old_archive_path and os.path.isfile(old_archive_path):
                old_name = os.path.basename(old_archive_path)
                if old_name not in new_referenced:
                    obsolete_files.add(old_name)

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
                    clean_bc = bc.replace(".build", "").replace(".txt", "").replace(".json", "")
                    collab_b_path = os.path.join(self.package_dir, f"osc-collab.{clean_bc}")
                    with open(collab_b_path, "w", encoding="utf-8") as fh:
                        fh.write(diff_b)
                    diff_files[f"osc-collab.{clean_bc}"] = collab_b_path
                    log(f"Extracted build file diff: osc-collab.{clean_bc}")

        if diff_files:
            self.ensure_gitignore_pattern("osc-collab.*")

        # 4b. Audit and Drop Merged Patches
        dropped_patches = self.audit_and_drop_merged_patches(new_archive_path, on_log=log)

        # 4b2. Sequentially verify remaining downstream patches
        try:
            import patch_checker
            p_res = patch_checker.check_patches_sequential(self.package_dir)
            if not p_res["success"] and p_res["failed_patch"]:
                log(f"⚠️  Notice: Downstream patch '{p_res['failed_patch']}' fails to apply to {target_version}!")
                log("   Run 'geckopit-cli --check-patches' to inspect and re-verify after rebasing.")
        except Exception:
            pass

        # 4c. Audit and Fix Meson Dependency Drift
        fixed_meson_drifts = []
        try:
            import meson_drift
            fixed_meson_drifts = meson_drift.fix_meson_drift(self.package_dir, on_log=log)
        except Exception:
            pass

        # 5. Format and Record .changes Entry via 'osc vc -F'
        has_retro = False
        has_meson_update = bool(fixed_meson_drifts)
        if news_diff:
            has_retro = check_retrospective_news_changes(news_diff)
            if has_retro:
                log("⚠️  Notice: Upstream NEWS diff contains additions to older release sections (e.g. historical CVE/GHSA annotations). Inspect 'osc-collab.NEWS' if past .changes entries should be updated.")
            formatted_entry = format_changelog_entry(news_diff, target_version, dropped_patches=dropped_patches, meson_deps_updated=has_meson_update)
        else:
            formatted_entry = format_changelog_entry("", target_version, dropped_patches=dropped_patches, meson_deps_updated=has_meson_update)

        tmp_news = None
        try:
            with tempfile.NamedTemporaryFile("w", dir=self.package_dir, prefix=".NEWS-", delete=False, encoding="utf-8") as fh:
                fh.write(formatted_entry.rstrip() + "\n")
                tmp_news = fh.name
            log("Recording changes entry using 'osc vc -F'...")
            subprocess.run(
                ["osc", "vc", "-F", os.path.basename(tmp_news)],
                cwd=self.package_dir,
                capture_output=True,
                check=True
            )
        except subprocess.CalledProcessError as e:
            log(f"Warning: 'osc vc -F' failed: {e.stderr.decode() if e.stderr else e}")
        finally:
            if tmp_news and os.path.exists(tmp_news):
                try:
                    os.remove(tmp_news)
                except OSError:
                    pass

        # 6. Clean obsolete files no longer referenced in spec (and any stale old archives)
        if new_archive_path:
            obsolete_files.discard(os.path.basename(new_archive_path))
        removed_files = self.clean_obsolete_files(obsolete_files, on_log=log)

        log(f"Successfully upgraded {package_name} to {target_version}!")
        return UpgradeResult(
            success=True,
            message=f"Successfully upgraded {package_name} to version {target_version}",
            package_name=package_name,
            old_version=current_version,
            new_version=target_version,
            diff_files=diff_files,
            has_retrospective_news=has_retro,
            removed_files=removed_files,
            fixed_meson_drifts=fixed_meson_drifts
        )
