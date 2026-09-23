# openSUSE GNOME Maintainer Workflows & Automation Suite

Outlining workflows to maintain a metaproject with 500+ submodules and multiple branches (factory and next), where branches drift apart with which submodules they contain (making traditional git checkouts impractical).

---

## 🚀 Active Superproject Infrastructure Scripts

### `gnome-clone.sh`
Clones the entire set of packages of the GNOME organization onto disk. The starting point is `_ObsPrj` (the OBS Project definition, referencing all submodules/packages for the factory branch).

Because openSUSE GNOME maintains multiple branches for `GNOME:Factory` and `GNOME:Next`, the script initializes a linked git worktree (`GNOME:Next`) directly adjacent to the initial `GNOME` checkout, targeting the `next` branch. This shares the underlying `.git` object store, saving gigabytes of disk space and bandwidth.

### `gnome-sync.sh`
Checks the `GNOME` and `GNOME:Next` superproject checkouts to see if submodules on `src.opensuse.org` have moved forward and synchronizes modules in need.

Operates as a high-performance bulk maintenance sync (`git submodule sync`, `git submodule update --remote --merge --jobs 10`, and `git clean -dff`), coping cleanly with submodules appearing and disappearing.

### `pr_manage.py`
Manages Pull Requests (PRs) in the `GNOME/_ObsPrj` repository. Allows grouping multiple package PRs into a single group PR, unselecting packages, and signaling approval for staging.
See [PR_MANAGE_MANUAL.md](PR_MANAGE_MANUAL.md) for detailed usage.

---

## ⚡ Modern Packaging Cockpit & CLI (`geckopit` / `geckopit-cli`)

Daily package release engineering, downstream synchronization, upstream version monitoring, and commit workflows are powered by the **Geckopit Suite** (`geckopit/` in this repository):

* **Interactive GUI Cockpit (`geckopit`)**: Complete visual dashboard with real-time multi-branch tracking, embedded terminal tabs, 1-click pull/push/merge actions, and automated Gitea PR prefilling from `.changes` diffs.
* **CLI Tool (`geckopit-cli`)**:
  * **Single Package Mode** (when inside a package directory or targeting a package):
    * `geckopit-cli`: Detailed single-package dashboard with worktree alignment, downstream sync, upstream release comparisons, and actionable recommendations.
    * `geckopit-cli --upgrade [REV]` (`-u`): Headless upgrade engine (`_service` bumping, `osc service mr`, strict 67-column changelog generation, patch dropping, and spec bumping).
    * `geckopit-cli --commit` (`-c`): Modernized commit helper with automated whitespace sanitization, gitignore-respecting staging, and `$EDITOR` pre-filling.
  * **Multi-Package Batch Mode** (in workspace root):
    * `geckopit-cli`: Parallel downstream sync audit (pool drift & catch-up merges across 50 threads).
    * `geckopit-cli --version` (`-v`): Parallel upstream release monitor querying release-monitoring.org.
    * `geckopit-cli --forward` (`-f`): Forwarding audit identifying branches ready to be merged to stable.

---

## 📦 Deprecated Legacy Scripts

The following standalone scripts are superseded by the Geckopit suite. For backwards compatibility and muscle memory, they contain transparent delegation shims that automatically defer to `geckopit-cli` when installed in `$PATH`, falling back to their legacy implementations otherwise:

| Legacy Script | Modern Equivalent | Description |
| :--- | :--- | :--- |
| `gnome-audit.sh` | `geckopit-cli --forward --pr` | Audits drift between `factory` and `next`, displaying commit deltas and active Gitea PR status. |
| `gnome-catchup.sh` | `geckopit-cli` (default/`--todo`) or GUI Track 3 (*Needs Merge*) | Identifies packages on `next` that have fallen behind `factory` and need catch-up merges. |
| `gnome-promote.sh` | `geckopit-cli --forward` or GUI Track 4 (*Forwardable*) | Lists packages on `next` ready to be forwarded to `factory` and surfaces open PRs. |
| `gnome-pool-audit.sh` | `geckopit-cli` (default) or GUI Track 1/2 (*Gitea Pool*) | Audits local `factory` packages against the central openSUSE source pool. |
| `gc.sh` | `geckopit-cli --commit` | Defensively stages modified packaging files, strips trailing whitespace, extracts `.changes`, and launches `$EDITOR`. |
