# 🦎 openSUSE GNOME PACKAGING: THE OFFICIAL MAINTAINERS' GUIDEBOOK

Welcome to the openSUSE GNOME Packaging Team!

This guidebook is the official manual for maintainers coordinating the openSUSE GNOME release pipeline. It covers setting up your local workstation, installing SCM automation tools, and executing the daily maintenance workflows that keep over 490+ packages in sync between GNOME upstream, Gitea, and Open Build Service (OBS).

---

## 🗺️ PART 1: THE PACKAGING LANDSCAPE

To package GNOME successfully, you must coordinate three separate developmental tracks:

```text
               (GNOME 50 Stable Line)
openSUSE:Factory  ●--------➔ [50.1 Stable Update] ➔ [50.2 Stable Update] (Tumbleweed)
                            \                      \
                             \ (Defensive Merges)   \
GNOME:Next        ●-----------➔ ●---------------------➔ ● (GNOME 51.rc Unstable Staging)
                 (51.alpha)    (51.beta)
```

1.  **Upstream Releases**: Tracked via `release-monitoring.org` (Anubis/Berghain API).
2.  **`GNOME:Next` (Unstable Staging Ground)**: Staged under your local package checkouts' `next` branches. This is where GNOME pre-releases (**GNOME 51.alpha/beta/rc**) are tested for compile-time and API compatibility.
3.  **`GNOME:Factory` (Stable Development Branch)**: Staged under your local package checkouts' `factory` branches. This is the stable track (**GNOME 50.x** maintenance releases) that builds directly into **openSUSE Tumbleweed**.
4.  **The Gitea Pool**: Located at `src.opensuse.org/pool/`. The central openSUSE source of truth where all packages (not only GNOME) come together before being committed to Tumbleweed.

---

## 💻 PART 2: WORKSTATION SETUP

To use our packaging dashboard and automated helper scripts, configure your openSUSE Tumbleweed development machine as follows:

### 📦 Step 1: Install System Dependencies
Install the required GTK4, Libadwaita, GObject Introspection bindings, and VTE terminal dependencies via `zypper`:

```bash
sudo zypper in \
    python3-requests \
    python3-gobject \
    typelib-1_0-Gtk-4_0 \
    typelib-1_0-Adw-1 \
    typelib-1_0-Vte-3_91 \
    gtksourceview5
```

### 🔗 Step 2: Clone and Link SCM Dashboard Tools
Clone the `openSUSE-helpers` repository to get the maintainer dashboards:

```bash
# Clone the repository
git clone https://src.opensuse.org/GNOME/openSUSE-helpers.git ~/Documents/git-rw/openSUSE-helpers

# Create a local bin folder if it doesn't exist
mkdir -p ~/bin

# Create clean symbolic links pointing to the frontend executables
ln -s ~/Documents/git-rw/openSUSE-helpers/GNOME-maintainer-scripts/check_sync.py ~/bin/check_sync.py
ln -s ~/Documents/git-rw/openSUSE-helpers/GNOME-maintainer-scripts/check_sync_gui.py ~/bin/check_sync_gui.py

# Ensure your local bin path is in your environment PATH
export PATH=$HOME/bin:$PATH
```

### 📂 Step 3: Set Up Sibling Git-Worktrees
Our GUI dashboard features automated **SCM worktree directory translation**. If you trigger a build or update on the `factory` branch, the dashboard automatically shifts the terminal's working directory to your sibling `GNOME` stable folder on disk, avoiding branch check-out locks.

To enable this, organize your SCM folders on disk side-by-side using `git worktree`:

```bash
# Main development workspace (Unstable next track / GNOME:Next)
/home/dimstar/Documents/src.o.o/GNOME:Next/
├── epiphany/
├── mutter/
└── gnome-shell/

# Sibling development workspace (Stable factory track / GNOME)
/home/dimstar/Documents/src.o.o/GNOME/
├── epiphany/
├── mutter/
└── gnome-shell/
```

### 🖥️ Step 4: Install the GNOME Desktop Template (Wayland Integration)
To integrate the dashboard cleanly with GNOME Shell's Alt+Tab switcher and top panel natively on Wayland, copy our repository desktop template file into your personal applications directory:

```bash
mkdir -p ~/.local/share/applications/
cp ~/Documents/git-rw/openSUSE-helpers/GNOME-maintainer-scripts/org.opensuse.gnome.sync_dashboard.desktop ~/.local/share/applications/
```

---

## ⚙️ PART 3: PACKAGING WORKFLOWS

Now that your workstation is configured, you are ready to execute our core packaging routines. Start the GUI from your workspace root:

```bash
cd /home/dimstar/Documents/src.o.o/GNOME:Next
check_sync_gui.py
```

---

### 🚦 STAGE 1: PROACTIVE UNSTABLE STAGING (`GNOME 51.x`)

During the initial phase of the GNOME release cycle, we track and stage unstable alphas and betas inside **`GNOME:Next`** (which maps to your local `next` SCM branches).

```text
               (release-monitoring.org)
Upstream GNOME  ●-------------------------➔ [GNOME 51.beta]
                                                   |
                                                   | (obs_scm-update.sh)
                                                   ▼
GNOME:Next      ●-------------------------➔ [next branch]
```

1.  **Isolate Unstable Updates**:
    *   Open `check_sync_gui.py` and select the **Upstream Updates** tab.
    *   Set the branch pipeline dropdown to **`Next Only`**. This filters out stable point releases and displays only GNOME packages that have unstable `51.x` updates pending upstream.
2.  **Surgically Upgrade the Source**:
    *   Right-click the flagged package (e.g., `epiphany`) and select **`Open Terminal in Next Worktree`**.
    *   In your embedded terminal drawer, execute:
        ```bash
        obs_scm-update.sh 51.beta
        ```
    *   This helper re-writes the local package's `_service` configuration.
3.  **Source Duplication Hygiene Rules**:
    *   If the `_service` file declares services at **`mode="buildtime"`**: Stage and check in only the generated **`.obscpio`** archive. **Never check in the final `.tar.xz`**, as OBS generates it dynamically at build time, preventing repository data duplication.
    *   If the service runs at **`mode="local"`** or **`mode="manual"`**: Execute the service locally, check in only the final compressed **`.tar.xz`** archive, and remove the intermediate `.obscpio`.
4.  **Push to Staging Ground**:
    *   Commit changes to the `next` branch and push to Gitea. Unstable builds are automatically triggered inside OBS under `GNOME:Next`.

---

### 🛡️ STAGE 2: DUAL-PIPELINE BACK-MERGING (`GNOME 50` ➔ `GNOME 51`)

While your unstable `next` branch is tracking the future `51.beta`, other contributors or maintainers will push stable **`50.1` or `50.2` point releases** directly to Gitea's `factory` branch.

Because these stable updates bypass the `next` branch completely, your unstable code is now missing critical stable commits (security patches, spec cleanups, or compiler fixes) added in those stable point releases. You must perform defensive back-merges regularly to prevent major package regressions.

```text
GNOME:Factory (50.1)   ●----------------➔ [Commit A: Security Patch]
                                                 \
                                                  \ (Defensive Back-Merge)
                                                   ▼
GNOME:Next (51.beta)   ●------------------------➔ [Integrated Patch]
```

1.  **Detecting Stable Changes**:
    *   Open the **Repository Sync** tab in `check_sync_gui.py`.
    *   When a `50.1` update lands on the stable remote, your dashboard immediately flags that package with: `"Next behind Factory by {N} commits"`.
2.  **Back-Merging the Stable Patches**:
    *   Right-click the flagged row and select **`Open Terminal in Next Worktree`**.
    *   In your embedded terminal, fetch the remote SCM state and merge:
        ```bash
        git fetch origin
        git merge origin/factory --no-commit
        ```
3.  **Overriding the Version Conflict**:
    Because `factory` is on `50.1` and `next` is on `51.beta`, git will throw a conflict in the `.spec` file version line.
    *   **The Rule**: Resolve the version conflict in favor of the newer unstable version (**`51.beta`**).
    *   **The Integration**: Accept and integrate all of the packaging cleanups, build fixes, or security patches introduced in the stable `50.1` release.
    *   Commit the merge and push to `origin/next`.

---

### 🚀 STAGE 3: STABLE POOL SUBMISSION

When upstream GNOME finally cuts the stable `51.0` release and we have verified our packages build in `GNOME:Next`, we must promote our staged packages to the stable track.

Because you executed regular back-merges in Stage 2, your `next` branch is already a complete, clean superset of your stable `factory` branch.

1.  **Merging to Stable Factory**:
    *   Open a terminal in your stable **`GNOME`** (Factory) worktree and merge:
        ```bash
        git checkout factory
        git merge origin/next
        ```
    *   This merge is guaranteed to be fast and conflict-free! Commit and push to your remote development branch.
2.  **Reviewing Outstanding Submissions**:
    *   Open the **Forward & PR** tab in `check_sync_gui.py`.
    *   This tab filters your workspace and lists only repositories where your local stable `factory` branch is ahead of Gitea's central pool (`pool/<pkg>`).
3.  **Verifying and Pushing**:
    *   Select the package in the sidebar to review the file-level git diff in the syntax-highlighted diff viewer.
    *   If the diff is correct, click **`Create Pull Request`**. This opens Gitea directly to open a pull request into Gitea's central repository, cleanly publishing your changes.

---

## ⌨️ PART 4: CHEAT SHEET & COMFORT SHORTCUTS

### 🖥️ 1. Interactive Help Legend
Click the **`help-about-symbolic`** (Information icon ℹ️) on the control bar of the **Repository Sync** tab to instantly view a floating explanation of Pool Sync (Stage 1) and Next Branch Sync (Stage 2) states on-demand.

### ⌨️ 2. Terminal Font Zooming
When working inside the tabbed terminal console drawer, use standard GNOME keyboard shortcuts to scale font sizes dynamically:
*   **Zoom In**: `Ctrl` + `+` (or `Ctrl` + `Shift` + `=`)
*   **Zoom Out**: `Ctrl` + `-`
*   **Reset Zoom**: `Ctrl` + `0`

### 🖱️ 3. SCM Right-Click Context Menu
Right-click on any package row in the **Upstream Updates** tab to instantly trigger:
*   Opening terminal tabs in either your unstable `next` or stable `factory` git-worktrees.
*   Executing automated package updates via `$PATH` available `obs_scm-update.sh` (displayed dynamically if a local `_service` configuration is detected).
