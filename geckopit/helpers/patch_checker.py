#!/usr/bin/env python3
"""
Sequential Downstream Patch Pre-Flight Verifier.
Validates downstream patches in strict .spec declaration order against the upstream
source tree (either unpacked tarball members or upstream OBS SCM git clone).

Like quilt, it aborts at the first failing patch to prevent false cascade errors.
Unlike quilt, re-running after rebasing requires zero directory teardowns or setup.
"""

import os
import re
import sys
import glob
import tarfile
import tempfile
import subprocess
from typing import Optional, List, Dict, Any, Callable

# Ensure terminal colors if available
BOLD = "\033[1m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"

try:
    from upgrade.tarball import parse_lfs_pointer, find_local_lfs_object
except ImportError:
    from .upgrade.tarball import parse_lfs_pointer, find_local_lfs_object

def get_spec_patches(package_dir: str) -> List[str]:
    """
    Parses all active downstream patches declared in the package's .spec file,
    preserving declaration order and expanding standard %define / %global / Name macros.
    """
    spec_files = glob.glob(os.path.join(package_dir, "*.spec"))
    if not spec_files:
        return []

    macros: Dict[str, str] = {}
    patches: List[str] = []

    re_macro = re.compile(r'^\s*%(?:define|global)\s+(\w+)\s+(.+)$', re.IGNORECASE)
    re_tag = re.compile(r'^\s*(Name|Version)\s*:\s*(.+)$', re.IGNORECASE)
    re_patch = re.compile(r'^\s*Patch(?:\d+)?\s*:\s*(\S+)', re.IGNORECASE)

    try:
        with open(spec_files[0], 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                m_mac = re_macro.match(line)
                if m_mac:
                    macros[m_mac.group(1)] = m_mac.group(2).strip()
                m_tag = re_tag.match(line)
                if m_tag:
                    macros[m_tag.group(1).lower()] = m_tag.group(2).strip()
                m_p = re_patch.match(line)
                if m_p:
                    raw_name = m_p.group(1).strip()
                    for k, v in macros.items():
                        raw_name = raw_name.replace(f"%{{{k}}}", v).replace(f"%{k}", v)
                    patches.append(os.path.basename(raw_name))
    except Exception:
        pass

    return patches

def check_patches_sequential(
    package_dir: str,
    on_log: Optional[Callable[[str], None]] = None
) -> Dict[str, Any]:
    """
    Sequentially tests downstream patches against the upstream source tree.
    Halts immediately upon the first failing patch and reports the failure reason.
    Returns a dictionary summarizing applied, failed, and skipped patches.
    """
    log = on_log or (lambda msg: None)
    pkg_dir = os.path.abspath(package_dir)
    pkg_name = os.path.basename(pkg_dir)

    patches = get_spec_patches(pkg_dir)
    if not patches:
        return {
            "success": True,
            "total": 0,
            "applied": [],
            "failed_patch": None,
            "error_message": None,
            "skipped": [],
            "source_type": "none"
        }

    # 1. Check for OBS SCM upstream git repository
    service_file = os.path.join(pkg_dir, "_service")
    is_obs_scm = False
    if os.path.isfile(service_file):
        try:
            with open(service_file, 'r', encoding='utf-8', errors='ignore') as sf:
                content = sf.read()
                if "obs_scm" in content or "tar_scm" in content:
                    is_obs_scm = True
        except Exception:
            pass

    if is_obs_scm:
        repo_dir = os.path.join(pkg_dir, pkg_name)
        if not os.path.isdir(os.path.join(repo_dir, ".git")):
            # Check child directories for git repo
            for item in os.listdir(pkg_dir):
                sub = os.path.join(pkg_dir, item)
                if os.path.isdir(os.path.join(sub, ".git")) and item != ".git":
                    repo_dir = sub
                    break

        if os.path.isdir(os.path.join(repo_dir, ".git")):
            # Test sequentially against upstream git clone
            applied = []
            failed_patch = None
            error_message = None

            try:
                # Reset to clean HEAD
                subprocess.run(["git", "-C", repo_dir, "reset", "--hard", "HEAD"], capture_output=True)
                subprocess.run(["git", "-C", repo_dir, "clean", "-fd"], capture_output=True)

                for idx, p in enumerate(patches, 1):
                    p_path = os.path.join(pkg_dir, p)
                    if not os.path.isfile(p_path):
                        failed_patch = p
                        error_message = f"Patch file '{p}' does not exist on disk"
                        log(f"[{idx}/{len(patches)}] ❌ {p}: File missing on disk!")
                        break

                    applied_cleanly = False
                    err_out = ""
                    for p_num in ["-p1", "-p0"]:
                        res = subprocess.run(
                            ["git", "-C", repo_dir, "apply", p_num, p_path],
                            capture_output=True,
                            text=True
                        )
                        if res.returncode == 0:
                            applied_cleanly = True
                            break
                        else:
                            err_out = res.stderr.strip()

                    if applied_cleanly:
                        applied.append(p)
                        log(f"[{idx}/{len(patches)}] ✅ {p}: Applied cleanly")
                    else:
                        failed_patch = p
                        error_message = err_out or "git apply returned non-zero"
                        log(f"[{idx}/{len(patches)}] ❌ {p}: Failed to apply!")
                        if err_out:
                            for l in err_out.splitlines()[:4]:
                                log(f"    {l}")
                        break
            finally:
                subprocess.run(["git", "-C", repo_dir, "reset", "--hard", "HEAD"], capture_output=True)
                subprocess.run(["git", "-C", repo_dir, "clean", "-fd"], capture_output=True)

            skipped = patches[len(applied) + 1:] if failed_patch else []
            return {
                "success": failed_patch is None,
                "total": len(patches),
                "applied": applied,
                "failed_patch": failed_patch,
                "error_message": error_message,
                "skipped": skipped,
                "source_type": "obs_scm",
                "source_target": os.path.basename(repo_dir)
            }

    # 2. Check for Tarball source archive
    archives = sorted(glob.glob(os.path.join(pkg_dir, "*.tar.*")), key=os.path.getmtime, reverse=True)
    if not archives:
        # Check .obscpio / .zip
        archives = sorted(glob.glob(os.path.join(pkg_dir, "*.obscpio")) + glob.glob(os.path.join(pkg_dir, "*.zip")), key=os.path.getmtime, reverse=True)

    if not archives:
        return {
            "success": False,
            "total": len(patches),
            "applied": [],
            "failed_patch": patches[0] if patches else None,
            "error_message": "No source archive or upstream git clone found to test patches against.",
            "skipped": patches[1:] if len(patches) > 1 else [],
            "source_type": "none"
        }

    archive_path = archives[0]
    is_lfs, oid, size = parse_lfs_pointer(archive_path)
    if is_lfs:
        loc = find_local_lfs_object(pkg_dir, oid)
        if loc:
            archive_path = loc

    # Parse target files referenced by patches (both stripped and unstripped paths)
    all_target_files = set()
    for p in patches:
        p_path = os.path.join(pkg_dir, p)
        if os.path.isfile(p_path):
            try:
                with open(p_path, 'r', encoding='utf-8', errors='replace') as pf:
                    for line in pf:
                        m = re.match(r"^(?:---|\+\+\+)\s+(\S+)", line)
                        if m:
                            raw = m.group(1).strip()
                            if raw != "/dev/null":
                                if '/' in raw:
                                    all_target_files.add(raw.split('/', 1)[1])
                                all_target_files.add(raw)
            except Exception:
                pass

    applied = []
    failed_patch = None
    error_message = None

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                with tarfile.open(archive_path, mode="r:*") as tf:
                    for member in tf.getmembers():
                        parts = member.name.split("/", 1)
                        rel_path = parts[1] if len(parts) > 1 else parts[0]
                        if rel_path in all_target_files or member.name in all_target_files:
                            dest = os.path.join(tmpdir, rel_path)
                            os.makedirs(os.path.dirname(dest), exist_ok=True)
                            with open(dest, "wb") as out_f:
                                src_f = tf.extractfile(member)
                                if src_f:
                                    out_f.write(src_f.read())
            except Exception as e:
                return {
                    "success": False,
                    "total": len(patches),
                    "applied": [],
                    "failed_patch": patches[0],
                    "error_message": f"Failed to extract target files from {os.path.basename(archive_path)}: {e}",
                    "skipped": patches[1:],
                    "source_type": "tarball",
                    "source_target": os.path.basename(archive_path)
                }

            # Initialize a temporary git repository so git apply handles hunks accurately
            subprocess.run(["git", "-C", tmpdir, "init", "-q"], check=True)
            subprocess.run(["git", "-C", tmpdir, "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", tmpdir, "config", "user.email", "test@test"], check=True)
            subprocess.run(["git", "-C", tmpdir, "add", "."], check=True)
            subprocess.run(["git", "-C", tmpdir, "commit", "-q", "-m", "upstream"], check=True)

            for idx, p in enumerate(patches, 1):
                p_path = os.path.join(pkg_dir, p)
                if not os.path.isfile(p_path):
                    failed_patch = p
                    error_message = f"Patch file '{p}' does not exist on disk"
                    log(f"[{idx}/{len(patches)}] ❌ {p}: File missing on disk!")
                    break

                applied_cleanly = False
                err_out = ""
                for p_num in ["-p1", "-p0"]:
                    res = subprocess.run(
                        ["git", "-C", tmpdir, "apply", p_num, p_path],
                        capture_output=True,
                        text=True
                    )
                    if res.returncode == 0:
                        applied_cleanly = True
                        break
                    else:
                        err_out = res.stderr.strip()

                if applied_cleanly:
                    # Commit applied changes to the temporary tree so subsequent patches chain on top
                    subprocess.run(["git", "-C", tmpdir, "add", "."], check=True)
                    subprocess.run(["git", "-C", tmpdir, "commit", "-q", "-m", f"apply {p}"], check=True)
                    applied.append(p)
                    log(f"[{idx}/{len(patches)}] ✅ {p}: Applied cleanly")
                else:
                    failed_patch = p
                    error_message = err_out or "patch apply failed"
                    log(f"[{idx}/{len(patches)}] ❌ {p}: Failed to apply!")
                    if err_out:
                        for l in err_out.splitlines()[:4]:
                            log(f"    {l}")
                    break
    except Exception as e:
        return {
            "success": False,
            "total": len(patches),
            "applied": applied,
            "failed_patch": failed_patch or (patches[len(applied)] if len(applied) < len(patches) else None),
            "error_message": f"Unexpected error during patch verification: {e}",
            "skipped": patches[len(applied)+1:] if len(applied) < len(patches) else [],
            "source_type": "tarball",
            "source_target": os.path.basename(archive_path)
        }

    skipped = patches[len(applied) + 1:] if failed_patch else []
    return {
        "success": failed_patch is None,
        "total": len(patches),
        "applied": applied,
        "failed_patch": failed_patch,
        "error_message": error_message,
        "skipped": skipped,
        "source_type": "tarball",
        "source_target": os.path.basename(archive_path)
    }

def run_check_patches_cli(package_dir: str) -> bool:
    """Executes the sequential patch check for a package and renders formatted CLI output."""
    pkg_dir = os.path.abspath(package_dir)
    pkg_name = os.path.basename(pkg_dir)

    print(f"\n{BOLD}{'='*80}{RESET}")
    print(f"🔍 {BOLD}SEQUENTIAL PATCH PRE-FLIGHT CHECK: {CYAN}{pkg_name}{RESET}")
    print(f"{BOLD}{'='*80}{RESET}")

    patches = get_spec_patches(pkg_dir)
    if not patches:
        print(f"  ℹ️  No downstream patches declared in {pkg_name}.spec.\n")
        return True

    print(f"  Patches in Spec: {len(patches)} patch(es) declared")

    res = check_patches_sequential(pkg_dir, on_log=lambda msg: print(f"  {msg}"))

    print(f"{BOLD}{'='*80}{RESET}")
    if res["success"]:
        print(f"{GREEN}🎉 All {res['total']} downstream patch(es) applied cleanly in sequence!{RESET}\n")
        return True
    else:
        print(f"{RED}⚠️  Sequential verification HALTED at '{res['failed_patch']}'!{RESET}")
        if res["applied"]:
            print(f"   • Successfully applied: {', '.join(res['applied'])}")
        if res["skipped"]:
            print(f"   • Skipped pending: {', '.join(res['skipped'])}")
        print(f"\n{CYAN}ℹ️  To re-verify after rebasing, simply re-run:{RESET}")
        print(f"     {BOLD}geckopit-cli --check-patches{RESET}\n")
        return False
