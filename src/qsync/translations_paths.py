from __future__ import annotations

import re
import shutil
from pathlib import Path

from .config import (
    WORKSPACE_LAYOUT_ACCOUNT_ROOT_V1,
    resolve_root,
    resolve_scoped_dir,
    resolve_workspace_layout,
)
from .survey_naming import resolve_survey_path, survey_named_candidate_paths
from .translations_utils import normalize_language_code

TRANSLATIONS_DIRNAME_LEGACY = "qualtrics_survey_translations"
TRANSLATIONS_DIRNAME_CANONICAL = "translations"
TRANSLATIONS_KEYS_DIRNAME = "key_snapshots"
TRANSLATIONS_KEYS_DIRNAME_LEGACY = "translation_key_snapshots"


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


def _legacy_translation_keys_root(root: Path | None = None) -> Path:
    base = root or workspace_root()
    surveys_dir = resolve_scoped_dir("surveys", root=base)
    return surveys_dir / TRANSLATIONS_KEYS_DIRNAME_LEGACY


def _legacy_translation_snapshot_candidates(
    survey_id: str, filename: str, *, root: Path | None = None
) -> list[Path]:
    base_root = root or workspace_root()
    legacy_root = _legacy_translation_keys_root(base_root)
    candidates: list[Path] = []
    seen: set[Path] = set()

    for survey_dir in survey_named_candidate_paths(
        legacy_root,
        survey_id,
        is_dir=True,
        root=base_root,
    ):
        candidate = survey_dir / filename
        if candidate in seen:
            continue
        seen.add(candidate)
        candidates.append(candidate)
    return candidates


def _prune_empty_ancestors(start: Path, *, stop: Path) -> None:
    current = start.resolve()
    stop = stop.resolve()
    while current != stop and current.exists() and current.is_dir():
        if any(current.iterdir()):
            break
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def translation_key_snapshot_path(
    survey_id: str, label: str, language: str, root: Path | None = None
) -> Path:
    base_root = root or workspace_root()
    lang = normalize_language_code(language)
    safe_label = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(label).strip()) or "snapshot"
    filename = f"{safe_label}_{lang}.json"
    canonical = translation_keys_dir(survey_id, base_root) / filename
    if canonical.exists():
        return canonical

    legacy_root = _legacy_translation_keys_root(base_root)
    for legacy in _legacy_translation_snapshot_candidates(
        survey_id, filename, root=base_root
    ):
        if not legacy.exists() or not legacy.is_file():
            continue
        try:
            canonical.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy), str(canonical))
            _prune_empty_ancestors(legacy.parent, stop=legacy_root)
            return canonical
        except OSError:
            # Keep reads/writes working even if migration cannot be completed now.
            return legacy
    return canonical
