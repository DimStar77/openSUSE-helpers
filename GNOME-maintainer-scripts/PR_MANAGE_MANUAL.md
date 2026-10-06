# pr_manage.py Manual

`pr_manage.py` (and the `pr-manage` CLI/TUI) is a multi-workspace utility for managing and grouping forwarded Pull Requests (PRs) across any openSUSE metaproject on Gitea (such as `GNOME/_ObsPrj`, `KDE/_ObsPrj`, or `openSUSE/Factory`).

## Table of Contents
1. [Installation & Requirements](#installation--requirements)
2. [Configuration](#configuration)
3. [General Usage](#general-usage)
4. [Commands](#commands)
   - [list](#list)
   - [select](#select)
   - [unselect](#unselect)
   - [combine](#combine)
   - [disintegrate](#disintegrate)
   - [accept](#accept)
5. [Workflow Overview](#workflow-overview)

---

## Installation & Requirements

### Dependencies
The script requires Python 3 and the following libraries:
- `pyyaml`: For parsing the `tea` configuration.
- `colorama`: For colorized terminal output.

You can install these via pip:
```bash
pip install pyyaml colorama
```

### External Tools
The script relies on the configuration from the [tea](https://gitea.com/gitea/tea) CLI tool for Gitea authentication.

---


## Multi-Workspace Resolution & Permission Guards

`pr-manage` is completely workspace-aware and dynamically targets any valid metaproject repository without hardcoded assumptions:

1. **Resolution Hierarchy**:
   - **CLI Flag**: `--repo <owner/repo>` or `-R <owner/repo>` (e.g. `pr-manage -R openSUSE/Factory tui`).
   - **Environment Variable**: `GITEA_REPO=<owner/repo>`.
   - **Local Git Remote**: Automatically interrogates `git remote get-url origin` if executed inside a checkout (e.g. `GNOME/_ObsPrj` or `openSUSE/Factory`).
   - **Project Configuration**: Reads `GitProjectName` from local `workflow.config`.
   - **Last-Used Workspace**: Automatically remembers the last targeted workspace in `~/.config/pr-manage.json` and launches directly into it.
   - **Default Fallback**: Defaults to `GNOME/_ObsPrj`.

2. **Dynamic Workspace Discovery (`[W]`)**:
   - Scans user organizations on Gitea (e.g. `GNOME`, `KDE`) and common public metaprojects.
   - **Validates Existence**: Queries Gitea for repository existence, automatically filtering out non-existent repositories (preventing 404s like `KDE/_ObsPrj`).
   - Caches recently accessed valid workspaces in `~/.cache/pr-manage/recent_workspaces.json`.

3. **Proactive Permission Guards**:
   - Automatically inspects the user's repository permissions (`admin`, `push`, `pull`).
   - **Read-Only Mode (`[🔒 READ-ONLY]`)**: When targeting a repository where the user does not have push/maintainer rights (e.g. `openSUSE/Factory` for general contributors):
     - Displays `[🔒 READ-ONLY]` in the top bar.
     - Proactively blocks mutating actions (`Add`, `Remove`, `Move`, `Rename`, `Combine`, `Disintegrate`) with clean warning toasts rather than failing with raw HTTP 403 Forbidden errors.

## Configuration & Workspace Persistence

`pr-manage` persists user-configured workspaces and the active workspace in `~/.config/pr-manage.json` (mirroring Geckopit's configuration architecture). Whenever you switch workspaces via `[W]` or pass `-R <repo>`, it is saved and automatically used on subsequent launches.

Authentication credentials are automatically read from `~/.config/tea/config.yml`.

1. **Setup tea**: If you haven't already, install `tea` and add your Gitea login:
   ```bash
   tea login add --name OBS --url https://src.opensuse.org --token <YOUR_TOKEN>
   ```
2. **Profile Selection**: The script looks for a profile named `OBS`. If not found, it falls back to the profile marked as `default`.

---

## General Usage

The script is typically invoked as follows:
```bash
python3 pr_manage.py <command> [arguments]
```

*Note: Depending on your environment, you might have it aliased or symlinked as `pr-manage`.*

---

## Commands

### `list [branch]`
Lists open PRs in `GNOME/_ObsPrj`.

- **Arguments**:
  - `branch` (Optional): Filter by target branch (e.g., `factory`, `next`).
- **Output**:
  - Index (PR ID)
  - Target Branch (Color-coded: Green for `factory`, Cyan for `next`, Yellow for others)
  - Package / Title: Shows the "host" package (marked with ★) and any additional peer packages in the group.

### `tui`
Launches the interactive 3-column Terminal User Interface (TUI) for visual queue management.

- **Layout**:
  - **Column 1 (Forwards & Groups)**: Lists all active forwarded PRs on the metaproject (both multi-package groups with package counts and single-package PR forwards). Any item can be selected, combined, or approved with 'o' ('merge ok').
  - **Column 2 (Members)**: Displays all packages currently inside the focused group or single PR forward, marking the host package (★).
  - **Asynchronous OBS Build Status**: Live OBS staging project build results are loaded asynchronously in background worker threads, displaying a temporary `⚡ Checking...` indicator so cursor navigation remains 100% fluid and non-blocking.
  - **Column 3 (Ungrouped Queue)**: Searchable list of standalone single-package PRs waiting to be grouped.
- **Keybindings**:
  - `Tab` / `1, 2, 3`: Switch active column.
  - `Home` / `End` (or `g` / `G`): Jump directly to the top / bottom of the active list.
  - `Enter` / `i`: Inspect highlighted package: view PR details & unified syntax-colored diff / changelog.
  - `l` / `L`: View OBS build failure log for highlighted package or group in an interactive scrollable viewer with error syntax highlighting.
  - `Space`: Toggle multi-selection mark (`[✓]`) on the highlighted item.
  - `*`: Mark all visible items in the active column.
  - `_`: Deselect all items in the active column.
  - `I`: Invert selection marks in the active column.
  - `A` / `a`: Batch add marked (or highlighted) ungrouped packages into active group.
  - `D` / `d` / `u`: Batch remove marked (or highlighted) packages from active group.
  - `v` / `m`: Move marked package(s) directly to another group via destination selection modal.
  - `e`: Inline rename active group PR title in Gitea.
  - `c` / `C`: Batch combine marked PRs in Column 1 (prompts with modal to pick which marked PR absorbs the others), or combines a single PR into active group.
  - `o`: Signal staging merge approval (`merge ok`) with pre-merge OBS failure guard.
  - `O` / `!`: Launch interactive Out-of-Sync / Orphan Audit triage modal.
  - `w` / `x`: Open highlighted PR in web browser (`xdg-open`).
  - `W`: Switch metaproject workspace on the fly (`GNOME/_ObsPrj`, `KDE/_ObsPrj`, `openSUSE/Factory`).
  - `d`: Disintegrate group back to standalone PRs.
  - `f` / `b`: Cycle branch filter (`all` / `factory` / `next`).
  - `/`: Search / filter ungrouped queue.
  - `r`: Refresh live state from Gitea and OBS.
  - `q`: Quit TUI.

### `gui`
Launches the modern GTK4 / Libadwaita graphical staging manager interface (`pr-manage gui` or standalone `pr-manage-gui`).

- **Architecture**: Powered by the headless `StagingService` backend, featuring a responsive Libadwaita layout:
  - **Sidebar**: Staging groups and standalone forwards with live package counts, branch pills, search, and checkbox multi-selection for batch combination.
  - **Color-Coded OBS Status Lines**: Sidebar items are dynamically styled once OBS build states are queried: **soft green** background with green left border for successful builds, **light blue** for in-progress builds, and **soft red** for failed builds (remaining neutral grey until queried).
  - **Header Card**: In-place PR title renaming (`document-edit-symbolic`), target branch pills, host package indicator (★), live OBS build status pill (`Built`, `Building`, `Failed`), and an unambiguous active approval button (`✓ Accept #<id>`).
  - **Batch vs Active Separation**: Checkboxes in the sidebar dynamically reveal a dedicated batch action bar (`⎘ Combine (N)`, `✓ Accept (N)`, and clear `✕`) for marked sets, keeping multi-selection distinct from the focused group.
  - **Interactive Diff Viewer**: Built with `GtkSourceView 5` displaying syntax-highlighted git diffs and changelogs with line numbers and one-click clipboard export.
  - **In-App OBS Build Log Inspector**: Dedicated `📋 Build Log` button in the header card and per-package `🔴 Log (<archs>)` buttons for failing group members to instantly inspect build failure logs without switching to the terminal.
  - **Dual Staging Work Area**:
    - **Group Members Panel**: Searchable list of bundled packages with upstream PR numbers, instant diff inspection, and single/batch removal.
    - **Ungrouped Staging Queue**: Searchable list of standalone package PRs with one-click addition into the active group or single-PR approval (`merge ok`).
  - **Out-of-Sync Orphan Auditor**: Integrated dialog to triage unforwarded package PRs with one-click child PR reopening or group adoption.
  - **Workspace & Permission Awareness**: HeaderBar dropdown for switching staging repositories with permission tags (`[Admin]`, `[Maintainer]`, `[Read-Only]`) and a persistent notification banner when operating in Read-Only mode.

### `audit [branch]`
Audits all open package PRs across the organization to detect out-of-sync / orphaned PRs (package PRs that have no active forward PR in staging).

- **Arguments**:
  - `branch` (Optional): Filter by target branch (e.g., `factory`, `next`).
- **Output**:
  - Reports total organization open PRs vs. staging tracked PRs.
  - Formatted table of out-of-sync packages detailing package name, PR ID, author, target branch, and historical forward PR ID/state.
  - Remediation instructions for both CLI and interactive TUI.

### `select <target-pr-id> <package1> [package2 ...]`
Groups individual package PRs into a single target PR.

- **Action**:
  - Finds open PRs matching the provided package names (using exact head ref, token, or title matching).
  - Ensures the target branch matches the target PR's branch.
  - **Uniqueness Guard**: Verifies that each package name only exists once. If a package is already present in the target group, it is safely skipped with a duplicate conflict warning.
  - Extracts reference tokens (e.g., `PR: GNOME/package!123`) from the source PRs.
  - Appends these tokens to the target PR's description (preserving the PR title intact so staging managers can assign meaningful group names like 'GNOME 51.0').
  - **Closes** the individual package PRs.
- **Use Case**: Consolidating multiple related package updates into one staging request.

### `unselect <target-pr-id> <package-name>`
Removes a package from a grouped PR.

- **Action**:
  - Removes the package's reference token from the target PR's description.
  - **Reopens** the original package PR.
- **Restrictions**: You cannot unselect the "host" package (the package that owns the target PR). To undo the entire group, use `disintegrate`.

### `combine <target-pr-id> <source-pr-id>`
Merges one group or package PR into another group PR.

- **Action**:
  - **Collision Guard**: Pre-scans both PRs for overlapping submodules. If any package exists in both PRs, the combine operation is immediately aborted with a detailed conflict report, guaranteeing that two PRs for the same submodule cannot be grouped together.
  - Transfers all reference tokens from the source PR to the target PR (preserving the target PR's title intact).
  - Closes the source PR.

### `disintegrate <target-pr-id>`
Breaks a group PR back into its individual components.

- **Action**:
  - Reopens all PRs referenced in the target PR's description.
  - Removes the reference tokens from the target PR, effectively making it a standalone package PR again.

### `accept <target-pr-id>`
Signals approval for a PR to be merged into staging with pre-flight safety guards.

- **Pre-Flight Guards**:
  - **Git Merge Conflict Guard**: Queries Gitea's `mergeable` status. If the PR has Git merge conflicts with the target base branch (`mergeable: false`), approval is strictly blocked to prevent bot merge failures downstream.
  - **Multi-Architecture OBS Guard**: Queries OBS build results across **all target architectures** (including `x86_64`, `aarch64`, `s390x`, `ppc64le`, `i586`). If any package failed or is broken on ANY architecture, approval is blocked and all failing packages with their specific architectures (e.g. `gdm [aarch64, s390x]`) are reported.
- **Action**: Posts a comment "merge ok" on the PR once all safety checks pass.
- **Effect**: This notifies the openSUSE staging workflow bots to process the merge.

---

## Workflow Overview

1. **Submit Packages**: Use Geckopit (GUI / `geckopit-cli --forward`) or legacy scripts (like `gnome-promote.sh`) to submit individual packages, creating multiple open PRs.
2. **Group Related Changes**: Use `pr_manage.py select` to pick one PR as the "anchor" and pull other related package PRs into it. This keeps the PR list clean and ensures they are tested together.
3. **Refine Group**: Use `unselect`, `combine`, or `disintegrate` if the grouping needs to change.
4. **Approve**: Once the group is ready and verified, use `pr_manage.py accept` to trigger the final merge.
