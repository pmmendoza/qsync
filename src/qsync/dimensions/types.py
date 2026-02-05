from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Set, Literal


@dataclass
class DimensionChanges:
    """Detected changes for a dimension."""

    dimension: str
    has_changes: bool
    change_summary: str
    affected_qids: Set[str]
    error_detail: Optional[str] = None
    warning_detail: Optional[str] = None
    safe_to_autofix: bool = False
    status_kind: Literal["none", "unstaged", "staged", "error"] = "none"
    edit_count: int = 0
