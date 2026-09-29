"""
Meson Dependency Drift Auditor for openSUSE Packaging
Audits upstream Meson build dependency declarations against .spec BuildRequires.
"""

import os
import re
import urllib.parse
from typing import Dict, List, Optional, Tuple

try:
    import rpm
except ImportError:
    rpm = None


# Common mapping from pkg-config / Meson dependency names to openSUSE -devel packages
DEVEL_PACKAGE_MAP = {
    "glib-2.0": "glib2-devel",
    "gobject-2.0": "glib2-devel",
    "gio-2.0": "glib2-devel",
    "gio-unix-2.0": "glib2-devel",
    "gmodule-2.0": "glib2-devel",
    "gthread-2.0": "glib2-devel",
    "gtk4": "gtk4-devel",
    "gtk+-3.0": "gtk3-devel",
    "libadwaita-1": "libadwaita-devel",
    "json-glib-1.0": "json-glib-devel",
    "soup-3.0": "libsoup-devel",
    "vips": "libvips-devel",
    "xmlb": "libxmlb-devel",
    "libxml-2.0": "libxml2-devel",
    "libcurl": "libcurl-devel",
    "pango": "pango-devel",
    "cairo": "cairo-devel",
    "fontconfig": "fontconfig-devel",
    "freetype2": "freetype2-devel",
    "wayland-client": "wayland-devel",
    "wayland-server": "wayland-devel",
    "wayland-protocols": "wayland-protocols-devel",
    "gcr-4": "gcr-devel",
    "gcr-3": "gcr3-devel",
}


def compare_versions(ver1: str, ver2: str) -> int:
    """Compares two RPM version strings using rpm.labelCompare (rpmvercmp)."""
    if not ver1 and not ver2:
        return 0
    if not ver1:
        return -1
    if not ver2:
        return 1

    if rpm is not None:
        try:
            return rpm.labelCompare(("", str(ver1), ""), ("", str(ver2), ""))
        except Exception:
            pass

    def to_tuple(v):
        return tuple(int(x) if x.isdigit() else x for x in re.split(r"([0-9]+)", str(v)) if x)

    t1, t2 = to_tuple(ver1), to_tuple(ver2)
    return (t1 > t2) - (t1 < t2)


def parse_meson_dependencies(content: str) -> Dict[str, Tuple[str, str, bool]]:
    """
    Parses Meson build definitions or unified diffs (osc-collab.meson).
    Returns a dict mapping:
        dependency_name -> (comparator, min_version, is_required)
    """
    if not content:
        return {}

    # 1. If content is a unified diff (e.g. osc-collab.meson), extract target state lines
    lines = []
    is_diff = any(line.startswith(("---", "+++", "@@")) for line in content.splitlines()[:5])
    if is_diff:
        for line in content.splitlines():
            if line.startswith(("---", "+++", "@@")):
                continue
            if line.startswith("-"):
                continue
            if line.startswith("+"):
                lines.append(line[1:])
            else:
                lines.append(line)
        content_clean = "\n".join(lines)
    else:
        content_clean = content

    # 2. Extract Meson string and version variables
    vars_map = {}
    for line in content_clean.splitlines():
        line = line.strip()
        m_var = re.match(r"^([a-zA-Z0-9_]+)\s*=\s*['\"](.*?)['\"]", line)
        if m_var:
            vars_map[m_var.group(1)] = m_var.group(2)

    # 3. Extract dependency(...) calls
    deps = {}
    dep_pattern = re.compile(
        r"dependency\s*\(\s*['\"]([^'\"]+)['\"]\s*(?:,\s*(.*?))?\s*\)",
        re.DOTALL
    )

    for m in dep_pattern.finditer(content_clean):
        name = m.group(1).strip()
        args = m.group(2) or ""

        # Check required flag
        req = not bool(re.search(r"required\s*:\s*false", args, re.IGNORECASE))

        # Check version argument
        ver_match = re.search(r"version\s*:\s*([^,\)]+)", args)
        ver_str = None
        if ver_match:
            raw_val = ver_match.group(1).strip()
            if raw_val.startswith("["):
                m_arr = re.search(r"['\"](>=?\s*[^'\"]+)['\"]", raw_val)
                if m_arr:
                    ver_str = m_arr.group(1)
            elif ('+' not in raw_val) and (raw_val.startswith(("'", '"')) or raw_val in vars_map):
                ver_str = vars_map.get(raw_val, raw_val.strip().strip(chr(39) + chr(34)).strip())
            else:
                tokens = [p.strip().strip(chr(39) + chr(34)).strip() for p in raw_val.split("+")]
                resolved = [vars_map.get(t, t) for t in tokens if t]
                ver_str = ' '.join(resolved).strip()
        else:
            m_pos = re.search(r"^['\"](>=?\s*[^'\"]+)['\"]", args.strip())
            if m_pos:
                ver_str = m_pos.group(1)

        op = ">="
        ver = ""
        if ver_str:
            m_norm = re.match(r"^([><=]+)\s*(.*)$", ver_str.strip())
            if m_norm:
                op = m_norm.group(1)
                ver = m_norm.group(2).strip()
            else:
                ver = ver_str.strip()

        if name in deps:
            existing_op, existing_ver, existing_req = deps[name]
            if compare_versions(ver, existing_ver) > 0:
                deps[name] = (op, ver, req or existing_req)
        else:
            deps[name] = (op, ver, req)

    return deps


def parse_spec_build_requires(spec_content: str) -> Dict[str, Tuple[str, str, str]]:
    """
    Parses BuildRequires in an RPM .spec file.
    Returns a dict mapping normalized package/pkgconfig name to:
        (comparator, version, raw_declaration)
    """
    if not spec_content:
        return {}

    # 1. Expand macros
    macros = {}
    for line in spec_content.splitlines():
        line = line.strip()
        if line.startswith("#"):
            continue
        m_def = re.match(r"^%(?:define|global)\s+([a-zA-Z0-9_]+)\s+(.*)$", line)
        if m_def:
            macros[m_def.group(1)] = m_def.group(2).strip()
            continue
        m_tag = re.match(r"^(Name|Version|Release):\s*(.*)$", line, re.IGNORECASE)
        if m_tag:
            macros[m_tag.group(1).lower()] = m_tag.group(2).strip()

    for _ in range(5):
        changed = False
        for k, v in list(macros.items()):
            for ok, ov in macros.items():
                if k == ok:
                    continue
                pat = r"%\{?" + re.escape(ok) + r"\}?"
                nv = re.sub(pat, ov, v, flags=re.IGNORECASE)
                if nv != v:
                    macros[k] = nv
                    v = nv
                    changed = True
        if not changed:
            break

    # 2. Extract BuildRequires declarations
    spec_deps = {}
    for line in spec_content.splitlines():
        clean_line = line.strip()
        if clean_line.startswith("#") or not clean_line.lower().startswith("buildrequires:"):
            continue

        m = re.match(
            r"^BuildRequires:\s+(\S+)(?:\s*([><=]+)\s*(\S+))?",
            clean_line,
            re.IGNORECASE
        )
        if not m:
            continue

        raw_name = m.group(1).strip()
        op = m.group(2) or ">="
        raw_ver = m.group(3) or ""

        ver = raw_ver
        for k, v in macros.items():
            ver = ver.replace(f"%{{{k}}}", v).replace(f"%{k}", v)

        m_pkg = re.match(r"^pkgconfig\(([^)]+)\)$", raw_name, re.IGNORECASE)
        if m_pkg:
            pkg_name = m_pkg.group(1).strip()
        else:
            pkg_name = raw_name

        spec_deps[pkg_name] = (op, ver, clean_line)
        if raw_name != pkg_name:
            spec_deps[raw_name] = (op, ver, clean_line)

    return spec_deps


def audit_meson_drift(package_dir: str, meson_content: Optional[str] = None, bumps_only: bool = True) -> List[Dict]:
    """
    Audits upstream Meson dependencies against the local .spec file BuildRequires.
    Returns a list of detected drift records.
    """
    if not package_dir or not os.path.isdir(package_dir):
        return []

    # 1. Resolve Meson content
    content = meson_content
    if not content:
        collab_path = os.path.join(package_dir, "osc-collab.meson")
        meson_path = os.path.join(package_dir, "meson.build")
        if os.path.isfile(collab_path):
            try:
                with open(collab_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except Exception:
                pass
        elif os.path.isfile(meson_path):
            try:
                with open(meson_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except Exception:
                pass

    if not content:
        # Check source archives in package_dir (safely resolving Git-LFS pointers)
        archives = [f for f in os.listdir(package_dir) if f.endswith((".tar.xz", ".tar.gz", ".tar.bz2", ".tar.zst"))]
        if archives:
            try:
                from upgrade.tarball import TarballUpgradeHelper
                archive_path = os.path.join(package_dir, sorted(archives)[-1])
                content = TarballUpgradeHelper.extract_member_content(archive_path, "meson.build", package_dir=package_dir)
            except Exception:
                pass

    if not content:
        return []

    upstream_deps = parse_meson_dependencies(content)
    if not upstream_deps:
        return []

    # 2. Resolve local .spec file
    spec_files = [f for f in os.listdir(package_dir) if f.endswith(".spec")]
    if not spec_files:
        return []

    spec_path = os.path.join(package_dir, spec_files[0])
    try:
        with open(spec_path, "r", encoding="utf-8", errors="replace") as f:
            spec_content = f.read()
    except Exception:
        return []

    spec_deps = parse_spec_build_requires(spec_content)

    # 3. Detect drift
    drifts = []
    for dep_name, (u_op, u_ver, u_req) in upstream_deps.items():
        if not u_ver and not u_req:
            continue

        matched_spec = None
        spec_decl_name = f"pkgconfig({dep_name})"

        candidates = [f"pkgconfig({dep_name})", dep_name]
        devel_name = DEVEL_PACKAGE_MAP.get(dep_name)
        if devel_name:
            candidates.append(devel_name)

        for cand in candidates:
            if cand in spec_deps:
                matched_spec = spec_deps[cand]
                spec_decl_name = cand if cand.endswith("-devel") else f"pkgconfig({dep_name})"
                break

        if matched_spec is not None:
            s_op, s_ver, s_raw = matched_spec
            if u_ver and s_ver and compare_versions(u_ver, s_ver) > 0:
                drifts.append({
                    "package": dep_name,
                    "spec_name": spec_decl_name,
                    "upstream_version": u_ver,
                    "spec_version": s_ver,
                    "comparator": u_op,
                    "type": "bump"
                })
            elif not bumps_only and u_ver and not s_ver:
                drifts.append({
                    "package": dep_name,
                    "spec_name": spec_decl_name,
                    "upstream_version": u_ver,
                    "spec_version": None,
                    "comparator": u_op,
                    "type": "unversioned"
                })
        elif not bumps_only:
            # Only report missing if explicitly requested, preventing false positives from disabled options (e.g. -Dqt=false)
            if u_req and u_ver:
                drifts.append({
                    "package": dep_name,
                    "spec_name": f"pkgconfig({dep_name})",
                    "upstream_version": u_ver,
                    "spec_version": None,
                    "comparator": u_op,
                    "type": "missing"
                })

    return drifts


def format_drift_cli_report(drifts: List[Dict]) -> str:
    """Formats detected Meson dependency drift items into an actionable CLI warning banner."""
    if not drifts:
        return ""

    lines = ["\x1b[1;33m⚠️  Meson Dependency Drift Detected:\x1b[0m"]
    for d in drifts:
        spec_name = d.get("spec_name") or d["package"]
        u_ver = d.get("upstream_version")
        op = d.get("comparator") or ">="
        dtype = d.get("type")

        if dtype == "bump":
            s_ver = d.get("spec_version")
            lines.append(f"   • \x1b[1m{spec_name}\x1b[0m {op} \x1b[1;32m{u_ver}\x1b[0m (spec currently requires \x1b[31m>= {s_ver}\x1b[0m)")
        elif dtype == "unversioned":
            lines.append(f"   • \x1b[1m{spec_name}\x1b[0m {op} \x1b[1;32m{u_ver}\x1b[0m (spec currently has unversioned BuildRequires)")
        elif dtype == "missing":
            lines.append(f"   • \x1b[1m{spec_name}\x1b[0m {op} \x1b[1;32m{u_ver}\x1b[0m (missing from .spec BuildRequires)")

    return "\n".join(lines)


def format_drift_short_summary(drifts: List[Dict]) -> str:
    """Formats a concise one-line summary for badges and GUI status cards."""
    if not drifts:
        return ""

    bumps = [d for d in drifts if d.get("type") == "bump"]
    missing = [d for d in drifts if d.get("type") == "missing"]
    unversioned = [d for d in drifts if d.get("type") == "unversioned"]

    parts = []
    if bumps:
        parts.append(f"{len(bumps)} version bump{'s' if len(bumps) != 1 else ''}")
    if missing:
        parts.append(f"{len(missing)} missing")
    if unversioned:
        parts.append(f"{len(unversioned)} unversioned")

    return f"⚠️ Meson drift: {', '.join(parts)}"
