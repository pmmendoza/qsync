from __future__ import annotations

import re
from pathlib import Path

from .config import resolve_root, resolve_scoped_dir
from .translations_utils import normalize_language_code

_SNAPSHOT_DIRNAME = "translation_key_snapshots"


def workspace_root() -> Path:
    return resolve_root(required=False) or Path.cwd()


def translation_key_snapshot_root(root: Path | None = None) -> Path:
    base = root or workspace_root()
    surveys_dir = resolve_scoped_dir("surveys", root=base)
    return surveys_dir / _SNAPSHOT_DIRNAME


def translation_key_snapshot_path(
    survey_id: str, label: str, language: str, root: Path | None = None
) -> Path:
    lang = normalize_language_code(language)
    safe_label = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(label).strip()) or "snapshot"
    return (
        translation_key_snapshot_root(root)
        / str(survey_id).strip()
        / (f"{safe_label}_{lang}.json")
    )
