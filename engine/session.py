"""Session and State Persistence Manager.

Saves download queues, task progress, and user settings to a JSON file
so downloading resumes seamlessly across application restarts.
"""

import json
import os
import threading
from typing import Any, Dict, List
from engine.task import DownloadTask


class SessionManager:
    """Manages persistent session state for PartHarvester."""

    def __init__(self, filepath: str = "download_session.json"):
        self.filepath = filepath
        # save_session may be called from worker threads (asyncio.to_thread) concurrently;
        # serialize writers so they never clobber the shared .tmp file.
        self._save_lock = threading.Lock()

    def load_session(self) -> Dict[str, Any]:
        """Loads saved session settings and tasks from disk."""
        if not os.path.exists(self.filepath):
            return {
                "settings": {
                    "download_dir": os.path.join(os.path.expanduser("~"), "Downloads", "PartHarvester"),
                    "max_concurrency": 4,
                    "auto_extract": True,
                    "rd_token": "",
                },
                "tasks": [],
            }

        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data
        except Exception:
            return {"settings": {}, "tasks": []}

    def save_session(self, settings: Dict[str, Any], tasks: List[DownloadTask]) -> None:
        """Atomically saves current settings and tasks to disk."""
        try:
            data = {
                "settings": settings,
                "tasks": [task.model_dump() for task in tasks],
            }
            tmp_path = f"{self.filepath}.tmp"
            with self._save_lock:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp_path, self.filepath)
        except Exception:
            # Non-fatal error saving session
            pass

