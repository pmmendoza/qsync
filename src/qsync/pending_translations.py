"""Persist and load pending translation pushes under `surveys/pending/translations/`.

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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PendingTranslationsRecord:
    survey_id: str
    languages: list[str]
    created_at: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingTranslationsRecord":
        return cls(
            survey_id=str(data.get("survey_id") or ""),
            languages=list(data.get("languages") or []),
            created_at=data.get("created_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "survey_id": self.survey_id,
            "languages": list(self.languages),
            "created_at": self.created_at or _now_iso(),
        }


def _pending_path(survey_id: str) -> Path:
    root = resolve_root(required=False) or Path.cwd()
    surveys_dir = resolve_scoped_dir("surveys", root=root)
    pending_dir = surveys_dir / "pending" / "translations"
    return resolve_survey_path(
        pending_dir,
        survey_id,
        suffix=".json",
        is_dir=False,
        root=root,
        prefer_existing=True,
        migrate_existing=False,
    )


def save_pending_translations(record: PendingTranslationsRecord) -> None:
    root = resolve_root(required=False) or Path.cwd()
    surveys_dir = resolve_scoped_dir("surveys", root=root)
    pending_dir = surveys_dir / "pending" / "translations"
    path = resolve_survey_path(
        pending_dir,
        record.survey_id,
        suffix=".json",
        is_dir=False,
        root=root,
        prefer_existing=False,
        migrate_existing=True,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = record.to_dict()
    if not payload.get("created_at"):
        payload["created_at"] = _now_iso()
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_pending_translations(survey_id: str) -> PendingTranslationsRecord | None:
    path = _pending_path(survey_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    record = PendingTranslationsRecord.from_dict(data)
    if not record.languages:
        return None
    return record


def clear_pending_translations(survey_id: str) -> None:
    root = resolve_root(required=False) or Path.cwd()
    surveys_dir = resolve_scoped_dir("surveys", root=root)
    pending_dir = surveys_dir / "pending" / "translations"
    for path in survey_named_candidate_paths(
        pending_dir,
        survey_id,
        suffix=".json",
        is_dir=False,
        root=root,
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
