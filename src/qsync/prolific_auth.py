"""Helpers for managing Prolific authenticity checks in Qualtrics."""

from __future__ import annotations

import re
from dataclasses import dataclass


PROLIFIC_QUALTRICS_SCRIPT_URL = (
    "https://assets.prolific.com/assets/js/qualtrics/qualtrics.min.js"
)

_RID_MARKER = "rid=${e://Field/ResponseID}"


@dataclass(frozen=True)
class ProlificSnippetValidation:
    ok: bool
    errors: list[str]
    warnings: list[str]


def normalize_html_snippet(value: str) -> str:
    if value is None:
        return ""
    # Strip BOM + surrounding whitespace; keep internal newlines as-is.
    value = value.replace("\ufeff", "")
    return value.strip()


def contains_prolific_qualtrics_script(value: str | None) -> bool:
    if not value:
        return False
    return PROLIFIC_QUALTRICS_SCRIPT_URL in value


def redact_prolific_token(value: str) -> str:
    """Redact the Prolific `t=` token for safe previews."""
    if not value:
        return value
    # Replace t=<...> up to an & or quote/space/end.
    return re.sub(r"(\bt=)([^&\"'\s>]+)", r"\1[REDACTED]", value)


def excerpt(value: str, *, max_chars: int = 260) -> str:
    value = value or ""
    compact = value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ")
    compact = re.sub(r"\s+", " ", compact).strip()
    if len(compact) <= max_chars:
        return compact
    return compact[: max(0, max_chars - 1)] + "…"


def validate_prolific_auth_snippet(value: str) -> ProlificSnippetValidation:
    """Best-effort validation for the Prolific Qualtrics authenticity header snippet."""
    snippet = normalize_html_snippet(value)
    errors: list[str] = []
    warnings: list[str] = []

    if not snippet:
        errors.append("Snippet is empty.")
        return ProlificSnippetValidation(ok=False, errors=errors, warnings=warnings)

    if PROLIFIC_QUALTRICS_SCRIPT_URL not in snippet:
        errors.append(
            "Missing Prolific Qualtrics script URL "
            f"({PROLIFIC_QUALTRICS_SCRIPT_URL})."
        )

    if _RID_MARKER not in snippet:
        warnings.append(f"Missing expected ResponseID marker ({_RID_MARKER}).")

    if "t=" not in snippet:
        warnings.append("Missing expected `t=` query parameter.")

    # Script tag sanity checks (not strict HTML parsing; just guardrails).
    if "<script" not in snippet.lower():
        warnings.append("Snippet does not include a `<script ...>` tag.")
    if "</script>" not in snippet.lower():
        warnings.append("Snippet is missing a closing `</script>` tag.")

    return ProlificSnippetValidation(ok=not errors, errors=errors, warnings=warnings)


def merge_header(existing: str | None, new: str, *, mode: str) -> str:
    """Merge a snippet into an existing header.

    mode:
      - "replace": return only `new`
      - "append": append `new` to existing (newline-separated)
    """
    new_snippet = normalize_html_snippet(new)
    existing_text = existing or ""

    if mode == "replace":
        return new_snippet
    if mode == "append":
        if not existing_text.strip():
            return new_snippet
        if new_snippet and new_snippet in existing_text:
            return existing_text
        left = existing_text.rstrip()
        right = new_snippet.lstrip()
        return f"{left}\n{right}"
    raise ValueError(f"Unknown mode: {mode}")
