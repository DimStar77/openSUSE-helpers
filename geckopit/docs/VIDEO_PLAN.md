# 🎬 Geckopit Video Tutorial Storyboards & Production Plan

Internal production reference and on-screen storyboards for producing 60–90 second workflow walkthrough clips for openSUSE packagers.

---

Short, focused 60–90 second screen recordings are the best medium for onboarding new maintainers. Here is the 4-clip production plan:

### Clip 1: "The 60-Second Upstream Bump" (~60 seconds)
* **Goal**: Demonstrate how Geckopit transforms a tedious version upgrade into an effortless 1-click operation.
* **Storyboard**:
  1. `00:00 - 00:10`: Show the Cockpit starting with `⚠️ Needs` active. Focus on `zenity` displaying `Update Av. 4.2.0 ➔ 4.2.2`.
  2. `00:10 - 00:25`: Click **`Update Next to 4.2.2`**. The console drawer slides open showing `ObsScmUpgradeHelper` fetching sources, dropping merged patches, and formatting `.changes` at 79 columns.
  3. `00:25 - 00:40`: Show the update button smoothly transitioning to `Up-To-Date`, and the `Push` button illuminating.
  4. `00:40 - 00:55`: Click `Push`. Toast confirms push. Package badges update in place.
  5. `00:55 - 01:00`: Closing splash: *"Automated upgrades with Geckopit."*

### Clip 2: "Promoting Next to Factory & 1-Click Gitea PRs" (~75 seconds)
* **Goal**: Illustrate pre-release forwarding and automated pull request generation.
* **Storyboard**:
  1. `00:00 - 00:15`: Press `Ctrl+5` to toggle the `🔀 Fwd.` track. Sidebar filters down to checkouts with commits ready to forward.
  2. `00:15 - 00:30`: Select a package. Review the syntax-highlighted diff between `next` and `factory`. Click `📋 Copy Diff` and show the toast.
  3. `00:30 - 00:50`: Click `Create Pull Request`. The dialog pops up with title and description already pre-filled from the `.changes` diff.
  4. `00:50 - 01:10`: Click `Submit`. PR opens on Gitea.
  5. `01:10 - 01:15`: Closing splash: *"Seamless promotion across branches."*

### Clip 3: "Automatic Meson Dependency Drift Auditing" (~45 seconds)
* **Goal**: Show how to eliminate build-time surprises from upstream Meson version bumps.
* **Storyboard**:
  1. `00:00 - 00:15`: Run `geckopit-cli --audit-deps` inside `zenity`. Terminal highlights `pkgconfig(libadwaita-1) >= 1.2 (unversioned)`.
  2. `00:15 - 00:30`: Run `geckopit-cli --fix-deps`. Show Git diff automatically appending `>= 1.2` to `BuildRequires:` and creating the `.changes` entry.
  3. `00:30 - 00:40`: Show the Cockpit GUI detail card where the inline `🔧 Fix in .spec` button performs the exact same action in one click.
  4. `00:40 - 00:45`: Closing splash: *"Zero dependency drift."*

### Clip 4: "Hands-Off-The-Mouse: Terminal Power User Navigation" (~60 seconds)
* **Goal**: Showcase keyboard agility for terminal power users.
* **Storyboard**:
  1. `00:00 - 00:15`: Press `/` to search for `gtk4`. Hit `Enter` to hand off focus to the package list.
  2. `00:15 - 00:30`: Tap `j` and `k` to step through packages, watching syntax-highlighted diffs and cards update in milliseconds without mouse clicks.
  3. `00:30 - 00:45`: Press `Ctrl+1` through `Ctrl+5` to toggle filter tracks instantly, showing scroll anchoring keeping the active row in view.
  4. `00:45 - 00:55`: Press `Ctrl+F` in the diff viewer to jump to search terms, and `Ctrl++` to zoom terminal fonts.
  5. `00:55 - 01:00`: Closing splash: *"Built for speed."*
