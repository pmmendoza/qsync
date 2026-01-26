#!/usr/bin/env python3
"""Preview differences between local survey_js files and cached QuestionJS."""

from __future__ import annotations

# Legacy import location. Canonical implementation now lives in qsync.dimensions.
from .dimensions.js_preview import main as _main
from .dimensions.js_preview import *  # noqa: F401,F403

if __name__ == "__main__":
    _main()
