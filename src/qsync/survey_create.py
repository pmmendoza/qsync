"""Shared helpers for creating/importing surveys via QSF upload."""

from __future__ import annotations

import copy
import json
from importlib import resources
from pathlib import Path
from typing import Any


MINIMAL_QSF_RESOURCE = "minimal_survey.qsf.json"


def load_minimal_qsf() -> dict[str, Any]:
    """Load the bundled minimal QSF seed used by `qsync survey create`."""

    resource = resources.files("qsync.resources").joinpath(MINIMAL_QSF_RESOURCE)
    return json.loads(resource.read_text(encoding="utf-8"))


def load_qsf_file(path: Path) -> dict[str, Any]:
    """Load a local QSF JSON file."""

    return json.loads(path.read_text(encoding="utf-8"))


def clone_qsf(qsf_content: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy suitable for mutation before upload."""

    return copy.deepcopy(qsf_content)


def prepare_qsf_for_import(
    qsf_content: dict[str, Any],
    new_name: str,
    *,
    language: str | None = None,
    status: str = "Inactive",
) -> dict[str, Any]:
    """Prepare QSF content for import by rewriting import-time metadata."""

    entry = qsf_content.get("SurveyEntry")
    if not isinstance(entry, dict):
        return qsf_content

    resolved_language = language or entry.get("SurveyLanguage") or "EN"
    entry["SurveyName"] = new_name
    entry["SurveyStatus"] = status
    entry["SurveyLanguage"] = resolved_language
    entry.pop("SurveyID", None)

    return qsf_content


def apply_project_category(qsf_content: dict[str, Any], project_category: str) -> None:
    """Set the QSF project category when a PROJ element exists."""

    elements = qsf_content.get("SurveyElements")
    if not isinstance(elements, list):
        return
    for elem in elements:
        if not isinstance(elem, dict) or elem.get("Element") != "PROJ":
            continue
        payload = elem.get("Payload")
        if isinstance(payload, dict):
            payload["ProjectCategory"] = project_category
        elem["PrimaryAttribute"] = project_category
