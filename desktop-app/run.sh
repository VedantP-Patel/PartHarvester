#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

BIN="target/release/partharvester-tauri"
if [ ! -x "$BIN" ]; then
    echo "=========================================================="
    echo "  PartHarvester Desktop - First-Time Release Build (Linux)"
    echo "=========================================================="

    MISSING=()
    for cmd in node npm cargo cmake nasm clang pkg-config; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            MISSING+=("$cmd")
        fi
    done

    if command -v pkg-config >/dev/null 2>&1; then
        if ! pkg-config --exists webkit2gtk-4.1 2>/dev/null; then
            MISSING+=("libwebkit2gtk-4.1-dev")
        fi
    fi

    if [ ${#MISSING[@]} -gt 0 ]; then
        echo "[ERROR] Missing build dependencies: ${MISSING[*]}"
        echo ""
        echo "If you are a NORMAL USER (no build tools needed):"
        echo "  1. Download the pre-built .AppImage/.deb from GitHub Releases, OR"
        echo "  2. Run the Python Web Edition:  ../web-edition/run.sh"
        echo ""
        echo "If you are a DEVELOPER on Ubuntu/Debian:"
        echo "  sudo apt update && sudo apt install -y build-essential cmake clang nasm pkg-config \\"
        echo "    libwebkit2gtk-4.1-dev libappindicator3-dev librsvg2-dev patchelf nodejs npm"
        echo "  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
        exit 1
    fi

    if [ ! -f "ui/dist/index.html" ]; then
        echo "Building Svelte 5 UI..."
        npm --prefix ui install
        npm --prefix ui run build
    fi

    echo "Building native desktop executable..."
    cargo build --release -p partharvester-tauri --features custom-protocol
fi

exec "$BIN"
