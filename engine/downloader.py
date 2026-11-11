"""High-Performance Async Download Engine with Adaptive Scheduling & Work-Stealing.

Features:
- Proportional adaptive slot allocation based on real-time EWMA bandwidth.
- Autonomous Straggler Work-Stealing / Hand-Off (fast idle adapters take over lagging parts).
- Manual 1-click network hand-off per task.
- Anti-429 per-interface connection limits.
- Pipelined background pre-resolving.
- Resumable Range streaming directly to .part files.
"""

import asyncio
import os
import re
import subprocess
import time
from typing import Callable, Dict, List, Optional
import aiofiles
from curl_cffi import requests

from engine.network import NetworkAdapter, NetworkManager
from engine.session import SessionManager
from engine.task import DownloadTask, Segment, TaskStatus
from resolvers.manager import LinkManager


class TokenBucketLimiter:
    """Async token bucket for enforcing global download bandwidth caps without busy-waiting."""

    def __init__(self, rate_mbps: float = 0.0):
        self.rate_bytes_sec = rate_mbps * 1024 * 1024 if rate_mbps > 0 else 0.0
        self.tokens = self.rate_bytes_sec
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()

    def set_rate(self, rate_mbps: float) -> None:
        self.rate_bytes_sec = rate_mbps * 1024 * 1024 if rate_mbps > 0 else 0.0
        self.tokens = max(self.tokens, self.rate_bytes_sec)

    async def consume(self, amount: int) -> None:
        if self.rate_bytes_sec <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_update
            self.last_update = now
            self.tokens = min(self.rate_bytes_sec, self.tokens + elapsed * self.rate_bytes_sec)
            if self.tokens < amount:
                deficit = amount - self.tokens
                wait_time = deficit / self.rate_bytes_sec
                if wait_time > 0:
                    await asyncio.sleep(min(wait_time, 1.0))
                self.tokens = 0.0
            else:
                self.tokens -= amount


class AsyncDownloader:
    """Core download manager handling tasks, adaptive workers, and work-stealing."""

    def __init__(
        self,
        link_manager: LinkManager,
        net_manager: NetworkManager,
        session_manager: SessionManager,
        max_concurrency: int = 6,
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
        self._session_pool: Dict[Optional[str], requests.AsyncSession] = {}
        self._queue_event = asyncio.Event()

        # Acceleration & Engine settings
        self.segments_per_file: int = 4
        self.buffer_size_kb: int = 256
        self.speed_limit_mbps: float = 0.0
        self._token_limiter = TokenBucketLimiter(0.0)
        self.max_retries: int = 3
        self.min_segment_file_size_mb: int = 10
        self.bonding_strategy: str = "ewma"
        self.delete_archive_after_extract: bool = False

    def trigger_queue_processing(self) -> None:
        """Signals the queue loop to immediately inspect and dispatch eligible tasks."""
        self._queue_event.set()

    def set_speed_limit(self, limit_mbps: float) -> None:
        """Updates the global bandwidth cap."""
        self.speed_limit_mbps = max(0.0, limit_mbps)
        self._token_limiter.set_rate(self.speed_limit_mbps)

    async def _get_session(self, adapter_ip: Optional[str]) -> requests.AsyncSession:
        """Retrieves or creates a pooled AsyncSession for the given adapter interface."""
        session = self._session_pool.get(adapter_ip)
        if session is None or getattr(session, "_closed", False):
            s_kwargs = {"impersonate": "chrome", "max_clients": 32}
            if adapter_ip:
                s_kwargs["interface"] = adapter_ip
            session = requests.AsyncSession(**s_kwargs)
            self._session_pool[adapter_ip] = session
        return session

    def _evict_session(self, adapter_ip: Optional[str]) -> None:
        """Closes and removes a session from pool upon socket failure."""
        s = self._session_pool.pop(adapter_ip, None)
        if s and not getattr(s, "_closed", False):
            try:
                asyncio.create_task(s.close())
            except Exception:
                pass

    def set_concurrency(self, limit: int) -> None:
        """Updates max simultaneous downloads limit."""
        self.max_concurrency = max(1, min(12, limit))

    def add_task(self, original_url: str, destination_dir: str) -> DownloadTask:
        """Adds a new URL to the download queue and preserves natural part ordering."""
        cand_name = "download.bin"
        import urllib.parse
        parsed = urllib.parse.urlparse(original_url)
        if parsed.fragment:
            cand_name = urllib.parse.unquote(parsed.fragment)
        elif parsed.path:
            segs = [s for s in parsed.path.split("/") if s]
            if segs:
                cand_name = segs[-1]

        task = DownloadTask(
            original_url=original_url,
            destination_dir=destination_dir,
            filename=cand_name,
        )
        self.tasks[task.id] = task
        self.sort_tasks("natural_part")
        self.trigger_queue_processing()
        return task

    def sort_tasks(self, criterion: str = "natural_part") -> None:
        """Sorts download tasks by natural part number, status, progress, or size."""
        def natural_key(text: str):
            return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', text or "")]

        def get_sort_name(t: DownloadTask) -> str:
            if t.filename and t.filename != "download.bin":
                return t.filename
            import urllib.parse
            p = urllib.parse.urlparse(t.original_url)
            if p.fragment:
                return urllib.parse.unquote(p.fragment)
            if p.path:
                segs = [s for s in p.path.split('/') if s]
                if segs:
                    return segs[-1]
            return t.original_url

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
        """Moves a task to top, up, or down in the download queue."""
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
        """Removes all completed tasks from the queue."""
        to_remove = [tid for tid, t in self.tasks.items() if t.status == TaskStatus.COMPLETED]
        for tid in to_remove:
            del self.tasks[tid]
        return len(to_remove)

    def clear_all(self) -> int:
        """Cancels and purges all tasks from the queue."""
        self.pause_all()
        count = len(self.tasks)
        self.tasks.clear()
        return count

    @staticmethod
    def open_download_folder(folder_path: str) -> bool:
        """Opens the download directory in Windows File Explorer."""
        try:
            if os.path.exists(folder_path):
                subprocess.Popen(["explorer.exe", folder_path])
                return True
        except Exception:
            pass
        return False

    def reveal_task_file(self, task_id: str) -> bool:
        """Highlights the task file in Windows File Explorer."""
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
        """Pauses an active or queued download."""
        task = self.tasks.get(task_id)
        if not task:
            return False

        if task.status in (TaskStatus.DOWNLOADING, TaskStatus.RESOLVING):
            task.status = TaskStatus.PAUSED
            if task_id in self._active_async_tasks:
                self._active_async_tasks[task_id].cancel()
            return True
        elif task.status == TaskStatus.QUEUED:
            task.status = TaskStatus.PAUSED
            return True
        return False

    def resume_task(self, task_id: str) -> bool:
        """Resumes a paused or failed task."""
        task = self.tasks.get(task_id)
        if not task:
            return False

        if task.status in (TaskStatus.PAUSED, TaskStatus.FAILED):
            task.status = TaskStatus.QUEUED
            task.error_message = None
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
        for task in self.tasks.values():
            if task.status == TaskStatus.FAILED:
                task.status = TaskStatus.QUEUED
                task.error_message = None
                task.retries = 0
        self.trigger_queue_processing()

    def remove_task(self, task_id: str) -> bool:
        self.pause_task(task_id)
        if task_id in self.tasks:
            del self.tasks[task_id]
            return True
        return False

    async def handoff_task_to_adapter(self, task_id: str, target_ip: str) -> bool:
        """Manually or autonomously hands off an active download to a specific network adapter."""
        task = self.tasks.get(task_id)
        if not task or task.status != TaskStatus.DOWNLOADING:
            return False

        target_adapter = self.net_manager.get_adapter_by_ip(target_ip)
        if not target_adapter or not target_adapter.is_enabled or not target_adapter.is_up:
            return False

        current_ip = self._task_assigned_ip.get(task_id)
        if current_ip == target_ip:
            return False  # Already on target adapter

        # Record hand-off event
        event = {
            "timestamp": time.time(),
            "task_id": task_id,
            "filename": task.filename,
            "from_adapter": task.assigned_adapter or "Unknown",
            "to_adapter": f"{target_adapter.friendly_type} ({target_ip})",
        }
        self.handoff_events.append(event)
        # Keep last 10 events
        if len(self.handoff_events) > 10:
            self.handoff_events.pop(0)

        # Cancel current running coroutine to trigger clean checkpointing
        if task_id in self._active_async_tasks:
            self._active_async_tasks[task_id].cancel()

        # Mark for immediate restart on target adapter
        task.assigned_adapter = f"{target_adapter.friendly_type} ({target_ip})"
        self._task_assigned_ip[task_id] = target_ip
        task.status = TaskStatus.QUEUED
        return True

    async def start(self) -> None:
        """Starts the background worker queue and straggler watchdog loops."""
        self._is_running = True
        self._worker_loop_task = asyncio.create_task(self._process_queue_loop())
        self._straggler_loop_task = asyncio.create_task(self._straggler_watchdog_loop())

    async def stop(self) -> None:
        """Stops all downloads and shuts down worker loops."""
        self._is_running = False
        self.pause_all()
        if self._worker_loop_task:
            self._worker_loop_task.cancel()
        if self._straggler_loop_task:
            self._straggler_loop_task.cancel()
        for s in list(self._session_pool.values()):
            try:
                if not getattr(s, "_closed", False):
                    await s.close()
            except Exception:
                pass
        self._session_pool.clear()

    async def _process_queue_loop(self) -> None:
        """Adaptive queue loop with pipelined pre-resolving and event-driven dispatch."""
        last_pre_resolve_time = 0.0
        while self._is_running:
            try:
                # 1. Pipelined background pre-resolution of upcoming queued tasks
                queued_tasks = [t for t in self.tasks.values() if t.status == TaskStatus.QUEUED]
                now = time.time()
                if queued_tasks and (now - last_pre_resolve_time >= 2.0):
                    last_pre_resolve_time = now
                    upcoming_urls = [t.original_url for t in queued_tasks[:5]]
                    asyncio.create_task(self.link_manager.pre_resolve_batch(upcoming_urls))

                # 2. Check total active download streams
                active_count = len([t for t in self.tasks.values() if t.status == TaskStatus.DOWNLOADING])

                if active_count < self.max_concurrency:
                    # Select best available adapter based on EWMA speed & quotas
                    best_adapter = self.net_manager.get_best_available_adapter(self.max_concurrency)
                    if best_adapter and queued_tasks:
                        task = queued_tasks[0]
                        if task.id not in self._active_async_tasks or self._active_async_tasks[task.id].done():
                            t = asyncio.create_task(self._execute_task_pipeline(task, best_adapter))
                            self._active_async_tasks[task.id] = t

                # Zero-latency wakeup on task completion or new tasks, with 0.3s fallback
                try:
                    await asyncio.wait_for(self._queue_event.wait(), timeout=0.3)
                    self._queue_event.clear()
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(0.5)

    async def _straggler_watchdog_loop(self) -> None:
        """Watchdog: Detects idle high-speed adapters and steals straggler downloads."""
        while self._is_running:
            try:
                await asyncio.sleep(2.5)

                # Check if there are no unstarted queued tasks left
                has_queued = any(t.status == TaskStatus.QUEUED for t in self.tasks.values())
                if has_queued:
                    continue

                adapters = [a for a in self.net_manager.get_available_adapters() if a.is_enabled and a.is_up]
                if len(adapters) < 2:
                    continue

                # Sort adapters by EWMA speed descending
                adapters.sort(key=lambda a: a.ewma_speed_bytes, reverse=True)
                fast_adapter = adapters[0]
                slow_adapter = adapters[-1]

                # Check if fast adapter is idle (0 active connections) or significantly faster
                fast_speed = fast_adapter.ewma_speed_bytes
                slow_speed = slow_adapter.ewma_speed_bytes

                if fast_adapter.active_connections < fast_adapter.max_concurrency_cap:
                    # Fast adapter has capacity. Check for stragglers on slower adapter
                    active_on_slow = [
                        t for t in self.tasks.values()
                        if t.status == TaskStatus.DOWNLOADING
                        and self._task_assigned_ip.get(t.id) == slow_adapter.ip_address
                    ]

                    for task in active_on_slow:
                        remaining = task.file_size - task.downloaded_bytes
                        # If more than 50 MB remaining and fast adapter is at least 2.0x faster
                        if remaining > 50 * 1024 * 1024 and (fast_speed > 2.0 * slow_speed or slow_speed < 1024 * 1024):
                            # Steal the task!
                            await self.handoff_task_to_adapter(task.id, fast_adapter.ip_address)
                            break
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(2.0)

    async def _execute_task_pipeline(self, task: DownloadTask, adapter: NetworkAdapter) -> None:
        """Executes the resolve -> stream pipeline for a task on the allocated adapter."""
        interface_ip = adapter.ip_address
        task.assigned_adapter = f"{adapter.friendly_type} ({interface_ip})"
        self._task_assigned_ip[task.id] = interface_ip

        # 1. Resolve direct link (using pre-resolved cache if available) with retry
        task.status = TaskStatus.RESOLVING
        max_resolve_retries = max(1, self.max_retries)
        resolve_success = False

        for r_attempt in range(max_resolve_retries):
            if task.status != TaskStatus.RESOLVING:
                return
            try:
                direct_url, filename, expected_size = await self.link_manager.resolve_link(
                    task.original_url, interface=interface_ip
                )
                task.direct_url = direct_url
                if filename and (task.filename == "download.bin" or not task.filename):
                    task.filename = filename
                if expected_size > 0:
                    task.file_size = expected_size
                resolve_success = True
                break
            except Exception as exc:
                err_str = str(exc)
                if "404" in err_str or "Dead link" in err_str:
                    task.status = TaskStatus.FAILED
                    task.error_message = err_str
                    return
                if r_attempt < max_resolve_retries - 1:
                    task.error_message = f"Resolving retry ({r_attempt + 1}/{max_resolve_retries}): {err_str}"
                    await asyncio.sleep(min(4.0, 1.5 * (r_attempt + 1)))
                else:
                    task.status = TaskStatus.FAILED
                    task.error_message = f"Resolve error: {err_str}"
                    # Rapid failure circuit breaker: pause briefly to prevent cascading failure loop down queue
                    await asyncio.sleep(1.5)
                    return

        if not resolve_success or not task.direct_url:
            return

        # 2. Download file stream with Range resuming
        task.status = TaskStatus.DOWNLOADING
        self.net_manager.increment_connections(interface_ip)
        try:
            await self._download_stream(task, interface_ip)
        except asyncio.CancelledError:
            if task.status != TaskStatus.PAUSED:
                task.status = TaskStatus.PAUSED
        except Exception as exc:
            err_str = str(exc)
            if "404" in err_str or "Dead link" in err_str or "HTML page" in err_str:
                task.status = TaskStatus.FAILED
                task.error_message = err_str
            else:
                task.retries += 1
                if task.retries < self.max_retries:
                    task.status = TaskStatus.QUEUED
                    task.error_message = f"Retrying ({task.retries}/{self.max_retries}): {err_str}"
                else:
                    task.status = TaskStatus.FAILED
                    task.error_message = f"Download error: {err_str}"
        finally:
            self.net_manager.decrement_connections(interface_ip)
            self.trigger_queue_processing()

    @staticmethod
    def _preallocate_file(filepath: str, size: int) -> None:
        """Instantly pre-allocates a sparse target file on disk to reserve space."""
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        if not os.path.exists(filepath) or os.path.getsize(filepath) < size:
            with open(filepath, "wb") as f:
                f.seek(size - 1)
                f.write(b"\0")

    async def _download_stream(self, task: DownloadTask, interface_ip: Optional[str]) -> None:
        """High-throughput download router with parallel multi-segment acceleration and single-stream fallback."""
        os.makedirs(task.destination_dir, exist_ok=True)

        # Probe file size & Range support if not already determined
        if task.file_size <= 0:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                "Referer": "https://fuckingfast.co/",
                "Range": "bytes=0-0",
            }
            s_kwargs = {"impersonate": "chrome"}
            if interface_ip:
                s_kwargs["interface"] = interface_ip
            try:
                async with requests.AsyncSession(**s_kwargs) as s:
                    probe = await s.get(task.direct_url, headers=headers, stream=True, timeout=20)
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
            except Exception:
                pass

        # Trigger parallel segmented chunking if file >= min_segment_file_size_mb and Range supported
        min_bytes = max(1, self.min_segment_file_size_mb) * 1024 * 1024
        if task.supports_range and task.file_size >= min_bytes and self.segments_per_file > 1:
            try:
                await self._download_segmented_stream(task, interface_ip)
                return
            except Exception as e:
                err_text = str(e)
                if "Range" in err_text or "206" in err_text:
                    task.supports_range = False
                else:
                    raise

        # Single-stream fallback
        await self._download_single_stream(task, interface_ip)

    async def _download_segmented_stream(self, task: DownloadTask, primary_ip: Optional[str]) -> None:
        """Downloads file partitioned into parallel byte-range segments directly into sparse file."""
        part_filepath = os.path.join(task.destination_dir, f"{task.filename}.part")
        final_filepath = os.path.join(task.destination_dir, task.filename)

        # 1. Pre-allocate sparse file
        self._preallocate_file(part_filepath, task.file_size)

        # 2. Partition into segments
        num_segments = max(1, min(16, self.segments_per_file))
        if not task.segments or len(task.segments) != num_segments:
            seg_size = task.file_size // num_segments
            task.segments = []
            for i in range(num_segments):
                start = i * seg_size
                end = (start + seg_size - 1) if i < num_segments - 1 else (task.file_size - 1)
                task.segments.append(
                    Segment(index=i, start_byte=start, end_byte=end, downloaded_bytes=0, status="QUEUED")
                )

        # Check existing segment progress if resuming
        total_pre_existing = sum(s.downloaded_bytes for s in task.segments)
        task.downloaded_bytes = total_pre_existing

        # Allocate adapters across segments using EWMA weighting or configured bonding strategy
        allocated_adapters = self.net_manager.allocate_segments_to_adapters(
            len(task.segments), strategy=getattr(self, "bonding_strategy", "ewma")
        )
        for s, ad in zip(task.segments, allocated_adapters):
            if ad:
                s.assigned_adapter = f"{ad.friendly_type} ({ad.ip_address})"
            else:
                s.assigned_adapter = primary_ip or "Default"

        chunk_buffer = max(65536, self.buffer_size_kb * 1024)

        async def segment_worker(segment: Segment, adapter: Optional[NetworkAdapter]):
            if segment.downloaded_bytes >= segment.total_bytes:
                segment.status = "COMPLETED"
                return

            adapter_ip = adapter.ip_address if adapter else primary_ip
            max_seg_retries = max(1, self.max_retries)

            for attempt in range(max_seg_retries):
                if task.status != TaskStatus.DOWNLOADING:
                    return

                segment.status = "DOWNLOADING"
                curr_offset = segment.start_byte + segment.downloaded_bytes
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                    "Referer": "https://fuckingfast.co/",
                    "Range": f"bytes={curr_offset}-{segment.end_byte}",
                    "Connection": "keep-alive",
                }

                f = None
                try:
                    session = await self._get_session(adapter_ip)
                    resp = await session.get(task.direct_url, headers=headers, stream=True, timeout=30)
                    if resp.status_code not in (200, 206):
                        raise ValueError(f"Segment {segment.index} failed with HTTP {resp.status_code}")

                    seg_last_check = time.time()
                    seg_bytes_since = 0

                    f = open(part_filepath, "r+b")
                    await asyncio.to_thread(f.seek, curr_offset)

                    async for chunk in resp.aiter_content(chunk_buffer):
                        if task.status != TaskStatus.DOWNLOADING:
                            break
                        if chunk:
                            await asyncio.to_thread(f.write, chunk)
                            c_len = len(chunk)
                            segment.downloaded_bytes += c_len
                            task.downloaded_bytes += c_len
                            seg_bytes_since += c_len

                            # Enforce global speed limit if configured
                            if self.speed_limit_mbps > 0:
                                await self._token_limiter.consume(c_len)

                            if adapter_ip:
                                self.net_manager.record_chunk_transfer(adapter_ip, c_len)

                            now = time.time()
                            delta = now - seg_last_check
                            if delta >= 0.5:
                                seg_spd = seg_bytes_since / delta
                                segment.speed_bytes_sec = seg_spd
                                seg_bytes_since = 0
                                seg_last_check = now

                                # Race-condition-free aggregate speed calculation
                                task.speed_bytes_sec = sum(s.speed_bytes_sec for s in task.segments)
                                rem = max(0, task.file_size - task.downloaded_bytes)
                                task.eta_seconds = (
                                    int(rem / task.speed_bytes_sec)
                                    if task.speed_bytes_sec > 0 and rem > 0
                                    else None
                                )

                    if segment.downloaded_bytes >= segment.total_bytes:
                        segment.status = "COMPLETED"
                        segment.speed_bytes_sec = 0.0
                        return
                    elif task.status != TaskStatus.DOWNLOADING:
                        return
                    else:
                        raise IOError(f"Segment {segment.index} stream truncated prematurely.")

                except Exception:
                    self._evict_session(adapter_ip)
                    if task.status != TaskStatus.DOWNLOADING:
                        return
                    if attempt == max_seg_retries - 1:
                        segment.status = "FAILED"
                        raise
                    await asyncio.sleep(min(2.0, 0.5 * (2 ** attempt)))
                finally:
                    if f:
                        await asyncio.to_thread(f.close)

        # Run all segment workers concurrently with per-segment isolation and retry
        active_pairs = [
            (seg, ad) for seg, ad in zip(task.segments, allocated_adapters) if seg.status != "COMPLETED"
        ]
        await asyncio.gather(*(segment_worker(seg, ad) for seg, ad in active_pairs))

        # Finalize if completed
        if task.status == TaskStatus.DOWNLOADING:
            all_done = all(seg.status == "COMPLETED" for seg in task.segments)
            if all_done and task.downloaded_bytes >= task.file_size:
                if os.path.exists(final_filepath):
                    os.remove(final_filepath)
                if os.path.exists(part_filepath):
                    os.rename(part_filepath, final_filepath)

                task.status = TaskStatus.COMPLETED
                task.speed_bytes_sec = 0.0
                task.eta_seconds = 0
                if self.on_complete_callback:
                    self.on_complete_callback(task)

    async def _download_single_stream(self, task: DownloadTask, interface_ip: Optional[str]) -> None:
        """Streams file chunks to disk using a single HTTP connection (fallback mode)."""
        part_filepath = os.path.join(task.destination_dir, f"{task.filename}.part")
        final_filepath = os.path.join(task.destination_dir, task.filename)

        existing_bytes = 0
        if os.path.exists(part_filepath):
            existing_bytes = os.path.getsize(part_filepath)
            task.downloaded_bytes = existing_bytes

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Referer": "https://fuckingfast.co/",
            "Accept": "*/*",
            "Connection": "keep-alive",
        }

        if existing_bytes > 0:
            headers["Range"] = f"bytes={existing_bytes}-"

        try:
            session = await self._get_session(interface_ip)
            resp = await session.get(task.direct_url, headers=headers, stream=True, timeout=30)

            # Content-Type guardrail
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
                write_mode = "wb"
                existing_bytes = 0
                task.downloaded_bytes = 0
                cl = resp.headers.get("content-length")
                if cl:
                    task.file_size = int(cl)
            elif resp.status_code == 416:
                task.downloaded_bytes = existing_bytes
                task.file_size = existing_bytes
                if os.path.exists(part_filepath):
                    if os.path.exists(final_filepath):
                        os.remove(final_filepath)
                    os.rename(part_filepath, final_filepath)
                task.status = TaskStatus.COMPLETED
                if self.on_complete_callback:
                    self.on_complete_callback(task)
                return
            else:
                raise ValueError(f"HTTP {resp.status_code} received from server.")

            if task.file_size == 0 and "content-length" in resp.headers:
                task.file_size = existing_bytes + int(resp.headers["content-length"])

            chunk_size = max(65536, self.buffer_size_kb * 1024)
            last_speed_check = time.time()
            bytes_since_last_check = 0
            is_first_chunk = True

            async with aiofiles.open(part_filepath, write_mode) as f:
                async for chunk in resp.aiter_content(chunk_size):
                    if task.status != TaskStatus.DOWNLOADING:
                        break

                    if chunk:
                        if is_first_chunk and write_mode == "wb":
                            is_first_chunk = False
                            chunk_head = chunk[:20].lower()
                            if chunk_head.startswith(b"<!doctype html") or chunk_head.startswith(b"<html"):
                                raise ValueError("Server returned an HTML page instead of binary file. Direct link may have expired.")

                        await f.write(chunk)
                        chunk_len = len(chunk)
                        task.downloaded_bytes += chunk_len
                        bytes_since_last_check += chunk_len

                        # Enforce global speed limit if configured
                        if self.speed_limit_mbps > 0:
                            await self._token_limiter.consume(chunk_len)

                        if interface_ip:
                            self.net_manager.record_chunk_transfer(interface_ip, chunk_len)

                        now = time.time()
                        delta = now - last_speed_check
                        if delta >= 0.5:
                            speed = bytes_since_last_check / delta
                            task.speed_bytes_sec = speed
                            bytes_since_last_check = 0
                            last_speed_check = now

                            remaining_bytes = max(0, task.file_size - task.downloaded_bytes)
                            if speed > 0 and remaining_bytes > 0:
                                task.eta_seconds = int(remaining_bytes / speed)
                            else:
                                task.eta_seconds = None

            if task.status == TaskStatus.DOWNLOADING:
                if task.file_size > 0 and task.downloaded_bytes < task.file_size:
                    raise IOError(
                        f"Stream truncated prematurely ({task.downloaded_bytes}/{task.file_size} bytes downloaded)."
                    )
                elif task.file_size == 0 and task.downloaded_bytes < 1024:
                    raise IOError(
                        f"Suspiciously small download ({task.downloaded_bytes} bytes). Possible broken link."
                    )

                if os.path.exists(final_filepath):
                    os.remove(final_filepath)
                if os.path.exists(part_filepath):
                    os.rename(part_filepath, final_filepath)

                task.status = TaskStatus.COMPLETED
                task.speed_bytes_sec = 0.0
                task.eta_seconds = 0
                if self.on_complete_callback:
                    self.on_complete_callback(task)
        except Exception:
            self._evict_session(interface_ip)
            raise
