"""Flow dimension module for survey flow synchronization.

This module provides the standard dimension interface (pull, detect_changes,
preview, stage, push) for survey flow management. It enables version-controlled
editing of survey branching logic, block ordering, and flow structure.

Usage:
    qsync flow pull --survey-id SV_xxx
    qsync flow preview --survey-id SV_xxx
    qsync flow stage --survey-id SV_xxx
    qsync flow push --survey-id SV_xxx
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Optional

from .types import DimensionChanges
from .flow_yaml import flow_to_yaml, yaml_to_flow
from .flow_diff import FlowChange, diff_flows, format_diff_for_display, format_diff_summary
from .flow_validate import validate_flow, validate_yaml_structure, FlowValidationError
from ..pending_stage import (
    FlowPendingPayload,
    PendingStagedChanges,
    clear_pending,
    load_pending,
    save_pending,
)
from ..config import resolve_root, resolve_scoped_dir


logger = logging.getLogger(__name__)


def _workspace_root() -> Path:
    """Get workspace root directory."""
    return resolve_root(required=False) or Path.cwd()


def _flow_dir(survey_id: str) -> Path:
    """Get the flow directory for a survey.

    Returns:
        Path to surveys/flow/{survey_id}/ (account-scoped when QSYNC_ACCOUNT is set)
    """
    root = _workspace_root()
    surveys_dir = resolve_scoped_dir("surveys", root=root)
    return surveys_dir / "flow" / survey_id


def _yaml_path(survey_id: str) -> Path:
    """Get the YAML file path for a survey."""
    return _flow_dir(survey_id) / "flow.yaml"


def _baseline_path(survey_id: str) -> Path:
    """Get the baseline JSON file path for a survey."""
    return _flow_dir(survey_id) / "baseline.json"


def pull(survey_id: str, *, force: bool = False) -> Path:
    """Pull flow from Qualtrics and save as YAML.

    Args:
        survey_id: Survey ID to pull flow from
        force: If True, overwrite existing YAML even if it has local changes

    Returns:
        Path to the created YAML file

    Raises:
        FileExistsError: If YAML exists with local changes and force=False
    """
    from ..qualtrics_client import load_cached_survey

    # Load cached survey (downloads if not cached)
    cache = load_cached_survey(survey_id)
    flow = cache.payload.get("result", {}).get("SurveyFlow", {})
    blocks = cache.payload.get("result", {}).get("Blocks", {})
    questions = cache.payload.get("result", {}).get("Questions", {})

    flow_dir = _flow_dir(survey_id)
    yaml_path = _yaml_path(survey_id)
    baseline_path = _baseline_path(survey_id)

    # Check for existing changes
    if yaml_path.exists() and baseline_path.exists() and not force:
        # Check if YAML differs from baseline
        try:
            import yaml as yaml_lib
            yaml_content = yaml_path.read_text(encoding="utf-8")
            yaml_data = yaml_lib.safe_load(yaml_content)
            edited_flow = yaml_to_flow(yaml_content)
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

            changes = diff_flows(baseline, edited_flow)
            if changes:
                raise FileExistsError(
                    f"Flow YAML has local changes ({len(changes)} change(s)). "
                    f"Use --force to overwrite or stage/push changes first."
                )
        except FileExistsError:
            raise
        except Exception:
            pass  # If we can't check, proceed with pull

    # Create directory structure
    flow_dir.mkdir(parents=True, exist_ok=True)

    # Convert to YAML with annotations
    yaml_content = flow_to_yaml(flow, survey_id, blocks, questions)

    # Save YAML and baseline
    yaml_path.write_text(yaml_content, encoding="utf-8")
    baseline_path.write_text(json.dumps(flow, indent=2), encoding="utf-8")

    logger.info(f"[sync:flow] Pulled flow to {yaml_path}")
    return yaml_path


def detect_changes(survey_id: str) -> DimensionChanges:
    """Detect staged or unstaged flow changes for a survey.

    Returns:
        DimensionChanges with change status and summary
    """
    # Check for staged changes first
    pending = load_pending(survey_id, "flow")
    if pending and isinstance(pending.payload, FlowPendingPayload):
        num_changes = len(pending.payload.changes)
        return DimensionChanges(
            dimension="flow",
            has_changes=True,
            change_summary=f"Staged: {num_changes} change(s)",
            affected_qids=set(),  # Flow doesn't affect QIDs directly
        )

    # Check for unstaged changes (YAML differs from baseline)
    yaml_path = _yaml_path(survey_id)
    baseline_path = _baseline_path(survey_id)

    if not yaml_path.exists() or not baseline_path.exists():
        return DimensionChanges(
            dimension="flow",
            has_changes=False,
            change_summary="Not initialized",
            affected_qids=set(),
            error_detail=f"Run: qsync flow pull --survey-id {survey_id}",
            safe_to_autofix=True,
        )

    try:
        yaml_content = yaml_path.read_text(encoding="utf-8")
        edited = yaml_to_flow(yaml_content)
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

        changes = diff_flows(baseline, edited)

        if changes:
            summary = format_diff_summary(changes)
            return DimensionChanges(
                dimension="flow",
                has_changes=True,
                change_summary=f"Unstaged: {summary}",
                affected_qids=set(),
            )

        return DimensionChanges(
            dimension="flow",
            has_changes=False,
            change_summary="No changes",
            affected_qids=set(),
        )

    except Exception as e:
        return DimensionChanges(
            dimension="flow",
            has_changes=False,
            change_summary="Error detecting changes",
            affected_qids=set(),
            error_detail=str(e),
        )


def preview(
    survey_id: str,
    *,
    verbose: bool = False,
    visual: bool = False,
    validate: bool = True,
) -> list[FlowChange]:
    """Preview flow changes (YAML vs baseline).

    Args:
        survey_id: Survey ID to preview
        verbose: If True, print detailed diff output
        visual: If True, generate Mermaid diagrams (not yet implemented)
        validate: If True, check for invalid references (deleted QIDs, missing blocks)

    Returns:
        List of FlowChange objects describing the differences
    """
    from ..qualtrics_client import load_cached_survey

    yaml_path = _yaml_path(survey_id)
    baseline_path = _baseline_path(survey_id)

    if not yaml_path.exists():
        print(f"[sync:flow] No flow.yaml found. Run: qsync flow pull --survey-id {survey_id}")
        return []

    if not baseline_path.exists():
        print(f"[sync:flow] No baseline found. Run: qsync flow pull --survey-id {survey_id}")
        return []

    try:
        yaml_content = yaml_path.read_text(encoding="utf-8")
        edited = yaml_to_flow(yaml_content)
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

        changes = diff_flows(baseline, edited)

        if not changes:
            print("[sync:flow] No changes detected")
        else:
            print(f"[sync:flow] {format_diff_summary(changes)}:")
            for line in format_diff_for_display(changes, verbose=verbose):
                print(line)

        # Validate references even if no changes (catch deleted QIDs early)
        if validate:
            try:
                cache = load_cached_survey(survey_id)
                blocks = cache.payload.get("result", {}).get("Blocks", {})
                questions = cache.payload.get("result", {}).get("Questions", {})
                validate_flow(edited, survey_id, blocks, questions)
            except FlowValidationError as e:
                print(f"\n[sync:flow] WARNING - Invalid references detected:")
                for err in e.errors:
                    print(f"  ! {err}")
                print("\nThese issues will block push. Fix the flow or restore deleted items.")

        if visual:
            print("\n[sync:flow] Visual diff (Mermaid) not yet implemented")

        return changes

    except FlowValidationError as e:
        print(f"[sync:flow] Validation error in YAML:\n{e}")
        return []
    except Exception as e:
        print(f"[sync:flow] Error previewing changes: {e}")
        return []


def stage(
    survey_id: str,
    *,
    allow_drift: bool = False,
    interactive: bool = True,
) -> bool:
    """Stage flow changes into pending cache.

    Args:
        survey_id: Survey ID to stage
        allow_drift: If True, allow staging even if remote has drifted
        interactive: If True, prompt for confirmation on drift

    Returns:
        True if staging succeeded, False otherwise
    """
    from ..drift_check import enforce_no_drift

    yaml_path = _yaml_path(survey_id)
    baseline_path = _baseline_path(survey_id)

    if not yaml_path.exists():
        print(f"[sync:flow] No flow.yaml found. Run: qsync flow pull --survey-id {survey_id}")
        return False

    if not baseline_path.exists():
        print(f"[sync:flow] No baseline found. Run: qsync flow pull --survey-id {survey_id}")
        return False

    # Check for drift
    try:
        enforce_no_drift(
            survey_id=survey_id,
            dimension="flow",
            allow_drift=allow_drift,
            interactive=interactive,
        )
    except Exception as e:
        print(f"[sync:flow] Drift check failed: {e}")
        return False

    # Load and validate YAML
    try:
        yaml_content = yaml_path.read_text(encoding="utf-8")
        import yaml as yaml_lib
        yaml_data = yaml_lib.safe_load(yaml_content)
        validate_yaml_structure(yaml_data)
        edited = yaml_to_flow(yaml_content)
    except FlowValidationError as e:
        print(f"[sync:flow] YAML validation error:\n{e}")
        return False
    except Exception as e:
        print(f"[sync:flow] Error parsing YAML: {e}")
        return False

    # Load baseline and compute changes
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    changes = diff_flows(baseline, edited)

    if not changes:
        clear_pending(survey_id, "flow")
        print("[sync:flow] No changes to stage")
        return True

    # Validate the converted flow against survey
    try:
        from ..qualtrics_client import load_cached_survey
        cache = load_cached_survey(survey_id)
        blocks = cache.payload.get("result", {}).get("Blocks", {})
        questions = cache.payload.get("result", {}).get("Questions", {})
        validate_flow(edited, survey_id, blocks, questions)
    except FlowValidationError as e:
        print(f"[sync:flow] Flow validation error:\n{e}")
        return False

    # Compute baseline hash for integrity check
    baseline_hash = hashlib.sha256(
        json.dumps(baseline, sort_keys=True).encode()
    ).hexdigest()

    # Save pending
    payload = FlowPendingPayload(
        flow_yaml_path=str(yaml_path),
        baseline_hash=baseline_hash,
        changes=[c.to_dict() for c in changes],
    )
    record = PendingStagedChanges(
        survey_id=survey_id,
        dimension="flow",
        payload=payload,
    )
    save_pending(record)

    print(f"[sync:flow] Staged {format_diff_summary(changes)}")
    return True


def push(
    survey_id: str,
    *,
    interactive: bool = True,
    force_live: bool = False,
    force_preview: bool = False,
    auto_yes: bool = False,
    allow_drift: bool = False,
    skip_publish: bool = False,
) -> bool:
    """Push staged flow changes to Qualtrics.

    Args:
        survey_id: Survey ID to push
        interactive: If True, prompt for confirmation
        force_live: If True, allow push to survey with responses
        force_preview: If True, show preview before push
        auto_yes: If True, skip confirmation prompts
        allow_drift: If True, allow push even if remote has drifted
        skip_publish: If True, don't publish after push

    Returns:
        True if push succeeded, False otherwise
    """
    from ..qualtrics_client import (
        load_cached_survey,
        push_survey_flow,
        refresh_survey_cache,
        ensure_backup,
    )
    from ..push_safeguards import enforce_push_safeguards

    # Load pending changes
    pending = load_pending(survey_id, "flow")
    if not pending or not isinstance(pending.payload, FlowPendingPayload):
        print("[sync:flow] No staged flow changes found")
        return True

    # Verify YAML file still exists
    yaml_path = Path(pending.payload.flow_yaml_path)
    if not yaml_path.exists():
        print(f"[sync:flow] YAML file not found: {yaml_path}")
        clear_pending(survey_id, "flow")
        return False

    # Load and validate
    try:
        yaml_content = yaml_path.read_text(encoding="utf-8")
        edited_flow = yaml_to_flow(yaml_content)
    except Exception as e:
        print(f"[sync:flow] Error loading YAML: {e}")
        return False

    # Verify YAML hasn't changed since staging (integrity check)
    # Compare staged changes with current changes to detect modifications
    staged_changes = pending.payload.changes
    try:
        baseline_path = _baseline_path(survey_id)
        if baseline_path.exists():
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            current_changes = diff_flows(baseline, edited_flow)
            current_change_ids = {c.node_id for c in current_changes}
            staged_change_ids = {c.get("node_id") for c in staged_changes}

            if current_change_ids != staged_change_ids:
                print(
                    "[sync:flow] WARNING: YAML has been modified since staging. "
                    f"Staged: {len(staged_changes)} changes, Current: {len(current_changes)} changes"
                )
                if interactive and not auto_yes:
                    confirm = input("Continue with current changes? [y/N] ").strip().lower()
                    if confirm != "y":
                        print("[sync:flow] Aborted - re-stage with 'qsync flow stage'")
                        return False
                # Update to use current changes
                staged_changes = [c.to_dict() for c in current_changes]
    except Exception as e:
        logger.warning(f"[sync:flow] Could not verify YAML consistency: {e}")

    # Validate flow structure
    try:
        cache = load_cached_survey(survey_id)
        blocks = cache.payload.get("result", {}).get("Blocks", {})
        questions = cache.payload.get("result", {}).get("Questions", {})
        validate_flow(edited_flow, survey_id, blocks, questions)
    except FlowValidationError as e:
        print(f"[sync:flow] Validation error:\n{e}")
        return False

    # Check push safeguards
    try:
        enforce_push_safeguards(
            survey_id=survey_id,
            force_live=force_live,
            interactive=interactive and not auto_yes,
        )
    except Exception as e:
        print(f"[sync:flow] Push safeguard failed: {e}")
        return False

    # Verify baseline hasn't changed (integrity check)
    baseline_path = _baseline_path(survey_id)
    if baseline_path.exists():
        current_baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        current_hash = hashlib.sha256(
            json.dumps(current_baseline, sort_keys=True).encode()
        ).hexdigest()
        if current_hash != pending.payload.baseline_hash:
            print(
                "[sync:flow] WARNING: Baseline has changed since staging. "
                "Consider re-staging with 'qsync flow stage'."
            )
            if interactive and not auto_yes:
                confirm = input("Continue anyway? [y/N] ").strip().lower()
                if confirm != "y":
                    print("[sync:flow] Aborted")
                    return False

    # Confirm with user
    if interactive and not auto_yes:
        changes = pending.payload.changes
        print(f"[sync:flow] About to push {len(changes)} change(s):")
        for change_dict in changes[:5]:
            change = FlowChange.from_dict(change_dict)
            symbol = {"added": "+", "removed": "-", "modified": "~"}.get(
                change.change_type, "?"
            )
            print(f"  {symbol} {change.node_type} [{change.node_id}]: {change.description}")
        if len(changes) > 5:
            print(f"  ... and {len(changes) - 5} more")

        confirm = input("Proceed? [y/N] ").strip().lower()
        if confirm != "y":
            print("[sync:flow] Aborted")
            return False

    # Create backup
    try:
        ensure_backup(survey_id)
    except Exception as e:
        logger.warning(f"[sync:flow] Backup failed: {e}")

    # Push to Qualtrics
    try:
        # Update cache payload with new flow
        cache = load_cached_survey(survey_id)
        cache.payload.setdefault("result", {})["SurveyFlow"] = edited_flow

        push_survey_flow(cache, context={"dimension": "flow"})
        print("[sync:flow] Flow pushed successfully")

    except Exception as e:
        print(f"[sync:flow] Push failed: {e}")
        return False

    # Verify push was successful by fetching from API
    try:
        from ..qualtrics_client import fetch_survey_definition_live

        live_payload = fetch_survey_definition_live(survey_id)
        live_flow = live_payload.get("result", {}).get("SurveyFlow", {})

        # Compare what we sent with what's now on the API
        sent_json = json.dumps(edited_flow, sort_keys=True)
        live_json = json.dumps(live_flow, sort_keys=True)

        if sent_json != live_json:
            logger.warning(
                "[sync:flow] Warning: Pushed flow differs from live API response. "
                "The API may have normalized or modified the flow structure."
            )
            # Use the live flow as baseline to stay in sync
            edited_flow = live_flow
    except Exception as e:
        logger.warning(f"[sync:flow] Could not verify push result: {e}")

    # Update baseline to match what's actually on the API
    baseline_path.write_text(json.dumps(edited_flow, indent=2), encoding="utf-8")

    # Refresh cache
    try:
        refresh_survey_cache(survey_id)
    except Exception as e:
        logger.warning(f"[sync:flow] Cache refresh failed: {e}")

    # Clear pending
    clear_pending(survey_id, "flow")

    # Publish if requested
    if not skip_publish:
        try:
            from ..qualtrics_client import publish_survey_definition
            publish_survey_definition(
                survey_id,
                description="Flow changes via qsync",
            )
            print("[sync:flow] Published survey version")
        except Exception as e:
            logger.warning(f"[sync:flow] Publish failed: {e}")

    print(f"[sync:flow] Pushed {len(pending.payload.changes)} change(s)")
    return True


def detect_unstaged_changes(survey_id: str) -> DimensionChanges:
    """Alias for detect_changes for orchestrator compatibility."""
    return detect_changes(survey_id)
