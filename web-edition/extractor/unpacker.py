"""Multi-Part Archive Detector and Automated 7-Zip Extractor."""
import asyncio
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple
from engine.task import DownloadTask, TaskStatus


class ArchiveExtractor:
    """Manages archive sequence validation and 7-Zip extraction."""

    PATTERNS = [
        # Example: Phantom_Blade_Zero_-_fitgirl-repacks.site_-_part01.rar or game.part1.rar
        re.compile(r"^(?P<prefix>.+?)[_\.-]part(?P<idx>\d+)\.(?P<ext>rar|exe)$", re.IGNORECASE),
        # Example: game.7z.001 or game.rar.001
        re.compile(r"^(?P<prefix>.+?)\.(?P<ext>7z|rar)\.(?P<idx>\d{3})$", re.IGNORECASE),
        # Example: game.r00, game.r01
        re.compile(r"^(?P<prefix>.+?)\.r(?P<idx>\d{2})$", re.IGNORECASE),
        # Example: fg-01.bin, fg-02.bin
        re.compile(r"^(?P<prefix>fg)[_\.-](?P<idx>\d+)\.(?P<ext>bin)$", re.IGNORECASE),
    ]

    def __init__(self, seven_zip_path: Optional[str] = None):
        self.seven_zip_path = seven_zip_path or self._find_7zip()
        self.extraction_jobs: Dict[str, dict] = {}

    def _find_7zip(self) -> Optional[str]:
        standard_paths = [
            r"C:\Program Files\7-Zip\7z.exe",
            r"C:\Program Files (x86)\7-Zip\7z.exe",
            shutil.which("7z"),
            shutil.which("7za"),
        ]
        for path in standard_paths:
            if path and os.path.exists(path):
                return path
        return None

    @classmethod
    def match_part_sequence(cls, filename: str) -> Optional[Tuple[str, int, str]]:
        for pattern in cls.PATTERNS:
            m = pattern.match(filename)
            if m:
                prefix = m.group("prefix")
                idx = int(m.group("idx"))
                ext = m.groupdict().get("ext") or "rar"
                return prefix, idx, ext.lower()
        return None

    def group_tasks_by_sequence(self, tasks: List[DownloadTask]) -> Dict[str, Dict[str, Any]]:
        groups: Dict[str, Dict[str, Any]] = {}
        for task in tasks:
            match = self.match_part_sequence(task.filename)
            if match:
                prefix, idx, ext = match
                norm_dir = os.path.normpath(task.destination_dir)
                group_key = f"{norm_dir}::{prefix}"
                if group_key not in groups:
                    groups[group_key] = {
                        "prefix": prefix,
                        "extension": ext,
                        "destination_dir": task.destination_dir,
                        "parts": {},
                        "is_complete": False,
                        "total_parts": 0,
                        "first_part": None,
                    }
                groups[group_key]["parts"][idx] = task

        for group_key, data in groups.items():
            parts = data["parts"]
            if not parts:
                continue
            indices = sorted(parts.keys())
            min_idx = indices[0]
            max_idx = indices[-1]
            expected_count = max_idx - min_idx + 1
            data["total_parts"] = expected_count

            all_present = min_idx in (0, 1) and len(indices) == expected_count
            all_completed = all_present and all(
                parts[i].status == TaskStatus.COMPLETED for i in indices
            )
            data["is_complete"] = all_completed
            data["first_part"] = parts[min_idx].filename

        return groups

    async def extract_archive(
        self,
        archive_path: str,
        output_dir: str,
        group_id: str,
        part_files: Optional[List[str]] = None,
        delete_after_extract: bool = False,
    ) -> bool:
        """Executes multithreaded 7-Zip extraction in background and optionally cleans up parts."""
        if not self.seven_zip_path:
            raise FileNotFoundError("7-Zip (7z.exe) not found on system.")
        if not os.path.exists(archive_path):
            raise FileNotFoundError(f"Archive first part does not exist: {archive_path}")

        os.makedirs(output_dir, exist_ok=True)
        self.extraction_jobs[group_id] = {
            "status": "EXTRACTING",
            "archive_path": archive_path,
            "output_dir": output_dir,
            "error": None,
        }

        cmd = [
            self.seven_zip_path,
            "x",
            "-y",
            "-p-",
            "-mmt=on",
            f"-o{output_dir}",
            archive_path,
        ]

        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            if proc.returncode == 0:
                self.extraction_jobs[group_id]["status"] = "SUCCESS"
                if delete_after_extract and part_files:
                    for pf in part_files:
                        try:
                            if os.path.exists(pf):
                                await asyncio.to_thread(os.remove, pf)
                        except Exception:
                            pass
                return True
            else:
                err_text = proc.stderr.decode("utf-8", errors="ignore") or proc.stdout.decode("utf-8", errors="ignore")
                self.extraction_jobs[group_id]["status"] = "FAILED"
                self.extraction_jobs[group_id]["error"] = err_text[-500:]
                return False
        except Exception as exc:
            self.extraction_jobs[group_id]["status"] = "FAILED"
            self.extraction_jobs[group_id]["error"] = str(exc)
            return False