"""Tests for log_reader module."""

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from qsync import log_reader


def create_test_log(log_path: Path, entries: list[dict]) -> None:
    """Helper to create a test JSONL log file."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def test_read_logs_empty_file(tmp_path: Path) -> None:
    """Test reading an empty log file."""
    log_path = tmp_path / "test.log"
    log_path.touch()

    entries = log_reader.read_logs(log_file=log_path)
    assert entries == []


def test_read_logs_basic(tmp_path: Path) -> None:
    """Test reading basic log entries."""
    log_path = tmp_path / "test.log"
    test_entries = [
        {
            "timestamp": "2026-01-10T12:00:00+00:00",
            "action": "qsync.survey.copy",
            "survey_id": "SV_001",
            "status": 200,
        },
        {
            "timestamp": "2026-01-10T12:01:00+00:00",
            "action": "qsync.survey.delete",
            "survey_id": "SV_002",
            "status": 200,
        },
    ]

    create_test_log(log_path, test_entries)

    entries = log_reader.read_logs(log_file=log_path, reverse=False)
    assert len(entries) == 2
    assert entries[0]["action"] == "qsync.survey.copy"
    assert entries[1]["action"] == "qsync.survey.delete"


def test_read_logs_reverse(tmp_path: Path) -> None:
    """Test reading logs in reverse order (most recent first)."""
    log_path = tmp_path / "test.log"
    test_entries = [
        {"timestamp": "2026-01-10T12:00:00+00:00", "action": "first", "status": 200},
        {"timestamp": "2026-01-10T12:01:00+00:00", "action": "second", "status": 200},
        {"timestamp": "2026-01-10T12:02:00+00:00", "action": "third", "status": 200},
    ]

    create_test_log(log_path, test_entries)

    entries = log_reader.read_logs(log_file=log_path, reverse=True)
    assert entries[0]["action"] == "third"
    assert entries[1]["action"] == "second"
    assert entries[2]["action"] == "first"


def test_read_logs_with_limit(tmp_path: Path) -> None:
    """Test limiting number of log entries returned."""
    log_path = tmp_path / "test.log"
    test_entries = [
        {
            "timestamp": f"2026-01-10T12:0{i}:00+00:00",
            "action": f"action_{i}",
            "status": 200,
        }
        for i in range(10)
    ]

    create_test_log(log_path, test_entries)

    entries = log_reader.read_logs(log_file=log_path, limit=3, reverse=False)
    assert len(entries) == 3


def test_filter_by_survey(tmp_path: Path) -> None:
    """Test filtering by survey ID."""
    log_path = tmp_path / "test.log"
    test_entries = [
        {"action": "test", "survey_id": "SV_001", "status": 200},
        {"action": "test", "survey_id": "SV_002", "status": 200},
        {"action": "test", "survey_id": "SV_001", "status": 200},
    ]

    create_test_log(log_path, test_entries)

    filter_fn = log_reader.filter_by_survey("SV_001")
    entries = log_reader.read_logs(
        log_file=log_path, filter_fn=filter_fn, reverse=False
    )
    assert len(entries) == 2
    assert all(e["survey_id"] == "SV_001" for e in entries)


def test_filter_by_action(tmp_path: Path) -> None:
    """Test filtering by action prefix."""
    log_path = tmp_path / "test.log"
    test_entries = [
        {"action": "qsync.survey.copy", "status": 200},
        {"action": "qsync.survey.delete", "status": 200},
        {"action": "qsync.master.apply", "status": 200},
        {"action": "qsync.survey.rename", "status": 200},
    ]

    create_test_log(log_path, test_entries)

    filter_fn = log_reader.filter_by_action("qsync.survey")
    entries = log_reader.read_logs(
        log_file=log_path, filter_fn=filter_fn, reverse=False
    )
    assert len(entries) == 3
    assert all(e["action"].startswith("qsync.survey") for e in entries)


def test_filter_by_error(tmp_path: Path) -> None:
    """Test filtering error entries."""
    log_path = tmp_path / "test.log"
    test_entries = [
        {"action": "test", "status": 200},
        {"action": "test", "status": 404},
        {"action": "test", "status": 200},
        {"action": "test", "status": 500},
        {"action": "test", "error": {"type": "ValueError", "message": "test"}},
    ]

    create_test_log(log_path, test_entries)

    filter_fn = log_reader.filter_by_error()
    entries = log_reader.read_logs(
        log_file=log_path, filter_fn=filter_fn, reverse=False
    )
    assert len(entries) == 3  # status 404, 500, and error field


def test_compute_stats(tmp_path: Path) -> None:
    """Test computing statistics from log entries."""
    log_path = tmp_path / "test.log"
    test_entries = [
        {"action": "qsync.survey.copy", "status": 200},
        {"action": "qsync.survey.copy", "status": 200},
        {"action": "qsync.survey.delete", "status": 404},
        {"action": "qsync.survey.rename", "status": 200},
        {"action": "qsync.survey.delete", "status": 500},
    ]

    create_test_log(log_path, test_entries)

    entries = log_reader.read_logs(log_file=log_path, reverse=False)
    stats = log_reader.compute_stats(entries)

    assert stats["total"] == 5
    assert stats["success"] == 3
    assert stats["errors"] == 2
    assert stats["error_rate"] == 40.0
    assert stats["by_action"]["qsync.survey.copy"] == 2
    assert stats["by_action"]["qsync.survey.delete"] == 2
    assert stats["by_status"][200] == 3
    assert stats["by_status"][404] == 1


def test_format_log_entry() -> None:
    """Test formatting a log entry for display."""
    entry = {
        "_log_line": 42,
        "timestamp": "2026-01-10T12:00:00+00:00",
        "action": "qsync.survey.copy",
        "survey_id": "SV_001",
        "method": "POST",
        "status": 200,
        "user": "testuser",
    }

    formatted = log_reader.format_log_entry(entry)

    assert "[42]" in formatted
    assert "2026-01-10 12:00:00 UTC" in formatted
    assert "qsync.survey.copy" in formatted
    assert "SV_001" in formatted
    assert "POST" in formatted
    assert "200" in formatted
    assert "testuser" in formatted


def test_format_log_entry_with_error() -> None:
    """Test formatting a log entry with error."""
    entry = {
        "_log_line": 1,
        "timestamp": "2026-01-10T12:00:00+00:00",
        "action": "qsync.survey.delete",
        "survey_id": "SV_002",
        "status": 404,
        "error": {
            "type": "HTTPError",
            "message": "404 Not Found",
            "detail": "Survey does not exist",
        },
    }

    formatted = log_reader.format_log_entry(entry, detailed=True)

    assert "HTTPError" in formatted
    assert "404 Not Found" in formatted
    assert "Survey does not exist" in formatted


def test_graceful_handling_malformed_lines(tmp_path: Path) -> None:
    """Test that malformed JSON lines are skipped gracefully."""
    log_path = tmp_path / "test.log"

    with log_path.open("w", encoding="utf-8") as f:
        f.write('{"action": "valid1", "status": 200}\n')
        f.write("this is not valid JSON\n")
        f.write('{"action": "valid2", "status": 200}\n')
        f.write('{"incomplete": \n')  # Incomplete JSON
        f.write('{"action": "valid3", "status": 200}\n')

    entries = log_reader.read_logs(log_file=log_path, reverse=False)

    # Should have skipped the malformed lines
    assert len(entries) == 3
    assert entries[0]["action"] == "valid1"
    assert entries[1]["action"] == "valid2"
    assert entries[2]["action"] == "valid3"


def test_count_total_entries(tmp_path: Path) -> None:
    """Test counting total log entries."""
    log_path = tmp_path / "test.log"
    test_entries = [{"action": f"action_{i}", "status": 200} for i in range(100)]

    create_test_log(log_path, test_entries)

    # Temporarily patch get_log_file_path for this test
    original_fn = log_reader.get_log_file_path
    log_reader.get_log_file_path = lambda: log_path

    try:
        count = log_reader.count_total_entries()
        assert count == 100
    finally:
        log_reader.get_log_file_path = original_fn
