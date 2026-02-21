from __future__ import annotations

import json
from pathlib import Path

from qsync import push_logger


def _load_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_log_push_event_includes_session_parent_and_duration(tmp_path: Path) -> None:
    push_logger.set_session_id("session-test-1")

    with push_logger.push_log_scope("qsync.sync"):
        push_logger.log_push_event(
            "qsync.test.action",
            method="POST",
            path="https://example/api",
            survey_id="SV_TEST",
            status=200,
            duration_ms=123.7,
            root=tmp_path,
        )

    log_file = tmp_path / "logs" / "qualtrics_push.log"
    entries = _load_entries(log_file)
    assert len(entries) == 1

    entry = entries[0]
    assert entry["session_id"] == "session-test-1"
    assert entry["parent_action"] == "qsync.sync"
    assert entry["duration_ms"] == 124
    assert entry["level"] == "INFO"


def test_log_push_event_respects_log_level_threshold(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("QSYNC_LOG_LEVEL", "ERROR")
    push_logger.set_session_id("session-test-2")

    # INFO-level (POST success) should be filtered out.
    push_logger.log_push_event(
        "qsync.test.info",
        method="POST",
        path="https://example/info",
        survey_id="SV_TEST",
        status=200,
        root=tmp_path,
    )

    # ERROR-level should be persisted.
    push_logger.log_push_event(
        "qsync.test.error",
        method="POST",
        path="https://example/error",
        survey_id="SV_TEST",
        status=500,
        error={"type": "HTTPError", "message": "boom"},
        root=tmp_path,
    )

    log_file = tmp_path / "logs" / "qualtrics_push.log"
    entries = _load_entries(log_file)
    assert len(entries) == 1
    assert entries[0]["action"] == "qsync.test.error"
    assert entries[0]["level"] == "ERROR"
