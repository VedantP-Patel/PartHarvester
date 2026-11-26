# ⚡ PartHarvester

> **High-Performance Multi-Part Repack Batch Downloader with Combinational Net Aggregator**

PartHarvester is an asynchronous, zero-lag download manager built specifically to eliminate the frustration of downloading multi-part game repacks (such as FitGirl releases). It handles encrypted pastebins, resolves Cloudflare-protected hosters, aggregates bandwidth across multiple internet connections simultaneously, and automatically unpacks completed archives.

---

## 🌟 Key Features

### 1. 🔑 1-Click PrivateBin Decryption (`paste.fitgirl-repacks.site`)
- Paste the full `paste.fitgirl-repacks.site/?id#key` URL directly into PartHarvester.
- The engine fetches the encrypted JSON payload and performs **hardware-accelerated AES-256-GCM** client-side decryption using the `#key` fragment in memory.
- All 10–80 multi-part links are imported and queued in under a second—no manual opening of tabs or copying lines required.

### 2. 🌐 Combinational Net & TurboBond (Multi-Adapter Bonding & Intelligent Scheduling)
- Standard Windows networking routes 100% of outbound traffic through only one default adapter (e.g. Ethernet).
- PartHarvester discovers all physical and tethered network adapters on your PC:
  - **Wi-Fi** (Home Broadband)
  - **USB Phone Tethering** (Mobile 4G/5G data)
  - **Ethernet / LAN**
- Binds individual download worker sockets directly to local interface IPs (`CURLOPT_INTERFACE`), **summing your bandwidth together**:
  $$\text{Total Speed} = \text{Wi-Fi (50 Mbps)} + \text{USB 5G (100 Mbps)} = \mathbf{150\text{ Mbps}}$$
- **⚡ Adaptive Concurrency Quotas**: Continuously computes an Exponentially Weighted Moving Average (EWMA) of throughput per interface. Faster adapters (e.g. 100 Mbps USB 5G) dynamically receive more concurrency slots, while slower connections (e.g. 20 Mbps Wi-Fi) get fewer slots to prevent bufferbloat.
- **🛡️ Anti-429 Clamping**: Limits connections per public IP (default max 3) to prevent hoster rate-limit bans while multiplying total throughput safely across distinct IPs.
- **🚀 Autonomous Straggler Work-Stealing**: When a fast connection finishes its assigned files and has idle slots while a slower connection is still grinding through a large 2.0 GB part, the watchdog automatically checkpoints the download and hands it off to the fast connection, resuming via HTTP Range.
- **⚡ Manual 1-Click Network Switch**: Any active download can be instantly handed off to any connected network adapter via the quick dropdown in the downloads table.
- **🏎️ Pipelined Background Pre-Resolving**: Resolves direct URLs and tokens 5 links ahead in the background so transfers begin instantaneously with zero waiting between parts.

### 3. 🛡️ Native Resolvers & Anti-Bot Bypass
- **`fuckingfast.co`**: Automatically extracts the direct `https://dl.fuckingfast.co/dl/...` binary stream using `curl_cffi` with Chrome JA4/TLS fingerprint impersonation, completely avoiding the common trap where standard downloaders accidentally save small `.htm` files.
- **`datanodes.to`**: Handles free `op=download2` form tokens automatically.
- **Real-Debrid API**: Optional 1-click token integration to un-restrict 1fichier, Rapidgator, and DataNodes links into uncapped gigabit CDN streams with zero wait timers or captchas.
- **PixelDrain & MediaFire**: Direct streaming via API and direct button scraping.

### 4. 🔄 Resumable Range Streaming
- Downloads stream into `.part` files using HTTP Range headers (`Range: bytes={existing}-`).
- If your connection drops or you pause the app, downloads resume byte-for-byte from where they left off—**never restarting a 2 GB file from 0%**.

### 5. 📦 Automated Multi-Part Archive Extraction
- Detects split sequences (`part01.rar` ... `part15.rar`, `game.7z.001`, `fg-01.bin`).
- Verifies that all parts 1 through $N$ are 100% downloaded and intact.
- Automatically launches multithreaded 7-Zip (`7z.exe -mmt=on`) in the background to unpack the clean installer directory.

### 6. 🎨 Modern Glassmorphism Dashboard
- FastAPI backend with sub-second WebSocket telemetry.
- Real-time speedometer, per-adapter live bandwidth breakdown, per-part progress bars, and queue controls.
- Zero build tools required—runs directly with instant startup.

---

## 🚀 Quick Start Guide

PartHarvester is available in **two full-featured editions** (both share identical core features: PrivateBin decryption, multi-adapter Combinational Net, smart auto-tuning, resumable range streaming, `.sfv` verification, file deletion from disk, and 7-Zip auto-extraction):

| Edition | Best For | Platforms | Launch |
| :--- | :--- | :--- | :--- |
| **Native Desktop (`desktop-app/`)** | Fastest lock-free Rust engine, SQLite WAL, native taskbar progress | Windows, Linux | Download from **Releases** or run `desktop-app\run.bat` / `./desktop-app/run.sh` |
| **Web Edition (`web-edition/`)** | Zero-compile browser UI (`http://localhost:5000`), Python only | Windows, Linux, macOS | Run `web-edition\run.bat` / `./web-edition/run.sh` |

### 👤 For Normal Users (Zero Build Tools)
1. **Native Desktop App**: Download the latest pre-built Windows `.exe` or Linux `.AppImage`/`.deb` from **GitHub Releases** and run it directly.
2. **Web Edition (Lightweight)**:
   - Install **Python 3.11+** (and **7-Zip** for auto-extraction).
   - **Windows**: Double-click `web-edition\run.bat`
   - **Linux / macOS**: Run `./web-edition/run.sh`
   - Your browser opens automatically to `http://localhost:5000`.

### 🛠️ For Developers (Building Desktop App from Source)
- **Windows prerequisites**:
  ```powershell
  winget install OpenJS.NodeJS.LTS Rustlang.Rustup Kitware.CMake LLVM.LLVM NASM.NASM
  ```
  Then double-click `desktop-app\run.bat` (auto-builds `ui` + `partharvester-tauri.exe`).
- **Linux (Ubuntu/Debian) prerequisites**:
  ```bash
  sudo apt update && sudo apt install -y build-essential cmake clang nasm pkg-config \
    libwebkit2gtk-4.1-dev libappindicator3-dev librsvg2-dev patchelf nodejs npm
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
  ```
  Then run `./desktop-app/run.sh`.

See [desktop-app/README.md](desktop-app/README.md) for detailed desktop architecture and developer commands.

---

## 📖 How to Download a FitGirl Repack

1. Go to any FitGirl Repack page (e.g. *Phantom Blade Zero*).
2. Under **Download Mirrors**, copy either:
   - The paste link: `https://paste.fitgirl-repacks.site/?99e460e2438a1459#36CYUjqDBywuhLVUUoEbSyT6RaWpC7bLkE2qDHTuKrpFV`
   - Or open the paste, copy all the `fuckingfast.co` or `datanodes.to` links, and copy them to your clipboard.
3. Open **PartHarvester** at `http://localhost:5000`.
4. Paste the link into the **Add Download Links** text area.
5. Choose your destination folder (defaults to `Downloads\PartHarvester`).
6. Click **🚀 Ingest Links & Start Download**.
7. Sit back and watch your connections download all parts concurrently at maximum speed!
8. When all parts reach 100%, PartHarvester will automatically un-rar the files into a ready-to-install folder.

---

## 🌐 Setting Up Combinational Net (Wi-Fi + Phone 5G USB Tethering)

To combine your Home Wi-Fi and Phone's 5G Mobile Data:
1. Connect your PC to your home Wi-Fi network.
2. Connect your Android or iPhone to your PC using a USB cable.
3. On your phone, go to **Settings > Hotspot & Tethering > USB Tethering** (turn it ON).
4. Windows will detect a new network adapter (Remote NDIS).
5. Open PartHarvester: both **Wi-Fi** and **USB Tethering** will appear in the **Combinational Net** card with green checkboxes.
6. PartHarvester will automatically split the downloads across both connections in parallel, summing your speeds together!

---

## ⚙️ Real-Debrid Configuration (Optional)

If you have a Real-Debrid account:
1. Copy your API token from [real-debrid.com/apitoken](https://real-debrid.com/apitoken).
2. In PartHarvester, click **⚙️ Settings** in the top-right corner.
3. Paste your token into the **Real-Debrid API Token** field and click **Save Settings**.
4. Now, any 1fichier, Rapidgator, or DataNodes links will be converted into direct, max-speed CDN streams with zero wait times.

---

## 🧪 Running the Test Suite

**Web Edition (Python):**
```powershell
cd web-edition
PYTHONPATH=. pytest tests
```

**Desktop App (Rust Core Engine):**
```powershell
cargo test --manifest-path desktop-app/Cargo.toml -p core-engine
```

---

## 📂 Project Architecture

```
PartHarvester/
├── README.md                  # Quick start, feature overview & repo map
├── .github/workflows/         # CI tests & automated Windows/Linux releases
├── docs/
│   ├── ARCHITECTURE.md        # Technical specifications & cryptography
│   ├── API.md                 # REST API & WebSocket protocol reference
│   ├── DESIGN.md              # UI/UX & system design notes
│   └── PRODUCT.md             # Product requirements & roadmap
├── web-edition/               # Python + FastAPI Web Edition (Win/Linux/macOS)
│   ├── engine/                # Async resumable engine & network bonding
│   ├── resolvers/             # PrivateBin, FuckingFast, DataNodes, Real-Debrid
│   ├── extractor/             # Multi-part sequence validator & 7-Zip runner
│   ├── server/                # FastAPI server & WebSocket telemetry
│   ├── web/                   # Browser dashboard UI
│   ├── tests/                 # Pytest suite
│   ├── main.py                # Entry point
│   ├── requirements.txt       # Python dependencies
│   ├── run.bat                # 1-click Windows launcher
│   └── run.sh                 # 1-click Linux/macOS launcher
└── desktop-app/               # Native Desktop Edition (Rust + Tauri v2 + Svelte 5)
    ├── README.md              # Desktop setup & build guide
    ├── Cargo.toml             # Rust workspace (core-engine + tauri-app)
    ├── crates/
    │   ├── core-engine/       # Lock-free positional I/O, wreq TLS, SQLite WAL
    │   └── tauri-app/         # Native Tauri v2 desktop shell & taskbar progress
    ├── ui/                    # Svelte 5 + Vite frontend
    ├── run.bat                # 1-click Windows build & launcher
    └── run.sh                 # 1-click Linux build & launcher
```

---

## 📜 License
MIT License. Built for personal speed, convenience, and high-performance game repack management.

