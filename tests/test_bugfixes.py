"""Regression tests for reliability, concurrency, failover, and UI fixes."""

import asyncio
import os
import threading
from unittest.mock import MagicMock, patch
import pytest

from engine.autotune import SmartAutoTuner
from engine.downloader import AsyncDownloader, RangeNotSupportedOrExpiredError
from engine.journal import IntervalJournal
from engine.network import NetworkAdapter, NetworkManager
from engine.task import TaskStatus
from resolvers.fuckingfast import FuckingFastResolver
from resolvers.manager import LinkManager


@pytest.mark.anyio
async def test_fuckingfast_http200_cf_challenge_does_not_raise_unbound_local():
    """HTTP 200 Cloudflare interstitial must not trigger UnboundLocalError on body_head."""
    class _FakeResp:
        def __init__(self, status_code, text, headers=None):
            self.status_code = status_code
            self.text = text
            self.headers = headers or {}

    calls = []

    class _FakeSess:
        _closed = False

        async def get(self, url, headers=None, timeout=15):
            calls.append(url)
            if len(calls) == 1:
                return _FakeResp(200, "<html><head><title>Just a moment...</title></head></html>")
            return _FakeResp(200, '<button hx-post="/f/abc123/go">Download</button>')

        async def post(self, url, headers=None, timeout=15, allow_redirects=False):
            return _FakeResp(200, "", headers={"hx-redirect": "https://dl.fuckingfast.co/dl/abc123"})

        async def close(self):
            self._closed = True

    fake_sess = _FakeSess()
    with patch.object(FuckingFastResolver, "_get_session", return_value=fake_sess), patch.object(
        FuckingFastResolver, "_evict_session"
    ) as mock_evict:
        direct_url, filename, size = await FuckingFastResolver.resolve(
            "https://fuckingfast.co/abc123#Game.part01.rar"
        )
        assert direct_url == "https://dl.fuckingfast.co/dl/abc123"
        assert filename == "Game.part01.rar"
        assert size == 0
        mock_evict.assert_called_once()


def test_journal_closed_prevents_recreating_meta_after_cleanup(tmp_path):
    """After cleanup(), delayed writes must not recreate .part.meta on disk."""
    part = str(tmp_path / "archive.part01.rar.part")
    j = IntervalJournal(part, 1024 * 1024)
    j.add_range(0, 4095, flush_interval=0.0)
    assert os.path.exists(j.meta_path)

    j.cleanup()
    assert not os.path.exists(j.meta_path)

    j.add_range(4096, 8191, flush_interval=0.0)
    j.save_sync(force=True)
    assert not os.path.exists(j.meta_path)


def test_pause_task_outside_running_event_loop(tmp_path):
    """pause_task must flush journal synchronously instead of raising RuntimeError when no loop runs."""
    d = AsyncDownloader(LinkManager(), NetworkManager(), MagicMock())
    t = d.add_task("https://example.com/game.part01.rar", str(tmp_path))
    t.file_size = 1024 * 1024
    part = os.path.join(str(tmp_path), "game.part01.rar.part")
    j = d._get_journal(t, part)
    j.add_range(0, 1023, flush_interval=1e9)
    if os.path.exists(j.meta_path):
        os.remove(j.meta_path)
    t.status = TaskStatus.DOWNLOADING

    errors = []
    def worker():
        try:
            assert d.pause_task(t.id) is True
        except Exception as exc:
            errors.append(exc)

    th = threading.Thread(target=worker)
    th.start()
    th.join()

    assert not errors
    assert t.status == TaskStatus.PAUSED
    assert os.path.exists(j.meta_path)


def test_network_ewma_decays_when_stalled_with_stale_samples():
    """EWMA speed must decay after >2s without new chunks even before the 8s sample window empties."""
    mgr = NetworkManager(ewma_alpha=0.5)
    with patch.object(mgr, "_scan_hardware_interfaces", return_value=[("Wi-Fi", "192.168.1.50", True, 100, "Wi-Fi")]):
        mgr.record_chunk_transfer("192.168.1.50", 20 * 1024 * 1024)
        initial_ewma = mgr._ewma_speeds["192.168.1.50"]
        assert initial_ewma > 10 * 1024 * 1024

        mgr._last_ewma_update["192.168.1.50"] -= 5.0
        adapters = mgr.get_available_adapters()
        assert len(adapters) == 1
        assert adapters[0].ewma_speed_bytes < initial_ewma * 0.6


def test_autotune_does_not_penalize_byte_offsets_containing_200():
    """Error messages with byte counts containing '200' should not trigger heavy socket pressure penalty."""
    tuner = SmartAutoTuner()
    tuner.record_stream_error("10.0.0.5", "Segment 0 failed at 200000 bytes")
    assert tuner._penalty_by_ip["10.0.0.5"] == 1

    tuner.record_stream_error("10.0.0.6", "Server returned HTTP 200 instead of 206")
    assert tuner._penalty_by_ip["10.0.0.6"] == 2


@pytest.mark.anyio
async def test_range_not_supported_falls_back_cleanly_to_single_stream(tmp_path):
    """If origin ignores Range even after URL refresh, engine cleans up sparse .part and finishes single-stream."""
    d = AsyncDownloader(LinkManager(), NetworkManager(), MagicMock())
    d.segments_per_file = 4
    d.min_segment_file_size_mb = 1

    t = d.add_task("https://example.com/no_range.rar", str(tmp_path))
    t.direct_url = "https://cdn.example.com/no_range.rar"
    t.file_size = 30 * 1024 * 1024
    t.supports_range = True
    t.status = TaskStatus.DOWNLOADING

    seg_calls = 0
    single_calls = []

    async def fake_segmented(task, ip, target_segments=None):
        nonlocal seg_calls
        seg_calls += 1
        part_path = os.path.join(task.destination_dir, f"{task.filename}.part")
        d._preallocate_file(part_path, task.file_size)
        d._get_journal(task, part_path)
        raise RangeNotSupportedOrExpiredError("Server returned HTTP 200 instead of 206")

    async def fake_refresh(task, ip, use_cache=False):
        task.direct_url = "https://cdn.example.com/no_range.rar?fresh=1"

    async def fake_single(task, ip):
        single_calls.append(os.path.exists(os.path.join(task.destination_dir, f"{task.filename}.part")))
        task.downloaded_bytes = task.file_size
        task.status = TaskStatus.COMPLETED

    d._download_segmented_stream = fake_segmented
    d._resolve_fresh_url = fake_refresh
    d._download_single_stream = fake_single

    await d._download_stream(t, "127.0.0.1")

    assert seg_calls == 2
    assert single_calls == [False], "Sparse preallocated .part file must be removed before single-stream fallback"
    assert t.supports_range is False
    assert t.status == TaskStatus.COMPLETED
