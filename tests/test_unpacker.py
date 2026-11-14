"""Unit Tests for Archive Sequence Detector and Unpacker."""
import pytest
import os
from engine.task import DownloadTask, TaskStatus
from extractor.unpacker import ArchiveExtractor


def test_match_part_sequence_fitgirl():
    filename = "Phantom_Blade_Zero_-_fitgirl-repacks.site_-_part01.rar"
    match = ArchiveExtractor.match_part_sequence(filename)
    assert match is not None
    prefix, idx, ext = match
    assert "Phantom_Blade_Zero" in prefix
    assert idx == 1
    assert ext.lower() == "rar"


def test_match_part_sequence_generic():
    m1 = ArchiveExtractor.match_part_sequence("game.part05.rar")
    assert m1 == ("game", 5, "rar")

    m2 = ArchiveExtractor.match_part_sequence("repack.7z.003")
    assert m2 is not None
    assert m2[0] == "repack" and m2[1] == 3

    m3 = ArchiveExtractor.match_part_sequence("fg-01.bin")
    assert m3 is not None
    assert m3[0] == "fg" and m3[1] == 1


def test_group_tasks_completeness():
    extractor = ArchiveExtractor()

    tasks = [
        DownloadTask(
            original_url="http://example.com/1",
            filename="game_part01.rar",
            status=TaskStatus.COMPLETED,
            destination_dir="./test",
        ),
        DownloadTask(
            original_url="http://example.com/2",
            filename="game_part02.rar",
            status=TaskStatus.DOWNLOADING,
            destination_dir="./test",
        ),
        DownloadTask(
            original_url="http://example.com/3",
            filename="game_part03.rar",
            status=TaskStatus.QUEUED,
            destination_dir="./test",
        ),
    ]

    key = f"{os.path.normpath('./test')}::game"
    groups = extractor.group_tasks_by_sequence(tasks)
    assert key in groups
    assert groups[key]["prefix"] == "game"
    assert groups[key]["is_complete"] is False

    tasks[1].status = TaskStatus.COMPLETED
    tasks[2].status = TaskStatus.COMPLETED

    groups_after = extractor.group_tasks_by_sequence(tasks)
    assert groups_after[key]["is_complete"] is True
    assert groups_after[key]["first_part"] == "game_part01.rar"


def test_group_tasks_zero_indexed_r00_sequence():
    """Verifies that 0-indexed sequences (.r00, .r01, .r02) evaluate completeness accurately."""
    extractor = ArchiveExtractor()
    tasks = [
        DownloadTask(
            original_url=f"http://example.com/r0{i}",
            filename=f"archive.r0{i}",
            status=TaskStatus.COMPLETED,
            destination_dir="./test",
        )
        for i in range(3)  # .r00, .r01, .r02 -> indices 0, 1, 2
    ]
    groups = extractor.group_tasks_by_sequence(tasks)
    key = f"{os.path.normpath('./test')}::archive"
    assert key in groups
    assert groups[key]["total_parts"] == 3
    assert groups[key]["is_complete"] is True
    assert groups[key]["first_part"] == "archive.r00"