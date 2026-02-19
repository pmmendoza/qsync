"""Persist and load pending qsync pushes under `surveys/pending/`.

DEPRECATED: This module is deprecated. Use `pending_stage.py` with unified schema instead.
Legacy support maintained for backward compatibility only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import resolve_root, resolve_scoped_dir
from .survey_naming import resolve_survey_path, survey_named_candidate_paths

ROOT = resolve_root(required=False) or Path.cwd()
SURVEYS_DIR = resolve_scoped_dir("surveys", root=ROOT)
PENDING_DIR = SURVEYS_DIR / "pending"
PENDING_JS_DIR = PENDING_DIR / "js"


@dataclass
class PendingPushRecord:
    """Pending Excel-based push state for a survey (QIDs and workbook context)."""

    survey_id: str
    qids: list[str]
    embedded_fields: list[dict[str, str]] | None = None
    workbook: str | None = None
    filter_column: str | None = None
    filter_value: str | None = None
    created_at: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingPushRecord":
        """Parse a `PendingPushRecord` from JSON/dict payload."""

        return cls(
            survey_id=data.get("survey_id", ""),
            qids=list(data.get("qids") or []),
            embedded_fields=list(data.get("embedded_fields") or []),
            workbook=data.get("workbook"),
            filter_column=data.get("filter_column"),
            filter_value=data.get("filter_value"),
            created_at=data.get("created_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert the record to a JSON-serializable dict."""

        return {
            "survey_id": self.survey_id,
            "qids": list(self.qids),
            "embedded_fields": list(self.embedded_fields or []),
            "workbook": self.workbook,
            "filter_column": self.filter_column,
            "filter_value": self.filter_value,
            "created_at": self.created_at or _now_iso(),
        }


@dataclass
class PendingJsRecord:
    """Pending JS push state for a survey (per QID mapping and statuses)."""

    survey_id: str
    entries: list[
        dict[str, str]
    ]  # each entry: {"qid":..., "js_file":..., "status":...}
    created_at: str | None = None

    @property
    def qids(self) -> list[str]:
        """Return a list of QIDs present in `entries`."""

        return [entry.get("qid") for entry in self.entries]

    def to_dict(self) -> dict[str, Any]:
        """Convert the record to a JSON-serializable dict."""

        return {
            "survey_id": self.survey_id,
            "entries": list(self.entries),
            "created_at": self.created_at or _now_iso(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingJsRecord":
        """Parse a `PendingJsRecord` from JSON/dict payload."""

        return cls(
            survey_id=data.get("survey_id", ""),
            entries=list(data.get("entries") or []),
            created_at=data.get("created_at"),
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pending_path(survey_id: str) -> Path:
    return resolve_survey_path(
        PENDING_DIR,
        survey_id,
        suffix=".json",
        is_dir=False,
        root=ROOT,
        prefer_existing=True,
        migrate_existing=False,
    )


def save_pending(record: PendingPushRecord) -> None:
    """Write the pending push record for a survey under `surveys/pending/`."""

    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    path = resolve_survey_path(
        PENDING_DIR,
        record.survey_id,
        suffix=".json",
        is_dir=False,
        root=ROOT,
        prefer_existing=False,
        migrate_existing=True,
    )
    payload = record.to_dict()
    if not payload.get("created_at"):
        payload["created_at"] = _now_iso()
    path.write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


def load_pending(survey_id: str) -> PendingPushRecord | None:
    """Load the pending push record for a survey, if present and valid."""

    path = _pending_path(survey_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    record = PendingPushRecord.from_dict(data)
    if not record.qids and not (record.embedded_fields or []):
        return None
    return record


def clear_pending(survey_id: str) -> None:
    """Remove the pending push record for a survey (best-effort)."""

    for path in survey_named_candidate_paths(
        PENDING_DIR,
        survey_id,
        suffix=".json",
        is_dir=False,
        root=ROOT,
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _pending_js_path(survey_id: str) -> Path:
    return resolve_survey_path(
        PENDING_JS_DIR,
        survey_id,
        suffix=".json",
        is_dir=False,
        root=ROOT,
        prefer_existing=True,
        migrate_existing=False,
    )


def save_js_pending(record: PendingJsRecord) -> None:
    """Write the pending JS record for a survey under `surveys/pending/js/`."""

    PENDING_JS_DIR.mkdir(parents=True, exist_ok=True)
    path = resolve_survey_path(
        PENDING_JS_DIR,
        record.survey_id,
        suffix=".json",
        is_dir=False,
        root=ROOT,
        prefer_existing=False,
        migrate_existing=True,
    )
    payload = record.to_dict()
    if not payload.get("created_at"):
        payload["created_at"] = _now_iso()
    path.write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


def load_js_pending(survey_id: str) -> PendingJsRecord | None:
    """Load the pending JS record for a survey, if present and valid."""

    path = _pending_js_path(survey_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    record = PendingJsRecord.from_dict(data)
    if not record.entries:
        return None
    return record


def clear_js_pending(survey_id: str) -> None:
    """Remove the pending JS record for a survey (best-effort)."""

    for path in survey_named_candidate_paths(
        PENDING_JS_DIR,
        survey_id,
        suffix=".json",
        is_dir=False,
        root=ROOT,
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
