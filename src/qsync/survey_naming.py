"""Slug-aware survey path naming helpers.

These helpers keep SurveyID as the canonical lookup key while allowing
filesystem-friendly names of the form `<slug>-<survey_id>`.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

from .config import resolve_root, resolve_scoped_dir

_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")


def slugify_survey_name(value: str) -> str:
    text = _SLUG_RE.sub("_", str(value or "").strip())
    text = re.sub(r"_+", "_", text).strip("_")
    return text


def _workspace_root(root: Path | None = None) -> Path:
    return root or resolve_root(required=False) or Path.cwd()


def _name_from_inventory(survey_id: str, *, root: Path | None = None) -> str | None:
    sid = str(survey_id or "").strip()
    if not sid:
        return None

    workspace = _workspace_root(root)
    surveys_dir = resolve_scoped_dir("surveys", root=workspace)
    csv_candidates = [
        surveys_dir / "inventory.csv",
        surveys_dir / "qualtrics_surveys.csv",
    ]
    for csv_path in csv_candidates:
        if not csv_path.exists():
            continue
        try:
            with csv_path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    if str(row.get("id") or "").strip() != sid:
                        continue
                    name = str(row.get("name") or "").strip()
                    return name or None
        except Exception:
            continue
    return None


def _name_from_cached_json_filename(
    survey_id: str, *, root: Path | None = None
) -> str | None:
    sid = str(survey_id or "").strip()
    if not sid:
        return None

    workspace = _workspace_root(root)
    surveys_dir = resolve_scoped_dir("surveys", root=workspace)
    if not surveys_dir.exists():
        return None

    candidates = [p for p in surveys_dir.glob(f"*__{sid}.json") if p.is_file()]
    if not candidates:
        return None
    candidates.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    stem = candidates[0].name.rsplit(".json", 1)[0]
    prefix, sep, _ = stem.rpartition(f"__{sid}")
    if not sep:
        return None
    return prefix.strip() or None


def derive_survey_slug(
    survey_id: str, *, survey_name: str | None = None, root: Path | None = None
) -> str:
    sid = str(survey_id or "").strip()
    if not sid:
        return "unknown"

    if str(survey_name or "").strip():
        slug = slugify_survey_name(str(survey_name))
        if slug:
            return slug

    inv_name = _name_from_inventory(sid, root=root)
    if inv_name:
        slug = slugify_survey_name(inv_name)
        if slug:
            return slug

    cached_name = _name_from_cached_json_filename(sid, root=root)
    if cached_name:
        slug = slugify_survey_name(cached_name)
        if slug:
            return slug

    return slugify_survey_name(sid) or sid


def survey_slugged_key(
    survey_id: str, *, survey_name: str | None = None, root: Path | None = None
) -> str:
    sid = str(survey_id or "").strip() or "unknown"
    slug = derive_survey_slug(sid, survey_name=survey_name, root=root)
    if not slug or slug == sid:
        return sid
    return f"{slug}-{sid}"


def _is_matching_path(path: Path, *, is_dir: bool) -> bool:
    return path.is_dir() if is_dir else path.is_file()


def _sorted_by_mtime(paths: list[Path]) -> list[Path]:
    return sorted(paths, key=lambda p: (p.stat().st_mtime, p.name), reverse=True)


def find_existing_survey_path(
    base_dir: Path,
    survey_id: str,
    *,
    suffix: str = "",
    is_dir: bool = False,
    preferred_name: str | None = None,
) -> Path | None:
    sid = str(survey_id or "").strip()
    if not sid or not base_dir.exists():
        return None

    if preferred_name:
        preferred_path = base_dir / preferred_name
        if preferred_path.exists() and _is_matching_path(preferred_path, is_dir=is_dir):
            return preferred_path

    candidates: list[Path] = []
    bare_name = f"{sid}{suffix}"
    bare_path = base_dir / bare_name
    if bare_path.exists() and _is_matching_path(bare_path, is_dir=is_dir):
        candidates.append(bare_path)

    pattern = f"*-{sid}{suffix}"
    candidates.extend(
        [
            p
            for p in base_dir.glob(pattern)
            if _is_matching_path(p, is_dir=is_dir)
        ]
    )
    if not candidates:
        return None
    return _sorted_by_mtime(candidates)[0]


def resolve_survey_path(
    base_dir: Path,
    survey_id: str,
    *,
    suffix: str = "",
    is_dir: bool = False,
    survey_name: str | None = None,
    root: Path | None = None,
    prefer_existing: bool = True,
    migrate_existing: bool = False,
) -> Path:
    preferred_name = f"{survey_slugged_key(survey_id, survey_name=survey_name, root=root)}{suffix}"
    preferred_path = base_dir / preferred_name
    existing = find_existing_survey_path(
        base_dir,
        survey_id,
        suffix=suffix,
        is_dir=is_dir,
        preferred_name=preferred_name,
    )

    if migrate_existing and existing and existing != preferred_path and not preferred_path.exists():
        try:
            base_dir.mkdir(parents=True, exist_ok=True)
            existing.rename(preferred_path)
            return preferred_path
        except OSError:
            # Fall through to best-effort resolution.
            pass

    if prefer_existing and existing:
        return existing
    return preferred_path


def survey_named_candidate_paths(
    base_dir: Path,
    survey_id: str,
    *,
    suffix: str = "",
    is_dir: bool = False,
    survey_name: str | None = None,
    root: Path | None = None,
) -> list[Path]:
    sid = str(survey_id or "").strip()
    if not sid:
        return []

    preferred_name = f"{survey_slugged_key(sid, survey_name=survey_name, root=root)}{suffix}"
    candidates: list[Path] = [base_dir / preferred_name]
    candidates.append(base_dir / f"{sid}{suffix}")

    if base_dir.exists():
        matches = [
            p
            for p in base_dir.glob(f"*-{sid}{suffix}")
            if _is_matching_path(p, is_dir=is_dir)
        ]
        for path in _sorted_by_mtime(matches):
            if path not in candidates:
                candidates.append(path)

    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        deduped.append(path)
    return deduped
