"""Pytest bootstrap helpers for qsync tests.

Ensures tests import the in-repo `src/qsync` package instead of any other
installed/worktree copy present on PYTHONPATH.
"""

from __future__ import annotations

import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_SRC = (_ROOT / "src").resolve()

if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

