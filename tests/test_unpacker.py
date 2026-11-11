"""Unit Tests for Archive Sequence Detector and Unpacker."""

from engine.task import DownloadTask, TaskStatus
from extractor.unpacker import ArchiveExtractor


def test_match_part_sequence_fitgirl():
    # FitGirl repack naming scheme from screenshot:
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
    assert m2 == ("repack", 3, "rar")


def test_group_tasks_completeness():
    extractor = ArchiveExtractor()

    # 3 parts incomplete
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

    groups = extractor.group_tasks_by_sequence(tasks)
    assert "game" in groups
    assert groups["game"]["is_complete"] is False

    # Mark all completed
    tasks[1].status = TaskStatus.COMPLETED
    tasks[2].status = TaskStatus.COMPLETED

    groups_after = extractor.group_tasks_by_sequence(tasks)
    assert groups_after["game"]["is_complete"] is True
    assert groups_after["game"]["first_part"] == "game_part01.rar"

