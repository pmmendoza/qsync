#!/usr/bin/env python3
"""Push cached QuestionJS updates to Qualtrics for mapped questions.

This script is a thin wrapper around qsync.qualtrics_client.push_questions:

1. Loads the cached survey JSON for a given survey_id (as used by qsync).
2. Reads the JS↔QID mapping CSV (one column per survey_id).
3. Optionally filters out questions that live in Trash blocks.
4. Calls push_questions() for the selected QIDs so their QuestionJS (and the
   rest of the question definition) is uploaded to Qualtrics.

Use this after running `qsync js sync` (or `python -m qsync.js_sync`) so that the cached
QuestionJS reflects the ground-truth files under survey_js/core.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, Iterable, List, Set

from ..argparse_support import QsyncArgumentParser
from ..qualtrics_client import SurveyCache, load_cached_survey, push_questions
from ..drift_check import enforce_no_drift
from ..config import resolve_root, resolve_scoped_dir
from ..push_safeguards import enforce_push_safeguards, SafeguardConfig
from ..auto_publish import auto_publish_after_push
from ..workspace_paths import survey_js_core_dir

ROOT = resolve_root(required=False) or Path.cwd()
DEFAULT_SURVEY_ID = "SV_5AsKyAO5QqswBcq"
DEFAULT_MAPPING_CSV = resolve_scoped_dir("survey_js", root=ROOT) / "survey_qid_js_map.csv"


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
                continue
            qids = [q.strip() for q in qids_raw.split(";") if q.strip()]
            if qids:
                mapping[js_file] = qids
    return mapping


def _classify_qids_by_scope(payload: dict) -> Dict[str, str]:
    """Return a mapping QID → scope ('active', 'trash', or 'unplaced')."""

    blocks = payload.get("Blocks") or {}
    questions = payload.get("Questions") or {}

    active: Set[str] = set()
    trash: Set[str] = set()

    for block in blocks.values():
        btype = (block.get("Type") or "").strip()
        elements = block.get("BlockElements") or block.get("Elements") or []
        for elem in elements:
            etype = (elem.get("Type") or elem.get("Element") or "").strip()
            if etype != "Question":
                continue
            qid = elem.get("QuestionID")
            if not qid:
                continue
            if btype == "Trash":
                trash.add(qid)
            else:
                active.add(qid)

    scope: Dict[str, str] = {}
    for qid in questions.keys():
        if qid in active:
            scope[qid] = "active"
        elif qid in trash:
            scope[qid] = "trash"
        else:
            scope[qid] = "unplaced"
    return scope


def _resolve_js_field(question: dict) -> str:
    if "QuestionJS" in question and question.get("QuestionJS"):
        return "QuestionJS"
    if "QuestionJSContent" in question and question.get("QuestionJSContent"):
        return "QuestionJSContent"
    if "QuestionJS" in question:
        return "QuestionJS"
    return "QuestionJS"


def _apply_local_js_entries(
    survey: SurveyCache,
    entries: Iterable[dict[str, str]],
) -> set[str]:
    from ..terminal_output import warn

    root = resolve_root(required=False) or Path.cwd()
    core_dir = survey_js_core_dir(root=root)
    updated: set[str] = set()
    js_cache: dict[str, str] = {}
    for entry in entries:
        js_file = (entry.get("js_file") or "").strip()
        qid = (entry.get("qid") or "").strip()
        if not js_file or not qid:
            continue
        core_path = core_dir / js_file
        if not core_path.exists():
            warn("[push-js]", f"Local JS file not found: {core_path}")
            continue
        if js_file not in js_cache:
            js_cache[js_file] = core_path.read_text(encoding="utf-8")
        question = survey.questions.get(qid)
        if not question:
            warn("[push-js]", f"QID {qid} not found in cached survey; skipping.")
            continue
        key = _resolve_js_field(question)
        question[key] = js_cache[js_file]
        updated.add(qid)
    return updated


def push_js_from_cache(
    survey_id: str,
    mapping_csv: Path,
    *,
    include_trash: bool = False,
    dry_run: bool = False,
    qids_override: Iterable[str] | None = None,
    pending_entries: Iterable[dict[str, str]] | None = None,
    publish: bool = True,
    publish_description: str | None = None,
    force_live: bool = False,
    force_preview: bool = False,
    interactive: bool = True,
    allow_drift: bool = False,
) -> List[str]:
    """Push QuestionJS for mapped QIDs from the cached survey to Qualtrics.

    Returns the list of QIDs that were (or would be) pushed.
    """
    from ..terminal_output import info, success, warn

    enforce_no_drift(
        survey_id=survey_id,
        dimension="js",
        allow_drift=allow_drift,
        interactive=interactive,
    )

    survey = load_cached_survey(survey_id)
    updated_qids: set[str] | None = None
    if pending_entries:
        updated_qids = _apply_local_js_entries(survey, pending_entries)
    payload = survey.payload.get("result") or survey.payload
    scope = _classify_qids_by_scope(payload)
    mapping = _load_mapping(mapping_csv, survey_id)

    qids_to_push: Set[str] = set()

    if qids_override:
        for qid in qids_override:
            if updated_qids is not None and qid not in updated_qids:
                warn(
                    "[push-js]",
                    f"Pending QID {qid} has no local JS update; skipping.",
                )
                continue
            if qid not in survey.questions:
                warn(
                    "[push-js]",
                    f"Pending QID {qid} not found in cached survey; skipping.",
                )
                continue
            q_scope = scope.get(qid, "unplaced")
            if q_scope == "trash" and not include_trash:
                info("[push-js]", f"Skipping {qid} (Trash block).")
                continue
            qids_to_push.add(qid)
    else:
        for js_file, qids in mapping.items():
            for qid in qids:
                if updated_qids is not None and qid not in updated_qids:
                    continue
                if qid not in survey.questions:
                    warn(
                        "[push-js]",
                        f"QID {qid} from {js_file} not found in cached survey; skipping.",
                    )
                    continue
                q_scope = scope.get(qid, "unplaced")
                if q_scope == "trash" and not include_trash:
                    info("[push-js]", f"Skipping {js_file} @ {qid} (Trash block).")
                    continue
                qids_to_push.add(qid)

    if not qids_to_push:
        info("[push-js]", "No QIDs qualified for push based on the mapping.")
        return []

    sorted_qids = sorted(qids_to_push)
    info(
        "[push-js]",
        f"Will push {len(sorted_qids)} question(s): {', '.join(sorted_qids)}",
    )

    if dry_run:
        info("[push-js]", "DRY-RUN: not calling Qualtrics API.")
        return sorted_qids

    # Enforce push safeguards
    config = SafeguardConfig(
        survey_id=survey_id,
        dimension="js",
        force_live=force_live,
        force_preview=force_preview,
        auto_yes=not interactive,
    )
    safeguard_result = enforce_push_safeguards(config)
    if safeguard_result.warnings:
        for warning in safeguard_result.warnings:
            warn("[qsync:js]", f"WARNING: {warning}")

    push_context = {
        "origin": "qsync.js_push",
        "mapping_csv": str(mapping_csv),
        "changed_qids": sorted_qids,
        "changed_count": len(sorted_qids),
    }

    push_questions(survey, sorted_qids, context=push_context)

    # Use auto-publish module
    if publish:
        auto_publish_after_push(
            survey_id=survey_id,
            dimension="js",
            changed_qids=sorted_qids,
            custom_description=publish_description,
            workbook_path=str(mapping_csv),
            interactive=interactive,
            context=push_context,
        )
    else:
        success("[push-js]", "Push complete (not published).")
    return sorted_qids


def main(argv: Iterable[str] | None = None) -> None:
    """CLI entry point for pushing local `survey_js/core/*.js` into Qualtrics QuestionJS."""

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
        "--include-trash",
        action="store_true",
        help="Also push questions that live in Trash blocks.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which QIDs would be pushed without calling the API.",
    )

    args = parser.parse_args(list(argv) if argv is not None else None)

    push_js_from_cache(
        survey_id=args.survey_id,
        mapping_csv=args.mapping,
        include_trash=bool(args.include_trash),
        dry_run=bool(args.dry_run),
    )


if __name__ == "__main__":
    main()
