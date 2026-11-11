"""Unit Tests for Download Task Models and Session Persistence."""

import os
from engine.session import SessionManager
from engine.task import DownloadTask, TaskStatus


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

    loaded = mgr.load_session()
    assert loaded["settings"]["max_concurrency"] == 6
    assert len(loaded["tasks"]) == 1
    assert loaded["tasks"][0]["filename"] == "part1.rar"


import pytest
from unittest.mock import MagicMock
from engine.downloader import AsyncDownloader
from engine.network import NetworkAdapter, NetworkManager
from resolvers.manager import LinkManager


@pytest.mark.anyio
async def test_task_handoff_workflow():
    net_mgr = NetworkManager()
    link_mgr = LinkManager()
    session_mgr = MagicMock()

    adapter_target = NetworkAdapter(
        name="Target Adapter",
        ip_address="192.168.10.5",
        friendly_type="Wi-Fi",
        is_up=True,
        is_enabled=True
    )

    downloader = AsyncDownloader(link_mgr, net_mgr, session_mgr)
    net_mgr.get_adapter_by_ip = MagicMock(return_value=adapter_target)

    task = downloader.add_task("https://example.com/part01.rar", "/tmp")
    task.status = TaskStatus.DOWNLOADING
    task.assigned_adapter = "Ethernet (192.168.1.2)"
    downloader._task_assigned_ip[task.id] = "192.168.1.2"

    # Perform handoff
    success = await downloader.handoff_task_to_adapter(task.id, "192.168.10.5")
    assert success is True
    assert task.status == TaskStatus.QUEUED
    assert "192.168.10.5" in task.assigned_adapter
    assert len(downloader.handoff_events) == 1
    assert downloader.handoff_events[0]["to_adapter"] == "Wi-Fi (192.168.10.5)"


