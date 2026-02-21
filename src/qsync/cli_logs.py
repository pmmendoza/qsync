"""CLI commands for viewing and analyzing qsync operation logs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from . import log_reader
from . import log_rotation
from .argparse_support import reorder_subparser_choices
from .log_analyzer import generate_error_report, render_error_report
from .terminal_output import dim, error, header, success, warn


def _add_common_log_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--level",
        choices=list(log_reader.LOG_LEVELS),
        help="Filter entries at or above this level",
    )
    parser.add_argument(
        "--include-archives",
        action="store_true",
        help="Include compressed archive logs",
    )


def _combined_filter(
    *filters: Callable[[dict[str, Any]], bool] | None,
    level: str | None = None,
) -> Callable[[dict[str, Any]], bool] | None:
    level_filter = log_reader.filter_by_level(level) if level else None
    return log_reader.combine_filters(*filters, level_filter)


def register_logs_commands(subparsers: Any) -> None:
    """Register `qsync logs` subcommand and its children."""
    logs_parser = subparsers.add_parser(
        "logs",
        help="View and analyze operation logs (group)",
        description="View and analyze qsync operation logs (logs/qualtrics_push.log)",
    )

    logs_subparsers = logs_parser.add_subparsers(
        dest="logs_command",
        required=True,
        metavar="COMMAND",
    )

    recent_parser = logs_subparsers.add_parser(
        "recent",
        help="Show recent operations",
        description="Display the most recent qsync operations from the log",
    )
    recent_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        metavar="N",
        help="Number of operations to show (default: 10)",
    )
    _add_common_log_filters(recent_parser)
    recent_parser.set_defaults(func=handle_recent)

    errors_parser = logs_subparsers.add_parser(
        "errors",
        help="Show recent errors",
        description="Display recent failed operations (status >= 400 or explicit errors)",
    )
    errors_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        metavar="N",
        help="Number of errors to show (default: 10)",
    )
    _add_common_log_filters(errors_parser)
    errors_parser.set_defaults(func=handle_errors)

    session_parser = logs_subparsers.add_parser(
        "session",
        help="Show operations for a logging session",
        description="Display all operations for a specific log session_id",
    )
    session_parser.add_argument(
        "session_id",
        metavar="SESSION_ID",
        help="Session identifier to filter by",
    )
    session_parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Maximum number of operations to show (default: all)",
    )
    _add_common_log_filters(session_parser)
    session_parser.set_defaults(func=handle_session)

    stats_parser = logs_subparsers.add_parser(
        "stats",
        help="Show summary statistics",
        description="Display summary statistics from the operation log",
    )
    _add_common_log_filters(stats_parser)
    stats_parser.set_defaults(func=handle_stats)

    slow_parser = logs_subparsers.add_parser(
        "slow",
        help="Show slow operations",
        description="Display operations sorted by highest duration_ms",
    )
    slow_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        metavar="N",
        help="Number of operations to show (default: 10)",
    )
    _add_common_log_filters(slow_parser)
    slow_parser.set_defaults(func=handle_slow)

    report_parser = logs_subparsers.add_parser(
        "report",
        help="Generate structured error report",
        description="Analyze log errors, trends, and systemic issues",
    )
    window_group = report_parser.add_mutually_exclusive_group()
    window_group.add_argument(
        "--daily",
        action="store_true",
        help="Group trend metrics by day (default)",
    )
    window_group.add_argument(
        "--weekly",
        action="store_true",
        help="Group trend metrics by ISO week",
    )
    report_parser.add_argument(
        "--errors-only",
        action="store_true",
        help="Limit report totals to error entries",
    )
    report_parser.add_argument(
        "--json",
        action="store_true",
        help="Print report JSON to stdout",
    )
    report_parser.add_argument(
        "--report-path",
        type=Path,
        help="Optional path to write report JSON",
    )
    _add_common_log_filters(report_parser)
    report_parser.set_defaults(func=handle_report)

    survey_parser = logs_subparsers.add_parser(
        "survey",
        help="Show operations for a specific survey",
        description="Display all operations for a specific survey ID",
    )
    survey_parser.add_argument(
        "survey_id",
        metavar="SURVEY_ID",
        help="Survey ID to filter by (e.g., SV_xxx)",
    )
    survey_parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Maximum number of operations to show (default: all)",
    )
    _add_common_log_filters(survey_parser)
    survey_parser.set_defaults(func=handle_survey)

    action_parser = logs_subparsers.add_parser(
        "action",
        help="Show operations for a specific action",
        description="Display all operations matching an action prefix (e.g., 'qsync.survey')",
    )
    action_parser.add_argument(
        "action",
        metavar="ACTION",
        help="Action prefix to filter by (e.g., 'qsync.survey', 'qsync.master')",
    )
    action_parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Maximum number of operations to show (default: all)",
    )
    _add_common_log_filters(action_parser)
    action_parser.set_defaults(func=handle_action)

    since_parser = logs_subparsers.add_parser(
        "since",
        help="Show operations since a timestamp",
        description="Display operations since a specific timestamp (ISO 8601 format)",
    )
    since_parser.add_argument(
        "timestamp",
        metavar="TIMESTAMP",
        help="ISO 8601 timestamp (e.g., '2026-01-10T12:00:00')",
    )
    since_parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Maximum number of operations to show (default: all)",
    )
    _add_common_log_filters(since_parser)
    since_parser.set_defaults(func=handle_since)

    rotate_parser = logs_subparsers.add_parser(
        "rotate",
        help="Rotate current log file now",
        description="Rotate logs/qualtrics_push.log into logs/archive/",
    )
    rotate_parser.add_argument(
        "--force",
        action="store_true",
        help="Rotate even if no threshold/month condition is met",
    )
    rotate_parser.add_argument(
        "--max-bytes",
        type=int,
        metavar="N",
        help="Override size threshold in bytes (default: QSYNC_LOG_ROTATION_SIZE or 10MB)",
    )
    rotate_parser.add_argument(
        "--retention-months",
        type=int,
        metavar="N",
        help="Override archive retention in months (default: QSYNC_LOG_RETENTION_MONTHS or 12)",
    )
    rotate_parser.set_defaults(func=handle_rotate)

    archives_parser = logs_subparsers.add_parser(
        "archives",
        help="List archived logs",
        description="List compressed archived logs in logs/archive/",
    )
    archives_parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Maximum number of archives to show (default: all)",
    )
    archives_parser.set_defaults(func=handle_archives)

    reorder_subparser_choices(
        logs_subparsers,
        [
            "recent",
            "errors",
            "session",
            "since",
            "survey",
            "action",
            "stats",
            "slow",
            "report",
            "archives",
            "rotate",
        ],
    )


def _print_entries(entries: list[dict[str, Any]], *, detailed: bool = False) -> None:
    for i, entry in enumerate(entries, start=1):
        print(log_reader.format_log_entry(entry, detailed=detailed))
        if i < len(entries):
            print()


def handle_recent(args: argparse.Namespace) -> None:
    try:
        filter_fn = _combined_filter(level=getattr(args, "level", None))
        entries = log_reader.read_logs(
            limit=args.limit,
            reverse=True,
            filter_fn=filter_fn,
            include_archives=bool(getattr(args, "include_archives", False)),
        )

        if not entries:
            warn("[logs]", "No operations found in log")
            return

        header(None, f"\n Recent Operations (last {len(entries)}):\n")
        _print_entries(entries)

        log_file = log_reader.get_log_file_path()
        dim(None, f"\nLog file: {log_file}")
        total = log_reader.count_total_entries(
            include_archives=bool(getattr(args, "include_archives", False))
        )
        dim(None, f"Total entries: {total}")

    except Exception as exc:
        error("[logs]", f"Failed to read logs: {exc}")
        sys.exit(1)


def handle_errors(args: argparse.Namespace) -> None:
    try:
        filter_fn = _combined_filter(
            log_reader.filter_by_error(),
            level=getattr(args, "level", None),
        )
        entries = log_reader.read_logs(
            limit=args.limit,
            reverse=True,
            filter_fn=filter_fn,
            include_archives=bool(getattr(args, "include_archives", False)),
        )

        if not entries:
            success("[logs]", "No errors found in recent operations ✓")
            return

        error("[logs]", f"Recent Errors (last {len(entries)}):")
        _print_entries(entries, detailed=True)

        log_file = log_reader.get_log_file_path()
        dim(None, f"\nLog file: {log_file}")

    except Exception as exc:
        error("[logs]", f"Failed to read logs: {exc}")
        sys.exit(1)


def handle_session(args: argparse.Namespace) -> None:
    try:
        filter_fn = _combined_filter(
            log_reader.filter_by_session(args.session_id),
            level=getattr(args, "level", None),
        )
        entries = log_reader.read_logs(
            reverse=True,
            filter_fn=filter_fn,
            include_archives=bool(getattr(args, "include_archives", False)),
        )
        if args.limit is not None and args.limit > 0:
            entries = entries[: args.limit]

        if not entries:
            warn("[logs]", f"No operations found for session {args.session_id}")
            return

        header(
            None,
            f"\n Operations for session {args.session_id} (showing {len(entries)}):\n",
        )
        _print_entries(entries)

        log_file = log_reader.get_log_file_path()
        dim(None, f"\nLog file: {log_file}")

    except Exception as exc:
        error("[logs]", f"Failed to read logs: {exc}")
        sys.exit(1)


def handle_stats(args: argparse.Namespace) -> None:
    try:
        filter_fn = _combined_filter(level=getattr(args, "level", None))
        all_entries = log_reader.read_logs(
            reverse=False,
            filter_fn=filter_fn,
            include_archives=bool(getattr(args, "include_archives", False)),
        )

        if not all_entries:
            warn("[logs]", "No operations found in log")
            return

        stats = log_reader.compute_stats(all_entries)

        header(None, "\n Operation Statistics:\n")
        print(f"  Total operations:    {stats['total']}")
        success(None, f"  Successful:          {stats['success']} ✓")
        error(None, f"  Failed:              {stats['errors']} ✗")
        print(f"  Error rate:          {stats['error_rate']:.1f}%")

        durations = stats.get("durations", {})
        if durations.get("count", 0):
            header(None, "\n Duration Summary:\n")
            print(f"  Samples:             {durations.get('count', 0)}")
            print(f"  Avg duration:        {durations.get('avg_ms', 0.0):.1f} ms")
            print(f"  P95 duration:        {durations.get('p95_ms', 0.0):.1f} ms")

        header(None, "\n Top Actions:\n")
        by_action = stats["by_action"]
        sorted_actions = sorted(by_action.items(), key=lambda x: x[1], reverse=True)
        for action, count in sorted_actions[:10]:
            print(f"  {action:40s} {count:5d}")

        header(None, "\n Levels:\n")
        for level in log_reader.LOG_LEVELS:
            count = int(stats.get("by_level", {}).get(level, 0))
            print(f"  {level:10s} {count:5d}")

        header(None, "\n Status Codes:\n")
        by_status = stats["by_status"]
        sorted_statuses = sorted(
            by_status.items(),
            key=lambda x: (x[0] if isinstance(x[0], int) else 999, x[1]),
            reverse=False,
        )
        for status, count in sorted_statuses:
            if isinstance(status, int):
                status_color = success if status < 400 else error
                status_color(None, f"  {status:<10} {count:5d}")
            else:
                error(None, f"  {str(status):10s} {count:5d}")

        log_file = log_reader.get_log_file_path()
        dim(None, f"\nLog file: {log_file}")

    except Exception as exc:
        error("[logs]", f"Failed to compute stats: {exc}")
        sys.exit(1)


def handle_slow(args: argparse.Namespace) -> None:
    try:
        filter_fn = _combined_filter(level=getattr(args, "level", None))
        entries = log_reader.read_logs(
            reverse=False,
            filter_fn=filter_fn,
            include_archives=bool(getattr(args, "include_archives", False)),
        )
        entries = [entry for entry in entries if entry.get("duration_ms") is not None]

        def _duration(entry: dict[str, Any]) -> int:
            try:
                return int(entry.get("duration_ms") or 0)
            except (TypeError, ValueError):
                return 0

        entries.sort(key=_duration, reverse=True)
        limit = int(getattr(args, "limit", 10) or 10)
        entries = entries[: max(limit, 0)]

        if not entries:
            warn("[logs]", "No duration entries found")
            return

        header(None, f"\n Slow Operations (top {len(entries)}):\n")
        _print_entries(entries)

        log_file = log_reader.get_log_file_path()
        dim(None, f"\nLog file: {log_file}")

    except Exception as exc:
        error("[logs]", f"Failed to analyze slow operations: {exc}")
        sys.exit(1)


def handle_report(args: argparse.Namespace) -> None:
    try:
        filter_fn = _combined_filter(level=getattr(args, "level", None))
        entries = log_reader.read_logs(
            reverse=False,
            filter_fn=filter_fn,
            include_archives=bool(getattr(args, "include_archives", False)),
        )

        if not entries:
            warn("[logs]", "No operations found in log")
            return

        granularity = "weekly" if bool(getattr(args, "weekly", False)) else "daily"
        report = generate_error_report(
            entries,
            granularity=granularity,
            errors_only=bool(getattr(args, "errors_only", False)),
        )

        report_path = getattr(args, "report_path", None)
        if report_path:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            success("[logs]", f"Wrote report JSON to {report_path}")

        if bool(getattr(args, "json", False)):
            print(json.dumps(report, indent=2))
            return

        header(None, "\n Log Error Report:\n")
        for line in render_error_report(report):
            print(line)

    except Exception as exc:
        error("[logs]", f"Failed to generate report: {exc}")
        sys.exit(1)


def handle_survey(args: argparse.Namespace) -> None:
    try:
        from .survey_ref import format_survey_ref

        filter_fn = _combined_filter(
            log_reader.filter_by_survey(args.survey_id),
            level=getattr(args, "level", None),
        )
        entries = log_reader.read_logs(
            reverse=True,
            filter_fn=filter_fn,
            include_archives=bool(getattr(args, "include_archives", False)),
        )
        survey_ref = format_survey_ref(args.survey_id)

        if args.limit is not None and args.limit > 0:
            entries = entries[: args.limit]

        if not entries:
            warn("[logs]", f"No operations found for survey {survey_ref}")
            return

        header(None, f"\n Operations for Survey {survey_ref} (showing {len(entries)}):\n")
        _print_entries(entries)

        log_file = log_reader.get_log_file_path()
        dim(None, f"\nLog file: {log_file}")

    except Exception as exc:
        error("[logs]", f"Failed to read logs: {exc}")
        sys.exit(1)


def handle_action(args: argparse.Namespace) -> None:
    try:
        filter_fn = _combined_filter(
            log_reader.filter_by_action(args.action),
            level=getattr(args, "level", None),
        )
        entries = log_reader.read_logs(
            reverse=True,
            filter_fn=filter_fn,
            include_archives=bool(getattr(args, "include_archives", False)),
        )

        if args.limit is not None and args.limit > 0:
            entries = entries[: args.limit]

        if not entries:
            warn("[logs]", f"No operations found matching action '{args.action}'")
            return

        header(
            None,
            f"\n Operations matching '{args.action}' (showing {len(entries)}):\n",
        )
        _print_entries(entries)

        log_file = log_reader.get_log_file_path()
        dim(None, f"\nLog file: {log_file}")

    except Exception as exc:
        error("[logs]", f"Failed to read logs: {exc}")
        sys.exit(1)


def handle_since(args: argparse.Namespace) -> None:
    try:
        filter_fn = _combined_filter(
            log_reader.filter_since(args.timestamp),
            level=getattr(args, "level", None),
        )
        entries = log_reader.read_logs(
            reverse=True,
            filter_fn=filter_fn,
            include_archives=bool(getattr(args, "include_archives", False)),
        )

        if args.limit is not None and args.limit > 0:
            entries = entries[: args.limit]

        if not entries:
            warn("[logs]", f"No operations found since {args.timestamp}")
            return

        header(None, f"\n Operations since {args.timestamp} (showing {len(entries)}):\n")
        _print_entries(entries)

        log_file = log_reader.get_log_file_path()
        dim(None, f"\nLog file: {log_file}")

    except Exception as exc:
        error("[logs]", f"Failed to read logs: {exc}")
        sys.exit(1)


def handle_rotate(args: argparse.Namespace) -> None:
    try:
        log_file = log_reader.get_log_file_path()
        result = log_rotation.rotate_log_file(
            log_file,
            force=bool(getattr(args, "force", False)),
            max_bytes=getattr(args, "max_bytes", None),
            retention_months=getattr(args, "retention_months", None),
        )
        if not result.rotated:
            warn("[logs]", "No rotation needed for current log file")
            dim(None, f"Log file: {log_file}")
            return

        archive_label = str(result.archive_path) if result.archive_path else "(unknown)"
        success("[logs]", f"Rotated log ({result.reason})")
        print(f"  Archive: {archive_label}")
        if result.deleted_archives:
            print(f"  Deleted old archives: {len(result.deleted_archives)}")
        dim(None, f"Current log path: {log_file}")
    except Exception as exc:
        error("[logs]", f"Failed to rotate logs: {exc}")
        sys.exit(1)


def handle_archives(args: argparse.Namespace) -> None:
    try:
        log_file = log_reader.get_log_file_path()
        archives = log_rotation.list_archives(log_file)
        if args.limit is not None and args.limit > 0:
            archives = archives[: args.limit]
        if not archives:
            warn("[logs]", "No archives found")
            return

        header(None, f"\n Archived Logs (showing {len(archives)}):\n")
        for archive in archives:
            try:
                size = archive.stat().st_size
            except OSError:
                size = 0
            print(f"  • {archive.name} ({size} bytes)")
        dim(None, f"\nArchive dir: {log_file.parent / 'archive'}")
    except Exception as exc:
        error("[logs]", f"Failed to list archives: {exc}")
        sys.exit(1)
