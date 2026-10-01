#!/usr/bin/env python3
"""
Upstream Build Dependency Drift Auditor & Automated Spec Fixer.
Supports Meson (meson.build), CMake (CMakeLists.txt), Autotools (configure.ac/in),
and Python (pyproject.toml / PEP 517 & 518 & 621 / Poetry).
Inspects RPM .spec files (including declarative 'BuildSystem:' tags and %build macros)
to strictly audit only the build system actually used during packaging.
"""

import os
import sys
import re
import shlex
import glob
from typing import Dict, List, Tuple, Optional

# Package name normalization map: maps pkg-config/upstream names to spec devel package names
DEVEL_PACKAGE_MAP = {
    "glib-2.0": "glib2-devel",
    "gobject-2.0": "glib2-devel",
    "gio-2.0": "glib2-devel",
    "gio-unix-2.0": "glib2-devel",
    "gmodule-2.0": "glib2-devel",
    "gmodule-export-2.0": "glib2-devel",
    "gthread-2.0": "glib2-devel",
    "gtk4": "gtk4-devel",
    "gtk+-3.0": "gtk3-devel",
    "libadwaita-1": "libadwaita-devel",
    "libxml-2.0": "libxml2-devel",
    "libxslt": "libxslt-devel",
    "libsoup-3.0": "libsoup-devel",
    "libsoup-2.4": "libsoup2-devel",
    "json-glib-1.0": "json-glib-devel",
    "gstreamer-1.0": "gstreamer-devel",
    "gstreamer-base-1.0": "gstreamer-plugins-base-devel",
    "gstreamer-video-1.0": "gstreamer-plugins-base-devel",
    "gstreamer-audio-1.0": "gstreamer-plugins-base-devel",
    "gstreamer-pbutils-1.0": "gstreamer-plugins-base-devel",
    "gstreamer-tag-1.0": "gstreamer-plugins-base-devel",
    "sqlite3": "sqlite3-devel",
    "wayland-client": "wayland-devel",
    "wayland-server": "wayland-devel",
    "wayland-protocols": "wayland-protocols-devel",
    "gcr-4": "gcr-devel",
    "gcr-3": "gcr3-devel",
    "cairo": "cairo-devel",
    "pango": "pango-devel",
    "fontconfig": "fontconfig-devel",
    "freetype2": "freetype2-devel",
    "atk": "atk-devel",
    "gdk-pixbuf-2.0": "gdk-pixbuf-devel",
    "gdk-pixbuf": "gdk-pixbuf-devel",
    "libcurl": "libcurl-devel",
}

# Python package alias map: maps PyPI module names to openSUSE %{python_module ...} candidate names
PYTHON_PACKAGE_MAP = {
    "pygobject": ["gobject", "pygobject"],
    "gobject": ["gobject", "pygobject"],
    "pycairo": ["cairo", "pycairo"],
    "cairo": ["cairo", "pycairo"],
    "poetry-core": ["poetry-core"],
    "poetry_core": ["poetry-core"],
    "flit-core": ["flit-core"],
    "flit_core": ["flit-core"],
    "setuptools-scm": ["setuptools-scm"],
    "setuptools_scm": ["setuptools-scm"],
}


def compare_versions(ver1: str, ver2: str) -> int:
    """Compares two RPM version strings using rpm.labelCompare (rpmvercmp)."""
    if not ver1 and not ver2:
        return 0
    if not ver1:
        return -1
    if not ver2:
        return 1

    try:
        import rpm
        # rpm.labelCompare expects (epoch, version, release) tuples
        return rpm.labelCompare(('0', str(ver1), '0'), ('0', str(ver2), '0'))
    except ImportError:
        pass

    # Lightweight fallback comparison
    def normalize_tokens(s):
        return [int(x) if x.isdigit() else x for x in re.split(r"([0-9]+|[\.\-_~])", s) if x and x not in (".", "-", "_", "~")]

    t1 = normalize_tokens(ver1)
    t2 = normalize_tokens(ver2)
    for a, b in zip(t1, t2):
        if type(a) == type(b):
            if a < b:
                return -1
            elif a > b:
                return 1
        else:
            if str(a) < str(b):
                return -1
            elif str(a) > str(b):
                return 1
    if len(t1) < len(t2):
        return -1
    elif len(t1) > len(t2):
        return 1
    return 0


def is_python_package(spec_content: str) -> bool:
    """Returns True if the spec builds or installs a Python wheel or module."""
    if not spec_content:
        return False
    if re.search(r"^\s*%(?:pyproject_wheel|pyproject_install|python_subpackages)\b", spec_content, re.MULTILINE):
        return True
    if re.search(r"^\s*BuildSystem:\s*pyproject\b", spec_content, re.MULTILINE | re.IGNORECASE):
        return True
    return False


def detect_package_build_system(spec_content: str) -> Optional[str]:
    """
    Detects which primary build system is actually used during the RPM build by inspecting .spec.
    Guards against sources providing multiple build files (e.g. legacy autotools alongside meson).
    Returns 'meson', 'cmake', 'autotools', 'python', or None.
    """
    if not spec_content:
        return None

    # 1. Declarative BuildSystem tag (RPM 4.20+, e.g. 'BuildSystem: meson')
    m_bs = re.search(r"^\s*BuildSystem:\s*([a-zA-Z0-9_\-+]+)\b", spec_content, re.MULTILINE | re.IGNORECASE)
    if m_bs:
        bs_val = m_bs.group(1).lower()
        if "meson" in bs_val:
            return "meson"
        elif "cmake" in bs_val:
            return "cmake"
        elif "autotools" in bs_val or "configure" in bs_val:
            return "autotools"
        elif "pyproject" in bs_val or "python" in bs_val:
            return "python"

    # 2. Build invocation macros in %build / %install
    if re.search(r"^\s*%(?:meson|meson_build|meson_install)\b", spec_content, re.MULTILINE):
        return "meson"
    if re.search(r"^\s*%(?:cmake|cmake_build|cmake_install|cmake_qt6)\b", spec_content, re.MULTILINE):
        return "cmake"
    if re.search(r"^\s*(?:%configure|\./configure|autoreconf)\b", spec_content, re.MULTILINE):
        return "autotools"
    if re.search(r"^\s*%(?:pyproject_wheel|pyproject_install)\b", spec_content, re.MULTILINE):
        return "python"

    # 3. Explicit build system tool BuildRequires
    if re.search(r"^\s*BuildRequires:\s+meson\b", spec_content, re.MULTILINE | re.IGNORECASE):
        return "meson"
    if re.search(r"^\s*BuildRequires:\s+cmake\b", spec_content, re.MULTILINE | re.IGNORECASE):
        return "cmake"
    if re.search(r"^\s*BuildRequires:\s+(?:libtool|autoconf|automake)\b", spec_content, re.MULTILINE | re.IGNORECASE):
        return "autotools"
    if re.search(r"^\s*BuildRequires:\s+python-rpm-macros\b", spec_content, re.MULTILINE | re.IGNORECASE):
        return "python"

    return None


def find_obs_scm_clone_dir(package_dir: str) -> Optional[str]:
    """
    Locates the directory where obs_scm / tar_scm cloned the upstream repository, if present.
    Strictly limited to the directory of the first obs_scm clone.
    """
    service_file = os.path.join(package_dir, "_service")
    if not os.path.isfile(service_file):
        return None

    repo_name = None
    try:
        import xml.etree.ElementTree as ET
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
                c = f.read()
            m = re.search(r"""<param\s+name=['"]url['"]>([^<]+)</param>""", c)
            if m:
                u = re.sub(r"\.git/?$", "", m.group(1).strip())
                repo_name = u.rstrip("/").split("/")[-1]
        except Exception:
            pass

    pkg_name = os.path.basename(os.path.abspath(package_dir))
    candidates = []
    if repo_name:
        candidates.append(repo_name)
    if pkg_name and pkg_name not in candidates:
        candidates.append(pkg_name)

    # 1. Check candidate directory paths with .git
    for cand in candidates:
        cand_path = os.path.join(package_dir, cand)
        if os.path.isdir(cand_path):
            git_entry = os.path.join(cand_path, ".git")
            if os.path.isdir(git_entry) or os.path.isfile(git_entry):
                return cand_path

    # 2. Check candidate directory paths with build definition files
    for cand in candidates:
        cand_path = os.path.join(package_dir, cand)
        if os.path.isdir(cand_path):
            for f in ("meson.build", "CMakeLists.txt", "configure.ac", "configure.in", "pyproject.toml"):
                if os.path.isfile(os.path.join(cand_path, f)):
                    return cand_path

    return None


def parse_meson_dependencies(content: str) -> Dict[str, Tuple[str, str, bool]]:
    """
    Parses dependency('foo', version: '>= 1.2') calls in Meson build files.
    Returns a dict mapping package name to:
        (comparator, minimum_version, is_required)
    """
    if not content:
        return {}

    # 1. Clean diff markers if content is a unified diff
    if "\n+" in content or "\n-" in content:
        lines = []
        for line in content.splitlines():
            if line.startswith("-") and not line.startswith("---"):
                continue
            if line.startswith("+") and not line.startswith("+++"):
                line = line[1:]
            line = re.sub(r"#.*$", "", line)
            lines.append(line)
        content_clean = "\n".join(lines)
    else:
        lines = [re.sub(r"#.*$", "", l) for l in content.splitlines()]
        content_clean = "\n".join(lines)

    # 2. Extract Meson string and version variables (including .format() resolution)
    vars_map = {}
    for line in content_clean.splitlines():
        line = line.strip()
        m_var = re.match(r"""^([a-zA-Z0-9_]+)\s*=\s*['"](.*?)['"](?:\.format\((.*?)\))?""", line)
        if m_var:
            v_name = m_var.group(1)
            v_fmt = m_var.group(2)
            v_args = m_var.group(3)
            if v_args:
                arg_tokens = [arg.strip().strip("'\"") for arg in v_args.split(",") if arg.strip()]
                for idx, arg_val in enumerate(arg_tokens):
                    resolved_val = vars_map.get(arg_val, arg_val)
                    v_fmt = v_fmt.replace(f"@{idx}@", resolved_val)
            vars_map[v_name] = v_fmt

    # 3. Extract dependency(...) calls
    deps = {}
    dep_pattern = re.compile(
        r"""dependency\s*\(\s*['"]([^'"]+)['"]\s*(?:,\s*(.*?))?\s*\)""",
        re.DOTALL
    )

    for m in dep_pattern.finditer(content_clean):
        name = m.group(1).strip()
        args_str = m.group(2) or ""

        # Check required flag
        is_req = True
        req_match = re.search(r"required\s*:\s*(true|false|get_option\([^)]+\))", args_str, re.IGNORECASE)
        if req_match and req_match.group(1).lower() == "false":
            is_req = False

        # Extract version parameter (named 'version:' or positional second argument)
        ver_match = re.search(r"version\s*:\s*([^,)]+)", args_str)
        ver_str = ""
        if ver_match:
            raw_val = ver_match.group(1).strip()
            if raw_val.startswith("["):
                m_arr = re.search(r"""['"](>=?\s*[^'"]+)['"]""", raw_val)
                if m_arr:
                    ver_str = m_arr.group(1)
            elif ('+' not in raw_val) and (raw_val.startswith(("'", '"')) or raw_val in vars_map):
                ver_str = vars_map.get(raw_val, raw_val.strip().strip(chr(39) + chr(34)).strip())
            else:
                tokens = [p.strip().strip(chr(39) + chr(34)).strip() for p in raw_val.split("+")]
                resolved = [vars_map.get(t, t) for t in tokens if t]
                ver_str = "".join(resolved)
        else:
            # Positional version argument, e.g. dependency('json-glib-1.0', '>= 1.6.0')
            m_pos = re.match(r"""^\s*['"](>=?\s*[^'"]+)['"]""", args_str)
            if m_pos:
                ver_str = m_pos.group(1)
            elif args_str.strip() in vars_map:
                ver_str = vars_map[args_str.strip()]

        op = ">="
        ver = ""
        if ver_str:
            m_norm = re.match(r"^([><=]+)\s*(.*)$", ver_str.strip())
            if m_norm:
                op = m_norm.group(1)
                ver = m_norm.group(2).strip()
            else:
                ver = ver_str.strip()

        if "@" in ver:
            ver = ""

        if name not in deps or (ver and not deps[name][1]):
            deps[name] = (op, ver, is_req)

    return deps


def parse_cmake_dependencies(content: str) -> Dict[str, Tuple[str, str, bool]]:
    """
    Parses pkg_check_modules() and find_package() calls in CMakeLists.txt.
    Returns a dict mapping package name to:
        (comparator, minimum_version, is_required)
    """
    if not content:
        return {}

    lines = [re.sub(r"#.*$", "", l) for l in content.splitlines()]
    clean_content = "\n".join(lines)

    # 1. Extract CMake set(...) variables
    vars_map = {}
    for m in re.finditer(r"""set\s*\(\s*([a-zA-Z0-9_]+)\s+['"]?([0-9]+(?:\.[0-9]+)*)['"]?\s*\)""", clean_content, re.IGNORECASE):
        vars_map[m.group(1)] = m.group(2)

    deps = {}

    # 2. Extract pkg_check_modules / pkg_check_modules_for_option calls
    pkg_calls = re.findall(r"pkg_check_modules(?:_for_option)?\s*\((.*?)\)", clean_content, re.DOTALL | re.IGNORECASE)
    for call in pkg_calls:
        expanded = re.sub(r"\$\{([a-zA-Z0-9_]+)\}", lambda m: vars_map.get(m.group(1), m.group(0)), call)
        is_req = "REQUIRED" in expanded.upper()
        try:
            tokens = shlex.split(expanded)
        except Exception:
            tokens = expanded.split()

        for t in tokens:
            m = re.match(r"^([a-zA-Z0-9_\-+.]+)\s*([><=]+)\s*([0-9]+(?:\.[0-9]+)*.*)$", t)
            if m:
                pkg, op, ver = m.group(1), m.group(2), m.group(3)
                if "@" not in ver and not ver.startswith("${"):
                    if pkg not in deps or (ver and not deps[pkg][1]):
                        deps[pkg] = (op, ver, is_req)
            else:
                m_unv = re.match(r"^([a-zA-Z0-9_\-+.]+)$", t)
                if m_unv and t.upper() not in ("REQUIRED", "QUIET", "IMPORTED_TARGET", "GLOBAL", "NO_CMAKE_PATH", "NO_CMAKE_ENVIRONMENT_PATH"):
                    if t not in deps and not t.isupper():
                        deps[t] = (">=", "", is_req)

    # 3. Extract find_package calls
    find_calls = re.findall(r"find_package\s*\(\s*([a-zA-Z0-9_\-+]+)(.*?)\)", clean_content, re.DOTALL | re.IGNORECASE)
    for pkg_name, rest in find_calls:
        if pkg_name.upper() in ("REQUIRED", "CONFIG", "MODULE", "NO_MODULE", "QUIET", "COMPONENTS", "OPTIONAL_COMPONENTS"):
            continue
        rest_expanded = re.sub(r"\$\{([a-zA-Z0-9_]+)\}", lambda m: vars_map.get(m.group(1), m.group(0)), rest)
        is_req = "REQUIRED" in rest_expanded.upper()
        m_ver = re.search(r"\b([0-9]+(?:\.[0-9]+)+)\b", rest_expanded)
        if m_ver:
            ver = m_ver.group(1)
            pkg_key = pkg_name.lower()
            if pkg_key not in deps or (ver and not deps[pkg_key][1]):
                deps[pkg_key] = (">=", ver, is_req)

    return deps


def parse_autotools_dependencies(content: str) -> Dict[str, Tuple[str, str, bool]]:
    """
    Parses PKG_CHECK_MODULES() and PKG_CHECK_EXISTS() calls in configure.ac / configure.in.
    Returns a dict mapping package name to:
        (comparator, minimum_version, is_required)
    """
    if not content:
        return {}

    lines = []
    for line in content.splitlines():
        line = re.sub(r"dnl.*$", "", line)
        line = re.sub(r"#.*$", "", line)
        lines.append(line)
    clean_content = "\n".join(lines)

    # 1. Extract shell and m4 variables
    vars_map = {}
    for line in clean_content.splitlines():
        line_s = line.strip()
        m_var = re.match(r"""^([a-zA-Z0-9_]+)\s*=\s*['"]?([a-zA-Z0-9_.\-+]+)['"]?""", line_s)
        if m_var:
            vars_map[m_var.group(1)] = m_var.group(2)
        m_m4 = re.match(r"""^m4_define\s*\(\s*\[?([a-zA-Z0-9_]+)\]?\s*,\s*\[?([a-zA-Z0-9_.\-+]+)\]?\s*\)""", line_s)
        if m_m4:
            vars_map[m_m4.group(1)] = m_m4.group(2)

    for _ in range(3):
        changed = False
        for k, v in list(vars_map.items()):
            nv = re.sub(r"\$([a-zA-Z0-9_]+)", lambda m: vars_map.get(m.group(1), m.group(0)), v)
            nv = re.sub(r"\$\{([a-zA-Z0-9_]+)\}", lambda m: vars_map.get(m.group(1), m.group(0)), nv)
            if nv != v:
                vars_map[k] = nv
                changed = True
        if not changed:
            break

    deps = {}
    pkg_calls = re.findall(r"PKG_CHECK_(?:MODULES|EXISTS)\s*\(\s*\[?[a-zA-Z0-9_]+\]?\s*,\s*\[?(.*?)\]?\s*(?:,\s*.*?)?\)", clean_content, re.DOTALL)
    for call in pkg_calls:
        expanded = re.sub(r"\$([a-zA-Z0-9_]+)", lambda m: vars_map.get(m.group(1), m.group(0)), call)
        expanded = re.sub(r"\$\{([a-zA-Z0-9_]+)\}", lambda m: vars_map.get(m.group(1), m.group(0)), expanded)
        tokens = re.split(r"[\s\\\\]+", expanded)
        idx = 0
        while idx < len(tokens):
            tok = tokens[idx].strip().strip('[]\"\'')
            if not tok:
                idx += 1
                continue
            m_full = re.match(r"^([a-zA-Z0-9_\-+.]+)\s*([><=]+)\s*(.*)$", tok)
            if m_full:
                pkg, op, raw_ver = m_full.group(1), m_full.group(2), m_full.group(3).strip('[]\"\'')
                ver = vars_map.get(raw_ver, raw_ver)
                if re.match(r"^[0-9]+(?:\.[0-9]+)*", ver):
                    if pkg not in deps or (ver and not deps[pkg][1]):
                        deps[pkg] = (op, ver, True)
                elif pkg not in deps:
                    deps[pkg] = (op, "", True)
                idx += 1
                continue
            m_pkg = re.match(r"^([a-zA-Z0-9_\-+.]+)$", tok)
            if m_pkg and idx + 2 < len(tokens) and re.match(r"^[><=]+$", tokens[idx+1].strip()):
                pkg = m_pkg.group(1)
                op = tokens[idx+1].strip()
                raw_ver = tokens[idx+2].strip().strip('[]\"\'')
                ver = vars_map.get(raw_ver, raw_ver)
                if re.match(r"^[0-9]+(?:\.[0-9]+)*", ver):
                    if pkg not in deps or (ver and not deps[pkg][1]):
                        deps[pkg] = (op, ver, True)
                elif pkg not in deps:
                    deps[pkg] = (op, "", True)
                idx += 3
                continue
            idx += 1

    return deps


def parse_python_dependencies(content: str) -> Dict[str, Tuple[str, str, bool]]:
    """
    Parses build-system.requires, project.dependencies, and tool.poetry.dependencies
    from pyproject.toml content.
    Returns a dict mapping normalized package name to:
        (comparator, minimum_version, is_required)
    """
    if not content:
        return {}

    deps = {}
    try:
        try:
            import tomllib
            data = tomllib.loads(content)
        except ImportError:
            import tomli as tomllib
            data = tomllib.loads(content)
    except Exception:
        # Fallback basic regex parsing for [build-system] and [project]
        data = {}
        m_bs = re.search(r"\[build-system\][^\[]*requires\s*=\s*\[(.*?)\]", content, re.DOTALL)
        if m_bs:
            data.setdefault("build-system", {})["requires"] = re.findall(r"""['"]([^'"]+)['"]""", m_bs.group(1))
        m_proj = re.search(r"\[project\][^\[]*dependencies\s*=\s*\[(.*?)\]", content, re.DOTALL)
        if m_proj:
            data.setdefault("project", {})["dependencies"] = re.findall(r"""['"]([^'"]+)['"]""", m_proj.group(1))

    reqs = []
    # 1. PEP 518 build-system requires
    if "build-system" in data and "requires" in data["build-system"]:
        for r in data["build-system"]["requires"]:
            if isinstance(r, str):
                reqs.append((r, True))

    # 2. PEP 621 project dependencies
    if "project" in data and "dependencies" in data["project"]:
        for r in data["project"]["dependencies"]:
            if isinstance(r, str):
                reqs.append((r, True))

    # 3. Poetry dependencies
    if "tool" in data and "poetry" in data["tool"] and "dependencies" in data["tool"]["poetry"]:
        poetry_deps = data["tool"]["poetry"]["dependencies"]
        for p_name, p_val in poetry_deps.items():
            if p_name.lower() == "python":
                continue
            if isinstance(p_val, str):
                reqs.append((f"{p_name} {p_val}", True))
            elif isinstance(p_val, dict) and "version" in p_val:
                is_opt = p_val.get("optional", False)
                if not is_opt:
                    reqs.append((f"{p_name} {p_val['version']}", True))

    for req_str, is_req in reqs:
        base = req_str.split(";")[0].strip()
        if not base:
            continue
        m = re.match(r"^([a-zA-Z0-9_\-+.]+)\s*([><=~!^]+)?\s*([0-9]+(?:\.[0-9]+)*.*)?$", base)
        if m:
            raw_name = m.group(1).strip()
            raw_op = m.group(2) or ">="
            raw_ver = (m.group(3) or "").strip()
            op = ">=" if raw_op in ("^", "~", "~=") else raw_op
            clean_name = re.sub(r"\[.*?\]", "", raw_name).lower().replace("_", "-")
            if clean_name not in deps or (raw_ver and not deps[clean_name][1]):
                deps[clean_name] = (op, raw_ver, is_req)

    return deps


def parse_spec_build_requires(spec_content: str) -> Dict[str, Tuple[str, str, str]]:
    """
    Parses BuildRequires in an RPM .spec file, including pkgconfig(...) declarations,
    regular package names, and openSUSE %{python_module ...} macro declarations.
    Returns a dict mapping normalized package/module name to:
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

        # Check for openSUSE %{python_module <modname> [op ver]}
        m_pymod = re.match(
            r"^BuildRequires:\s+%{python_module\s+([a-zA-Z0-9_\-+.]+)(?:\s*([><=]+)\s*([^}]+))?}",
            clean_line,
            re.IGNORECASE
        )
        if m_pymod:
            pkg_name = m_pymod.group(1).strip().lower().replace("_", "-")
            op = m_pymod.group(2) or ">="
            raw_ver = (m_pymod.group(3) or "").strip()
            ver = raw_ver
            for k, v in macros.items():
                ver = ver.replace(f"%{{{k}}}", v).replace(f"%{k}", v)

            spec_deps[pkg_name] = (op, ver, clean_line)
            spec_deps[f"python-{pkg_name}"] = (op, ver, clean_line)
            spec_deps[f"python3-{pkg_name}"] = (op, ver, clean_line)
            spec_deps[f"%{{python_module {pkg_name}}}"] = (op, ver, clean_line)
            continue

        # Standard BuildRequires: <name> [op ver]
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

        # Also register python3- prefixed names without prefix
        if pkg_name.startswith("python3-"):
            spec_deps[pkg_name[8:]] = (op, ver, clean_line)
        elif pkg_name.startswith("python-"):
            spec_deps[pkg_name[7:]] = (op, ver, clean_line)

    return spec_deps


def audit_meson_drift(package_dir: str, meson_content: Optional[str] = None, bumps_only: bool = True, include_unversioned: bool = True, build_system: Optional[str] = None) -> List[Dict]:
    """
    Audits upstream build dependencies (Meson, CMake, Autotools, or Python/pyproject.toml)
    against local .spec BuildRequires.
    Detects build system from .spec to ensure only the build system used during RPM build is audited.
    Returns a list of detected drift records.
    """
    if not package_dir or not os.path.isdir(package_dir):
        return []

    # 1. Resolve local .spec file and detect active build system
    spec_files = [f for f in os.listdir(package_dir) if f.endswith(".spec")]
    if not spec_files:
        return []

    spec_path = os.path.join(package_dir, spec_files[0])
    try:
        with open(spec_path, "r", encoding="utf-8", errors="replace") as f:
            spec_content = f.read()
    except Exception:
        return []

    active_build_system = build_system or detect_package_build_system(spec_content)
    if not active_build_system:
        active_build_system = "meson"

    has_python = is_python_package(spec_content)
    spec_deps = parse_spec_build_requires(spec_content)
    drifts = []

    # 2. Resolve build definition content for the active primary build system
    content = meson_content
    if not content:
        if active_build_system == "meson":
            meson_path = os.path.join(package_dir, "meson.build")
            if os.path.isfile(meson_path):
                try:
                    with open(meson_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                except Exception:
                    pass
            else:
                clone_dir = find_obs_scm_clone_dir(package_dir)
                if clone_dir and os.path.isfile(os.path.join(clone_dir, "meson.build")):
                    try:
                        with open(os.path.join(clone_dir, "meson.build"), "r", encoding="utf-8", errors="replace") as f:
                            content = f.read()
                    except Exception:
                        pass
            if not content:
                archives = [f for f in os.listdir(package_dir) if f.endswith((".tar.xz", ".tar.gz", ".tar.bz2", ".tar.zst"))]
                if archives:
                    try:
                        from upgrade.tarball import TarballUpgradeHelper
                        archive_path = os.path.join(package_dir, sorted(archives)[-1])
                        content = TarballUpgradeHelper.extract_member_content(archive_path, "meson.build", package_dir=package_dir)
                    except Exception:
                        pass

        elif active_build_system == "cmake":
            cmake_path = os.path.join(package_dir, "CMakeLists.txt")
            if os.path.isfile(cmake_path):
                try:
                    with open(cmake_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                except Exception:
                    pass
            else:
                clone_dir = find_obs_scm_clone_dir(package_dir)
                if clone_dir and os.path.isfile(os.path.join(clone_dir, "CMakeLists.txt")):
                    try:
                        with open(os.path.join(clone_dir, "CMakeLists.txt"), "r", encoding="utf-8", errors="replace") as f:
                            content = f.read()
                    except Exception:
                        pass
            if not content:
                archives = [f for f in os.listdir(package_dir) if f.endswith((".tar.xz", ".tar.gz", ".tar.bz2", ".tar.zst"))]
                if archives:
                    try:
                        from upgrade.tarball import TarballUpgradeHelper
                        archive_path = os.path.join(package_dir, sorted(archives)[-1])
                        content = TarballUpgradeHelper.extract_member_content(archive_path, "CMakeLists.txt", package_dir=package_dir)
                    except Exception:
                        pass

        elif active_build_system == "autotools":
            for conf_name in ("configure.ac", "configure.in"):
                conf_path = os.path.join(package_dir, conf_name)
                if os.path.isfile(conf_path):
                    try:
                        with open(conf_path, "r", encoding="utf-8", errors="replace") as f:
                            content = f.read()
                            break
                    except Exception:
                        pass
            if not content:
                clone_dir = find_obs_scm_clone_dir(package_dir)
                if clone_dir:
                    for conf_name in ("configure.ac", "configure.in"):
                        c_p = os.path.join(clone_dir, conf_name)
                        if os.path.isfile(c_p):
                            try:
                                with open(c_p, "r", encoding="utf-8", errors="replace") as f:
                                    content = f.read()
                                    break
                            except Exception:
                                pass
            if not content:
                archives = [f for f in os.listdir(package_dir) if f.endswith((".tar.xz", ".tar.gz", ".tar.bz2", ".tar.zst"))]
                if archives:
                    try:
                        from upgrade.tarball import TarballUpgradeHelper
                        archive_path = os.path.join(package_dir, sorted(archives)[-1])
                        content = TarballUpgradeHelper.extract_member_content(archive_path, "configure.ac", package_dir=package_dir)
                        if not content:
                            content = TarballUpgradeHelper.extract_member_content(archive_path, "configure.in", package_dir=package_dir)
                    except Exception:
                        pass

    # 3. Parse primary upstream dependencies
    upstream_deps = {}
    if content:
        if active_build_system == "cmake":
            upstream_deps = parse_cmake_dependencies(content)
        elif active_build_system == "autotools":
            upstream_deps = parse_autotools_dependencies(content)
        elif active_build_system == "meson":
            upstream_deps = parse_meson_dependencies(content)

    for dep_name, (u_op, u_ver, u_req) in upstream_deps.items():
        matched_spec = None
        spec_decl_name = None

        cands = [f"pkgconfig({dep_name})", dep_name]
        devel_name = DEVEL_PACKAGE_MAP.get(dep_name)
        if devel_name:
            cands.append(devel_name)

        for cand in cands:
            if cand in spec_deps:
                matched_spec = spec_deps[cand]
                spec_decl_name = cand
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
                    "type": "bump",
                    "build_system": active_build_system
                })
            elif u_ver and not s_ver and (include_unversioned or not bumps_only):
                drifts.append({
                    "package": dep_name,
                    "spec_name": spec_decl_name,
                    "upstream_version": u_ver,
                    "spec_version": None,
                    "comparator": u_op,
                    "type": "unversioned",
                    "build_system": active_build_system
                })
        elif not bumps_only and u_req and u_ver:
            drifts.append({
                "package": dep_name,
                "spec_name": f"pkgconfig({dep_name})",
                "upstream_version": u_ver,
                "spec_version": None,
                "comparator": u_op,
                "type": "missing",
                "build_system": active_build_system
            })

    # 4. If package uses Python (%pyproject_wheel), audit pyproject.toml
    if has_python or active_build_system == "python":
        pyproject_content = None
        py_local = os.path.join(package_dir, "pyproject.toml")
        if os.path.isfile(py_local):
            try:
                with open(py_local, "r", encoding="utf-8", errors="replace") as f:
                    pyproject_content = f.read()
            except Exception:
                pass
        else:
            clone_dir = find_obs_scm_clone_dir(package_dir)
            if clone_dir and os.path.isfile(os.path.join(clone_dir, "pyproject.toml")):
                try:
                    with open(os.path.join(clone_dir, "pyproject.toml"), "r", encoding="utf-8", errors="replace") as f:
                        pyproject_content = f.read()
                except Exception:
                    pass
        if not pyproject_content:
            archives = [f for f in os.listdir(package_dir) if f.endswith((".tar.xz", ".tar.gz", ".tar.bz2", ".tar.zst"))]
            if archives:
                try:
                    from upgrade.tarball import TarballUpgradeHelper
                    archive_path = os.path.join(package_dir, sorted(archives)[-1])
                    pyproject_content = TarballUpgradeHelper.extract_member_content(archive_path, "pyproject.toml", package_dir=package_dir)
                except Exception:
                    pass

        if pyproject_content:
            py_deps = parse_python_dependencies(pyproject_content)
            for py_pkg, (u_op, u_ver, u_req) in py_deps.items():
                matched_spec = None
                spec_decl_name = None

                # Build candidate names to look up in spec_deps
                py_cands = [py_pkg, f"python-{py_pkg}", f"python3-{py_pkg}", f"%{{python_module {py_pkg}}}"]
                mapped = PYTHON_PACKAGE_MAP.get(py_pkg, [])
                for m_alias in mapped:
                    py_cands.extend([m_alias, f"python-{m_alias}", f"python3-{m_alias}", f"%{{python_module {m_alias}}}"])

                for cand in py_cands:
                    if cand in spec_deps:
                        matched_spec = spec_deps[cand]
                        spec_decl_name = cand
                        break

                if matched_spec is not None:
                    s_op, s_ver, s_raw = matched_spec
                    if u_ver and s_ver and compare_versions(u_ver, s_ver) > 0:
                        drifts.append({
                            "package": py_pkg,
                            "spec_name": spec_decl_name,
                            "upstream_version": u_ver,
                            "spec_version": s_ver,
                            "comparator": u_op,
                            "type": "bump",
                            "build_system": "python"
                        })
                    elif u_ver and not s_ver and (include_unversioned or not bumps_only):
                        drifts.append({
                            "package": py_pkg,
                            "spec_name": spec_decl_name,
                            "upstream_version": u_ver,
                            "spec_version": None,
                            "comparator": u_op,
                            "type": "unversioned",
                            "build_system": "python"
                        })

    return drifts


def format_drift_cli_report(drifts: List[Dict]) -> str:
    """Formats detected dependency drift items into an actionable CLI warning banner."""
    if not drifts:
        return ""

    build_systems = list(dict.fromkeys(d.get("build_system", "meson") for d in drifts))
    sys_labels = []
    for bs in build_systems:
        if bs == "cmake":
            sys_labels.append("CMake")
        elif bs == "autotools":
            sys_labels.append("Autotools")
        elif bs == "python":
            sys_labels.append("Python (pyproject.toml)")
        else:
            sys_labels.append("Meson")

    title = " & ".join(sys_labels)
    lines = [f"\x1b[1;33m⚠️  {title} Dependency Drift Detected:\x1b[0m"]
    for d in drifts:
        spec_name = d.get("spec_name") or d["package"]
        u_ver = d["upstream_version"]
        s_ver = d.get("spec_version")
        op = d.get("comparator") or ">="
        dtype = d.get("type", "bump")

        if dtype == "bump":
            lines.append(f"   • \x1b[1m{spec_name}\x1b[0m {op} \x1b[1;32m{u_ver}\x1b[0m (spec currently requires \x1b[1;31m{s_ver}\x1b[0m)")
        elif dtype == "unversioned":
            lines.append(f"   • \x1b[1m{spec_name}\x1b[0m {op} \x1b[1;32m{u_ver}\x1b[0m (spec currently has unversioned BuildRequires)")
        elif dtype == "missing":
            lines.append(f"   • \x1b[1m{spec_name}\x1b[0m {op} \x1b[1;32m{u_ver}\x1b[0m (missing from spec BuildRequires)")

    return "\n".join(lines)


def format_drift_short_summary(drifts: List[Dict]) -> str:
    """Returns a short single-line summary badge for detail card display."""
    if not drifts:
        return ""

    build_systems = list(dict.fromkeys(d.get("build_system", "meson") for d in drifts))
    sys_labels = []
    for bs in build_systems:
        if bs == "cmake":
            sys_labels.append("CMake")
        elif bs == "autotools":
            sys_labels.append("Autotools")
        elif bs == "python":
            sys_labels.append("Python")
        else:
            sys_labels.append("Meson")
    title = " & ".join(sys_labels)

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

    return f"⚠️ {title} drift: {', '.join(parts)}"


def fix_meson_drift(package_dir: str, on_log: Optional[any] = None) -> List[Dict]:
    """
    Audits and automatically updates .spec BuildRequires (including %{python_module ...}
    and associated %define macros) to match upstream build dependency requirements.
    Returns the list of fixed drift items.
    """
    log = on_log or (lambda msg: None)
    if not package_dir or not os.path.isdir(package_dir):
        return []

    drifts = audit_meson_drift(package_dir, bumps_only=True, include_unversioned=True)
    if not drifts:
        return []

    spec_files = [f for f in os.listdir(package_dir) if f.endswith(".spec")]
    if not spec_files:
        return []
    spec_path = os.path.join(package_dir, spec_files[0])

    try:
        with open(spec_path, "r", encoding="utf-8", errors="replace") as f:
            spec_lines = f.read().splitlines()
    except Exception as e:
        log(f"Error reading spec file: {e}")
        return []

    fixed_drifts = []
    lines = list(spec_lines)

    for d in drifts:
        pkg = d["package"]
        u_ver = d["upstream_version"]
        op = d.get("comparator") or ">="
        b_sys = d.get("build_system", "meson")

        if not u_ver or not re.match(r"^[0-9]", u_ver):
            continue

        if b_sys == "python":
            cands = [pkg, pkg.replace("-", "_")]
            mapped = PYTHON_PACKAGE_MAP.get(pkg, [])
            cands.extend(mapped)
        else:
            cands = [f"pkgconfig({pkg})", pkg]
            devel_name = DEVEL_PACKAGE_MAP.get(pkg)
            if devel_name:
                cands.append(devel_name)

        found = False
        for c in cands:
            for idx, line in enumerate(lines):
                clean = line.strip()
                if clean.startswith("#") or not clean.lower().startswith("buildrequires:"):
                    continue

                # 1. Check for %{python_module <c> [op ver]}
                m_pymod_line = re.search(
                    r"^(\s*BuildRequires:\s+%{python_module\s+" + re.escape(c) + r")(?:\s*([><=]+)\s*([^}]+))?(\s*}.*)$",
                    line,
                    re.IGNORECASE
                )
                if m_pymod_line:
                    prefix = m_pymod_line.group(1)
                    raw_val = (m_pymod_line.group(3) or "").strip()
                    suffix = m_pymod_line.group(4)

                    m_macro = re.match(r"^%\{?([a-zA-Z0-9_]+)\}?$", raw_val)
                    if m_macro:
                        macro_name = m_macro.group(1)
                        for def_idx, def_line in enumerate(lines):
                            m_def = re.match(
                                r"^(%(?:define|global)\s+" + re.escape(macro_name) + r"\s+)(.*)$",
                                def_line
                            )
                            if m_def:
                                lines[def_idx] = f"{m_def.group(1)}{u_ver}"
                                d["fixed_macro"] = macro_name
                                d["fixed_declaration"] = f"%{macro_name} ➔ {u_ver}"
                                fixed_drifts.append(d)
                                found = True
                                break
                    if not found:
                        lines[idx] = f"{prefix} {op} {u_ver}{suffix}"
                        d["fixed_declaration"] = f"%{{python_module {c} {op} {u_ver}}}"
                        fixed_drifts.append(d)
                        found = True
                    break

                # 2. Standard BuildRequires: <c> [op ver]
                m_req = re.search(
                    r"^BuildRequires:\s+" + re.escape(c) + r"(?:\s*([><=]+)\s*(\S+))?",
                    clean,
                    re.IGNORECASE
                )
                if m_req:
                    raw_val = m_req.group(2) or ""
                    m_macro = re.match(r"^%\{?([a-zA-Z0-9_]+)\}?$", raw_val)
                    if m_macro:
                        macro_name = m_macro.group(1)
                        for def_idx, def_line in enumerate(lines):
                            m_def = re.match(
                                r"^(%(?:define|global)\s+" + re.escape(macro_name) + r"\s+)(.*)$",
                                def_line
                            )
                            if m_def:
                                lines[def_idx] = f"{m_def.group(1)}{u_ver}"
                                d["fixed_macro"] = macro_name
                                d["fixed_declaration"] = f"%{macro_name} ➔ {u_ver}"
                                fixed_drifts.append(d)
                                found = True
                                break

                    if not found:
                        if raw_val:
                            lines[idx] = re.sub(
                                r"([><=]+\s*)" + re.escape(raw_val),
                                r"\g<1>" + u_ver,
                                line,
                                count=1
                            )
                        else:
                            lines[idx] = re.sub(r"(\s*(?:#.*)?)$", rf" {op} {u_ver}\g<1>", line, count=1)

                        d["fixed_declaration"] = f"{d.get('spec_name') or c} {op} {u_ver}"
                        fixed_drifts.append(d)
                        found = True
                    break
            if found:
                break

    if fixed_drifts:
        try:
            with open(spec_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            log(f"Synced {len(fixed_drifts)} build dependenc{'ies' if len(fixed_drifts) != 1 else 'y'} in {os.path.basename(spec_path)}")
        except Exception as e:
            log(f"Error writing updated spec file: {e}")
            return []

    return fixed_drifts


def record_drift_changelog(package_dir: str, fixed_drifts: List[Dict], on_log: Optional[any] = None) -> bool:
    """
    Records an automated openSUSE changelog entry for applied build dependency drift fixes.
    Dynamically names the build definition file(s).
    """
    log = on_log or (lambda msg: None)
    if not fixed_drifts:
        return False

    build_systems = list(dict.fromkeys(d.get("build_system", "meson") for d in fixed_drifts))
    file_names = []
    for bs in build_systems:
        if bs == "cmake":
            file_names.append("CMakeLists.txt")
        elif bs == "autotools":
            file_names.append("configure.ac")
        elif bs == "python":
            file_names.append("pyproject.toml")
        else:
            file_names.append("meson.build")

    import textwrap
    files_str = " and ".join(file_names)
    raw_text = f"Update version dependencies according to {files_str}."

    # Format strictly according to openSUSE 67-column changelog wrapping rules
    wrapper = textwrap.TextWrapper(
        width=67,
        initial_indent="- ",
        subsequent_indent="  ",
        break_long_words=False,
        break_on_hyphens=False
    )
    full_bullet = wrapper.fill(raw_text)
    # For osc vc -m (which prepends '- ' to the first line), pass message without leading '- '
    osc_msg = full_bullet[2:] if full_bullet.startswith("- ") else full_bullet

    # 1. Try osc vc first
    try:
        import subprocess
        res = subprocess.run(
            ["osc", "vc", "-m", osc_msg],
            cwd=package_dir,
            capture_output=True,
            text=True
        )
        if res.returncode == 0:
            log("Recorded changelog entry via osc vc")
            return True
    except Exception:
        pass

    # 2. Try changelog helper if available
    try:
        from upgrade.changelog import ChangelogManager
        changes_files = [f for f in os.listdir(package_dir) if f.endswith(".changes")]
        if not changes_files:
            return False
        changes_path = os.path.join(package_dir, changes_files[0])
        cm = ChangelogManager(changes_path)
        cm.prepend_entry([full_bullet])
        log(f"Recorded changelog entry in {changes_files[0]}")
        return True
    except Exception:
        # Fallback to direct prepending in .changes file
        try:
            import datetime
            now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%a %b %d %H:%M:%S UTC %Y")
            user_name = os.environ.get("USER", "maintainer")
            user_email = os.environ.get("MAIL", f"{user_name}@opensuse.org")

            entry = f"-------------------------------------------------------------------\n{now_str} - {user_email}\n\n{full_bullet}\n\n"
            changes_files = [f for f in os.listdir(package_dir) if f.endswith(".changes")]
            if changes_files:
                p = os.path.join(package_dir, changes_files[0])
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    old_c = f.read()
                with open(p, "w", encoding="utf-8") as f:
                    f.write(entry + old_c)
                log(f"Pre-pended changelog entry to {changes_files[0]}")
                return True
        except Exception:
            pass

    return False


# Aliases for generic multi-build-system usage
audit_dependency_drift = audit_meson_drift
fix_dependency_drift = fix_meson_drift
