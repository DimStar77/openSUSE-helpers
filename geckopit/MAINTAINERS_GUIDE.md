# <img src="./org.opensuse.geckopit.svg" width="48" height="48" align="center" /> GECKOPIT: UNIVERSAL openSUSE PACKAGING COCKPIT

Welcome to **Geckopit** (formerly the GNOME Sync Dashboard) — the unified, multi-profile openSUSE packaging cockpit designed for modern, rapid, and secure release engineering.

This guidebook is the official manual for package maintainers and release engineers coordinating openSUSE pipelines. It covers setting up your workstation, configuring custom repository profiles, and executing standard packaging workflows to keep hundreds of SCM checkouts in sync between upstream releases, local checkouts, Gitea, and the Open Build Service (OBS).

---

## 🗺️ PART 1: THE PACKAGING COCKPIT LANDSCAPE

Geckopit translates complex Git-branch synchronization and version comparisons into a clean, modern, and snappy desktop dashboard. It manages the coordination of three main tracks:

```text
               (Upstream Stable Release)
openSUSE:Factory  ●--------➔ [Stable Update] ➔ [Stable Maintenance] (Tumbleweed)
                            \                    \
                             \ (Back-Merges)      \
Staging (Next)    ●-----------➔ ●-------------------➔ ● (Unstable Staging / Forwarding)
```

1.  **Upstream Releases**: Monitored via standard API endpoints (`release-monitoring.org` API integrations).
2.  **Unstable Staging Branches**: Staged under your local checkouts' unstable branch (`next`, `develop`, etc.). This is where upcoming upstream development cycles (such as GNOME alpha, beta, and release candidates) are integrated and compiled.
3.  **Stable Development Branches**: Staged under your local checkouts' stable branch (`factory`, `master`, etc.). This is the stable production code that builds directly into openSUSE Tumbleweed.
4.  **Central Gitea Pools**: Located under openSUSE Gitea (`src.opensuse.org/pool/`). The central source of truth where packages from all maintainers align before being submitted to Tumbleweed.

---

## 💻 PART 2: WORKSTATION SETUP & ARCHITECTURE

Geckopit is location-independent and can be launched from anywhere on your system. It is fully integrated with GTK 4, Libadwaita, and Wayland.

### 📦 Step 1: Install System Dependencies
Install the required GTK4, Libadwaita, GObject Introspection bindings, and VTE terminal dependencies via `zypper`:

```bash
sudo zypper in \
    python3-rpm \
    python3-requests \
    python3-gobject \
    typelib-1_0-Gtk-4_0 \
    typelib-1_0-Adw-1 \
    typelib-1_0-Vte-3_91 \
    gitea-tea
```

### 🔗 Step 2: Clone and Link SCM Dashboard Tools
Clone your helper repository containing Geckopit:

```bash
# Clone the repository
git clone https://src.opensuse.org/GNOME/openSUSE-helpers.git ~/Documents/git-rw/openSUSE-helpers

# Create a local bin folder if it doesn't exist
mkdir -p ~/bin

# Create clean symbolic links pointing to the Geckopit executables
ln -s ~/Documents/git-rw/openSUSE-helpers/geckopit/geckopit.py ~/bin/geckopit
ln -s ~/Documents/git-rw/openSUSE-helpers/geckopit/geckopit-cli ~/bin/geckopit-cli

# Ensure your local bin path is in your environment PATH
export PATH=$HOME/bin:$PATH
```

### 📂 Step 3: Zero-Click Onboarding & Multi-Profile Layouts
On launch, Geckopit executes a **three-tier autodetection pipeline** that automatically maps your active directories and populates its profiles. If launched from inside a folder containing standard SCM sub-checkouts, it instantly registers that workspace.

🪄 **Magic SCM Worktree Auto-Discovery**: When adding or editing profiles in the *Workspace Profile Manager Settings* dialog, simply browse and select your **Stable Path** folder. Geckopit will instantly interrogate the local Git repository's `.git/worktrees` database. If exactly one valid active Git worktree is discovered on disk, Geckopit will **automatically pre-fill** your **Unstable Path**, **Stable Branch**, and **Unstable Branch** entries, displaying a gorgeous floating toast notification. This delivers a magic, zero-click setup for split worktree workspaces!

To support multiple SCM workspaces (e.g. `GNOME`, `Games`, `Virt`), Geckopit stores settings inside **`~/.config/geckopit.json`**:

```json
{
  "active_workspace": "GNOME",
  "max_workers": 50,
  "workspaces": {
    "GNOME": {
      "stable_path": "/home/dimstar/Documents/src.o.o/GNOME",
      "stable_branch": "factory",
      "unstable_path": "/home/dimstar/Documents/src.o.o/GNOME:Next",
      "unstable_branch": "next"
    }
  }
}
```

*Note: SCM paths must be structured side-by-side (using standard checkouts or `git worktree`) to allow Geckopit to cleanly translate terminal directories between stable and unstable tracks.*

### 🖥️ Step 4: Wayland Desktop & Icon Integration
Geckopit features a custom, high-definition SVG application icon. To install the launcher and icon natively into your desktop environment, run:

```bash
# Install the Wayland Desktop Launcher
mkdir -p ~/.local/share/applications/
cp ~/Documents/git-rw/openSUSE-helpers/geckopit/org.opensuse.geckopit.desktop ~/.local/share/applications/

# Install the HD Scalable Vector SVG Icon
mkdir -p ~/.local/share/icons/hicolor/scalable/apps/
cp ~/Documents/git-rw/openSUSE-helpers/geckopit/org.opensuse.geckopit.svg ~/.local/share/icons/hicolor/scalable/apps/org.opensuse.geckopit.svg
```

---

## ⚡ PART 3: ADVANCED RUNTIME OPTIMIZATIONS

Geckopit integrates cutting-edge performance and ergonomics to manage massive monorepos containing 500+ checkouts with zero interface lag:

### 💾 1. Instant Startup Caching
Fanning out 500+ parallel SCM git queries and Gitea API calls takes several seconds. To make the interface **instantaneous (0.01 seconds launch)**, Geckopit saves and serializes states inside profile-specific cache files (`~/.cache/geckopit/cache_{profile}.json`).
*   **The `(cached)` Badge**: When starting or swapping profiles, Geckopit instantly draws your entire layout using cached data. Package headers carry a gray `(cached)` badge signaling that the cockpit is showing last-run details.
*   **Quiet Background Resyncs**: While you browse the cached state, a background pool of 50 workers quietly fanned out to fetch live states.
*   **Live Freshness Swapping**: Once both SCM sync and version scans finish for a package, the `(cached)` tag vanishes, and your action cards instantly redraw to represent the fresh, live SCM state.

### 🚦 2. User-Selected Priority Threading
If you select a package from the sidebar while the slow background sweep is running, Geckopit **immediately prioritizes it**.
It spawns **direct, dedicated priority threads** that completely bypass the saturated background queue, fetching stable, unstable, and Gitea SCM details instantly. Furthermore, the **code-diff viewer** bypasses the thread pool, loading your syntax-highlighted diffs in milliseconds!

### 🔒 3. The Selection Lock Safeguard
If you select a package row under the `"Needs Action"` filter, and a background scan discovers that someone has already updated it, running a list refilter would normally delete the row from your sidebar, causing sudden layout shifts.
Geckopit enforces a **Selection Lock**: the currently focused package is completely immune to being filtered out. Its badges will resolve in-place (e.g. changing to a green `✅` or `"Fully In Sync"`), but the row remains visible in your sidebar until you select a different package, respecting your active focus.

### 🎨 4. Adaptive Dark Mode Synchronization
The inline code-diff viewer and Gitea Pull Request dialogue feature real-time style-scheme listeners connected to `Adw.StyleManager`. If your system is in dark mode, the editors adopt a gorgeous, dark-gray theme (`"oblivion"`); if toggled to light mode, they instantly transition back to light rendering (`"classic"`), eliminating visual fatigue.

### ⚙️ 5. Configurable Background Scan Concurrency
By default, Geckopit executes up to `50` concurrent background SCM and version scans to maximize throughput. However, on slower internet connections or highly restricted corporate networks, fanning out 50 concurrent `git fetch` and HTTP API calls can saturate your local network or trigger server-side rate limits (e.g. HTTP 429).
*   **The Tuning Knob**: Open the **Workspace Profile Manager Settings** dialog (gear icon ⚙️) and locate **`Background Concurrency`**.
*   **Adjusting the Load**: Use the spinner to set your preferred concurrent worker count (ranging from `5` to `50` in increments of `5`).
*   **On-the-Fly Scaling**: Saving your profile instantly scales the thread pool worker count on-the-fly, allowing you to fine-tune network and CPU usage without needing to restart the application!

---

## ⚙️ PART 4: MAINTENANCE WORKFLOWS

Start the cockpit natively from your desktop launcher or the command line:

```bash
geckopit
```

### 🧪 1. Version Upgrades & `_service` Hygiene
Inside the **Upstream Updates** tab, Geckopit matches local package declarations with upstream versions. If an update is available, you can click **`Update Factory/Next`** to spawn an embedded terminal tab with pre-filled update hooks:

*   **Source Duplication Rules**:
    *   If the local `_service` file runs at **`mode="buildtime"`**: Stage and check in only the generated **`.obscpio`** intermediate archive. **Never check in the final `.tar.xz`**, as OBS rebuilds it on-the-fly, preventing duplicate commits.
    *   If the service runs at **`mode="local"`** or **`mode="manual"`**: Check in only the final compressed **`.tar.xz`** tarball, and do **not** check in the `.obscpio`.

### 🔀 2. Unstable Pipeline Back-Merging
When other maintainers check stable bugfixes directly into your stable branch, your unstable branch falls behind. Geckopit's **Repository Sync** tab will flag the package with: `"Next behind Factory by {N} commits"`.
*   Click **`Catch-up Merge`** to spawn an embedded terminal tab prefilled with merge hooks.
*   Resolve conflicts in favor of the newer unstable version line while preserving packaging improvements.
*   Commit and push to remote.

### 📤 3. Standard Gitea PR Promotion
Once your unstable staging has been fully tested and upstream cuts a stable release, you can merge unstable back into your stable branch and push.
*   Open the **Forward & PR** tab in Geckopit.
*   Review the code-diff inside the panel.
*   Click **`Create Pull Request`** to open our custom, in-app PR dialogue.
*   Review the prefilled source/target branches, edit your PR title and description, and submit—**completely in-app** via secure command integration!
    *(Note: This uses the official Gitea CLI tool `tea` under the hood. Make sure you have authenticated once by running `tea login add` in your terminal so it has access to create PRs on `src.opensuse.org`!)*

---

## ⌨️ PART 5: CHEAT SHEET & QUICK KEYBOARD SCAPE

### ℹ️ 1. Interactive SCM Legend
Click the **`help-about-symbolic`** (Information icon ℹ️) on the control bar of the **Repository Sync** tab to instantly view a dynamic explanation of SCM States, customized dynamically to your active profile's branch mappings and pipeline layout.

### ⌨️ 2. Terminal Font Zooming
When working inside the tabbed terminal drawer, scale fonts dynamically:
*   **Zoom In**: `Ctrl` + `+` (or `Ctrl` + `Shift` + `=`)
*   **Zoom Out**: `Ctrl` + `-`
*   **Reset Zoom**: `Ctrl` + `0`

### 🖱️ 3. SCM Right-Click Context Menu
Right-click on any package row in the sidebar list to instantly open terminal tabs in your unstable or stable worktrees, trigger manual scans, or execute SCM updates.

---

## 🛠️ PART 6: ADVANCED COCKPIT SECRETS (POWER USER GUIDE)

### 🚫 Interactive Version-Ignoring Mouse Gesture

Some upstream repositories have stagnant, broken, or unusable unstable versions out for years (e.g. `telepathy-idle` is stable at `0.2.2` but upstream reports unstable `0.99.11` which is stagnant and dead). To prevent these bad updates from perpetually cluttering your active tasks and flagging as `"Needs Action"`:

1.  **Trigger the Hidden Gesture**:
    Hold **`Ctrl` + `Alt`** and **right-click** directly on the **Version Alignment** label inside the Unstable Pipeline card.
2.  **Toggle the Ignore State**:
    *   A contextual GTK Popover menu will spawn. Click **`🚫 Ignore Version {Version}`**.
    *   The upstream version label will instantly transition to a muted, italicized gray **`(Ignored)`** tag, and the SCM Update button will disable.
    *   The package will **automatically and cleanly slide out of your filtered sidebar list in real-time!**
3.  **To Unignore**:
    Perform the same gesture (**`Ctrl` + `Alt` + Right Click**) on the ignored label and select **`🔄 Unignore Version {Version}`**.

*Note: Geckopit saves these ignored states inside your active profile's `ignored_unstable_versions` block inside `~/.config/geckopit.json`. Both the GUI and CLI (`geckopit-cli -v next`) automatically and seamlessly respect these ignored configurations in real-time, completely silencing stale upstreams across both interfaces!*
