from __future__ import annotations

import re
from pathlib import Path

from .config import (
    WORKSPACE_LAYOUT_ACCOUNT_ROOT_V1,
    resolve_root,
    resolve_scoped_dir,
    resolve_workspace_layout,
)
from .survey_naming import resolve_survey_path
from .translations_utils import normalize_language_code

TRANSLATIONS_DIRNAME_LEGACY = "qualtrics_survey_translations"
TRANSLATIONS_DIRNAME_CANONICAL = "translations"
TRANSLATIONS_KEYS_DIRNAME = "key_snapshots"


def workspace_root() -> Path:
    return resolve_root(required=False) or Path.cwd()


def translations_root(root: Path | None = None) -> Path:
    base = root or workspace_root()
    layout = resolve_workspace_layout(root=base)
    if layout == WORKSPACE_LAYOUT_ACCOUNT_ROOT_V1:
        surveys_dir = resolve_scoped_dir("surveys", root=base)
        return surveys_dir / TRANSLATIONS_DIRNAME_CANONICAL
    contents_dir = resolve_scoped_dir("contents", root=base)
    return contents_dir / TRANSLATIONS_DIRNAME_LEGACY


def translation_dir(survey_id: str, root: Path | None = None) -> Path:
    base_root = root or workspace_root()
    return resolve_survey_path(
        translations_root(base_root),
        survey_id,
        is_dir=True,
        root=base_root,
        prefer_existing=True,
        migrate_existing=True,
    )


def translation_map_path(
    survey_id: str, language: str, root: Path | None = None
) -> Path:
    lang = normalize_language_code(language)
    return translation_dir(survey_id, root) / f"{lang}.json"


def translation_keys_dir(survey_id: str, root: Path | None = None) -> Path:
    return translation_dir(survey_id, root) / TRANSLATIONS_KEYS_DIRNAME


def translation_key_snapshot_path(
    survey_id: str, label: str, language: str, root: Path | None = None
) -> Path:
    lang = normalize_language_code(language)
    safe_label = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(label).strip()) or "snapshot"
    return translation_keys_dir(survey_id, root) / f"{safe_label}_{lang}.json"
