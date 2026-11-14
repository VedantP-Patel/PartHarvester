"""Tests for journal periodic persistence and automatic failed-part retry."""

import json
import os
from unittest.mock import MagicMock

from engine.downloader import AsyncDownloader
from engine.journal import IntervalJournal
from engine.network import NetworkManager
from engine.task import TaskStatus
from resolvers.manager import LinkManager


def test_journal_add_range_flushes_periodically(tmp_path):
    """add_range must persist the .meta journal on its own (crash safety), not only on forced saves."""
    part = str(tmp_path / "file.rar.part")
    j = IntervalJournal(part, 10 * 1024 * 1024)
    j.add_range(0, 1023, flush_interval=0.0)
    assert os.path.exists(j.meta_path), "Periodic flush should have written the journal"
    with open(j.meta_path, encoding="utf-8") as f:
        assert json.load(f)["intervals"] == [[0, 1023]]


def _downloader():
    return AsyncDownloader(LinkManager(), NetworkManager(), MagicMock())


def test_auto_retry_requeues_with_backoff_and_cap():
    d = _downloader()
    t = d.add_task("https://example.com/a.part01.rar", "/tmp")
    t.status = TaskStatus.FAILED
    t.error_message = "Download error: connection reset"
    base = d._AUTO_RETRY_BASE_DELAY_SEC

    assert d.auto_retry_failed_tasks(now=1000.0) == 0          # first sighting schedules +30s
    assert t.status == TaskStatus.FAILED
    assert d.auto_retry_failed_tasks(now=1000.0 + base - 1) == 0
    assert d.auto_retry_failed_tasks(now=1000.0 + base) == 1   # round 1
    assert t.status == TaskStatus.QUEUED

    t.status = TaskStatus.FAILED                                # fails again -> 60s backoff
    assert d.auto_retry_failed_tasks(now=2000.0) == 0
    assert d.auto_retry_failed_tasks(now=2000.0 + base * 2 - 1) == 0
    assert d.auto_retry_failed_tasks(now=2000.0 + base * 2) == 1  # round 2

    t.status = TaskStatus.FAILED
    d.auto_retry_failed_tasks(now=3000.0)
    assert d.auto_retry_failed_tasks(now=3000.0 + base * 4) == 1  # round 3

    t.status = TaskStatus.FAILED                                # budget exhausted
    d.auto_retry_failed_tasks(now=4000.0)
    assert d.auto_retry_failed_tasks(now=9999999.0) == 0
    assert t.status == TaskStatus.FAILED


def test_auto_retry_skips_dead_links_and_manual_retry_resets_budget():
    d = _downloader()
    dead = d.add_task("https://example.com/dead.part01.rar", "/tmp")
    dead.status = TaskStatus.FAILED
    dead.error_message = "Dead link (HTTP 404)"
    flaky = d.add_task("https://example.com/flaky.part01.rar", "/tmp")
    flaky.status = TaskStatus.FAILED
    flaky.error_message = "Resolve error: timeout"

    d.auto_retry_failed_tasks(now=0.0)
    assert d.auto_retry_failed_tasks(now=1e6) == 1
    assert dead.status == TaskStatus.FAILED and flaky.status == TaskStatus.QUEUED

    d._auto_retry_state[flaky.id][0] = d._AUTO_RETRY_MAX_ROUNDS
    flaky.status = TaskStatus.FAILED
    d.retry_failed()
    assert flaky.id not in d._auto_retry_state
