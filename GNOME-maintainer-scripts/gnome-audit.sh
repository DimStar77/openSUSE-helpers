#!/bin/bash
# gnome-audit.sh

FACTORY_DIR="GNOME"
NEXT_DIR="GNOME:Next"

# 1. Setup options
CHECK_PRS_VAL="false"
while getopts "p" opt; do
  case $opt in
    p) CHECK_PRS_VAL="true" ;;
    *) echo "Usage: $0 [-p]"; exit 1 ;;
  esac
done

BASE_DIR=$(pwd)
FACTORY_PATH="$BASE_DIR/$FACTORY_DIR"
NEXT_PATH="$BASE_DIR/$NEXT_DIR"

if [[ ! -d "$FACTORY_PATH" || ! -d "$NEXT_PATH" ]]; then
    echo "❌ Error: Missing directories."
    exit 1
fi

echo "----------------------------------------------------------------------------------------------------"
echo "🔍 Auditing: Next vs. Factory $( [ "$CHECK_PRS_VAL" = "true" ] && echo "(+ OBS PR Check)" )"
echo "----------------------------------------------------------------------------------------------------"
printf "%-35s | %-15s | %-20s | %s\n" "Submodule Path" "Status" "Delta" "Pending OBS PRs"
echo "----------------------------------------------------------------------------------------------------"

cd "$NEXT_PATH" || exit

# 2. Execution
RAW_OUTPUT=$(git submodule foreach --quiet "
    case \"\$sm_path\" in
        gnome-next-pkglist|gnome-next.x86_64|obs-service-pkg_version)
          exit 0
          ;;
    esac
    NEXT_HASH=\$(git rev-parse HEAD 2>/dev/null)
    FACTORY_HASH=\$(git -C \"$FACTORY_PATH\" ls-tree factory \"\$sm_path\" | awk '{print \$3}')
    
    STATUS_STR=\"\"
    D=\"--\"
    PR_INFO=\"--\"

    # Identify Status
    if [ -z \"\$FACTORY_HASH\" ]; then
        STATUS_STR=\"NEW\"
        D=\"Added in Next\"
    elif [ \"\$NEXT_HASH\" != \"\$FACTORY_HASH\" ]; then
        AHEAD=\$(git rev-list --count \"\$FACTORY_HASH..\$NEXT_HASH\" 2>/dev/null || echo 0)
        BEHIND=\$(git rev-list --count \"\$NEXT_HASH..\$FACTORY_HASH\" 2>/dev/null || echo 0)

        if [ \"\$AHEAD\" -gt 0 ] && [ \"\$BEHIND\" -gt 0 ]; then 
            STATUS_STR=\"OUT OF SYNC\"; D=\"+\$AHEAD/-\$BEHIND\"
        elif [ \"\$AHEAD\" -gt 0 ]; then 
            STATUS_STR=\"PENDING PUSH\"; D=\"\$AHEAD ahead\"
        elif [ \"\$BEHIND\" -gt 0 ]; then 
            STATUS_STR=\"BEHIND\"; D=\"\$BEHIND behind\"
        else
            STATUS_STR=\"DIVERGED\"; D=\"Manual Check\"
        fi
    fi

    # PR Logic (Only run if Status is NOT empty)
    if [ -n \"\$STATUS_STR\" ] && [ \"$CHECK_PRS_VAL\" = \"true\" ]; then
        RID=\$(git remote get-url origin 2>/dev/null | sed -E 's/.*[:\/]([^\/]+\/[^\/]+)(\.git)?$/\1/')
        PR_DATA=\$(git obs pr list --state open \"\$RID\" 2>/dev/null)
        
        if [ -n \"\$PR_DATA\" ] && echo \"\$PR_DATA\" | grep -q \"ID  \"; then
            PR_INFO=\$(echo \"\$PR_DATA\" | awk '
                /^ID/ { match(\$0, /#[0-9]+/); id=substr(\$0, RSTART, RLENGTH); }
                /^Target/ { 
                    split(\$0, a, \"branch: \"); split(a[2], b, \",\"); 
                    if (id!=\"\") { printf \"%s(%s) \", id, b[1]; id=\"\"; }
                }
            ' | sed 's/ $//; s/ /, /g')
        fi
    fi

    # Output Formatting
    if [ -n \"\$STATUS_STR\" ]; then
        case \"\$STATUS_STR\" in
            \"NEW\")          COL=\"\033[0;32m\" ;;
            \"PENDING PUSH\") COL=\"\033[0;33m\" ;;
            \"BEHIND\")       COL=\"\033[0;31m\" ;;
            \"OUT OF SYNC\")  COL=\"\033[0;35m\" ;;
            *)                COL=\"\033[0;35m\" ;;
        esac

        [ -z \"\$PR_INFO\" ] && PR_INFO=\"--\"
        [ \"\$PR_INFO\" != \"--\" ] && PR_DISPLAY=\"\033[0;36m\$PR_INFO\033[0m\" || PR_DISPLAY=\"--\"

        printf \"%-35s | %b%-15s\033[0m | %-20s | %b\n\" \"\$sm_path\" \"\$COL\" \"\$STATUS_STR\" \"\$D\" \"\$PR_DISPLAY\"
    fi
")

# 3. Final Output & Summary
SORTED_RESULTS=$(echo "$RAW_OUTPUT" | sort)
echo "$SORTED_RESULTS"

echo "----------------------------------------------------------------------------------------------------"
echo "📈 TOTAL TO-DO LIST:"

# wc -l is back to stay. xargs strips whitespace.
N_COUNT=$(echo "$SORTED_RESULTS" | grep "NEW" | wc -l | xargs)
P_COUNT=$(echo "$SORTED_RESULTS" | grep "PENDING PUSH" | wc -l | xargs)
O_COUNT=$(echo "$SORTED_RESULTS" | grep "OUT OF SYNC" | wc -l | xargs)
B_COUNT=$(echo "$SORTED_RESULTS" | grep "BEHIND" | wc -l | xargs)

printf -- "- New for Factory:    %d\n" "$N_COUNT"
printf -- "- Pending Promotions: %d\n" "$P_COUNT"
printf -- "- Out of Sync:        %d (Needs Merge)\n" "$O_COUNT"
printf -- "- Behind Factory:     %d (Needs Merge)\n" "$B_COUNT"
echo "----------------------------------------------------------------------------------------------------"
