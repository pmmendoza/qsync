"""Centralized error guidance for qsync logging."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_ERROR_DOCS_URL_CANDIDATES = (
    "docs/troubleshooting.md",
    "packages/qsync/docs/troubleshooting.md",
)

_STATUS_SUGGESTIONS: dict[int, str] = {
    400: "Check the request payload/fields; re-run with corrected input.",
    401: "Verify QUALTRICS_API_KEY / credentials and re-run `qsync doctor`.",
    403: "Check account permissions for this survey/resource.",
    404: "Verify the survey ID or endpoint; refresh inventory if needed.",
    409: "Conflict detected; refresh state (inventory/master pull) and retry.",
    422: "Validation error; inspect field values and required parameters.",
    429: "Rate limited; wait and retry (consider reducing parallel requests).",
    500: "Server error on Qualtrics; retry later.",
    502: "Upstream error; retry later.",
    503: "Service unavailable; retry later.",
    504: "Gateway timeout; retry later.",
}

_RECOVERABLE_STATUSES = {429, 500, 502, 503, 504}
_RECOVERABLE_EXCEPTIONS = {
    "Timeout",
    "ReadTimeout",
    "ConnectTimeout",
    "ConnectionError",
}


def get_docs_url(*_args: Any, **_kwargs: Any) -> str:
    override = (os.environ.get("QSYNC_DOCS_URL") or "").strip()
    if override:
        return override

    for candidate in _ERROR_DOCS_URL_CANDIDATES:
        try:
            if Path(candidate).exists():
                return candidate
        except Exception:
            continue

    return _ERROR_DOCS_URL_CANDIDATES[0]


def get_suggestion(status: int | None, *, exc_type: str | None = None) -> str:
    if status is not None:
        return _STATUS_SUGGESTIONS.get(status) or "Inspect logs and retry."
    if exc_type in {"ValueError", "KeyError"}:
        return "Check input values and retry."
    return "Retry the operation; check network connectivity and credentials."


def is_recoverable(status: int | None, *, exc_type: str | None = None) -> bool:
    if status is not None:
        return status in _RECOVERABLE_STATUSES
    return exc_type in _RECOVERABLE_EXCEPTIONS
