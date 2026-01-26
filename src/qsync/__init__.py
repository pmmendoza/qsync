"""
NEWSFLOWS Qualtrics–Excel syncing tools (Phase 1).

This package implements the Phase 1 / MVP functionality described in
`project_dev/excel_syncing_plan-MVP.md` and `rules/qsync_workflow.md`.

The main entry points for callers are:

- `qsync.cli.main`  → console script (`qsync`) with `init`, `preview`, `apply`.
- `qsync.sync_core` → Python helpers for orchestration.
- `qsync.qualtrics_client` → low-level Qualtrics API + survey JSON cache helpers.
- `qsync.excel_io` → Excel read/write utilities.
- `qsync.markdown_codec` → small HTML ⇄ Markdown codec.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

__all__ = [
    "qualtrics_client",
    "excel_io",
    "markdown_codec",
    "sync_core",
]

try:
    __version__ = version("qsync")
except PackageNotFoundError:  # pragma: no cover
    try:
        __version__ = version("newsflows-qsync")
    except PackageNotFoundError:  # pragma: no cover
        __version__ = "0.0.0"
