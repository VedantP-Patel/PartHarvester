"""Session and State Persistence Manager.

Saves download queues, task progress, and user settings to a JSON file
with automatic .bak fallback recovery across application restarts.
"""

import json
import os
import shutil
import threading
from typing import Any, Dict, List
from engine.task import DownloadTask


class SessionManager:
    """Manages persistent session state for PartHarvester."""

    def __init__(self, filepath: str = "download_session.json"):
        self.filepath = filepath
        self.backup_filepath = f"{filepath}.bak"
        self._save_lock = threading.Lock()

    def _default_state(self) -> Dict[str, Any]:
        return {
            "settings": {
                "download_dir": os.path.join(
                    os.path.expanduser("~"), "Downloads", "PartHarvester"
                ),
                "max_concurrency": 4,
                "auto_extract": True,
                "auto_resume_on_start": True,
                "rd_token": "",
            },
            "tasks": [],
        }

    def load_session(self) -> Dict[str, Any]:
        """Loads saved session settings and tasks from disk with .bak recovery."""
        for candidate in (self.filepath, self.backup_filepath):
            if not os.path.exists(candidate):
                continue
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and "tasks" in data:
                    return data
            except Exception:
                continue

        return self._default_state()

    def save_session(self, settings: Dict[str, Any], tasks: List[DownloadTask]) -> None:
        """Atomically saves current settings and compact task state to disk."""
        try:
            serialized_tasks = []
            for task in tasks:
                d = task.model_dump()
                # Segments are reconstructed from .part.meta journals on boot
                d["segments"] = []
                d["speed_bytes_sec"] = 0.0
                d["eta_seconds"] = None
                serialized_tasks.append(d)

            data = {
                "settings": settings,
                "tasks": serialized_tasks,
            }
            tmp_path = f"{self.filepath}.tmp"
            with self._save_lock:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, separators=(",", ":"))
                if os.path.exists(self.filepath):
                    try:
                        shutil.copy2(self.filepath, self.backup_filepath)
                    except Exception:
                        pass
                os.replace(tmp_path, self.filepath)
        except Exception:
            pass