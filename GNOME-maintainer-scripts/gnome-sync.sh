#!/bin/bash
# gnome-sync.sh - Workspace synchronizer for GNOME and GNOME:Next

# Resolve script directory (following symlinks if any)
SOURCE=${BASH_SOURCE[0]}
while [ -L "$SOURCE" ]; do
    DIR=$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)
    SOURCE=$(readlink "$SOURCE")
    [[ $SOURCE != /* ]] && SOURCE=$DIR/$SOURCE
done
SCRIPT_DIR=$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)

# Locate git-project-sync engine
SYNC_CMD=""
if [ -x "$SCRIPT_DIR/../git-helpers/git-project-sync" ]; then
    SYNC_CMD="$SCRIPT_DIR/../git-helpers/git-project-sync"
elif command -v git-project-sync >/dev/null 2>&1; then
    SYNC_CMD=$(command -v git-project-sync)
else
    echo "Error: git-project-sync not found in '$SCRIPT_DIR/../git-helpers' or PATH." >&2
    exit 1
fi

FACTORY_DIR="GNOME"
NEXT_DIR="GNOME:Next"

exec "$SYNC_CMD" "$@" "$FACTORY_DIR" "$NEXT_DIR"
