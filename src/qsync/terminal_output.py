"""Shared terminal output helpers for qsync CLIs.

These helpers standardize:
- prefix rendering (e.g., "[qsync:push]")
- severity styling (success/warn/error/header/dim)
- stderr routing for warnings/errors

Note: Machine-readable output modes (e.g. JSON-only) should avoid these helpers
and print only JSON to stdout.

Terminal style guide (human output):
- Headers: `header(prefix, message)` for major steps / section starts.
- Sections: use a blank line, then a header, then indented bullets/lines.
- Summaries/decisions: prefer Rich `Panel`/`Table`/`Columns` when available.
- Non-TTY / CI / JSON mode: keep output line-oriented; no cursor control.
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, TextIO

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


def format_elapsed(seconds: float) -> str:
    """Format elapsed time for user-friendly output."""
    if seconds < 1:
        return "< 1s"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    remaining = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}m {remaining:02d}s"
    hours = minutes // 60
    minutes = minutes % 60
    return f"{hours}h {minutes:02d}m"


_TIMING_EMITTED: ContextVar[bool] = ContextVar("qsync_timing_emitted", default=False)


def mark_timing_emitted() -> None:
    _TIMING_EMITTED.set(True)


def reset_timing_emitted() -> None:
    _TIMING_EMITTED.set(False)


def is_json_mode() -> bool:
    return (os.environ.get("QSYNC_JSON_MODE") or "").strip().lower() in {
        "1",
        "true",
        "t",
        "yes",
        "y",
        "on",
    }


@contextmanager
def operation_timer(
    prefix: str | None,
    *,
    enabled: bool | None = None,
) -> Iterator[None]:
    """Emit a timing footer after an operation completes."""
    if enabled is None:
        # Only emit timing in interactive terminals (avoid polluting machine-readable
        # output and captured stdout in tests).
        enabled = not is_json_mode() and sys.stdout.isatty()
    if not enabled:
        yield
        return

    start_time = time.perf_counter()
    try:
        yield
    finally:
        if sys.exc_info()[0] is not None:
            return
        if _TIMING_EMITTED.get():
            return
        elapsed = time.perf_counter() - start_time
        dim(prefix, f"Completed in {format_elapsed(elapsed)}")
        mark_timing_emitted()


def rich_console():
    """Return a Rich Console when Rich output is allowed, else None."""
    from .rich_support import should_use_rich

    if not should_use_rich():
        return None

    from rich.console import Console

    try:
        from .terminal_colors import colors_enabled

        no_color = not colors_enabled()
    except Exception:
        no_color = False

    return Console(no_color=no_color)


def print_panel(
    title: str,
    body: str,
    *,
    border_style: str = "cyan",
) -> None:
    """Print a Rich panel (TTY only) with a plain-text fallback."""
    if is_json_mode():
        return
    console = rich_console()
    if console is None:
        header(None, title)
        if body:
            print(body)
        return

    from rich import box
    from rich.panel import Panel

    console.print(
        Panel(
            body or "",
            title=title,
            border_style=border_style,
            box=box.ROUNDED,
        )
    )


def print_panels_in_columns(
    panels: list[tuple[str, str, str]],
    *,
    expand: bool = True,
    equal: bool = True,
) -> None:
    """Print multiple (title, body, border_style) panels in columns when possible."""
    if is_json_mode():
        return
    console = rich_console()
    if console is None:
        for title, body, _style in panels:
            header(None, title)
            if body:
                print(body)
            print()
        return

    from rich import box
    from rich.columns import Columns
    from rich.panel import Panel

    renderables = [
        Panel(
            body or "",
            title=title,
            border_style=style,
            box=box.ROUNDED,
        )
        for title, body, style in panels
    ]
    console.print(Columns(renderables, expand=expand, equal=equal))
