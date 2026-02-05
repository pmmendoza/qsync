from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Optional
import os

from .types import DimensionChanges
from .items import _build_pending_payload_from_workbook
from .items_core import (
    _collect_embedded_data_changes,
    check_embedded_data_health,
    _format_field_list,
    push_staged_changes,
)
from ..pending_stage import (
    ItemsPendingPayload,
    PendingStagedChanges,
    clear_pending,
    load_pending,
    save_pending,
)
from .. import excel_io
from ..qualtrics_client import (
    find_cached_survey_file,
    load_cached_survey,
    refresh_survey_cache,
)
from ..scope_filter import ScopeFilter
from ..workbook_resolver import WorkbookResolver

logger = logging.getLogger(__name__)

_EDF_HEALTH_CACHE: dict[str, tuple[str, int, int, object]] = {}


@dataclass
class EdfRepairReport:
    survey_id: str
    workbook_path: Path
    dry_run: bool
    changed: bool
    rows_before: int
    rows_after: int
    rows_added: int
    rows_removed: int
    duplicate_rows_removed: int
    unchanged_rows: int
    extra_rows_preserved: int
    backup_path: Optional[Path]
    before_health: object
    after_health: object


def _health_signature(xlsx_path: Path) -> tuple[str, int, int]:
    stat = xlsx_path.stat()
    return (str(xlsx_path), stat.st_mtime_ns, stat.st_size)


def _load_embedded_health(survey_id: str, payload: dict, xlsx_path: Path):
    sig = _health_signature(xlsx_path)
    cached = _EDF_HEALTH_CACHE.get(survey_id)
    if cached and cached[:3] == sig:
        return cached[3]
    health = check_embedded_data_health(survey_id, payload, xlsx_path)
    _EDF_HEALTH_CACHE[survey_id] = (*sig, health)
    return health


def _format_edf_guidance(health, survey_id: str, has_local_edits: bool) -> str:
    issues: list[str] = []
    actions: list[str] = []

    if health.missing_fields:
        issues.append(f"missing fields: {_format_field_list(health.missing_fields)}")
        actions.append(f"Run: qsync items repair-edf --survey-id {survey_id}")
    if health.extra_fields:
        issues.append(f"extra fields: {_format_field_list(health.extra_fields)}")
        actions.append("Remove extra rows or re-run pull")
    if health.duplicate_fields:
        issues.append(
            f"duplicate fields: {_format_field_list(health.duplicate_fields)}"
        )
        actions.append(f"Run: qsync items repair-edf --survey-id {survey_id}")
    if health.ambiguous_fields:
        issues.append(
            f"ambiguous fields (missing FlowID): {_format_field_list(health.ambiguous_fields)}"
        )
        actions.append(
            "Repair may be incomplete for ambiguous rows; "
            f"use `qsync items pull --survey-id {survey_id}` if needed"
        )

    issue_summary = "; ".join([issue for issue in issues if issue]) or "unknown issue"
    action_summary = "; ".join(dict.fromkeys(actions)) if actions else "Repair required"

    message = (
        "Embedded_Data worksheet is inconsistent with the cached survey "
        f"({issue_summary}). {action_summary}."
    )
    if has_local_edits:
        message += (
            " Local edits detected (items/translations). "
            "Avoid full pulls until edits are staged/pushed; consider pushing safe dimensions first, then repair EDF."
        )
    return message


def _row_key(row: excel_io.EmbeddedDataRow) -> tuple[str, str]:
    return (str(row.flow_id or "").strip(), str(row.field or "").strip())


def _row_signature(row: excel_io.EmbeddedDataRow) -> tuple[str, str, int, str, str, str]:
    return (
        str(row.flow_id or "").strip(),
        str(row.field or "").strip(),
        int(row.flow_order or 0),
        str(row.value or ""),
        str(row.ed_type or "").strip(),
        str(row.written_by_qids or "").strip(),
    )


def _count_duplicate_rows(rows: list[excel_io.EmbeddedDataRow]) -> int:
    counts = Counter(_row_key(row) for row in rows if str(row.field or "").strip())
    return sum(max(0, count - 1) for count in counts.values())


def _build_repaired_workbook(
    *,
    survey_id: str,
    survey_payload: dict,
    xlsx_path: Path,
) -> Path:
    wb = excel_io.load_workbook(xlsx_path)
    expected_rows = excel_io.build_embedded_data_rows(survey_id, survey_payload)
    excel_io._init_embedded_data_sheet(wb, expected_rows)
    sheet = wb[excel_io.EMBEDDED_DATA_SHEET]
    excel_io._sort_sheet_by_flow_order(sheet)
    excel_io._format_embedded_data_sheet(sheet)

    fd, temp_name = tempfile.mkstemp(prefix="qsync_edf_repair_", suffix=".xlsx")
    os.close(fd)
    temp_path = Path(temp_name)
    wb.save(temp_path)
    return temp_path


def _create_workbook_backup(xlsx_path: Path, retain_backups: int) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = xlsx_path.with_name(
        f"{xlsx_path.stem}.embedded_data.{stamp}.bak{xlsx_path.suffix}"
    )
    shutil.copy2(xlsx_path, backup_path)

    if retain_backups > 0:
        pattern = f"{xlsx_path.stem}.embedded_data.*.bak{xlsx_path.suffix}"
        backups = sorted(
            xlsx_path.parent.glob(pattern),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        for stale in backups[retain_backups:]:
            try:
                stale.unlink()
            except OSError:
                logger.warning(f"[qsync:edf] Failed to remove stale backup {stale}")

    return backup_path


def repair_workbook(
    survey_id: str,
    *,
    xlsx_path: Path,
    dry_run: bool = False,
    refresh_cache: bool = False,
    retain_backups: int = 5,
) -> EdfRepairReport:
    """Repair only the Embedded_Data worksheet for a survey workbook.

    By default this command is cache-only and will refuse to run if no cached
    survey JSON exists locally. Use refresh_cache=True to force a live refresh.
    """

    xlsx_path = Path(xlsx_path)
    if not xlsx_path.exists():
        raise FileNotFoundError(f"Workbook not found: {xlsx_path}")

    if refresh_cache:
        refresh_survey_cache(survey_id)
        survey_payload = load_cached_survey(survey_id).payload
    else:
        cached = find_cached_survey_file(survey_id, in_backups=False)
        if not cached:
            raise RuntimeError(
                f"No cached survey JSON for {survey_id}. "
                "Run `qsync items pull --survey-id ...` or re-run with --refresh-cache."
            )
        survey_payload = load_cached_survey(survey_id).payload

    before_rows = excel_io.load_embedded_data_from_workbook(xlsx_path)
    before_health = check_embedded_data_health(survey_id, survey_payload, xlsx_path)
    before_signatures = Counter(_row_signature(row) for row in before_rows)
    before_duplicates = _count_duplicate_rows(before_rows)

    expected_rows = excel_io.build_embedded_data_rows(survey_id, survey_payload)
    expected_keys = {_row_key(row) for row in expected_rows}
    before_extra_keys = {
        key for key in {_row_key(row) for row in before_rows} if key not in expected_keys
    }

    temp_path: Optional[Path] = None
    backup_path: Optional[Path] = None
    try:
        temp_path = _build_repaired_workbook(
            survey_id=survey_id,
            survey_payload=survey_payload,
            xlsx_path=xlsx_path,
        )
        after_rows = excel_io.load_embedded_data_from_workbook(temp_path)
        after_health = check_embedded_data_health(survey_id, survey_payload, temp_path)
        after_signatures = Counter(_row_signature(row) for row in after_rows)
        after_duplicates = _count_duplicate_rows(after_rows)
        after_keys = {_row_key(row) for row in after_rows}

        changed = before_signatures != after_signatures
        rows_added = max(0, len(after_rows) - len(before_rows))
        rows_removed = max(0, len(before_rows) - len(after_rows))
        unchanged_rows = sum((before_signatures & after_signatures).values())
        duplicate_rows_removed = max(0, before_duplicates - after_duplicates)
        extra_rows_preserved = len(before_extra_keys & after_keys)

        if changed and not dry_run:
            try:
                backup_path = _create_workbook_backup(xlsx_path, retain_backups)
                shutil.copy2(temp_path, xlsx_path)
            except OSError as exc:
                raise RuntimeError(
                    f"Unable to write repaired workbook at {xlsx_path}: {exc}"
                ) from exc

        return EdfRepairReport(
            survey_id=survey_id,
            workbook_path=xlsx_path,
            dry_run=dry_run,
            changed=changed,
            rows_before=len(before_rows),
            rows_after=len(after_rows),
            rows_added=rows_added,
            rows_removed=rows_removed,
            duplicate_rows_removed=duplicate_rows_removed,
            unchanged_rows=unchanged_rows,
            extra_rows_preserved=extra_rows_preserved,
            backup_path=backup_path,
            before_health=before_health,
            after_health=after_health,
        )
    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                logger.warning(f"[qsync:edf] Failed to clean up temp workbook {temp_path}")


def _has_local_edits(survey_id: str) -> bool:
    try:
        from . import items as items_dimension
        from . import translations as translations_dimension

        if items_dimension.detect_changes(survey_id).has_changes:
            return True
        if translations_dimension.detect_unstaged_changes(survey_id).has_changes:
            return True
    except Exception:
        return False
    return False


def detect_unstaged_changes(survey_id: str) -> DimensionChanges:
    resolver = WorkbookResolver()
    xlsx_path = resolver.resolve(survey_id)
    if not xlsx_path.exists():
        return DimensionChanges(
            dimension="edf",
            has_changes=False,
            change_summary="No changes",
            affected_qids=set(),
            status_kind="none",
            edit_count=0,
        )

    try:
        survey = load_cached_survey(survey_id)
        health = _load_embedded_health(survey_id, survey.payload, xlsx_path)
        if not health.is_valid:
            return DimensionChanges(
                dimension="edf",
                has_changes=False,
                change_summary="No changes",
                affected_qids=set(),
                warning_detail=_format_edf_guidance(
                    health, survey_id=survey_id, has_local_edits=_has_local_edits(survey_id)
                ),
                status_kind="none",
                edit_count=0,
            )

        changes = _collect_embedded_data_changes(survey_id, survey.payload, xlsx_path)
        if changes:
            return DimensionChanges(
                dimension="edf",
                has_changes=True,
                change_summary=f"⚡ Unstaged: {len(changes)} field(s)",
                affected_qids=set(),
                status_kind="unstaged",
                edit_count=len(changes),
            )
    except Exception as exc:
        return DimensionChanges(
            dimension="edf",
            has_changes=False,
            change_summary="✗ Error",
            affected_qids=set(),
            error_detail=f"EDF detection failed: {str(exc).split(chr(10))[0]}",
            safe_to_autofix=False,
            status_kind="error",
            edit_count=0,
        )

    return DimensionChanges(
        dimension="edf",
        has_changes=False,
        change_summary="No changes",
        affected_qids=set(),
        status_kind="none",
        edit_count=0,
    )


def detect_changes(survey_id: str) -> DimensionChanges:
    pending = load_pending(survey_id, "edf")
    if pending and isinstance(pending.payload, ItemsPendingPayload):
        count = len(pending.payload.embedded_fields or [])
        if count:
            return DimensionChanges(
                dimension="edf",
                has_changes=True,
                change_summary=f"✓ Staged: {count} field(s)",
                affected_qids=set(),
                status_kind="staged",
                edit_count=count,
            )
        clear_pending(survey_id, "edf")

    return detect_unstaged_changes(survey_id)


def stage(
    survey_id: str,
    *,
    scope: Optional[ScopeFilter] = None,
    allow_drift: bool = False,
    interactive: bool = True,
) -> bool:
    del scope  # EDF is workbook-global in Stage 1.
    resolver = WorkbookResolver()
    xlsx_path = resolver.resolve(survey_id)
    if not xlsx_path.exists():
        logger.warning(f"[sync:stage] Workbook not found for {survey_id}")
        return False

    existing = load_pending(survey_id, "edf")
    existing_payload = (
        existing.payload
        if existing and isinstance(existing.payload, ItemsPendingPayload)
        else None
    )
    payload = _build_pending_payload_from_workbook(
        survey_id,
        xlsx_path,
        scope_expr=None,
        ignore_embedded=False,
        allow_drift=allow_drift,
        interactive=interactive,
        existing=existing_payload,
        include_non_embedded=False,
        include_embedded=True,
    )

    if not payload or not payload.embedded_fields:
        clear_pending(survey_id, "edf")
        logger.info("[sync:stage] No changes to stage for edf dimension")
        return True

    payload.qids = []
    payload.changes = []
    payload.structural_ops = []
    payload.structural_summary = {}
    payload.push_journal = {}

    record = PendingStagedChanges(
        survey_id=survey_id,
        dimension="edf",
        payload=payload,
        schema_version=2,
    )
    save_pending(record)
    return True


def push(
    survey_id: str,
    *,
    scope: Optional[ScopeFilter] = None,
    interactive: bool,
    force_live: bool,
    force_preview: bool,
    auto_yes: bool,
    allow_drift: bool,
    skip_publish: bool,
    prefer_pending: bool | None = None,
) -> bool:
    del scope  # EDF is workbook-global in Stage 1.
    del prefer_pending
    del auto_yes

    resolver = WorkbookResolver()
    xlsx_path = resolver.resolve(survey_id)
    if not xlsx_path.exists():
        print(f"[sync:edf] Workbook not found at {xlsx_path}.")
        return False

    pending = load_pending(survey_id, "edf")
    if not pending:
        ok = stage(
            survey_id,
            scope=None,
            allow_drift=allow_drift,
            interactive=interactive,
        )
        if not ok:
            return False
        pending = load_pending(survey_id, "edf")

    if not pending or not isinstance(pending.payload, ItemsPendingPayload):
        print("[sync:edf] No staged changes found")
        return False

    embedded_fields = list(pending.payload.embedded_fields or [])
    if not embedded_fields:
        print("[sync:edf] No EDF changes to push.")
        clear_pending(survey_id, "edf")
        return True

    push_staged_changes(
        survey_id=survey_id,
        qids=[],
        embedded_fields=embedded_fields,
        pending_changes=[],
        workbook=pending.payload.workbook or str(Path(xlsx_path)),
        filter_column=None,
        filter_value=None,
        publish=not skip_publish,
        force_live=force_live,
        force_preview=force_preview,
        interactive=interactive,
        allow_drift=allow_drift,
        skip_drift_check=True,
    )

    try:
        refresh_survey_cache(survey_id)
        clear_pending(survey_id, "edf")
    except Exception as exc:
        print(f"[sync:edf] WARNING: Push succeeded but cache refresh failed: {exc}")
        return True
    return True
