"""Push cached QuestionJS content to Qualtrics."""

from __future__ import annotations

# Legacy import location. Canonical implementation now lives in qsync.dimensions.
from .dimensions.js_push import main as _main
from .dimensions.js_push import *  # noqa: F401,F403

if __name__ == "__main__":
    _main()
