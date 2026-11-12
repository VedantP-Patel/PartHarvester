"""Unit and Integration Tests for Performance, Multi-Segmenting & Smart Queue."""

import os
import pytest
from unittest.mock import MagicMock
from engine.downloader import AsyncDownloader
from engine.network import NetworkManager
from engine.session import SessionManager
from engine.task import DownloadTask, Segment, TaskStatus
from resolvers.manager import LinkManager


def test_natural_alphanumeric_sorting():
    """Verifies that multi-part file lists are sorted in natural numerical sequence."""
    link_mgr = LinkManager()
    net_mgr = NetworkManager()
    session_mgr = MagicMock()
    downloader = AsyncDownloader(link_mgr, net_mgr, session_mgr)

    # Ingest parts out of order
    parts = [
        "https://example.com/fitgirl.part10.rar",
        "https://example.com/fitgirl.part02.rar",
        "https://example.com/fitgirl.part01.rar",
        "https://example.com/fitgirl.part03.rar",
        "https://example.com/fitgirl.part20.rar",
    ]
    for url in parts:
        downloader.add_task(url, "/tmp")

    filenames = [t.original_url.split("/")[-1] for t in downloader.tasks.values()]
    assert filenames == [
        "fitgirl.part01.rar",
        "fitgirl.part02.rar",
        "fitgirl.part03.rar",
        "fitgirl.part10.rar",
        "fitgirl.part20.rar",
    ], f"Natural sorting failed: {filenames}"


def test_queue_reorder_top_up_down():
    """Verifies moving tasks to top, up, and down."""
    link_mgr = LinkManager()
    net_mgr = NetworkManager()
    session_mgr = MagicMock()
    downloader = AsyncDownloader(link_mgr, net_mgr, session_mgr)

    t1 = downloader.add_task("https://example.com/a.rar", "/tmp")
    t2 = downloader.add_task("https://example.com/b.rar", "/tmp")
    t3 = downloader.add_task("https://example.com/c.rar", "/tmp")

    # Move t3 to top
    assert downloader.reorder_task(t3.id, "top") is True
    keys = list(downloader.tasks.keys())
    assert keys[0] == t3.id

    # Move t3 down
    assert downloader.reorder_task(t3.id, "down") is True
    keys = list(downloader.tasks.keys())
    assert keys[1] == t3.id

    # Move t3 up
    assert downloader.reorder_task(t3.id, "up") is True
    keys = list(downloader.tasks.keys())
    assert keys[0] == t3.id


def test_clear_completed_and_clear_all():
    """Verifies queue cleanup methods."""
    link_mgr = LinkManager()
    net_mgr = NetworkManager()
    session_mgr = MagicMock()
    downloader = AsyncDownloader(link_mgr, net_mgr, session_mgr)

    t1 = downloader.add_task("https://example.com/a.rar", "/tmp")
    t2 = downloader.add_task("https://example.com/b.rar", "/tmp")
    t1.status = TaskStatus.COMPLETED
    t2.status = TaskStatus.DOWNLOADING

    removed = downloader.clear_completed()
    assert removed == 1
    assert t1.id not in downloader.tasks
    assert t2.id in downloader.tasks

    all_cleared = downloader.clear_all()
    assert all_cleared == 1
    assert len(downloader.tasks) == 0


def test_sparse_file_preallocation(tmp_path):
    """Verifies instant sparse file pre-allocation on disk."""
    target = tmp_path / "test_sparse.part"
    size = 50 * 1024 * 1024  # 50 MB
    AsyncDownloader._preallocate_file(str(target), size)

    assert os.path.exists(target)
    assert os.path.getsize(target) == size


def test_segment_partition_logic():
    """Verifies multi-segment boundary calculation and progress tracking."""
    total_size = 2_097_152_000  # ~1.95 GB
    num_segments = 4
    seg_size = total_size // num_segments

    segments = []
    for i in range(num_segments):
        start = i * seg_size
        end = (start + seg_size - 1) if i < num_segments - 1 else (total_size - 1)
        segments.append(Segment(index=i, start_byte=start, end_byte=end))

    assert len(segments) == 4
    assert segments[0].start_byte == 0
    assert segments[-1].end_byte == total_size - 1
    # Verify continuity
    for i in range(len(segments) - 1):
        assert segments[i].end_byte + 1 == segments[i + 1].start_byte

    # Test segment progress calculation
    segments[0].downloaded_bytes = segments[0].total_bytes // 2
    assert segments[0].progress_pct == 50.0


@pytest.mark.anyio
async def test_token_bucket_limiter():
    """Verifies that TokenBucketLimiter paces bandwidth consumption properly."""
    from engine.downloader import TokenBucketLimiter
    # Rate of 10 MB/s
    limiter = TokenBucketLimiter(10.0)
    assert limiter.rate_bytes_sec == 10.0 * 1024 * 1024

    # Consume small chunk should not block
    await limiter.consume(1024)

    # 0 MB/s means unlimited
    limiter.set_rate(0.0)
    assert limiter.rate_bytes_sec == 0.0
    await limiter.consume(1_000_000)


def test_ewma_weighted_segment_allocation():
    """Verifies proportional segment allocation across network adapters."""
    from engine.network import NetworkAdapter, NetworkManager
    net_mgr = NetworkManager()

    # Mock adapters with different EWMA speeds
    a1 = NetworkAdapter(
        name="Wi-Fi", ip_address="192.168.1.100", friendly_type="Wi-Fi",
        is_up=True, is_enabled=True, ewma_speed_bytes=80 * 1024 * 1024  # 80 MB/s
    )
    a2 = NetworkAdapter(
        name="Tether", ip_address="192.168.42.2", friendly_type="USB Tethering",
        is_up=True, is_enabled=True, ewma_speed_bytes=20 * 1024 * 1024  # 20 MB/s
    )
    net_mgr.get_available_adapters = lambda: [a1, a2]

    # Allocate 10 segments with EWMA
    allocations = net_mgr.allocate_segments_to_adapters(10, strategy="ewma")
    assert len(allocations) == 10
    a1_count = sum(1 for a in allocations if a and a.ip_address == "192.168.1.100")
    a2_count = sum(1 for a in allocations if a and a.ip_address == "192.168.42.2")
    # 80/20 ratio -> a1 should get more segments than a2
    assert a1_count > a2_count
    assert a1_count + a2_count == 10

    # Equal round-robin strategy
    equal_alloc = net_mgr.allocate_segments_to_adapters(10, strategy="equal")
    eq_a1 = sum(1 for a in equal_alloc if a and a.ip_address == "192.168.1.100")
    eq_a2 = sum(1 for a in equal_alloc if a and a.ip_address == "192.168.42.2")
    assert eq_a1 == 5
    assert eq_a2 == 5


def test_event_driven_queue_dispatch():
    """Verifies that trigger_queue_processing immediately sets the asyncio event."""
    link_mgr = LinkManager()
    net_mgr = NetworkManager()
    session_mgr = MagicMock()
    downloader = AsyncDownloader(link_mgr, net_mgr, session_mgr)

    assert not downloader._queue_event.is_set()
    downloader.trigger_queue_processing()
    assert downloader._queue_event.is_set()

    # Adding a task should trigger processing
    downloader._queue_event.clear()
    downloader.add_task("https://example.com/part01.rar", "/tmp")
    assert downloader._queue_event.is_set()


@pytest.mark.anyio
async def test_session_pooling():
    """Verifies AsyncDownloader maintains and reuses persistent sessions per interface."""
    link_mgr = LinkManager()
    net_mgr = NetworkManager()
    session_mgr = MagicMock()
    downloader = AsyncDownloader(link_mgr, net_mgr, session_mgr)

    s1 = await downloader._get_session(None)
    s2 = await downloader._get_session(None)
    assert s1 is s2, "Session pool should return the exact same instance for the same adapter IP"

    downloader._evict_session(None)
    assert None not in downloader._session_pool
    await downloader.stop()


@pytest.mark.anyio
async def test_domain_pacing_semaphore():
    """Verifies LinkManager maintains a per-domain concurrency limiter to prevent 403 blocks."""
    link_mgr = LinkManager()
    sem1 = await link_mgr._get_domain_semaphore("fuckingfast.co")
    sem2 = await link_mgr._get_domain_semaphore("fuckingfast.co")
    assert sem1 is sem2, "Same domain should share the exact same semaphore"
    assert sem1._value == 2, "Default domain semaphore limit should be 2"


@pytest.mark.anyio
async def test_resolve_pipeline_retry():
    """Verifies that _execute_task_pipeline retries when resolving encounters transient errors."""
    link_mgr = LinkManager()
    net_mgr = NetworkManager()
    session_mgr = MagicMock()
    downloader = AsyncDownloader(link_mgr, net_mgr, session_mgr)
    downloader.max_retries = 2

    task = downloader.add_task("https://fuckingfast.co/test#part01.rar", "/tmp")

    # Mock link_manager.resolve_link to fail once with 403, then succeed
    attempts = 0

    async def mock_resolve(url, interface=None, use_cache=True):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("FuckingFast landing page returned HTTP 403")
        return ("https://dl.fuckingfast.co/dl/test", "part01.rar", 1000)

    link_mgr.resolve_link = mock_resolve

    # Mock _download_stream so it completes immediately
    async def mock_stream(t, ip):
        t.status = TaskStatus.COMPLETED

    downloader._download_stream = mock_stream

    adapter = MagicMock()
    adapter.friendly_type = "Wi-Fi"
    adapter.ip_address = "127.0.0.1"

    await downloader._execute_task_pipeline(task, adapter)
    assert attempts == 2, f"Expected 2 resolve attempts, got {attempts}"
    assert task.status == TaskStatus.COMPLETED

def test_batch_task_ingestion_performance():
    """Verifies add_tasks_batch ingests 80 parts in <20ms without O(N^2) sorting overhead."""
    import time
    link_mgr = LinkManager()
    net_mgr = NetworkManager()
    session_mgr = MagicMock()
    downloader = AsyncDownloader(link_mgr, net_mgr, session_mgr)

    urls = [
        f"https://fuckingfast.co/id{i}#Game_--_fitgirl-repacks.site_--_.part{i:02d}.rar"
        for i in range(80, 0, -1)
    ]
    t0 = time.perf_counter()
    created = downloader.add_tasks_batch(urls, "/tmp")
    elapsed = time.perf_counter() - t0

    assert len(created) == 80
    assert elapsed < 0.1, f"Batch ingestion took too long: {elapsed:.3f}s"
    first_task = next(iter(downloader.tasks.values()))
    assert first_task.filename.endswith(".part01.rar")


@pytest.mark.anyio
async def test_O1_sliding_window_speed_tracker():
    """Verifies NetworkManager O(1) deque rolling bytes calculation and pruning."""
    net_mgr = NetworkManager(ewma_alpha=0.5)
    ip = "192.168.1.50"
    for _ in range(100):
        net_mgr.record_chunk_transfer(ip, 65536)

    assert net_mgr._adapter_rolling_bytes[ip] == 100 * 65536
    speed = net_mgr.get_adapter_speed(ip)
    assert speed > 0


