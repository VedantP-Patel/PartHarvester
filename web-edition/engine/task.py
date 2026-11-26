"""Download Task Data Models.

Defines Pydantic models for individual download items, status state machine,
and telemetry metrics.
"""

from enum import Enum
import uuid
from typing import List, Optional
from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """Download task lifecycle statuses."""
    QUEUED = "QUEUED"
    RESOLVING = "RESOLVING"
    DOWNLOADING = "DOWNLOADING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Segment(BaseModel):
    """Represents an individual byte-range chunk stream within a multi-segment download."""
    index: int
    start_byte: int
    end_byte: int
    downloaded_bytes: int = 0
    status: str = "QUEUED"  # QUEUED, DOWNLOADING, COMPLETED, FAILED
    assigned_adapter: Optional[str] = None
    speed_bytes_sec: float = 0.0

    @property
    def total_bytes(self) -> int:
        return max(0, self.end_byte - self.start_byte + 1)

    @property
    def progress_pct(self) -> float:
        tot = self.total_bytes
        if tot <= 0:
            return 100.0 if self.status == "COMPLETED" else 0.0
        return min(100.0, round((self.downloaded_bytes / tot) * 100.0, 1))


class DownloadTask(BaseModel):
    """Represents a single file part download item."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    original_url: str
    direct_url: Optional[str] = None
    filename: str = "download.bin"
    file_size: int = 0
    downloaded_bytes: int = 0
    status: TaskStatus = TaskStatus.QUEUED
    assigned_adapter: Optional[str] = None  # e.g. "Wi-Fi (10.225.0.236)" or "USB Tethering"
    speed_bytes_sec: float = 0.0
    eta_seconds: Optional[int] = None
    error_message: Optional[str] = None
    retries: int = 0
    destination_dir: str = ""
    segments: List[Segment] = []
    supports_range: bool = True
    priority: int = 0

    @property
    def progress_pct(self) -> float:
        """Calculates download percentage (0.0 - 100.0)."""
        if self.file_size <= 0:
            return 100.0 if self.status == TaskStatus.COMPLETED else 0.0
        return min(100.0, round((self.downloaded_bytes / self.file_size) * 100.0, 1))

    @property
    def is_finished(self) -> bool:
        """Returns True if the task reached a terminal state."""
        return self.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)

