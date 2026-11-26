"""Unit Tests for Download Task Models, Session Persistence, Handoff Pinning, and Async Callbacks."""
import asyncio
import os
from unittest.mock import MagicMock
import pytest

from engine.downloader import AsyncDownloader
from engine.network import NetworkAdapter, NetworkManager
from engine.session import SessionManager
from engine.task import DownloadTask, TaskStatus
from resolvers.manager import LinkManager


def test_download_task_progress_calculation():
    task = DownloadTask(
        original_url="https://example.com/file.bin",
        file_size=1000,
        downloaded_bytes=500,
        status=TaskStatus.DOWNLOADING,
    )
    assert task.progress_pct == 50.0
    assert task.is_finished is False

    task.downloaded_bytes = 1000
    task.status = TaskStatus.COMPLETED
    assert task.progress_pct == 100.0
    assert task.is_finished is True


def test_session_manager_persistence(tmp_path):
    session_file = tmp_path / "test_session.json"
    mgr = SessionManager(filepath=str(session_file))

    settings = {"download_dir": "/custom/path", "max_concurrency": 6}
    tasks = [
        DownloadTask(
            original_url="https://example.com/part1.rar",
            filename="part1.rar",
            file_size=5000,
            downloaded_bytes=2500,
            status=TaskStatus.PAUSED,
        )
    ]

    mgr.save_session(settings, tasks)
    assert os.path.exists(str(session_file))
    assert not os.path.exists(f"{session_file}.tmp"), "Atomic temp file should be cleaned up"

    loaded = mgr.load_session()
    assert loaded["settings"]["max_concurrency"] == 6
    assert len(loaded["tasks"]) == 1
    assert loaded["tasks"][0]["filename"] == "part1.rar"


@pytest.mark.anyio
async def test_task_handoff_workflow_and_pinned_dispatch():
    """Verifies handoff queues task AND dispatches it onto the pinned target adapter rather than overwriting it."""
    net_mgr = NetworkManager()
    link_mgr = LinkManager()
    session_mgr = MagicMock()

    adapter_default = NetworkAdapter(
        name="Default Fast",
        ip_address="192.168.1.2",
        friendly_type="Ethernet",
        is_up=True,
        is_enabled=True,
        ewma_speed_bytes=50 * 1024 * 1024,
    )
    adapter_target = NetworkAdapter(
        name="Target Adapter",
        ip_address="192.168.10.5",
        friendly_type="Wi-Fi",
        is_up=True,
        is_enabled=True,
        ewma_speed_bytes=10 * 1024 * 1024,
    )

    downloader = AsyncDownloader(link_mgr, net_mgr, session_mgr)
    net_mgr.get_adapter_by_ip = MagicMock(
        side_effect=lambda ip: adapter_target if ip == "192.168.10.5" else adapter_default
    )
    net_mgr.get_best_available_adapter = MagicMock(return_value=adapter_default)

    task = downloader.add_task("https://example.com/part01.rar", "/tmp")
    task.status = TaskStatus.DOWNLOADING
    task.assigned_adapter = "Ethernet (192.168.1.2)"
    downloader._task_assigned_ip[task.id] = "192.168.1.2"

    # Perform handoff to 192.168.10.5
    success = await downloader.handoff_task_to_adapter(task.id, "192.168.10.5")
    assert success is True
    assert task.status == TaskStatus.QUEUED
    assert "192.168.10.5" in task.assigned_adapter
    assert len(downloader.handoff_events) == 1
    assert downloader.handoff_events[0]["to_adapter"] == "Wi-Fi (192.168.10.5)"

    # Verify queue loop honors the pinned adapter instead of reverting to adapter_default
    dispatched_adapters = []

    async def mock_pipeline(t, chosen_adapter):
        dispatched_adapters.append(chosen_adapter.ip_address)
        t.status = TaskStatus.COMPLETED

    downloader._execute_task_pipeline = mock_pipeline
    await downloader.start()
    await asyncio.sleep(0.05)
    await downloader.stop()

    assert dispatched_adapters == ["192.168.10.5"]


@pytest.mark.anyio
async def test_async_on_complete_callback_is_awaited():
    """Ensures async def on_complete callbacks (used by server/app.py for auto-extract) are actually scheduled."""
    net_mgr = NetworkManager()
    link_mgr = LinkManager()
    session_mgr = MagicMock()

    callback_triggered = asyncio.Event()
    completed_ids = []

    async def async_on_complete(task: DownloadTask):
        completed_ids.append(task.id)
        callback_triggered.set()

    downloader = AsyncDownloader(
        link_mgr, net_mgr, session_mgr, on_complete_callback=async_on_complete
    )
    task = downloader.add_task("https://example.com/part01.rar", "/tmp")
    downloader._fire_complete_callback(task)

    await asyncio.wait_for(callback_triggered.wait(), timeout=1.0)
    assert completed_ids == [task.id]