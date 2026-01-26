"""Utilities for reading and filtering qsync operation logs."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .config import resolve_root
from .survey_ref import format_survey_ref


def get_log_file_path() -> Path:
    """Return the path to the primary log file."""
    root = resolve_root(required=False) or Path.cwd()
    return root / "logs" / "qualtrics_push.log"


def read_logs(
    log_file: Path | None = None,
    *,
    limit: int | None = None,
    reverse: bool = True,
    filter_fn: Callable[[dict[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    """
    Read and parse JSONL log entries.

    Args:
        log_file: Path to log file (default: logs/qualtrics_push.log)
        limit: Maximum number of entries to return (after filtering)
        reverse: If True, return most recent entries first
        filter_fn: Optional function to filter entries (return True to include)

    Returns:
        List of parsed log entries (dicts)
    """
    if log_file is None:
        log_file = get_log_file_path()

    if not log_file.exists():
        return []

    entries: list[dict[str, Any]] = []

    try:
        with log_file.open("r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue

                try:
                    entry = json.loads(line)
                    entry["_log_line"] = line_num  # Track line number for reference

                    # Apply filter if provided
                    if filter_fn is None or filter_fn(entry):
                        entries.append(entry)

                except json.JSONDecodeError:
                    # Skip malformed lines gracefully
                    continue

    except Exception:
        # If file can't be read, return empty list
        return []

    # Reverse to get most recent first
    if reverse:
        entries.reverse()

    # Apply limit after filtering and reversing
    if limit is not None and limit > 0:
        entries = entries[:limit]

    return entries


def filter_by_survey(survey_id: str) -> Callable[[dict[str, Any]], bool]:
    """Create filter function for specific survey ID."""

    def _filter(entry: dict[str, Any]) -> bool:
        return entry.get("survey_id") == survey_id

    return _filter


def filter_by_action(action: str) -> Callable[[dict[str, Any]], bool]:
    """Create filter function for specific action (supports prefix matching)."""

    def _filter(entry: dict[str, Any]) -> bool:
        entry_action = entry.get("action", "")
        if not entry_action:
            return False
        normalized_entry = entry_action.replace("-", ".").replace("_", ".")
        normalized_query = action.replace("-", ".").replace("_", ".")
        return normalized_entry.startswith(normalized_query)

    return _filter


def filter_by_error() -> Callable[[dict[str, Any]], bool]:
    """Create filter function for error entries."""

    def _filter(entry: dict[str, Any]) -> bool:
        # Errors have status >= 400 or explicit error field
        status = entry.get("status")
        has_error = entry.get("error") is not None
        return (status is not None and status >= 400) or has_error

    return _filter


def filter_since(timestamp_str: str) -> Callable[[dict[str, Any]], bool]:
    """Create filter function for entries since a timestamp."""
    try:
        cutoff = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    except ValueError:
        # If timestamp can't be parsed, return no-op filter
        return lambda entry: True

    def _filter(entry: dict[str, Any]) -> bool:
        entry_ts_str = entry.get("timestamp")
        if not entry_ts_str:
            return False

        try:
            entry_ts = datetime.fromisoformat(entry_ts_str.replace("Z", "+00:00"))
            return entry_ts >= cutoff
        except ValueError:
            return False

    return _filter


def format_log_entry(entry: dict[str, Any], *, detailed: bool = False) -> str:
    """
    Format a single log entry for human-readable display.

    Args:
        entry: Parsed log entry dict
        detailed: If True, include all available metadata

    Returns:
        Formatted string representation
    """
    lines: list[str] = []

    # Timestamp
    timestamp = entry.get("timestamp", "Unknown time")
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        formatted_time = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, AttributeError):
        formatted_time = timestamp

    lines.append(f"[{entry.get('_log_line', '?')}] {formatted_time}")

    # Action
    action = entry.get("action", "Unknown action")
    lines.append(f"    Action:     {action}")

    # Survey ID
    survey_id = entry.get("survey_id")
    if survey_id:
        lines.append(f"    Survey:     {format_survey_ref(str(survey_id))}")

    # Method
    method = entry.get("method")
    if method:
        lines.append(f"    Method:     {method}")

    # Status
    status = entry.get("status")
    if status is not None:
        status_emoji = "✓" if status < 400 else "✗"
        lines.append(f"    Status:     {status_emoji} {status}")

    # Error (if present)
    error = entry.get("error")
    if error:
        error_type = error.get("type", "Unknown")
        error_msg = error.get("message", "No message")
        lines.append(f"    Error:      {error_type}: {error_msg}")

        if error.get("detail"):
            lines.append(f"    Detail:     {error['detail']}")
        if error.get("retry_count") is not None:
            lines.append(f"    Retries:    {error.get('retry_count')}")
        if error.get("recoverable") is not None:
            lines.append(
                f"    Recoverable:{' yes' if error.get('recoverable') else ' no'}"
            )
        if error.get("suggestion"):
            lines.append(f"    Suggestion: {error.get('suggestion')}")
        if error.get("docs_url"):
            lines.append(f"    Docs:       {error.get('docs_url')}")

    # User
    user = entry.get("user")
    if user:
        lines.append(f"    User:       {user}")

    # Meta (if detailed or if contains useful info)
    meta = entry.get("meta", {})
    if meta and (detailed or len(meta) <= 3):
        meta_str = ", ".join(f"{k}={v}" for k, v in meta.items())
        if meta_str:
            lines.append(f"    Meta:       {meta_str}")

    return "\n".join(lines)


def compute_stats(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Compute summary statistics from log entries.

    Args:
        entries: List of parsed log entries

    Returns:
        Dict containing various statistics
    """
    total = len(entries)
    if total == 0:
        return {
            "total": 0,
            "success": 0,
            "errors": 0,
            "error_rate": 0.0,
            "by_action": {},
            "by_status": {},
        }

    # Count by status
    success_count = 0
    error_count = 0
    by_status: dict[int | str, int] = {}

    for entry in entries:
        status = entry.get("status")
        if status is not None:
            by_status[status] = by_status.get(status, 0) + 1
            if status < 400:
                success_count += 1
            else:
                error_count += 1
        elif entry.get("error"):
            # Entries with explicit error field but no status
            error_count += 1
            by_status["error"] = by_status.get("error", 0) + 1
        else:
            # No status, no error - likely local operation
            success_count += 1

    # Count by action
    by_action: dict[str, int] = {}
    for entry in entries:
        action = entry.get("action", "unknown")
        by_action[action] = by_action.get(action, 0) + 1

    # Compute error rate
    error_rate = (error_count / total * 100) if total > 0 else 0.0

    return {
        "total": total,
        "success": success_count,
        "errors": error_count,
        "error_rate": error_rate,
        "by_action": by_action,
        "by_status": by_status,
    }


def get_recent_entries(limit: int = 10) -> list[dict[str, Any]]:
    """Convenience function to get recent entries."""
    return read_logs(limit=limit, reverse=True)


def get_error_entries(limit: int = 10) -> list[dict[str, Any]]:
    """Convenience function to get recent error entries."""
    return read_logs(limit=limit, reverse=True, filter_fn=filter_by_error())


def get_survey_entries(survey_id: str) -> list[dict[str, Any]]:
    """Convenience function to get all entries for a survey."""
    return read_logs(reverse=True, filter_fn=filter_by_survey(survey_id))


def count_total_entries() -> int:
    """Count total number of log entries (for performance: doesn't parse JSON)."""
    log_file = get_log_file_path()
    if not log_file.exists():
        return 0

    try:
        with log_file.open("r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except Exception:
        return 0
