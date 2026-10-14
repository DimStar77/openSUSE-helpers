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

def expand_macros(text: str, macros: Dict[str, str]) -> str:
    """Recursively expands RPM macros in text using provided macro dictionary."""
    res = text
    for _ in range(5):
        changed = False
        for k, v in macros.items():
            pattern = re.compile(r'%\{?' + re.escape(k) + r'\}?', re.IGNORECASE)
            new_res = pattern.sub(v, res)
            if new_res != res:
                res = new_res
                changed = True
        if not changed:
            break
    return res

def get_patch_target_files(patch_path: str) -> List[str]:
    """Extracts target relative file paths from diff headers in a patch file."""
    targets: List[str] = []
    if not os.path.isfile(patch_path):
        return targets
    try:
        with open(patch_path, "r", encoding="utf-8", errors="replace") as pf:
            for line in pf:
                m = re.match(r"^(?:---|\+\+\+)\s+(\S+)", line)
                if m:
                    raw = m.group(1).strip()
                    if raw != "/dev/null":
                        rel = raw.split("/", 1)[1] if "/" in raw else raw
                        if rel not in targets:
                            targets.append(rel)
    except Exception:
        pass
    return targets

def get_spec_details(package_dir: str) -> Dict[str, Any]:
    """
    Parses package .spec file to extract macros, Name, Version, Sources, and Patches.
    Preserves declaration order, expands macros, and identifies SLE/conditional patch status.
    """
    spec_files = glob.glob(os.path.join(package_dir, "*.spec"))
    if not spec_files:
        return {"name": "", "version": "", "macros": {}, "sources": {}, "patches": []}

    macros: Dict[str, str] = {}
    sources: Dict[int, str] = {}
    patches: List[Dict[str, Any]] = []

    re_macro = re.compile(r'^\s*%(?:define|global)\s+(\w+)\s+(.+)$', re.IGNORECASE)
    re_tag = re.compile(r'^\s*(Name|Version)\s*:\s*(.+)$', re.IGNORECASE)
    re_source = re.compile(r'^\s*Source(\d+)?\s*:\s*(\S+)', re.IGNORECASE)
    re_patch = re.compile(r'^\s*Patch(\d+)?\s*:\s*(\S+)', re.IGNORECASE)

    in_sle_block = False
    sle_block_depth = 0
    current_depth = 0
    prev_comment = ""

    try:
        with open(spec_files[0], 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith('#'):
                    prev_comment += " " + stripped
                    continue

                if re.match(r'^\s*%if', line, re.IGNORECASE):
                    current_depth += 1
                    if re.search(r'%if\s+(?:!0%\{\?is_opensuse\}|0%\{\?sle_version\})', line, re.IGNORECASE):
                        in_sle_block = True
                        sle_block_depth = current_depth
                elif re.match(r'^\s*%endif', line, re.IGNORECASE):
                    if in_sle_block and current_depth == sle_block_depth:
                        in_sle_block = False
                    current_depth = max(0, current_depth - 1)

                m_mac = re_macro.match(line)
                if m_mac:
                    macros[m_mac.group(1)] = m_mac.group(2).strip()
                    prev_comment = ""
                    continue
                m_tag = re_tag.match(line)
                if m_tag:
                    macros[m_tag.group(1).lower()] = m_tag.group(2).strip()
                    prev_comment = ""
                    continue
                m_src = re_source.match(line)
                if m_src:
                    num_str = m_src.group(1)
                    s_num = int(num_str) if num_str is not None else 0
                    sources[s_num] = m_src.group(2).strip()
                    prev_comment = ""
                    continue
                m_p = re_patch.match(line)
                if m_p:
                    num_str = m_p.group(1)
                    p_num = int(num_str) if num_str is not None else 0
                    raw_name = m_p.group(2).strip()
                    expanded = expand_macros(raw_name, macros)
                    is_sle = (
                        p_num >= 1000 or
                        in_sle_block or
                        bool(re.search(r'PATCH-(?:FIX|FEATURE)-SLE', prev_comment, re.IGNORECASE))
                    )
                    patches.append({
                        "name": os.path.basename(expanded),
                        "num": p_num,
                        "raw": raw_name,
                        "is_sle": is_sle
                    })
                    prev_comment = ""
    except Exception:
        pass

    return {
        "name": macros.get("name", ""),
        "version": macros.get("version", ""),
        "macros": macros,
        "sources": sources,
        "patches": patches
    }

def get_spec_patches(package_dir: str) -> List[str]:
    """
    Parses all active downstream patches declared in the package's .spec file,
    preserving declaration order and expanding standard %define / %global / Name macros.
    """
    details = get_spec_details(package_dir)
    return [p["name"] for p in details.get("patches", [])]

def find_primary_source_archive(package_dir: str, spec_details: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """
    Identifies the primary upstream source archive for the package.
    Prioritizes explicit Source0 / Source declarations from .spec, then Name-Version matching,
    preventing secondary archives (gitmodules, vendor tarballs) with newer mtimes from being misidentified.
    """
    if spec_details is None:
        spec_details = get_spec_details(package_dir)

    pkg_name = spec_details.get("name", "")
    pkg_ver = spec_details.get("version", "")
    sources = spec_details.get("sources", {})
    macros = spec_details.get("macros", {})

    archives = glob.glob(os.path.join(package_dir, "*.tar.*")) +                glob.glob(os.path.join(package_dir, "*.obscpio")) +                glob.glob(os.path.join(package_dir, "*.zip"))

    if not archives:
        return None

    archive_basenames = {os.path.basename(a): a for a in archives}

    # 1. Match Source0 / Source
    src0 = sources.get(0)
    if src0:
        expanded_src0 = expand_macros(src0, macros)
        base_src0 = os.path.basename(expanded_src0.split("?")[0])
        if base_src0 in archive_basenames:
            return archive_basenames[base_src0]

        # Match stem if extension changed (e.g. .tar.gz -> .tar.xz)
        stem = re.sub(r'\.(?:tar(?:\.[a-z0-9]+)?|obscpio|zip)$', '', base_src0, flags=re.IGNORECASE)
        for bname, apath in archive_basenames.items():
            if bname.startswith(stem + "."):
                return apath

    # 2. Match Name-Version prefix
    if pkg_name and pkg_ver:
        nv_prefix = f"{pkg_name}-{pkg_ver}."
        for bname, apath in archive_basenames.items():
            if bname.startswith(nv_prefix):
                return apath
        nv = f"{pkg_name}-{pkg_ver}"
        for bname, apath in archive_basenames.items():
            if nv in bname:
                return apath

    # 3. Match Name- prefix
    if pkg_name:
        n_prefix = f"{pkg_name}-"
        for bname, apath in archive_basenames.items():
            if bname.startswith(n_prefix):
                return apath

    # 4. Fallback: newest mtime
    return sorted(archives, key=os.path.getmtime, reverse=True)[0]

def check_patches_sequential(
    package_dir: str,
    on_log: Optional[Callable[[str], None]] = None
) -> Dict[str, Any]:
    """
    Sequentially tests downstream patches against the upstream source tree.
    Validates openSUSE patches in the primary sequential log, while testing SLE-only
    patches quietly and summarizing them without noise.
    Returns a dictionary summarizing applied, failed, and skipped patches.
    """
    log = on_log or (lambda msg: None)
    pkg_dir = os.path.abspath(package_dir)
    pkg_name = os.path.basename(pkg_dir)

    spec_details = get_spec_details(pkg_dir)
    patch_infos = spec_details.get("patches", [])
    if not patch_infos:
        return {
            "success": True,
            "total": 0,
            "applied": [],
            "failed_patch": None,
            "failure_reason": None,
            "sle_status": {"total": 0, "clean": 0, "failed": 0, "failed_patches": []},
            "sle_issues": [],
            "error_message": None,
            "skipped": [],
            "source_type": "none"
        }

    opensuse_patches = [p for p in patch_infos if not p.get("is_sle")]
    sle_patches = [p for p in patch_infos if p.get("is_sle")]

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
            for item in os.listdir(pkg_dir):
                sub = os.path.join(pkg_dir, item)
                if os.path.isdir(os.path.join(sub, ".git")) and item != ".git":
                    repo_dir = sub
                    break

        if os.path.isdir(os.path.join(repo_dir, ".git")):
            applied = []
            failed_patch = None
            failure_reason = None
            error_message = None

            try:
                subprocess.run(["git", "-C", repo_dir, "reset", "--hard", "HEAD"], capture_output=True)
                subprocess.run(["git", "-C", repo_dir, "clean", "-fd"], capture_output=True)

                for idx, p_info in enumerate(opensuse_patches, 1):
                    p = p_info["name"]
                    p_path = os.path.join(pkg_dir, p)

                    if not os.path.isfile(p_path):
                        failed_patch = p
                        failure_reason = "missing_file"
                        error_message = f"Patch file '{p}' does not exist on disk"
                        log(f"[{idx}/{len(opensuse_patches)}] ❌ {p}: File missing on disk!")
                        break

                    applied_cleanly = False
                    err_out = ""
                    res_p1 = subprocess.run(["git", "-C", repo_dir, "apply", "-p1", p_path], capture_output=True, text=True)
                    if res_p1.returncode == 0:
                        applied_cleanly = True
                    else:
                        res_p0 = subprocess.run(["git", "-C", repo_dir, "apply", "-p0", p_path], capture_output=True, text=True)
                        if res_p0.returncode == 0:
                            applied_cleanly = True
                        else:
                            err_out = res_p1.stderr.strip() if ("patch failed" in res_p1.stderr or "patch does not apply" in res_p1.stderr) else (res_p0.stderr.strip() or res_p1.stderr.strip())

                    if applied_cleanly:
                        applied.append(p)
                        log(f"[{idx}/{len(opensuse_patches)}] ✅ {p}: Applied cleanly")
                    else:
                        p_targets = get_patch_target_files(p_path)
                        missing_targets = [tf for tf in p_targets if not os.path.exists(os.path.join(repo_dir, tf))]
                        reason = "missing_file" if missing_targets else "conflict"
                        failed_patch = p
                        failure_reason = reason
                        error_message = err_out or "git apply returned non-zero"
                        log(f"[{idx}/{len(opensuse_patches)}] ❌ {p}: Failed to apply!")
                        if err_out:
                            for l in err_out.splitlines()[:4]:
                                log(f"    {l}")
                        break

                sle_clean = []
                sle_failed = []
                if failed_patch is None and sle_patches:
                    for p_info in sle_patches:
                        p = p_info["name"]
                        p_path = os.path.join(pkg_dir, p)
                        if not os.path.isfile(p_path):
                            sle_failed.append(p)
                            continue
                        r_p1 = subprocess.run(["git", "-C", repo_dir, "apply", "-p1", p_path], capture_output=True, text=True)
                        if r_p1.returncode == 0:
                            sle_clean.append(p)
                        else:
                            r_p0 = subprocess.run(["git", "-C", repo_dir, "apply", "-p0", p_path], capture_output=True, text=True)
                            if r_p0.returncode == 0:
                                sle_clean.append(p)
                            else:
                                sle_failed.append(p)
            finally:
                subprocess.run(["git", "-C", repo_dir, "reset", "--hard", "HEAD"], capture_output=True)
                subprocess.run(["git", "-C", repo_dir, "clean", "-fd"], capture_output=True)

            skipped = [pi["name"] for pi in opensuse_patches[idx:]] if failed_patch else []
            sle_stat = {
                "total": len(sle_patches),
                "clean": len(sle_clean),
                "failed": len(sle_failed),
                "failed_patches": sle_failed
            }
            return {
                "success": failed_patch is None,
                "total": len(opensuse_patches),
                "applied": applied,
                "failed_patch": failed_patch,
                "failure_reason": failure_reason,
                "sle_status": sle_stat,
                "sle_issues": [{"patch": p, "reason": "conflict"} for p in sle_failed],
                "error_message": error_message,
                "skipped": skipped,
                "source_type": "obs_scm",
                "source_target": os.path.basename(repo_dir)
            }

    # 2. Check for Tarball source archive
    archive_path = find_primary_source_archive(pkg_dir, spec_details)

    if not archive_path:
        first_patch = opensuse_patches[0]["name"] if opensuse_patches else (patch_infos[0]["name"] if patch_infos else None)
        return {
            "success": False,
            "total": len(opensuse_patches),
            "applied": [],
            "failed_patch": first_patch,
            "failure_reason": "missing_file",
            "sle_status": {"total": len(sle_patches), "clean": 0, "failed": len(sle_patches), "failed_patches": [p["name"] for p in sle_patches]},
            "sle_issues": [],
            "error_message": "No source archive or upstream git clone found to test patches against.",
            "skipped": [pi["name"] for pi in opensuse_patches[1:]] if len(opensuse_patches) > 1 else [],
            "source_type": "none"
        }
    is_lfs, oid, size = parse_lfs_pointer(archive_path)
    if is_lfs:
        loc = find_local_lfs_object(pkg_dir, oid)
        if loc:
            archive_path = loc

    # Parse target files referenced by patches (both stripped and unstripped paths)
    all_target_files = set()
    for p_info in patch_infos:
        p_path = os.path.join(pkg_dir, p_info["name"])
        for tf in get_patch_target_files(p_path):
            all_target_files.add(tf)

    applied = []
    failed_patch = None
    failure_reason = None
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
                first_patch = opensuse_patches[0]["name"] if opensuse_patches else (patch_infos[0]["name"] if patch_infos else None)
                return {
                    "success": False,
                    "total": len(opensuse_patches),
                    "applied": [],
                    "failed_patch": first_patch,
                    "failure_reason": "missing_file",
                    "sle_status": {"total": len(sle_patches), "clean": 0, "failed": len(sle_patches), "failed_patches": [p["name"] for p in sle_patches]},
                    "sle_issues": [],
                    "error_message": f"Failed to extract target files from {os.path.basename(archive_path)}: {e}",
                    "skipped": [pi["name"] for pi in opensuse_patches[1:]] if len(opensuse_patches) > 1 else [],
                    "source_type": "tarball",
                    "source_target": os.path.basename(archive_path)
                }

            # Secondary archives: locate any target files not present in the primary archive
            # (e.g. git submodules or vendor tarballs placed into subprojects/ during %prep)
            missing_targets = [
                t for t in all_target_files
                if not os.path.exists(os.path.join(tmpdir, t))
            ]

            if missing_targets:
                all_archives = glob.glob(os.path.join(pkg_dir, "*.tar.*")) +                                glob.glob(os.path.join(pkg_dir, "*.obscpio")) +                                glob.glob(os.path.join(pkg_dir, "*.zip"))
                secondary_archives = [
                    a for a in all_archives
                    if os.path.abspath(a) != os.path.abspath(archive_path)
                ]

                for sec_arch in secondary_archives:
                    if not missing_targets:
                        break
                    sec_path = sec_arch
                    sec_lfs, s_oid, s_size = parse_lfs_pointer(sec_path)
                    if sec_lfs:
                        s_loc = find_local_lfs_object(pkg_dir, s_oid)
                        if s_loc:
                            sec_path = s_loc

                    try:
                        if tarfile.is_tarfile(sec_path):
                            with tarfile.open(sec_path, mode="r:*") as stf:
                                for member in stf.getmembers():
                                    if not member.isfile():
                                        continue
                                    m_parts = member.name.split("/", 1)
                                    m_rel = m_parts[1] if len(m_parts) > 1 else m_parts[0]
                                    m_base = os.path.basename(member.name)

                                    for target in list(missing_targets):
                                        t_base = os.path.basename(target)
                                        if m_rel == target or target.endswith(m_rel) or (m_base == t_base and len(m_base) > 3):
                                            dest = os.path.join(tmpdir, target)
                                            if not os.path.exists(dest):
                                                os.makedirs(os.path.dirname(dest), exist_ok=True)
                                                with open(dest, "wb") as out_f:
                                                    src_f = stf.extractfile(member)
                                                    if src_f:
                                                        out_f.write(src_f.read())
                                                if target in missing_targets:
                                                    missing_targets.remove(target)
                    except Exception:
                        pass

            # Initialize a temporary git repository so git apply handles hunks accurately
            subprocess.run(["git", "-C", tmpdir, "init", "-q"], check=True)
            subprocess.run(["git", "-C", tmpdir, "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", tmpdir, "config", "user.email", "test@test"], check=True)
            subprocess.run(["git", "-C", tmpdir, "add", "."], check=True)
            subprocess.run(["git", "-C", tmpdir, "commit", "-q", "-m", "upstream", "--allow-empty"], check=True)

            for idx, p_info in enumerate(opensuse_patches, 1):
                p = p_info["name"]
                p_path = os.path.join(pkg_dir, p)

                if not os.path.isfile(p_path):
                    failed_patch = p
                    failure_reason = "missing_file"
                    error_message = f"Patch file '{p}' does not exist on disk"
                    log(f"[{idx}/{len(opensuse_patches)}] ❌ {p}: File missing on disk!")
                    break

                applied_cleanly = False
                err_out = ""
                res_p1 = subprocess.run(["git", "-C", tmpdir, "apply", "-p1", p_path], capture_output=True, text=True)
                if res_p1.returncode == 0:
                    applied_cleanly = True
                else:
                    res_p0 = subprocess.run(["git", "-C", tmpdir, "apply", "-p0", p_path], capture_output=True, text=True)
                    if res_p0.returncode == 0:
                        applied_cleanly = True
                    else:
                        err_out = res_p1.stderr.strip() if ("patch failed" in res_p1.stderr or "patch does not apply" in res_p1.stderr) else (res_p0.stderr.strip() or res_p1.stderr.strip())

                if applied_cleanly:
                    subprocess.run(["git", "-C", tmpdir, "add", "."], check=True)
                    subprocess.run(["git", "-C", tmpdir, "commit", "-q", "-m", f"apply {p}", "--allow-empty"], check=True)
                    applied.append(p)
                    log(f"[{idx}/{len(opensuse_patches)}] ✅ {p}: Applied cleanly")
                else:
                    p_targets = get_patch_target_files(p_path)
                    missing_targets = [
                        tf for tf in p_targets
                        if not os.path.exists(os.path.join(tmpdir, tf)) and
                           not os.path.exists(os.path.join(tmpdir, tf.split('/', 1)[-1]))
                    ]
                    reason = "missing_file" if missing_targets else "conflict"
                    failed_patch = p
                    failure_reason = reason
                    error_message = err_out or "patch apply failed"
                    log(f"[{idx}/{len(opensuse_patches)}] ❌ {p}: Failed to apply!")
                    if err_out:
                        for l in err_out.splitlines()[:4]:
                            log(f"    {l}")
                    break

            # Quietly evaluate SLE-only patches if openSUSE patches succeeded
            sle_clean = []
            sle_failed = []
            if failed_patch is None and sle_patches:
                for p_info in sle_patches:
                    p = p_info["name"]
                    p_path = os.path.join(pkg_dir, p)
                    if not os.path.isfile(p_path):
                        sle_failed.append(p)
                        continue

                    applied_cleanly = False
                    r_p1 = subprocess.run(["git", "-C", tmpdir, "apply", "-p1", p_path], capture_output=True, text=True)
                    if r_p1.returncode == 0:
                        applied_cleanly = True
                    else:
                        r_p0 = subprocess.run(["git", "-C", tmpdir, "apply", "-p0", p_path], capture_output=True, text=True)
                        if r_p0.returncode == 0:
                            applied_cleanly = True

                    if applied_cleanly:
                        subprocess.run(["git", "-C", tmpdir, "add", "."], check=True)
                        subprocess.run(["git", "-C", tmpdir, "commit", "-q", "-m", f"apply {p}", "--allow-empty"], check=True)
                        sle_clean.append(p)
                    else:
                        sle_failed.append(p)

    except Exception as e:
        first_patch = opensuse_patches[0]["name"] if opensuse_patches else (patch_infos[0]["name"] if patch_infos else None)
        return {
            "success": False,
            "total": len(opensuse_patches),
            "applied": applied,
            "failed_patch": failed_patch or first_patch,
            "failure_reason": failure_reason or "conflict",
            "sle_status": {"total": len(sle_patches), "clean": 0, "failed": len(sle_patches), "failed_patches": [p["name"] for p in sle_patches]},
            "sle_issues": [{"patch": p["name"], "reason": "conflict"} for p in sle_patches],
            "error_message": f"Unexpected error during patch verification: {e}",
            "skipped": [pi["name"] for pi in opensuse_patches[idx:]] if failed_patch else [],
            "source_type": "tarball",
            "source_target": os.path.basename(archive_path)
        }

    skipped = [pi["name"] for pi in opensuse_patches[idx:]] if failed_patch else []
    sle_stat = {
        "total": len(sle_patches),
        "clean": len(sle_clean),
        "failed": len(sle_failed),
        "failed_patches": sle_failed
    }
    return {
        "success": failed_patch is None,
        "total": len(opensuse_patches),
        "applied": applied,
        "failed_patch": failed_patch,
        "failure_reason": failure_reason,
        "sle_status": sle_stat,
        "sle_issues": [{"patch": p, "reason": "conflict"} for p in sle_failed],
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

    spec_details = get_spec_details(pkg_dir)
    patch_infos = spec_details.get("patches", [])
    if not patch_infos:
        print(f"  ℹ️  No downstream patches declared in {pkg_name}.spec.\n")
        return True

    sle_count = sum(1 for p in patch_infos if p.get("is_sle", False))
    common_count = len(patch_infos) - sle_count
    if sle_count > 0:
        print(f"  Patches: {common_count} openSUSE declared ({sle_count} SLE-only)")
    else:
        print(f"  Patches: {common_count} openSUSE declared")

    res = check_patches_sequential(pkg_dir, on_log=lambda msg: print(f"  {msg}"))

    print(f"{BOLD}{'='*80}{RESET}")
    if res["success"]:
        print(f"{GREEN}🎉 All {res['total']} openSUSE downstream patch(es) applied cleanly in sequence!{RESET}")
        sle_stat = res.get("sle_status", {})
        if sle_stat.get("total", 0) > 0:
            if sle_stat["failed"] > 0:
                print(f"{CYAN}ℹ️  SLE status: {sle_stat['clean']}/{sle_stat['total']} apply cleanly ({sle_stat['failed']} conflict against new upstream).{RESET}")
            else:
                print(f"{CYAN}ℹ️  SLE status: All {sle_stat['total']} SLE-only patch(es) apply cleanly.{RESET}")
        print()
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
