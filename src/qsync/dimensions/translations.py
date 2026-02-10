from __future__ import annotations

import logging
from typing import Optional


from .types import DimensionChanges
from ..pending_stage import TranslationsPendingPayload, load_pending
from ..scope_filter import ScopeFilter
from ..workbook_resolver import WorkbookResolver
from ..qualtrics_client import load_cached_survey
from .. import excel_io
from .translations_core import (
    apply_translations,
    push_translations,
    _resolve_stage_languages,
)
from .translations_workbook_extract import diff_workbook_vs_cache

logger = logging.getLogger(__name__)


def detect_unstaged_changes(
    survey_id: str,
    *,
    scope: Optional[ScopeFilter] = None,
) -> DimensionChanges:
    """Detect unstaged translation changes based on workbook vs cached survey."""
    resolver = WorkbookResolver()
    workbook_path = resolver.default_path(survey_id)
    if not workbook_path.exists():
        return DimensionChanges(
            dimension="translations",
            has_changes=False,
            change_summary="✗ Error",
            affected_qids=set(),
            error_detail=(
                f"Workbook not found at {workbook_path}. "
                f"Run: qsync items pull --survey-id {survey_id}"
            ),
            safe_to_autofix=True,
            status_kind="error",
            edit_count=0,
        )

    try:
        survey = load_cached_survey(survey_id)
        languages = _resolve_stage_languages(
            survey_id,
            survey.payload,
            workbook_path,
            explicit_languages=None,
            allow_empty=True,
        )
        if not languages:
            return DimensionChanges(
                dimension="translations",
                has_changes=False,
                change_summary="No translations (monolingual)",
                affected_qids=set(),
                status_kind="none",
                edit_count=0,
            )
        question_rows = excel_io.load_questions_from_workbook(workbook_path)
        changes = diff_workbook_vs_cache(
            survey.payload,
            workbook_path,
            languages,
            scope=scope,
            question_rows=question_rows,
        )
    except Exception as exc:
        return DimensionChanges(
            dimension="translations",
            has_changes=False,
            change_summary="✗ Error",
            affected_qids=set(),
            error_detail=f"Translation detection failed: {str(exc).split(chr(10))[0]}",
            safe_to_autofix=False,
            status_kind="error",
            edit_count=0,
        )

    if changes:
        affected_qids = {change.qid for change in changes if change.field != "Metadata"}
        return DimensionChanges(
            dimension="translations",
            has_changes=True,
            change_summary=f"⚡ Unstaged: {len(changes)} change(s)",
            affected_qids=affected_qids,
            status_kind="unstaged",
            edit_count=len(changes),
        )

    return DimensionChanges(
        dimension="translations",
        has_changes=False,
        change_summary="No changes",
        affected_qids=set(),
        status_kind="none",
        edit_count=0,
    )


def detect_changes(survey_id: str) -> DimensionChanges:
    """Detect staged or unstaged translation changes for a survey."""
    pending = load_pending(survey_id, "translations")
    if pending and isinstance(pending.payload, TranslationsPendingPayload):
        count = len(pending.payload.qids) if pending.payload.qids else 0
        return DimensionChanges(
            dimension="translations",
            has_changes=True,
            change_summary=f"✓ Staged: {count} QID(s)",
            affected_qids=set(pending.payload.qids or []),
            status_kind="staged",
            edit_count=count,
        )

    return detect_unstaged_changes(survey_id)


def stage(
    survey_id: str,
    *,
    scope: Optional[ScopeFilter] = None,
    allow_drift: bool = False,
    interactive: bool = True,
) -> bool:
    """Stage translation changes by applying workbook content to cached survey."""
    apply_translations(
        survey_id,
        None,
        scope=scope,
        allow_drift=allow_drift,
        interactive=interactive,
    )
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
    """Push staged translation changes to Qualtrics."""
    push_translations(
        survey_id=survey_id,
        languages=None,
        scope=scope,
        dry_run=False,
        force_live=force_live,
        force_preview=force_preview,
        interactive=interactive and not auto_yes,
        allow_drift=allow_drift,
        publish=not skip_publish,
        prefer_pending=prefer_pending,
    )
    return True
