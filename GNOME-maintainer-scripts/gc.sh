#!/bin/bash
# gc.sh - The Complete OBS-to-Git Commit Tool

# --- 1. THE ADD/REMOVE PHASE ---
# Equivalent to 'osc addremove'
git add -A .

# --- 2. THE PRE-CHECK PHASE ---
CHANGES_FILE=$(ls *.changes 2>/dev/null | head -n 1)
if [ -z "$CHANGES_FILE" ]; then
    echo "❌ ERROR: No .changes file found."
    exit 1
fi

# Check if .changes actually has staged changes
if ! git diff --cached --name-only | grep -q "$(basename "$CHANGES_FILE")"; then
    echo "⚠️  WARNING: .changes file has no staged changes. Did you run 'osc vc'?"
    read -p "Continue anyway? [y/N]: " confirm
    [[ "$confirm" =~ ^[Yy]$ ]] || exit 1
fi

# --- 3. THE EXTRACTION PHASE ---
RAW_ENTRY=$(awk '/^----------------------------------/ { c++; next } c==1 { print } c==2 { exit }' "$CHANGES_FILE")
CONTENT=$(echo "$RAW_ENTRY" | sed '1d' | sed -e :a -e '/^\n*$/{$d;N;ba' -e '}')

if [ -z "$CONTENT" ]; then
    echo "❌ ERROR: Could not extract changelog content."
    exit 1
fi

# --- 4. THE SMART FORMATTING PHASE ---
FIRST_LINE=$(echo "$CONTENT" | grep "^-" | head -n 1)
SUMMARY=$(echo "$FIRST_LINE" | sed 's/^- //' | sed 's/:[[:space:]]*$//')

if [[ "$FIRST_LINE" =~ "Update to version" ]]; then
    # Version update: Summary + full body (minus the version line)
    BODY=$(echo "$CONTENT" | sed '1d' | sed -e :a -e '/^\n*$/{$d;N;ba' -e '}')
else
    # Regular fix: Summary + everything else
    BODY=$(echo "$CONTENT" | sed '1d')
fi

# --- 5. THE COMMIT PHASE ---
TMP_MSG=$(mktemp)
{
    echo "$SUMMARY"
    echo ""
    [ -n "$BODY" ] && echo "$BODY"
} > "$TMP_MSG"

# Launch editor (Vim Tip: Use 'dG' to wipe body if it is too long!)
git commit -e -F "$TMP_MSG"

rm -f "$TMP_MSG"
