# pr_manage.py Manual

`pr_manage.py` is a utility for managing Pull Requests (PRs) within the `GNOME/_ObsPrj` repository on Gitea (typically `src.opensuse.org`). It is specifically designed to handle the complex workflow of grouping and managing multiple package submissions in the GNOME metaproject.

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

## Configuration

`pr_manage.py` automatically reads credentials from `~/.config/tea/config.yml`.

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

### `select <target-pr-id> <package1> [package2 ...]`
Groups individual package PRs into a single target PR.

- **Action**:
  - Finds open PRs matching the provided package names.
  - Ensures the target branch matches the target PR's branch.
  - Extracts reference tokens (e.g., `PR: GNOME/package!123`) from the source PRs.
  - Appends these tokens to the target PR's description.
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
  - Transfers all reference tokens from the source PR to the target PR.
  - Closes the source PR.

### `disintegrate <target-pr-id>`
Breaks a group PR back into its individual components.

- **Action**:
  - Reopens all PRs referenced in the target PR's description.
  - Removes the reference tokens from the target PR, effectively making it a standalone package PR again.

### `accept <target-pr-id>`
Signals approval for a PR to be merged into staging.

- **Action**: Posts a comment "merge ok" on the PR.
- **Effect**: This notifies the openSUSE staging workflow bots to process the merge.

---

## Workflow Overview

1. **Submit Packages**: Use other scripts (like `gnome-promote.sh`) to submit individual packages, creating multiple open PRs.
2. **Group Related Changes**: Use `pr_manage.py select` to pick one PR as the "anchor" and pull other related package PRs into it. This keeps the PR list clean and ensures they are tested together.
3. **Refine Group**: Use `unselect`, `combine`, or `disintegrate` if the grouping needs to change.
4. **Approve**: Once the group is ready and verified, use `pr_manage.py accept` to trigger the final merge.
