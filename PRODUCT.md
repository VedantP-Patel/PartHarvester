# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary user is a high-speed PC gamer and repack collector on Windows downloading large multi-part game archives (e.g., FitGirl Repacks, 10–80 parts of 1.95–2.0 GB each, total 40–150 GB) across single or bonded multiple network connections (e.g., home Wi-Fi combined with 5G USB phone tethering). They want maximum bandwidth utilization, zero tedious manual tab clicking or captcha loops, immediate visual clarity over active downloads, and automatic extraction upon completion.

## Product Purpose

PartHarvester automates and accelerates the entire multi-part repack download lifecycle. It eliminates manual link management by ingesting and decrypting encrypted pastebins in under 1 second, bypasses Cloudflare-protected hosters via Chrome TLS impersonation, aggregates all active local network adapters into a unified download pipe via local socket binding, balances concurrency slots dynamically using EWMA throughput, detects straggling parts and steals work onto idle fast connections, and unpacks the finished split archives using 7-Zip into a clean, ready-to-install folder.

## Positioning

Unlike conventional browser download managers or generic torrent clients:
1. **Zero-Knowledge 1-Click Ingestion:** Native client-side AES-256-GCM decryption of `paste.fitgirl-repacks.site/?id#key` in under a second with zero copy-pasting of individual URLs.
2. **True Hardware Combinational Net:** Outsmarting Windows single-gateway route metric limitation by binding worker sockets directly to distinct physical and tethered IPv4 adapters (`CURLOPT_INTERFACE`), safely multiplying bandwidth across distinct public IP rate-limit budgets.
3. **TurboBond Autonomous Scheduling:** Continuous EWMA throughput tracking, adaptive concurrency slot allocation with anti-429 per-IP caps, and autonomous straggler work-stealing via HTTP Range resumption.
4. **Standalone Local Zero-Build Execution:** Pure Python FastAPI + native HTML5/CSS3/Vanilla JS web interface with sub-second WebSocket telemetry, runnable instantly via 1-click batch launcher without Node.js, npm, or heavy framework builds.

## Operating Context

- **Environment:** Windows 10/11 desktop/laptop, standard 1080p (1920x1080) to 1440p displays, running locally at `http://127.0.0.1:5000`.
- **Operating Scene:** Active while gaming or downloading in the background. The user wants a mission-control, high-density HUD visible at a glance without having to scroll down to check active part status, throughput, or remaining time.
- **Tools Involved:** Web browser (Chrome, Edge, Firefox), local Python 3.15 runtime, 7-Zip 64-bit CLI (`7z.exe`), and connected physical network adapters (Ethernet, Wi-Fi, USB 5G Tethering).

## Capabilities and Constraints

- **Capabilities:**
  - Automated PrivateBin v2 payload retrieval, Base58 key decoding, compact JSON AAD verification, and zlib decompression.
  - FuckingFast HTMX POST `/f/{id}/go` protocol traversal, JA4 Chrome fingerprint impersonation, direct `dl.fuckingfast.co` binary stream extraction, and 404 dead link detection.
  - DataNodes and Real-Debrid API unrestrictor support.
  - Resumable HTTP Range streaming with sparse `.part` checkpointing.
  - Dynamic network adapter enumeration via `psutil`, EWMA speed calculations, and adaptive quota distribution.
  - Autonomous straggler work-stealing watchdog and manual 1-click "⚡ Switch Net" per-task adapter hand-off.
  - Automated regex multi-part sequence completeness verification and background multithreaded 7-Zip extraction (`7z.exe x -y -mmt=on`).
- **Constraints:**
  - Pure zero-npm standalone stack: all frontend assets must remain vanilla HTML5, CSS3, and JavaScript served statically from FastAPI.
  - Mission-control single-screen vertical density: critical metrics (bandwidth, adapter health, active streams, and download table) must fit seamlessly within a standard viewport without unnecessary vertical scrolling.
  - Zero disruption to active verified download pipelines.

## Brand Commitments

- **Name:** PartHarvester
- **Identity:** High-tech, mission-control cyber-utility. Clean obsidian dark ground with electric neon accents (cyan, emerald, amber, rose). Crisp monospaced data readouts, compact geometric forms, and high-performance tactile feedback.
- **Voice:** Direct, precise, technical, transparent. No cartoonish gamification; clear telemetry and honest error diagnostics (e.g. flagging hoster 404s distinctly).

## Evidence on Hand

- Verified live PrivateBin AES-256-GCM decryptor (`resolvers/privatebin.py`).
- Verified live FuckingFast HTMX resolver extracting 1.95 GB / 2.00 GB streams with Range 206 (`resolvers/fuckingfast.py`).
- Verified multi-adapter bonding engine and EWMA bandwidth tracker (`engine/network.py`).
- Verified autonomous work-stealing downloader (`engine/downloader.py`).
- Complete 14/14 automated pytest test suite (`tests/`).

## Product Principles

1. **Every Byte Visible at a Glance:** The operator must never hunt through tabs or scroll past oversized banners to see their real-time speeds, active connections, or queue progress.
2. **Zero Ingestion Friction:** Pasting one URL must immediately queue all 10–80 parts with true filenames and sizes; no manual captcha clicking or link extraction.
3. **Bandwidth Maximization Without Host Bans:** Distribute load intelligently across separate physical network adapters and distinct public IPs, enforcing safe per-IP caps while aggregating total throughput.
4. **Never Restart a Download:** Every byte downloaded to a `.part` file is preserved; pausing, adapter switching, and error retries always resume via HTTP Range.
5. **Zero-Build Reliability:** Keep the local web interface entirely self-contained in standard HTML, CSS, and vanilla JS so it runs instantly on any Windows machine with zero build steps or external package managers.
