#!/usr/bin/env python3
"""Sync cached QuestionJS with local survey_js/core versions.

For a given survey_id, this script:
  - Uses the multi-survey JS mapping CSV to find (js_file, QID) pairs.
  - Compares each local `survey_js/core/<js_file>` to the cached QuestionJS.
  - If they are identical or differ only in leading comments/whitespace,
    it overwrites the cached QuestionJS with the local file content.

This keeps the cached JSON aligned with our local ground-truth JS, while
avoiding automatic changes where there are substantive code differences.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from ..argparse_support import QsyncArgumentParser
from ..config import resolve_root, resolve_scoped_dir, resolve_survey_cache_dir
from ..workspace_paths import survey_js_core_dir

ROOT = resolve_root(required=False) or Path.cwd()

DEFAULT_SURVEY_ID = "SV_5AsKyAO5QqswBcq"
DEFAULT_MAPPING_CSV = resolve_scoped_dir("survey_js", root=ROOT) / "survey_qid_js_map.csv"


def _core_dir() -> Path:
    root = resolve_root(required=False) or Path.cwd()
    return survey_js_core_dir(root=root)


def _find_survey_file(survey_id: str) -> Path:
    """Return the newest cached survey JSON for the given survey ID."""

    root = resolve_root(required=False) or Path.cwd()
    surveys_dir = resolve_survey_cache_dir(root=root)
    matches = sorted(
        surveys_dir.glob(f"*{survey_id}.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        raise FileNotFoundError(
            f"No survey JSON found in {surveys_dir} for ID {survey_id}. "
            "Run qsync init first."
        )
    return matches[0]


def _resolve_mapping_column(fieldnames: List[str], survey_id: str) -> str:
    for name in fieldnames:
        if name == "js_file":
            continue
        prefix = name.split("-", 1)[0]
        if prefix == survey_id:
            return name
    raise ValueError(
        f"Mapping CSV missing a column for survey_id '{survey_id}'. "
        f"Available columns: {', '.join(n for n in fieldnames if n != 'js_file')}"
    )


def _load_mapping(path: Path, survey_id: str) -> Dict[str, List[str]]:
    """Load js_file → [QID,…] mapping for a given survey_id from the CSV."""

    mapping: Dict[str, List[str]] = {}
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        if "js_file" not in fieldnames:
            raise ValueError(
                f"Mapping CSV {path} is missing required 'js_file' column."
            )
        column = _resolve_mapping_column(fieldnames, survey_id)
        for row in reader:
            js_file = (row.get("js_file") or "").strip()
            if not js_file or js_file.startswith('"'):
                continue
            qids_raw = (row.get(column) or "").strip()
            if not qids_raw:
                mapping[js_file] = []
                continue
            qids = [q.strip() for q in qids_raw.split(";") if q.strip()]
            mapping[js_file] = qids
    return mapping


def _normalize_newlines(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


def _strip_leading_comments(s: str) -> str:
    """Remove leading //-comment lines for semantic comparison."""

    lines = _normalize_newlines(s).splitlines()
    out: List[str] = []
    strip_phase = True
    for line in lines:
        if strip_phase and line.lstrip().startswith("//"):
            continue
        strip_phase = False
        out.append(line)
    return "\n".join(out).strip()


def _classify_js(local_code: str, question_js: str) -> str:
    """Return 'match', 'comments-only', or 'diff' for the given JS pair."""

    local_raw = _normalize_newlines(local_code).strip()
    remote_raw = _normalize_newlines(question_js).strip()

    if local_raw == remote_raw:
        return "match"

    local_sem = _strip_leading_comments(local_raw)
    remote_sem = _strip_leading_comments(remote_raw)
    if local_sem == remote_sem:
        return "comments-only"

    return "diff"


def sync_js_with_cached(
    survey_id: str,
    mapping_csv: Path,
    *,
    include_match: bool = True,
    dry_run: bool = False,
    create_missing: bool = False,
    allow_diff: bool = False,
    include_qids: set[str] | None = None,
    include_js: set[str] | None = None,
    scope_expr: str | None = None,
) -> List[Tuple[str, str, str]]:
    """Synchronise cached QuestionJS with local core JS where safe.

    Returns a list of (js_file, qid, status) for each updated pair.
    """
    from ..terminal_output import info, success, warn

    survey_path = _find_survey_file(survey_id)
    root = json.loads(survey_path.read_text(encoding="utf-8"))
    result = root.get("result") or root
    questions = result.get("Questions") or {}

    mapping = _load_mapping(mapping_csv, survey_id)
    if include_js:
        mapping = {js: qids for js, qids in mapping.items() if js in include_js}

    info("[js-sync]", f"Using survey file: {survey_path}")
    info("[js-sync]", f"Using mapping: {mapping_csv}")

    updates: List[Tuple[str, str, str]] = []
    core_dir = _core_dir()

    for js_file, qids in sorted(mapping.items()):
        core_path = core_dir / js_file
        if not core_path.exists():
            warn("[js-sync]", f"Local JS file not found: {core_path}")
            continue

        local_code = core_path.read_text(encoding="utf-8")
        if not qids:
            continue

        for qid in qids:
            if include_qids and qid not in include_qids:
                continue
            details = questions.get(qid)
            if not details:
                warn(
                    "[js-sync]",
                    f"QID {qid} not found in cached survey JSON; skipping {js_file}.",
                )
                continue

            question_js = (
                details.get("QuestionJS") or details.get("QuestionJSContent") or ""
            ).strip()
            if not question_js:
                if not create_missing:
                    warn(
                        "[js-sync]",
                        f"QID {qid} has no QuestionJS/QuestionJSContent; skipping {js_file}.",
                    )
                    continue
                status = "created"
            else:
                status = _classify_js(local_code, question_js)
                if status == "diff" and not allow_diff:
                    continue
                if status == "match" and not include_match:
                    continue

            # Decide which field to write back into.
            if "QuestionJS" in details and details.get("QuestionJS"):
                key = "QuestionJS"
            elif "QuestionJSContent" in details and details.get("QuestionJSContent"):
                key = "QuestionJSContent"
            elif "QuestionJS" in details:
                key = "QuestionJS"
            else:
                key = "QuestionJS"

            details[key] = local_code
            updates.append((js_file, qid, status))

    if not updates:
        info("[js-sync]", "No QuestionJS blocks qualified for synchronisation.")
        return updates

    if dry_run:
        info(
            "[js-sync]",
            f"DRY-RUN: {len(updates)} update(s) would be applied; survey JSON not modified.",
        )
        return updates

    survey_path.write_text(
        json.dumps(root, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    success("[js-sync]", f"Applied {len(updates)} update(s) to {survey_path}.")

    for js_file, qid, status in updates:
        print(f"- {js_file} @ {qid}: synced ({status})")

    return updates


def main(argv: Iterable[str] | None = None) -> None:
    """CLI entry point for syncing cached QuestionJS from local `survey_js/core`."""

    parser = QsyncArgumentParser(description=__doc__)
    parser.add_argument(
        "--survey-id",
        default=DEFAULT_SURVEY_ID,
        help=f"Qualtrics survey ID (default: {DEFAULT_SURVEY_ID})",
    )
    parser.add_argument(
        "--mapping",
        type=Path,
        default=DEFAULT_MAPPING_CSV,
        help=f"Path to JS↔QID mapping CSV (default: {DEFAULT_MAPPING_CSV})",
    )
    parser.add_argument(
        "--include-match",
        action="store_true",
        help="Also sync mapped QIDs whose cached JS already matches local files.",
    )
    parser.add_argument(
        "--no-include-match",
        action="store_true",
        help="Alias for default behavior (sync changed QIDs only).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute potential updates but do not modify the survey JSON.",
    )
    parser.add_argument(
        "--create-missing",
        action="store_true",
        help="If a mapped QID has no QuestionJS block yet, create it automatically.",
    )
    parser.add_argument(
        "--allow-diff",
        action="store_true",
        help="Overwrite cached JS even when substantive diffs are detected.",
    )
    parser.add_argument(
        "--include-qid",
        action="append",
        dest="include_qids",
        default=[],
        help="Limit sync to specific Qualtrics QIDs (can be repeated).",
    )
    parser.add_argument(
        "--include-js",
        action="append",
        dest="include_js",
        default=[],
        help="Limit sync to specific core JS filenames.",
    )

    args = parser.parse_args(list(argv) if argv is not None else None)

    sync_js_with_cached(
        survey_id=args.survey_id,
        mapping_csv=args.mapping,
        include_match=bool(getattr(args, "include_match", False))
        and not bool(args.no_include_match),
        dry_run=bool(args.dry_run),
        create_missing=bool(args.create_missing),
        allow_diff=bool(args.allow_diff),
        include_qids=set(args.include_qids) if args.include_qids else None,
        include_js=set(args.include_js) if args.include_js else None,
    )


if __name__ == "__main__":
    main()
