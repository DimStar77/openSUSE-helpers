#!/bin/bash
# gnome-sync.sh

JOBS=10
FACTORY_DIR="GNOME"
NEXT_DIR="GNOME:Next"

# Check for force flag (-f)
FORCE_SYNC=false
while getopts "f" opt; do
  case $opt in
    f) FORCE_SYNC=true ;;
    *) echo "Usage: $0 [-f]"; exit 1 ;;
  esac
done

sync_project() {
    local dir=$1
    echo "--- Synchronizing $dir ---"
    if [ ! -d "$dir" ]; then 
        echo "Directory $dir not found, skipping."
        return 
    fi

    cd "$dir" || return
    
    # 1. Fetch metadata quietly
    git fetch --all --prune --quiet
    
    CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
    REMOTE_HASH=$(git rev-parse "origin/$CURRENT_BRANCH")
    LOCAL_HASH=$(git rev-parse HEAD)

    # 2. Logic: Only sync if hashes differ OR if user forced it
    if [ "$REMOTE_HASH" == "$LOCAL_HASH" ] && [ "$FORCE_SYNC" = false ]; then
        echo "✅ No changes detected on $CURRENT_BRANCH. (Use -f to force audit)"
    else
        if [ "$FORCE_SYNC" = true ]; then
            echo "🔄 Force audit requested. Deep-scanning 500+ submodules..."
        else
            echo "🚀 Bot update detected ($LOCAL_HASH -> $REMOTE_HASH). Syncing..."
        fi

        # Snap parent to the Bot's state
        git reset --hard "origin/$CURRENT_BRANCH"
        
        # 3. Submodule Sync & Branch Re-attachment
        # Ensure submodules are synced and initialized at the superproject's recorded commits
        git submodule sync --recursive --quiet
        git submodule update --init --recursive --jobs $JOBS
        
        echo "🌿 Re-attaching submodules to branch: $CURRENT_BRANCH"
        # 1. Set submodules to track the parent's branch
        # 2. Checkout the branch to avoid detached HEAD
        git submodule foreach --quiet --recursive "
            git -C \"\$toplevel\" config submodule.\$name.branch $CURRENT_BRANCH
            git checkout -q $CURRENT_BRANCH 2>/dev/null || true
        "

        # 3. Parallel pull/update to the tip of the branch (equivalent to git pull for all submodules)
        echo "🚀 Pulling latest changes for 500+ submodules (Parallel)..."
        git submodule update --remote --merge --jobs $JOBS

        # This is the "Safety Valve": it cleans up folders of submodules 
        # that were removed from the index, effectively doing what --prune intended.
        git clean -dff
    fi

    cd ..
}
sync_project "$FACTORY_DIR"
sync_project "$NEXT_DIR"
