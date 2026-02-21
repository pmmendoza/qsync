from __future__ import annotations

import gzip
import os
from datetime import datetime, timezone
from pathlib import Path

from qsync import log_rotation


def _write_log(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_should_rotate_by_size(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "qualtrics_push.log"
    _write_log(log_file, ['{"action":"a"}'])

    should, reason = log_rotation.should_rotate(log_file, max_bytes=1)
    assert should is True
    assert reason == "size"


def test_should_rotate_by_month(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "qualtrics_push.log"
    _write_log(log_file, ['{"action":"a"}'])

    old = datetime(2026, 1, 31, 12, 0, tzinfo=timezone.utc).timestamp()
    os.utime(log_file, (old, old))

    should, reason = log_rotation.should_rotate(
        log_file,
        now=datetime(2026, 2, 1, 8, 0, tzinfo=timezone.utc),
        max_bytes=10_000,
    )
    assert should is True
    assert reason == "month"


def test_rotate_log_file_writes_archive_and_prunes_old(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "qualtrics_push.log"
    _write_log(log_file, ['{"action":"a"}', '{"action":"b"}'])

    old = datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc).timestamp()
    os.utime(log_file, (old, old))

    archive_dir = log_file.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    very_old_archive = archive_dir / "qualtrics_push.log.2024-01.gz"
    with gzip.open(very_old_archive, "wt", encoding="utf-8") as handle:
        handle.write('{"action":"old"}\n')

    result = log_rotation.rotate_log_file(
        log_file,
        now=datetime(2026, 2, 1, 0, 0, tzinfo=timezone.utc),
        retention_months=1,
    )
    assert result.rotated is True
    assert result.archive_path is not None
    assert result.archive_path.exists()
    assert result.archive_path.suffix == ".gz"
    assert not log_file.exists()
    assert very_old_archive in result.deleted_archives
    assert not very_old_archive.exists()


def test_list_archives_sorted_newest_first(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "qualtrics_push.log"
    archive_dir = log_file.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "qualtrics_push.log.2026-01.gz",
        "qualtrics_push.log.2026-03.gz",
        "qualtrics_push.log.2026-02.1.gz",
    ):
        with gzip.open(archive_dir / name, "wt", encoding="utf-8") as handle:
            handle.write('{"action":"x"}\n')

    archives = log_rotation.list_archives(log_file)
    assert [path.name for path in archives] == [
        "qualtrics_push.log.2026-03.gz",
        "qualtrics_push.log.2026-02.1.gz",
        "qualtrics_push.log.2026-01.gz",
    ]
