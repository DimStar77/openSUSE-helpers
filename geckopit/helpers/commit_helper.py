#!/usr/bin/env python3
"""
openSUSE Packaging Git Commit Helper.
Modernizes gc.sh in Python with git-native file staging, automated whitespace
sanitization, and smart .changes commit message extraction.
"""

import glob
import os
import re
import subprocess
import tempfile
from typing import Optional, Tuple, List, Callable

TEXT_FILE_EXTENSIONS = (
    ".spec",
    ".changes",
    ".patch",
    ".diff",
    ".service",
    ".timer",
    ".desktop",
    ".xml",
    ".json",
    ".conf",
    ".sh",
)


def get_modified_and_untracked_files(package_dir: str) -> List[str]:
    """Returns relative paths of files that have local modifications or are untracked."""
    try:
        res_m = subprocess.run(
            ["git", "-C", package_dir, "diff", "--name-only"],
            capture_output=True, text=True, check=True
        ).stdout.splitlines()
        res_c = subprocess.run(
            ["git", "-C", package_dir, "diff", "--cached", "--name-only"],
            capture_output=True, text=True, check=True
        ).stdout.splitlines()
        res_u = subprocess.run(
            ["git", "-C", package_dir, "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, check=True
        ).stdout.splitlines()
        return list(set(res_m + res_c + res_u))
    except Exception:
        return []


def sanitize_trailing_whitespaces(package_dir: str, on_log: Optional[Callable[[str], None]] = None) -> List[str]:
    """
    Sanitizes and strips trailing whitespace from modified or newly added packaging text files
    before staging and committing. Does not touch unmodified files.
    """
    candidates = get_modified_and_untracked_files(package_dir)
    if not candidates and not os.path.exists(os.path.join(package_dir, ".git")):
        try:
            candidates = [f for f in os.listdir(package_dir) if os.path.isfile(os.path.join(package_dir, f))]
        except OSError:
            candidates = []

    sanitized = []
    for rel_path in candidates:
        if "/" in rel_path or rel_path.startswith("."):
            continue
        if rel_path.endswith(TEXT_FILE_EXTENSIONS) or rel_path == "_service":
            fpath = os.path.join(package_dir, rel_path)
            if not os.path.isfile(fpath):
                continue
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.readlines()
                has_trailing = any(line.rstrip("\r\n") != line.rstrip(" \t\r\n") for line in lines)
                if has_trailing:
                    clean_lines = [line.rstrip(" \t\r\n") + "\n" for line in lines]
                    with open(fpath, "w", encoding="utf-8") as fh:
                        fh.writelines(clean_lines)
                    sanitized.append(rel_path)
            except (OSError, UnicodeError):
                pass

    if sanitized and on_log:
        on_log(f"Sanitized trailing whitespace in: {', '.join(sanitized)}")
    return sanitized


def stage_package_files(package_dir: str, on_log: Optional[Callable[[str], None]] = None) -> Tuple[bool, str]:
    """
    Stages all modified/deleted files and top-level untracked packaging files,
    relying natively on git and .gitignore.
    """
    # 1. Stage tracked modifications and deletions (git add -u .)
    try:
        subprocess.run(
            ["git", "-C", package_dir, "add", "-u", "."],
            capture_output=True,
            check=True
        )
    except subprocess.CalledProcessError as e:
        return False, f"Failed to stage tracked modifications: {e.stderr.decode()}"

    # 2. Stage untracked files respecting .gitignore (git ls-files --others --exclude-standard)
    staged_untracked = []
    try:
        untracked = subprocess.run(
            ["git", "-C", package_dir, "ls-files", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            check=True
        ).stdout.splitlines()

        for entry in untracked:
            # Only stage regular files at the top-level of the package (skip subdirectories/clones)
            if "/" not in entry and not entry.startswith("."):
                full_path = os.path.join(package_dir, entry)
                if os.path.isfile(full_path):
                    subprocess.run(
                        ["git", "-C", package_dir, "add", entry],
                        capture_output=True,
                        check=True
                    )
                    staged_untracked.append(entry)
    except Exception as e:
        return False, f"Error staging untracked files: {e}"

    if staged_untracked and on_log:
        on_log(f"Staged new packaging files: {', '.join(staged_untracked)}")

    return True, "Files staged successfully"


def extract_latest_changes_entry(changes_content: str) -> Optional[str]:
    """Extracts the topmost changelog entry content between dividers, omitting author header."""
    blocks = re.split(r'^-{20,}\s*$', changes_content, flags=re.MULTILINE)
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        lines = block.splitlines()
        if len(lines) > 1:
            body_lines = lines[1:]
            while body_lines and not body_lines[0].strip():
                body_lines.pop(0)
            while body_lines and not body_lines[-1].strip():
                body_lines.pop()
            return "\n".join(body_lines)
    return None


def format_commit_message_from_entry(entry_text: str) -> Tuple[str, str]:
    """
    Extracts a concise, user-focused Git commit subject and body from the latest changelog entry.
    """
    lines = entry_text.splitlines()
    first_dash_idx = None
    for idx, l in enumerate(lines):
        if l.strip().startswith("- "):
            first_dash_idx = idx
            break

    if first_dash_idx is None:
        first_line = lines[0].strip() if lines else "Package update"
        body = "\n".join(lines[1:]).rstrip() if len(lines) > 1 else ""
        return first_line, body

    first_line = lines[first_dash_idx].strip()
    # Strip leading '- ' and trailing ':'
    subject = re.sub(r'^-+\s*', '', first_line).rstrip(':').strip()

    body_lines = lines[:first_dash_idx] + lines[first_dash_idx + 1:]
    while body_lines and not body_lines[0].strip():
        body_lines.pop(0)
    while body_lines and not body_lines[-1].strip():
        body_lines.pop()

    body = "\n".join(body_lines).rstrip()
    return subject, body


def execute_package_commit(
    package_dir: str = ".",
    interactive: bool = True,
    on_log: Optional[Callable[[str], None]] = None
) -> Tuple[bool, str]:
    """
    Full commit pipeline:
      1. Sanitizes trailing whitespace on modified/untracked files
      2. Stages modified and new packaging files (respecting .gitignore)
      3. Verifies .changes has staged modifications
      4. Extracts and formats Git commit subject and body
      5. Runs 'git commit -e -F' with pre-filled message in user's editor
    """
    log = on_log or (lambda msg: None)
    package_dir = os.path.abspath(package_dir)

    # 1. Whitespace sanitization on modified files
    sanitize_trailing_whitespaces(package_dir, on_log=log)

    # 2. Staging
    ok, msg = stage_package_files(package_dir, on_log=log)
    if not ok:
        return False, msg

    # 3. Pre-check: Ensure .changes file exists and has staged changes
    changes_files = glob.glob(os.path.join(package_dir, "*.changes"))
    if not changes_files:
        return False, "No .changes file found in package directory"

    changes_file = changes_files[0]
    changes_basename = os.path.basename(changes_file)

    try:
        diff_cached = subprocess.run(
            ["git", "-C", package_dir, "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            check=True
        ).stdout.splitlines()
    except subprocess.CalledProcessError as e:
        return False, f"Git status error: {e}"

    if not diff_cached:
        return False, "No staged changes to commit in this package"

    if changes_basename not in diff_cached:
        log(f"Warning: {changes_basename} has no staged changes (did you run 'osc vc'?)")

    # 4. Extract changelog entry and construct commit message
    with open(changes_file, "r", encoding="utf-8", errors="replace") as f:
        changes_content = f.read()

    entry = extract_latest_changes_entry(changes_content)
    if not entry:
        return False, f"Could not extract latest changelog entry from {changes_basename}"

    subject, body = format_commit_message_from_entry(entry)

    # 5. Commit Phase
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tf:
        tf.write(subject + "\n")
        if body:
            tf.write("\n" + body + "\n")
        tmp_msg_path = tf.name

    try:
        cmd = ["git", "-C", package_dir, "commit"]
        if interactive:
            cmd.extend(["-e", "-F", tmp_msg_path])
            res = subprocess.run(cmd)
        else:
            cmd.extend(["-F", tmp_msg_path])
            res = subprocess.run(cmd, capture_output=True, text=True)

        if res.returncode == 0:
            return True, f"Successfully committed: {subject}"
        else:
            return False, "Commit aborted or failed"
    finally:
        if os.path.isfile(tmp_msg_path):
            os.remove(tmp_msg_path)
