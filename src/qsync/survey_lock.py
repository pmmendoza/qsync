"""Enforce per-survey API push locks based on the inventory CSV."""

from __future__ import annotations

import csv
import os
from typing import Dict

_LOCK_CACHE: Dict[str, bool] | None = None
_NAME_CACHE: Dict[str, str] | None = None
_CACHE_MTIME: float | None = None
_CACHE_PATH: str | None = None

ERROR_ID_SURVEY_LOCKED = "QSYNC-LOCKED-SURVEY-001"


class SurveyLockedError(RuntimeError):
    """Raised when a survey is blocked by the workspace lock policy."""


def _allow_locked_override_enabled() -> bool:
    raw = os.environ.get("QSYNC_ALLOW_LOCKED")
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def _refresh_cache() -> None:
    global _LOCK_CACHE, _NAME_CACHE, _CACHE_MTIME, _CACHE_PATH
    from .survey_inventory import resolve_inventory_csv_path

    inventory_path = resolve_inventory_csv_path(required=False)
    try:
        mtime = inventory_path.stat().st_mtime
    except FileNotFoundError:
        _LOCK_CACHE = {}
        _NAME_CACHE = {}
        _CACHE_MTIME = None
        _CACHE_PATH = None
        return

    if (
        _CACHE_MTIME == mtime
        and _CACHE_PATH == str(inventory_path)
        and _LOCK_CACHE is not None
        and _NAME_CACHE is not None
    ):
        return

    lock_map: Dict[str, bool] = {}
    name_map: Dict[str, str] = {}
    true_values = {"true", "1", "yes", "y", "t"}

    try:
        with inventory_path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(
                line for line in fh if not line.lstrip().startswith("#")
            )
            for row in reader:
                sid = (row.get("id") or "").strip()
                if not sid:
                    continue
                name_map[sid] = row.get("name") or sid
                locked_val = row.get("locked") or ""
                if locked_val.strip().lower() in true_values:
                    lock_map[sid] = True
    except Exception:
        lock_map = {}
        name_map = {}

    _LOCK_CACHE = lock_map
    _NAME_CACHE = name_map
    _CACHE_MTIME = mtime
    _CACHE_PATH = str(inventory_path)


def _get_lock_map() -> Dict[str, bool]:
    _refresh_cache()
    return _LOCK_CACHE or {}


def is_locked(survey_id: str) -> bool:
    """Return True if a survey is marked locked in the inventory CSV."""

    return _get_lock_map().get(survey_id, False)


def ensure_unlocked(survey_id: str) -> None:
    """Raise if `survey_id` is locked for API editing."""

    if not survey_id:
        return
    if _allow_locked_override_enabled():
        return
    if not is_locked(survey_id):
        return
    _refresh_cache()
    name = (_NAME_CACHE or {}).get(survey_id) or survey_id

    message = (
        f"{ERROR_ID_SURVEY_LOCKED}: Survey '{name}' ({survey_id}) is locked for API editing.\n"
        "Why this happens:\n"
        "  - This workspace uses surveys/inventory.csv (legacy: surveys/qualtrics_surveys.csv) as a safety lock "
        "to prevent accidental API edits.\n"
        "  - Locked surveys are typically live, sensitive, or otherwise unsafe to modify without explicit review.\n"
        "Risk:\n"
        "  - Editing a locked survey can invalidate active data collection, break participant flow, or create hard-to-debug version drift.\n"
        "How to proceed:\n"
        "  1. Confirm the survey is safe to edit (no active collection / downstream dependencies).\n"
        "  2. Unlock it in surveys/inventory.csv (set locked=FALSE) and retry.\n"
        "Emergency override:\n"
        "  - Re-run the command with --allow-locked to bypass this local lock check (dangerous).\n"
    )
    raise SurveyLockedError(message)
