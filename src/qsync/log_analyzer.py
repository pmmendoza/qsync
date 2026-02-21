"""Higher-level analysis helpers for qsync operation logs."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from .log_reader import filter_by_error


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _bucket_timestamp(ts: datetime | None, *, granularity: str) -> str:
    if ts is None:
        return "unknown"
    if granularity == "weekly":
        iso_year, iso_week, _ = ts.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    return ts.date().isoformat()


def _error_type(entry: dict[str, Any]) -> str:
    err = entry.get("error")
    if isinstance(err, dict):
        etype = (err.get("type") or "").strip()
        if etype:
            return etype
    status = entry.get("status")
    if isinstance(status, int):
        return f"HTTP_{status}"
    return "UnknownError"


def _error_code(entry: dict[str, Any]) -> str | None:
    err = entry.get("error")
    if isinstance(err, dict):
        code = (err.get("qualtrics_error_code") or "").strip()
        if code:
            return code
    return None


def _error_message(entry: dict[str, Any]) -> str:
    err = entry.get("error")
    if isinstance(err, dict):
        message = (err.get("message") or "").strip()
        if message:
            return message
    status = entry.get("status")
    if isinstance(status, int):
        return f"HTTP {status}"
    return "Unknown error"


def _error_signature(entry: dict[str, Any]) -> str:
    code = _error_code(entry)
    etype = _error_type(entry)
    message = _error_message(entry)
    if code:
        return f"{etype}:{code}:{message}"
    return f"{etype}:{message}"


def analyze_error_patterns(
    error_entries: list[dict[str, Any]], *, granularity: str = "daily"
) -> dict[str, Any]:
    """Summarize error patterns over time and by category."""

    by_action = Counter()
    by_type = Counter()
    by_code = Counter()
    by_signature = Counter()
    trend = Counter()

    signature_examples: dict[str, dict[str, Any]] = {}

    for entry in error_entries:
        action = str(entry.get("action") or "unknown")
        by_action[action] += 1

        etype = _error_type(entry)
        by_type[etype] += 1

        code = _error_code(entry)
        if code:
            by_code[code] += 1

        signature = _error_signature(entry)
        by_signature[signature] += 1

        ts = _parse_timestamp(entry.get("timestamp"))
        trend[_bucket_timestamp(ts, granularity=granularity)] += 1

        if signature not in signature_examples:
            signature_examples[signature] = {
                "action": action,
                "error_type": etype,
                "error_code": code,
                "message": _error_message(entry),
            }

    recurring = []
    for signature, count in by_signature.most_common(10):
        info = signature_examples.get(signature, {})
        recurring.append(
            {
                "signature": signature,
                "count": count,
                "action": info.get("action"),
                "error_type": info.get("error_type"),
                "error_code": info.get("error_code"),
                "message": info.get("message"),
            }
        )

    return {
        "error_total": len(error_entries),
        "by_action": dict(by_action.most_common()),
        "by_type": dict(by_type.most_common()),
        "by_code": dict(by_code.most_common()),
        "trend": dict(sorted(trend.items())),
        "recurring": recurring,
    }


def identify_systemic_issues(
    all_entries: list[dict[str, Any]],
    error_entries: list[dict[str, Any]],
    *,
    min_samples: int = 3,
    error_rate_threshold: float = 0.2,
) -> list[dict[str, Any]]:
    """Identify actions with persistently high failure rates."""

    total_by_action: dict[str, int] = defaultdict(int)
    errors_by_action: dict[str, int] = defaultdict(int)

    for entry in all_entries:
        action = str(entry.get("action") or "unknown")
        total_by_action[action] += 1

    for entry in error_entries:
        action = str(entry.get("action") or "unknown")
        errors_by_action[action] += 1

    issues: list[dict[str, Any]] = []
    for action, total in total_by_action.items():
        if total < min_samples:
            continue
        errors = errors_by_action.get(action, 0)
        if errors <= 0:
            continue
        rate = float(errors) / float(total)
        if rate < error_rate_threshold:
            continue
        issues.append(
            {
                "action": action,
                "error_count": errors,
                "total_count": total,
                "error_rate": rate,
            }
        )

    issues.sort(key=lambda item: (item["error_rate"], item["error_count"]), reverse=True)
    return issues


def _suggestions_for_report(patterns: dict[str, Any]) -> list[str]:
    suggestions: list[str] = []

    by_code = patterns.get("by_code") or {}
    if "QVAL_3" in by_code:
        suggestions.append(
            "QVAL_3 appears repeatedly: reduce per-value translation payload size or use survey-definition question updates for long text."
        )
    if "QMST_1" in by_code:
        suggestions.append(
            "QMST_1 appears repeatedly: validate question payload schema and field names before push."
        )

    by_type = patterns.get("by_type") or {}
    if by_type.get("HTTPError", 0) >= 3:
        suggestions.append(
            "Multiple HTTP errors detected: verify credentials/account context (`qsync account status`) and endpoint permissions."
        )
    if by_type.get("ConnectionError", 0) >= 2 or by_type.get("Timeout", 0) >= 2:
        suggestions.append(
            "Network instability detected: retry with backoff and review connectivity/rate limiting."
        )

    if not suggestions:
        suggestions.append(
            "No dominant recurring error signature detected; inspect per-action failures for project-specific remediation."
        )

    return suggestions


def generate_error_report(
    entries: list[dict[str, Any]],
    *,
    granularity: str = "daily",
    errors_only: bool = False,
) -> dict[str, Any]:
    """Generate a structured report for operational error analysis."""

    filtered_entries = list(entries)
    error_filter = filter_by_error()
    error_entries = [entry for entry in filtered_entries if error_filter(entry)]

    patterns = analyze_error_patterns(error_entries, granularity=granularity)
    systemic = identify_systemic_issues(filtered_entries, error_entries)

    timestamps = [
        _parse_timestamp(entry.get("timestamp"))
        for entry in (error_entries if errors_only else filtered_entries)
    ]
    parsed_times = [ts for ts in timestamps if ts is not None]
    range_start = min(parsed_times).astimezone(timezone.utc).isoformat() if parsed_times else None
    range_end = max(parsed_times).astimezone(timezone.utc).isoformat() if parsed_times else None

    total = len(error_entries) if errors_only else len(filtered_entries)
    error_total = len(error_entries)
    error_rate = float(error_total) / float(total) * 100.0 if total > 0 else 0.0

    report = {
        "window": granularity,
        "errors_only": bool(errors_only),
        "time_range": {
            "start": range_start,
            "end": range_end,
        },
        "totals": {
            "operations": total,
            "errors": error_total,
            "error_rate_pct": error_rate,
        },
        "patterns": patterns,
        "systemic_issues": systemic,
        "suggestions": _suggestions_for_report(patterns),
    }
    return report


def render_error_report(report: dict[str, Any]) -> list[str]:
    """Render a concise human-readable report for terminal output."""

    lines: list[str] = []
    lines.append("Error Report")
    lines.append("")

    totals = report.get("totals", {})
    lines.append(
        f"Operations: {totals.get('operations', 0)} | Errors: {totals.get('errors', 0)} | Error rate: {totals.get('error_rate_pct', 0.0):.1f}%"
    )

    time_range = report.get("time_range", {})
    if time_range.get("start") or time_range.get("end"):
        lines.append(
            f"Range: {time_range.get('start') or '?'} -> {time_range.get('end') or '?'}"
        )

    patterns = report.get("patterns", {})
    by_action = patterns.get("by_action", {})
    if by_action:
        lines.append("")
        lines.append("Top failing actions:")
        for action, count in list(by_action.items())[:10]:
            lines.append(f"  {action}: {count}")

    recurring = patterns.get("recurring", [])
    if recurring:
        lines.append("")
        lines.append("Recurring error signatures:")
        for item in recurring[:10]:
            message = item.get("message") or ""
            lines.append(
                f"  {item.get('count', 0)}x {item.get('error_type', 'Unknown')}"
                + (f" ({item.get('error_code')})" if item.get("error_code") else "")
                + (f": {message}" if message else "")
            )

    systemic = report.get("systemic_issues", [])
    if systemic:
        lines.append("")
        lines.append("Systemic issues:")
        for issue in systemic[:10]:
            lines.append(
                f"  {issue.get('action')}: {issue.get('error_count')}/{issue.get('total_count')} errors ({issue.get('error_rate', 0.0) * 100.0:.1f}%)"
            )

    suggestions = report.get("suggestions", [])
    if suggestions:
        lines.append("")
        lines.append("Suggestions:")
        for suggestion in suggestions[:10]:
            lines.append(f"  - {suggestion}")

    return lines
