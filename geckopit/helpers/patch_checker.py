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
import shlex
import shutil
from typing import Optional, List, Dict, Any, Callable

# Ensure terminal colors if available
BOLD = "\033[1m"
DIM = "\033[2m"
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
    Preserves declaration order, expands macros, and identifies SLE-only conditional patch status.
    Unconditionally applied patches (even if tagged # PATCH-*-SLE) remain part of the normal openSUSE run.
    """
    spec_files = glob.glob(os.path.join(package_dir, "*.spec"))
    if not spec_files:
        return {"name": "", "version": "", "macros": {}, "sources": {}, "patches": []}

    macros: Dict[str, str] = {}
    sources: Dict[int, str] = {}
    raw_patches: List[Dict[str, Any]] = []
    sle_patch_nums = set()
    sle_autopatch_min: Optional[int] = None

    re_macro = re.compile(r'^\s*%(?:define|global)\s+(\w+)\s+(.+)$', re.IGNORECASE)
    re_tag = re.compile(r'^\s*(Name|Version)\s*:\s*(.+)$', re.IGNORECASE)
    re_source = re.compile(r'^\s*Source(\d+)?\s*:\s*(\S+)', re.IGNORECASE)
    re_patch = re.compile(r'^\s*Patch(\d+)?\s*:\s*(\S+)', re.IGNORECASE)
    re_section = re.compile(r'^\s*%(prep|build|install|check|clean|files|changelog|package|description)\b', re.IGNORECASE)

    current_depth = 0
    in_sle_block = False
    sle_block_depth = 0
    in_prep = False

    try:
        with open(spec_files[0], 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith('#'):
                    continue

                m_sec = re_section.match(line)
                if m_sec:
                    sec_name = m_sec.group(1).lower()
                    in_prep = (sec_name == "prep")

                if re.match(r'^\s*%if\b', line, re.IGNORECASE):
                    current_depth += 1
                    if re.search(r'%if\s+(?:!0%\{\?is_opensuse\}|0%\{\?sle_version\}|0%\{\?suse_version\}\s*<\s*1699)', line, re.IGNORECASE):
                        in_sle_block = True
                        sle_block_depth = current_depth
                elif re.match(r'^\s*%endif\b', line, re.IGNORECASE):
                    if in_sle_block and current_depth == sle_block_depth:
                        in_sle_block = False
                    current_depth = max(0, current_depth - 1)

                if in_prep and in_sle_block:
                    # Detect %patch -P <num> or %patch<num> applied under SLE conditions
                    m_papply = re.search(r'%patch(?:(?:\s+-(?:P\s+|[a-zA-Z0-9]*P))(\d+)|\s+(\d+)|(\d+))', line)
                    if m_papply:
                        pnum = m_papply.group(1) or m_papply.group(2) or m_papply.group(3)
                        if pnum:
                            sle_patch_nums.add(int(pnum))
                    # Detect %autopatch -m <num> under SLE conditions
                    m_auto = re.search(r'%autopatch\b.*?-m\s+(\d+)', line)
                    if m_auto:
                        sle_autopatch_min = int(m_auto.group(1))

                m_mac = re_macro.match(line)
                if m_mac:
                    macros[m_mac.group(1)] = m_mac.group(2).strip()
                    continue
                m_tag = re_tag.match(line)
                if m_tag:
                    macros[m_tag.group(1).lower()] = m_tag.group(2).strip()
                    continue
                m_src = re_source.match(line)
                if m_src:
                    num_str = m_src.group(1)
                    s_num = int(num_str) if num_str is not None else 0
                    sources[s_num] = m_src.group(2).strip()
                    continue
                m_p = re_patch.match(line)
                if m_p:
                    num_str = m_p.group(1)
                    p_num = int(num_str) if num_str is not None else 0
                    raw_name = m_p.group(2).strip()
                    expanded = expand_macros(raw_name, macros)
                    raw_patches.append({
                        "name": os.path.basename(expanded),
                        "num": p_num,
                        "raw": raw_name,
                        "declared_in_sle_block": in_sle_block
                    })
    except Exception:
        pass

    patches: List[Dict[str, Any]] = []
    for rp in raw_patches:
        is_sle = (
            rp["num"] >= 1000 or
            rp["declared_in_sle_block"] or
            rp["num"] in sle_patch_nums or
            (sle_autopatch_min is not None and rp["num"] >= sle_autopatch_min)
        )
        patches.append({
            "name": rp["name"],
            "num": rp["num"],
            "raw": rp["raw"],
            "is_sle": is_sle
        })

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
                subprocess.run(["git", "-C", repo_dir, "submodule", "foreach", "--recursive", "git", "reset", "--hard", "HEAD"], capture_output=True)
                subprocess.run(["git", "-C", repo_dir, "submodule", "foreach", "--recursive", "git", "clean", "-fd"], capture_output=True)

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
                        # Check if patch is already merged upstream in reverse
                        rev_check = subprocess.run(["git", "-C", repo_dir, "apply", "-R", "--check", "-p1", p_path], capture_output=True)
                        if rev_check.returncode != 0:
                            rev_check = subprocess.run(["git", "-C", repo_dir, "apply", "-R", "--check", "-p0", p_path], capture_output=True)
                        if rev_check.returncode == 0:
                            failed_patch = p
                            failure_reason = "already_merged"
                            error_message = f"Patch '{p}' is already merged upstream"
                            log(f"[{idx}/{len(opensuse_patches)}] ℹ️  {p}: Already merged upstream (can be dropped)")
                            break

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
                sle_details = []
                if failed_patch is None and sle_patches:
                    for p_info in sle_patches:
                        p = p_info["name"]
                        p_path = os.path.join(pkg_dir, p)
                        if not os.path.isfile(p_path):
                            sle_failed.append(p)
                            sle_details.append({
                                "patch": p,
                                "num": p_info.get("num"),
                                "reason": "missing_file",
                                "error": f"Patch '{p}' does not exist on disk"
                            })
                            continue

                        err_out = ""
                        applied = False
                        r_p1 = subprocess.run(["git", "-C", repo_dir, "apply", "-p1", p_path], capture_output=True, text=True)
                        if r_p1.returncode == 0:
                            applied = True
                        else:
                            r_p0 = subprocess.run(["git", "-C", repo_dir, "apply", "-p0", p_path], capture_output=True, text=True)
                            if r_p0.returncode == 0:
                                applied = True
                            else:
                                err_out = r_p1.stderr.strip() if ("patch failed" in r_p1.stderr or "patch does not apply" in r_p1.stderr) else (r_p0.stderr.strip() or r_p1.stderr.strip())

                        if applied:
                            sle_clean.append(p)
                        else:
                            p_targets = get_patch_target_files(p_path)
                            missing_targets = [tf for tf in p_targets if not os.path.exists(os.path.join(repo_dir, tf))]
                            reason = "missing_file" if missing_targets else "conflict"
                            sle_failed.append(p)
                            sle_details.append({
                                "patch": p,
                                "num": p_info.get("num"),
                                "reason": reason,
                                "error": err_out
                            })
            finally:
                subprocess.run(["git", "-C", repo_dir, "reset", "--hard", "HEAD"], capture_output=True)
                subprocess.run(["git", "-C", repo_dir, "clean", "-fd"], capture_output=True)
                subprocess.run(["git", "-C", repo_dir, "submodule", "foreach", "--recursive", "git", "reset", "--hard", "HEAD"], capture_output=True)
                subprocess.run(["git", "-C", repo_dir, "submodule", "foreach", "--recursive", "git", "clean", "-fd"], capture_output=True)

            skipped = [pi["name"] for pi in opensuse_patches[idx:]] if failed_patch else []
            sle_stat = {
                "total": len(sle_patches),
                "clean": len(sle_clean),
                "failed": len(sle_failed),
                "failed_patches": sle_failed,
                "details": sle_details
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
                    # Check if patch is already merged upstream in reverse
                    rev_check = subprocess.run(["git", "-C", tmpdir, "apply", "-R", "--check", "-p1", p_path], capture_output=True)
                    if rev_check.returncode != 0:
                        rev_check = subprocess.run(["git", "-C", tmpdir, "apply", "-R", "--check", "-p0", p_path], capture_output=True)
                    if rev_check.returncode == 0:
                        failed_patch = p
                        failure_reason = "already_merged"
                        error_message = f"Patch '{p}' is already merged upstream"
                        log(f"[{idx}/{len(opensuse_patches)}] ℹ️  {p}: Already merged upstream (can be dropped)")
                        break

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
            sle_details = []
            if failed_patch is None and sle_patches:
                for p_info in sle_patches:
                    p = p_info["name"]
                    p_path = os.path.join(pkg_dir, p)
                    if not os.path.isfile(p_path):
                        sle_failed.append(p)
                        sle_details.append({
                            "patch": p,
                            "num": p_info.get("num"),
                            "reason": "missing_file",
                            "error": f"Patch '{p}' does not exist on disk"
                        })
                        continue

                    applied_cleanly = False
                    err_out = ""
                    r_p1 = subprocess.run(["git", "-C", tmpdir, "apply", "-p1", p_path], capture_output=True, text=True)
                    if r_p1.returncode == 0:
                        applied_cleanly = True
                    else:
                        r_p0 = subprocess.run(["git", "-C", tmpdir, "apply", "-p0", p_path], capture_output=True, text=True)
                        if r_p0.returncode == 0:
                            applied_cleanly = True
                        else:
                            err_out = r_p1.stderr.strip() if ("patch failed" in r_p1.stderr or "patch does not apply" in r_p1.stderr) else (r_p0.stderr.strip() or r_p1.stderr.strip())

                    if applied_cleanly:
                        subprocess.run(["git", "-C", tmpdir, "add", "."], check=True)
                        subprocess.run(["git", "-C", tmpdir, "commit", "-q", "-m", f"apply {p}", "--allow-empty"], check=True)
                        sle_clean.append(p)
                    else:
                        p_targets = get_patch_target_files(p_path)
                        missing_targets = [
                            tf for tf in p_targets
                            if not os.path.exists(os.path.join(tmpdir, tf)) and
                               not os.path.exists(os.path.join(tmpdir, tf.split('/', 1)[-1]))
                        ]
                        reason = "missing_file" if missing_targets else "conflict"
                        sle_failed.append(p)
                        sle_details.append({
                            "patch": p,
                            "num": p_info.get("num"),
                            "reason": reason,
                            "error": err_out
                        })

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
        "failed_patches": sle_failed,
        "details": sle_details
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

def run_check_patches_cli(package_dir: str, verbose: bool = False) -> bool:
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
                if verbose and sle_stat.get("details"):
                    print(f"\n{BOLD}Detailed SLE Patch Breakdown:{RESET}")
                    for item in sle_stat["details"]:
                        reason_str = "missing target file" if item["reason"] == "missing_file" else "conflict"
                        print(f"  • {YELLOW}{item['patch']}{RESET} ({reason_str})")
                        if item.get("error"):
                            for l in item["error"].splitlines()[:2]:
                                print(f"      {DIM}{l}{RESET}")
            else:
                print(f"{CYAN}ℹ️  SLE status: All {sle_stat['total']} SLE-only patch(es) apply cleanly.{RESET}")

        if sle_stat.get("failed", 0) > 0:
            print(f"💡 {CYAN}To rebase failing SLE patch(es):{RESET}")
            print(f"     {BOLD}geckopit-cli --rebase-patch --sle{RESET}")
        print()
        return True
    else:
        if res.get("failure_reason") == "already_merged":
            print(f"{YELLOW}ℹ️  Sequential verification HALTED at '{res['failed_patch']}' (Already Merged Upstream)!{RESET}")
            print(f"💡 {GREEN}The changes from '{res['failed_patch']}' are already present in the upstream release.{RESET}")
            print(f"   You can safely drop this patch from {pkg_name}.spec and delete the file.\n")
        else:
            print(f"{RED}⚠️  Sequential verification HALTED at '{res['failed_patch']}'!{RESET}")
        if res["applied"]:
            print(f"   • Successfully applied: {', '.join(res['applied'])}")
        if res["skipped"]:
            print(f"   • Skipped pending: {', '.join(res['skipped'])}")
        print(f"\n{CYAN}ℹ️  To re-verify after rebasing, simply re-run:{RESET}")
        print(f"     {BOLD}geckopit-cli --check-patches{RESET}\n")
        print(f"💡 {CYAN}Or rebase interactively without quilt:{RESET}")
        print(f"     {BOLD}geckopit-cli --rebase-patch {res['failed_patch']}{RESET}\n")
        return False

def record_patch_rebase_changelog(package_dir: str, patch_name: str) -> bool:
    """
    Records a '- Rebase {patch_name}.' entry in the package's .changes file.
    If the .changes file currently has an uncommitted entry from an in-flight upgrade,
    the bullet is cleanly appended to the active top entry. Otherwise, runs 'osc vc -m'.
    """
    changes_files = glob.glob(os.path.join(package_dir, "*.changes"))
    if not changes_files:
        return False
    changes_path = os.path.abspath(changes_files[0])
    bullet = f"- Rebase {patch_name}."

    try:
        with open(changes_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        # Prevent duplicate entries if user re-runs rebase
        if bullet in content or f"- Rebase {patch_name}:" in content or f"- Rebased {patch_name}" in content:
            return True

        # Check if .changes is currently modified in git (e.g. from an in-flight --upgrade)
        is_dirty = False
        try:
            res = subprocess.run(
                ["git", "-C", package_dir, "status", "--porcelain", os.path.basename(changes_path)],
                capture_output=True, text=True
            )
            if res.stdout.strip():
                is_dirty = True
        except Exception:
            pass

        if is_dirty:
            # Append to current top entry
            parts = content.split("-------------------------------------------------------------------\n")
            for i in range(len(parts)):
                if parts[i].strip():
                    entry = parts[i].rstrip()
                    parts[i] = entry + "\n" + bullet + "\n\n"
                    break
            new_content = "-------------------------------------------------------------------\n".join(parts)
            with open(changes_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            print(f"📝 Appended '{bullet}' to active changelog entry in {os.path.basename(changes_path)}")
            return True

        # Standalone rebase: try osc vc first
        try:
            r = subprocess.run(
                ["osc", "vc", "-m", f"Rebase {patch_name}."],
                cwd=package_dir, capture_output=True, text=True
            )
            if r.returncode == 0:
                print(f"📝 Recorded changelog entry via 'osc vc': {bullet}")
                return True
        except Exception:
            pass

        # Fallback to direct prepending
        import datetime
        now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%a %b %d %H:%M:%S UTC %Y")
        user_name = os.environ.get("USER", "maintainer")
        user_email = os.environ.get("MAIL", f"{user_name}@opensuse.org")
        new_entry = f"-------------------------------------------------------------------\n{now_str} - {user_email}\n\n{bullet}\n\n"
        with open(changes_path, "w", encoding="utf-8") as f:
            f.write(new_entry + content)
        print(f"📝 Recorded changelog entry in {os.path.basename(changes_path)}")
        return True
    except Exception as e:
        print(f"⚠️  Could not record changelog entry: {e}", file=sys.stderr)
        return False


def get_default_editor(package_dir: Optional[str] = None) -> str:
    """
    Resolves the user's preferred editor matching Git's exact resolution precedence (git/editor.c):
      1. $GIT_EDITOR environment variable
      2. 'core.editor' configuration option (local repository, global, or system)
      3. $VISUAL environment variable
      4. $EDITOR environment variable
      5. Default system fallback: 'vi' (or 'vim')
    """
    # 1. $GIT_EDITOR
    val = os.environ.get("GIT_EDITOR")
    if val and val.strip():
        return val.strip()

    # 2. core.editor from Git config
    cmd = ["git"]
    if package_dir and os.path.isdir(package_dir):
        cmd += ["-C", package_dir]
    cmd += ["config", "--get", "core.editor"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
        git_editor = res.stdout.strip()
        if git_editor:
            return git_editor
    except Exception:
        pass

    # Direct check of ~/.gitconfig if git command was overridden or returned empty
    try:
        gitconfig_path = os.path.expanduser("~/.gitconfig")
        if os.path.isfile(gitconfig_path):
            with open(gitconfig_path, "r", encoding="utf-8", errors="ignore") as f:
                in_core = False
                for line in f:
                    sline = line.strip()
                    if sline.startswith("[") and sline.endswith("]"):
                        in_core = (sline.lower() == "[core]")
                    elif in_core and "=" in sline:
                        k, v = sline.split("=", 1)
                        if k.strip().lower() == "editor" and v.strip():
                            return v.strip()
    except Exception:
        pass

    # 3. $VISUAL
    val = os.environ.get("VISUAL")
    if val and val.strip():
        return val.strip()

    # 4. $EDITOR
    val = os.environ.get("EDITOR")
    if val and val.strip():
        return val.strip()

    # 5. Default fallback: vi (or vim)
    for cand in ("vi", "vim"):
        if shutil.which(cand):
            return cand

    return "nano"

def rebase_patch_cli(
    package_dir: str,
    patch_name: Optional[str] = None,
    shell_mode: bool = False,
    sle_mode: bool = False
) -> bool:
    """
    Interactively rebases a failing downstream patch against the current upstream sources.
    Sets up a temporary Git scratch workspace, applies preceding patches in .spec sequence,
    applies the target patch with rejected hunks, and spawns $EDITOR or an interactive subshell.
    Upon exit, automatically updates the patch file on disk preserving headers, and re-verifies.
    """
    pkg_dir = os.path.abspath(package_dir)
    pkg_name = os.path.basename(pkg_dir)

    spec_details = get_spec_details(pkg_dir)
    patch_infos = spec_details.get("patches", [])
    if not patch_infos:
        print(f"{RED}[Error]{RESET} No downstream patches declared in {pkg_name}.spec.", file=sys.stderr)
        return False

    target_patch = None
    target_is_sle = False

    if patch_name:
        clean_target = os.path.basename(patch_name)
        for p in patch_infos:
            if p["name"] == clean_target or p["name"] == patch_name:
                target_patch = p["name"]
                target_is_sle = bool(p.get("is_sle"))
                break
        if not target_patch:
            print(f"{RED}[Error]{RESET} Patch '{patch_name}' is not declared in {pkg_name}.spec.", file=sys.stderr)
            return False
    else:
        # Auto-detect failing patch (openSUSE first, then SLE)
        print(f"🔍 Analyzing downstream patch sequence for {CYAN}{pkg_name}{RESET}...")
        res = check_patches_sequential(pkg_dir)
        sle_stat = res.get("sle_status", {})
        sle_failed = sle_stat.get("failed_patches", [])

        if sle_mode:
            if not sle_failed:
                print(f"{GREEN}🎉 All {sle_stat.get('total', 0)} SLE-only patch(es) already apply cleanly! No rebasing needed.{RESET}\n")
                return True
            target_patch = sle_failed[0]
            target_is_sle = True
            print(f"🎯 Auto-targeting first failing SLE patch: {BOLD}{CYAN}{target_patch}{RESET}\n")
        else:
            if not res["success"] and res.get("failed_patch"):
                target_patch = res["failed_patch"]
                target_is_sle = False
                print(f"🎯 Auto-targeting first failing openSUSE patch: {BOLD}{CYAN}{target_patch}{RESET}\n")
            elif sle_failed:
                target_patch = sle_failed[0]
                target_is_sle = True
                print(f"{GREEN}🎉 All {res['total']} openSUSE patch(es) apply cleanly!{RESET}")
                print(f"🎯 Auto-targeting first failing SLE patch: {BOLD}{CYAN}{target_patch}{RESET}\n")
            else:
                print(f"{GREEN}🎉 All {res['total']} downstream patch(es) already apply cleanly! No rebasing needed.{RESET}\n")
                return True

    target_patch_path = os.path.join(pkg_dir, target_patch)
    if not os.path.isfile(target_patch_path):
        print(f"{RED}[Error]{RESET} Patch file '{target_patch}' does not exist on disk.", file=sys.stderr)
        return False

    # Find patches preceding target_patch
    target_idx = -1
    for idx, p in enumerate(patch_infos):
        if p["name"] == target_patch:
            target_idx = idx
            break

    if target_is_sle:
        # SLE patches apply on top of ALL openSUSE patches, plus any preceding SLE patches
        opensuse_preceding = [p["name"] for p in patch_infos if not p.get("is_sle")]
        sle_preceding = [p["name"] for p in patch_infos[:target_idx] if p.get("is_sle")]
        preceding_patches = opensuse_preceding + sle_preceding
    else:
        preceding_patches = [p["name"] for p in patch_infos[:target_idx] if not p.get("is_sle")]

    # 1. Identify primary source: OBS SCM git repo first (matching check_patches_sequential), then source archive
    service_file = os.path.join(pkg_dir, "_service")
    is_obs_scm = False
    if os.path.isfile(service_file):
        try:
            with open(service_file, 'r', encoding='utf-8', errors='ignore') as sf:
                c = sf.read()
                if "obs_scm" in c or "tar_scm" in c:
                    is_obs_scm = True
        except Exception:
            pass

    repo_dir = None
    if is_obs_scm:
        cand_repo = os.path.join(pkg_dir, pkg_name)
        if os.path.isdir(os.path.join(cand_repo, ".git")):
            repo_dir = cand_repo
        else:
            for item in os.listdir(pkg_dir):
                sub = os.path.join(pkg_dir, item)
                if os.path.isdir(os.path.join(sub, ".git")) and item != ".git":
                    repo_dir = sub
                    break

    archive_path = None
    if not repo_dir:
        archive_path = find_primary_source_archive(pkg_dir, spec_details)
        if not archive_path:
            print(f"{RED}[Error]{RESET} No source archive or upstream Git clone found in {pkg_name}.", file=sys.stderr)
            return False

    # Gather target files
    all_target_files = set()
    for p_info in patch_infos:
        if p_info["name"] in preceding_patches or p_info["name"] == target_patch:
            p_path = os.path.join(pkg_dir, p_info["name"])
            for tf in get_patch_target_files(p_path):
                all_target_files.add(tf)

    with tempfile.TemporaryDirectory() as tmpdir:
        # Populate scratch tree
        if repo_dir:
            # OBS SCM repository: clone locally with shared object store (-s) in milliseconds
            subprocess.run(["git", "clone", "-s", "-q", repo_dir, tmpdir], check=True)
            subprocess.run(["git", "-C", tmpdir, "config", "user.name", "Geckopit Rebase"], check=True)
            subprocess.run(["git", "-C", tmpdir, "config", "user.email", "geckopit@opensuse.org"], check=True)
        else:
            is_lfs, oid, size = parse_lfs_pointer(archive_path)
            if is_lfs:
                loc = find_local_lfs_object(pkg_dir, oid)
                if loc:
                    archive_path = loc

            with tarfile.open(archive_path, mode="r:*") as tf:
                for member in tf.getmembers():
                    parts = member.name.split("/", 1)
                    rel_path = parts[1] if len(parts) > 1 else parts[0]
                    if rel_path in all_target_files or member.name in all_target_files:
                        dest = os.path.join(tmpdir, rel_path)
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        with open(dest, "wb") as out_f:
                            f = tf.extractfile(member)
                            if f:
                                out_f.write(f.read())

            # Check secondary archives
            missing_targets = [t for t in all_target_files if not os.path.exists(os.path.join(tmpdir, t))]
            if missing_targets:
                all_archives = glob.glob(os.path.join(pkg_dir, "*.tar.*")) + glob.glob(os.path.join(pkg_dir, "*.obscpio")) + glob.glob(os.path.join(pkg_dir, "*.zip"))
                secondary_archives = [a for a in all_archives if os.path.abspath(a) != os.path.abspath(archive_path)]
                for sec_arch in secondary_archives:
                    if not missing_targets:
                        break
                    sec_path = sec_arch
                    sec_lfs, s_oid, s_size = parse_lfs_pointer(sec_path)
                    if sec_lfs:
                        s_loc = find_local_lfs_object(pkg_dir, s_oid)
                        if s_loc: sec_path = s_loc
                    try:
                        if tarfile.is_tarfile(sec_path):
                            with tarfile.open(sec_path, mode="r:*") as stf:
                                for member in stf.getmembers():
                                    if not member.isfile(): continue
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
                                                    if src_f: out_f.write(src_f.read())
                                                if target in missing_targets:
                                                    missing_targets.remove(target)
                    except Exception:
                        pass

            subprocess.run(["git", "-C", tmpdir, "init", "-q"], check=True)
            subprocess.run(["git", "-C", tmpdir, "config", "user.name", "Geckopit Rebase"], check=True)
            subprocess.run(["git", "-C", tmpdir, "config", "user.email", "geckopit@opensuse.org"], check=True)
            subprocess.run(["git", "-C", tmpdir, "add", "."], check=True)
            subprocess.run(["git", "-C", tmpdir, "commit", "-qm", "upstream", "--allow-empty"], check=True)


        # Apply preceding patches
        for p in preceding_patches:
            p_path = os.path.join(pkg_dir, p)
            r = subprocess.run(["git", "-C", tmpdir, "apply", "-p1", p_path], capture_output=True)
            if r.returncode != 0:
                subprocess.run(["git", "-C", tmpdir, "apply", "-p0", p_path], capture_output=True)
            subprocess.run(["git", "-C", tmpdir, "add", "."], check=True)
            subprocess.run(["git", "-C", tmpdir, "commit", "-qm", f"Apply {p}", "--allow-empty"], check=True)

        # Check if target patch is already merged upstream (reverse apply succeeds)
        rev_check = subprocess.run(["git", "-C", tmpdir, "apply", "-R", "--check", "-p1", target_patch_path], capture_output=True)
        if rev_check.returncode != 0:
            rev_check = subprocess.run(["git", "-C", tmpdir, "apply", "-R", "--check", "-p0", target_patch_path], capture_output=True)
        if rev_check.returncode == 0:
            print(f"\n🎉 {GREEN}Patch '{target_patch}' is ALREADY MERGED upstream!{RESET}")
            print(f"   (Confirmed: diff matches upstream source and applies in reverse cleanly).")
            print(f"   You can safely drop this patch from {pkg_name}.spec.\n")
            return True

        # Now apply target patch with --reject
        res_apply = subprocess.run(["git", "-C", tmpdir, "apply", "-p1", "--reject", target_patch_path], capture_output=True, text=True)
        if res_apply.returncode != 0 and "No such file or directory" in res_apply.stderr:
            subprocess.run(["git", "-C", tmpdir, "apply", "-p0", "--reject", target_patch_path], capture_output=True, text=True)

        rej_files = sorted(glob.glob(os.path.join(tmpdir, "**", "*.rej"), recursive=True))

        print(f"{BOLD}{'='*80}{RESET}")
        print(f"🔧 {BOLD}INTERACTIVE PATCH REBASE: {CYAN}{target_patch}{RESET}")
        print(f"{BOLD}{'='*80}{RESET}")

        target_sources = []
        if rej_files:
            print(f"❌ {RED}The patch failed to apply cleanly. Rejected hunks were written to:{RESET}")
            for rf in rej_files:
                rel_rf = os.path.relpath(rf, tmpdir)
                source_f = rf[:-4]
                rel_sf = os.path.relpath(source_f, tmpdir)
                target_sources.append(source_f)
                print(f"  • {YELLOW}{rel_sf}{RESET} (rejected hunk in {rel_rf})")

                try:
                    with open(rf, "r", encoding="utf-8", errors="replace") as rfh:
                        lines = rfh.readlines()
                        print(f"\n{DIM}{'─'*35} Rejected Hunk: {rel_rf} {'─'*35}{RESET}")
                        for l in lines[:25]:
                            if l.startswith("+"):
                                print(f"{GREEN}{l.rstrip()}{RESET}")
                            elif l.startswith("-"):
                                print(f"{RED}{l.rstrip()}{RESET}")
                            elif l.startswith("@@"):
                                print(f"{CYAN}{l.rstrip()}{RESET}")
                            else:
                                print(f"{DIM}{l.rstrip()}{RESET}")
                        if len(lines) > 25:
                            print(f"{DIM}... [{len(lines) - 25} lines omitted] ...{RESET}")
                        print(f"{DIM}{'─'*80}{RESET}\n")
                except Exception:
                    pass
        else:
            print(f"{YELLOW}ℹ️  The patch applied without rejected files, but may have offsets or partial fuzz.{RESET}\n")
            target_sources = [os.path.join(tmpdir, tf) for tf in get_patch_target_files(target_patch_path) if os.path.exists(os.path.join(tmpdir, tf))]

        # Launch resolution
        if shell_mode:
            print(f"🚀 {CYAN}Dropping into scratch subshell. Edit the source file(s) and resolve conflicts.{RESET}")
            print(f"   When finished, type {BOLD}'exit'{RESET} to capture the refreshed patch.\n")
            shell_env = os.environ.copy()
            shell_env["PS1"] = fr"(geckopit-rebase:{pkg_name}) \w $ "
            subprocess.run([shell_env.get("SHELL", "/bin/bash")], cwd=tmpdir, env=shell_env)
        else:
            editor = get_default_editor(pkg_dir)
            print(f"✏️  {CYAN}Opening editor ({editor}) to resolve rejected hunks.{RESET}")
            print(f"   Save changes and close the editor when done.\n")

            edit_targets = []
            for sf in target_sources:
                edit_targets.append(sf)
                rf = sf + ".rej"
                if os.path.exists(rf):
                    edit_targets.append(rf)

            editor_parts = shlex.split(editor)
            editor_bin = os.path.basename(editor_parts[0]) if editor_parts else ""

            if "vim" in editor_bin and len(edit_targets) >= 2:
                cmd = editor_parts + ["-O", edit_targets[0], edit_targets[1]]
            else:
                cmd = editor_parts + (edit_targets[:1] if edit_targets else [tmpdir])

            subprocess.run(cmd, cwd=tmpdir)

        # Remove any leftover .rej and .orig files before diffing
        for f in glob.glob(os.path.join(tmpdir, "**", "*.rej"), recursive=True) + glob.glob(os.path.join(tmpdir, "**", "*.orig"), recursive=True):
            try:
                os.remove(f)
            except Exception:
                pass

        # Capture diff against pre-patch HEAD
        diff_res = subprocess.run(["git", "-C", tmpdir, "diff", "--no-ext-diff"], capture_output=True, text=True)
        diff_out = diff_res.stdout

        if not diff_out.strip():
            print(f"\n{YELLOW}⚠️  No changes detected in scratch tree. '{target_patch}' was left unchanged.{RESET}\n")
            return False

        # Extract original patch description and metadata
        orig_header = []
        with open(target_patch_path, "r", encoding="utf-8", errors="replace") as pf:
            for line in pf:
                if line.startswith("diff --git ") or line.startswith("--- "):
                    break
                orig_header.append(line)
        header_str = "".join(orig_header)

        # Write updated patch in place
        with open(target_patch_path, "w", encoding="utf-8") as pf:
            if header_str and not header_str.endswith("\n"):
                header_str += "\n"
            pf.write(header_str + diff_out)

        print(f"\n{GREEN}✨ Successfully refreshed and updated '{target_patch}' in place!{RESET}\n")

        # Record patch rebase in .changes file
        record_patch_rebase_changelog(pkg_dir, target_patch)

        # Automatically re-verify downstream patch sequence
        return run_check_patches_cli(pkg_dir)

