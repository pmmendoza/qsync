"""Shared terminal output helpers for qsync CLIs.

These helpers standardize:
- prefix rendering (e.g., "[qsync:push]")
- severity styling (success/warn/error/header/dim)
- stderr routing for warnings/errors

Note: Machine-readable output modes (e.g. JSON-only) should avoid these helpers
and print only JSON to stdout.
"""

from __future__ import annotations

import sys
from typing import TextIO

from .terminal_colors import Colors, colored


def _format_prefix(prefix: str | None) -> str:
    if not prefix:
        return ""
    return colored(prefix, Colors.CYAN, bold=True)


def _emit(
    prefix: str | None,
    message: str,
    *,
    file: TextIO,
    color: str | None = None,
    bold: bool = False,
    dim: bool = False,
) -> None:
    rendered_prefix = _format_prefix(prefix)
    rendered_message = message
    if color is not None:
        rendered_message = colored(message, color, bold=bold, dim=dim)
    if rendered_prefix:
        print(f"{rendered_prefix} {rendered_message}", file=file)
    else:
        print(rendered_message, file=file)


def header(prefix: str | None, message: str) -> None:
    _emit(prefix, message, file=sys.stdout, color=Colors.CYAN, bold=True)


def info(prefix: str | None, message: str) -> None:
    _emit(prefix, message, file=sys.stdout, color=None)


def success(prefix: str | None, message: str) -> None:
    _emit(prefix, message, file=sys.stdout, color=Colors.GREEN)


def warn(prefix: str | None, message: str) -> None:
    _emit(prefix, message, file=sys.stderr, color=Colors.YELLOW, bold=True)


def error(prefix: str | None, message: str) -> None:
    _emit(prefix, message, file=sys.stderr, color=Colors.RED, bold=True)


def dim(prefix: str | None, message: str) -> None:
    _emit(prefix, message, file=sys.stdout, color=Colors.GRAY, dim=True)


def log_confirmation(prefix: str | None) -> None:
    """
    Print confirmation that operation was logged.

    Format:
      [prefix] Logged to logs/qualtrics_push.log
      [prefix] View: qsync logs recent --limit 1

    Only prints if logging is enabled (checks QSYNC_LOG_DISABLED env var).
    """
    import os
    from pathlib import Path

    # Check if logging is disabled
    log_disabled_raw = os.environ.get("QSYNC_LOG_DISABLED") or os.environ.get(
        "NEWSFLOWS_LOG_DISABLED"
    )
    if log_disabled_raw and log_disabled_raw.strip().lower() in {
        "1",
        "true",
        "t",
        "yes",
        "y",
        "on",
    }:
        return

    # Determine log file location
    override = os.environ.get("QSYNC_LOG_DIR") or os.environ.get("NEWSFLOWS_LOG_DIR")
    if override:
        log_path = Path(override).expanduser() / "qualtrics_push.log"
    else:
        log_path = Path("logs") / "qualtrics_push.log"

    _emit(
        prefix,
        f"Logged to {log_path}",
        file=sys.stdout,
        color=Colors.GRAY,
        dim=True,
    )
    _emit(
        prefix,
        "View: qsync logs recent --limit 1",
        file=sys.stdout,
        color=Colors.GRAY,
        dim=True,
    )


def prompt_yes_no(message: str, *, default: bool = True) -> bool:
    """
    Prompt user for yes/no confirmation.

    Args:
        message: Prompt message
        default: Default value if user presses Enter without input

    Returns:
        True if user confirmed, False otherwise
    """
    prompt_suffix = " [Y/n]" if default else " [y/N]"
    full_prompt = colored(message + prompt_suffix, Colors.CYAN, bold=True) + " "

    try:
        response = input(full_prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()  # newline after ^C or ^D
        return False

    if not response:
        return default

    return response in {"y", "yes"}
