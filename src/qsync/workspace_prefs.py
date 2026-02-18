"""Workspace-local preferences stored under `.qsync/`.

This module is intentionally small and dependency-light so it can be used
early in CLI startup (before importing modules that compute account-scoped
paths at import time).

Current preferences include:
- `active_account` (managed via `qsync account use|clear`)
- `survey_cache_subdir` (optional cache folder name under `surveys/`, e.g. `caches`)
- `items_allow_externally_managed_qids` (optional QID override tokens for items sync)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import QsyncConfigError

_STATE_DIRNAME = ".qsync"
_PREFS_FILENAME = "preferences.json"
_ACTIVE_ACCOUNT_KEY = "active_account"
_SURVEY_CACHE_SUBDIR_KEY = "survey_cache_subdir"
_ITEMS_ALLOW_EXTERNALLY_MANAGED_QIDS_KEY = "items_allow_externally_managed_qids"


def state_dir(root: Path) -> Path:
    return (root / _STATE_DIRNAME).resolve()


def prefs_path(root: Path) -> Path:
    return state_dir(root) / _PREFS_FILENAME


def load_prefs(root: Path) -> tuple[dict[str, Any], str | None]:
    path = prefs_path(root)
    if not path.exists():
        return {}, None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {}, f"Failed to parse {path}: {exc}"
    if not isinstance(raw, dict):
        return {}, f"Invalid preferences format in {path} (expected a JSON object)."
    return dict(raw), None


def save_prefs(root: Path, prefs: dict[str, Any]) -> None:
    sd = state_dir(root)
    sd.mkdir(parents=True, exist_ok=True)
    path = prefs_path(root)
    path.write_text(
        json.dumps(prefs, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def get_workspace_active_account(root: Path) -> str | None:
    prefs, _err = load_prefs(root)
    raw = prefs.get(_ACTIVE_ACCOUNT_KEY)
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    return value or None


def set_workspace_active_account(root: Path, account: str | None) -> None:
    prefs, err = load_prefs(root)
    if err:
        raise QsyncConfigError(
            error_id="QSYNC-CONFIG-PREFS-001",
            problem="Workspace preferences file is not valid JSON.",
            why="qsync stores workspace-local preferences under `.qsync/preferences.json`.",
            impact="qsync cannot safely update workspace preferences without risking data loss.",
            action=f"Fix or delete `{prefs_path(root)}`, then retry the command.",
            context={"prefs_path": str(prefs_path(root)), "parse_error": err},
            exit_code=1,
        )
    if account is None:
        prefs.pop(_ACTIVE_ACCOUNT_KEY, None)
    else:
        prefs[_ACTIVE_ACCOUNT_KEY] = str(account)
    save_prefs(root, prefs)


def get_workspace_survey_cache_subdir(root: Path) -> str | None:
    prefs, _err = load_prefs(root)
    raw = prefs.get(_SURVEY_CACHE_SUBDIR_KEY)
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    return value or None


def set_workspace_survey_cache_subdir(root: Path, subdir: str | None) -> None:
    prefs, err = load_prefs(root)
    if err:
        raise QsyncConfigError(
            error_id="QSYNC-CONFIG-PREFS-002",
            problem="Workspace preferences file is not valid JSON.",
            why="qsync stores workspace-local preferences under `.qsync/preferences.json`.",
            impact="qsync cannot safely update workspace preferences without risking data loss.",
            action=f"Fix or delete `{prefs_path(root)}`, then retry the command.",
            context={"prefs_path": str(prefs_path(root)), "parse_error": err},
            exit_code=1,
        )
    if subdir is None:
        prefs.pop(_SURVEY_CACHE_SUBDIR_KEY, None)
    else:
        prefs[_SURVEY_CACHE_SUBDIR_KEY] = str(subdir)
    save_prefs(root, prefs)


def get_workspace_items_allow_externally_managed_qids(root: Path) -> str | None:
    prefs, _err = load_prefs(root)
    raw = prefs.get(_ITEMS_ALLOW_EXTERNALLY_MANAGED_QIDS_KEY)
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    return value or None


def set_workspace_items_allow_externally_managed_qids(
    root: Path, value: str | None
) -> None:
    prefs, err = load_prefs(root)
    if err:
        raise QsyncConfigError(
            error_id="QSYNC-CONFIG-PREFS-003",
            problem="Workspace preferences file is not valid JSON.",
            why="qsync stores workspace-local preferences under `.qsync/preferences.json`.",
            impact="qsync cannot safely update workspace preferences without risking data loss.",
            action=f"Fix or delete `{prefs_path(root)}`, then retry the command.",
            context={"prefs_path": str(prefs_path(root)), "parse_error": err},
            exit_code=1,
        )
    if value is None:
        prefs.pop(_ITEMS_ALLOW_EXTERNALLY_MANAGED_QIDS_KEY, None)
    else:
        cleaned = str(value).strip()
        if cleaned:
            prefs[_ITEMS_ALLOW_EXTERNALLY_MANAGED_QIDS_KEY] = cleaned
        else:
            prefs.pop(_ITEMS_ALLOW_EXTERNALLY_MANAGED_QIDS_KEY, None)
    save_prefs(root, prefs)
