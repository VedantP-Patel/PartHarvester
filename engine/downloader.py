"""High-Performance Async Download Engine with Automatic NIC Failover & Active Tail-Stealing."""
import asyncio
import ctypes
import inspect
import itertools
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.parse

if sys.platform == "win32":
    import msvcrt

FSCTL_SET_SPARSE = 0x000900C4

from typing import Callable, Dict, List, Optional, Set
import aiofiles
from curl_cffi import requests

try:
    from curl_cffi.const import CurlHttpVersion
    _HTTP1_VERSION = CurlHttpVersion.V1_1
except Exception:
    _HTTP1_VERSION = None

from engine.autotune import AutoTuneRecommendation, SmartAutoTuner
from engine.journal import IntervalJournal
from engine.network import NetworkAdapter, NetworkManager
from engine.session import SessionManager
from engine.task import DownloadTask, Segment, TaskStatus
from resolvers.manager import LinkManager


class RangeNotSupportedOrExpiredError(Exception):
    """Raised when a non-zero Range request receives HTTP 200 instead of 206 Partial Content."""


class RateLimitCooldownError(Exception):
    """Raised when the CDN responds with HTTP 429 / 503 and a Retry-After window."""

    def __init__(self, message: str, retry_after: float = 5.0):
        super().__init__(message)
        self.retry_after = retry_after


class PositionedFileWriter:
    """Thread-safe shared random-access writer avoiding per-segment handle open/close churn."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self._lock = threading.Lock()
        self._fh = open(filepath, "r+b")

    def write_at(self, offset: int, data: bytes) -> None:
        with self._lock:
            if self._fh.closed:
                raise IOError("Target file handle is closed.")
            self._fh.seek(offset)
            self._fh.write(data)

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                try:
                    self._fh.flush()
                finally:
                    self._fh.close()


def _commit_chunk_sync(
    writer: PositionedFileWriter,
    journal: IntervalJournal,
    offset: int,
    payload: bytes,
) -> int:
    """Atomically writes bytes to disk and updates in-memory interval journal inside a worker thread."""
    writer.write_at(offset, payload)
    journal.add_range(offset, offset + len(payload) - 1)
    return len(payload)


class TokenBucketLimiter:
    """Non-blocking reservation token bucket for global bandwidth capping across concurrent streams."""

    def __init__(self, rate_mbps: float = 0.0):
        self.rate_bytes_sec = rate_mbps * 1024 * 1024 if rate_mbps > 0 else 0.0
        self._next_free_time = time.monotonic()
        self._lock = asyncio.Lock()

    def set_rate(self, rate_mbps: float) -> None:
        self.rate_bytes_sec = rate_mbps * 1024 * 1024 if rate_mbps > 0 else 0.0
        self._next_free_time = time.monotonic()

    async def consume(self, amount: int) -> None:
        if self.rate_bytes_sec <= 0 or amount <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            min_cursor = now - 0.25
            if self._next_free_time < min_cursor:
                self._next_free_time = min_cursor
            self._next_free_time += amount / self.rate_bytes_sec
            wait_time = self._next_free_time - now

        if wait_time > 0.005:
            await asyncio.sleep(min(wait_time, 2.0))


class AsyncDownloader:
    """Core download manager handling tasks, automatic NIC failover, interval journals, and work-stealing."""

    _NATURAL_RE = re.compile(r"(\d+)")
    _SHARDS_PER_ADAPTER = 6
    _MIN_SPLIT_BYTES = 4 * 1024 * 1024
    _READ_STALL_TIMEOUT_SEC = 12.0
    _AUTO_RETRY_MAX_ROUNDS = 3
    _AUTO_RETRY_BASE_DELAY_SEC = 30.0
    _AUTO_RETRY_CHECK_INTERVAL_SEC = 5.0

    def __init__(
        self,
        link_manager: LinkManager,
        net_manager: NetworkManager,
        session_manager: SessionManager,
        max_concurrency: int = 2,
        on_progress_callback: Optional[Callable[[], None]] = None,
        on_complete_callback: Optional[Callable[[DownloadTask], None]] = None,
    ):
        self.link_manager = link_manager
        self.net_manager = net_manager
        self.session_manager = session_manager
        self.max_concurrency = max_concurrency
        self.on_progress_callback = on_progress_callback
        self.on_complete_callback = on_complete_callback

        self.tasks: Dict[str, DownloadTask] = {}
        self.handoff_events: List[dict] = []
        self._is_running = True
        self._worker_loop_task: Optional[asyncio.Task] = None
        self._straggler_loop_task: Optional[asyncio.Task] = None
        self._active_async_tasks: Dict[str, asyncio.Task] = {}
        self._task_assigned_ip: Dict[str, str] = {}
        self._pinned_handoff_ip: Dict[str, str] = {}
        self._active_pinned_ips: Dict[str, str] = {}
        self._session_shards: Dict[tuple, List[Optional[requests.AsyncSession]]] = {}
        self._shard_counter = itertools.count()
        self._journals: Dict[str, IntervalJournal] = {}
        self._queue_event = asyncio.Event()

        # Acceleration & Engine settings
        self.auto_tune_enabled: bool = True
        self.auto_tuner = SmartAutoTuner()
        self.segments_per_file: int = 2
        self.buffer_size_kb: int = 256
        self.speed_limit_mbps: float = 0.0
        self._token_limiter = TokenBucketLimiter(0.0)
        self.max_retries: int = 6
        self.min_segment_file_size_mb: int = 10
        self.bonding_strategy: str = "ewma"
        self.work_stealing_enabled: bool = True
        self.delete_archive_after_extract: bool = False
        self.auto_retry_failed: bool = True
        # task_id -> [rounds_used, next_retry_monotonic_ts or None]
        self._auto_retry_state: Dict[str, list] = {}

    def auto_retry_failed_tasks(self, now: Optional[float] = None) -> int:
        """Re-queues transiently failed parts with exponential backoff (30s, 60s, 120s).

        Dead links (404) are never retried. Returns the number of tasks re-queued.
        """
        if now is None:
            now = time.monotonic()
        requeued = 0
        for tid in [k for k in self._auto_retry_state if k not in self.tasks]:
            self._auto_retry_state.pop(tid, None)

        for task in self.tasks.values():
            if task.status != TaskStatus.FAILED:
                if task.status == TaskStatus.COMPLETED:
                    self._auto_retry_state.pop(task.id, None)
                continue
            err = task.error_message or ""
            if "404" in err or "Dead link" in err:
                continue
            state = self._auto_retry_state.setdefault(task.id, [0, None])
            rounds, next_ts = state
            if rounds >= self._AUTO_RETRY_MAX_ROUNDS:
                continue
            if next_ts is None:
                state[1] = next_ts = now + self._AUTO_RETRY_BASE_DELAY_SEC * (2 ** rounds)
            if now >= next_ts:
                task.status = TaskStatus.QUEUED
                task.error_message = f"Auto-retry {rounds + 1}/{self._AUTO_RETRY_MAX_ROUNDS}"
                task.retries = 0
                state[0] = rounds + 1
                state[1] = None
                requeued += 1
        if requeued:
            self.trigger_queue_processing()
        return requeued

    def _record_failover_event(self, task: DownloadTask, from_label: str, to_adapter: NetworkAdapter) -> None:
        event = {
            "timestamp": time.time(),
            "task_id": task.id,
            "filename": task.filename,
            "from_adapter": from_label or "Disconnected",
            "to_adapter": f"{to_adapter.friendly_type} ({to_adapter.ip_address})",
        }
        self.handoff_events.append(event)
        if len(self.handoff_events) > 15:
            self.handoff_events.pop(0)

    def _pick_live_adapter(
        self,
        preferred_ip: Optional[str] = None,
        exclude_ips: Optional[Set[str]] = None,
    ) -> Optional[NetworkAdapter]:
        """Returns preferred adapter if up/enabled/uncooled, otherwise automatically fails over."""
        exclude = exclude_ips or set()
        adapters = self.net_manager.get_available_adapters()
        healthy = [
            a for a in adapters
            if a.is_enabled
            and a.is_up
            and a.ip_address not in exclude
            and self.auto_tuner.get_cooldown_remaining(a.ip_address) <= 0.0
        ]
        if not healthy:
            # Fallback to any enabled+up adapter even if cooling down
            healthy = [
                a for a in adapters
                if a.is_enabled and a.is_up and a.ip_address not in exclude
            ]
        if not healthy:
            healthy = [a for a in adapters if a.is_enabled and a.is_up]
        if not healthy:
            return None

        if preferred_ip:
            for a in healthy:
                if a.ip_address == preferred_ip:
                    return a

        # Choose healthiest adapter with available socket headroom and highest EWMA speed
        healthy.sort(
            key=lambda a: (
                a.max_concurrency_cap - a.active_connections > 0,
                a.ewma_speed_bytes,
                a.max_concurrency_cap - a.active_connections,
            ),
            reverse=True,
        )
        return healthy[0]

    def _get_journal(self, task: DownloadTask, part_filepath: str) -> IntervalJournal:
        journal = self._journals.get(task.id)
        if journal is None or journal.part_filepath != part_filepath or (
            task.file_size > 0 and journal.file_size != task.file_size
        ):
            journal = IntervalJournal(part_filepath, task.file_size)
            self._journals[task.id] = journal
        return journal

    def set_auto_tune(self, enabled: bool) -> None:
        self.auto_tune_enabled = bool(enabled)
        self.auto_tuner.enabled = self.auto_tune_enabled
        if self.auto_tune_enabled:
            self.apply_auto_tune()
        self.trigger_queue_processing()

    def apply_auto_tune(self) -> AutoTuneRecommendation:
        adapters = self.net_manager.get_available_adapters()
        adapter_dicts = [
            {
                "ip_address": getattr(a, "ip_address", ""),
                "is_enabled": getattr(a, "is_enabled", True),
                "is_up": getattr(a, "is_up", True),
                "ewma_speed_bytes": getattr(a, "ewma_speed_bytes", 0.0),
                "current_speed_bytes": getattr(a, "current_speed_bytes", 0.0),
            }
            for a in adapters
        ]
        rec = self.auto_tuner.evaluate(adapter_dicts)

        for ip, cap in rec.per_adapter_max_conns.items():
            if hasattr(self.net_manager, "set_adapter_cap"):
                self.net_manager.set_adapter_cap(ip, cap)

        if self.auto_tune_enabled:
            self.max_concurrency = rec.max_concurrency
            self.segments_per_file = rec.segments_per_file
            self.buffer_size_kb = rec.buffer_size_kb

        return rec

    def _compute_effective_segments(self, task: DownloadTask) -> int:
        """Scales segment count by file size and harvests idle adapter sockets at queue tail."""
        base = max(1, min(16, self.segments_per_file))
        if not self.auto_tune_enabled:
            return base

        if task.file_size > 0:
            if task.file_size < 20 * 1024 * 1024:
                size_cap = 1
            elif task.file_size < 80 * 1024 * 1024:
                size_cap = 2
            elif task.file_size < 400 * 1024 * 1024:
                size_cap = max(base, 4)
            else:
                size_cap = max(base, 6)
        else:
            size_cap = base

        queued_count = sum(1 for t in self.tasks.values() if t.status == TaskStatus.QUEUED)
        active_count = sum(
            1 for t in self.tasks.values() if t.status in (TaskStatus.DOWNLOADING, TaskStatus.RESOLVING)
        )
        if queued_count == 0 and active_count <= 2 and task.file_size >= 80 * 1024 * 1024:
            active_adapters = [
                a for a in self.net_manager.get_available_adapters() if a.is_enabled and a.is_up
            ]
            total_socket_cap = sum(a.max_concurrency_cap for a in active_adapters)
            if total_socket_cap > 0:
                per_active_budget = max(base, total_socket_cap // max(1, active_count))
                size_cap = max(size_cap, per_active_budget)

        return max(1, min(16, size_cap))

    def _fire_complete_callback(self, task: DownloadTask) -> None:
        if not self.on_complete_callback:
            return
        try:
            res = self.on_complete_callback(task)
            if inspect.isawaitable(res):
                asyncio.create_task(res)
        except Exception:
            pass

    def trigger_queue_processing(self) -> None:
        self._queue_event.set()

    def set_speed_limit(self, limit_mbps: float) -> None:
        self.speed_limit_mbps = max(0.0, limit_mbps)
        self._token_limiter.set_rate(self.speed_limit_mbps)

    async def _get_session(
        self, adapter_ip: Optional[str], force_http1: bool = True
    ) -> requests.AsyncSession:
        key = (adapter_ip, force_http1)
        shards = self._session_shards.get(key)
        if shards is None:
            shards = [None] * self._SHARDS_PER_ADAPTER
            self._session_shards[key] = shards

        idx = next(self._shard_counter) % self._SHARDS_PER_ADAPTER
        session = shards[idx]
        if session is None or getattr(session, "_closed", False):
            s_kwargs: dict = {"impersonate": "chrome", "max_clients": 16}
            if adapter_ip:
                s_kwargs["interface"] = adapter_ip
            if force_http1 and _HTTP1_VERSION is not None:
                s_kwargs["http_version"] = _HTTP1_VERSION
            session = requests.AsyncSession(**s_kwargs)
            shards[idx] = session
        return session

    def set_concurrency(self, limit: int) -> None:
        self.max_concurrency = max(1, min(12, limit))
        self.trigger_queue_processing()

    @staticmethod
    def _extract_candidate_filename(original_url: str) -> str:
        parsed = urllib.parse.urlparse(original_url)
        if parsed.fragment:
            return urllib.parse.unquote(parsed.fragment)
        if parsed.path:
            segs = [s for s in parsed.path.split("/") if s]
            if segs:
                return urllib.parse.unquote(segs[-1])
        return "download.bin"

    def add_task(self, original_url: str, destination_dir: str, sort: bool = True) -> DownloadTask:
        norm_dir = os.path.normpath(destination_dir)
        for existing in self.tasks.values():
            if existing.original_url == original_url and os.path.normpath(existing.destination_dir) == norm_dir:
                if existing.status == TaskStatus.FAILED:
                    existing.status = TaskStatus.QUEUED
                    existing.error_message = None
                    existing.retries = 0
                    self.trigger_queue_processing()
                return existing

        cand_name = self._extract_candidate_filename(original_url)
        task = DownloadTask(
            original_url=original_url,
            destination_dir=destination_dir,
            filename=cand_name,
        )
        part_path = os.path.join(destination_dir, f"{cand_name}.part")
        meta_path = f"{part_path}.meta"
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                sz = int(meta.get("file_size", 0))
                if sz > 0:
                    task.file_size = sz
                    j = self._get_journal(task, part_path)
                    task.downloaded_bytes = j.total_downloaded()
            except Exception:
                pass
        elif os.path.exists(part_path):
            try:
                sz = os.path.getsize(part_path)
                if sz > 0:
                    task.file_size = sz
                    j = self._get_journal(task, part_path)
                    task.downloaded_bytes = j.total_downloaded()
            except Exception:
                pass

        self.tasks[task.id] = task
        if sort:
            self.sort_tasks("natural_part")
        self.trigger_queue_processing()
        return task

    def add_tasks_batch(self, urls: List[str], destination_dir: str) -> List[DownloadTask]:
        created = [self.add_task(u, destination_dir, sort=False) for u in urls]
        if created:
            self.apply_auto_tune()
            self.sort_tasks("natural_part")
            self.trigger_queue_processing()
        return created

    def sort_tasks(self, criterion: str = "natural_part") -> None:
        def natural_key(text: str):
            return [
                int(c) if c.isdigit() else c.lower()
                for c in self._NATURAL_RE.split(text or "")
            ]

        def get_sort_name(t: DownloadTask) -> str:
            if t.filename and t.filename != "download.bin":
                return t.filename
            return self._extract_candidate_filename(t.original_url)

        items = list(self.tasks.items())
        if criterion == "natural_part":
            items.sort(key=lambda item: natural_key(get_sort_name(item[1])))
        elif criterion == "status":
            order = {
                TaskStatus.DOWNLOADING: 0,
                TaskStatus.RESOLVING: 1,
                TaskStatus.QUEUED: 2,
                TaskStatus.PAUSED: 3,
                TaskStatus.FAILED: 4,
                TaskStatus.COMPLETED: 5,
            }
            items.sort(key=lambda item: order.get(item[1].status, 6))
        elif criterion == "progress":
            items.sort(key=lambda item: item[1].progress_pct, reverse=True)
        elif criterion == "size":
            items.sort(key=lambda item: item[1].file_size, reverse=True)

        self.tasks = dict(items)
        self.trigger_queue_processing()

    def reorder_task(self, task_id: str, direction: str) -> bool:
        if task_id not in self.tasks:
            return False
        keys = list(self.tasks.keys())
        idx = keys.index(task_id)
        if direction == "top":
            keys.remove(task_id)
            keys.insert(0, task_id)
        elif direction == "up" and idx > 0:
            keys[idx], keys[idx - 1] = keys[idx - 1], keys[idx]
        elif direction == "down" and idx < len(keys) - 1:
            keys[idx], keys[idx + 1] = keys[idx + 1], keys[idx]
        else:
            return False
        self.tasks = {k: self.tasks[k] for k in keys}
        self.trigger_queue_processing()
        return True

    def clear_completed(self) -> int:
        to_remove = [tid for tid, t in self.tasks.items() if t.status == TaskStatus.COMPLETED]
        for tid in to_remove:
            self._journals.pop(tid, None)
            del self.tasks[tid]
        return len(to_remove)

    def clear_all(self) -> int:
        self.pause_all()
        count = len(self.tasks)
        for j in list(self._journals.values()):
            j.cleanup()
        self._journals.clear()
        self.tasks.clear()
        return count

    @staticmethod
    def open_download_folder(folder_path: str) -> bool:
        try:
            if os.path.exists(folder_path):
                subprocess.Popen(["explorer.exe", folder_path])
                return True
        except Exception:
            pass
        return False

    def reveal_task_file(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if not task:
            return False
        filepath = os.path.join(task.destination_dir, task.filename)
        partpath = os.path.join(task.destination_dir, f"{task.filename}.part")
        target = filepath if os.path.exists(filepath) else partpath
        try:
            if os.path.exists(target):
                subprocess.Popen(["explorer.exe", f"/select,{target}"])
                return True
            elif os.path.exists(task.destination_dir):
                subprocess.Popen(["explorer.exe", task.destination_dir])
                return True
        except Exception:
            pass
        return False

    def pause_task(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if not task:
            return False
        if task.status in (TaskStatus.DOWNLOADING, TaskStatus.RESOLVING):
            task.status = TaskStatus.PAUSED
            task.speed_bytes_sec = 0.0
            if task_id in self._active_async_tasks:
                self._active_async_tasks[task_id].cancel()
            j = self._journals.get(task_id)
            if j and task_id in self.tasks:
                asyncio.create_task(j.save_async(force=True))
            return True
        elif task.status == TaskStatus.QUEUED:
            task.status = TaskStatus.PAUSED
            return True
        return False

    def resume_task(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if not task:
            return False
        if task.status in (TaskStatus.PAUSED, TaskStatus.FAILED):
            task.status = TaskStatus.QUEUED
            task.error_message = None
            task.retries = 0
            self.trigger_queue_processing()
            return True
        return False

    def pause_all(self) -> None:
        for task_id in list(self.tasks.keys()):
            self.pause_task(task_id)

    def resume_all(self) -> None:
        for task_id in list(self.tasks.keys()):
            self.resume_task(task_id)
        self.trigger_queue_processing()

    def retry_failed(self) -> None:
        self.apply_auto_tune()
        for task in self.tasks.values():
            if task.status == TaskStatus.FAILED:
                task.status = TaskStatus.QUEUED
                task.error_message = None
                task.retries = 0
                self._auto_retry_state.pop(task.id, None)
        self.trigger_queue_processing()

    def remove_task(self, task_id: str) -> bool:
        self.pause_task(task_id)
        self._pinned_handoff_ip.pop(task_id, None)
        self._task_assigned_ip.pop(task_id, None)
        j = self._journals.pop(task_id, None)
        if j:
            j.cleanup()
        if task_id in self.tasks:
            del self.tasks[task_id]
            return True
        return False

    async def handoff_task_to_adapter(self, task_id: str, target_ip: str) -> bool:
        task = self.tasks.get(task_id)
        if not task or task.status != TaskStatus.DOWNLOADING:
            return False

        target_adapter = self.net_manager.get_adapter_by_ip(target_ip)
        if not target_adapter or not target_adapter.is_enabled or not target_adapter.is_up:
            return False

        current_ip = self._task_assigned_ip.get(task_id)
        if current_ip == target_ip:
            return False

        self._record_failover_event(task, task.assigned_adapter or "Unknown", target_adapter)
        self._pinned_handoff_ip[task_id] = target_ip
        task.assigned_adapter = f"{target_adapter.friendly_type} ({target_ip})"
        self._task_assigned_ip[task_id] = target_ip
        task.status = TaskStatus.QUEUED

        active_coro = self._active_async_tasks.get(task_id)
        if active_coro and not active_coro.done():
            active_coro.cancel()
            try:
                await active_coro
            except (asyncio.CancelledError, Exception):
                pass

        j = self._journals.get(task_id)
        if j and task_id in self.tasks:
            await j.save_async(force=True)

        self.reorder_task(task_id, "top")
        task.status = TaskStatus.QUEUED
        self.trigger_queue_processing()
        return True

    async def start(self) -> None:
        self._is_running = True
        self.apply_auto_tune()
        self._worker_loop_task = asyncio.create_task(self._process_queue_loop())
        self._straggler_loop_task = asyncio.create_task(self._straggler_watchdog_loop())

    async def stop(self) -> None:
        self._is_running = False
        self.pause_all()
        for tid, j in list(self._journals.items()):
            if tid in self.tasks:
                j.save_sync(force=True)
        if self._worker_loop_task:
            self._worker_loop_task.cancel()
        if self._straggler_loop_task:
            self._straggler_loop_task.cancel()
        for shards in list(self._session_shards.values()):
            for s in shards:
                try:
                    if s and not getattr(s, "_closed", False):
                        await s.close()
                except Exception:
                    pass
        self._session_shards.clear()

    async def _process_queue_loop(self) -> None:
        last_pre_resolve_time = 0.0
        last_auto_retry_check = 0.0
        while self._is_running:
            try:
                self.apply_auto_tune()
                _now_ar = time.monotonic()
                if self.auto_retry_failed and (_now_ar - last_auto_retry_check) >= self._AUTO_RETRY_CHECK_INTERVAL_SEC:
                    last_auto_retry_check = _now_ar
                    self.auto_retry_failed_tasks(_now_ar)
                total_stream_budget = max(1, self.max_concurrency * self.segments_per_file)

                queued_tasks = [t for t in self.tasks.values() if t.status == TaskStatus.QUEUED]
                now = time.monotonic()
                if queued_tasks and (now - last_pre_resolve_time >= 1.0):
                    last_pre_resolve_time = now
                    upcoming_urls = [t.original_url for t in queued_tasks[:8]]
                    active_ads = [
                        a for a in self.net_manager.get_available_adapters() if a.is_enabled and a.is_up
                    ]
                    if active_ads:
                        # Distribute pre-resolution across all healthy interfaces in parallel
                        for idx, ad in enumerate(active_ads):
                            slice_urls = upcoming_urls[idx::len(active_ads)]
                            if slice_urls:
                                asyncio.create_task(
                                    self.link_manager.pre_resolve_batch(
                                        slice_urls, interface=ad.ip_address
                                    )
                                )
                    else:
                        asyncio.create_task(self.link_manager.pre_resolve_batch(upcoming_urls))

                active_count = sum(
                    1 for t in self.tasks.values() if t.status in (TaskStatus.DOWNLOADING, TaskStatus.RESOLVING)
                )
                if active_count < self.max_concurrency and queued_tasks:
                    for task in queued_tasks:
                        if active_count >= self.max_concurrency:
                            break
                        if task.id in self._active_async_tasks and not self._active_async_tasks[task.id].done():
                            continue

                        pinned_ip = self._pinned_handoff_ip.pop(task.id, None)
                        chosen_adapter = (
                            self.net_manager.get_adapter_by_ip(pinned_ip) if pinned_ip else None
                        )
                        if chosen_adapter and chosen_adapter.is_enabled and chosen_adapter.is_up:
                            self._active_pinned_ips[task.id] = chosen_adapter.ip_address
                        else:
                            self._active_pinned_ips.pop(task.id, None)
                            chosen_adapter = self.net_manager.get_best_available_adapter(total_stream_budget)
                        if not chosen_adapter:
                            break

                        task.status = TaskStatus.RESOLVING
                        active_count += 1
                        t = asyncio.create_task(self._execute_task_pipeline(task, chosen_adapter))
                        self._active_async_tasks[task.id] = t

                try:
                    await asyncio.wait_for(self._queue_event.wait(), timeout=0.25)
                    self._queue_event.clear()
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(0.5)

    async def _straggler_watchdog_loop(self) -> None:
        while self._is_running:
            try:
                await asyncio.sleep(2.0)
                if not self.work_stealing_enabled:
                    continue

                if any(t.status == TaskStatus.QUEUED for t in self.tasks.values()):
                    continue

                adapters = [a for a in self.net_manager.get_available_adapters() if a.is_enabled and a.is_up]
                if len(adapters) < 2:
                    continue

                adapters.sort(key=lambda a: a.ewma_speed_bytes, reverse=True)
                fast_adapter = adapters[0]
                slow_adapter = adapters[-1]

                fast_speed = fast_adapter.ewma_speed_bytes
                slow_speed = slow_adapter.ewma_speed_bytes

                if fast_adapter.active_connections < fast_adapter.max_concurrency_cap:
                    active_single_on_slow = [
                        t for t in self.tasks.values()
                        if t.status == TaskStatus.DOWNLOADING
                        and len(t.segments) <= 1
                        and self._task_assigned_ip.get(t.id) == slow_adapter.ip_address
                    ]
                    for task in active_single_on_slow:
                        remaining = task.file_size - task.downloaded_bytes
                        if remaining > 40 * 1024 * 1024 and (
                            fast_speed > 2.0 * slow_speed or slow_speed < 1024 * 1024
                        ):
                            await self.handoff_task_to_adapter(task.id, fast_adapter.ip_address)
                            break
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(2.0)

    async def _resolve_fresh_url(self, task: DownloadTask, interface_ip: Optional[str], use_cache: bool) -> None:
        direct_url, filename, expected_size = await self.link_manager.resolve_link(
            task.original_url, interface=interface_ip, use_cache=use_cache
        )
        task.direct_url = direct_url
        if filename and (task.filename == "download.bin" or not task.filename):
            task.filename = filename
        if expected_size > 0:
            task.file_size = expected_size
            task.supports_range = True

    async def _execute_task_pipeline(self, task: DownloadTask, adapter: NetworkAdapter) -> None:
        current_adapter: Optional[NetworkAdapter] = adapter
        interface_ip = current_adapter.ip_address
        task.assigned_adapter = f"{current_adapter.friendly_type} ({interface_ip})"
        self._task_assigned_ip[task.id] = interface_ip
        task.status = TaskStatus.RESOLVING
        max_resolve_retries = max(1, self.max_retries)
        resolve_success = False
        failed_resolve_ips: Set[str] = set()

        for r_attempt in range(max_resolve_retries):
            if task.status != TaskStatus.RESOLVING:
                return
            live_ad = self._pick_live_adapter(preferred_ip=interface_ip, exclude_ips=failed_resolve_ips)
            if live_ad and live_ad.ip_address != interface_ip:
                old_label = task.assigned_adapter or interface_ip
                interface_ip = live_ad.ip_address
                task.assigned_adapter = f"{live_ad.friendly_type} ({interface_ip})"
                self._task_assigned_ip[task.id] = interface_ip
                self._record_failover_event(task, old_label, live_ad)

            try:
                await self._resolve_fresh_url(
                    task, interface_ip, use_cache=(r_attempt == 0 and task.retries == 0)
                )
                resolve_success = True
                break
            except asyncio.CancelledError:
                if task.status not in (TaskStatus.QUEUED, TaskStatus.PAUSED):
                    task.status = TaskStatus.PAUSED
                return
            except Exception as exc:
                err_str = str(exc)
                if "404" in err_str or "Dead link" in err_str:
                    task.status = TaskStatus.FAILED
                    task.error_message = err_str
                    return
                if interface_ip:
                    failed_resolve_ips.add(interface_ip)
                if r_attempt < max_resolve_retries - 1:
                    task.error_message = f"Failover resolve ({r_attempt + 1}/{max_resolve_retries}): {err_str}"
                    await asyncio.sleep(min(3.0, 0.8 * (r_attempt + 1)))
                else:
                    task.status = TaskStatus.FAILED
                    task.error_message = f"Resolve error: {err_str}"
                    await asyncio.sleep(1.0)
                    return

        if not resolve_success or not task.direct_url:
            return

        task.status = TaskStatus.DOWNLOADING
        try:
            await self._download_stream(task, interface_ip)
        except asyncio.CancelledError:
            if task.status not in (TaskStatus.QUEUED, TaskStatus.PAUSED):
                task.status = TaskStatus.PAUSED
            task.speed_bytes_sec = 0.0
        except Exception as exc:
            err_str = str(exc)
            task.speed_bytes_sec = 0.0
            self.auto_tuner.record_stream_error(interface_ip, err_str)
            self.apply_auto_tune()
            if any(k in err_str for k in ("HTML page", "403", "410", "expired", "Range", "200")):
                self.link_manager.invalidate_cache(task.original_url)
            if "404" in err_str or "Dead link" in err_str:
                task.status = TaskStatus.FAILED
                task.error_message = err_str
            else:
                task.retries += 1
                if task.retries < self.max_retries:
                    task.status = TaskStatus.QUEUED
                    task.error_message = f"Retrying ({task.retries}/{self.max_retries}): {err_str}"
                    await asyncio.sleep(min(4.0, 0.8 * task.retries))
                else:
                    task.status = TaskStatus.FAILED
                    task.error_message = f"Download error: {err_str}"
        finally:
            j = self._journals.get(task.id)
            if j and task.id in self.tasks and task.status != TaskStatus.COMPLETED:
                await j.save_async(force=True)
            self.trigger_queue_processing()

    @staticmethod
    def _preallocate_file(filepath: str, size: int) -> None:
        if size <= 0:
            return
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        if not os.path.exists(filepath) or os.path.getsize(filepath) < size:
            mode = "r+b" if os.path.exists(filepath) else "wb"
            with open(filepath, mode) as f:
                if sys.platform == "win32":
                    try:
                        handle = msvcrt.get_osfhandle(f.fileno())
                        bytes_returned = ctypes.c_ulong(0)
                        ctypes.windll.kernel32.DeviceIoControl(
                            handle,
                            FSCTL_SET_SPARSE,
                            None,
                            0,
                            None,
                            0,
                            ctypes.byref(bytes_returned),
                            None,
                        )
                    except Exception:
                        pass
                f.seek(size - 1)
                f.write(b"\0")
                f.flush()

    async def _download_stream(self, task: DownloadTask, interface_ip: Optional[str]) -> None:
        os.makedirs(task.destination_dir, exist_ok=True)

        if task.file_size <= 0:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                "Referer": "https://fuckingfast.co/",
                "Range": "bytes=0-0",
            }
            try:
                session = await self._get_session(interface_ip, force_http1=True)
                probe = await session.get(task.direct_url, headers=headers, stream=True, timeout=20)
                try:
                    if probe.status_code == 206:
                        cr = probe.headers.get("content-range", "")
                        if "/" in cr:
                            task.file_size = int(cr.split("/")[-1])
                        task.supports_range = True
                    elif probe.status_code == 200:
                        cl = probe.headers.get("content-length")
                        if cl:
                            task.file_size = int(cl)
                        task.supports_range = False
                finally:
                    await probe.aclose()
            except Exception:
                pass

        min_bytes = max(1, self.min_segment_file_size_mb) * 1024 * 1024
        effective_segs = self._compute_effective_segments(task)
        if task.supports_range and task.file_size >= min_bytes and effective_segs > 1:
            try:
                await self._download_segmented_stream(task, interface_ip, target_segments=effective_segs)
                return
            except RangeNotSupportedOrExpiredError:
                self.link_manager.invalidate_cache(task.original_url)
                await self._resolve_fresh_url(task, interface_ip, use_cache=False)
                await self._download_segmented_stream(task, interface_ip, target_segments=effective_segs)
                return

        self._active_pinned_ips.pop(task.id, None)
        await self._download_single_stream(task, interface_ip)

    async def _download_segmented_stream(
        self, task: DownloadTask, primary_ip: Optional[str], target_segments: Optional[int] = None
    ) -> None:
        part_filepath = os.path.join(task.destination_dir, f"{task.filename}.part")
        final_filepath = os.path.join(task.destination_dir, task.filename)

        await asyncio.to_thread(self._preallocate_file, part_filepath, task.file_size)
        journal = self._get_journal(task, part_filepath)
        task.downloaded_bytes = journal.total_downloaded()

        if journal.is_complete():
            journal.cleanup()
            if os.path.exists(part_filepath):
                os.replace(part_filepath, final_filepath)
            task.downloaded_bytes = task.file_size
            task.status = TaskStatus.COMPLETED
            task.speed_bytes_sec = 0.0
            task.eta_seconds = 0
            self._fire_complete_callback(task)
            return

        num_segments = target_segments if target_segments is not None else max(1, min(16, self.segments_per_file))
        hole_slices = journal.plan_hole_slices(num_segments)
        task.segments = [
            Segment(index=i, start_byte=s, end_byte=e, downloaded_bytes=0, status="QUEUED")
            for i, (s, e) in enumerate(hole_slices)
        ]

        pinned_ip = self._active_pinned_ips.pop(task.id, None)
        if pinned_ip:
            pinned_adapter = self.net_manager.get_adapter_by_ip(pinned_ip)
            allocated_adapters = [pinned_adapter] * len(task.segments)
        else:
            allocated_adapters = self.net_manager.allocate_segments_to_adapters(
                len(task.segments), strategy=getattr(self, "bonding_strategy", "ewma")
            )
        for s, ad in zip(task.segments, allocated_adapters):
            if ad:
                s.assigned_adapter = f"{ad.friendly_type} ({ad.ip_address})"
            else:
                s.assigned_adapter = primary_ip or "Default"

        writer = await asyncio.to_thread(PositionedFileWriter, part_filepath)
        bisect_lock = threading.Lock()
        unflushed_by_seg: Dict[int, int] = {}
        in_flight_by_seg: Dict[int, int] = {}
        active_worker_tasks: List[asyncio.Task] = []
        segment_ip_map: Dict[int, Optional[str]] = {}

        def refresh_task_speed() -> None:
            task.speed_bytes_sec = sum(
                s.speed_bytes_sec for s in task.segments if s.status == "DOWNLOADING"
            )
            rem = max(0, task.file_size - task.downloaded_bytes)
            task.eta_seconds = (
                int(rem / task.speed_bytes_sec)
                if task.speed_bytes_sec > 0 and rem > 0
                else None
            )

        def try_bisect_straggler() -> Optional[Segment]:
            if not self.work_stealing_enabled:
                return None
            with bisect_lock:
                best_seg: Optional[Segment] = None
                best_rem = 0
                best_committed = 0
                for cand in task.segments:
                    if cand.status == "DOWNLOADING":
                        sk = id(cand)
                        committed = (
                            cand.downloaded_bytes
                            + in_flight_by_seg.get(sk, 0)
                            + unflushed_by_seg.get(sk, 0)
                        )
                        rem = cand.total_bytes - committed
                        if rem >= self._MIN_SPLIT_BYTES and rem > best_rem:
                            best_rem = rem
                            best_seg = cand
                            best_committed = committed
                if best_seg is None:
                    return None

                curr_pos = best_seg.start_byte + best_committed
                mid = curr_pos + (best_rem // 2)
                old_end = best_seg.end_byte
                if mid <= curr_pos or mid >= old_end:
                    return None

                best_seg.end_byte = mid
                new_seg = Segment(
                    index=len(task.segments),
                    start_byte=mid + 1,
                    end_byte=old_end,
                    downloaded_bytes=0,
                    status="QUEUED",
                )
                task.segments.append(new_seg)
                return new_seg

        async def segment_worker(segment: Segment, adapter: Optional[NetworkAdapter], allow_steal: bool = True):
            if segment.downloaded_bytes >= segment.total_bytes:
                segment.downloaded_bytes = segment.total_bytes
                segment.status = "COMPLETED"
                return

            current_adapter = adapter
            adapter_ip = current_adapter.ip_address if current_adapter else primary_ip
            max_seg_retries = max(1, self.max_retries)
            seg_key = id(segment)
            failed_ips_for_seg: Set[str] = set()

            try:
                for attempt in range(max_seg_retries):
                    if task.status != TaskStatus.DOWNLOADING:
                        return

                    # Automatic per-segment NIC failover if current adapter dropped or is rate-limited
                    live_ad = self._pick_live_adapter(
                        preferred_ip=adapter_ip,
                        exclude_ips=failed_ips_for_seg if attempt > 0 else None,
                    )
                    if live_ad and live_ad.ip_address != adapter_ip:
                        old_ad_label = segment.assigned_adapter or adapter_ip or "Offline"
                        adapter_ip = live_ad.ip_address
                        current_adapter = live_ad
                        segment.assigned_adapter = f"{live_ad.friendly_type} ({adapter_ip})"
                        self._record_failover_event(task, old_ad_label, live_ad)

                    segment_ip_map[seg_key] = adapter_ip

                    cooldown = self.auto_tuner.get_cooldown_remaining(adapter_ip)
                    if cooldown > 0:
                        await asyncio.sleep(min(cooldown, 10.0))

                    segment.status = "DOWNLOADING"
                    with bisect_lock:
                        curr_offset = segment.start_byte + segment.downloaded_bytes
                        seg_end = segment.end_byte
                    if curr_offset > seg_end:
                        segment.status = "COMPLETED"
                        segment.speed_bytes_sec = 0.0
                        refresh_task_speed()
                        break

                    headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                        "Referer": "https://fuckingfast.co/",
                        "Range": f"bytes={curr_offset}-{seg_end}",
                        "Connection": "keep-alive" if attempt == 0 else "close",
                    }
                    resp = None
                    write_buf = bytearray()
                    conn_counted = False

                    async def flush_write_buffer() -> None:
                        nonlocal write_buf
                        with bisect_lock:
                            if not write_buf:
                                return
                            payload_bytes = bytes(write_buf)
                            write_buf.clear()
                            unflushed_by_seg[seg_key] = 0
                            in_flight_by_seg[seg_key] = len(payload_bytes)
                            write_start = segment.start_byte + segment.downloaded_bytes
                        try:
                            written_len = await asyncio.to_thread(
                                _commit_chunk_sync, writer, journal, write_start, payload_bytes
                            )
                        finally:
                            with bisect_lock:
                                in_flight_by_seg[seg_key] = 0
                        with bisect_lock:
                            segment.downloaded_bytes = min(
                                segment.total_bytes, segment.downloaded_bytes + written_len
                            )
                        task.downloaded_bytes = journal.total_downloaded()

                    try:
                        chunk_buffer = max(65536, self.buffer_size_kb * 1024)
                        speed_scale = 2 if task.speed_bytes_sec > 25 * 1024 * 1024 else 1
                        flush_threshold = max(1024 * 1024 * speed_scale, chunk_buffer)

                        if adapter_ip:
                            self.net_manager.increment_connections(adapter_ip)
                            conn_counted = True

                        session = await self._get_session(adapter_ip, force_http1=True)
                        resp = await session.get(task.direct_url, headers=headers, stream=True, timeout=25)

                        if resp.status_code in (429, 503):
                            ra_raw = resp.headers.get("retry-after", "")
                            ra_sec = float(ra_raw) if ra_raw.isdigit() else 6.0
                            raise RateLimitCooldownError(
                                f"Segment {segment.index} rate-limited (HTTP {resp.status_code})",
                                retry_after=ra_sec,
                            )
                        if resp.status_code == 200 and curr_offset > 0:
                            raise RangeNotSupportedOrExpiredError(
                                f"Server returned HTTP 200 instead of 206 for Range bytes={curr_offset}-{seg_end}"
                            )
                        if resp.status_code not in (200, 206):
                            raise ValueError(f"Segment {segment.index} failed with HTTP {resp.status_code}")

                        seg_last_check = time.monotonic()
                        seg_bytes_since = 0

                        chunk_iter = resp.aiter_content(chunk_buffer).__aiter__()
                        while task.status == TaskStatus.DOWNLOADING:
                            try:
                                chunk = await asyncio.wait_for(
                                    chunk_iter.__anext__(), timeout=self._READ_STALL_TIMEOUT_SEC
                                )
                            except StopAsyncIteration:
                                break
                            except asyncio.TimeoutError:
                                raise IOError(
                                    f"Segment {segment.index} stalled (0 bytes for {int(self._READ_STALL_TIMEOUT_SEC)}s)."
                                )

                            if not chunk:
                                continue

                            c_len = len(chunk)
                            with bisect_lock:
                                committed_now = (
                                    segment.downloaded_bytes
                                    + in_flight_by_seg.get(seg_key, 0)
                                    + len(write_buf)
                                )
                                remaining_in_seg = segment.total_bytes - committed_now
                            if remaining_in_seg <= 0:
                                break
                            if c_len > remaining_in_seg:
                                chunk = chunk[:remaining_in_seg]
                                c_len = len(chunk)

                            write_buf.extend(chunk)
                            with bisect_lock:
                                unflushed_by_seg[seg_key] = len(write_buf)

                            if len(write_buf) >= flush_threshold:
                                await flush_write_buffer()

                            seg_bytes_since += c_len

                            if self.speed_limit_mbps > 0:
                                await self._token_limiter.consume(c_len)

                            if adapter_ip:
                                self.net_manager.record_chunk_transfer(adapter_ip, c_len)

                            now = time.monotonic()
                            delta = now - seg_last_check
                            if delta >= 0.5:
                                segment.speed_bytes_sec = seg_bytes_since / delta
                                seg_bytes_since = 0
                                seg_last_check = now
                                refresh_task_speed()

                        await flush_write_buffer()

                        with bisect_lock:
                            done = segment.downloaded_bytes >= segment.total_bytes
                            if done:
                                segment.downloaded_bytes = segment.total_bytes
                        if done:
                            segment.status = "COMPLETED"
                            segment.speed_bytes_sec = 0.0
                            refresh_task_speed()
                            self.auto_tuner.record_stream_success(adapter_ip)
                            break
                        elif task.status != TaskStatus.DOWNLOADING:
                            segment.speed_bytes_sec = 0.0
                            refresh_task_speed()
                            await journal.save_async(force=True)
                            return
                        else:
                            raise IOError(f"Segment {segment.index} stream truncated prematurely.")
                    except asyncio.CancelledError:
                        await flush_write_buffer()
                        if task.id in self.tasks:
                            await journal.save_async(force=True)
                        segment.speed_bytes_sec = 0.0
                        refresh_task_speed()
                        raise
                    except RangeNotSupportedOrExpiredError:
                        await flush_write_buffer()
                        await journal.save_async(force=True)
                        segment.speed_bytes_sec = 0.0
                        refresh_task_speed()
                        raise
                    except RateLimitCooldownError as rl_exc:
                        await flush_write_buffer()
                        segment.speed_bytes_sec = 0.0
                        refresh_task_speed()
                        self.auto_tuner.register_cooldown(adapter_ip, rl_exc.retry_after)
                        self.apply_auto_tune()
                        if adapter_ip:
                            failed_ips_for_seg.add(adapter_ip)
                        if task.status != TaskStatus.DOWNLOADING:
                            return
                        if attempt == max_seg_retries - 1:
                            segment.status = "FAILED"
                            raise
                        await asyncio.sleep(0.35 + random.uniform(0.1, 0.4))
                    except Exception as seg_exc:
                        await flush_write_buffer()
                        segment.speed_bytes_sec = 0.0
                        refresh_task_speed()
                        self.auto_tuner.record_stream_error(adapter_ip, str(seg_exc))
                        self.apply_auto_tune()
                        if adapter_ip:
                            failed_ips_for_seg.add(adapter_ip)
                        if task.status != TaskStatus.DOWNLOADING:
                            return
                        if attempt == max_seg_retries - 1:
                            segment.status = "FAILED"
                            raise
                        if any(k in str(seg_exc) for k in ("403", "410", "expired")):
                            self.link_manager.invalidate_cache(task.original_url)
                            await self._resolve_fresh_url(task, adapter_ip, use_cache=False)
                        # Fast retry when another adapter is available for immediate failover
                        has_alt = self._pick_live_adapter(exclude_ips=failed_ips_for_seg) is not None
                        backoff = 0.25 if has_alt else (min(5.0, 1.0 * (2 ** attempt)) + random.uniform(0.1, 0.4))
                        await asyncio.sleep(backoff)
                    finally:
                        if conn_counted and adapter_ip:
                            self.net_manager.decrement_connections(adapter_ip)
                        if resp is not None:
                            try:
                                await resp.aclose()
                            except Exception:
                                pass

                # Non-destructive intra-file work stealing upon segment completion
                if allow_steal and task.status == TaskStatus.DOWNLOADING:
                    stolen = try_bisect_straggler()
                    if stolen is not None:
                        total_budget = max(1, self.max_concurrency * self.segments_per_file)
                        best_ad = self.net_manager.get_best_available_adapter(total_budget)
                        chosen_ad = best_ad if best_ad is not None else current_adapter
                        stolen.assigned_adapter = (
                            f"{chosen_ad.friendly_type} ({chosen_ad.ip_address})"
                            if chosen_ad
                            else segment.assigned_adapter
                        )
                        t = asyncio.create_task(segment_worker(stolen, chosen_ad, allow_steal=True))
                        active_worker_tasks.append(t)
            finally:
                unflushed_by_seg.pop(seg_key, None)
                in_flight_by_seg.pop(seg_key, None)
                segment_ip_map.pop(seg_key, None)

        async def hotplug_and_tail_scaler_loop() -> None:
            while task.status == TaskStatus.DOWNLOADING:
                await asyncio.sleep(1.0)
                if task.status != TaskStatus.DOWNLOADING:
                    break

                live_workers = sum(1 for t in active_worker_tasks if not t.done())
                target_workers = self._compute_effective_segments(task)
                total_budget = max(1, self.max_concurrency * target_workers)
                best_ad = self.net_manager.get_best_available_adapter(total_budget)

                if best_ad is None or best_ad.active_connections >= best_ad.max_concurrency_cap:
                    continue

                # Scale up if worker count < target OR if fast adapter is >=2x faster than a straggler
                should_steal = live_workers < target_workers
                if not should_steal and self.work_stealing_enabled and live_workers < 16:
                    slowest_speed = min(
                        (s.speed_bytes_sec for s in task.segments if s.status == "DOWNLOADING"),
                        default=0.0,
                    )
                    if best_ad.ewma_speed_bytes > max(2 * 1024 * 1024, slowest_speed * 2.2):
                        should_steal = True

                if should_steal:
                    stolen = try_bisect_straggler()
                    if stolen is not None:
                        stolen.assigned_adapter = f"{best_ad.friendly_type} ({best_ad.ip_address})"
                        t = asyncio.create_task(segment_worker(stolen, best_ad, allow_steal=True))
                        active_worker_tasks.append(t)

        scaler_task = asyncio.create_task(hotplug_and_tail_scaler_loop())
        try:
            for seg, ad in zip(task.segments, allocated_adapters):
                active_worker_tasks.append(asyncio.create_task(segment_worker(seg, ad)))

            while True:
                pending = [t for t in active_worker_tasks if not t.done()]
                if not pending:
                    break
                await asyncio.gather(*pending)
        finally:
            scaler_task.cancel()
            for t in active_worker_tasks:
                if not t.done():
                    t.cancel()
            await asyncio.to_thread(writer.close)

        if task.status == TaskStatus.DOWNLOADING:
            await journal.save_async(force=True)
            if journal.is_complete() or task.downloaded_bytes >= task.file_size:
                journal.cleanup()
                if os.path.exists(part_filepath):
                    os.replace(part_filepath, final_filepath)
                task.downloaded_bytes = task.file_size
                task.status = TaskStatus.COMPLETED
                task.speed_bytes_sec = 0.0
                task.eta_seconds = 0
                self._fire_complete_callback(task)

    async def _download_single_stream(self, task: DownloadTask, interface_ip: Optional[str]) -> None:
        part_filepath = os.path.join(task.destination_dir, f"{task.filename}.part")
        final_filepath = os.path.join(task.destination_dir, task.filename)

        existing_bytes = os.path.getsize(part_filepath) if os.path.exists(part_filepath) else 0
        task.downloaded_bytes = existing_bytes

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Referer": "https://fuckingfast.co/",
            "Accept": "*/*",
            "Connection": "keep-alive",
        }
        if existing_bytes > 0:
            headers["Range"] = f"bytes={existing_bytes}-"

        resp = None
        conn_counted = False
        try:
            if interface_ip:
                self.net_manager.increment_connections(interface_ip)
                conn_counted = True

            session = await self._get_session(interface_ip, force_http1=True)
            resp = await session.get(task.direct_url, headers=headers, stream=True, timeout=25)

            content_type = resp.headers.get("content-type", "").lower()
            if content_type.startswith("text/html"):
                raise ValueError("Server returned an HTML page instead of binary file. Direct link may have expired.")

            if resp.status_code == 206:
                write_mode = "ab"
                cr = resp.headers.get("content-range", "")
                if "/" in cr:
                    try:
                        task.file_size = int(cr.split("/")[-1])
                    except ValueError:
                        pass
            elif resp.status_code == 200:
                if existing_bytes > 0:
                    raise RangeNotSupportedOrExpiredError(
                        f"Server ignored Range header (HTTP 200) with {existing_bytes} bytes already on disk."
                    )
                write_mode = "wb"
                cl = resp.headers.get("content-length")
                if cl:
                    task.file_size = int(cl)
            elif resp.status_code == 416:
                task.downloaded_bytes = existing_bytes
                task.file_size = existing_bytes
                if os.path.exists(part_filepath):
                    os.replace(part_filepath, final_filepath)
                task.status = TaskStatus.COMPLETED
                self._fire_complete_callback(task)
                return
            else:
                raise ValueError(f"HTTP {resp.status_code} received from server.")

            if task.file_size == 0 and "content-length" in resp.headers:
                task.file_size = existing_bytes + int(resp.headers["content-length"])

            chunk_size = max(65536, self.buffer_size_kb * 1024)
            last_speed_check = time.monotonic()
            bytes_since_last_check = 0
            is_first_chunk = True

            async with aiofiles.open(part_filepath, write_mode) as f:
                chunk_iter = resp.aiter_content(chunk_size).__aiter__()
                while task.status == TaskStatus.DOWNLOADING:
                    try:
                        chunk = await asyncio.wait_for(
                            chunk_iter.__anext__(), timeout=self._READ_STALL_TIMEOUT_SEC
                        )
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        raise IOError(f"Stream stalled (0 bytes for {int(self._READ_STALL_TIMEOUT_SEC)}s).")

                    if not chunk:
                        continue

                    if is_first_chunk and write_mode == "wb":
                        is_first_chunk = False
                        chunk_head = chunk[:20].lower()
                        if chunk_head.startswith((b"<!doctype html", b"<html")):
                            raise ValueError(
                                "Server returned an HTML page instead of binary file. Direct link may have expired."
                            )

                    await f.write(chunk)
                    chunk_len = len(chunk)
                    task.downloaded_bytes += chunk_len
                    bytes_since_last_check += chunk_len

                    if self.speed_limit_mbps > 0:
                        await self._token_limiter.consume(chunk_len)

                    if interface_ip:
                        self.net_manager.record_chunk_transfer(interface_ip, chunk_len)

                    now = time.monotonic()
                    delta = now - last_speed_check
                    if delta >= 0.5:
                        speed = bytes_since_last_check / delta
                        task.speed_bytes_sec = speed
                        bytes_since_last_check = 0
                        last_speed_check = now
                        remaining_bytes = max(0, task.file_size - task.downloaded_bytes)
                        task.eta_seconds = (
                            int(remaining_bytes / speed) if speed > 0 and remaining_bytes > 0 else None
                        )
                await f.flush()

            if task.status == TaskStatus.DOWNLOADING:
                if task.file_size > 0 and task.downloaded_bytes < task.file_size:
                    raise IOError(
                        f"Stream truncated prematurely ({task.downloaded_bytes}/{task.file_size} bytes downloaded)."
                    )
                elif task.file_size == 0 and task.downloaded_bytes < 1024:
                    raise IOError(
                        f"Suspiciously small download ({task.downloaded_bytes} bytes). Possible broken link."
                    )

                if os.path.exists(part_filepath):
                    os.replace(part_filepath, final_filepath)

                self.auto_tuner.record_stream_success(interface_ip)
                task.status = TaskStatus.COMPLETED
                task.speed_bytes_sec = 0.0
                task.eta_seconds = 0
                self._fire_complete_callback(task)
        finally:
            if conn_counted and interface_ip:
                self.net_manager.decrement_connections(interface_ip)
            if resp is not None:
                try:
                    await resp.aclose()
                except Exception:
                    pass