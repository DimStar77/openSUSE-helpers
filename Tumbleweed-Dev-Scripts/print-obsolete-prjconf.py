#!/usr/bin/env python3
# Script downloads openSUSE:Factory prjconf, checks for obsolete entries,
# and modifies the stored file directly, commenting out or removing
# obsolete Prefer: and onlybuild/excludebuild entries.

import os
import re
import sys
import shutil
import tempfile
import subprocess
import concurrent.futures

PRJCONF = "openSUSE:Factory.prjconf"

# Packages that are known to be special or bootstrapped and should never be marked as obsolete
PRESERVED_PACKAGES = {"rpmlint-mini-AGGR"}

def fetch_binaries(port):
    project = f"openSUSE:Factory{port}"
    cmd = ["osc", "ls", "-b", project, "-r", "standard"]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]
    except subprocess.CalledProcessError as e:
        print(f"Error fetching binaries for {project}: {e.stderr}", file=sys.stderr)
        return []

def fetch_sources(project):
    cmd = ["osc", "prjresults", "--show-excluded", project, "-a", "x86_64", "-r", "standard", "-V"]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        sources = []
        for line in res.stdout.splitlines():
            if line and line[0] in ".FUf%bsSx":
                parts = line.split()
                if len(parts) >= 2:
                    sources.append(parts[1])
        return sources
    except subprocess.CalledProcessError as e:
        print(f"Error fetching sources for {project}: {e.stderr}", file=sys.stderr)
        return []

def clean_package_name(word):
    # Remove leading minus
    if word.startswith("-"):
        word = word[1:]
    # Remove project prefix (anything before the last colon)
    if ":" in word:
        word = word.split(":")[-1]
    return word

def main():
    # 1. Fetch current prjconf
    print(f"Downloading openSUSE:Factory prjconf to {PRJCONF}...")
    try:
        with open(PRJCONF, "w") as f:
            subprocess.run(["osc", "meta", "prjconf", "openSUSE:Factory"], stdout=f, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Failed to download prjconf: {e}", file=sys.stderr)
        sys.exit(1)

    # 2. Fetch active packages in parallel
    print("Fetching active packages from OBS...")
    ports = ["", ":ARM", ":PowerPC", ":zSystems"]
    projects = ["openSUSE:Factory", "openSUSE:Factory:NonFree"]

    all_packages = set()
    all_sources = set()

    with concurrent.futures.ThreadPoolExecutor() as executor:
        # Query binaries and sources concurrently
        pkg_futures = executor.map(fetch_binaries, ports)
        src_futures = executor.map(fetch_sources, projects)

        for pkgs in pkg_futures:
            all_packages.update(pkgs)
        for srcs in src_futures:
            all_sources.update(srcs)

    # Map binary package names and filenames for robust checking
    existing_binary_pkgs = set()
    existing_binary_filenames = set()
    for line in all_packages:
        parts = line.split()
        if len(parts) > 1:
            existing_binary_pkgs.add(parts[-1])
        if parts:
            filename = parts[0]
            if filename.endswith(".rpm"):
                filename = filename[:-4]
            existing_binary_filenames.add(filename)

    def binary_package_exists(pkg):
        if pkg in existing_binary_pkgs:
            return True
        # Check if filename is exactly pkg, or starts with 'pkg-' followed by a version digit
        prefix = f"{pkg}-"
        for fname in existing_binary_filenames:
            if fname == pkg:
                return True
            if fname.startswith(prefix) and len(fname) > len(prefix) and fname[len(prefix)].isdigit():
                return True
        return False

    # 3. Identify obsolete packages
    obsolete_prefers = set()
    obsolete_buildflags = set()

    prefer_pattern = re.compile(r"^[ \t]*Prefer:(.*)")
    buildflags_pattern = re.compile(r"^[ \t]*BuildFlags:[ \t]*(onlybuild|excludebuild):(.*)")

    with open(PRJCONF, "r") as f:
        for line in f:
            line_stripped = line.strip()
            m_prefer = prefer_pattern.match(line_stripped)
            if m_prefer:
                for word in m_prefer.group(1).split():
                    clean = clean_package_name(word)
                    if "%" in clean or clean in PRESERVED_PACKAGES:
                        continue
                    if not binary_package_exists(clean):
                        obsolete_prefers.add(clean)

            m_bf = buildflags_pattern.match(line_stripped)
            if m_bf:
                for word in m_bf.group(2).split():
                    if "%" in word or word in PRESERVED_PACKAGES:
                        continue
                    if word not in all_sources:
                        obsolete_buildflags.add(word)

    if not obsolete_prefers and not obsolete_buildflags:
        print(f"No obsolete entries found in {PRJCONF}.")
        return

    print("### Found obsolete Prefer: packages:")
    for pkg in sorted(obsolete_prefers):
        print(f"  - {pkg}")

    print("### Found obsolete BuildFlags: packages:")
    for pkg in sorted(obsolete_buildflags):
        print(f"  - {pkg}")

    # 4. Modify the file on disk
    print(f"\nModifying {PRJCONF} directly...")
    fd, orig_path = tempfile.mkstemp(suffix="-openSUSE:Factory.prjconf.orig")
    os.close(fd)
    shutil.move(PRJCONF, orig_path)

    prefer_line_re = re.compile(r"^([ \t]*Prefer:)([ \t]*)(.*)")
    buildflags_line_re = re.compile(r"^([ \t]*BuildFlags:[ \t]*(onlybuild|excludebuild):)([ \t]*)(.*)")

    with open(orig_path, "r") as f_in, open(PRJCONF, "w") as f_out:
        for line in f_in:
            line_stripped = line.rstrip("\n")

            # Process Prefer: lines
            m_prefer = prefer_line_re.match(line_stripped)
            if m_prefer:
                prefix = m_prefer.group(1)
                whitespace = m_prefer.group(2)
                pkgs_part = m_prefer.group(3)

                words = pkgs_part.split()
                remaining_words = []
                for word in words:
                    clean = clean_package_name(word)
                    if clean in obsolete_prefers:
                        print(f"  [Prefer] Removing obsolete: {word}", file=sys.stderr)
                    else:
                        remaining_words.append(word)

                if remaining_words:
                    new_line = prefix + whitespace + " ".join(remaining_words)
                    f_out.write(new_line + "\n")
                else:
                    f_out.write(f"# {line_stripped}\n")

            # Process BuildFlags: lines
            elif m_bf := buildflags_line_re.match(line_stripped):
                prefix = m_bf.group(1)
                whitespace = m_bf.group(3)
                pkgs_part = m_bf.group(4)

                words = pkgs_part.split()
                remaining_words = []
                for word in words:
                    if word in obsolete_buildflags:
                        print(f"  [BuildFlags] Removing obsolete: {word}", file=sys.stderr)
                    else:
                        remaining_words.append(word)

                if remaining_words:
                    new_line = prefix + whitespace + " ".join(remaining_words)
                    f_out.write(new_line + "\n")
                else:
                    f_out.write(f"# {line_stripped}\n")

            else:
                f_out.write(line)

    # 5. Display diff and instructions
    print(f"\nSuccessfully updated {PRJCONF}!")
    print("You can review the diff using:")
    print(f"  git diff --no-index \"{orig_path}\" \"{PRJCONF}\"")
    print("\nTo push the updated project configuration to OBS, run:")
    print(f"  osc meta prjconf openSUSE:Factory -F \"{PRJCONF}\"")

if __name__ == "__main__":
    main()
