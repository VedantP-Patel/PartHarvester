"""High-Performance Async Download Engine with Adaptive Scheduling & Work-Stealing."""
import asyncio
import inspect
import os
import re
import subprocess
import time
import urllib.parse
from typing import Callable, Dict, List, Optional
import aiofiles
from curl_cffi import requests

from engine.network import NetworkAdapter, NetworkManager
from engine.session import SessionManager
from engine.task import DownloadTask, Segment, TaskStatus
from resolvers.manager import LinkManager


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
            # Allow burst up to 0.25s of bandwidth
            min_cursor = now - 0.25
            if self._next_free_time < min_cursor:
                self._next_free_time = min_cursor
            self._next_free_time += amount / self.rate_bytes_sec
            wait_time = self._next_free_time - now

        # Sleep OUTSIDE the lock so concurrent workers do not serialize
        if wait_time > 0.005:
            await asyncio.sleep(min(wait_time, 2.0))


class AsyncDownloader:
    """Core download manager handling tasks, adaptive workers, and work-stealing."""

    _NATURAL_RE = re.compile(r"(\d+)")

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
        self._pinned_handoff_ip: Dict[str, str] = {}
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
        self.work_stealing_enabled: bool = True
        self.delete_archive_after_extract: bool = False

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

    async def _get_session(self, adapter_ip: Optional[str]) -> requests.AsyncSession:
        session = self._session_pool.get(adapter_ip)
        if session is None or getattr(session, "_closed", False):
            s_kwargs = {"impersonate": "chrome", "max_clients": 32}
            if adapter_ip:
                s_kwargs["interface"] = adapter_ip
            session = requests.AsyncSession(**s_kwargs)
            self._session_pool[adapter_ip] = session
        return session

    def _evict_session(self, adapter_ip: Optional[str]) -> None:
        s = self._session_pool.pop(adapter_ip, None)
        if s and not getattr(s, "_closed", False):
            try:
                asyncio.create_task(s.close())
            except Exception:
                pass

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
        cand_name = self._extract_candidate_filename(original_url)
        task = DownloadTask(
            original_url=original_url,
            destination_dir=destination_dir,
            filename=cand_name,
        )
        self.tasks[task.id] = task
        if sort:
            self.sort_tasks("natural_part")
        self.trigger_queue_processing()
        return task

    def add_tasks_batch(self, urls: List[str], destination_dir: str) -> List[DownloadTask]:
        """Batch-inserts multiple URLs and sorts only once in O(N log N) time."""
        created = [self.add_task(u, destination_dir, sort=False) for u in urls]
        if created:
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
            del self.tasks[tid]
        return len(to_remove)

    def clear_all(self) -> int:
        self.pause_all()
        count = len(self.tasks)
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
        self._pinned_handoff_ip.pop(task_id, None)
        self._task_assigned_ip.pop(task_id, None)
        if task_id in self.tasks:
            del self.tasks[task_id]
            return True
        return False

    async def handoff_task_to_adapter(self, task_id: str, target_ip: str) -> bool:
        """Hands off an active download to a specific network adapter and pins it."""
        task = self.tasks.get(task_id)
        if not task or task.status != TaskStatus.DOWNLOADING:
            return False

        target_adapter = self.net_manager.get_adapter_by_ip(target_ip)
        if not target_adapter or not target_adapter.is_enabled or not target_adapter.is_up:
            return False

        current_ip = self._task_assigned_ip.get(task_id)
        if current_ip == target_ip:
            return False

        event = {
            "timestamp": time.time(),
            "task_id": task_id,
            "filename": task.filename,
            "from_adapter": task.assigned_adapter or "Unknown",
            "to_adapter": f"{target_adapter.friendly_type} ({target_ip})",
        }
        self.handoff_events.append(event)
        if len(self.handoff_events) > 10:
            self.handoff_events.pop(0)

        # Pin target adapter so _process_queue_loop dispatches onto target_ip
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

        task.status = TaskStatus.QUEUED
        self.trigger_queue_processing()
        return True

    async def start(self) -> None:
        self._is_running = True
        self._worker_loop_task = asyncio.create_task(self._process_queue_loop())
        self._straggler_loop_task = asyncio.create_task(self._straggler_watchdog_loop())

    async def stop(self) -> None:
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
        last_pre_resolve_time = 0.0
        while self._is_running:
            try:
                queued_tasks = [t for t in self.tasks.values() if t.status == TaskStatus.QUEUED]
                now = time.monotonic()
                if queued_tasks and (now - last_pre_resolve_time >= 2.0):
                    last_pre_resolve_time = now
                    upcoming_urls = [t.original_url for t in queued_tasks[:5]]
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

                        # Check if task has a pinned handoff target adapter
                        pinned_ip = self._pinned_handoff_ip.pop(task.id, None)
                        chosen_adapter = (
                            self.net_manager.get_adapter_by_ip(pinned_ip) if pinned_ip else None
                        )
                        if not chosen_adapter or not chosen_adapter.is_enabled or not chosen_adapter.is_up:
                            chosen_adapter = self.net_manager.get_best_available_adapter(self.max_concurrency)

                        if not chosen_adapter:
                            break

                        task.status = TaskStatus.RESOLVING
                        active_count += 1
                        t = asyncio.create_task(self._execute_task_pipeline(task, chosen_adapter))
                        self._active_async_tasks[task.id] = t

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
        while self._is_running:
            try:
                await asyncio.sleep(2.5)
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
                    active_on_slow = [
                        t for t in self.tasks.values()
                        if t.status == TaskStatus.DOWNLOADING
                        and self._task_assigned_ip.get(t.id) == slow_adapter.ip_address
                    ]
                    for task in active_on_slow:
                        remaining = task.file_size - task.downloaded_bytes
                        if remaining > 50 * 1024 * 1024 and (
                            fast_speed > 2.0 * slow_speed or slow_speed < 1024 * 1024
                        ):
                            await self.handoff_task_to_adapter(task.id, fast_adapter.ip_address)
                            break
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(2.0)

    async def _execute_task_pipeline(self, task: DownloadTask, adapter: NetworkAdapter) -> None:
        interface_ip = adapter.ip_address
        task.assigned_adapter = f"{adapter.friendly_type} ({interface_ip})"
        self._task_assigned_ip[task.id] = interface_ip

        task.status = TaskStatus.RESOLVING
        max_resolve_retries = max(1, self.max_retries)
        resolve_success = False

        for r_attempt in range(max_resolve_retries):
            if task.status != TaskStatus.RESOLVING:
                return
            try:
                direct_url, filename, expected_size = await self.link_manager.resolve_link(
                    task.original_url, interface=interface_ip, use_cache=(r_attempt == 0)
                )
                task.direct_url = direct_url
                if filename and (task.filename == "download.bin" or not task.filename):
                    task.filename = filename
                if expected_size > 0:
                    task.file_size = expected_size
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
                if r_attempt < max_resolve_retries - 1:
                    task.error_message = f"Resolving retry ({r_attempt + 1}/{max_resolve_retries}): {err_str}"
                    await asyncio.sleep(min(4.0, 1.5 * (r_attempt + 1)))
                else:
                    task.status = TaskStatus.FAILED
                    task.error_message = f"Resolve error: {err_str}"
                    await asyncio.sleep(1.5)
                    return

        if not resolve_success or not task.direct_url:
            return

        task.status = TaskStatus.DOWNLOADING
        self.net_manager.increment_connections(interface_ip)
        try:
            await self._download_stream(task, interface_ip)
        except asyncio.CancelledError:
            if task.status not in (TaskStatus.QUEUED, TaskStatus.PAUSED):
                task.status = TaskStatus.PAUSED
            task.speed_bytes_sec = 0.0
        except Exception as exc:
            err_str = str(exc)
            task.speed_bytes_sec = 0.0
            if "HTML page" in err_str:
                self.link_manager.invalidate_cache(task.original_url)
            if "404" in err_str or "Dead link" in err_str:
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
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        if not os.path.exists(filepath) or os.path.getsize(filepath) < size:
            mode = "r+b" if os.path.exists(filepath) else "wb"
            with open(filepath, mode) as f:
                f.seek(size - 1)
                f.write(b"\0")

    async def _download_stream(self, task: DownloadTask, interface_ip: Optional[str]) -> None:
        os.makedirs(task.destination_dir, exist_ok=True)

        if task.file_size <= 0:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                "Referer": "https://fuckingfast.co/",
                "Range": "bytes=0-0",
            }
            try:
                session = await self._get_session(interface_ip)
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

        await self._download_single_stream(task, interface_ip)

    async def _download_segmented_stream(self, task: DownloadTask, primary_ip: Optional[str]) -> None:
        part_filepath = os.path.join(task.destination_dir, f"{task.filename}.part")
        final_filepath = os.path.join(task.destination_dir, task.filename)

        await asyncio.to_thread(self._preallocate_file, part_filepath, task.file_size)

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

        task.downloaded_bytes = sum(s.downloaded_bytes for s in task.segments)

        # If task was explicitly handed off to primary_ip, prefer primary_ip; otherwise use bonding strategy
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
            # Track extra segment connections on secondary adapters
            counted_extra_conn = adapter_ip and adapter_ip != primary_ip
            if counted_extra_conn:
                self.net_manager.increment_connections(adapter_ip)

            try:
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
                    resp = None
                    write_buf = bytearray()
                    try:
                        session = await self._get_session(adapter_ip)
                        resp = await session.get(task.direct_url, headers=headers, stream=True, timeout=30)
                        if resp.status_code not in (200, 206):
                            raise ValueError(f"Segment {segment.index} failed with HTTP {resp.status_code}")

                        seg_last_check = time.monotonic()
                        seg_bytes_since = 0

                        f = await asyncio.to_thread(open, part_filepath, "r+b")
                        await asyncio.to_thread(f.seek, curr_offset)

                        async for chunk in resp.aiter_content(chunk_buffer):
                            if task.status != TaskStatus.DOWNLOADING:
                                break
                            if chunk:
                                c_len = len(chunk)
                                # Clamp in case server sends past end_byte
                                remaining_in_seg = segment.total_bytes - (segment.downloaded_bytes + len(write_buf))
                                if remaining_in_seg <= 0:
                                    break
                                if c_len > remaining_in_seg:
                                    chunk = chunk[:remaining_in_seg]
                                    c_len = len(chunk)

                                write_buf.extend(chunk)
                                if len(write_buf) >= chunk_buffer:
                                    payload_bytes = bytes(write_buf)
                                    write_buf.clear()
                                    await asyncio.to_thread(f.write, payload_bytes)
                                    segment.downloaded_bytes += len(payload_bytes)
                                    task.downloaded_bytes += len(payload_bytes)

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
                                    task.speed_bytes_sec = sum(s.speed_bytes_sec for s in task.segments)
                                    rem = max(0, task.file_size - task.downloaded_bytes)
                                    task.eta_seconds = (
                                        int(rem / task.speed_bytes_sec)
                                        if task.speed_bytes_sec > 0 and rem > 0
                                        else None
                                    )

                        # Flush any remaining buffered bytes to disk
                        if write_buf and f:
                            payload_bytes = bytes(write_buf)
                            write_buf.clear()
                            await asyncio.to_thread(f.write, payload_bytes)
                            segment.downloaded_bytes += len(payload_bytes)
                            task.downloaded_bytes += len(payload_bytes)

                        if segment.downloaded_bytes >= segment.total_bytes:
                            segment.status = "COMPLETED"
                            segment.speed_bytes_sec = 0.0
                            return
                        elif task.status != TaskStatus.DOWNLOADING:
                            segment.speed_bytes_sec = 0.0
                            return
                        else:
                            raise IOError(f"Segment {segment.index} stream truncated prematurely.")
                    except asyncio.CancelledError:
                        if write_buf and f:
                            payload_bytes = bytes(write_buf)
                            write_buf.clear()
                            await asyncio.to_thread(f.write, payload_bytes)
                            segment.downloaded_bytes += len(payload_bytes)
                            task.downloaded_bytes += len(payload_bytes)
                        segment.speed_bytes_sec = 0.0
                        raise
                    except Exception:
                        segment.speed_bytes_sec = 0.0
                        self._evict_session(adapter_ip)
                        if task.status != TaskStatus.DOWNLOADING:
                            return
                        if attempt == max_seg_retries - 1:
                            segment.status = "FAILED"
                            raise
                        await asyncio.sleep(min(2.0, 0.5 * (2 ** attempt)))
                    finally:
                        if resp is not None:
                            try:
                                await resp.aclose()
                            except Exception:
                                pass
                        if f is not None:
                            await asyncio.to_thread(f.close)
            finally:
                if counted_extra_conn:
                    self.net_manager.decrement_connections(adapter_ip)

        active_pairs = [
            (seg, ad) for seg, ad in zip(task.segments, allocated_adapters) if seg.status != "COMPLETED"
        ]
        await asyncio.gather(*(segment_worker(seg, ad) for seg, ad in active_pairs))

        if task.status == TaskStatus.DOWNLOADING:
            all_done = all(seg.status == "COMPLETED" for seg in task.segments)
            if all_done and task.downloaded_bytes >= task.file_size:
                if os.path.exists(part_filepath):
                    os.replace(part_filepath, final_filepath)
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
        try:
            session = await self._get_session(interface_ip)
            resp = await session.get(task.direct_url, headers=headers, stream=True, timeout=30)

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
                async for chunk in resp.aiter_content(chunk_size):
                    if task.status != TaskStatus.DOWNLOADING:
                        break
                    if chunk:
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

                task.status = TaskStatus.COMPLETED
                task.speed_bytes_sec = 0.0
                task.eta_seconds = 0
                self._fire_complete_callback(task)
        except Exception:
            self._evict_session(interface_ip)
            raise
        finally:
            if resp is not None:
                try:
                    await resp.aclose()
                except Exception:
                    pass