#!/usr/bin/env python3
"""
openSUSE Package Changelog Formatter Library.
Provides strict 67-column multi-level bullet wrapping and upstream NEWS/ChangeLog parsing.
Shared across all Geckopit upgrade engines and standalone packaging tools.
"""

import re
import textwrap
from typing import Optional, List, Tuple

CHANGELOG_WRAP_WIDTH = 67

def wrap_bullet(text: str, level: int = 1, width: int = CHANGELOG_WRAP_WIDTH) -> str:
    """
    Wraps bullet point text strictly at width characters with level-specific prefixes:
      Level 0: '- '       (continuation: '  ')
      Level 1: '  + '     (continuation: '    ')
      Level 2: '    - '   (continuation: '      ')
      Level 3: '      . ' (continuation: '        ')
    """
    if level == 0:
        prefix = "- "
        cont = "  "
    elif level == 1:
        prefix = "  + "
        cont = "    "
    elif level == 2:
        prefix = "    - "
        cont = "      "
    else:
        prefix = "      . "
        cont = "        "

    wrapper = textwrap.TextWrapper(
        width=width,
        initial_indent=prefix,
        subsequent_indent=cont,
        break_long_words=False,
        break_on_hyphens=False
    )
    return wrapper.fill(text)


def clean_trailing_issue_ref(text: str) -> str:
    """
    Strips parenthesized issue tracker references like (#6705, #6678) or (#6710) (#6715),
    while preserving CVE identifiers like (CVE-2026-87766).
    """
    pattern = r'\s*\((?:(?:[#!]|gh#|glgo#|bgo#)?\s*\d+[\s,]*)+\)'
    cleaned = re.sub(pattern, '', text)
    cleaned = re.sub(pattern, '', cleaned)
    return cleaned.strip()


def build_changelog_from_items(
    items: List[Tuple[int, str]],
    target_version: str,
    dropped_patches: Optional[List[str]] = None,
    has_translations: bool = False,
    width: int = CHANGELOG_WRAP_WIDTH
) -> str:
    """
    Formats a structured list of (level, text) items into a standardized openSUSE changelog entry.
    Useful for any upgrade engine (OBS SCM, PyPI, Cargo, Git release notes).
    """
    entries = []
    # Level 0 Header
    entries.append(wrap_bullet(f"Update to version {target_version}:", level=0, width=width))

    for level, text in items:
        if text.strip():
            entries.append(wrap_bullet(text.strip(), level=level, width=width))

    if has_translations:
        entries.append(wrap_bullet("Updated translations.", level=1, width=width))

    if dropped_patches:
        for p in dropped_patches:
            entries.append(wrap_bullet(f"Drop {p}: fixed upstream.", level=0, width=width))

    return "\n".join(entries)


def format_changelog_entry(
    diff_text: str,
    target_version: str,
    dropped_patches: Optional[List[str]] = None,
    width: int = CHANGELOG_WRAP_WIDTH
) -> str:
    """
    Parses an upstream NEWS/ChangeLog diff and produces openSUSE-compliant
    changelog formatting wrapped strictly at width characters with multi-level hierarchy.
    """
    raw_lines = []
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            raw_lines.append(line[1:])

    re_release_header = re.compile(r'^(changes|overview\s+of\s+changes|release)\s+in\s+.*', re.IGNORECASE)
    re_ver_header = re.compile(r'^(version\s+)?v?\d+[\d._rcbetalpha+-]*(\s*[-–(].*)?$', re.IGNORECASE)
    re_date_header = re.compile(r'^(released|date):\s*.*', re.IGNORECASE)
    re_underline = re.compile(r'^[=\-~_]{2,}$')
    re_bullet = re.compile(r'^(\s*)([*•\-+])\s+(.*)')

    filtered_lines = []
    in_translations = False
    has_translations = False

    for line in raw_lines:
        s = line.strip()
        if not s:
            in_translations = False
            continue
        if re_release_header.match(s) or re_ver_header.match(s) or re_date_header.match(s) or re_underline.match(s):
            continue

        # Standalone translation section header (e.g. 'Translations:' or 'Translation updates:')
        if not re_bullet.match(line) and (re.match(r'^(translation\s+updates?|translations?):?$', s, re.IGNORECASE) or s.endswith("translation:")):
            has_translations = True
            in_translations = True
            continue

        if in_translations:
            if (s.endswith(":") and not re_bullet.match(line)) or re_ver_header.match(s) or re_release_header.match(s):
                in_translations = False
            else:
                continue

        # Skip raw author names or committer signatures (e.g. Author Name <email>:)
        if re.match(r'^[A-Z][a-zA-Z\s.-]+<[^>]+>:$', s):
            continue

        filtered_lines.append(line)

    sections: List[Tuple[Optional[str], List[Tuple[int, str]]]] = []
    current_header: Optional[str] = None
    current_bullets: List[Tuple[int, str]] = []
    current_parent_bullet: Optional[str] = None

    i = 0
    n = len(filtered_lines)
    while i < n:
        line = filtered_lines[i]
        s = line.strip()
        m = re_bullet.match(line)

        if not m:
            is_header = False
            if s.endswith(":"):
                is_header = True
            elif i + 1 < n and re_bullet.match(filtered_lines[i + 1]):
                is_header = True

            if is_header:
                if current_bullets or current_header:
                    sections.append((current_header, current_bullets))
                    current_bullets = []
                current_header = s.rstrip(":").strip()
                current_parent_bullet = None
                i += 1
                continue
            else:
                if current_bullets:
                    sub_lvl, prev_txt = current_bullets[-1]
                    current_bullets[-1] = (sub_lvl, prev_txt + " " + s)
                i += 1
                continue
        else:
            indent_spaces = len(m.group(1))
            bullet_char = m.group(2)
            bullet_text = m.group(3).strip()

            i += 1
            while i < n:
                next_line = filtered_lines[i]
                next_s = next_line.strip()
                if re_bullet.match(next_line):
                    break
                if (next_s.endswith(":") and not re_bullet.match(next_line)) or (
                    i + 1 < n and re_bullet.match(filtered_lines[i + 1]) and not re_bullet.match(next_line)
                ):
                    if len(next_line) - len(next_line.lstrip()) > indent_spaces:
                        bullet_text += " " + next_s
                        i += 1
                        continue
                    break
                bullet_text += " " + next_s
                i += 1

            # Check if this entire bullet was a translation update (including its multi-line continuations)
            if re.match(r'^(translation\s+updates?|translations?):?', bullet_text, re.IGNORECASE) or re.search(r'updated?\s+.*translations?', bullet_text, re.IGNORECASE):
                has_translations = True
                continue

            bullet_text = clean_trailing_issue_ref(bullet_text)

            sub_level = 1
            if current_header:
                if bullet_char == "-" and indent_spaces >= 2 and current_parent_bullet:
                    sub_level = 2
                elif bullet_text.endswith(":"):
                    sub_level = 1
                    current_parent_bullet = bullet_text
                else:
                    sub_level = 1
                    current_parent_bullet = None
            else:
                if bullet_text.endswith(":"):
                    current_parent_bullet = bullet_text
                elif indent_spaces >= 2 and current_parent_bullet:
                    sub_level = 2

            current_bullets.append((sub_level, bullet_text))

    if current_header or current_bullets:
        sections.append((current_header, current_bullets))

    structured_items: List[Tuple[int, str]] = []
    for header, bullets in sections:
        if header and len(bullets) == 1 and bullets[0][0] == 1:
            _, b_txt = bullets[0]
            if b_txt:
                b_txt = b_txt[0].upper() + b_txt[1:]
            header_cap = header[0].upper() + header[1:] if header else ""
            structured_items.append((1, f"{header_cap}: {b_txt}"))
        elif header:
            header_cap = header[0].upper() + header[1:] if header else ""
            structured_items.append((1, f"{header_cap}:"))
            for sub_lvl, b_txt in bullets:
                lvl = 3 if sub_lvl == 2 else 2
                if lvl != 3 and b_txt:
                    b_txt = b_txt[0].upper() + b_txt[1:]
                structured_items.append((lvl, b_txt))
        else:
            for sub_lvl, b_txt in bullets:
                lvl = 2 if sub_lvl == 2 else 1
                if lvl != 3 and b_txt:
                    b_txt = b_txt[0].upper() + b_txt[1:]
                structured_items.append((lvl, b_txt))

    if not structured_items and not has_translations:
        structured_items.append((1, "Misc. bug fixes and cleanups."))

    return build_changelog_from_items(
        structured_items,
        target_version=target_version,
        dropped_patches=dropped_patches,
        has_translations=has_translations,
        width=width
    )


def remove_patch_from_spec(spec_content: str, patch_name: str) -> str:
    """Removes a dropped patch declaration and its preceding comments from spec file content."""
    lines = spec_content.splitlines(keepends=True)
    new_lines = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if re.search(r'^\s*Patch\d*:\s*.*' + re.escape(patch_name), line, re.IGNORECASE):
            while new_lines and (
                re.match(r'^\s*#\s*(PATCH|GLGO|BGO|FIX|FEATURE|\bhttps?://)', new_lines[-1], re.IGNORECASE)
                or new_lines[-1].strip() == ""
            ):
                if new_lines[-1].strip() == "" and len(new_lines) > 1 and not re.match(r'^\s*#', new_lines[-2]):
                    break
                new_lines.pop()
            i += 1
            continue
        new_lines.append(line)
        i += 1
    return "".join(new_lines)
