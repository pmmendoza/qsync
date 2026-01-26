"""Helpers for rendering survey identifiers in user-facing output.

Goal: Prefer showing "{SurveyName} ({SurveyID})" instead of only "SV_...".
Falls back to the SurveyID if the name is unavailable.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from .survey_inventory import load_inventory_record


@lru_cache(maxsize=2048)
def _inventory_name_for_survey_id(survey_id: str) -> str | None:
    sid = (survey_id or "").strip()
    if not sid:
        return None
    try:
        record = load_inventory_record(sid)
    except Exception:
        record = None
    name = str((record or {}).get("name") or "").strip()
    if not name:
        return None
    return name


def format_survey_ref(survey_id: str, survey_name: Optional[str] = None) -> str:
    """Format a survey reference for terminal output.

    Returns:
        - "{survey_name} ({survey_id})" when a name is known
        - "{survey_id}" as fallback
    """
    sid = (survey_id or "").strip()
    if not sid:
        return ""

    name = str(survey_name or "").strip() or (_inventory_name_for_survey_id(sid) or "")
    if name and name != sid:
        return f"{name} ({sid})"
    return sid
