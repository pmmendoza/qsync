from __future__ import annotations

import re
from pathlib import Path


def test_no_new_raise_runtimeerror_introduced() -> None:
    """
    Guardrail: discourage adding new `raise RuntimeError(...)` sites in qsync.

    This intentionally allows reducing/removing RuntimeError usage over time.
    """

    qsync_dir = Path("src") / "qsync"
    assert qsync_dir.exists(), f"Expected qsync source dir at {qsync_dir}"

    pattern = re.compile(r"\braise\s+RuntimeError\s*\(")
    count = 0
    for path in qsync_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        count += len(pattern.findall(text))

    # Baseline after account-scoped/workflow hardening changes (2026-02-18).
    baseline_max = 64
    assert (
        count <= baseline_max
    ), f"Found {count} `raise RuntimeError(...)` sites under {qsync_dir} (max allowed: {baseline_max})."
