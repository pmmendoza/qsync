"""
Unified push safeguards for all qsync dimensions.

Provides centralized enforcement of survey lock checks and response count
validation before pushing changes to Qualtrics API.

Behavior matrix:
- Survey locked: BLOCK (hard fail) unless --allow-locked
- Counts unknown/stale: BLOCK (hard fail) unless --force-live
- Live responses (>0): BLOCK (hard fail) unless --force-live
- Preview-only responses: WARN unless --force-preview
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from qsync.push_logger import log_push_event
from qsync.push_policy import FRESHNESS_LIMIT, PushContext, load_push_context
from qsync.survey_lock import (
    ERROR_ID_SURVEY_LOCKED,
    SurveyLockedError,
    ensure_unlocked,
)
from qsync.survey_ref import format_survey_ref

DimensionType = Literal["items", "js", "translations", "eos"]


@dataclass
class SafeguardConfig:
    """Configuration for push safeguard enforcement."""

    survey_id: str
    dimension: DimensionType
    force_live: bool = False
    force_preview: bool = False
    allow_locked: bool = False
    auto_yes: bool = False


@dataclass
class SafeguardResult:
    """Result of safeguard enforcement."""

    push_context: PushContext
    warnings: list[str]
    blocked: bool = False
    block_reason: Optional[str] = None


def enforce_push_safeguards(config: SafeguardConfig) -> SafeguardResult:
    """
    Enforce unified push safeguards across all dimensions.

    Checks:
    1. Survey lock status
    2. Response counts (unknown/stale → block)
    3. Live responses → block unless --force-live
    4. Preview responses → warn unless --force-preview

    Args:
        config: Safeguard configuration

    Returns:
        SafeguardResult with push context and any warnings

    Raises:
        SystemExit: If safeguards block the push (locked survey, live responses, etc.)
    """
    warnings = []
    survey_ref = format_survey_ref(config.survey_id)

    # 1. Check survey lock status
    if not config.allow_locked:
        try:
            ensure_unlocked(config.survey_id)
        except (SurveyLockedError, RuntimeError) as exc:
            _log_blocked(
                survey_id=config.survey_id,
                dimension=config.dimension,
                error_id=ERROR_ID_SURVEY_LOCKED,
                message=str(exc),
            )
            raise SystemExit(f"[qsync:{config.dimension}] ERROR: {exc}") from exc

    # 2. Load push context (response counts)
    ctx = load_push_context(config.survey_id)
    summary = _format_counts(ctx)
    survey_ref = format_survey_ref(config.survey_id, getattr(ctx, "survey_name", None))

    # 3. Check if counts are unknown
    if ctx.counts_unknown and not config.force_live:
        raise SystemExit(
            f"[qsync:{config.dimension}] Unable to verify response counts "
            f"for {survey_ref}. Run 'make pull' (which refreshes inventory) "
            "or 'qsync survey inventory', then retry or pass --force-live "
            "after reviewing the survey manually."
        )

    # 4. Check live responses
    if ctx.response_count > 0:
        if not config.force_live:
            raise SystemExit(
                f"[qsync:{config.dimension}] {survey_ref} has {ctx.response_count} "
                "finished response(s). Re-run with --force-live after "
                "double-checking the diffs."
            )

        # Forced push with live responses - require confirmation
        warning = (
            f"WARNING: pushing {survey_ref} despite live responses -- {summary}. "
            "Next: double-check diffs and confirm this is safe."
        )
        print(f"[qsync:{config.dimension}] {warning}")
        warnings.append(warning)

        if not config.auto_yes and not _prompt_confirmation("Proceed with push?"):
            raise SystemExit(f"[qsync:{config.dimension}] Aborted by user.")

        return SafeguardResult(
            push_context=ctx,
            warnings=warnings,
        )

    # 5. Check preview-only responses
    preview_only = ctx.preview_count > 0

    if preview_only and not config.force_live and not config.force_preview:
        # Warn but don't block (harmonized behavior across all dimensions)
        warning = (
            f"WARNING: {survey_ref} has {ctx.preview_count} preview/test response(s). "
            f"Pushing will affect these responses. -- {summary}"
        )
        print(f"[qsync:{config.dimension}] {warning}")
        warnings.append(warning)

        if not config.auto_yes:
            prompt_msg = _dimension_preview_prompt(config.dimension)
            if not _prompt_confirmation(prompt_msg):
                raise SystemExit(f"[qsync:{config.dimension}] Aborted by user.")

    # 6. Note if inventory is stale
    if ctx.stale:
        stale_warning = f"NOTE: inventory timestamp is older than {FRESHNESS_LIMIT} for {survey_ref} -- {summary}"
        print(f"[qsync:{config.dimension}] {stale_warning}")
        warnings.append(stale_warning)

    return SafeguardResult(
        push_context=ctx,
        warnings=warnings,
    )


def _format_counts(ctx: PushContext) -> str:
    """Format response counts for display."""
    return ctx.describe_counts()


def _dimension_preview_prompt(dimension: DimensionType) -> str:
    """Get dimension-specific confirmation prompt for preview responses."""
    prompts = {
        "items": "Push item wording anyway?",
        "js": "Continue with JS push?",
        "translations": "Continue with translation push?",
        "eos": "Continue with EOS message push?",
    }
    return prompts.get(dimension, "Continue with push?")


def _prompt_confirmation(prompt: str) -> bool:
    """Prompt user for yes/no confirmation."""
    try:
        from .interactive_menu import confirm

        return confirm(prompt, default=True)
    except Exception:
        response = input(f"{prompt} [Y/n]: ").strip().lower()
        if not response:
            return True
        return response in ("y", "yes")


def _log_blocked(
    survey_id: str,
    dimension: str,
    error_id: str,
    message: str,
) -> None:
    """Log a blocked push event."""
    log_push_event(
        action=f"qsync.{dimension}.safeguards.blocked",
        method="LOCAL",
        path="push_safeguards:enforce_push_safeguards",
        survey_id=survey_id,
        status=None,
        error={"error_id": error_id, "message": message},
    )
