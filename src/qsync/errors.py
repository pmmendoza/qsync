"""Structured error types for qsync CLI and library code.

Goal: keep user-facing errors consistent and actionable while retaining enough
structure (error_id, context, docs link) for logging and future tooling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


def _format_kv(context: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for key in sorted(context.keys(), key=str):
        value = context.get(key)
        if value is None:
            continue
        lines.append(f"  - {key}: {value}")
    return lines


@dataclass
class QsyncError(RuntimeError):
    """A structured, user-facing error suitable for CLI output and logs."""

    error_id: str
    problem: str
    why: str | None = None
    impact: str | None = None
    action: str | None = None
    context: Mapping[str, Any] = field(default_factory=dict)
    docs_url: str | None = None
    exit_code: int = 2

    def to_lines(self) -> list[str]:
        # Short-first summary line
        lines: list[str] = [f"{self.error_id}: {self.problem}"]

        if self.why:
            lines.append("Why it happened:")
            lines.append(f"  {self.why}")
        if self.impact:
            lines.append("Impact:")
            lines.append(f"  {self.impact}")
        if self.action:
            lines.append("How to fix:")
            lines.append(f"  {self.action}")
        if self.context:
            lines.append("Context:")
            lines.extend(_format_kv(self.context))
        if self.docs_url:
            lines.append(f"Docs: {self.docs_url}")
        return lines

    def to_log_error(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error_id": self.error_id,
            "problem": self.problem,
        }
        if self.why:
            payload["why"] = self.why
        if self.impact:
            payload["impact"] = self.impact
        if self.action:
            payload["action"] = self.action
        if self.docs_url:
            payload["docs_url"] = self.docs_url
        if self.context:
            payload["context"] = dict(self.context)
        return payload

    def __str__(self) -> str:  # pragma: no cover (formatting covered by to_lines)
        return "\n".join(self.to_lines())


class QsyncConfigError(QsyncError):
    """Configuration/credential problems (missing base URL/token/etc.)."""


class QsyncValidationError(QsyncError):
    """User input / workspace validation failures (files, workbook, args)."""


class QsyncStateError(QsyncError):
    """Unexpected local state (corrupt cache/pending files) that user can fix."""
