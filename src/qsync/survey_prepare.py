"""One-command workspace hydration for survey editing surfaces (`qsync survey prepare`)."""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .api_push import send_api_request
from .config import get_client_config, resolve_root
from .qualtrics_client import load_cached_survey
from .qualtrics_client import find_cached_survey_file
from .survey_inventory import (
    INVENTORY_CSV,
    LEGACY_SURVEY_CACHE,
    get_focal_survey_ids,
    load_cached_inventory_records,
    refresh_inventory,
)
from .workbook_resolver import WorkbookResolver


@dataclass
class PrepareSurfaceResult:
    created: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class PrepareSurveyResult:
    survey_id: str
    inventory: PrepareSurfaceResult = field(default_factory=PrepareSurfaceResult)
    cache: PrepareSurfaceResult = field(default_factory=PrepareSurfaceResult)
    workbook: PrepareSurfaceResult = field(default_factory=PrepareSurfaceResult)
    translations: PrepareSurfaceResult = field(default_factory=PrepareSurfaceResult)
    eos: PrepareSurfaceResult = field(default_factory=PrepareSurfaceResult)
    js: PrepareSurfaceResult = field(default_factory=PrepareSurfaceResult)
    js_mapping: PrepareSurfaceResult = field(default_factory=PrepareSurfaceResult)


def ensure_workspace_dirs(root: Path) -> None:
    for rel in (
        "surveys",
        "excel",
        "survey_js",
        "survey_js/core",
        "contents",
        "logs",
        "export",
        "responses",
    ):
        (root / rel).mkdir(parents=True, exist_ok=True)


def ensure_inventory_exists(
    *,
    yes: bool,
    interactive: bool,
) -> tuple[bool, Path | None]:
    """Ensure surveys/inventory.csv exists (legacy accepted for reads)."""

    if INVENTORY_CSV.exists() or LEGACY_SURVEY_CACHE.exists():
        resolved = INVENTORY_CSV if INVENTORY_CSV.exists() else LEGACY_SURVEY_CACHE
        return True, resolved

    if not interactive and not yes:
        return False, None

    if not yes:
        try:
            from .interactive_menu import confirm

            if not confirm(
                "Inventory file missing. Run `qsync survey inventory` now?",
                default=False,
            ):
                return False, None
        except Exception:
            return False, None

    base_url, headers = get_client_config()
    refresh_inventory(
        base_url,
        headers,
        survey_filter=None,
        dry_run=False,
        counts_scope=None,
    )
    return True, INVENTORY_CSV


def resolve_target_surveys(
    *,
    survey_id: str | None,
    focal: bool,
    all_surveys: bool,
    interactive: bool,
    yes: bool,
) -> list[str]:
    if survey_id:
        return [survey_id]

    if focal or all_surveys or interactive:
        ok, _ = ensure_inventory_exists(yes=yes, interactive=interactive)
        if not ok:
            raise RuntimeError(
                "Inventory file missing. Run `qsync survey inventory`, pass --survey-id, "
                "or re-run in an interactive terminal."
            )

    if focal:
        ids = get_focal_survey_ids()
        if not ids:
            raise RuntimeError("No focal surveys found in inventory.")
        return ids

    if all_surveys:
        records = load_cached_inventory_records()
        ids = sorted(records.keys())
        if not ids:
            raise RuntimeError("No surveys found in inventory.")
        return ids

    if not interactive:
        raise RuntimeError(
            "Provide --survey-id (or use --focal/--all in an interactive terminal)."
        )

    from .survey_inventory import prompt_for_survey_id

    selected = prompt_for_survey_id(allow_all_surveys=True, interactive=True)
    if not selected:
        raise RuntimeError("No survey selected.")
    return [selected]


def _is_active_qid(payload: Mapping[str, Any], qid: str) -> bool:
    blocks = payload.get("Blocks") or {}
    for block in blocks.values():
        btype = str(block.get("Type") or "").strip()
        elements = block.get("BlockElements") or block.get("Elements") or []
        for elem in elements:
            etype = str(elem.get("Type") or elem.get("Element") or "").strip()
            if etype != "Question":
                continue
            if str(elem.get("QuestionID") or "").strip() != qid:
                continue
            return btype != "Trash"
    return False


def _qid_to_block_tag(payload: Mapping[str, Any]) -> dict[str, str]:
    blocks = payload.get("Blocks") or {}
    mapping: dict[str, str] = {}
    for block_id, block in blocks.items():
        btype = str(block.get("Type") or "").strip()
        if btype == "Trash":
            continue
        tag = (
            str(block.get("Description") or block.get("BlockDescription") or "").strip()
            or str(block_id).strip()
        )
        elements = block.get("BlockElements") or block.get("Elements") or []
        for elem in elements:
            etype = str(elem.get("Type") or elem.get("Element") or "").strip()
            if etype != "Question":
                continue
            qid = str(elem.get("QuestionID") or "").strip()
            if qid and qid not in mapping:
                mapping[qid] = tag
    return mapping


def _slugify_token(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in value)


def _strip_leading_mapping_hint(js_text: str) -> str:
    lines = js_text.splitlines()
    out: list[str] = []
    seen_first_code = False
    removed_hint = False
    for raw in lines:
        line = raw.rstrip("\n")
        if not seen_first_code:
            if not line.strip():
                continue
            if (not removed_hint) and line.lstrip().startswith("//"):
                token = line.lstrip()[2:].strip().split(None, 1)[0]
                if token.endswith(".js") or "/" in token or "__" in token:
                    removed_hint = True
                    continue
            seen_first_code = True
        out.append(line)
    return "\n".join(out).strip() + ("\n" if out else "")


def _parse_js_hint(js_text: str) -> str | None:
    for raw in js_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("//"):
            token = line[2:].strip().split(None, 1)[0]
            return token or None
        return None
    return None


def _content_hash(js_text: str) -> str:
    normalized = (
        _strip_leading_mapping_hint(js_text)
        .strip()
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _shared_file_name(hash_hex: str) -> str:
    return f"SHARED__{hash_hex[:8]}.js"


def _iter_active_js_questions(
    payload: Mapping[str, Any],
) -> Iterable[tuple[str, dict, str]]:
    questions = payload.get("Questions") or {}
    for qid, details in questions.items():
        if not qid or not isinstance(details, dict):
            continue
        if not _is_active_qid(payload, qid):
            continue
        js = (
            details.get("QuestionJS") or details.get("QuestionJSContent") or ""
        ).strip()
        if not js:
            continue
        yield qid, details, js


def hydrate_js_surfaces(
    *,
    root: Path,
    survey_id: str,
    overwrite_js: bool,
    shared_js: bool,
) -> tuple[PrepareSurfaceResult, dict[str, list[str]]]:
    """Create local JS files and return mapping updates: js_file -> [qid,...]."""

    result = PrepareSurfaceResult()

    cache = load_cached_survey(survey_id)
    payload = cache.payload.get("result") if isinstance(cache.payload, dict) else None
    payload = payload if isinstance(payload, dict) else cache.payload

    qid_to_block = _qid_to_block_tag(payload)

    # First pass: collect QIDs with existing mapping hints vs unmapped JS.
    hinted: list[tuple[str, str, str]] = []
    unmapped: list[tuple[str, str]] = []
    for qid, details, js_text in _iter_active_js_questions(payload):
        hint = _parse_js_hint(js_text)
        if hint:
            hinted.append((qid, hint, js_text))
        else:
            unmapped.append((qid, js_text))

    mapping_updates: dict[str, list[str]] = {}

    core_dir = root / "survey_js" / "core"
    per_survey_dir = core_dir / survey_id
    per_survey_dir.mkdir(parents=True, exist_ok=True)

    # Hydrate any existing hinted mapping: ensure the referenced file exists.
    for qid, hint, js_text in hinted:
        rel = hint
        target = (core_dir / rel).resolve()
        if not str(target).startswith(str(core_dir.resolve())):
            result.errors.append(f"Unsafe JS mapping path for {qid}: {rel}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        mapping_updates.setdefault(rel, []).append(qid)
        if target.exists() and not overwrite_js:
            result.skipped += 1
            continue
        if target.exists() and overwrite_js:
            # Overwrite intentionally.
            pass
        if not js_text:
            continue
        target.write_text(js_text + "\n", encoding="utf-8")
        result.created += 1

    # Second pass: create new files for unique unmapped JS blocks.
    by_hash: dict[str, list[str]] = {}
    js_by_qid: dict[str, str] = {}
    for qid, js_text in unmapped:
        js_by_qid[qid] = js_text
        by_hash.setdefault(_content_hash(js_text), []).append(qid)

    shared_groups = 0
    for h, qids in by_hash.items():
        if len(qids) == 1:
            qid = qids[0]
            js_text = js_by_qid.get(qid) or ""
            details = (payload.get("Questions") or {}).get(qid) or {}
            export_tag = str(details.get("DataExportTag") or "").strip() or qid
            block_tag = str(qid_to_block.get(qid) or "").strip() or "UNBLOCKED"

            export_slug = _slugify_token(export_tag) or _slugify_token(qid)
            block_slug = _slugify_token(block_tag) or "UNBLOCKED"
            filename = f"{block_slug}__{export_slug}.js"
            rel = f"{survey_id}/{filename}"

            target = per_survey_dir / filename
            if target.exists() and not overwrite_js:
                mapping_updates.setdefault(rel, []).append(qid)
                result.skipped += 1
                continue

            cleaned = _strip_leading_mapping_hint(js_text).rstrip()
            content = f"// {rel}\n\n{cleaned}\n"
            target.write_text(content, encoding="utf-8")
            mapping_updates.setdefault(rel, []).append(qid)
            result.created += 1
            continue

        # Shared duplicate group (>1 QID).
        shared_groups += 1
        if not shared_js:
            continue

        filename = _shared_file_name(h)
        rel = f"{survey_id}/{filename}"
        target = per_survey_dir / filename

        if target.exists() and not overwrite_js:
            for qid in qids:
                mapping_updates.setdefault(rel, []).append(qid)
            result.skipped += 1
            continue

        seed_qid = qids[0]
        seed_text = js_by_qid.get(seed_qid) or ""
        cleaned = _strip_leading_mapping_hint(seed_text).rstrip()
        qid_list = ", ".join(qids[:20]) + (" …" if len(qids) > 20 else "")
        content = f"// {rel}\n// QIDs: {qid_list}\n\n{cleaned}\n"
        target.write_text(content, encoding="utf-8")
        for qid in qids:
            mapping_updates.setdefault(rel, []).append(qid)
        result.created += 1

    if shared_groups and not shared_js:
        result.notes.append(
            f"Skipped {shared_groups} shared JS group(s) (unmapped duplicates within survey); "
            "no files generated for those. Re-run with --shared-js to extract them."
        )
    if shared_groups and shared_js:
        result.notes.append(
            f"Extracted {shared_groups} shared JS group(s) using SHARED__<hash8>.js naming."
        )

    return result, mapping_updates


def _resolve_js_column(
    fieldnames: Sequence[str], survey_id: str, header_hint: str
) -> str:
    for name in fieldnames:
        if name == "js_file":
            continue
        if name.split("-", 1)[0] == survey_id:
            return name
    return header_hint


def update_js_mapping_csv(
    *,
    root: Path,
    per_survey_updates: dict[str, dict[str, list[str]]],
) -> PrepareSurfaceResult:
    """Merge JS mapping updates into survey_js/survey_qid_js_map.csv."""

    result = PrepareSurfaceResult()
    mapping_path = root / "survey_js" / "survey_qid_js_map.csv"
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    is_new_file = not mapping_path.exists()

    # Resolve per-survey column header hints (use cached inventory name if available).
    inv = load_cached_inventory_records()
    header_hints: dict[str, str] = {}
    for survey_id in per_survey_updates.keys():
        label = ""
        record = inv.get(survey_id) or {}
        label = str(record.get("name") or "").strip()
        label = _slugify_token(label) if label else ""
        header_hints[survey_id] = f"{survey_id}-{label}" if label else survey_id

    if is_new_file:
        fieldnames = ["js_file"] + [
            header_hints[sid] for sid in per_survey_updates.keys()
        ]
        rows: dict[str, dict[str, str]] = {}
    else:
        with mapping_path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            fieldnames = list(reader.fieldnames or [])
            if "js_file" not in fieldnames:
                raise RuntimeError(
                    f"Mapping CSV missing 'js_file' column: {mapping_path}"
                )
            rows = {}
            for row in reader:
                key = (row.get("js_file") or "").strip()
                if not key:
                    continue
                rows[key] = dict(row)

    # Ensure columns exist per survey (reuse existing survey columns if present).
    existing_fields = list(fieldnames)
    survey_cols: dict[str, str] = {}
    for survey_id, hint in header_hints.items():
        col = _resolve_js_column(existing_fields, survey_id, hint)
        survey_cols[survey_id] = col
        if col not in existing_fields:
            existing_fields.append(col)

    # Merge updates.
    created_rows = 0
    for survey_id, updates in per_survey_updates.items():
        col = survey_cols[survey_id]
        for js_file, qids in updates.items():
            row = rows.get(js_file)
            if row is None:
                row = {"js_file": js_file}
                rows[js_file] = row
                created_rows += 1
            existing = (row.get(col) or "").strip()
            existing_qids = (
                [q.strip() for q in existing.split(";") if q.strip()]
                if existing
                else []
            )
            merged = sorted(set(existing_qids).union(qids))
            if merged:
                row[col] = ";".join(merged)

    # Write back.
    ordered_keys = list(rows.keys())
    with mapping_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=existing_fields)
        writer.writeheader()
        for key in ordered_keys:
            writer.writerow(rows[key])

    result.created = 1 if (created_rows > 0 or is_new_file) else 0
    result.skipped = 0
    return result


def prepare_workspace(
    *,
    survey_ids: Sequence[str],
    yes: bool,
    interactive: bool,
    overwrite_js: bool,
    shared_js: bool,
    surfaces: set[str] | None,
    languages: list[str] | None,
) -> list[PrepareSurveyResult]:
    root = resolve_root(required=True) or Path.cwd()
    ensure_workspace_dirs(root)

    # NOTE: "workbook" is intentionally separate from "items". "items" hydration
    # includes cache + workbook, while "workbook" is a lightweight "ensure the
    # Excel workbook exists" operation (create missing only; no overwrite).
    allowed_surfaces = {"inventory", "items", "workbook", "translations", "eos", "js"}
    if surfaces is None:
        selected = set(allowed_surfaces)
    else:
        unknown = sorted(s for s in surfaces if s not in allowed_surfaces)
        if unknown:
            raise RuntimeError(f"Unknown --surfaces value(s): {', '.join(unknown)}")
        selected = set(surfaces)
        if "all" in selected:
            selected = set(allowed_surfaces)

    # Inventory is optional when user provides --survey-id and doesn't request it,
    # but required for many workflows and for a "complete" workspace by default.
    if "inventory" in selected:
        ok, _ = ensure_inventory_exists(yes=yes, interactive=interactive)
        if not ok:
            raise RuntimeError(
                "Inventory file missing. Next: run `qsync survey inventory` or re-run with --yes."
            )

    results: list[PrepareSurveyResult] = []
    js_updates: dict[str, dict[str, list[str]]] = {}

    for survey_id in survey_ids:
        r = PrepareSurveyResult(survey_id=survey_id)

        # Cached survey JSON (create if missing; do not refresh/overwrite by default).
        if {"items", "eos", "js"} & selected:
            try:
                existed = (
                    find_cached_survey_file(survey_id, in_backups=False) is not None
                )
                load_cached_survey(survey_id)
                if existed:
                    r.cache.skipped += 1
                else:
                    r.cache.created += 1
            except Exception as exc:
                r.cache.errors.append(str(exc))

        # Translations (languages drive workbook columns).
        langs_for_workbook: list[str] = []
        if "translations" in selected or (
            ("items" in selected or "workbook" in selected) and languages
        ):
            try:
                if languages:
                    langs = list(languages)
                else:
                    base_url, headers = get_client_config()
                    resp = send_api_request(
                        action="qsync.translations.prepare.languages.list",
                        method="GET",
                        base_url=base_url,
                        headers=headers,
                        path=f"surveys/{survey_id}/languages",
                        survey_id=survey_id,
                        log_event=False,
                        timeout=30,
                    )
                    result_payload = resp.json().get("result") or {}
                    langs_raw = (
                        result_payload.get("AvailableLanguages")
                        or result_payload.get("languages")
                        or []
                    )
                    langs = []
                    seen = set()
                    for item in (langs_raw if isinstance(langs_raw, list) else []):
                        code = str(item or "").strip()
                        if not code:
                            continue
                        code = "-".join(
                            part.strip().upper()
                            for part in code.split("-")
                            if part.strip()
                        )
                        if code and code not in seen:
                            seen.add(code)
                            langs.append(code)

                langs_for_workbook = list(langs)

                if "translations" in selected:
                    r.translations.skipped += 1
                    r.translations.notes.append(
                        "Translation maps are deprecated; translations live in the workbook."
                    )
            except Exception as exc:
                r.translations.errors.append(str(exc))

        # Excel workbook (create if missing; do not overwrite).
        if "items" in selected or "translations" in selected or "workbook" in selected:
            try:
                resolver = WorkbookResolver(root=root)
                xlsx_path = resolver.default_path(survey_id)
                if xlsx_path.exists():
                    r.workbook.skipped += 1
                else:
                    from .sync_core import init_survey_to_excel

                    init_survey_to_excel(
                        survey_id,
                        xlsx_path,
                        languages=langs_for_workbook or None,
                        # We're about to refresh the cache from the API anyway.
                        # Skip the extra "cache vs live" drift check to avoid an
                        # additional full survey-definition fetch per survey.
                        check_drift=False,
                    )
                    r.workbook.created += 1
            except Exception as exc:
                r.workbook.errors.append(str(exc))

        # EOS messages (create missing only).
        if "eos" in selected:
            try:
                from .eos_messages import extract_eos_message_refs
                from .eos_messages import write_library_message_to_disk
                from .eos_messages import find_message_contexts

                cache = load_cached_survey(survey_id)
                refs = extract_eos_message_refs(survey_id, cache.payload)
                missing_refs = []
                for ref in refs:
                    base = (
                        root
                        / "contents"
                        / "qualtrics_library_messages"
                        / ref.library_id
                        / ref.message_id
                    )
                    meta_path = base / "meta.json"
                    keys_path = base / "messages" / "_keys.json"
                    if meta_path.exists() and keys_path.exists():
                        r.eos.skipped += 1
                    else:
                        missing_refs.append(ref)

                if missing_refs:
                    base_url, headers = get_client_config()
                    contexts = find_message_contexts(
                        refs={(r.library_id, r.message_id) for r in missing_refs},
                        include_backups=False,
                    )
                    for ref in missing_refs:
                        resp = send_api_request(
                            action="qsync.eos.prepare.pull.message",
                            method="GET",
                            base_url=base_url,
                            headers=headers,
                            path=f"libraries/{ref.library_id}/messages/{ref.message_id}",
                            survey_id=survey_id,
                            log_event=False,
                            timeout=60,
                        )
                        write_library_message_to_disk(
                            library_id=ref.library_id,
                            message_id=ref.message_id,
                            api_payload=resp.json(),
                            contexts=contexts.get((ref.library_id, ref.message_id))
                            or [ref.to_context_dict()],
                        )
                        r.eos.created += 1
            except Exception as exc:
                r.eos.errors.append(str(exc))

        # JS surfaces + mapping updates (write missing only; no Qualtrics writes).
        if "js" in selected:
            try:
                js_surface, updates = hydrate_js_surfaces(
                    root=root,
                    survey_id=survey_id,
                    overwrite_js=overwrite_js,
                    shared_js=shared_js,
                )
                r.js = js_surface
                if updates:
                    js_updates[survey_id] = updates
            except Exception as exc:
                r.js.errors.append(str(exc))

        results.append(r)

    # Update mapping once per run (after all surveys).
    if js_updates and "js" in selected:
        mapping_result = update_js_mapping_csv(root=root, per_survey_updates=js_updates)
        for r in results:
            r.js_mapping = mapping_result

    return results
