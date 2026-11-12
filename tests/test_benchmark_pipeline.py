"""End-to-End Multi-Segment Download Integrity & Throughput Benchmark."""

import asyncio
import hashlib
import os
import time
from unittest.mock import MagicMock
import pytest

from engine.downloader import AsyncDownloader
from engine.network import NetworkAdapter, NetworkManager
from engine.task import TaskStatus
from resolvers.manager import LinkManager


class _FakeRangeResponse:
    """Simulates a high-speed HTTP 206 Partial Content stream from memory."""

    def __init__(self, full_payload: bytes, range_header: str):
        self.headers = {"content-type": "application/octet-stream"}
        if range_header.startswith("bytes="):
            spec = range_header.split("=")[1]
            start_s, end_s = spec.split("-")
            start = int(start_s)
            end = int(end_s) if end_s else len(full_payload) - 1
            self._slice = full_payload[start : end + 1]
            self.status_code = 206
            self.headers["content-range"] = f"bytes {start}-{end}/{len(full_payload)}"
            self.headers["content-length"] = str(len(self._slice))
        else:
            self._slice = full_payload
            self.status_code = 200
            self.headers["content-length"] = str(len(full_payload))

    async def aiter_content(self, chunk_size: int = 65536):
        # Yield in 64 KB network packets to exercise the segment write buffer
        step = 65536
        for offset in range(0, len(self._slice), step):
            yield self._slice[offset : offset + step]
            await asyncio.sleep(0)

    async def aclose(self):
        pass


class _FakeSession:
    def __init__(self, payload: bytes):
        self._payload = payload
        self._closed = False

    async def get(self, url: str, headers: dict = None, stream: bool = True, timeout: int = 30):
        range_hdr = (headers or {}).get("Range", "")
        return _FakeRangeResponse(self._payload, range_hdr)

    async def close(self):
        self._closed = True


@pytest.mark.anyio
async def test_segmented_download_integrity_and_throughput(tmp_path):
    """Verifies 8-segment parallel download assembles a 100% bit-exact file at high speed."""
    # Generate 16 MB deterministic pseudo-random binary payload
    payload_size = 16 * 1024 * 1024
    pattern = os.urandom(64 * 1024)
    expected_payload = pattern * (payload_size // len(pattern))
    expected_sha256 = hashlib.sha256(expected_payload).hexdigest()

    net_mgr = NetworkManager()
    a_wifi = NetworkAdapter(
        name="Wi-Fi",
        ip_address="192.168.1.10",
        friendly_type="Wi-Fi",
        is_up=True,
        is_enabled=True,
        ewma_speed_bytes=60 * 1024 * 1024,
    )
    a_usb = NetworkAdapter(
        name="USB 5G",
        ip_address="192.168.42.10",
        friendly_type="USB Tethering",
        is_up=True,
        is_enabled=True,
        ewma_speed_bytes=120 * 1024 * 1024,
    )
    net_mgr.get_available_adapters = lambda: [a_wifi, a_usb]

    link_mgr = LinkManager()
    session_mgr = MagicMock()
    completed_tasks = []

    async def on_done(t):
        completed_tasks.append(t.id)

    downloader = AsyncDownloader(
        link_mgr, net_mgr, session_mgr, on_complete_callback=on_done
    )
    downloader.segments_per_file = 8
    downloader.buffer_size_kb = 512
    downloader.min_segment_file_size_mb = 2

    # Inject fake high-speed session
    fake_session = _FakeSession(expected_payload)
    downloader._get_session = AsyncMockSession(fake_session)

    task = downloader.add_task(
        "https://fuckingfast.co/bench#Benchmark_Repack.part01.rar",
        str(tmp_path),
    )
    task.direct_url = "https://dl.fuckingfast.co/dl/Benchmark_Repack.part01.rar"
    task.file_size = payload_size
    task.supports_range = True
    task.status = TaskStatus.DOWNLOADING

    t0 = time.perf_counter()
    await downloader._download_stream(task, "192.168.42.10")
    elapsed = time.perf_counter() - t0

    # Allow async on_complete task to finish
    await asyncio.sleep(0.02)

    final_file = tmp_path / "Benchmark_Repack.part01.rar"
    part_file = tmp_path / "Benchmark_Repack.part01.rar.part"

    assert task.status == TaskStatus.COMPLETED
    assert not part_file.exists(), ".part file should be atomically renamed on completion"
    assert final_file.exists(), "Final file must exist on disk"
    assert final_file.stat().st_size == payload_size

    actual_sha256 = hashlib.sha256(final_file.read_bytes()).hexdigest()
    assert actual_sha256 == expected_sha256, "SHA-256 mismatch across parallel segments!"
    assert completed_tasks == [task.id]
    assert elapsed < 2.0, f"Segmented assembly too slow: {elapsed:.2f}s"


class AsyncMockSession:
    def __init__(self, session):
        self._session = session

    async def __call__(self, adapter_ip):
        return self._session