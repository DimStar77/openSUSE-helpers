#!/bin/bash
# gnome-promote.sh

FACTORY_DIR="GNOME"
NEXT_DIR="GNOME:Next"

BASE_DIR=$(pwd)
FACTORY_PATH="$BASE_DIR/$FACTORY_DIR"
NEXT_PATH="$BASE_DIR/$NEXT_DIR"

if [[ ! -d "$FACTORY_PATH" || ! -d "$NEXT_PATH" ]]; then
    echo "❌ Error: Missing directories."
    exit 1
fi

cd "$NEXT_PATH" || exit

echo "-------------------------------------------------------------------"
echo "🚀 GNOME Bulk Promotion: Next -> Factory"
echo "-------------------------------------------------------------------"

# Discovery Logic (Matches Audit exactly)
PROMOTABLE=$(git submodule foreach --quiet "
    NEXT_HASH=\$(git rev-parse HEAD 2>/dev/null)
    FACTORY_HASH=\$(git -C \"$FACTORY_PATH\" ls-tree factory \"\$sm_path\" | awk '{print \$3}')
    
    if [ -n \"\$FACTORY_HASH\" ] && [ \"\$NEXT_HASH\" != \"\$FACTORY_HASH\" ]; then
        AHEAD=\$(git rev-list --count \"\$FACTORY_HASH..\$NEXT_HASH\" 2>/dev/null || echo 0)
        BEHIND=\$(git rev-list --count \"\$NEXT_HASH..\$FACTORY_HASH\" 2>/dev/null || echo 0)
        
        # Only promote if we are strictly ahead
        if [ \"\$AHEAD\" -gt 0 ] && [ \"\$BEHIND\" -eq 0 ]; then
             RID=\$(git remote get-url origin 2>/dev/null | sed -E 's/.*[:\/]([^\/]+\/[^\/]+)(\.git)?$/\1/')
             # Double-check for existing PRs
             PR_CHECK=\$(git obs pr list --state open \"\$RID\" 2>/dev/null | grep 'ID  ')
             
             if [ -z \"\$PR_CHECK\" ]; then
                 echo \"\$sm_path|\$AHEAD|\$FACTORY_HASH\"
             fi
        fi
    fi
")

if [ -z "$PROMOTABLE" ]; then
    echo "✅ No submodules are eligible for promotion (all have PRs or are in-sync)."
    exit 0
fi

for entry in $PROMOTABLE; do
    IFS='|' read -r sm_path count f_hash <<< "$entry"
    
    echo ""
    echo "📦 Submodule: $sm_path ($count new commits)"
    echo "-------------------------------------------------------------------"
    # --no-pager prevents the script from hanging on a 'less' screen
    git --no-pager -C "$sm_path" log --oneline --reverse --color "$f_hash..HEAD"
    echo "-------------------------------------------------------------------"
    
    read -p "Create PR for $sm_path to Factory? [y/N/q]: " confirm
    
    if [[ "$confirm" =~ ^[Qq]$ ]]; then
        exit 0
    elif [[ "$confirm" =~ ^[Yy]$ ]]; then
        echo "📤 Creating PR..."
        # Create the PR targeting the 'factory' branch
        (cd "$sm_path" && git obs pr create --target-branch factory)
    else
        echo "⏭️  Skipping $sm_path"
    fi
done
