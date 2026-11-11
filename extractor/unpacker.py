"""Multi-Part Archive Detector and Automated 7-Zip Extractor.

Features:
- Identifies split archives (.part01.rar, .r00, .001, fg-01.bin).
- Validates sequence completeness (verifies part01 through partN all exist).
- Invokes multithreaded 7-Zip (7z.exe -mmt=on) in the background to unpack game folders cleanly.
"""

import asyncio
import os
import re
import shutil
from typing import Any, Dict, List, Optional, Tuple
from engine.task import DownloadTask, TaskStatus


class ArchiveExtractor:
    """Manages archive sequence validation and 7-Zip extraction."""

    # Regex patterns for multi-part archives
    PATTERNS = [
        # Example: Phantom_Blade_Zero_-_fitgirl-repacks.site_-_part01.rar
        re.compile(r"^(?P<prefix>.+?)[_\.-]part(?P<idx>\d+)\.(?P<ext>rar|exe)$", re.IGNORECASE),
        # Example: game.part1.rar
        re.compile(r"^(?P<prefix>.+?)\.part(?P<idx>\d+)\.(?P<ext>rar)$", re.IGNORECASE),
        # Example: game.7z.001
        re.compile(r"^(?P<prefix>.+?)\.(?:7z|rar)\.(?P<idx>\d{3})$", re.IGNORECASE),
        # Example: game.r00, game.r01
        re.compile(r"^(?P<prefix>.+?)\.r(?P<idx>\d{2})$", re.IGNORECASE),
    ]

    def __init__(self, seven_zip_path: Optional[str] = None):
        self.seven_zip_path = seven_zip_path or self._find_7zip()
        self.extraction_jobs: Dict[str, dict] = {}

    def _find_7zip(self) -> Optional[str]:
        """Locates 7-Zip executable on Windows."""
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
        """Matches a filename against multi-part patterns.

        Returns:
            (prefix, part_index, extension) or None.
        """
        for pattern in cls.PATTERNS:
            m = pattern.match(filename)
            if m:
                prefix = m.group("prefix")
                idx = int(m.group("idx"))
                ext = m.group("ext") if "ext" in m.groupdict() else "rar"
                return prefix, idx, ext
        return None

    def group_tasks_by_sequence(
        self, tasks: List[DownloadTask]
    ) -> Dict[str, Dict[str, Any]]:
        """Groups tasks into multi-part sequences and checks completeness."""
        groups: Dict[str, Dict[str, Any]] = {}

        for task in tasks:
            match = self.match_part_sequence(task.filename)
            if match:
                prefix, idx, ext = match
                if prefix not in groups:
                    groups[prefix] = {
                        "prefix": prefix,
                        "extension": ext,
                        "destination_dir": task.destination_dir,
                        "parts": {},
                        "is_complete": False,
                        "total_parts": 0,
                        "first_part": None,
                    }

                groups[prefix]["parts"][idx] = task

        # Check completeness for each group
        for prefix, data in groups.items():
            parts = data["parts"]
            if not parts:
                continue

            indices = sorted(parts.keys())
            max_idx = max(indices)
            data["total_parts"] = max_idx

            # Verify all parts 1 to max_idx exist and are COMPLETED
            all_present = len(indices) == max_idx and min(indices) in (0, 1)
            all_completed = all_present and all(
                parts[i].status == TaskStatus.COMPLETED for i in indices
            )

            data["is_complete"] = all_completed
            # Find the primary first part file (part01 or part1)
            first_idx = min(indices)
            data["first_part"] = parts[first_idx].filename

        return groups

    async def extract_archive(
        self,
        archive_path: str,
        output_dir: str,
        group_id: str,
    ) -> bool:
        """Executes multithreaded 7-Zip extraction in background."""
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

        # Run 7z.exe x -y -mmt=on -o<output_dir> <archive_path>
        cmd = [
            self.seven_zip_path,
            "x",
            "-y",
            "-mmt=on",
            f"-o{output_dir}",
            archive_path,
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                self.extraction_jobs[group_id]["status"] = "SUCCESS"
                return True
            else:
                err_text = stderr.decode("utf-8", errors="ignore") or stdout.decode("utf-8", errors="ignore")
                self.extraction_jobs[group_id]["status"] = "FAILED"
                self.extraction_jobs[group_id]["error"] = err_text[-500:]
                return False
        except Exception as exc:
            self.extraction_jobs[group_id]["status"] = "FAILED"
            self.extraction_jobs[group_id]["error"] = str(exc)
            return False
