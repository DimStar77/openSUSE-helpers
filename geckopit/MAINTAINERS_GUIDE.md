# <img src="./org.opensuse.geckopit.svg" width="48" height="48" align="center" /> GECKOPIT: UNIVERSAL openSUSE PACKAGING COCKPIT

Welcome to **Geckopit** (formerly the GNOME Sync Dashboard) — the unified, multi-profile openSUSE packaging cockpit and release engineering suite designed for modern, rapid, and dependable maintenance.

This guidebook is the official manual for package maintainers and release engineers coordinating openSUSE pipelines. It covers workstation setup, repository profile configuration, and standard packaging workflows to keep hundreds of SCM checkouts synchronized between upstream releases, local checkouts, Gitea, and the Open Build Service (OBS).

The Geckopit suite provides two unified interfaces:
*   **Desktop Cockpit (`geckopit`)**: A fast GTK 4 / Libadwaita application featuring real-time multi-branch tracking, embedded terminal tabs, 1-click update/merge/PR actions, and syntax-highlighted code diffing.
*   **Terminal Suite (`geckopit-cli`)**: A high-throughput CLI tool providing single-package dashboards, headless strategy-based upgrades (`--upgrade`), and whitespace-sanitized commit workflows (`--commit`).

---

## 🗺️ PART 1: THE PACKAGING COCKPIT LANDSCAPE

Geckopit translates complex Git-branch synchronization and version comparisons into an intuitive desktop dashboard and CLI companion. It coordinates across four primary tracks:

```text
               (Upstream Stable Release)
openSUSE:Factory  ●--------➔ [Stable Update] ➔ [Stable Maintenance] (Tumbleweed)
                            \                    \
                             \ (Back-Merges)      \
Staging (Next)    ●-----------➔ ●-------------------➔ ● (Unstable Staging / Forwarding)
```

1.  **Upstream Releases**: Monitored via API endpoints (`release-monitoring.org` API integrations).
2.  **Unstable Staging Branches**: Staged under your local checkouts' unstable branch (`next`, `develop`, etc.). Upcoming upstream development cycles (such as GNOME alpha, beta, and release candidates) are integrated and compiled here.
3.  **Stable Development Branches**: Staged under your local checkouts' stable branch (`factory`, `master`, etc.). This represents the stable production code building directly into openSUSE Tumbleweed.
4.  **Central Gitea Pools**: Located under openSUSE Gitea (`src.opensuse.org/pool/`). The central source of truth where packages from all maintainers align before being submitted to Tumbleweed.

*Profile Versatility*: Geckopit natively supports both **Dual-Pipeline** setups (e.g. `factory` + `next` for GNOME) and **Single-Pipeline** setups (e.g. standalone Factory-only packaging). When a profile omits the unstable branch, Geckopit dynamically adapts the interface to a streamlined single-pipeline view.

---

## 💻 PART 2: WORKSTATION SETUP & ARCHITECTURE

Geckopit is location-independent and can be launched from anywhere on your system. It is fully integrated with GTK 4, Libadwaita, and Wayland.

### 🚀 Step 1: Automated 1-Step Setup (`install.sh`)
The fastest and recommended way to install Geckopit, its dependencies, local binary symlinks, and desktop integration is running the automated installer:

```bash
# Clone the helper repository
git clone https://src.opensuse.org/GNOME/openSUSE-helpers.git ~/Documents/git-rw/openSUSE-helpers

# Run the installer
cd ~/Documents/git-rw/openSUSE-helpers/geckopit
./install.sh
```

**What `install.sh` handles automatically:**
1.  **Dependency Verification**: Checks for required Python modules and GObject Introspection bindings (`python3-rpm`, `python3-requests`, `python3-gobject`, `typelib-1_0-Gtk-4_0`, `typelib-1_0-Adw-1`, `typelib-1_0-Vte-3_91`, and `gitea-tea`).
2.  **Automated Zypper Prompt**: If any packages are missing, it prompts you to install them with a single `sudo zypper in -y` command.
3.  **CLI Command Symlinks**: Creates clean symlinks in `~/bin` for `geckopit`, `geckopit-cli`, and `geckopit-upgrade` (and verifies `~/bin` is in your `$PATH`).
4.  **Wayland Desktop Launcher & Icon**: Copies `org.opensuse.geckopit.desktop` into `~/.local/share/applications/` and the HD vector SVG icon into `~/.local/share/icons/hicolor/scalable/apps/`, refreshing application and icon databases.

---

### 📦 Manual Setup & Dependencies (Alternative)
If you prefer manual configuration or are working in a custom headless environment:

```bash
# 1. Install system dependencies
sudo zypper in \
    python3-rpm \
    python3-requests \
    python3-gobject \
    typelib-1_0-Gtk-4_0 \
    typelib-1_0-Adw-1 \
    typelib-1_0-Vte-3_91 \
    gitea-tea

# 2. Create command symlinks in ~/bin
mkdir -p ~/bin
ln -sf ~/Documents/git-rw/openSUSE-helpers/geckopit/geckopit.py ~/bin/geckopit
ln -sf ~/Documents/git-rw/openSUSE-helpers/geckopit/geckopit-cli ~/bin/geckopit-cli
ln -sf ~/Documents/git-rw/openSUSE-helpers/geckopit/geckopit-upgrade ~/bin/geckopit-upgrade
export PATH=$HOME/bin:$PATH

# 3. Install desktop launcher and icon
mkdir -p ~/.local/share/applications ~/.local/share/icons/hicolor/scalable/apps
cp ~/Documents/git-rw/openSUSE-helpers/geckopit/org.opensuse.geckopit.desktop ~/.local/share/applications/
cp ~/Documents/git-rw/openSUSE-helpers/geckopit/org.opensuse.geckopit.svg ~/.local/share/icons/hicolor/scalable/apps/
```

---

### 📂 Step 2: Zero-Click Onboarding & Multi-Profile Layouts
On launch, Geckopit executes an autodetection pipeline that maps your active directories and populates profiles. If launched inside an SCM repository or meta-checkout directory, it immediately registers that workspace.

🪄 **Magic SCM Worktree Auto-Discovery**: When adding or editing profiles in the *Workspace Profile Manager Settings* dialog (gear icon ⚙️), browse and select your **Stable Path** folder. Geckopit interrogates the local Git repository's `.git/worktrees` database. If exactly one active Git worktree is discovered on disk, Geckopit **automatically pre-fills** your **Unstable Path**, **Stable Branch**, and **Unstable Branch** entries, displaying a confirmation toast notification.

Geckopit persists profile settings and UI state in **`~/.config/geckopit.json`**:

```json
{
  "active_workspace": "GNOME",
  "max_workers": 50,
  "horizontal_splitter_pos": 460,
  "vertical_splitter_pos": 750,
  "workspaces": {
    "GNOME": {
      "stable_path": "/home/dimstar/Documents/src.o.o/GNOME",
      "stable_branch": "factory",
      "unstable_path": "/home/dimstar/Documents/src.o.o/GNOME:Next",
      "unstable_branch": "next",
      "ignored_unstable_versions": {}
    }
  }
}
```

*Splitter positions and sidebar width persist automatically across restarts.*

---

## ⚡ PART 3: ADVANCED RUNTIME OPTIMIZATIONS

Geckopit manages massive monorepos containing 500+ checkouts with zero UI latency through several architectural optimizations:

### 💾 1. Instant Startup Caching
Querying 500+ Git repositories and external APIs sequentially takes significant time. Geckopit serializes workspace states in profile-specific cache files (`~/.cache/geckopit/cache_{profile}.json`), enabling an **instantaneous (0.01 seconds) startup**.
*   **The `(cached)` Badge**: On launch or profile switch, Geckopit draws your workspace instantly from cache. Package headers display a subtle `(cached)` badge signaling cached data.
*   **Quiet Background Resyncs**: In the background, worker threads quietly fetch live SCM and API states.
*   **Live Freshness Swapping**: Once live scans finish for a package, the `(cached)` indicator clears and action cards update seamlessly.

### 🚦 2. Priority Direct Threading
Selecting a package in the sidebar immediately prioritizes that package. Geckopit spawns **direct foreground priority threads** that bypass the background queue, fetching stable, unstable, and Gitea details immediately. The syntax-highlighted code-diff viewer similarly loads without queue contention.

### 🔒 3. The Selection Lock Safeguard
If you select a package under the `"⚠️ Needs Action"` filter and background threads discover that someone already updated it, re-filtering could abruptly remove the row from under your mouse. Geckopit enforces a **Selection Lock**: the focused package is immune to removal while active. Its badges update in place, but the row remains selectable until you navigate elsewhere.

### ⚓ 4. Viewport Scroll Anchoring
Toggling filter tracks or typing search queries smoothly anchors the scroll position so your active selection remains visible on-screen, preventing disorienting list jumps.

### 📡 5. Local Worktree Remote Tracking
Geckopit checks whether local checkouts are synchronized with their remote tracking branches (`origin`):
*   **`Local -N` Badges**: If your local checkout is behind remote, the sidebar displays an amber `Local -N` badge, and pipeline cards provide a 1-click **`📥 Pull`** fast-forward button.
*   **`Local +N` Badges**: Unpushed local commits surface as `Local +N` badges with an active **`📤 Push (N)`** button.
*   **Update Guards**: SCM updates automatically detect pending pulls, executing `git pull --ff-only` prior to updates to prevent merge conflicts.

### 🔄 6. Instant On-Disk Synchronization
Selecting a package or closing an embedded terminal tab immediately re-evaluates the local `.spec` file on disk, transitioning the update button to `"✅ Up-To-Date"` and enabling the **`📤 Push`** action without waiting for background network polling.

### 🎨 7. Adaptive Dark Mode Synchronization
The diff viewer and Gitea Pull Request dialogue listen to `Adw.StyleManager` system theme events. When dark mode is active, editors adopt a high-contrast dark theme (`"oblivion"`); when toggled to light mode, they transition to `"classic"`, ensuring consistent contrast.

### ⚙️ 8. Configurable Scan Concurrency
Geckopit defaults to `50` concurrent background workers. On bandwidth-constrained or metered connections:
*   Open the **Settings Dialog** (gear icon ⚙️) and locate **`Background Concurrency`**.
*   Adjust the worker count (from `5` to `50` in steps of `5`).
*   Saving the profile scales the thread pool dynamically on-the-fly without requiring an application restart.

---

## ⚙️ PART 4: MAINTENANCE WORKFLOWS

Launch the cockpit from your application launcher or terminal:

```bash
geckopit
```

### 🧪 1. Strategy-Based Package Upgrades
In the package detail workspace, review the **🟢 STABLE PIPELINE** and **🟡 UNSTABLE PIPELINE** cards. If an update is detected from upstream, click **`⚙️ Run SCM Update`** (or execute `geckopit-cli --upgrade` in your terminal).

Geckopit inspects the package directory and automatically executes the appropriate upgrade engine (`geckopit/helpers/upgrade/`):

#### A. OBS SCM Engine (`ObsScmUpgradeHelper`)
For packages managed through `_service` (`obs_scm` / `tar_scm`):
*   Detects revision formatting (e.g. `1_0_5` vs `1.0.5`), updates the revision tag inside `_service`, and executes `osc service mr download_files`.
*   **openSUSE Source Duplication Rules**:
    *   **`mode="buildtime"`**: Commit only the intermediate source archive (e.g. **`.obscpio`**). **Never commit the `.tar.xz`** (OBS compresses it at build time).
    *   **`mode="local"` or `mode="manual"`**: Commit only the compressed **`.tar.xz`** tarball, and **never commit the `.obscpio`**.

#### B. Direct Tarball Engine (`TarballUpgradeHelper`)
For packages managed directly via `.spec` URL definitions:
*   Executes `osc service mr download_files` to retrieve the latest upstream release tarball.
*   **Git-LFS Disk Cache Access**: Reads archives directly from the local Git-LFS cache (`.git/lfs/objects/`) to prevent duplicate downloads and conserve network bandwidth.
*   **100MB Memory Guard**: Defensively manages memory buffers when handling large source tarballs (e.g. LibreOffice, Chromium, WebKitGTK).
*   **In-Memory Archive Diffing**: Inspects upstream release notes and build files (`NEWS`, `ChangeLog`, `meson.build`) in memory without extracting massive archives to disk.

#### C. Automated openSUSE Changelog Engine (`changelog.py`)
Both engines use Geckopit's shared changelog library:
*   Formats `*.changes` entries following openSUSE packaging guidelines.
*   Enforces strict **67-column multi-level bullet wrapping** (`- `, `  + `, `    - `, `      . `).
*   Unwraps upstream `NEWS` / `ChangeLog` paragraphs cleanly.
*   Strips compound upstream issue tracker references (e.g. `!123`, `#456`), preserving standard CVE identifiers.
*   Consolidates multi-line translation updates into a single summary entry.
*   Automatically bumps the `Version:` declaration in the `.spec` file.

#### D. Automated Merged Patch Dropper
Before completing an upgrade, Geckopit tests existing patches using `git merge-base` or reverse application. Any downstream patches already incorporated upstream are automatically deleted from disk, and their corresponding `PatchN:` declarations and `%patch` macro invocations are cleanly removed from the `.spec` file.

---

### 📝 2. Modern Packaging Commit Helper (`geckopit-cli --commit`)
When packaging edits and test builds are complete, commit your work using the modernized packaging commit helper:

```bash
geckopit-cli --commit
# or via the short flag:
geckopit-cli -c
# or via the backwards-compatible shim:
gc.sh
```

**Key Features:**
*   **Automated Whitespace Sanitization**: Strips trailing whitespace from modified packaging files (`.spec`, `.changes`, `_service`, patches) before staging.
*   **Defensive Staging**: Honors `.gitignore` and stages only relevant packaging files, safely ignoring untracked review directories, test artifacts, or nested Git clones.
*   **Pre-filled Commit Message**: Extracts the latest entry from the top of the `*.changes` file and prefills `$EDITOR` (or `$VISUAL`) with a clean, standard Git commit message. Use `--no-edit` for automated commits.

---

### 🔀 3. Unstable Pipeline Back-Merging (Catch-Up Merges)
When stable fixes land directly on your stable branch (`factory`), the unstable branch (`next`) falls behind:
*   The **Unstable Pipeline card** surfaces: `Branch Alignment: Next behind Factory by {N} commits`.
*   Click **`🔀 Catch-up Merge`** to launch an embedded terminal tab prefilled with `git merge factory`.
*   Resolve any merge conflicts favoring the newer unstable version line while preserving packaging fixes.
*   Commit using `geckopit-cli --commit` and push using the **`📤 Push`** button in the pipeline card.

---

### 📤 4. Local Push & Gitea PR Promotion
When unstable testing concludes and upstream issues a release, promote changes to stable:
1.  **Review Differences**: Inspect branch differences in the integrated **Diff Viewer** panel below the pipeline cards.
2.  **Push Commits**: If unpushed commits exist, click **`📤 Push`** to push to `origin`.
3.  **Create Pull Request**: Click **`📤 Create PR`** in the Unstable Pipeline card to open the PR dialogue.
    *   **Smart Title**: Auto-populates to `Update <pkg> to version <ver>` on version bumps, or the commit subject for packaging fixes.
    *   **Smart Description**: Auto-fills directly with entries extracted from the `*.changes` Git diff.
    *   **In-App Submission**: Submits the PR directly to `src.opensuse.org` via Gitea's `tea` CLI integration.
    *(Note: Ensure you have authenticated once by running `tea login add` in your terminal).*

---

## 🖥️ PART 5: THE CLI COMPANION (`geckopit-cli`)

`geckopit-cli` provides terminal-based monitoring and automation across both single packages and entire workspaces:

```bash
# Display help and available modes
geckopit-cli --help
```

### 📦 1. Contextual Single-Package Dashboard
Run `geckopit-cli` inside any package directory (or supply a package name from the workspace root: `geckopit-cli <package>`):

```text
================================================================================
  📦 Package: mutter
================================================================================
  Local Spec Version:    47.1
  Upstream Release:      Stable: 47.1 | Unstable: 47.beta

  Local Branches:
    Stable (factory):    ✅ Up-to-date with origin/factory
    Unstable (next):     ✅ Up-to-date with origin/next

  Branch Sync:
    Alignment:           Next is ahead of Factory by 3 commits (Ready to Forward)
    Gitea Pool:          ✅ In Sync with Gitea Pool

  📋 Recommendation:
    👉 Create Gitea Pull Request: forward next into factory.
================================================================================
```

### ⚡ 2. Headless Strategy Upgrades
Upgrade any package directly from the command line:

```bash
# Upgrade package in current directory to latest upstream revision
geckopit-cli --upgrade

# Upgrade to a specific tag or revision
geckopit-cli --upgrade 47.2

# Dry-run mode to preview steps without disk modifications
geckopit-cli --upgrade --dry-run
```

### 🔍 3. Workspace Batch Audits
From the workspace root directory, run parallel batch audits across all packages:

```bash
# Default audit: Check downstream sync and Gitea pool status
geckopit-cli

# Upstream release monitor: Query release-monitoring.org for new releases
geckopit-cli -v

# Forwarding audit: Identify next branches ready to merge to factory
geckopit-cli -f

# Filter for packages requiring maintainer action
geckopit-cli --todo
```

### 🔄 4. Legacy Script Compatibility
Existing maintainer scripts (`gc.sh`, `gnome-audit.sh`, `gnome-catchup.sh`, `gnome-promote.sh`, `gnome-pool-audit.sh`, and `obs_scm-update.sh`) automatically delegate to `geckopit-cli` when installed in `$PATH`, maintaining muscle memory while using the modern Python backend.

---

## ⌨️ PART 6: CHEAT SHEET & QUICK KEYBOARD SCAPE

### 🖥️ 1. Pipeline Card Terminal Launchers
Each pipeline card in the package detail workspace provides a dedicated one-click terminal launcher:
*   **`🖥️ Terminal (<stable_branch>)`** (e.g. `Terminal (factory)`): Spawns an embedded terminal drawer tab navigated to the package's local stable worktree.
*   **`🖥️ Terminal (<unstable_branch>)`** (e.g. `Terminal (next)`): Spawns an embedded terminal drawer tab navigated to the package's local unstable worktree.

### ⌨️ 2. Terminal Font Zooming & Indicators
When working inside the terminal drawer, scale fonts dynamically:
*   **Zoom In**: `Ctrl` + `+` (or `Ctrl` + `Shift` + `=`)
*   **Zoom Out**: `Ctrl` + `-`
*   **Reset Zoom**: `Ctrl` + `0`
*   An auto-fading percentage indicator badge (`🔍 120%`) appears temporarily in the console drawer header during zoom adjustments.

### 📑 3. Drag-and-Drop Terminal Tab Reordering
When multiple terminals are open in the bottom drawer, drag and drop tab headers to reorder them. Closing a tab (`exit` or close button) smoothly returns keyboard focus to the remaining active shell.

### ⌨️ 4. Keyboard Shortcuts & Quick Navigation
Navigate and filter your packages rapidly without taking your hands off the keyboard:
*   **Search Packages**: Press `Ctrl` + `F` or `/` from anywhere in the app to focus the search bar.
*   **Select First Match**: Press `Enter` or `Down` inside the search bar to immediately focus and select the first matching package in the list.
*   **Navigate Package List**: Press `j` or `Down` to step to the next package; press `k` or `Up` to step to the previous package, loading details and diffs in real time.
*   **Toggle Filter Tracks**:
    *   `Ctrl` + `1`: Toggle **⚠️ Needs Action** global filter
    *   `Ctrl` + `2`: Toggle **📡 Pool Sync** track
    *   `Ctrl` + `3`: Toggle **🟢 Stable Updates** track
    *   `Ctrl` + `4`: Toggle **🟠 Unstable Updates** track
    *   `Ctrl` + `5`: Toggle **🔀 Forwarding** track
*   *Note: Defensive input guards prevent shortcuts from intercepting keystrokes while you are typing inside a terminal shell or text field.*

### 📋 5. One-Click Diff Copying
Click the **`edit-copy-symbolic`** (Copy icon 📋) in the diff viewer or PR dialog header to copy raw Git patch content directly to your clipboard, confirmed by a floating toast notification.

---

## 🛠️ PART 7: ADVANCED COCKPIT SECRETS (POWER USER GUIDE)

### 🚫 Interactive Version-Ignoring Mouse Gesture
Some upstream projects have abandoned, broken, or unusable unstable versions reported by release monitoring for years. To prevent these stagnant releases from cluttering your `"⚠️ Needs Action"` list:

1.  **Trigger the Gesture**:
    Hold **`Ctrl` + `Alt`** and **right-click** directly on the **Version Alignment** label inside the Unstable Pipeline card.
2.  **Toggle Ignore State**:
    *   In the popup menu, click **`🚫 Ignore Version {Version}`**.
    *   The version label transitions to an italicized gray **`(Ignored)`** badge, and the SCM Update button disables.
    *   The package smoothly slides out of your filtered sidebar list in real time.
3.  **To Unignore**:
    Perform the same gesture (**`Ctrl` + `Alt` + Right Click**) on the ignored label and select **`🔄 Unignore Version {Version}`**.

*Note: Ignored states are saved in active profile configuration under `ignored_unstable_versions` in `~/.config/geckopit.json`. Both the GUI and CLI (`geckopit-cli -v`) respect these rules seamlessly.*
