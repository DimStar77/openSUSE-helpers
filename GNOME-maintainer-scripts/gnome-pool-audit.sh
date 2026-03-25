#!/bin/bash
# gnome-pool-audit.sh

# Configuration
GNOME_FACTORY_DIR="GNOME"
BASE_DIR=$(pwd)
FACTORY_PATH="$BASE_DIR/$GNOME_FACTORY_DIR"

if [[ ! -d "$FACTORY_PATH" ]]; then
    echo "❌ Error: GNOME Factory directory not found."
    exit 1
fi

echo "--------------------------------------------------------------------------------"
echo "🔍 Auditing GNOME Org vs. Pool (Upstream Source)"
echo "--------------------------------------------------------------------------------"
printf "%-35s | %-15s | %s\n" "Submodule" "Status" "Commits Ahead of Pool"
echo "--------------------------------------------------------------------------------"

cd "$FACTORY_PATH" || exit

# Use foreach to check every submodule in the Factory parent
RAW_OUTPUT=$(git submodule foreach --quiet '
    # 1. Get current local Factory hash
    LOCAL_FACTORY_HASH=$(git rev-parse HEAD 2>/dev/null)
    
    # 2. Determine Pool URL 
    # Logic: Replace "GNOME" with "pool" in the origin URL
    ORIGIN_URL=$(git remote get-url origin 2>/dev/null)
    POOL_URL=$(echo "$ORIGIN_URL" | sed "s/\/GNOME\//\/pool\//")

    # 3. Get the Pool Factory hash via ls-remote (Network call)
    # This gets the hash without needing to add a remote or fetch
    POOL_FACTORY_HASH=$(git ls-remote "$POOL_URL" factory 2>/dev/null | awk "{print \$1}")

    if [ -z "$POOL_FACTORY_HASH" ]; then
        printf "%-35s | \033[0;35mUNKNOWN\033[0m    | Pool repo or branch not found\n" "$sm_path"
    elif [ "$LOCAL_FACTORY_HASH" != "$POOL_FACTORY_HASH" ]; then
        # Check if we are ahead (contains commits pool doesnt have)
        AHEAD_COUNT=$(git rev-list --count "$POOL_FACTORY_HASH..$LOCAL_FACTORY_HASH" 2>/dev/null || echo 0)
        BEHIND_COUNT=$(git rev-list --count "$LOCAL_FACTORY_HASH..$POOL_FACTORY_HASH" 2>/dev/null || echo 0)

        if [ "$AHEAD_COUNT" -gt 0 ] && [ "$BEHIND_COUNT" -gt 0 ]; then
             printf "%-35s | \033[0;35mDIVERGED\033[0m   | +%s / -%s\n" "$sm_path" "$AHEAD_COUNT" "$BEHIND_COUNT"
        elif [ "$AHEAD_COUNT" -gt 0 ]; then
             printf "%-35s | \033[0;33mAHEAD\033[0m      | %s commits ahead of Pool\n" "$sm_path" "$AHEAD_COUNT"
        elif [ "$BEHIND_COUNT" -gt 0 ]; then
             printf "%-35s | \033[0;32mSTALE\033[0m      | %s behind Pool (Needs sync from Pool)\n" "$sm_path" "$BEHIND_COUNT"
        fi
    fi
')

echo "$RAW_OUTPUT" | sort

# Summary logic using the robust wc -l method
echo "--------------------------------------------------------------------------------"
echo "📈 POOL SYNC SUMMARY:"
AHEAD_TOTAL=$(echo "$RAW_OUTPUT" | grep "AHEAD" | wc -l | xargs)
STALE_TOTAL=$(echo "$RAW_OUTPUT" | grep "STALE" | wc -l | xargs)
DIV_TOTAL=$(echo "$RAW_OUTPUT" | grep "DIVERGED" | wc -l | xargs)

printf -- "- Ahead of Pool:  %d (Local patches exist)\n" "$AHEAD_TOTAL"
printf -- "- Behind Pool:   %d (Updates available in Pool)\n" "$STALE_TOTAL"
printf -- "- Diverged:      %d (Manual conflict check needed)\n" "$DIV_TOTAL"
echo "--------------------------------------------------------------------------------"
