#!/usr/bin/env bash

# Exit on error
set -e

# Parse arguments
VERBOSE=false
for arg in "$@"; do
    case $arg in
        -v|--verbose)
            VERBOSE=true
            ;;
    esac
done

# Setup output descriptors
if [ "$VERBOSE" = true ]; then
    exec 3>&1
    exec 4>&2
else
    exec 3>/dev/null
    exec 4>/dev/null
fi

# Print helper functions
info() {
    echo -e "\033[1;32m=>\033[0m $@"
}

warn() {
    echo -e "\033[1;33m[!]\033[0m $@"
}

error() {
    echo -e "\033[1;31m[Error]\033[0m $@" >&2
}

# Resolve directory of the install.sh script
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

info "Checking system dependencies..."

MISSING_PACKAGES=()

# 1. Python dependency and GObject Introspection checks
python3 -c "
import rpm
" >&3 2>&4 || MISSING_PACKAGES+=("python3-rpm")

python3 -c "
import requests
" >&3 2>&4 || MISSING_PACKAGES+=("python3-requests")

python3 -c "
import gi
" >&3 2>&4 || MISSING_PACKAGES+=("python3-gobject")

python3 -c "
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk
" >&3 2>&4 || MISSING_PACKAGES+=("typelib-1_0-Gtk-4_0")

python3 -c "
import gi
gi.require_version('Adw', '1')
from gi.repository import Adw
" >&3 2>&4 || MISSING_PACKAGES+=("typelib-1_0-Adw-1")

python3 -c "
import gi
gi.require_version('Vte', '3.91')
from gi.repository import Vte
" >&3 2>&4 || MISSING_PACKAGES+=("typelib-1_0-Vte-3_91")

# 2. Check for Gitea CLI (tea) package
TEA_INSTALLED=false
if command -v tea >/dev/null 2>&1; then
    TEA_INSTALLED=true
elif command -v rpm >/dev/null 2>&1; then
    if rpm -q gitea-tea >&3 2>&4; then
        TEA_INSTALLED=true
    fi
fi

if [ "$TEA_INSTALLED" = false ]; then
    MISSING_PACKAGES+=("gitea-tea")
fi

# 3. Handle missing packages
if [ ${#MISSING_PACKAGES[@]} -ne 0 ]; then
    echo ""
    info "The following missing system dependencies are required for Geckopit:"
    for pkg in "${MISSING_PACKAGES[@]}"; do
        echo "  - $pkg"
    done
    echo ""

    if command -v zypper >/dev/null 2>&1; then
        # Inform/ask the user about the execution steps
        echo "We will run the following command using sudo to install them:"
        echo "  sudo zypper in -y ${MISSING_PACKAGES[*]}"
        echo ""
        read -p "Would you like to proceed with installing these packages? [y/N]: " confirm
        if [[ "$confirm" =~ ^[yY](es)?$ ]]; then
            info "Running zypper to install dependencies..."
            if [ "$VERBOSE" = true ]; then
                sudo zypper in "${MISSING_PACKAGES[@]}"
            else
                sudo zypper in -y "${MISSING_PACKAGES[@]}" >/dev/null
            fi
        else
            warn "Skipped package installation. Please install them manually."
        fi
    else
        warn "zypper command not found. Please install the packages listed above manually using your package manager."
    fi
else
    info "All system dependencies are already installed!"
fi

# 4. Local binary symlinking
info "Setting up local command-line symlinks..."
mkdir -p "$HOME/bin"

ln -sf "${SCRIPT_DIR}/geckopit.py" "$HOME/bin/geckopit"
ln -sf "${SCRIPT_DIR}/geckopit-cli" "$HOME/bin/geckopit-cli"
ln -sf "${SCRIPT_DIR}/geckopit-upgrade" "$HOME/bin/geckopit-upgrade"

# Check if ~/bin is in PATH
if [[ ":$PATH:" != *":$HOME/bin:"* ]]; then
    warn "$HOME/bin is not in your PATH environment variable."
    warn "To run geckopit from your terminal, add this to your ~/.bashrc or ~/.zshrc:"
    warn "  export PATH=\$HOME/bin:\$PATH"
fi

# 5. Desktop & Icon integration
info "Installing desktop launcher and HD icon..."
mkdir -p "$HOME/.local/share/applications"
cp "${SCRIPT_DIR}/org.opensuse.geckopit.desktop" "$HOME/.local/share/applications/"

mkdir -p "$HOME/.local/share/icons/hicolor/scalable/apps"
cp "${SCRIPT_DIR}/org.opensuse.geckopit.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/org.opensuse.geckopit.svg"

# Update desktop and icon database if tools are available
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$HOME/.local/share/applications" >&3 2>&4 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >&3 2>&4 || true
fi

info "Setup completed successfully! Enjoy Geckopit SCM Cockpit."
