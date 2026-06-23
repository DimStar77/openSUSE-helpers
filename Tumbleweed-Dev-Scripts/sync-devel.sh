
#!/bin/bash
set -e

# Start ssh-agent and add SSH keys so we are only prompted for the passphrase once
eval "$(ssh-agent -s)" >/dev/null
trap "ssh-agent -k >/dev/null" EXIT

if ! ssh-add; then
    echo "Failed to add SSH key. Exiting."
    exit 1
fi

pushd ~/Documents/src.o.o/Factory/pkgs/_meta

git pull
~dimstar/Documents/git-rw/openSUSE-release-tools/devel_update.sh sync
# to be replaced by
# ~dimstar/Documents/git-rw/openSUSE-release-tools/devel_update.sh syncnewpackages
~dimstar/Downloads/test.sh

if ! git diff --quiet; then
    git commit -a -m "Devel sync $(date --iso-8601)"
    git push
else
    echo "No changes to commit."
fi

# git obs pr create --title "Devel sync $(date --iso-8601)"

popd
