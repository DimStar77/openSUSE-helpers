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
import tempfile
from typing import Optional, Callable, Dict, Tuple, List

from .base import BaseUpgradeHelper, UpgradeResult
from .changelog import (
    CHANGELOG_WRAP_WIDTH,
    wrap_bullet,
    format_changelog_entry,
    remove_patch_from_spec,
    check_retrospective_news_changes,
    extract_appstream_notes,
    APPSTREAM_XML_REGEX
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
        """Determines canonical package name (preferring .spec file over URL basename)."""
        base = os.path.basename(self.package_dir)
        try:
            if os.path.isfile(os.path.join(self.package_dir, f"{base}.spec")):
                return base
            specs = [f[:-5] for f in os.listdir(self.package_dir) if f.endswith(".spec")]
            if specs:
                return specs[0]
        except Exception:
            pass

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

        return base

    def get_obsinfo_path(self, pkg_name: Optional[str] = None) -> Optional[str]:
        """
        Locates the primary .obsinfo file in package_dir.
        Checks service filename param, package name, repo URL name, case variants,
        and filters out submodule obsinfos.
        """
        service_file = os.path.join(self.package_dir, "_service")
        filename_param = None
        repo_name = None
        submodule_names = set()

        if os.path.isfile(service_file):
            try:
                tree = ET.parse(service_file)
                root = tree.getroot()
                for service in root.findall("service"):
                    if service.get("name") in ("obs_scm", "tar_scm"):
                        vfmt = None
                        fname = None
                        url_text = None
                        for p in service.findall("param"):
                            p_name = p.get("name")
                            if p_name == "versionformat":
                                vfmt = p.text.strip() if p.text else ""
                            elif p_name == "filename":
                                fname = p.text.strip() if p.text else ""
                            elif p_name == "url":
                                url_text = p.text.strip() if p.text else ""

                        rname = None
                        if url_text:
                            u = re.sub(r"\.git/?$", "", url_text.strip())
                            rname = u.rstrip("/").split("/")[-1]

                        if vfmt == "0.gitmodule":
                            if fname:
                                submodule_names.add(fname)
                            if rname:
                                submodule_names.add(rname)
                            continue

                        if not filename_param and fname:
                            filename_param = fname
                        if not repo_name and rname:
                            repo_name = rname
            except Exception:
                pass

            if not filename_param or not repo_name:
                try:
                    with open(service_file, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    if not filename_param:
                        m_fn = re.search(r'<param\s+name=["\']filename["\']>([^<]+)</param>', content)
                        if m_fn:
                            filename_param = m_fn.group(1).strip()
                    if not repo_name:
                        m_u = re.search(r'<param\s+name=["\']url["\']>([^<]+)</param>', content)
                        if m_u:
                            u = re.sub(r"\.git/?$", "", m_u.group(1).strip())
                            repo_name = u.rstrip("/").split("/")[-1]
                except Exception:
                    pass

        actual_pkg = pkg_name or self.get_package_name()

        candidates = []
        if filename_param:
            candidates.append(f"{filename_param}.obsinfo")
        if actual_pkg:
            candidates.append(f"{actual_pkg}.obsinfo")
        if repo_name and repo_name not in candidates:
            candidates.append(f"{repo_name}.obsinfo")

        # 1. Exact matches
        for cand in candidates:
            p = os.path.join(self.package_dir, cand)
            if os.path.isfile(p):
                return p

        # 2. Case-insensitive matches
        try:
            dir_files = os.listdir(self.package_dir)
            lower_map = {f.lower(): f for f in dir_files if f.endswith(".obsinfo")}
            for cand in candidates:
                if cand.lower() in lower_map:
                    return os.path.join(self.package_dir, lower_map[cand.lower()])

            # 3. Fallback: filter out submodules
            all_obsinfos = [f for f in dir_files if f.endswith(".obsinfo")]
            non_sub = [f for f in all_obsinfos if f[:-8] not in submodule_names]
            if len(non_sub) == 1:
                return os.path.join(self.package_dir, non_sub[0])
            if len(all_obsinfos) == 1:
                return os.path.join(self.package_dir, all_obsinfos[0])
        except OSError:
            pass

        return None

    def get_obsinfo_metadata(self, pkg_name: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
        """Returns (version, commit) from the package's .obsinfo file."""
        obsinfo_file = self.get_obsinfo_path(pkg_name)
        if not obsinfo_file or not os.path.isfile(obsinfo_file):
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

    def get_upstream_repo_dir(self, pkg_name: Optional[str] = None) -> Optional[str]:
        """
        Locates the directory where obs_scm cloned the upstream repository.
        Checks repo name from URL, package name, case variations, and subdirectories with .git.
        """
        service_file = os.path.join(self.package_dir, "_service")
        repo_name = None

        if os.path.isfile(service_file):
            try:
                tree = ET.parse(service_file)
                root = tree.getroot()
                for service in root.findall("service"):
                    if service.get("name") in ("obs_scm", "tar_scm"):
                        vfmt = None
                        url_text = None
                        for p in service.findall("param"):
                            p_name = p.get("name")
                            if p_name == "versionformat":
                                vfmt = p.text.strip() if p.text else ""
                            elif p_name == "url":
                                url_text = p.text.strip() if p.text else ""

                        if vfmt == "0.gitmodule":
                            continue

                        if url_text:
                            u = re.sub(r"\.git/?$", "", url_text.strip())
                            repo_name = u.rstrip("/").split("/")[-1]
                            break
            except Exception:
                pass

            if not repo_name:
                try:
                    with open(service_file, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    m = re.search(r'<param\s+name=["\']url["\']>([^<]+)</param>', content)
                    if m:
                        u = re.sub(r"\.git/?$", "", m.group(1).strip())
                        repo_name = u.rstrip("/").split("/")[-1]
                except Exception:
                    pass

        actual_pkg = pkg_name or self.get_package_name()

        candidates = []
        if repo_name:
            candidates.append(repo_name)
        if actual_pkg and actual_pkg not in candidates:
            candidates.append(actual_pkg)

        # 1. Exact candidate directory paths
        for cand in candidates:
            p = os.path.join(self.package_dir, cand)
            if os.path.isdir(p) and (os.path.isdir(os.path.join(p, ".git")) or os.path.isfile(os.path.join(p, ".git"))):
                return p

        # 2. Case-insensitive candidate paths
        try:
            subdirs = [d for d in os.listdir(self.package_dir) if os.path.isdir(os.path.join(self.package_dir, d))]
            lower_map = {d.lower(): d for d in subdirs}
            for cand in candidates:
                if cand.lower() in lower_map:
                    p = os.path.join(self.package_dir, lower_map[cand.lower()])
                    if os.path.isdir(os.path.join(p, ".git")) or os.path.isfile(os.path.join(p, ".git")):
                        return p

            # 3. Fallback: any subdirectory containing .git that is not .osc or hidden
            for d in subdirs:
                if d.startswith("."):
                    continue
                p = os.path.join(self.package_dir, d)
                if os.path.isdir(os.path.join(p, ".git")) or os.path.isfile(os.path.join(p, ".git")):
                    return p
        except OSError:
            pass

        return None

    def is_git_managed(self) -> bool:
        """Returns True if this package directory is part of a git repository (not a legacy osc checkout)."""
        if os.path.isdir(os.path.join(self.package_dir, ".osc")) and not os.path.exists(os.path.join(self.package_dir, ".git")):
            return False
        try:
            res = subprocess.run(
                ["git", "-C", self.package_dir, "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                text=True,
                check=False
            )
            return res.returncode == 0 and res.stdout.strip() == "true"
        except Exception:
            return False

    def uses_obscpio(self) -> bool:
        """Returns True if the package is configured to produce or track .obscpio archives."""
        service_file = os.path.join(self.package_dir, "_service")
        if os.path.isfile(service_file):
            try:
                with open(service_file, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                is_manual_tar = bool(re.search(r'["\']tar["\'].*["\']manual["\']', content) or re.search(r'name=["\']tar["\']\s+mode=["\']manual["\']', content))
                is_local_tar = bool(re.search(r'["\']tar["\'].*["\']local["\']', content) or re.search(r'name=["\']tar["\']\s+mode=["\']local["\']', content))
                if is_manual_tar or is_local_tar:
                    return False
                if re.search(r'["\']tar["\'].*["\']buildtime["\']', content) or re.search(r'name=["\']tar["\']\s+mode=["\']buildtime["\']', content):
                    return True
            except OSError:
                pass
        return bool(glob.glob(os.path.join(self.package_dir, "*.obscpio")))

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

    def find_upstream_changelog_target(
        self,
        upstream_repo: str,
        old_rev: Optional[str] = None,
        new_rev: Optional[str] = None
    ) -> str:
        """
        Discovers the upstream release notes file.
        Prioritizes condensed, release-targeted files (NEWS*) over commit-dump logs (ChangeLog*).
        If multiple exist and old_rev is provided, prioritizes the highest-precedence file
        that was actually modified in this release. Also checks for AppStream metainfo/appdata XML.
        """
        candidates = [
            "NEWS", "NEWS.md", "NEWS.rst", "NEWS.txt",
            "RELEASES.md", "releasenotes.txt",
            "CHANGELOG.md", "CHANGELOG.rst", "ChangeLog"
        ]

        # 1. If old_rev is available, check which candidates actually changed in git
        rev_range = f"{old_rev}..{new_rev or 'HEAD'}" if old_rev else None
        if rev_range:
            try:
                res = subprocess.run(
                    ["git", "-C", upstream_repo, "diff", "--no-ext-diff", "--name-only", rev_range, "--"] + candidates,
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

            # 1b. Check if an AppStream metainfo/appdata XML file was modified
            try:
                res_all = subprocess.run(
                    ["git", "-C", upstream_repo, "diff", "--no-ext-diff", "--name-only", rev_range],
                    capture_output=True,
                    text=True,
                    check=False
                )
                if res_all.returncode == 0:
                    for f in res_all.stdout.splitlines():
                        if APPSTREAM_XML_REGEX.search(f):
                            return f
            except Exception:
                pass

        # 2. Fallback to existence priority
        for c in candidates:
            if os.path.isfile(os.path.join(upstream_repo, c)):
                return c

        # 2b. Check for AppStream XML file on disk
        for root, _, files in os.walk(upstream_repo):
            for f in files:
                if APPSTREAM_XML_REGEX.search(f):
                    return os.path.relpath(os.path.join(root, f), upstream_repo)

        return "NEWS"

    def extract_git_diffs(
        self,
        pkg_name: Optional[str] = None,
        old_rev: Optional[str] = None,
        new_rev: Optional[str] = None,
        new_ver: Optional[str] = None,
        on_log: Optional[Callable[[str], None]] = None
    ) -> Dict[str, str]:
        """Extracts upstream git diffs between old_rev and new_rev (or HEAD) for NEWS/ChangeLog and meson build files."""
        diff_files = {}
        upstream_repo = self.get_upstream_repo_dir(pkg_name)
        if not (upstream_repo and os.path.isdir(upstream_repo) and (os.path.isdir(os.path.join(upstream_repo, ".git")) or os.path.isfile(os.path.join(upstream_repo, ".git")))):
            return diff_files

        # Auto-detect upstream changelog filename (evaluating diff activity against old_rev)
        changelog_target = self.find_upstream_changelog_target(upstream_repo, old_rev=old_rev, new_rev=new_rev)

        is_appstream = bool(APPSTREAM_XML_REGEX.search(changelog_target))
        if is_appstream:
            xml_path = os.path.join(upstream_repo, changelog_target)
            notes = extract_appstream_notes(xml_path, version=new_ver)
            if notes:
                raw_lines = notes.splitlines()
                diff_lines = [
                    f"--- a/{changelog_target}",
                    f"+++ b/{changelog_target}",
                    f"@@ -0,0 +1,{len(raw_lines)} @@"
                ]
                for l in raw_lines:
                    diff_lines.append("+" + l)
                diff_text = "\n".join(diff_lines) + "\n"
                out_path = os.path.join(self.package_dir, "osc-collab.NEWS")
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(diff_text)
                diff_files["osc-collab.NEWS"] = diff_text
                if on_log:
                    on_log(f"Extracted AppStream release notes from {changelog_target} for {new_ver or 'HEAD'}")

        targets = []
        if not is_appstream and old_rev:
            targets.append((changelog_target, "osc-collab.NEWS"))
        if old_rev:
            targets.extend([
                ("meson.build", "osc-collab.meson"),
                ("meson_options.txt", "osc-collab.meson_options"),
                ("meson_options.json", "osc-collab.meson_options")
            ])

        rev_range = f"{old_rev}..{new_rev or 'HEAD'}" if old_rev else None
        if rev_range:
            for git_target, out_filename in targets:
                try:
                    res = subprocess.run(
                        ["git", "-C", upstream_repo, "diff", "--no-ext-diff", rev_range, "--", git_target],
                        capture_output=True,
                        text=True,
                        check=False
                    )
                    if res.returncode == 0 and res.stdout.strip():
                        out_path = os.path.join(self.package_dir, out_filename)
                        with open(out_path, "w", encoding="utf-8") as f:
                            f.write(res.stdout)
                        diff_files[out_filename] = res.stdout
                except Exception as e:
                    if on_log:
                        on_log(f"Notice: Could not extract diff for {git_target}: {e}")

        if diff_files:
            self.ensure_gitignore_pattern("osc-collab.*")
            if on_log:
                on_log(f"Extracted upstream diffs: {', '.join(diff_files.keys())}")

        return diff_files

    def audit_and_drop_merged_patches(
        self,
        pkg_name: Optional[str] = None,
        on_log: Optional[Callable[[str], None]] = None
    ) -> List[str]:
        """
        Scans package directory for *.patch files.
        Checks if each patch is merged into upstream HEAD (by commit SHA or reverse apply).
        If merged, deletes patch file, cleans spec file, and returns dropped patch list.
        """
        dropped_patches = []
        upstream_repo = self.get_upstream_repo_dir(pkg_name)
        if not (upstream_repo and os.path.isdir(upstream_repo) and (os.path.isdir(os.path.join(upstream_repo, ".git")) or os.path.isfile(os.path.join(upstream_repo, ".git")))):
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
        meson_deps_updated: bool = False,
        on_log: Optional[Callable[[str], None]] = None
    ) -> bool:
        """Formats 67-column changelog entry and records it via non-interactive 'osc vc -F'."""
        changelog_content = format_changelog_entry(
            diff_text,
            new_version,
            dropped_patches=dropped_patches,
            meson_deps_updated=meson_deps_updated,
            width=CHANGELOG_WRAP_WIDTH
        )

        tmp_news = None
        try:
            with tempfile.NamedTemporaryFile("w", dir=self.package_dir, prefix=".NEWS-", delete=False, encoding="utf-8") as f:
                f.write(changelog_content + "\n")
                tmp_news = f.name

            rel_tmp_news = os.path.basename(tmp_news)
            res = subprocess.run(
                ["osc", "vc", "-F", rel_tmp_news],
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
            if tmp_news and os.path.exists(tmp_news):
                try:
                    os.remove(tmp_news)
                except OSError:
                    pass

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
        diff_files = self.extract_git_diffs(
            pkg_name=pkg_name,
            old_rev=old_rev,
            new_rev=new_rev,
            new_ver=new_ver,
            on_log=log
        )

        # 6. Source duplication hygiene & obscpio git check
        self.clean_duplicate_obscpio_if_manual(on_log=log)
        is_obscpio_git = self.is_git_managed() and self.uses_obscpio()
        if is_obscpio_git:
            log("⚠️  Notice: Package is using buildtime obscpio in a git-managed repository. Storing .obscpio in Git LFS is discouraged. Migrating away from obscpio (e.g. via migrate_service.sh to manual tar.xz) is highly recommended.")

        # 7. Audit and drop merged patches
        dropped_patches = self.audit_and_drop_merged_patches(pkg_name, on_log=log)

        # 7b. Sequentially verify remaining downstream patches
        try:
            import patch_checker
            p_res = patch_checker.check_patches_sequential(self.package_dir)
            if not p_res["success"] and p_res["failed_patch"]:
                log(f"⚠️  Notice: Downstream patch '{p_res['failed_patch']}' fails to apply to {new_ver or rev}!")
                log("   Run 'geckopit-cli --check-patches' to inspect and re-verify after rebasing.")
        except Exception:
            pass

        # 8. Update Version: in *.spec
        if new_ver:
            self.update_spec_version(new_ver, on_log=log)

        # 8b. Audit and fix Meson dependency drift
        fixed_meson_drifts = []
        try:
            import meson_drift
            fixed_meson_drifts = meson_drift.fix_meson_drift(self.package_dir, on_log=log)
        except Exception:
            pass

        # 9. Update changelog via non-interactive osc vc
        news_diff = diff_files.get("osc-collab.NEWS", "")
        has_retro = False
        if news_diff:
            has_retro = check_retrospective_news_changes(news_diff)
            if has_retro:
                log("⚠️  Notice: Upstream NEWS diff contains additions to older release sections (e.g. historical CVE/GHSA annotations). Inspect 'osc-collab.NEWS' if past .changes entries should be updated.")
        else:
            log("⚠️  Notice: No upstream NEWS/changelog diff found. Please review upstream release notes.")

        effective_ver = new_ver or (rev.lstrip("v") if not rev.startswith("@") else None)
        if effective_ver and (old_rev != new_rev or not old_rev):
            self.update_changelog_via_osc(
                effective_ver,
                diff_text=news_diff,
                dropped_patches=dropped_patches,
                meson_deps_updated=bool(fixed_meson_drifts),
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
            has_retrospective_news=has_retro,
            has_obscpio_warning=is_obscpio_git,
            fixed_meson_drifts=fixed_meson_drifts
        )
