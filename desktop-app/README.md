# ⚡ PartHarvester v2 — Native Desktop Edition

Native cross-platform (**Windows + Linux**) desktop application powered by **Rust (`tokio` + `wreq` + SQLite WAL)**, **Tauri v2**, and **Svelte 5**.

---

## 👤 For Normal Users

If you don't want to install Rust, Node.js, or C++ build tools:
- Download the pre-built **Windows installer / portable `.exe`** or **Linux `.AppImage` / `.deb`** from **[GitHub Releases](https://github.com/VedantP-Patel/PartHarvester/releases)**, OR
- Use the **Python Web Edition** (`../web-edition/run.bat` on Windows, `../web-edition/run.sh` on Linux/macOS).

Settings and queue state are automatically stored in your OS AppData directory (`%APPDATA%\PartHarvester` on Windows, `~/.local/share/PartHarvester` on Linux), and archives unpack automatically via **7-Zip** or **WinRAR / UnRAR**.

---

## 🛠️ For Developers

### Prerequisites

**Windows:**
- [Node.js LTS](https://nodejs.org/) (`node` + `npm`)
- [Rust](https://rustup.rs/) (`cargo`)
- [CMake](https://cmake.org/), [LLVM/Clang](https://releases.llvm.org/), and [NASM](https://www.nasm.us/) (required by BoringSSL/`btls-sys` for TLS fingerprint emulation)
- Visual Studio C++ Build Tools (MSVC) or MSYS2 UCRT64 (`gcc`/`g++`)
- Winget helper:
  ```powershell
  winget install OpenJS.NodeJS.LTS Rustlang.Rustup Kitware.CMake LLVM.LLVM NASM.NASM
  ```

**Linux (Ubuntu / Debian):**
```bash
sudo apt update && sudo apt install -y \
  build-essential cmake clang nasm pkg-config \
  libwebkit2gtk-4.1-dev libappindicator3-dev librsvg2-dev patchelf nodejs npm
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

### 1-Click Build & Launch
- **Windows**: `desktop-app\run.bat`
- **Linux**: `./desktop-app/run.sh`

### Manual Commands
```bash
# Build UI
npm --prefix desktop-app/ui install
npm --prefix desktop-app/ui run build

# Run Rust engine unit tests
cargo test --manifest-path desktop-app/Cargo.toml -p core-engine

# Build release desktop binary
cargo build --manifest-path desktop-app/Cargo.toml --release -p partharvester-tauri --features custom-protocol
```
