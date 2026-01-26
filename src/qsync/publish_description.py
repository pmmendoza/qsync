"""Helpers for generating Qualtrics version descriptions within length limits."""

from __future__ import annotations

from typing import Iterable

ELLIPSIS = "…"


def truncate_with_ellipsis(text: str, max_chars: int) -> str:
    """Truncate text to max_chars, appending an ellipsis when needed."""
    if max_chars <= 0:
        return ""
    text = text or ""
    if len(text) <= max_chars:
        return text
    if max_chars == 1:
        return ELLIPSIS
    return text[: max_chars - 1] + ELLIPSIS


def _format_qid_suffix(qids: list[str], max_chars: int) -> str:
    """Format a bracketed QID suffix like `[QID1,QID2,+3]` within max_chars."""

    if max_chars < 4:  # "[]"+ at least 2 chars inside
        return ""
    if not qids:
        return ""

    uniq = []
    seen = set()
    for qid in qids:
        q = (qid or "").strip()
        if not q or q in seen:
            continue
        seen.add(q)
        uniq.append(q)

    if not uniq:
        return ""

    inner_max = max_chars - 2
    inner = ""
    included = 0
    for qid in uniq:
        token = qid if included == 0 else f",{qid}"
        if len(inner) + len(token) <= inner_max:
            inner += token
            included += 1
            continue
        break

    remaining = len(uniq) - included
    if remaining > 0:
        tail = f"+{remaining}" if included == 0 else f",+{remaining}"
        if len(tail) > inner_max:
            return ""
        if len(inner) + len(tail) > inner_max:
            allowed = inner_max - len(tail)
            inner = inner[:allowed].rstrip(",")
            if not inner and tail.startswith(","):
                tail = tail[1:]
        inner = inner + tail

    return f"[{inner}]"


def make_publish_description(
    *,
    operation: str,
    changed_qids: Iterable[str] | None = None,
    count: int | None = None,
    label: str | None = None,
    max_chars: int = 140,
) -> str:
    """Build a consistent publish description with a hard length cap.

    MVP baseline: count-based description; optionally adds a compact QID list
    when there is remaining space.
    """

    op = (operation or "").strip()
    if not op:
        op = "publish"

    qids_list = list(changed_qids) if changed_qids else []
    n = count if count is not None else (len(qids_list) if qids_list else 0)

    base = f"qsync {op}: {n} question(s)"

    text = base
    if label:
        lab = (label or "").strip()
        if lab:
            overhead = 3  # " (" + ")"
            max_label = max_chars - len(text) - overhead
            if max_label > 0:
                text = f"{text} ({truncate_with_ellipsis(lab, max_label)})"

    if qids_list:
        # Reserve a leading space before the suffix.
        remaining = max_chars - len(text) - 1
        if remaining >= 4:
            suffix = _format_qid_suffix(qids_list, remaining)
            if suffix:
                text = f"{text} {suffix}"

    return truncate_with_ellipsis(text, max_chars)
