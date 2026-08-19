# 🚀 openSUSE Git Packaging Automation Guide
### *A Parallel Dashboard and Automated Background Merge Suite for openSUSE Package Maintainers*

This guide documents a complete, highly optimized, and production-ready automation suite designed for openSUSE packaging setups that use Gitea submodules (such as `src.opensuse.org`). 

When managing hundreds of active package repositories across three development cycles (`pool ➔ devel:factory ➔ devel:next`), maintainers frequently face massive overhead checking sync states, monitoring upstream releases, and manually splicing `.changes` and `.spec` files during merges. This suite automates **100% of those repetitive tasks** silently in the background.

---

## 📊 Part 1: The Workspace Checker (`check-sync`)

The core tool is a globally callable dashboard that scans all package submodules in parallel (using a Python thread pool of 50 workers) and outputs clean, color-coded monospace Markdown tables. It has **zero hardcoded dependencies** on desktop environments or projects, making it fully universal.

### The Three Modes:
1. **Daily Todo Sync Status (Default):** Highlights any package out-of-sync in your downstream pipeline. It hides all "In Sync" packages, giving you a focused, high-signal task list of needed pulls, pushes, and merges.
2. **Upstream Release Monitor (`--version` / `-v`):** Queries **release-monitoring.org (Anitya)** in parallel (using `curl` in a subprocess to bypass anti-scraper challenges). It compares your local `factory` spec versions against upstream **`stable_version`**, and your `next` spec versions against upstream **`version`** (unstable/latest), identifying what needs to be updated.
3. **Release Staging Planner (`--forward` / `-f`):** Checks which `next` branches carry unique staging commits ahead of `factory` and are structurally ready to be cleanly forwarded to `factory` (or warns if `next` needs a merge first).

### 🛠️ Installation:
Save the script as `check-sync` in your local path (e.g., `~/bin/check-sync`) and make it executable:

```bash
chmod +x ~/bin/check-sync
```

---

## 🔀 Part 2: Automated Background Merge Drivers

Standard Git line-by-line merging frequently breaks on package metadata during branch merges (e.g., merging `factory` stable bugfixes into `next`). These custom merge drivers run **silently in the background** on `git merge` or `git pull`, completely eliminating merge conflicts for `.changes`, `.spec`, `_service`, and `.obsinfo` files!

### 1. The `.changes` Semantic Resolver (`resolve-changes`)
* **The Problem:** Both branches append entries to the top of the `.changes` file. When histories diverge, Git gets confused and splits the conflict markers into multiple scrambled blocks, corrupting the changelog. Furthermore, if a stable fix has a *newer* timestamp than an unstable release, simple date sorting puts the older stable version above the newer unstable version, violating openSUSE's strict chronological rules.
* **The Solution:** A Python script that uses Git's **three-stage merge index** (`:1:` for ancestor, `:2:` for `next/ours`, and `:3:` for `factory/theirs`) to resolve the conflict structurally based on branch hierarchy:
  * **Top Block:** `[New next/unstable entries]` (always on top).
  * **Middle Block:** `[New factory/stable entries]` (below unstable).
  * **Bottom Block:** `[Common Ancestor shared history]` (de-duplicated).
  * **Automated Redating:** If both branches have new entries, it **automatically redates the new `next` entries to `now`** (using POSIX `%e` double-space padding), ensuring 100% compliance with openSUSE's chronological validation rules while maintaining their internal ordering!
  * **De-duplication:** Automatically parses description bodies and **discards duplicate entries caused by parallel cherry-picks** between branches!

### 2. The `.spec` Smart Resolver (`resolve-spec`)
* **The Problem:** Merges from `factory` to `next` always conflict on the `Version:` line since `next` is a higher development version.
* **The Solution:** A custom script that runs Git's standard `git merge-file` first. If a conflict is detected **solely on the `Version:` line**, it automatically resolves it in favor of your active `next` branch. **If any other complex conflict is detected (such as a patch being added in factory), it safely exits with a conflict**, preserving manual verification.

### 3. The `keep-ours` Driver (For `_service` and `*.obsinfo`)
* **The Problem:** Your `_service` and `.obsinfo` files on `next` are configured for unstable development streams and should never adopt `factory`'s stable versions.
* **The Solution:** A standard shell `true` driver. Because Git populates `%A` (Ours) with your clean `next` file before calling the driver, a driver of `true` (which immediately exits `0` and does nothing) forces Git to accept your clean `next` file as the merged result!

---

## 🛡️ Part 3: The `post-merge` Auto-Amending Hook

* **The Problem:** During a merge, any new stable point-release tarball (e.g. `libadwaita-1.9.3.tar.xz`) added in `factory` does not exist in `next`. Because there is no file of that name in `next` to create a "content conflict", **Git cleanly merges and stages the stable tarball into your unstable `next` commit history automatically**, which is packaging-wise incorrect.
* **The Solution:** A **`post-merge` Git hook** that runs automatically right after the merge commit is created:
  1. It reads the local `.spec` file `Version:` field.
  2. It scans the newly created merge commit using `git diff-tree --name-status -r -m HEAD` (filtering by status `A` and `M` so it ignores deleted files).
  3. If it detects any staged tarball whose filename version does not match the `.spec` version, **it unstages and deletes it (`git rm -f`)**.
  4. It automatically removes the `.git/MERGE_HEAD` transaction lock and runs **`git commit --amend --no-edit`**, completely and silently wiping the stable tarball from your workspace and git commit history in milliseconds!

---

## ⚙️ Step-by-Step Global System Configuration

To activate this entire automation engine globally across **all** of your Git packaging repositories:

### Step 1: Install the Executables
Save the two companion scripts as `resolve-changes` and `resolve-spec` in your global `~/bin/` folder and make them executable:

```bash
mkdir -p ~/bin
# [Copy resolve-changes, resolve-spec, and check-sync into ~/bin/]
chmod +x ~/bin/resolve-changes ~/bin/resolve-spec ~/bin/check-sync
```

### Step 2: Register the Drivers in your Global `~/.gitconfig`
Run these commands to define the three merge drivers globally:

```bash
# 1. Register the .changes driver
git config --global merge.merge-changes.name "openSUSE changes file merge driver"
git config --global merge.merge-changes.driver "/home/$USER/bin/resolve-changes %O %A %B %P"

# 2. Register the .spec driver
git config --global merge.spec-merge.name "openSUSE spec file merge driver"
git config --global merge.spec-merge.driver "/home/$USER/bin/resolve-spec %O %A %B %P"

# 3. Register the keep-ours driver (for _service & .obsinfo)
git config --global merge.keep-ours.name "Keep Ours merge driver"
git config --global merge.keep-ours.driver "true"
```

### Step 3: Map files to Drivers in `~/.config/git/attributes`
Append these mappings to your global git attributes file. (*Note: This perfectly matches Gitea's pre-defined `merge-changes` repository attributes!*):

```bash
mkdir -p ~/.config/git
git config --global core.attributesfile "~/.config/git/attributes"

echo "*.changes merge=merge-changes" >> ~/.config/git/attributes
echo "*.spec merge=spec-merge" >> ~/.config/git/attributes
echo "_service merge=keep-ours" >> ~/.config/git/attributes
echo "*.obsinfo merge=keep-ours" >> ~/.config/git/attributes
```

### Step 4: Install the Global `post-merge` Hook
Create your global user hooks directory, copy the `post-merge` script into it, and register the path:

```bash
mkdir -p ~/.config/git/hooks
# [Copy the post-merge script into ~/.config/git/hooks/post-merge]
chmod +x ~/.config/git/hooks/post-merge

git config --global core.hooksPath ~/.config/git/hooks
```

---

## 🎯 Cheat Sheet: Daily Usage

Once configured, your maintenance workflow simplifies to this:

| Task / Command | What Happens Under the Hood |
| :--- | :--- |
| **`check-sync`** | Prints a gorgeous parallel "todo list" of packages needing pulls, pushes, or merges. Hides clean ones. |
| **`check-sync -v`** | Compares your local spec versions against upstream release-monitoring.org to spot package updates. |
| **`check-sync -f`** | Finds which `next` branches are ready to be cleanly forwarded to `factory` (or need merging first). |
| **`git merge origin/factory`** | **The Magic Command:** `.changes` is merged, sorted, de-duplicated, and redated; `Version:` conflicts on `.spec` are auto-resolved; `_service` and `.obsinfo` are preserved; stable tarballs are stripped from history. **All of it merges cleanly in <1s!** |
