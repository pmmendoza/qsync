"""Master dimension change detection for qsync sync orchestrator."""

from typing import Set

from ..pending_stage import load_pending, MasterPendingPayload
from ..survey_master import (
    load_master_csv,
    load_snapshot,
    compute_diff,
    validate_master_csv,
)
from .types import DimensionChanges


def detect_changes(survey_id: str) -> DimensionChanges:
    """Detect staged or unstaged master changes for a survey.

    Args:
        survey_id: Survey ID to check

    Returns:
        DimensionChanges with status, summary, and affected QIDs
    """
    # Check for staged changes (pending records)
    pending = load_pending(survey_id, "master")
    if pending and isinstance(pending.payload, MasterPendingPayload):
        changes = pending.payload.changes
        total_fields = sum(len(diff.get("changes", [])) for diff in changes)
        survey_ids = set(pending.payload.survey_ids)

        return DimensionChanges(
            dimension="master",
            has_changes=bool(total_fields),
            change_summary=f"✓ Staged: {total_fields} field(s) in {len(survey_ids)} survey(s)",
            affected_qids=set(),  # Master doesn't track QIDs
            status_kind="staged",
            edit_count=total_fields,
        )

    # Check for unstaged changes (CSV vs snapshot)
    try:
        headers, rows = load_master_csv()
    except Exception as e:
        return DimensionChanges(
            dimension="master",
            has_changes=False,
            change_summary="No CSV found",
            affected_qids=set(),
            error_detail=f"Failed to load master CSV: {e}",
            status_kind="error",
        )

    # Find row for this survey
    csv_row = None
    for row in rows:
        if row.get("SurveyID", "").strip() == survey_id:
            csv_row = row
            break

    if not csv_row:
        # Survey not in CSV
        return DimensionChanges(
            dimension="master",
            has_changes=False,
            change_summary="Not in CSV",
            affected_qids=set(),
            status_kind="none",
        )

    # Check if snapshot exists
    snapshot = load_snapshot(survey_id)
    if not snapshot:
        return DimensionChanges(
            dimension="master",
            has_changes=False,
            change_summary="No snapshot (run 'qsync survey master pull')",
            affected_qids=set(),
            safe_to_autofix=True,
            status_kind="none",
        )

    # Compute diff
    row_for_diff = dict(csv_row)
    validation_errors = validate_master_csv(headers, [row_for_diff])
    if validation_errors:
        return DimensionChanges(
            dimension="master",
            has_changes=False,
            change_summary="✗ Error",
            affected_qids=set(),
            error_detail="; ".join(validation_errors),
            status_kind="error",
        )

    try:
        diff = compute_diff(survey_id, row_for_diff)
    except Exception as e:
        return DimensionChanges(
            dimension="master",
            has_changes=False,
            change_summary="Error computing diff",
            affected_qids=set(),
            error_detail=str(e),
            status_kind="error",
        )

    # Check for errors in diff
    if diff.get("error"):
        return DimensionChanges(
            dimension="master",
            has_changes=False,
            change_summary="Error in diff",
            affected_qids=set(),
            error_detail=diff["error"],
            status_kind="error",
        )

    # Check for changes
    changes = diff.get("changes", [])
    if changes:
        return DimensionChanges(
            dimension="master",
            has_changes=True,
            change_summary=f"⚡ Unstaged: {len(changes)} field(s) changed",
            affected_qids=set(),
            status_kind="unstaged",
            edit_count=len(changes),
        )

    # No changes
    return DimensionChanges(
        dimension="master",
        has_changes=False,
        change_summary="No changes",
        affected_qids=set(),
        status_kind="none",
    )
