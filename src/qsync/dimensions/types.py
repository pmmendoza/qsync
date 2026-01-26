from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Set


@dataclass
class DimensionChanges:
    """Detected changes for a dimension."""

    dimension: str
    has_changes: bool
    change_summary: str
    affected_qids: Set[str]
    error_detail: Optional[str] = None
    safe_to_autofix: bool = False
