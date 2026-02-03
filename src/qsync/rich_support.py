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
from typing import Iterable, Iterator, Optional, Tuple

from .terminal_colors import colors_enabled


def should_use_rich() -> bool:
    """Return True when Rich output should be used."""
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


@contextmanager
def rich_status(message: str, *, spinner: str = "dots") -> Iterator[None]:
    """Show a Rich status spinner while running a block."""
    if not should_use_rich():
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

    from rich.progress import (
        Progress,
        SpinnerColumn,
        TextColumn,
        BarColumn,
        TaskProgressColumn,
        TimeElapsedColumn,
    )

    console = _build_console()

    def _generator() -> Iterator:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        ) as progress:
            task_id = progress.add_task(description, total=total)
            for item in iterable:
                yield item
                progress.advance(task_id)

    return _generator()


@contextmanager
def progress_context(
    description: str, *, total: Optional[int] = None
) -> Iterator[Optional[Tuple[object, int]]]:
    """Context manager yielding a (progress, task_id) tuple when enabled."""
    if not should_use_rich():
        yield None
        return

    from rich.progress import (
        Progress,
        SpinnerColumn,
        TextColumn,
        BarColumn,
        TaskProgressColumn,
        TimeElapsedColumn,
    )

    console = _build_console()
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task_id = progress.add_task(description, total=total)
        yield (progress, task_id)
