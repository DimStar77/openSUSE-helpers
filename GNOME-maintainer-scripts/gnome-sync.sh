#!/bin/bash
# gnome-sync.sh - High Performance Superproject Workspace Synchronizer

JOBS=16
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

# Setup persistent OpenSSH connection multiplexing to eliminate handshake and TCP setup overhead
SSH_MUX_DIR=$(mktemp -d "${TMPDIR:-/tmp}/ssh-mux-XXXXXX")
export GIT_SSH_COMMAND="ssh -o ControlMaster=auto -o ControlPath=${SSH_MUX_DIR}/%C -o ControlPersist=5m"
cleanup_ssh() {
    rm -rf "${SSH_MUX_DIR}"
}
trap cleanup_ssh EXIT INT TERM

sync_project() {
    local dir=$1
    echo "--- Synchronizing $dir ---"
    if [ ! -d "$dir" ]; then
        echo "Directory $dir not found, skipping."
        return
    fi

    cd "$dir" || return

    # 1. Fetch superproject metadata quietly
    git fetch origin --prune --quiet

    CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
    REMOTE_HASH=$(git rev-parse "origin/$CURRENT_BRANCH" 2>/dev/null)
    LOCAL_HASH=$(git rev-parse HEAD 2>/dev/null)

    # 2. Logic: Only sync if hashes differ OR if user forced it
    if [ "$REMOTE_HASH" == "$LOCAL_HASH" ] && [ "$FORCE_SYNC" = false ]; then
        echo "✅ No changes detected on $CURRENT_BRANCH. (Use -f to force audit)"
    else
        if [ "$FORCE_SYNC" = true ]; then
            echo "🔄 Force audit requested. Deep-scanning 500+ submodules with SSH multiplexing..."
            git reset --hard "origin/$CURRENT_BRANCH"
            git submodule sync --recursive --quiet
            git submodule update --init --recursive --jobs "$JOBS"

            echo "🌿 Re-attaching submodules to branch: $CURRENT_BRANCH"
            git submodule foreach --quiet --recursive "
                git -C \"\$toplevel\" config submodule.\$name.branch $CURRENT_BRANCH
                git checkout -q $CURRENT_BRANCH 2>/dev/null || true
            "

            echo "🚀 Pulling latest changes for 500+ submodules (Parallel)..."
            git submodule update --remote --merge --jobs "$JOBS"
            git clean -dff
        else
            # Targeted differential sync: identify submodules actually updated by the bot!
            CHANGED_SUBMODULES=$(git diff --name-only "$LOCAL_HASH" "$REMOTE_HASH")
            COUNT=$(echo "$CHANGED_SUBMODULES" | grep -v '^[[:space:]]*$' | wc -l)

            if [ "$COUNT" -gt 0 ]; then
                echo "🚀 Bot update detected ($COUNT package(s) changed: $(echo $CHANGED_SUBMODULES | tr '\n' ' ')). Fast-syncing..."
                git reset --hard "origin/$CURRENT_BRANCH"
                git submodule sync --quiet -- $CHANGED_SUBMODULES
                git submodule update --init --jobs "$JOBS" -- $CHANGED_SUBMODULES

                for sm in $CHANGED_SUBMODULES; do
                    if [ -d "$sm" ]; then
                        (cd "$sm" && git checkout -q "$CURRENT_BRANCH" 2>/dev/null || true)
                    fi
                done
            else
                echo "🚀 Superproject metadata updated ($LOCAL_HASH -> $REMOTE_HASH). Fast-syncing..."
                git reset --hard "origin/$CURRENT_BRANCH"
            fi
        fi
    fi

    cd ..
}

sync_project "$FACTORY_DIR"
sync_project "$NEXT_DIR"
