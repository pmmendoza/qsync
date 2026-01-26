from __future__ import annotations

import re
from pathlib import Path

from .config import resolve_root
from .translations_utils import normalize_language_code

TRANSLATIONS_DIR = Path("contents") / "qualtrics_survey_translations"
TRANSLATIONS_KEYS_DIRNAME = "key_snapshots"


def workspace_root() -> Path:
    return resolve_root(required=False) or Path.cwd()


def translations_root(root: Path | None = None) -> Path:
    base = root or workspace_root()
    return base / TRANSLATIONS_DIR


def translation_dir(survey_id: str, root: Path | None = None) -> Path:
    return translations_root(root) / str(survey_id).strip()


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
