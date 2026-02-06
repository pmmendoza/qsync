"""Flow dimension module for survey flow synchronization."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from .flow_diff import (
    FlowChange,
    diff_flows,
    format_diff_for_display,
    format_diff_summary,
)
from .flow_validate import FlowValidationError, validate_flow, validate_yaml_structure
from .flow_yaml import flow_to_yaml, yaml_to_flow
from .types import DimensionChanges
from ..config import resolve_root
from ..pending_stage import (
    FlowPendingPayload,
    PendingStagedChanges,
    clear_pending,
    load_pending,
    save_pending,
)

logger = logging.getLogger(__name__)


def _workspace_root() -> Path:
    return resolve_root(required=False) or Path.cwd()


def _flow_dir(survey_id: str) -> Path:
    return _workspace_root() / "surveys" / "flow" / survey_id


def _yaml_path(survey_id: str) -> Path:
    return _flow_dir(survey_id) / "flow.yaml"


def _baseline_path(survey_id: str) -> Path:
    return _flow_dir(survey_id) / "baseline.json"


def _hash_flow(flow: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(flow, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _confirm(prompt: str, *, default: bool = False) -> bool:
    try:
        from ..interactive_menu import confirm

        return confirm(prompt, default=default)
    except Exception:
        response = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        if not response:
            return default
        return response in {"y", "yes"}


def _read_yaml_and_baseline(
    *,
    survey_id: str,
    require_files: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    yaml_path = _yaml_path(survey_id)
    baseline_path = _baseline_path(survey_id)

    if require_files and not yaml_path.exists():
        print(
            f"[sync:flow] No flow.yaml found. Run: qsync flow pull --survey-id {survey_id}"
        )
        return None
    if require_files and not baseline_path.exists():
        print(
            f"[sync:flow] No baseline found. Run: qsync flow pull --survey-id {survey_id}"
        )
        return None

    yaml_content = yaml_path.read_text(encoding="utf-8")
    edited = yaml_to_flow(yaml_content)
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    return edited, baseline


def pull(survey_id: str, *, force: bool = False) -> Path:
    """Pull flow from cached survey JSON and save as YAML + baseline."""
    from ..qualtrics_client import load_cached_survey

    cache = load_cached_survey(survey_id)
    flow = cache.payload.get("result", {}).get("SurveyFlow", {})
    blocks = cache.payload.get("result", {}).get("Blocks", {})
    questions = cache.payload.get("result", {}).get("Questions", {})

    flow_dir = _flow_dir(survey_id)
    yaml_path = _yaml_path(survey_id)
    baseline_path = _baseline_path(survey_id)

    if yaml_path.exists() and baseline_path.exists() and not force:
        try:
            loaded = _read_yaml_and_baseline(
                survey_id=survey_id,
                require_files=False,
            )
            if loaded:
                edited, baseline = loaded
                changes = diff_flows(baseline, edited)
                if changes:
                    raise FileExistsError(
                        f"Flow YAML has local changes ({len(changes)} change(s)). "
                        "Use --force to overwrite or stage/push changes first."
                    )
        except FileExistsError:
            raise
        except Exception:
            # If comparison fails, do not block pull; flow files may be malformed.
            pass

    flow_dir.mkdir(parents=True, exist_ok=True)
    yaml_path.write_text(
        flow_to_yaml(flow, survey_id, blocks, questions),
        encoding="utf-8",
    )
    baseline_path.write_text(
        json.dumps(flow, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("[sync:flow] Pulled flow to %s", yaml_path)
    return yaml_path


def detect_unstaged_changes(survey_id: str) -> DimensionChanges:
    """Detect unstaged YAML-vs-baseline flow changes (ignores pending)."""
    yaml_path = _yaml_path(survey_id)
    baseline_path = _baseline_path(survey_id)

    if not yaml_path.exists() and not baseline_path.exists():
        return DimensionChanges(
            dimension="flow",
            has_changes=False,
            change_summary="Not initialized",
            affected_qids=set(),
            status_kind="none",
            edit_count=0,
        )

    if yaml_path.exists() != baseline_path.exists():
        return DimensionChanges(
            dimension="flow",
            has_changes=False,
            change_summary="⚠ Incomplete local flow files",
            affected_qids=set(),
            warning_detail=(
                "Flow files are incomplete. Run: "
                f"qsync flow pull --survey-id {survey_id}"
            ),
            safe_to_autofix=True,
            status_kind="none",
            edit_count=0,
        )

    try:
        loaded = _read_yaml_and_baseline(survey_id=survey_id, require_files=False)
        if loaded is None:
            return DimensionChanges(
                dimension="flow",
                has_changes=False,
                change_summary="Not initialized",
                affected_qids=set(),
                status_kind="none",
                edit_count=0,
            )
        edited, baseline = loaded
        changes = diff_flows(baseline, edited)
        if changes:
            return DimensionChanges(
                dimension="flow",
                has_changes=True,
                change_summary=f"⚡ Unstaged: {format_diff_summary(changes)}",
                affected_qids=set(),
                status_kind="unstaged",
                edit_count=len(changes),
            )
        return DimensionChanges(
            dimension="flow",
            has_changes=False,
            change_summary="No changes",
            affected_qids=set(),
            status_kind="none",
            edit_count=0,
        )
    except Exception as exc:
        return DimensionChanges(
            dimension="flow",
            has_changes=False,
            change_summary="✗ Error",
            affected_qids=set(),
            error_detail=f"Flow detection failed: {str(exc).split(chr(10))[0]}",
            safe_to_autofix=False,
            status_kind="error",
            edit_count=0,
        )


def detect_changes(survey_id: str) -> DimensionChanges:
    """Detect staged or unstaged flow changes for a survey."""
    pending = load_pending(survey_id, "flow")
    if pending and isinstance(pending.payload, FlowPendingPayload):
        count = len(pending.payload.changes or [])
        return DimensionChanges(
            dimension="flow",
            has_changes=True,
            change_summary=f"✓ Staged: {count} change(s)",
            affected_qids=set(),
            status_kind="staged",
            edit_count=count,
        )
    return detect_unstaged_changes(survey_id)


def preview(
    survey_id: str,
    *,
    verbose: bool = False,
    visual: bool = False,
    validate: bool = True,
) -> list[FlowChange]:
    """Preview flow changes (YAML vs baseline)."""
    from ..qualtrics_client import load_cached_survey

    loaded = _read_yaml_and_baseline(survey_id=survey_id, require_files=True)
    if loaded is None:
        return []
    edited, baseline = loaded

    try:
        changes = diff_flows(baseline, edited)
        if not changes:
            print("[sync:flow] No changes detected")
        else:
            print(f"[sync:flow] {format_diff_summary(changes)}:")
            for line in format_diff_for_display(changes, verbose=verbose):
                print(line)

        if validate:
            try:
                cache = load_cached_survey(survey_id)
                blocks = cache.payload.get("result", {}).get("Blocks", {})
                questions = cache.payload.get("result", {}).get("Questions", {})
                validate_flow(edited, survey_id, blocks, questions)
            except FlowValidationError as exc:
                print("\n[sync:flow] WARNING - Invalid references detected:")
                for err in exc.errors:
                    print(f"  ! {err}")
                print(
                    "\nThese issues will block push. Fix the flow or restore missing items."
                )

        if visual:
            print("\n[sync:flow] Visual diff (Mermaid) not yet implemented")
        return changes

    except FlowValidationError as exc:
        print(f"[sync:flow] Validation error in YAML:\n{exc}")
        return []
    except Exception as exc:
        print(f"[sync:flow] Error previewing changes: {exc}")
        return []


def stage(
    survey_id: str,
    *,
    allow_drift: bool = False,
    interactive: bool = True,
) -> bool:
    """Stage flow changes into pending cache."""
    from ..drift_check import enforce_no_drift
    from ..qualtrics_client import load_cached_survey
    import yaml as yaml_lib

    loaded = _read_yaml_and_baseline(survey_id=survey_id, require_files=True)
    if loaded is None:
        return False

    # Stage should be blocked by live drift unless explicitly allowed.
    enforce_no_drift(
        survey_id=survey_id,
        dimension="flow",
        allow_drift=allow_drift,
        interactive=interactive,
    )

    yaml_text = _yaml_path(survey_id).read_text(encoding="utf-8")
    try:
        yaml_data = yaml_lib.safe_load(yaml_text)
        validate_yaml_structure(yaml_data)
        edited = yaml_to_flow(yaml_text)
    except FlowValidationError as exc:
        print(f"[sync:flow] YAML validation error:\n{exc}")
        return False
    except Exception as exc:
        print(f"[sync:flow] Error parsing YAML: {exc}")
        return False

    _, baseline = loaded
    changes = diff_flows(baseline, edited)
    if not changes:
        clear_pending(survey_id, "flow")
        print("[sync:flow] No changes to stage")
        return True

    try:
        cache = load_cached_survey(survey_id)
        blocks = cache.payload.get("result", {}).get("Blocks", {})
        questions = cache.payload.get("result", {}).get("Questions", {})
        validate_flow(edited, survey_id, blocks, questions)
    except FlowValidationError as exc:
        print(f"[sync:flow] Flow validation error:\n{exc}")
        return False

    payload = FlowPendingPayload(
        flow_yaml_path=str(_yaml_path(survey_id)),
        baseline_hash=_hash_flow(baseline),
        changes=[c.to_dict() for c in changes],
    )
    save_pending(
        PendingStagedChanges(
            survey_id=survey_id,
            dimension="flow",
            payload=payload,
        )
    )
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
    """Push staged flow changes to Qualtrics."""
    from ..drift_check import enforce_no_drift
    from ..push_safeguards import SafeguardConfig, enforce_push_safeguards
    from ..qualtrics_client import (
        ensure_backup,
        fetch_survey_definition_live,
        load_cached_survey,
        publish_survey_definition,
        push_survey_flow,
        refresh_survey_cache,
    )

    pending = load_pending(survey_id, "flow")
    if not pending or not isinstance(pending.payload, FlowPendingPayload):
        print("[sync:flow] No staged flow changes found")
        return True

    yaml_path = Path(pending.payload.flow_yaml_path)
    if not yaml_path.exists():
        print(f"[sync:flow] YAML file not found: {yaml_path}")
        clear_pending(survey_id, "flow")
        return False

    loaded = _read_yaml_and_baseline(survey_id=survey_id, require_files=True)
    if loaded is None:
        return False
    edited_flow, baseline = loaded

    current_changes = diff_flows(baseline, edited_flow)
    if not current_changes:
        clear_pending(survey_id, "flow")
        print("[sync:flow] No changes to push")
        return True

    # Ensure live API has not drifted from local baseline unless overridden.
    enforce_no_drift(
        survey_id=survey_id,
        dimension="flow",
        allow_drift=allow_drift,
        interactive=interactive,
    )

    current_hash = _hash_flow(baseline)
    if current_hash != (pending.payload.baseline_hash or ""):
        print(
            "[sync:flow] WARNING: Baseline has changed since staging. "
            "Consider re-staging with 'qsync flow stage'."
        )
        if interactive and not auto_yes and not _confirm("Continue anyway?"):
            print("[sync:flow] Aborted")
            return False

    staged_change_ids = {str(c.get("node_id") or "") for c in pending.payload.changes}
    current_change_ids = {c.node_id for c in current_changes}
    if staged_change_ids != current_change_ids:
        print(
            "[sync:flow] WARNING: YAML has changed since staging "
            f"(staged={len(staged_change_ids)}, current={len(current_change_ids)})."
        )
        if interactive and not auto_yes and not _confirm("Push current YAML changes?"):
            print("[sync:flow] Aborted - re-stage with 'qsync flow stage'")
            return False

    try:
        cache = load_cached_survey(survey_id)
        blocks = cache.payload.get("result", {}).get("Blocks", {})
        questions = cache.payload.get("result", {}).get("Questions", {})
        validate_flow(edited_flow, survey_id, blocks, questions)
    except FlowValidationError as exc:
        print(f"[sync:flow] Validation error:\n{exc}")
        return False

    enforce_push_safeguards(
        SafeguardConfig(
            survey_id=survey_id,
            dimension="flow",
            force_live=force_live,
            force_preview=force_preview,
            auto_yes=auto_yes,
        )
    )

    if interactive and not auto_yes:
        print(f"[sync:flow] About to push {len(current_changes)} change(s):")
        for change in current_changes[:5]:
            symbol = {"added": "+", "removed": "-", "modified": "~"}.get(
                change.change_type,
                "?",
            )
            print(f"  {symbol} {change.node_type} [{change.node_id}]: {change.description}")
        if len(current_changes) > 5:
            print(f"  ... and {len(current_changes) - 5} more")
        if not _confirm("Proceed with flow push?"):
            print("[sync:flow] Aborted")
            return False

    try:
        ensure_backup(survey_id)
    except Exception as exc:
        logger.warning("[sync:flow] Backup failed: %s", exc)

    try:
        cache = load_cached_survey(survey_id)
        cache.payload.setdefault("result", {})["SurveyFlow"] = edited_flow
        push_survey_flow(cache, context={"dimension": "flow"})
        print("[sync:flow] Flow pushed successfully")
    except Exception as exc:
        print(f"[sync:flow] Push failed: {exc}")
        return False

    final_flow = edited_flow
    try:
        live_payload = fetch_survey_definition_live(survey_id)
        live_flow = live_payload.get("result", {}).get("SurveyFlow", {})
        sent_json = json.dumps(edited_flow, sort_keys=True)
        live_json = json.dumps(live_flow, sort_keys=True)
        if sent_json != live_json:
            logger.warning(
                "[sync:flow] Pushed flow differs from API response; using API flow as baseline."
            )
        final_flow = live_flow
    except Exception as exc:
        logger.warning("[sync:flow] Could not verify push result: %s", exc)

    final_blocks: dict[str, Any] = {}
    final_questions: dict[str, Any] = {}
    try:
        refreshed, _ = refresh_survey_cache(survey_id)
        final_flow = (
            refreshed.payload.get("result", {}).get("SurveyFlow", {}) or final_flow
        )
        final_blocks = refreshed.payload.get("result", {}).get("Blocks", {}) or {}
        final_questions = refreshed.payload.get("result", {}).get("Questions", {}) or {}
    except Exception as exc:
        logger.warning("[sync:flow] Cache refresh failed: %s", exc)

    _baseline_path(survey_id).write_text(
        json.dumps(final_flow, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        _yaml_path(survey_id).write_text(
            flow_to_yaml(final_flow, survey_id, final_blocks, final_questions),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("[sync:flow] Failed to refresh flow YAML after push: %s", exc)

    clear_pending(survey_id, "flow")

    if not skip_publish:
        try:
            publish_survey_definition(
                survey_id,
                description=f"qsync: update flow ({len(current_changes)} change(s))",
            )
            print("[sync:flow] Published survey version")
        except Exception as exc:
            logger.warning("[sync:flow] Publish failed: %s", exc)

    print(f"[sync:flow] Pushed {len(current_changes)} change(s)")
    return True
