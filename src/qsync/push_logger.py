"""Write structured qsync push events to `logs/qualtrics_push.log`."""

from __future__ import annotations

import getpass
import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import resolve_root

ROOT = resolve_root(required=False) or Path.cwd()

_HOSTNAME = socket.gethostname()
_USERNAME = getpass.getuser()


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


def log_push_event(
    action: str,
    *,
    method: str,
    path: str,
    survey_id: str | None = None,
    status: int | None = None,
    error: Mapping[str, Any] | None = None,
    meta: Mapping[str, Any] | None = None,
    root: Path | None = None,
) -> None:
    """Append a JSONL event describing a push/preview action (best-effort)."""

    log_file = _resolve_log_file(root=root)
    if log_file is None:
        return

    entry: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "hostname": _HOSTNAME,
        "user": _USERNAME,
        "git_commit": _GIT_COMMIT,
        "script": Path(sys.argv[0]).name if sys.argv else None,
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

    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception as exc:  # pragma: no cover - logging must never raise
        try:
            print(f"[push-log] Failed to record event: {exc}", file=sys.stderr)
        except Exception:
            pass
