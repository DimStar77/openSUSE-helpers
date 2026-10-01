#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

APP_ID="org.opensuse.geckopit"
MANIFEST="${APP_ID}.json"
BUILD_DIR="${SCRIPT_DIR}/.flatpak-build"
REPO_DIR="${SCRIPT_DIR}/.flatpak-repo"

ACTION="${1:-build}"

case "${ACTION}" in
    build)
        echo "==> Building ${APP_ID}..."
        flatpak-builder --force-clean "${BUILD_DIR}" "${MANIFEST}"
        echo "==> Build complete in ${BUILD_DIR}."
        echo "==> To install locally, run: $0 install"
        ;;
    install)
        echo "==> Building and installing ${APP_ID} for current user..."
        flatpak-builder --user --install --force-clean "${BUILD_DIR}" "${MANIFEST}"
        echo "==> Successfully installed ${APP_ID}!"
        echo "==> Run with: flatpak run ${APP_ID}"
        ;;
    run)
        echo "==> Launching ${APP_ID} via flatpak..."
        flatpak run "${APP_ID}"
        ;;
    shell)
        echo "==> Entering sandbox shell for ${APP_ID}..."
        flatpak run --command=bash "${APP_ID}"
        ;;
    bundle)
        echo "==> Exporting single-file offline bundle (${APP_ID}.flatpak)..."
        flatpak-builder --repo="${REPO_DIR}" --force-clean "${BUILD_DIR}" "${MANIFEST}"
        flatpak build-bundle "${REPO_DIR}" "${APP_ID}.flatpak" "${APP_ID}"
        echo "==> Bundle created: ${SCRIPT_DIR}/${APP_ID}.flatpak"
        ;;
    clean)
        echo "==> Cleaning build artifacts..."
        rm -rf "${BUILD_DIR}" "${REPO_DIR}" "${APP_ID}.flatpak" .flatpak-builder
        echo "==> Clean complete."
        ;;
    *)
        echo "Usage: $0 [build|install|run|shell|bundle|clean]"
        exit 1
        ;;
esac
