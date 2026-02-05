#!/usr/bin/env python3
"""Preview differences between local survey_js files and cached QuestionJS.

This script uses a mapping CSV (js_file ↔ QID list) to compare each local
`survey_js/core/<js_file>` against the `QuestionJS` block for the mapped QIDs
in a cached survey JSON, similar in spirit to `qsync preview` for Excel.

It reports, per (js_file, QID) pair:
  - whether the inline QuestionJS matches the local file exactly,
  - whether differences are comment-only,
  - or whether there are substantive code differences (with optional diffs).
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
from dataclasses import dataclass
import re
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from ..config import resolve_root
from ..drift_check import check_drift as run_drift_check
from ..scope_filter import ScopeFilter

ROOT = resolve_root(required=False) or Path.cwd()
SURVEYS_DIR = ROOT / "surveys"
SURVEY_JS_CORE = ROOT / "survey_js" / "core"
CORE_JS_FILES = {
    p.relative_to(SURVEY_JS_CORE).as_posix()
    for p in SURVEY_JS_CORE.rglob("*.js")
    if p.is_file()
}
DEFAULT_SURVEY_ID = "SV_5AsKyAO5QqswBcq"
DEFAULT_MAPPING_CSV = ROOT / "survey_js" / "survey_qid_js_map.csv"


@dataclass
class JsDiffResult:
    """Result row for comparing a local JS file against cached `QuestionJS`."""

    js_file: str
    qid: str
    status: str  # "match", "comments-only", "diff", "missing", "trash", or "unused"
    detail: str
    diff_lines: List[str]
    data_export_tag: str | None = None


def load_survey_json(survey_id: str) -> Tuple[Dict, Path]:
    """Load the most recent cached survey JSON for a given survey_id."""

    matches = sorted(
        SURVEYS_DIR.glob(f"*{survey_id}.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        raise FileNotFoundError(
            f"No survey JSON found in {SURVEYS_DIR} for ID {survey_id}. "
            "Run qsync init or download_survey_definition.py first."
        )
    path = matches[0]
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "result" in payload and isinstance(payload["result"], dict):
        payload = payload["result"]
    return payload, path


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


def load_mapping(path: Path, survey_id: str) -> Dict[str, List[str]]:
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


def _build_qid_scope(payload: Dict) -> Dict[str, str]:
    """Classify QIDs by where they live in the survey structure.

    Returns a mapping `QID -> scope`, where scope is one of:
      - "active"  – question appears in a non-Trash block
      - "trash"   – question appears in a Trash block
      - "unplaced" – defined under Questions but not in any block
    """

    blocks = payload.get("Blocks") or {}
    questions = payload.get("Questions") or {}

    active_qids: set[str] = set()
    trash_qids: set[str] = set()

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
                trash_qids.add(qid)
            else:
                active_qids.add(qid)

    scope: Dict[str, str] = {}
    for qid in questions.keys():
        if qid in active_qids:
            scope[qid] = "active"
        elif qid in trash_qids:
            scope[qid] = "trash"
        else:
            scope[qid] = "unplaced"
    return scope


def _collect_active_js_assignments(payload: Dict) -> List[Tuple[str, str | None]]:
    questions = payload.get("Questions") or {}
    scope = _build_qid_scope(payload)
    assignments: List[Tuple[str, str | None]] = []
    for qid, details in questions.items():
        if scope.get(qid) != "active":
            continue
        js = (
            details.get("QuestionJS") or details.get("QuestionJSContent") or ""
        ).strip()
        if not js:
            continue
        first_line = next(
            (line.strip() for line in js.splitlines() if line.strip()), ""
        )
        match = re.match(r"//\s*([^\s]+)", first_line)
        js_name = match.group(1) if match else None
        assignments.append((qid, js_name))
    return assignments


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


def compare_js_pair(
    local_code: str,
    question_js: str,
    *,
    label: str,
    from_label: str = "cached",
    to_label: str = "local",
) -> JsDiffResult:
    """Classify the difference between a local JS file and inline QuestionJS."""

    local_raw = _normalize_newlines(local_code).strip()
    remote_raw = _normalize_newlines(question_js).strip()

    if local_raw == remote_raw:
        return JsDiffResult(
            label, "", "match", "Local JS matches cached QuestionJS exactly.", []
        )

    local_sem = _strip_leading_comments(local_raw)
    remote_sem = _strip_leading_comments(remote_raw)
    if local_sem == remote_sem:
        diff = list(
            difflib.unified_diff(
                remote_raw.splitlines(),
                local_raw.splitlines(),
                fromfile=from_label,
                tofile=to_label,
                lineterm="",
            )
        )
        return JsDiffResult(
            label,
            "",
            "comments-only",
            "Differences are limited to leading comments/whitespace.",
            diff,
        )

    diff = list(
        difflib.unified_diff(
            remote_raw.splitlines(),
            local_raw.splitlines(),
            fromfile=from_label,
            tofile=to_label,
            lineterm="",
        )
    )
    return JsDiffResult(
        label, "", "diff", "Substantive code differences detected.", diff
    )


def preview_differences(
    survey_id: str,
    mapping_csv: Path,
    *,
    show_equal: bool = False,
    detailed: bool = False,
    include_qids: set[str] | None = None,
    include_js: set[str] | None = None,
    scope_expr: str | None = None,
    interactive: bool = True,
    verbose: bool = True,
    check_drift: bool = True,
) -> List[JsDiffResult]:
    """Compute JS differences for all (js_file, QID) pairs from the mapping.

    Args:
        survey_id: Survey ID
        mapping_csv: Path to JS mapping CSV
        show_equal: Show unchanged JS blocks
        detailed: Show detailed diffs
        include_qids: Filter to specific QIDs
        include_js: Filter to specific JS files
        scope_expr: Scope filter expression (e.g., 'qid:Q1 OR tag:baseline')
        interactive: Allow interactive prompts (e.g., for drift check)
        verbose: Show info messages and summaries (disable for quiet detection)
    """

    # Check for drift before preview
    if check_drift:
        drift_report = run_drift_check(
            survey_id, dimension="js", interactive=interactive
        )
        if drift_report.has_drift:
            drift_report.display(interactive=False)

    payload, path = load_survey_json(survey_id)
    questions = payload.get("Questions") or {}
    qid_scope = _build_qid_scope(payload)

    mapping = load_mapping(mapping_csv, survey_id)
    if include_js:
        mapping = {js: qids for js, qids in mapping.items() if js in include_js}

    # Apply scope filtering if provided
    if scope_expr:
        scope_filter = ScopeFilter.parse(scope_expr)
        filtered_mapping = {}
        for js_file, qids in mapping.items():
            filtered_qids = [
                qid
                for qid in qids
                if qid in questions
                and scope_filter.matches(
                    qid=qid,
                    tags=(
                        [questions[qid].get("DataExportTag")]
                        if questions[qid].get("DataExportTag")
                        else None
                    ),
                    js_file=js_file,
                )
            ]
            if filtered_qids:
                filtered_mapping[js_file] = filtered_qids
        mapping = filtered_mapping

    assignments = _collect_active_js_assignments(payload)
    total_js_blocks = len(assignments)
    unbacked = [
        (qid, js_name)
        for qid, js_name in assignments
        if not js_name or js_name not in CORE_JS_FILES
    ]
    results: List[JsDiffResult] = []

    from ..terminal_colors import Colors, colored, colors_enabled
    from ..terminal_output import header, info

    if verbose:
        info("[js-preview]", f"Using survey file: {path}")
        info("[js-preview]", f"Using mapping: {mapping_csv}")

    for js_file, qids in sorted(mapping.items()):
        core_path = SURVEY_JS_CORE / js_file
        if not core_path.exists():
            results.append(
                JsDiffResult(
                    js_file,
                    "",
                    "missing",
                    f"Local JS file not found at {core_path}",
                    [],
                )
            )
            continue

        local_code = core_path.read_text(encoding="utf-8")

        if not qids:
            results.append(
                JsDiffResult(
                    js_file,
                    "",
                    "unmapped",
                    "No QIDs mapped for this JS file in the CSV.",
                    [],
                )
            )
            continue

        for qid in qids:
            if include_qids and qid not in include_qids:
                continue
            scope = qid_scope.get(qid)
            if scope in {"trash", "unplaced"}:
                status = "trash" if scope == "trash" else "unused"
                detail = (
                    "Question is in a Trash block; ignoring for live survey preview."
                    if status == "trash"
                    else "Question is not placed in any block; ignoring for live survey preview."
                )
                results.append(
                    JsDiffResult(
                        js_file,
                        qid,
                        status,
                        detail,
                        [],
                    )
                )
                continue

            details = questions.get(qid)
            if not details:
                results.append(
                    JsDiffResult(
                        js_file,
                        qid,
                        "missing",
                        f"QID {qid} not found in cached survey JSON.",
                        [],
                    )
                )
                continue

            question_js = (
                details.get("QuestionJS") or details.get("QuestionJSContent") or ""
            ).strip()
            if not question_js:
                results.append(
                    JsDiffResult(
                        js_file,
                        qid,
                        "missing",
                        "Cached question has no QuestionJS/QuestionJSContent block.",
                        [],
                        data_export_tag=(details.get("DataExportTag") or "").strip()
                        or None,
                    )
                )
                continue

            diff = compare_js_pair(
                local_code,
                question_js,
                label=js_file,
                from_label=f"cache [{path.name}]",
                to_label=f"local [{js_file}]",
            )
            diff.qid = qid
            diff.data_export_tag = (details.get("DataExportTag") or "").strip() or None
            if show_equal or diff.status != "match":
                results.append(diff)

    # Summarise
    total_pairs = sum(len(v) or 1 for v in mapping.values())
    trash_pairs = [r for r in results if r.status == "trash"]
    unused_pairs = [r for r in results if r.status == "unused"]
    changed = [r for r in results if r.status in {"diff", "comments-only"}]
    comment_only = [r for r in changed if r.status == "comments-only"]
    substantive = [r for r in changed if r.status == "diff"]

    if verbose:
        print()
        info("[js-preview]", f"Survey contains {total_js_blocks} active JS block(s).")
        info(
            "[js-preview]",
            f"Of these, {len(unbacked)} block(s) currently use inline JS with no matching file under survey_js/core.",
        )
        info(
            "[js-preview]",
            f"{len(changed)} block(s) would change ({len(substantive)} substantive, {len(comment_only)} comments-only).",
        )
        info("[js-preview]", f"Checked {total_pairs} mapping pair(s) for this survey.")
        if trash_pairs or unused_pairs:
            info(
                "[js-preview]",
                f"{len(trash_pairs)} mapped pair(s) are in Trash, {len(unused_pairs)} mapped pair(s) are unplaced.",
            )

    if verbose and changed:
        header("[js-preview]", "QIDs with pending changes:")
        header_text = (
            f"{'QID':<8} {'Tag':<20} {'Core JS File':<40} {'Change':<14} {'Δ(+/-)'}"
        )
        if colors_enabled():
            print(colored(header_text, Colors.GRAY, dim=True))
        else:
            print(header_text)
        print("-" * len(header_text))
        for r in sorted(changed, key=lambda item: item.qid):
            change = "comments-only" if r.status == "comments-only" else "substantive"
            plus = sum(
                1
                for line in r.diff_lines
                if line.startswith("+") and not line.startswith("+++")
            )
            minus = sum(
                1
                for line in r.diff_lines
                if line.startswith("-") and not line.startswith("---")
            )
            delta = f"+{plus}/-{minus}" if plus or minus else "-"
            tag = (r.data_export_tag or "")[:20]
            print(f"{r.qid:<8} {tag:<20} {(r.js_file or ''):<40} {change:<14} {delta}")
        print()

    # Detailed output (optional)
    if verbose and detailed:
        from ..terminal_colors import colorize_unified_diff_lines

        for r in results:
            header_text = f"- {r.js_file} @ {r.qid or '∅'}: {r.status}"
            print(header_text)
            if r.detail:
                print(f"  {r.detail}")
            if r.diff_lines:
                local_path = SURVEY_JS_CORE / r.js_file if r.js_file else SURVEY_JS_CORE
                print(f"  context: local={local_path}, cache={path}")
                for line in colorize_unified_diff_lines(r.diff_lines):
                    print("  " + line)
            print()

    return results


def main(argv: Iterable[str] | None = None) -> None:
    """CLI entry point for previewing diffs between `survey_js/core` and cached QuestionJS."""

    parser = argparse.ArgumentParser(description=__doc__)
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
        "--show-equal",
        action="store_true",
        help="Include pairs where local JS matches cached QuestionJS exactly.",
    )
    parser.add_argument(
        "--detailed",
        action="store_true",
        help="Print unified diffs for pairs with differences.",
    )
    parser.add_argument(
        "--include-qid",
        action="append",
        dest="include_qids",
        default=[],
        help="Limit preview to specific Qualtrics QIDs (repeatable).",
    )
    parser.add_argument(
        "--include-js",
        action="append",
        dest="include_js",
        default=[],
        help="Limit preview to specific core JS filenames.",
    )

    args = parser.parse_args(list(argv) if argv is not None else None)

    preview_differences(
        survey_id=args.survey_id,
        mapping_csv=args.mapping,
        show_equal=args.show_equal,
        detailed=args.detailed,
        include_qids=set(args.include_qids) if args.include_qids else None,
        include_js=set(args.include_js) if args.include_js else None,
    )


if __name__ == "__main__":
    main()
