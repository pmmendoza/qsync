"""Shared helpers for consistent unified diff generation across dimensions."""

from __future__ import annotations

import difflib


def unified_diff_lines(
    old_text: str,
    new_text: str,
    *,
    fromfile: str,
    tofile: str,
) -> list[str]:
    """Generate unified diff lines between two text payloads."""
    return list(
        difflib.unified_diff(
            (old_text or "").splitlines(),
            (new_text or "").splitlines(),
            fromfile=fromfile,
            tofile=tofile,
            lineterm="",
        )
    )


def contextual_diff_lines(
    old_text: str,
    new_text: str,
    *,
    context: str | None = None,
    fromfile_base: str = "cached",
    tofile_base: str = "excel",
) -> list[str]:
    """Generate unified diff lines with optional context-aware labels."""
    fromfile = fromfile_base
    tofile = tofile_base
    if context:
        fromfile = f"{fromfile_base} ({context})"
        tofile = f"{tofile_base} ({context})"
    return unified_diff_lines(
        old_text,
        new_text,
        fromfile=fromfile,
        tofile=tofile,
    )
