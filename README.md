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

### Prerequisites
- **Python 3.12+ / 3.15** (64-bit)
- **7-Zip** (64-bit) installed at `C:\Program Files\7-Zip\7z.exe` (for automated unpacking)

### 1-Click Launch (Windows)
Simply double-click:
```cmd
run.bat
```
This starts the local FastAPI server and automatically opens your web browser to:
**`http://localhost:5000`**

### Manual Launch (Terminal)
```powershell
cd d:\Projects\multi-part-downloader
py main.py
```

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

```powershell
cd d:\Projects\multi-part-downloader
$env:PYTHONPATH = "."
py -m pytest tests
```

---

## 📂 Project Architecture

```
multi-part-downloader/
├── README.md               # User manual and overview
├── docs/
│   ├── ARCHITECTURE.md     # Deep-dive technical specifications & cryptography
│   └── API.md              # REST API and WebSocket protocol reference
├── engine/
│   ├── downloader.py       # Async resumable chunked engine
│   ├── network.py          # Network interface bonding & discovery
│   ├── session.py          # Session persistence (download_session.json)
│   └── task.py             # Pydantic v2 data models & state machine
├── resolvers/
│   ├── datanodes.py        # DataNodes form handler & unrestrictor
│   ├── debrider.py         # Real-Debrid API client
│   ├── fuckingfast.py      # FuckingFast direct stream extractor
│   ├── generic.py          # PixelDrain, MediaFire, and direct links
│   ├── manager.py          # Universal router & text parser
│   └── privatebin.py       # PrivateBin v2 AES-256-GCM client decryptor
├── extractor/
│   └── unpacker.py         # Multi-part sequence validator & 7-Zip runner
├── server/
│   └── app.py              # FastAPI server & WebSocket telemetry
├── web/
│   ├── index.html          # Modern glassmorphism single-page app
│   ├── style.css           # Styling & micro-animations
│   └── app.js              # Real-time WebSocket state manager
├── tests/
│   ├── test_downloader.py  # Range & session tests
│   ├── test_network.py     # Interface binding tests
│   ├── test_privatebin.py  # Decryption verification tests
│   └── test_unpacker.py    # Multi-part sequence tests
├── main.py                 # Application entry point
├── run.bat                 # 1-click Windows launcher
└── requirements.txt        # Python dependencies
```

---

## 📜 License
MIT License. Built for personal speed, convenience, and high-performance game repack management.

