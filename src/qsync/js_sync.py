"""Synchronise cached QuestionJS with local survey_js/core files."""

from __future__ import annotations

# Legacy import location. Canonical implementation now lives in qsync.dimensions.
from .dimensions.js_sync import main as _main
from .dimensions.js_sync import *  # noqa: F401,F403

if __name__ == "__main__":
    _main()
