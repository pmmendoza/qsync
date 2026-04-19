"""Helpers for Qualtrics survey response exports.

The Qualtrics response export API supports `csv`, `tsv`, `spss`, `json`,
`ndjson`, and `xml` for the export formats used by `qsync`.

One API wrinkle matters here: Qualtrics rejects several tabular-export options
for `json` and `ndjson`. This module keeps that rule in one place so the CLI can
offer every supported format while still sending a valid start-export payload.
"""

from __future__ import annotations

from typing import Any

DEFAULT_RESPONSE_EXPORT_FORMAT = "csv"
SUPPORTED_RESPONSE_EXPORT_FORMATS = (
    "csv",
    "tsv",
    "spss",
    "json",
    "ndjson",
    "xml",
)
JSON_RESPONSE_EXPORT_FORMATS = frozenset({"json", "ndjson"})


def normalize_response_export_format(value: str | None) -> str:
    """Return a validated lower-case response export format.

    The CLI accepts mixed-case input, but the Qualtrics API expects the lower-
    case wire values.
    """

    export_format = (value or DEFAULT_RESPONSE_EXPORT_FORMAT).strip().lower()
    if export_format not in SUPPORTED_RESPONSE_EXPORT_FORMATS:
        allowed = ", ".join(SUPPORTED_RESPONSE_EXPORT_FORMATS)
        raise ValueError(
            f"Unsupported response export format '{value}'. Choose one of: {allowed}."
        )
    return export_format


def build_response_export_payload(*, export_format: str) -> dict[str, Any]:
    """Build a start-export payload valid for the requested Qualtrics format.

    For tabular formats we keep the historical qsync defaults:
    - `useLabels=True`
    - `seenUnansweredRecode=999`
    - `timeZone="UTC"`

    Qualtrics documents those options as invalid for `json` and `ndjson`, so
    those formats get a minimal payload containing only the requested format.
    """

    normalized = normalize_response_export_format(export_format)
    payload: dict[str, Any] = {"format": normalized}
    if normalized not in JSON_RESPONSE_EXPORT_FORMATS:
        payload.update(
            {
                "useLabels": True,
                "seenUnansweredRecode": 999,
                "timeZone": "UTC",
            }
        )
    return payload
