from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .. import excel_io
from .types import DimensionChanges
from ..pending_stage import (
    ItemsPendingPayload,
    PendingStagedChanges,
    clear_pending,
    load_pending,
    save_pending,
)
from ..scope_filter import ScopeFilter
from .items_core import (
    preview_changes,
    push_staged_changes,
    _collect_embedded_data_changes,
    ERROR_ID_EMBEDDED_DANGEROUS_SKIPPED,
)
from ..terminal_colors import colorize_unified_diff_lines
from ..workbook_resolver import WorkbookResolver
from ..drift_check import enforce_no_drift
from ..qualtrics_client import load_cached_survey

logger = logging.getLogger(__name__)


def _orphan_warning_detail(
    *,
    survey_id: str,
    report: excel_io.WorkbookOrphanRowsReport,
) -> str:
    return (
        f"Workbook has orphan item rows for {survey_id} "
        f"({report.counts_text()}; affected QIDs: {report.unknown_qids_text()}). "
        f"Run: qsync items pull --survey-id {survey_id} --prune-orphans"
    )


def _build_pending_payload_from_workbook(
    survey_id: str,
    xlsx_path: Path,
    *,
    scope_expr: str | None,
    filter_column: str | None = None,
    filter_value: str | None = None,
    include_qids: set[str] | None = None,
    include_tags: set[str] | None = None,
    ignore_embedded: bool,
    allow_drift: bool,
    interactive: bool,
    allow_dangerous: bool = False,
    existing: Optional[ItemsPendingPayload] = None,
    include_non_embedded: bool = True,
    include_embedded: bool = True,
) -> ItemsPendingPayload | None:
    enforce_no_drift(
        survey_id=survey_id,
        dimension="items",
        allow_drift=allow_drift,
        interactive=interactive,
    )

    changes = preview_changes(
        survey_id,
        xlsx_path,
        filter_column=filter_column,
        filter_value=filter_value,
        include_qids=include_qids,
        include_tags=include_tags,
        scope_expr=scope_expr,
        embedded_only=False,
        skip_embedded=ignore_embedded,
        check_drift=False,
        annotate_dirty=False,
        self_heal_system_columns=False,
    )
    non_embedded = (
        [c for c in changes if c.kind != "embedded"] if include_non_embedded else []
    )
    pending_changes: list[dict[str, object]] = []
    qids: set[str] = set()
    for change in non_embedded:
        qids.add(change.qid)
        pending_changes.append(
            {
                "kind": change.kind,
                "qid": change.qid,
                "choice_id": change.choice_id,
                "answer_id": change.answer_id,
                "old_html": change.old_html,
                "new_html": change.new_html,
                "data_export_tag": change.data_export_tag,
            }
        )

    embedded_pending: list[dict[str, object]] = []
    embedded_skipped: list[dict] = []
    if include_embedded and not ignore_embedded:
        survey = load_cached_survey(survey_id)
        embedded_changes = _collect_embedded_data_changes(
            survey_id, survey.payload, xlsx_path
        )
        for change in embedded_changes:
            if change.get("is_dangerous") and not allow_dangerous:
                embedded_skipped.append(change)
                continue
            row = change["row"]
            embedded_pending.append(
                {
                    "flow_id": row.flow_id or "",
                    "field": row.field,
                    "old_value": change.get("old_value"),
                    "new_value": change.get("new_value"),
                    "is_dangerous": bool(change.get("is_dangerous")),
                }
            )

    if embedded_skipped:
        skipped_fields = ", ".join(sorted({c["row"].field for c in embedded_skipped}))
        logger.warning(
            "[sync:stage] WARNING: %s: Dangerous embedded data default changes were skipped "
            "(fields: %s). Use `qsync items stage --allow-dangerous` if intended.",
            ERROR_ID_EMBEDDED_DANGEROUS_SKIPPED,
            skipped_fields,
        )

    if not pending_changes and not embedded_pending:
        return None

    structural_ops = (
        list(getattr(existing, "structural_ops", None) or [])
        if include_non_embedded
        else []
    )
    structural_summary = (
        dict(getattr(existing, "structural_summary", None) or {})
        if include_non_embedded
        else {}
    )
    push_journal = (
        dict(getattr(existing, "push_journal", None) or {})
        if include_non_embedded
        else {}
    )
    return ItemsPendingPayload(
        qids=sorted(qids),
        embedded_fields=embedded_pending,
        workbook=str(xlsx_path),
        filter_column=filter_column,
        filter_value=filter_value,
        structural_ops=structural_ops,
        structural_summary=structural_summary,
        push_journal=push_journal,
        changes=pending_changes,
    )


def _ensure_pending_changes(
    survey_id: str,
    pending: PendingStagedChanges,
    *,
    scope_expr: str | None,
    allow_drift: bool,
    interactive: bool,
    ignore_embedded: bool = False,
) -> PendingStagedChanges | None:
    if not isinstance(pending.payload, ItemsPendingPayload):
        return pending
    if pending.schema_version >= 2 and pending.payload.changes:
        return pending
    workbook_path = pending.payload.workbook
    if not workbook_path:
        return pending
    xlsx_path = Path(workbook_path)
    if not xlsx_path.exists():
        return pending
    rebuilt = _build_pending_payload_from_workbook(
        survey_id,
        xlsx_path,
        scope_expr=scope_expr,
        filter_column=pending.payload.filter_column,
        filter_value=pending.payload.filter_value,
        ignore_embedded=ignore_embedded,
        allow_drift=allow_drift,
        interactive=interactive,
        existing=pending.payload,
    )
    if not rebuilt:
        return pending
    pending.payload = rebuilt
    pending.schema_version = 2
    save_pending(pending)
    return pending


def detect_changes(survey_id: str) -> DimensionChanges:
    """Detect staged or unstaged items changes for a survey."""
    pending = load_pending(survey_id, "items")
    if pending and isinstance(pending.payload, ItemsPendingPayload):
        qids = set(pending.payload.qids or [])
        structural_ops = list(getattr(pending.payload, "structural_ops", None) or [])
        qids |= {
            str(op.get("qid") or "").strip() for op in structural_ops if op.get("qid")
        }
        return DimensionChanges(
            dimension="items",
            has_changes=bool(qids),
            change_summary=(
                f"✓ Staged: {len(qids)} QIDs"
                + (
                    f" ({len(structural_ops)} structural op(s))"
                    if structural_ops
                    else ""
                )
            ),
            affected_qids=qids,
            status_kind="staged",
            edit_count=len(qids),
        )

    resolver = WorkbookResolver()
    xlsx_path = resolver.resolve(survey_id)
    if xlsx_path.exists():
        orphan_warning: str | None = None
        try:
            survey = load_cached_survey(survey_id)
            orphan_report = excel_io.inspect_workbook_orphan_rows(
                survey_id=survey_id,
                survey_payload=survey.payload,
                xlsx_path=xlsx_path,
            )
            if orphan_report.has_orphans:
                orphan_warning = _orphan_warning_detail(
                    survey_id=survey_id,
                    report=orphan_report,
                )
        except Exception as exc:
            logger.debug(
                "[sync:items] Could not inspect workbook orphan rows for %s: %s",
                survey_id,
                exc,
            )

        changes = preview_changes(
            survey_id,
            xlsx_path,
            check_drift=False,
            skip_embedded=True,
            annotate_dirty=False,
            self_heal_system_columns=False,
        )
        if changes:
            qids = set(c.qid for c in changes if c.qid)
            return DimensionChanges(
                dimension="items",
                has_changes=True,
                change_summary=f"⚡ Unstaged: {len(changes)} change(s) in {len(qids)} QID(s)",
                affected_qids=qids,
                warning_detail=orphan_warning,
                safe_to_autofix=bool(orphan_warning),
                status_kind="unstaged",
                edit_count=len(qids),
            )
        if orphan_warning:
            return DimensionChanges(
                dimension="items",
                has_changes=False,
                change_summary="Workbook has orphan rows",
                affected_qids=set(),
                warning_detail=orphan_warning,
                safe_to_autofix=True,
                status_kind="none",
                edit_count=0,
            )

    return DimensionChanges(
        dimension="items",
        has_changes=False,
        change_summary="No changes",
        affected_qids=set(),
        status_kind="none",
        edit_count=0,
    )


def stage(
    survey_id: str,
    *,
    scope: Optional[ScopeFilter] = None,
    ignore_embedded: bool = False,
    allow_drift: bool = False,
    interactive: bool = True,
    allow_dangerous: bool = False,
) -> bool:
    """Stage items changes into pending cache."""
    resolver = WorkbookResolver()
    xlsx_path = resolver.resolve(survey_id)

    if not xlsx_path.exists():
        logger.warning(f"[sync:stage] Workbook not found for {survey_id}")
        return False

    scope_expr = scope.expression if scope and scope.expression else None
    existing = load_pending(survey_id, "items")
    existing_payload = (
        existing.payload
        if existing and isinstance(existing.payload, ItemsPendingPayload)
        else None
    )
    payload = _build_pending_payload_from_workbook(
        survey_id,
        xlsx_path,
        scope_expr=scope_expr,
        ignore_embedded=ignore_embedded,
        allow_drift=allow_drift,
        interactive=interactive,
        allow_dangerous=allow_dangerous,
        existing=existing_payload,
    )

    if not payload:
        logger.info("[sync:stage] No changes to stage for items dimension")
        clear_pending(survey_id, "items")
        return True

    record = PendingStagedChanges(
        survey_id=survey_id,
        dimension="items",
        payload=payload,
        schema_version=2,
    )
    save_pending(record)
    return True


def push(
    survey_id: str,
    *,
    scope: Optional[ScopeFilter],
    interactive: bool,
    force_live: bool,
    force_preview: bool,
    auto_yes: bool,
    allow_drift: bool,
    skip_publish: bool,
    prefer_pending: bool | None = None,
    ignore_embedded: bool = False,
    allow_delete: bool = False,
) -> bool:
    """Push staged items changes (re-stage from Excel if needed)."""
    resolver = WorkbookResolver()
    xlsx_path = resolver.resolve(survey_id)

    if not xlsx_path.exists():
        print(f"[sync:items] Workbook not found at {xlsx_path}.")
        return False

    scope_expr = scope.expression if scope and scope.expression else None
    workbook_diffs = preview_changes(
        survey_id,
        xlsx_path,
        scope_expr=scope_expr,
        check_drift=False,
        annotate_dirty=False,
        self_heal_system_columns=False,
        skip_embedded=ignore_embedded,
    )

    pending = load_pending(survey_id, "items")
    structural_ops: list[dict] = []
    if pending and isinstance(pending.payload, ItemsPendingPayload):
        structural_ops = list(getattr(pending.payload, "structural_ops", None) or [])
    if structural_ops:
        # Structural ops are staged via `qsync items edit` and must not be silently cleared.
        # Delete ops require explicit opt-in so sync users do not accidentally remove options/subitems.
        has_deletes = any(
            op.get("op") in {"choice_remove", "answer_remove"} for op in structural_ops
        )
        if has_deletes and not allow_delete:
            print(
                "[sync:items] Structural deletes are staged. "
                "Run `qsync items push --survey-id ... --allow-delete` to proceed."
            )
            return False

        from .items_structural import push_structural_ops

        def _save_journal(updated: dict) -> None:
            if pending and isinstance(pending.payload, ItemsPendingPayload):
                pending.payload.push_journal = dict(updated or {})
                save_pending(pending)

        push_structural_ops(
            survey_id=survey_id,
            payload={},
            structural_ops=structural_ops,
            push_journal=dict(getattr(pending.payload, "push_journal", {}) or {}),
            interactive=interactive and not auto_yes,
            allow_delete=allow_delete,
            force_live=force_live,
            force_preview=force_preview,
            publish=(not skip_publish)
            and not bool(workbook_diffs)
            and not bool(
                list(getattr(pending.payload, "qids", None) or [])
                or list(getattr(pending.payload, "embedded_fields", None) or [])
            ),
            dry_run=False,
            refresh_cache=True,
            save_journal_cb=_save_journal,
        )

        # Clear structural ops after success; keep any other staged payload for later steps.
        if pending and isinstance(pending.payload, ItemsPendingPayload):
            pending.payload.structural_ops = []
            pending.payload.push_journal = {}
            save_pending(pending)
        # Do not clear the whole pending record here; fall through to workbook diffs / normal pushes.

    if workbook_diffs and pending:
        import sys

        decision = prefer_pending
        if decision is None:
            if interactive and sys.stdin.isatty():
                from ..interactive_menu import select_from_list

                choices = [
                    "Use staged changes (ignore Excel)",
                    "Restage from Excel (overwrite pending)",
                    "↩ Abort push",
                ]
                selection = select_from_list(
                    message="Excel differs from cache and staged changes exist. Which should be pushed?",
                    choices=choices,
                    default=choices[1],  # default is “restage” (legacy behavior)
                )
                if selection is None or selection.startswith("↩"):
                    decision = None
                elif selection.startswith("Use staged"):
                    decision = True
                else:
                    decision = False
            else:
                decision = False

        if decision is True and structural_ops:
            print(
                "[sync:items] ⚠️  Staged structural ops detected; ignoring `--use-pending` and re-staging from Excel."
            )
            decision = False

        if decision is True:
            print(
                "[sync:items] Using staged changes and ignoring workbook differences."
            )
            workbook_diffs = []
        elif decision is None:
            raise SystemExit("[qsync:items] Aborted by user.")

    if workbook_diffs:
        if pending:
            print(
                "[sync:items] ⚠️  Excel differs from cache, re-staging from current Excel "
                "(overriding stale staging)..."
            )

        # Keep the existing unified diff output as the default (high signal, easy to copy/paste),
        # but offer an optional side-by-side "before/after" view in interactive Rich terminals.
        detailed_choice = "Detailed diffs (unified; cached vs Excel)"
        summary_choice = "Before/after panels (summary; cached vs Excel)"
        both_choice = "Both"
        skip_choice = "↩ Continue without showing diffs"
        view_mode = detailed_choice
        try:
            from ..rich_support import should_use_rich

            if interactive and should_use_rich():
                from ..interactive_menu import select_from_list

                view_mode = select_from_list(
                    message="How do you want to view workbook diffs?",
                    choices=[
                        detailed_choice,
                        summary_choice,
                        both_choice,
                        skip_choice,
                    ],
                    default=detailed_choice,
                ) or detailed_choice
        except Exception:
            view_mode = detailed_choice

        if view_mode.startswith("↩") or "Continue without" in view_mode:
            view_mode = skip_choice

        def _before_after_from_unified(diff_lines: list[str] | None) -> tuple[str, str]:
            removed: list[str] = []
            added: list[str] = []
            for raw in diff_lines or []:
                line = str(raw)
                if not line:
                    continue
                if line.startswith(("---", "+++", "@@")):
                    continue
                if line.startswith("-") and not line.startswith("---"):
                    removed.append(line[1:])
                elif line.startswith("+") and not line.startswith("+++"):
                    added.append(line[1:])
            before = "\n".join(removed).strip()
            after = "\n".join(added).strip()
            return before or "(no removed lines)", after or "(no added lines)"

        if view_mode.startswith("Before/after") or view_mode == both_choice:
            try:
                from ..terminal_output import print_panels_in_columns

                print("[sync:items] Before/after (cached vs Excel):")
                for idx, change in enumerate(workbook_diffs[:20], 1):
                    print("-" * 80)
                    title = f"{change.kind.upper()} qid={change.qid}"
                    if getattr(change, "choice_id", None) is not None:
                        title += f", choice_id={change.choice_id}"
                    if getattr(change, "answer_id", None) is not None:
                        title += f", answer_id={change.answer_id}"
                    print(title)
                    before, after = _before_after_from_unified(getattr(change, "diff_lines", None))
                    print_panels_in_columns(
                        [
                            ("Cached", before, "yellow"),
                            ("Excel", after, "green"),
                        ]
                    )
                if len(workbook_diffs) > 20:
                    print(
                        f"[sync:items] (Showing first 20 changes; total {len(workbook_diffs)}.)"
                    )
            except Exception:
                # Fall back to unified diffs below.
                pass

        if not (view_mode.startswith("Detailed diffs") or view_mode == both_choice):
            view_mode = skip_choice

        if view_mode != skip_choice:
            print("[sync:items] Detailed diffs (cached vs Excel):")
            for change in workbook_diffs:
                print("-" * 80)
                if change.kind == "embedded":
                    flow = f", flow_id={change.flow_id}" if change.flow_id else ""
                    header = (
                        f"{change.kind.upper()} field={change.field or change.qid}{flow}"
                    )
                else:
                    header = f"{change.kind.upper()} qid={change.qid}"
                if change.choice_id is not None:
                    header += f", choice_id={change.choice_id}"
                if change.answer_id is not None:
                    header += f", answer_id={change.answer_id}"
                print(header)
                diff_lines = change.diff_lines or []
                if diff_lines:
                    for line in colorize_unified_diff_lines(diff_lines):
                        print("  " + line)
                else:
                    old_html = (change.old_html or "").strip()
                    new_html = (change.new_html or "").strip()
                    print("  OLD:", old_html)
                    print("  NEW:", new_html)

        print(f"[sync:items] Staging {len(workbook_diffs)} change(s) from Excel...")
        existing_payload = (
            pending.payload
            if pending and isinstance(pending.payload, ItemsPendingPayload)
            else None
        )
        payload = _build_pending_payload_from_workbook(
            survey_id,
            xlsx_path,
            scope_expr=scope_expr,
            filter_column=existing_payload.filter_column if existing_payload else None,
            filter_value=existing_payload.filter_value if existing_payload else None,
            ignore_embedded=ignore_embedded,
            allow_drift=allow_drift,
            interactive=interactive and not auto_yes,
            existing=existing_payload,
        )
        if not payload:
            print("[sync:items] No stageable changes after staging; skipping.")
            clear_pending(survey_id, "items")
            return True
        record = PendingStagedChanges(
            survey_id=survey_id,
            dimension="items",
            payload=payload,
            schema_version=2,
        )
        save_pending(record)
        pending = load_pending(survey_id, "items")
    elif not workbook_diffs and not pending:
        print("[sync:items] No differences between Excel and cached survey.")
        return True

    if not pending or not isinstance(pending.payload, ItemsPendingPayload):
        # If we just pushed structural ops and there are no workbook diffs, this is OK.
        if not workbook_diffs:
            return True
        print("[sync:items] No staged changes found")
        return False

    pending = _ensure_pending_changes(
        survey_id,
        pending,
        scope_expr=scope_expr,
        allow_drift=allow_drift,
        interactive=interactive and not auto_yes,
        ignore_embedded=ignore_embedded,
    )
    if not pending or not isinstance(pending.payload, ItemsPendingPayload):
        print("[sync:items] No staged changes found")
        return False

    push_staged_changes(
        survey_id=survey_id,
        qids=list(pending.payload.qids or []),
        embedded_fields=list(pending.payload.embedded_fields or []),
        pending_changes=list(pending.payload.changes or []),
        workbook=pending.payload.workbook,
        filter_column=pending.payload.filter_column,
        filter_value=pending.payload.filter_value,
        publish=not skip_publish,
        force_live=force_live,
        force_preview=force_preview,
        interactive=interactive and not auto_yes,
        allow_drift=allow_drift,
        skip_drift_check=True,
    )
    from ..qualtrics_client import refresh_survey_cache

    try:
        refresh_survey_cache(survey_id)
        clear_pending(survey_id, "items")
    except Exception as exc:
        print(f"[sync:items] WARNING: Push succeeded but cache refresh failed: {exc}")
        return True
    return True
