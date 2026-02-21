"""Write structured qsync operation events to `logs/qualtrics_push.log`."""

from __future__ import annotations

import contextvars
import getpass
import json
import os
import socket
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import resolve_root
from .log_rotation import rotate_log_file

ROOT = resolve_root(required=False) or Path.cwd()

_HOSTNAME = socket.gethostname()
_USERNAME = getpass.getuser()
_LOG_LEVELS = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
}
_SESSION_ID = str(uuid.uuid4())
_PARENT_ACTION: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "qsync_parent_action", default=None
)


def _detect_git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        )
        commit = result.stdout.strip()
        return commit or None
    except Exception:
        return None


_GIT_COMMIT = _detect_git_commit()


def create_session_id() -> str:
    """Return a fresh session identifier suitable for one CLI invocation."""

    return str(uuid.uuid4())


def set_session_id(session_id: str | None = None) -> str:
    """Set and return the active logging session id."""

    global _SESSION_ID
    _SESSION_ID = (session_id or create_session_id()).strip()
    if not _SESSION_ID:
        _SESSION_ID = create_session_id()
    return _SESSION_ID


def get_session_id() -> str:
    """Return the active logging session id."""

    return _SESSION_ID


@contextmanager
def push_log_scope(parent_action: str | None):
    """Temporarily set a parent operation action for grouped log entries."""

    token = _PARENT_ACTION.set((parent_action or "").strip() or None)
    try:
        yield
    finally:
        _PARENT_ACTION.reset(token)


def current_parent_action() -> str | None:
    """Return the active parent action, if any."""

    return _PARENT_ACTION.get()


def _safe(obj: Any) -> Any:
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if isinstance(obj, Mapping):
        return {str(key): _safe(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_safe(value) for value in obj]
    if isinstance(obj, Path):
        return str(obj)
    return repr(obj)


def _is_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def _resolve_log_file(*, root: Path | None = None) -> Path | None:
    if _is_truthy(os.environ.get("NEWSFLOWS_LOG_DISABLED")) or _is_truthy(
        os.environ.get("QSYNC_LOG_DISABLED")
    ):
        return None

    override = os.environ.get("QSYNC_LOG_DIR") or os.environ.get("NEWSFLOWS_LOG_DIR")
    if override:
        log_dir = Path(override).expanduser()
    else:
        base = root or resolve_root(required=False) or Path.cwd()
        log_dir = base / "logs"
    return log_dir / "qualtrics_push.log"


def _normalize_level(value: str | None) -> str:
    normalized = str(value or "").strip().upper()
    if normalized in _LOG_LEVELS:
        return normalized
    return "INFO"


def _resolve_min_level() -> str:
    raw = (
        os.environ.get("QSYNC_LOG_LEVEL")
        or os.environ.get("NEWSFLOWS_LOG_LEVEL")
        or "INFO"
    )
    return _normalize_level(raw)


def _derive_level(
    *,
    level: str | None,
    method: str,
    status: int | None,
    error: Mapping[str, Any] | None,
) -> str:
    if level is not None:
        return _normalize_level(level)
    if error is not None or (status is not None and status >= 400):
        return "ERROR"
    if str(method or "").upper() == "GET":
        return "DEBUG"
    return "INFO"


def _should_log(level: str) -> bool:
    threshold = _resolve_min_level()
    return _LOG_LEVELS[level] >= _LOG_LEVELS[threshold]


def log_push_event(
    action: str,
    *,
    method: str,
    path: str,
    survey_id: str | None = None,
    status: int | None = None,
    error: Mapping[str, Any] | None = None,
    meta: Mapping[str, Any] | None = None,
    level: str | None = None,
    duration_ms: int | float | None = None,
    parent_action: str | None = None,
    session_id: str | None = None,
    root: Path | None = None,
) -> None:
    """Append a JSONL event describing an operation (best-effort)."""

    log_file = _resolve_log_file(root=root)
    if log_file is None:
        return

    entry_level = _derive_level(level=level, method=method, status=status, error=error)
    if not _should_log(entry_level):
        return

    resolved_parent = (parent_action or "").strip() or current_parent_action()

    entry: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "hostname": _HOSTNAME,
        "user": _USERNAME,
        "git_commit": _GIT_COMMIT,
        "script": Path(sys.argv[0]).name if sys.argv else None,
        "session_id": (session_id or get_session_id()),
        "parent_action": resolved_parent,
        "level": entry_level,
        "action": action,
        "method": method,
        "path": path,
        "survey_id": survey_id,
        "status": status,
    }
    if error:
        entry["error"] = _safe(error)
    if meta:
        entry["meta"] = _safe(meta)
    if duration_ms is not None:
        try:
            entry["duration_ms"] = max(int(round(float(duration_ms))), 0)
        except (TypeError, ValueError):
            pass

    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        rotate_log_file(log_file)
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception as exc:  # pragma: no cover - logging must never raise
        try:
            print(f"[push-log] Failed to record event: {exc}", file=sys.stderr)
        except Exception:
            pass
