# openSUSE Tumbleweed Development Scripts

This repository contains a collection of helper scripts and automation tools designed for openSUSE Tumbleweed release managers, staging administrators, and package maintainers. These scripts automate routine operations in the Open Build Service (OBS), git staging workflows, Gitea repository management, and local packages updates.

---

## Prerequisites

To run these scripts, you will need the following CLI utilities installed and configured on your system:

*   **`osc`**: The Open Build Service command-line tool (authenticated with your OBS account).
*   **`git`**: For checking out, committing, and pushing sources to Gitea (`src.opensuse.org`).
*   **`xmlstarlet`**: Used for editing XML structure in `_service` configurations.
*   **`dialog`**: Used for interactive prompts and progress bars.
*   **`python3`** (with the `requests` library): Required for running OBS/Gitea synchronization audits.

---

## Scripts Directory

Below is an alphabetical reference explaining the purpose, syntax, and behaviors of each script.

---

### `accept_green_stagings.sh`

*   **Description:** Automates the daily staging acceptance check for `openSUSE:Factory` and `openSUSE:Factory:NonFree` when they pass openQA testing.
*   **Usage:**
    ```bash
    ./accept_green_stagings.sh
    ```
*   **How it works:**
    1.  Verifies `devel_update.sh` is in the `$PATH` to ensure development packages can be updated properly.
    2.  Attempts to lock the OBS staging area with `osc staging lock -m "Factory accept bot"`.
    3.  Checks if Tumbleweed/Factory has already been accepted today by comparing the current date (`YYYYMMDD`) to the `OSRT:ProductVersion` attribute.
    4.  Ensures that the current snapshot has migrated to openQA by checking the `OSRT:ToTestManagerStatus` attribute.
    5.  Clones the Factory package metadata repository from Gitea (`gitea@src.opensuse.org:openSUSE/Factory.git`).
    6.  Runs `devel_update.sh syncnewpackages` inside `Factory/pkgs/_meta`, commits, and pushes any new package metadata.
    7.  Clears supersede caches and lists existing ADI (Automated Dep Chain Integration) projects.
    8.  Accepts acceptable staging projects for `openSUSE:Factory:NonFree` first, followed by standard `openSUSE:Factory`.
    9.  Unlocks the staging area.

---

### `adi-remove-failures.sh`

*   **Description:** Scans an active ADI staging project, identifies packages that are failing (`F`) or unresolvable (`U`), and moves them out of that staging. This helps clear stuck ADI projects, letting passing packages proceed while failed ones are rescheduled to a new staging project.
*   **Usage:**
    ```bash
    ./adi-remove-failures.sh <ADI_PROJECT_NUMBER>
    ```
    *Example:* `./adi-remove-failures.sh 42` will process `openSUSE:Factory:Staging:adi:42`.
*   **How it works:**
    1.  Fetches build results with `osc prjresults openSUSE:Factory:Staging:adi:<n> -V`.
    2.  Uses `awk` to filter package names with failing or unresolvable flags.
    3.  Moves those packages out of the staging using `osc staging adi --move`.

---

### `check_obs_gitea.py`

*   **Description:** An auditing and cleanup script that reconciles OBS packages against repositories hosted in the Gitea repository pool. It finds OBS packages that lack a Gitea repository, and orphaned Gitea repositories that are no longer present in OBS.
*   **Usage:**
    ```bash
    python3 check_obs_gitea.py [options]
    ```
*   **Key Parameters:**
    *   `--remove-obsoletes`: Enables rename/removal actions on orphaned repositories (requires `GITEA_TOKEN` environment variable).
    *   `--dry`: Performs a global non-destructive dry-run simulation of any mutations.
    *   `--only [missing|orphans]`: Filters output to only show missing Gitea repos (`missing`) or orphaned Gitea repos (`orphans`).
    *   `--obs-project <project>`: Override the default OBS project (default: `openSUSE:Factory`).
    *   `--gitea-api-url <url>`: Override the Gitea API endpoint.
    *   `--target-branch <branch>`: The branch to audit and clean up (default: `factory`).
    *   `--rename-branch-to <name>`: Backup name for target branch on orphan removal (default: `factory-deleted`).
*   **How it works:**
    1.  Uses `osc ls` to obtain a list of active source packages in OBS.
    2.  Queries Gitea's organization repositories via API.
    3.  Identifies missing repositories (OBS packages not in Gitea).
    4.  Identifies orphaned repositories (Gitea repos containing target branch but not in OBS).
    5.  If `--remove-obsoletes` is active, it runs branch transitions (creates backup branch, moves default branch pointer if necessary, and deletes the obsolete target branch).

---

### `find-have-choice.sh`

*   **Description:** Quickly identifies packages in `openSUSE:Factory` experiencing "have choice" unresolvable build blocks.
*   **Usage:**
    ```bash
    ./find-have-choice.sh
    ```
*   **How it works:**
    1.  Queries unresolvable packages in the `x86_64` architecture for standard `openSUSE:Factory`.
    2.  Loops through each unresolvable package and queries its full verbose build log results via `osc r -v`.
    3.  Filters for the exact phrase "have choice" and prints out matching package names.

---

### `livecd-size.sh`

*   **Description:** Retrieves the size or artifact metadata of a specific Tumbleweed Live CD image build from OBS.
*   **Usage:**
    ```bash
    ./livecd-size.sh [image_name]
    ```
    *Example:* `./livecd-size.sh gnome` (default if parameter is omitted) or `./livecd-size.sh kde`.
*   **How it works:** Queries the OBS build endpoint for `/build/openSUSE:Factory:Live/images/x86_64/livecd-tumbleweed-$IMG` using `osc api`.

---

### `migrate_service.sh`

*   **Description:** Performs migrations on package source-service (`_service`) files, moving them from `.zst` compression to standard `.xz` compression, changing service runs to `manual` mode, updating spec files, and staging the resulting changes in git.
*   **Usage:** Run this script inside a package checkout directory that contains a `_service` configuration file.
    ```bash
    ./migrate_service.sh
    ```
*   **How it works:**
    1.  Uses `xmlstarlet` to configure `_service` parameters, setting `tar` and `recompress` compression modes to `manual` and compression level to `xz`.
    2.  Updates `cargo_vendor` compression setting to `xz` if present.
    3.  Modifies `.spec` file references from `.tar.zst` to `.tar.xz`.
    4.  Appends `osc-collab.*` to `.gitignore`.
    5.  Cleans up previously tracked `.tar.zst` and `.obscpio` files.
    6.  Runs local services with `osc service mr` to download and rebuild dependencies.
    7.  Creates a changelog entry (`osc vc`) indicating the compression migration, and stages changes to `git`.

---

### `obs_scm-update.sh`

*   **Description:** Updates the source revision configuration in a package's `_service` file, rebuilds the files locally, and generates a structured changelog entry by running a git diff on NEWS, meson, and other metadata if available.
*   **Usage:** Run inside a package checkout directory:
    ```bash
    ./obs_scm-update.sh [revision]
    ```
    *If `revision` is omitted, it defaults to `@PARENT_TAG@`.*
*   **How it works:**
    1.  Locates the package's local `_service` file and parses the git clone URL.
    2.  Replaces the revision value with the specified argument.
    3.  Runs `osc service mr` to fetch the new sources.
    4.  Enters the locally checked-out source directory and generates diffs for `NEWS`, `meson.build`, and `meson_options.txt`.
    5.  Extracts lines from the `NEWS` file and creates a pre-populated changelog entry via `osc vc`.

---

### `osc_clean_devel.py`

*   **Description:** Scans, identifies, and cleans up incorrect or redundant `<devel project="..." package="..."/>` tags from package metadata XML on the OBS server. Supports targeted single-package cleaning and project-wide batch cleanup (with a wildcard mode for projects like `openSUSE:Factory:RISCV`).
*   **Usage:**
    ```bash
    ./osc_clean_devel.py [global_options] <command> [args]
    ```
*   **Commands:**
    *   `list`: Scans and lists all packages with the specified or any devel project tags.
    *   `drop <package>`: Interactively verifies and removes the tag from a single package.
    *   `drop-all`: Scans, generates, and processes a batch cleanup for all matching packages.
*   **Key Parameters:**
    *   `--project <project>`: The target OBS project to scan (default: `openSUSE:Factory`).
    *   `--devel-project <devel>`: The incorrect devel project target to remove (default: `GNOME:Apps`).
    *   `--all-devel`: Matches and removes **any** `<devel>` tag regardless of target project (wildcard mode).
    *   `--dry-run`: Performs a trial run showing unified diffs of proposed changes without writing them back to OBS.
    *   `-y`, `--yes`: Bypasses all interactive confirmation prompts.
*   **How it works:**
    1.  Uses high-performance XPath queries (`devel/@project='...'` or wildcard tautology `devel/@project = devel/@project`) to query the OBS search API, locating matching packages instantly.
    2.  If `--all-devel` list is requested, retrieves associated package metadata in parallel using a `ThreadPoolExecutor` to display each package's actual devel project inline.
    3.  Parses metadata XML, surgically removing the target `<devel>` tag while preserving trailing whitespaces and formatting.
    4.  Presents a clean, colorized unified diff showing exact XML modifications.
    5.  Saves the corrected XML back to OBS via `osc.core.edit_meta` once approved.

---

### `print-obsolete-prjconf.py`

*   **Description:** Scans the `openSUSE:Factory` project configuration (`prjconf`) to detect obsolete or non-existent entries.
*   **Usage:**
    ```bash
    ./print-obsolete-prjconf.py
    ```
*   **How it works:**
    1.  Assembles a master list of all package binaries built in `openSUSE:Factory` and its architectures/ports (`:ARM`, `:PowerPC`, `:zSystems`).
    2.  Compares `Prefer:` package targets configured in the `prjconf` against existing packages, printing any targets that no longer exist.
    3.  Gathers active package sources in `openSUSE:Factory` and `openSUSE:Factory:NonFree`.
    4.  Compares `onlybuild:` and `excludebuild:` constraints in the `prjconf` against existing source lists, printing out entries that can be removed safely.

---

### `rebuild-dep-chain.sh`

*   **Description:** Triggers a rebuild for all packages that depend on a specified package in `openSUSE:Factory` and its main architecture ports, presenting a visual progress gauge.
*   **Usage:**
    ```bash
    ./rebuild-dep-chain.sh <package_name>
    ```
*   **How it works:**
    1.  Uses `osc whatdependson` to find all packages that depend on `<package_name>`.
    2.  Presents an interactive `dialog` box asking for permission to trigger the rebuilds.
    3.  Loops through each dependency and executes `osc rebuildpac` across standard and port projects (`:ARM`, `:LegacyX86`, `:PowerPC`, `:zSystems`).
    4.  Renders a real-time progress gauge on-screen.

---

### `Recover Staging.sh`

*   **Description:** Re-bootstraps a staging project after its binary packages have been wiped, aggregating essential dependencies (such as polkit, ovmf, java, and rust versions) so build jobs can resume.
*   **Usage:**
    ```bash
    ./"Recover Staging.sh" <STAGING_LETTER>
    ```
    *Example:* `./"Recover Staging.sh" L` will bootstrap staging area `openSUSE:Factory:Staging:L`.
*   **How it works:**
    1.  Copies standard basic `rpmlint-mini-AGGR` from `openSUSE:Factory:Rings:0-Bootstrap` to the target staging project.
    2.  Aggregates `polkit-default-privs` and `ovmf` from `openSUSE:Factory`.
    3.  Discovers and aggregates Rust versions from the `MinimalX` ring for both `x86_64` and `i586`.
    4.  Discovers and aggregates Java JDK versions from the `MinimalX` ring for both `x86_64` and `i586`.

---

### `sync-devel.sh`

*   **Description:** Synchronizes development package metadata on `src.opensuse.org`.
*   **Usage:**
    ```bash
    ./sync-devel.sh
    ```
*   **Environment Assumptions:**
    *   The script assumes a specific home folder setup and executes paths located under `~dimstar/`.
*   **How it works:**
    1.  Initializes an ephemeral `ssh-agent` and runs `ssh-add` to cache your key passphrase for the duration of the script.
    2.  Moves into the development packages git checkout located at `~/Documents/src.o.o/Factory/pkgs/_meta`.
    3.  Performs a `git pull` to fetch up-to-date states.
    4.  Runs the local helper `devel_update.sh sync`.
    5.  Runs local test suite runner `~dimstar/Downloads/test.sh`.
    6.  Audits changes using `git diff --quiet`. If any local differences exist, it commits them and triggers `git push`.
    7.  Kills the ephemeral `ssh-agent` on exit.

---

### `trigger-if-failed.sh`

*   **Description:** Monitors a package's build process, automatically triggering rebuild attempts if the state shifts to `failed` until it eventually succeeds.
*   **Usage:**
    *   *Standard Form:*
        ```bash
        ./trigger-if-failed.sh <PROJECT> <PACKAGE> <REPOSITORY> <ARCHITECTURE> [DELAY_IN_SECONDS]
        ```
    *   *URL Form (Copy and paste from the OBS build log URL directly):*
        ```bash
        ./trigger-if-failed.sh <PROJECT>/<PACKAGE>/<REPOSITORY>/<ARCHITECTURE> [DELAY_IN_SECONDS]
        ```
*   **How it works:**
    1.  Parses the target parameters.
    2.  Queries OBS build results with `osc results --no-multibuild`.
    3.  If status is `failed`, triggers `osc rebuildpac` and updates attempt counters.
    4.  Repeats checks after the designated delay (defaults to 60 seconds) until status reaches `succeeded*`.
    5.  Triggers a terminal alert beep on success.
