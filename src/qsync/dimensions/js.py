from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .types import DimensionChanges
from ..config import resolve_root
from ..pending_stage import (
    JsPendingPayload,
    PendingStagedChanges,
    clear_pending,
    load_pending,
    save_pending,
)
from ..scope_filter import ScopeFilter
from .js_preview import preview_differences
from .js_push import push_js_from_cache

logger = logging.getLogger(__name__)


def _mapping_csv_path() -> Path:
    root = resolve_root(required=False) or Path.cwd()
    return root / "survey_js" / "survey_qid_js_map.csv"


def detect_changes(survey_id: str) -> DimensionChanges:
    """Detect staged or unstaged JS changes for a survey."""
    pending = load_pending(survey_id, "js")
    if pending and isinstance(pending.payload, JsPendingPayload):
        # Validate that staged entries still have actual diffs
        # (staged changes may be obsolete if cache was updated)
        mapping_csv = _mapping_csv_path()
        if mapping_csv.exists():
            try:
                current_changes = preview_differences(
                    survey_id,
                    mapping_csv,
                    show_equal=False,
                    interactive=False,
                    verbose=False,
                    check_drift=False,
                )
                if current_changes:
                    # Staged entries are still valid
                    qids = set()
                    if pending.payload.entries:
                        for entry in pending.payload.entries:
                            if isinstance(entry, dict) and "qid" in entry:
                                qids.add(entry["qid"])
                    return DimensionChanges(
                        dimension="js",
                        has_changes=True,
                        change_summary=f"✓ Staged: {len(pending.payload.entries)} entries",
                        affected_qids=qids,
                    )
                else:
                    # Staged entries are obsolete - clear them
                    clear_pending(survey_id, "js")
            except Exception:
                # If validation fails, trust the pending stage
                qids = set()
                if pending.payload.entries:
                    for entry in pending.payload.entries:
                        if isinstance(entry, dict) and "qid" in entry:
                            qids.add(entry["qid"])
                return DimensionChanges(
                    dimension="js",
                    has_changes=True,
                    change_summary=f"✓ Staged: {len(pending.payload.entries)} entries",
                    affected_qids=qids,
                )

    mapping_csv = _mapping_csv_path()
    if mapping_csv.exists():
        try:
            changes = preview_differences(
                survey_id,
                mapping_csv,
                show_equal=False,
                interactive=False,
                verbose=False,
                check_drift=False,
            )
            changes = [c for c in changes if getattr(c, "status", None) != "unmapped"]
            if changes:
                qids = set(c.qid for c in changes if hasattr(c, "qid") and c.qid)
                return DimensionChanges(
                    dimension="js",
                    has_changes=True,
                    change_summary=f"⚡ Unstaged: {len(changes)} JS file(s) changed",
                    affected_qids=qids,
                )
        except Exception:
            pass

    return DimensionChanges(
        dimension="js",
        has_changes=False,
        change_summary="No changes",
        affected_qids=set(),
    )


def _select_stage_entries(
    survey_id: str,
    mapping_csv: Path,
    *,
    include_qids: set[str] | None,
    include_js: set[str] | None,
    scope_expr: str | None,
    include_match: bool,
    allow_diff: bool,
    create_missing: bool,
    interactive: bool,
) -> list[dict[str, str]]:
    results = preview_differences(
        survey_id=survey_id,
        mapping_csv=mapping_csv,
        show_equal=include_match,
        detailed=False,
        include_qids=include_qids,
        include_js=include_js,
        scope_expr=scope_expr,
        interactive=interactive,
        verbose=False,
        check_drift=False,
    )

    def _missing_js_block(detail: str) -> bool:
        return "no QuestionJS" in detail or "QuestionJSContent" in detail

    entries: list[dict[str, str]] = []
    for result in results:
        if not result.qid or not result.js_file:
            continue
        status = result.status
        if status in {"trash", "unused"}:
            if include_match:
                entries.append(
                    {"js_file": result.js_file, "qid": result.qid, "status": status}
                )
            continue
        if status == "unmapped":
            continue
        if status == "match":
            if not include_match:
                continue
        elif status == "comments-only":
            pass
        elif status == "diff":
            if not allow_diff:
                continue
        elif status == "missing":
            if not create_missing:
                continue
            if not _missing_js_block(result.detail or ""):
                continue
            status = "created"
        else:
            continue
        entries.append({"js_file": result.js_file, "qid": result.qid, "status": status})

    return entries


def stage(
    survey_id: str,
    *,
    scope: Optional[ScopeFilter] = None,
    allow_drift: bool = False,
    interactive: bool = True,
) -> bool:
    """Stage JS changes without mutating the cached survey JSON."""
    from ..drift_check import confirm_preview_drift
    from ..qualtrics_client import refresh_survey_cache
    from ..terminal_output import info

    def _update_cache() -> None:
        refresh_survey_cache(survey_id)
        info("[qsync:js]", "Refreshed cached survey definition from API.")

    confirm_preview_drift(
        survey_id=survey_id,
        dimension="js",
        allow_drift=allow_drift,
        interactive=interactive,
        update_cache=_update_cache,
    )

    mapping_csv = _mapping_csv_path()
    if not mapping_csv.exists():
        logger.warning(f"[sync:stage] JS mapping not found at {mapping_csv}")
        return False

    scope_expr = scope.expression if scope and scope.expression else None
    entries = _select_stage_entries(
        survey_id=survey_id,
        mapping_csv=mapping_csv,
        include_qids=None,
        include_js=None,
        scope_expr=scope_expr,
        include_match=True,
        allow_diff=True,
        create_missing=False,
        interactive=interactive,
    )

    if not entries:
        clear_pending(survey_id, "js")
        logger.info("[sync:stage] No JS changes to stage")
        return True

    record = PendingStagedChanges(
        survey_id=survey_id,
        dimension="js",
        payload=JsPendingPayload(entries=entries),
    )
    save_pending(record)
    return True


def push(
    survey_id: str,
    *,
    interactive: bool,
    force_live: bool,
    force_preview: bool,
    auto_yes: bool,
    allow_drift: bool,
    skip_publish: bool,
) -> bool:
    """Push staged (or all cached) JS changes to Qualtrics."""
    mapping_csv = _mapping_csv_path()
    qids_override = None
    pending_entries: list[dict[str, str]] | None = None

    pending = load_pending(survey_id, "js")
    if pending and isinstance(pending.payload, JsPendingPayload):
        pending_entries = [
            entry
            for entry in pending.payload.entries
            if isinstance(entry, dict)
            and entry.get("qid")
            and entry.get("js_file")
        ]
        qids_override = [
            entry.get("qid")
            for entry in pending.payload.entries
            if isinstance(entry, dict) and entry.get("qid")
        ] or None

    if pending is None:
        print("[sync:js] No staged JS changes found.")
        return True

    push_js_from_cache(
        survey_id=survey_id,
        mapping_csv=mapping_csv,
        qids_override=qids_override,
        pending_entries=pending_entries,
        force_live=force_live,
        force_preview=force_preview,
        interactive=interactive and not auto_yes,
        allow_drift=allow_drift,
        publish=not skip_publish,
    )

    from ..qualtrics_client import refresh_survey_cache
    from ..terminal_output import warn

    try:
        refresh_survey_cache(survey_id)
        clear_pending(survey_id, "js")
    except Exception as exc:
        warn(
            "[qsync:js]",
            f"Push succeeded but cache refresh failed: {exc}",
        )
    return True
