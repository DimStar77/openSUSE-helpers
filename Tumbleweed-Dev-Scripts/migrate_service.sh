#!/bin/bash

fail() {
    echo -e "\n[ERROR] $1"
    exit 1
}

# 1. Dependency Check
command -v xmlstarlet >/dev/null 2>&1 || fail "xmlstarlet is not installed."
command -v osc >/dev/null 2>&1 || fail "osc is not installed."

echo "--- Starting Migration ---"

# 2. Update _service file
if [ -f "_service" ]; then
    echo "[1/6] Updating _service file..."
    xmlstarlet ed -L \
        -u "//service[@name='tar']/@mode" -v "manual" \
        -u "//service[@name='recompress']/@mode" -v "manual" \
        -u "//service[@name='recompress']/param[@name='compression']" -v "xz" \
        "_service"

    if xmlstarlet sel -t -v "//service[@name='cargo_vendor']/@name" "_service" &> /dev/null; then
        xmlstarlet ed -L \
            -d "//service[@name='cargo_vendor']/param[@name='compression']" \
            -s "//service[@name='cargo_vendor']" -t elem -n "param" -v "xz" \
            -i "//service[@name='cargo_vendor']/param[not(@name)][last()]" -t attr -n "name" -v "compression" \
            "_service"
    fi
fi

# 3. Update .spec file
SPEC_FILE=$(ls *.spec 2>/dev/null | head -n 1)
if [ -n "$SPEC_FILE" ]; then
    echo "[2/6] Updating $SPEC_FILE (zst -> xz)..."
    sed -i 's/\.tar\.zst/.tar.xz/g' "$SPEC_FILE"
fi

# 4. Handle .gitignore
echo "[3/6] Updating .gitignore..."
grep -q "osc-collab.*" .gitignore 2>/dev/null || echo "osc-collab.*" >> .gitignore

# 5. Cleanup Stale Artifacts
echo "[4/6] Cleaning up stale .zst and .obscpio files..."
# Remove the old main tarball and vendor tarball if they were tracked
git rm -f *.tar.zst *.obscpio 2>/dev/null

# 6. OSC Service Run
echo "[5/6] Running osc service mr..."
if ! osc service mr; then
    fail "osc service mr failed! Check for cargo_vendor or network issues."
fi

# Cleanup intermediate obscpio files left by the manual run
# These are no longer needed now that the .tar.xz is generated
git rm -f *.obscpio 2>/dev/null

# 7. Idempotency Check
CHANGES=$(git status --porcelain . | grep -v "_changes")

if [ -z "$CHANGES" ]; then
    echo "--- No changes detected. Package is already up to date. ---"
    exit 0
fi

# 8. Finalize Metadata
echo "[6/6] Changes detected. Updating changelog and staging..."
osc vc -m 'Migrate to xz compression and manual service run'

# Add control files
git add _service "$SPEC_FILE" .gitignore 2>/dev/null

# Add ONLY files in the current directory (tarballs and changes)
find . -maxdepth 1 -type f \( -name "*.tar.xz" -o -name "*.changes" \) -exec git add {} +

echo -e "\n--- Migration Complete ---"
git status
