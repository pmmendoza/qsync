"""Rebuild survey_qid_js_map.csv from survey_js/core and cached survey JSONs."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from .argparse_support import QsyncArgumentParser
from .config import resolve_root, resolve_scoped_dir
from .survey_inventory import _read_csv_rows

ROOT = resolve_root(required=False) or Path.cwd()
SURVEYS_DIR = resolve_scoped_dir("surveys", root=ROOT)
CORE_DIR = ROOT / "survey_js" / "core"
DEFAULT_MAPPING_PATH = resolve_scoped_dir("survey_js", root=ROOT) / "survey_qid_js_map.csv"

COMMENT_RE = re.compile(r"//\s*([^\s]+)")
QID_RE = re.compile(r"([A-Za-z]+)(\d+)")


def _qid_sort_key(qid: str) -> Tuple[str, int]:
    match = QID_RE.fullmatch(qid)
    if match:
        prefix, num = match.groups()
        return prefix, int(num)
    return ("", 0)


def _parse_survey_filename(path: Path) -> Tuple[str, str] | None:
    stem = path.stem
    if "__SV_" in stem:
        label, _, rest = stem.partition("__SV_")
        survey_id = f"SV_{rest}"
        survey_label = label or survey_id
        return survey_id, survey_label
    if stem.startswith("SV_") and "-" in stem:
        survey_id, _, label = stem.partition("-")
        survey_label = label or survey_id
        return survey_id, survey_label
    return None


def _collect_surveys() -> List[Tuple[str, str, Path]]:
    latest: Dict[str, Tuple[str, Path, float]] = {}
    for path in SURVEYS_DIR.glob("*SV*.json"):
        parsed = _parse_survey_filename(path)
        if not parsed:
            continue
        survey_id, label = parsed
        mtime = path.stat().st_mtime
        current = latest.get(survey_id)
        if not current or mtime > current[2]:
            latest[survey_id] = (label, path, mtime)
    columns: List[Tuple[str, str, Path]] = []
    for survey_id in sorted(latest):
        label, path, _ = latest[survey_id]
        columns.append((survey_id, label, path))
    return columns


def _classify_qids_by_scope(result: dict) -> Dict[str, str]:
    blocks = result.get("Blocks") or {}
    questions = result.get("Questions") or {}
    active: set[str] = set()
    trash: set[str] = set()
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


def _extract_js_assignments(result: dict) -> List[Tuple[str, str | None, str]]:
    questions = result.get("Questions") or {}
    scope = _classify_qids_by_scope(result)
    assignments: List[Tuple[str, str | None, str]] = []
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
        match = COMMENT_RE.match(first_line)
        js_name = match.group(1) if match else None
        assignments.append((qid, js_name, js))
    return assignments


def _make_hint(js_text: str, length: int = 20) -> str:
    snippet = js_text.strip()[:length]
    snippet = snippet.replace('"', "'")
    return snippet


def rebuild_mapping(mapping_path: Path, *, dry_run: bool = False) -> None:
    """Rebuild the JS↔QID mapping CSV based on cached survey JSON under `surveys/`."""

    surveys = _collect_surveys()
    if not surveys:
        raise RuntimeError(
            "No cached surveys found under surveys/. Run qsync init first."
        )

    # Get survey inventory for lastModified sorting
    inventory = {entry.get("id"): entry for entry in _read_csv_rows()}

    # Sort surveys by lastModified (newest first)
    surveys_sorted = sorted(
        surveys,
        key=lambda s: inventory.get(s[0], {}).get("lastModified", ""),
        reverse=True,
    )

    columns: List[str] = []
    column_map: Dict[str, str] = {}
    for survey_id, label, _ in surveys_sorted:
        header = f"{survey_id}-{label}"
        columns.append(header)
        column_map[survey_id] = header

    core_files = sorted(
        p.relative_to(CORE_DIR).as_posix()
        for p in CORE_DIR.rglob("*.js")
        if p.is_file()
    )
    rows: Dict[str, Dict[str, List[str]]] = {
        name: {col: [] for col in columns} for name in core_files
    }
    hint_rows: Dict[str, Dict[str, List[str]]] = {}

    for survey_id, label, path in surveys_sorted:
        col = f"{survey_id}-{label}"
        result = json.loads(path.read_text(encoding="utf-8"))
        result = result.get("result") or result
        for qid, js_name, js_text in _extract_js_assignments(result):
            target = rows.get(js_name)
            if target is not None:
                target[col].append(qid)
            else:
                hint = _make_hint(js_text)
                key = f'"{hint}"'
                row = hint_rows.setdefault(key, {c: [] for c in columns})
                row[col].append(qid)

    ordered_rows: List[Tuple[str, Dict[str, List[str]]]] = []
    for name in sorted(rows):
        ordered_rows.append((name, rows[name]))
    for hint in sorted(hint_rows):
        ordered_rows.append((hint, hint_rows[hint]))

    fieldnames = ["js_file"] + columns

    if dry_run:
        print(
            f"[rebuild] Would write {len(ordered_rows)} rows covering {len(columns)} surveys."
        )
        return

    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    with mapping_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for js_name, data in ordered_rows:
            row = {"js_file": js_name}
            for col in columns:
                qids = sorted(set(data.get(col, [])), key=_qid_sort_key)
                if qids:
                    row[col] = ";".join(qids)
            writer.writerow(row)

    print(f"[rebuild] Wrote {len(ordered_rows)} rows to {mapping_path}")


def main(argv: Iterable[str] | None = None) -> None:
    """CLI entry point for rebuilding `survey_js/survey_qid_js_map.csv`."""

    parser = QsyncArgumentParser(description=__doc__)
    parser.add_argument(
        "--mapping",
        type=Path,
        default=DEFAULT_MAPPING_PATH,
        help="Target CSV path (default: survey_js/survey_qid_js_map.csv)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show a summary instead of writing the CSV.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    rebuild_mapping(args.mapping, dry_run=bool(args.dry_run))


if __name__ == "__main__":
    main()
