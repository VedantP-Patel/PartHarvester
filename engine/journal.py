"""Crash-safe atomic byte-interval journal with NTFS sparse-range recovery."""
import asyncio
import ctypes
import json
import os
import sys
import threading
import time
from typing import List, Tuple

if sys.platform == "win32":
    import msvcrt

FSCTL_QUERY_ALLOCATED_RANGES = 0x000940CF
ERROR_MORE_DATA = 234


class _FileAllocatedRangeBuffer(ctypes.Structure):
    _fields_ = [
        ("FileOffset", ctypes.c_int64),
        ("Length", ctypes.c_int64),
    ]


class IntervalJournal:
    """
    Maintains an exact, coalesced set of downloaded byte intervals [start, end] (inclusive)
    persisted atomically to <part_filepath>.meta.
    """

    def __init__(self, part_filepath: str, file_size: int):
        self.part_filepath = part_filepath
        self.meta_path = f"{part_filepath}.meta"
        self.file_size = max(0, int(file_size))
        self.intervals: List[List[int]] = []
        self._lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._last_flush_ts = 0.0
        self._dirty = False
        self._load_or_bootstrap()

    def _query_ntfs_allocated_ranges(self) -> List[Tuple[int, int]]:
        """Queries NTFS for physically allocated byte ranges inside a sparse .part file."""
        if sys.platform != "win32" or self.file_size <= 0 or not os.path.exists(self.part_filepath):
            return []
        ranges: List[Tuple[int, int]] = []
        try:
            with open(self.part_filepath, "rb") as f:
                handle = msvcrt.get_osfhandle(f.fileno())
                query = _FileAllocatedRangeBuffer(0, self.file_size)
                out_buf = (_FileAllocatedRangeBuffer * 512)()
                bytes_returned = ctypes.c_ulong(0)

                while True:
                    ok = ctypes.windll.kernel32.DeviceIoControl(
                        handle,
                        FSCTL_QUERY_ALLOCATED_RANGES,
                        ctypes.byref(query),
                        ctypes.sizeof(query),
                        ctypes.byref(out_buf),
                        ctypes.sizeof(out_buf),
                        ctypes.byref(bytes_returned),
                        None,
                    )
                    err = ctypes.get_last_error() if not ok else 0
                    count = bytes_returned.value // ctypes.sizeof(_FileAllocatedRangeBuffer)
                    for i in range(count):
                        off = out_buf[i].FileOffset
                        length = out_buf[i].Length
                        if length <= 0:
                            continue
                        end = min(self.file_size - 1, off + length - 1)
                        # Ignore tiny trailing sentinel cluster (<= 64 KB) created by _preallocate_file
                        if end == self.file_size - 1 and length <= 65536 and off > 0:
                            continue
                        ranges.append((off, end))

                    if ok or err != ERROR_MORE_DATA or count == 0:
                        break
                    last_end = out_buf[count - 1].FileOffset + out_buf[count - 1].Length
                    if last_end >= self.file_size:
                        break
                    query = _FileAllocatedRangeBuffer(last_end, self.file_size - last_end)
        except Exception:
            return []
        return ranges

    def _load_or_bootstrap(self) -> None:
        # Clean up any orphaned .tmp files left from a prior hard power loss
        parent = os.path.dirname(self.meta_path)
        base = os.path.basename(self.meta_path)
        if parent and os.path.isdir(parent):
            try:
                for entry in os.listdir(parent):
                    if entry.startswith(f"{base}.") and entry.endswith(".tmp"):
                        os.remove(os.path.join(parent, entry))
            except Exception:
                pass

        if os.path.exists(self.meta_path):
            try:
                with open(self.meta_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                saved_size = int(data.get("file_size", 0))
                if self.file_size > 0 and saved_size == self.file_size:
                    for s, e in data.get("intervals", []):
                        self._insert_locked(int(s), int(e))
                    return
            except Exception:
                pass

        if self.file_size > 0 and os.path.exists(self.part_filepath):
            try:
                disk_size = os.path.getsize(self.part_filepath)
                if 0 < disk_size < self.file_size:
                    self._insert_locked(0, disk_size - 1)
                    self.save_sync(force=True)
                elif disk_size == self.file_size:
                    recovered = self._query_ntfs_allocated_ranges()
                    for s, e in recovered:
                        self._insert_locked(s, e)
                    if self.intervals:
                        self.save_sync(force=True)
            except Exception:
                pass

    def _insert_locked(self, start: int, end: int) -> None:
        if self.file_size <= 0:
            return
        s = max(0, int(start))
        e = min(self.file_size - 1, int(end))
        if s > e:
            return

        merged: List[List[int]] = []
        inserted = False
        for cur_s, cur_e in self.intervals:
            if cur_e + 1 < s:
                merged.append([cur_s, cur_e])
            elif e + 1 < cur_s:
                if not inserted:
                    merged.append([s, e])
                    inserted = True
                merged.append([cur_s, cur_e])
            else:
                s = min(s, cur_s)
                e = max(e, cur_e)

        if not inserted:
            merged.append([s, e])
        self.intervals = merged
        self._dirty = True

    def add_range(self, start: int, end: int, flush_interval: float = 1.5) -> None:
        should_flush = False
        with self._lock:
            self._insert_locked(start, end)
            now = time.monotonic()
            if self._dirty and (now - self._last_flush_ts) >= flush_interval:
                self._last_flush_ts = now
                should_flush = True
        if should_flush and self._io_lock.acquire(blocking=False):
            try:
                self._write_snapshot_unlocked()
            finally:
                self._io_lock.release()

    def _write_snapshot_unlocked(self) -> None:
        """Caller must hold self._io_lock."""
        with self._lock:
            snapshot = [list(iv) for iv in self.intervals]
            self._dirty = False
            self._last_flush_ts = time.monotonic()
        try:
            os.makedirs(os.path.dirname(self.meta_path), exist_ok=True)
            tmp_path = f"{self.meta_path}.{os.getpid()}.{threading.get_ident()}.tmp"
            payload = {
                "file_size": self.file_size,
                "intervals": snapshot,
                "updated_at": time.time(),
            }
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, separators=(",", ":"))
                f.flush()
            os.replace(tmp_path, self.meta_path)
        except Exception:
            pass

    def total_downloaded(self) -> int:
        with self._lock:
            return sum(e - s + 1 for s, e in self.intervals)

    def is_complete(self) -> bool:
        if self.file_size <= 0:
            return False
        with self._lock:
            return (
                len(self.intervals) == 1
                and self.intervals[0][0] == 0
                and self.intervals[0][1] == self.file_size - 1
            )

    def missing_ranges(self) -> List[Tuple[int, int]]:
        if self.file_size <= 0:
            return []
        with self._lock:
            holes: List[Tuple[int, int]] = []
            cursor = 0
            for s, e in self.intervals:
                if cursor < s:
                    holes.append((cursor, s - 1))
                cursor = max(cursor, e + 1)
            if cursor < self.file_size:
                holes.append((cursor, self.file_size - 1))
            return holes

    def plan_hole_slices(self, max_slices: int) -> List[Tuple[int, int]]:
        holes = self.missing_ranges()
        if not holes:
            return []

        max_slices = max(1, int(max_slices))
        total_missing = sum(e - s + 1 for s, e in holes)
        target_chunk = max(256 * 1024, (total_missing + max_slices - 1) // max_slices)

        slices: List[Tuple[int, int]] = []
        for h_start, h_end in holes:
            cur = h_start
            while cur <= h_end:
                if len(slices) >= max_slices - 1:
                    slices.append((cur, h_end))
                    break
                nxt_end = min(h_end, cur + target_chunk - 1)
                if h_end - nxt_end < 256 * 1024:
                    nxt_end = h_end
                slices.append((cur, nxt_end))
                cur = nxt_end + 1

        return slices

    def save_sync(self, force: bool = True) -> None:
        with self._lock:
            if not force and not self._dirty:
                return
            snapshot = [list(iv) for iv in self.intervals]
            self._dirty = False
            self._last_flush_ts = time.monotonic()

        with self._io_lock:
            try:
                os.makedirs(os.path.dirname(self.meta_path), exist_ok=True)
                tmp_path = f"{self.meta_path}.{os.getpid()}.{threading.get_ident()}.tmp"
                payload = {
                    "file_size": self.file_size,
                    "intervals": snapshot,
                    "updated_at": time.time(),
                }
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, separators=(",", ":"))
                    f.flush()
                os.replace(tmp_path, self.meta_path)
            except Exception:
                pass

    async def save_async(self, force: bool = True) -> None:
        await asyncio.to_thread(self.save_sync, force)

    def cleanup(self) -> None:
        with self._io_lock:
            try:
                if os.path.exists(self.meta_path):
                    os.remove(self.meta_path)
            except Exception:
                pass