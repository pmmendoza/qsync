"""CLI commands for viewing and analyzing qsync operation logs."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from . import log_reader
from .argparse_support import reorder_subparser_choices
from .terminal_colors import dim, error, header, success, warn


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

    # qsync logs recent
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
    recent_parser.set_defaults(func=handle_recent)

    # qsync logs errors
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
    errors_parser.set_defaults(func=handle_errors)

    # qsync logs stats
    stats_parser = logs_subparsers.add_parser(
        "stats",
        help="Show summary statistics",
        description="Display summary statistics from the operation log",
    )
    stats_parser.set_defaults(func=handle_stats)

    # qsync logs survey
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
    survey_parser.set_defaults(func=handle_survey)

    # qsync logs action
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
    action_parser.set_defaults(func=handle_action)

    # qsync logs since
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
    since_parser.set_defaults(func=handle_since)

    reorder_subparser_choices(
        logs_subparsers,
        [
            "recent",
            "errors",
            "since",
            "survey",
            "action",
            "stats",
        ],
    )


def handle_recent(args: argparse.Namespace) -> None:
    """Handle `qsync logs recent` command."""
    try:
        entries = log_reader.get_recent_entries(limit=args.limit)

        if not entries:
            print(warn("[logs] No operations found in log"))
            return

        print(header(f"\n Recent Operations (last {len(entries)}):\n"))

        for i, entry in enumerate(entries, start=1):
            formatted = log_reader.format_log_entry(entry)
            print(formatted)
            if i < len(entries):
                print()  # Blank line between entries

        # Show hint about log location
        log_file = log_reader.get_log_file_path()
        print(dim(f"\nLog file: {log_file}"))
        total = log_reader.count_total_entries()
        print(dim(f"Total entries: {total}"))

    except Exception as exc:
        print(error(f"[logs] Failed to read logs: {exc}"), file=sys.stderr)
        sys.exit(1)


def handle_errors(args: argparse.Namespace) -> None:
    """Handle `qsync logs errors` command."""
    try:
        entries = log_reader.get_error_entries(limit=args.limit)

        if not entries:
            print(success("[logs] No errors found in recent operations ✓"))
            return

        print(error(f"\n Recent Errors (last {len(entries)}):\n"))

        for i, entry in enumerate(entries, start=1):
            formatted = log_reader.format_log_entry(entry, detailed=True)
            print(formatted)
            if i < len(entries):
                print()  # Blank line between entries

        # Show hint
        log_file = log_reader.get_log_file_path()
        print(dim(f"\nLog file: {log_file}"))

    except Exception as exc:
        print(error(f"[logs] Failed to read logs: {exc}"), file=sys.stderr)
        sys.exit(1)


def handle_stats(args: argparse.Namespace) -> None:
    """Handle `qsync logs stats` command."""
    try:
        # Read all entries for stats
        all_entries = log_reader.read_logs(reverse=False)

        if not all_entries:
            print(warn("[logs] No operations found in log"))
            return

        stats = log_reader.compute_stats(all_entries)

        print(header("\n Operation Statistics:\n"))

        # Overall stats
        print(f"  Total operations:    {stats['total']}")
        print(success(f"  Successful:          {stats['success']} ✓"))
        print(error(f"  Failed:              {stats['errors']} ✗"))
        print(f"  Error rate:          {stats['error_rate']:.1f}%")

        # Top actions
        print(header("\n Top Actions:\n"))
        by_action = stats["by_action"]
        sorted_actions = sorted(by_action.items(), key=lambda x: x[1], reverse=True)
        for action, count in sorted_actions[:10]:
            print(f"  {action:40s} {count:5d}")

        # Status codes
        print(header("\n Status Codes:\n"))
        by_status = stats["by_status"]
        sorted_statuses = sorted(
            by_status.items(),
            key=lambda x: (x[0] if isinstance(x[0], int) else 999, x[1]),
            reverse=False,
        )
        for status, count in sorted_statuses:
            if isinstance(status, int):
                status_color = success if status < 400 else error
                print(status_color(f"  {status:10s} {count:5d}"))
            else:
                print(error(f"  {status:10s} {count:5d}"))

        # Show log file
        log_file = log_reader.get_log_file_path()
        print(dim(f"\nLog file: {log_file}"))

    except Exception as exc:
        print(error(f"[logs] Failed to compute stats: {exc}"), file=sys.stderr)
        sys.exit(1)


def handle_survey(args: argparse.Namespace) -> None:
    """Handle `qsync logs survey` command."""
    try:
        from .survey_ref import format_survey_ref

        entries = log_reader.get_survey_entries(args.survey_id)
        survey_ref = format_survey_ref(args.survey_id)

        # Apply limit if specified
        if args.limit is not None and args.limit > 0:
            entries = entries[: args.limit]

        if not entries:
            print(warn(f"[logs] No operations found for survey {survey_ref}"))
            return

        print(
            header(f"\n Operations for Survey {survey_ref} (showing {len(entries)}):\n")
        )

        for i, entry in enumerate(entries, start=1):
            formatted = log_reader.format_log_entry(entry)
            print(formatted)
            if i < len(entries):
                print()  # Blank line between entries

        # Show hint
        log_file = log_reader.get_log_file_path()
        print(dim(f"\nLog file: {log_file}"))

    except Exception as exc:
        print(error(f"[logs] Failed to read logs: {exc}"), file=sys.stderr)
        sys.exit(1)


def handle_action(args: argparse.Namespace) -> None:
    """Handle `qsync logs action` command."""
    try:
        filter_fn = log_reader.filter_by_action(args.action)
        entries = log_reader.read_logs(reverse=True, filter_fn=filter_fn)

        # Apply limit if specified
        if args.limit is not None and args.limit > 0:
            entries = entries[: args.limit]

        if not entries:
            print(warn(f"[logs] No operations found matching action '{args.action}'"))
            return

        print(
            header(
                f"\n Operations matching '{args.action}' (showing {len(entries)}):\n"
            )
        )

        for i, entry in enumerate(entries, start=1):
            formatted = log_reader.format_log_entry(entry)
            print(formatted)
            if i < len(entries):
                print()  # Blank line between entries

        # Show hint
        log_file = log_reader.get_log_file_path()
        print(dim(f"\nLog file: {log_file}"))

    except Exception as exc:
        print(error(f"[logs] Failed to read logs: {exc}"), file=sys.stderr)
        sys.exit(1)


def handle_since(args: argparse.Namespace) -> None:
    """Handle `qsync logs since` command."""
    try:
        filter_fn = log_reader.filter_since(args.timestamp)
        entries = log_reader.read_logs(reverse=True, filter_fn=filter_fn)

        # Apply limit if specified
        if args.limit is not None and args.limit > 0:
            entries = entries[: args.limit]

        if not entries:
            print(warn(f"[logs] No operations found since {args.timestamp}"))
            return

        print(
            header(f"\n Operations since {args.timestamp} (showing {len(entries)}):\n")
        )

        for i, entry in enumerate(entries, start=1):
            formatted = log_reader.format_log_entry(entry)
            print(formatted)
            if i < len(entries):
                print()  # Blank line between entries

        # Show hint
        log_file = log_reader.get_log_file_path()
        print(dim(f"\nLog file: {log_file}"))

    except Exception as exc:
        print(error(f"[logs] Failed to read logs: {exc}"), file=sys.stderr)
        sys.exit(1)
