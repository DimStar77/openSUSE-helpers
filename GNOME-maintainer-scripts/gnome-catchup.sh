#!/bin/bash
# gnome-catchup.sh (Lives in the parent directory)

FACTORY_DIR="GNOME"
NEXT_DIR="GNOME:Next"

BASE_DIR=$(pwd)
FACTORY_PATH="$BASE_DIR/$FACTORY_DIR"
NEXT_PATH="$BASE_DIR/$NEXT_DIR"

if [[ ! -d "$FACTORY_PATH" || ! -d "$NEXT_PATH" ]]; then
    echo "❌ Error: Missing directories."
    exit 1
fi

echo "-------------------------------------------------------------------"
echo "🔄 GNOME Submodule Catch-up: Factory -> Next"
echo "-------------------------------------------------------------------"

# We go into NEXT_DIR to find the culprits
cd "$NEXT_PATH" || exit
export FACTORY_PATH

# Find all BEHIND submodules
BEHIND_MODULES=$(git submodule foreach --quiet '
    NEXT_HASH=$(git rev-parse HEAD 2>/dev/null)
    FACTORY_HASH=$(git -C "$FACTORY_PATH" ls-tree factory "$sm_path" | awk "{print \$3}")
    
    if [ -n "$FACTORY_HASH" ] && [ "$NEXT_HASH" != "$FACTORY_HASH" ]; then
        BEHIND_VAL=$(git rev-list --count "$NEXT_HASH..$FACTORY_HASH" 2>/dev/null)
        BEHIND_COUNT=${BEHIND_VAL:-0}
        
        if [ "$BEHIND_COUNT" -gt 0 ]; then
            echo "$sm_path|$FACTORY_HASH|$BEHIND_COUNT"
        fi
    fi
')

if [ -z "$BEHIND_MODULES" ]; then
    echo "✅ No submodules are behind Factory. You are all caught up!"
    exit 0
fi

# Iterate over the findings
for entry in $BEHIND_MODULES; do
    IFS='|' read -r sm_path factory_hash count <<< "$entry"
    
    echo ""
    echo "📂 Submodule: $sm_path"
    echo "⚠️  Behind Factory by $count commit(s)."
    echo "-------------------------------------------------------------------"
    
    # Show the log of what is in Factory but not in Next
    # We use -C to run the log inside the specific submodule
    git -C "$sm_path" log --oneline --color HEAD.."$factory_hash"
    
    echo "-------------------------------------------------------------------"
    read -p "Merge these changes into 'next' and push? [y/N]: " confirm
    
    if [[ "$confirm" =~ ^[Yy]$ ]]; then
        echo "🚀 Merging..."
        # 1. Ensure we are on 'next' branch
        git -C "$sm_path" checkout next --quiet
        
        # 2. Merge the specific factory hash
        if git -C "$sm_path" merge "$factory_hash" -m "Merge factory updates into next"; then
            echo "📤 Pushing to origin/next..."
            git -C "$sm_path" push origin next
            echo "✅ Successfully updated $sm_path"
        else
            echo "❌ Conflict detected in $sm_path! Manual intervention required."
        fi
    else
        echo "⏭️  Skipping $sm_path"
    fi
done

echo ""
echo "--- Catch-up Session Complete ---"
