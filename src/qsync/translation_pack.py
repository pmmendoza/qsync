"""Build a translator-facing pack (docx + cached translations + context)."""

from __future__ import annotations

import json
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .config import resolve_root, resolve_scoped_dir
from .errors import QsyncValidationError
from .qualtrics_client import load_cached_survey, refresh_survey_cache
from .translation_export import (
    export_survey_to_word,
    build_translation_map_from_cache,
    _preflight_cache_freshness,
)
from .dimensions.translations_language_blocks import (
    get_base_language,
    list_enabled_languages,
)
from .translations_utils import normalize_language_list

EXPORT_DIRNAME = "export"
PACK_SUBDIR = "translation_packs"


@dataclass(frozen=True)
class TranslationPackResult:
    pack_path: Path
    staging_dir: Path


def _sanitize_filename(value: str) -> str:
    s = "".join(c if c.isalnum() or c in " -_." else "_" for c in str(value or ""))
    s = " ".join(s.split()).strip().strip(".")
    return s or "export"


def _resolve_pack_output_path(
    *,
    survey_id: str,
    survey_name: str,
    export_dir: Path,
    output_path: Path | None,
    smart_name: bool,
) -> Path:
    def default_name() -> str:
        if not smart_name:
            return f"{survey_id}__translation_pack.zip"
        safe = _sanitize_filename(survey_name) if survey_name else survey_id
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{safe}__{survey_id}__translation_pack__{stamp}.zip"

    if output_path is None:
        return export_dir / default_name()

    output_path = Path(output_path)
    if output_path.is_dir():
        return output_path / default_name()

    if not output_path.parent.exists():
        raise ValueError(f"Output directory does not exist: {output_path.parent}")

    if output_path.suffix == "":
        return output_path.with_suffix(".zip")
    if output_path.suffix.lower() != ".zip":
        raise ValueError(f"Output path must be a .zip file (got: {output_path})")
    return output_path


def _resolve_languages(
    survey_id: str,
    payload: dict,
    languages: Sequence[str] | None,
    *,
    include_base: bool,
) -> tuple[list[str], str]:
    base_language = get_base_language(payload) or "EN"
    resolved = normalize_language_list(languages) if languages else []
    if not resolved:
        resolved = list_enabled_languages(payload)
    if not include_base:
        resolved = [lang for lang in resolved if lang != base_language]
    if not resolved:
        raise QsyncValidationError(
            error_id="QSYNC-TRANSLATIONS-PACK-001",
            problem="No languages selected for translation pack.",
            why="Resolved language list is empty.",
            impact="Pack cannot be created.",
            action="Provide --language/--languages or enable at least one language.",
            context={"survey_id": survey_id},
        )
    return resolved, base_language


def _write_manifest(
    staging_dir: Path,
    *,
    survey_id: str,
    survey_name: str,
    base_language: str,
    languages: Sequence[str],
    docx_path: Path,
    extra_files: Iterable[str],
) -> None:
    manifest = {
        "survey_id": survey_id,
        "survey_name": survey_name,
        "base_language": base_language,
        "languages": list(languages),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": [docx_path.name, *sorted(extra_files)],
    }
    (staging_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
    )


def _write_readme(
    staging_dir: Path,
    *,
    survey_id: str,
    survey_name: str,
    base_language: str,
    languages: Sequence[str],
    edf_overrides: Mapping[str, str] | None,
    workbook_name: str | None,
) -> None:
    lines = [
        "Translation Pack (qsync)",
        "",
        f"Survey: {survey_name or survey_id} ({survey_id})",
        f"Base language: {base_language}",
        f"Target languages: {', '.join(languages)}",
        "",
        "Contents:",
        "- survey_translation.docx (survey flow + logic; translator review)",
        "- survey_translation.flow.mmd (mermaid flow chart, if generated)",
        "- survey_translation.flow.png (flow chart image, if generated)",
        "- translations/<LANG>.json (from cached survey definition)",
    ]
    if workbook_name:
        lines.append(f"- {workbook_name} (Excel workbook with translation columns)")
    lines.extend(
        [
            "",
            "Notes:",
            "- This pack does not include UI screenshots; the DOCX export is the primary context.",
            "- JS-injected strings and EOS messages live outside the translations API and must be edited via qsync JS/EOS workflows.",
        ]
    )
    if edf_overrides:
        lines.extend(
            [
                "",
                "Scenario overrides applied:",
            ]
        )
        for key, value in sorted(edf_overrides.items()):
            lines.append(f"- {key}={value}")
    (staging_dir / "README.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_translation_maps_from_cache(
    payload: dict,
    *,
    base_language: str,
    languages: Sequence[str],
    staging_dir: Path,
) -> list[str]:
    translations_dir = staging_dir / "translations"
    translations_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for lang in languages:
        data = build_translation_map_from_cache(
            payload,
            language=lang,
            base_language=base_language,
        )
        dest = translations_dir / f"{lang}.json"
        dest.write_text(
            json.dumps(data, indent=2, ensure_ascii=True, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        copied.append(str(dest.relative_to(staging_dir)))
    return copied


def _copy_workbook(workbook_path: Path, staging_dir: Path) -> str:
    dest = staging_dir / workbook_path.name
    shutil.copy2(workbook_path, dest)
    return str(dest.relative_to(staging_dir))


def build_translation_pack(
    survey_id: str,
    *,
    languages: Sequence[str] | None = None,
    output_path: Path | None = None,
    smart_name: bool = False,
    edf_overrides: Mapping[str, str] | None = None,
    include_base: bool = False,
    refresh: bool = False,
    workbook_path: Path | None = None,
    keep_staging: bool = False,
    render_mermaid: bool = False,
) -> TranslationPackResult:
    root = resolve_root(required=False) or Path.cwd()
    export_dir = resolve_scoped_dir(EXPORT_DIRNAME, root=root) / PACK_SUBDIR
    export_dir.mkdir(parents=True, exist_ok=True)
    from .interactive_menu import is_interactive

    interactive = is_interactive()
    if refresh:
        cache, _ = refresh_survey_cache(survey_id)
    else:
        _preflight_cache_freshness(survey_id, interactive=interactive)
        cache = load_cached_survey(survey_id)

    survey_name = str((cache.payload.get("result", {}) or {}).get("SurveyName") or "")

    pack_path = _resolve_pack_output_path(
        survey_id=survey_id,
        survey_name=survey_name,
        export_dir=export_dir,
        output_path=output_path,
        smart_name=smart_name,
    )

    languages, base_language = _resolve_languages(
        survey_id, cache.payload, languages, include_base=include_base
    )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    staging_dir = (
        resolve_scoped_dir("tmp", root=root) / "translation_pack" / f"{survey_id}__{stamp}"
    )
    staging_dir.mkdir(parents=True, exist_ok=True)

    docx_path = staging_dir / "survey_translation.docx"
    export_survey_to_word(
        survey_id,
        output_path=docx_path,
        edf_overrides=dict(edf_overrides) if edf_overrides else None,
        smart_name=False,
        include_html_source=True,
        render_mermaid=render_mermaid,
        include_mermaid=render_mermaid,
        refresh=False,
        interactive=interactive,
        skip_preflight=True,
    )

    extra_files: list[str] = []
    translations_files = _write_translation_maps_from_cache(
        cache.payload,
        base_language=base_language,
        languages=languages,
        staging_dir=staging_dir,
    )
    extra_files.extend(translations_files)

    workbook_name = None
    if workbook_path:
        workbook_name = _copy_workbook(Path(workbook_path), staging_dir)
        extra_files.append(workbook_name)

    _write_manifest(
        staging_dir,
        survey_id=survey_id,
        survey_name=survey_name,
        base_language=base_language,
        languages=languages,
        docx_path=docx_path,
        extra_files=extra_files,
    )
    _write_readme(
        staging_dir,
        survey_id=survey_id,
        survey_name=survey_name,
        base_language=base_language,
        languages=languages,
        edf_overrides=edf_overrides,
        workbook_name=workbook_name,
    )

    with zipfile.ZipFile(pack_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(staging_dir.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(staging_dir))

    if not keep_staging:
        shutil.rmtree(staging_dir, ignore_errors=True)

    return TranslationPackResult(pack_path=pack_path, staging_dir=staging_dir)
