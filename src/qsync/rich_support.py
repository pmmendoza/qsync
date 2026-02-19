"""Helpers for optional Rich-based UI (progress/spinners).

Rich output is enabled only when:
- stdout is a TTY, and
- QSYNC_USE_RICH is not explicitly disabled.

Set QSYNC_USE_RICH=0 to disable, or =1 to force (TTY only).
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterable, Iterator, Optional, Tuple, Any

from .terminal_colors import colors_enabled

_RICH_PROGRESS_ACTIVE: ContextVar[bool] = ContextVar(
    "qsync_rich_progress_active", default=False
)


def should_use_rich() -> bool:
    """Return True when Rich output should be used."""
    if os.environ.get("QSYNC_JSON_MODE", "").strip().lower() in {
        "1",
        "true",
        "t",
        "yes",
        "y",
        "on",
    }:
        return False
    if not sys.stdout.isatty():
        return False
    raw = os.environ.get("QSYNC_USE_RICH", "auto").strip().lower()
    if raw in {"0", "false", "no"}:
        return False
    if raw in {"1", "true", "yes"}:
        return True
    # "auto" / unset: enable only when interactive TTY.
    return True


def _build_console():
    from rich.console import Console

    return Console(no_color=not colors_enabled())


def canonical_text_progress_bar(
    current: int,
    total: int,
    *,
    width: int = 18,
    done: str = "#",
    todo: str = "-",
) -> str:
    """Return a deterministic ASCII progress bar.

    Example: ``[######------------]  33% (1/3)``
    """
    safe_total = max(int(total), 0)
    safe_current = max(int(current), 0)
    safe_width = max(int(width), 4)
    if safe_total <= 0:
        return f"[{todo * safe_width}]   0% (0/0)"

    clamped = min(safe_current, safe_total)
    ratio = clamped / safe_total
    filled = int(round(ratio * safe_width))
    filled = max(0, min(filled, safe_width))
    empty = safe_width - filled
    pct = int(round(ratio * 100))
    return f"[{done * filled}{todo * empty}] {pct:>3}% ({clamped}/{safe_total})"


def format_step_progress(
    step: int,
    total: int,
    label: str,
    *,
    width: int = 18,
) -> str:
    """Return a canonical single-line step progress message."""
    safe_total = max(int(total), 0)
    safe_step = max(int(step), 0)
    bar = canonical_text_progress_bar(safe_step, safe_total, width=width)
    remaining = max(safe_total - min(safe_step, safe_total), 0)
    suffix = "step left" if remaining == 1 else "steps left"
    if safe_total <= 0:
        return f"Step {safe_step}: {label}"
    return f"Step {safe_step}/{safe_total} {bar} | {remaining} {suffix} | {label}"


def progress_active() -> bool:
    return bool(_RICH_PROGRESS_ACTIVE.get())


@contextmanager
def rich_status(message: str, *, spinner: str = "dots") -> Iterator[None]:
    """Show a Rich status spinner while running a block."""
    if not should_use_rich() or progress_active():
        yield
        return

    console = _build_console()
    with console.status(message, spinner=spinner):
        yield


def track_iterable(iterable: Iterable, *, description: str) -> Iterable:
    """Iterate with a Rich progress bar when enabled."""
    if not should_use_rich():
        return iterable

    try:
        total = len(iterable)  # type: ignore[arg-type]
    except Exception:
        total = None

    console = _build_console()

    def _generator() -> Iterator:
        token = _RICH_PROGRESS_ACTIVE.set(True)
        try:
            with _canonical_progress(console=console) as progress:
                task_id = progress.add_task(description, total=total)
                for item in iterable:
                    yield item
                    progress.advance(task_id)
        finally:
            _RICH_PROGRESS_ACTIVE.reset(token)

    return _generator()


@contextmanager
def progress_context(
    description: str, *, total: Optional[int] = None
) -> Iterator[Optional[Tuple[object, int]]]:
    """Context manager yielding a (progress, task_id) tuple when enabled."""
    if not should_use_rich():
        yield None
        return

    console = _build_console()
    token = _RICH_PROGRESS_ACTIVE.set(True)
    try:
        with _canonical_progress(console=console) as progress:
            task_id = progress.add_task(description, total=total)
            yield (progress, task_id)
    finally:
        _RICH_PROGRESS_ACTIVE.reset(token)


def _canonical_progress(*, console: Any):
    """Create the canonical Rich progress renderer used across qsync."""
    from rich.progress import (
        Progress,
        SpinnerColumn,
        TextColumn,
        BarColumn,
        TaskProgressColumn,
        MofNCompleteColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )

    return Progress(
        SpinnerColumn(style="cyan"),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(complete_style="green", finished_style="green"),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=True,
    )
