from __future__ import annotations

from qsync.rich_support import canonical_text_progress_bar, format_step_progress


def test_canonical_text_progress_bar_formats_percentage_and_counts() -> None:
    bar = canonical_text_progress_bar(2, 5, width=10)
    assert bar == "[####------]  40% (2/5)"


def test_canonical_text_progress_bar_handles_zero_total() -> None:
    bar = canonical_text_progress_bar(3, 0, width=6)
    assert bar == "[------]   0% (0/0)"


def test_format_step_progress_includes_remaining_steps() -> None:
    text = format_step_progress(2, 5, "Syncing surveys", width=8)
    assert text.startswith("Step 2/5 [###-----]  40% (2/5) | 3 steps left | ")
    assert text.endswith("Syncing surveys")


def test_format_step_progress_without_total_falls_back_to_simple_step() -> None:
    text = format_step_progress(1, 0, "Preparing")
    assert text == "Step 1: Preparing"
