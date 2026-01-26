"""Lookup helpers for the local Qualtrics survey inventory CSV."""

from __future__ import annotations

import csv
from typing import Dict, List


class SurveyInventoryError(RuntimeError):
    """Raised when the local inventory cache is missing or malformed."""


def _load_inventory() -> Dict:
    from .survey_inventory import resolve_inventory_csv_path

    path = resolve_inventory_csv_path(required=False)
    if not path.exists():
        raise SurveyInventoryError(
            "Missing surveys/inventory.csv (legacy: surveys/qualtrics_surveys.csv). "
            "Run 'qsync survey inventory' first."
        )
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(
                line for line in fh if not line.lstrip().startswith("#")
            )
            return list(reader)
    except Exception as exc:
        raise SurveyInventoryError(
            f"Failed to parse {path}: {exc}. Delete/refresh the file and rerun the inventory script."
        ) from exc


def list_surveys() -> List[Dict]:
    """Return the full local survey inventory as a list of dict rows."""

    return _load_inventory()


def surveys_by_name(name: str) -> List[Dict]:
    """Return inventory rows whose `name` matches exactly."""

    if not name:
        return []
    name = name.strip()
    return [survey for survey in list_surveys() if (survey.get("name") or "") == name]


def ensure_unique_survey_name(name: str, *, allow_duplicate: bool = False) -> None:
    """Abort if a survey with the same name already exists and duplicates aren't allowed."""

    matches = surveys_by_name(name)
    if not matches or allow_duplicate:
        return

    ids = ", ".join(sorted(filter(None, (m.get("id") for m in matches))))
    created = ", ".join(
        f"{m.get('id')} (created {m.get('creationDate')})"
        for m in matches
        if m.get("id")
    )
    raise RuntimeError(
        f"Survey name '{name}' already exists for SurveyID(s): {ids}. "
        "Specify --survey-id to update an existing survey or rerun with --force-duplicate "
        "if you intentionally need another survey with the same name."
        + (f" Existing copies: {created}." if created else "")
    )
