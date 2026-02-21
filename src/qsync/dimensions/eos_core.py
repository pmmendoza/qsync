"""Sync EndSurvey (EOS) library messages to/from disk.

This supports the workflow:
  pull -> edit -> preview -> apply (stage) -> push (API)
"""

from __future__ import annotations

import base64
import difflib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from ..api_push import send_api_request
from ..config import (
    WORKSPACE_LAYOUT_ACCOUNT_ROOT_V1,
    get_client_config,
    resolve_root,
    resolve_scoped_dir,
    resolve_survey_cache_dir,
    resolve_workspace_layout,
)
from ..pending_stage import (
    PendingStagedChanges,
    EosPendingPayload,
    EosOperation,
    save_pending,
)
from ..push_logger import log_push_event
from ..qualtrics_client import (
    ensure_backup,
    load_cached_survey,
    publish_survey_definition,
    refresh_survey_cache,
)
from ..drift_check import check_drift as run_drift_check, enforce_no_drift
from ..push_safeguards import enforce_push_safeguards, SafeguardConfig
from ..auto_publish import auto_publish_after_push

LIB_MESSAGE_DIRNAME_LEGACY = "qualtrics_library_messages"
LIB_MESSAGE_DIRNAME_CANONICAL = "library_messages"
SURVEY_SOURCE = "SurveyFlow.EndSurvey.DisplayMessage"

ERROR_ID_EOS_SHARED_MESSAGE = "QSYNC-EOS-SHARED-001"


class EosSharedMessageError(RuntimeError):
    """Raised when EOS message edits are blocked due to shared usage (local scan)."""


@dataclass(frozen=True)
class EndSurveyMessageRef:
    survey_id: str
    flow_id: str | None
    library_id: str
    message_id: str

    def to_context_dict(self) -> dict[str, Any]:
        return {
            "survey_id": self.survey_id,
            "flow_id": self.flow_id,
            "source": SURVEY_SOURCE,
        }


@dataclass(frozen=True)
class CrossAccountPlannedImport:
    target_library_id: str
    target_message_id: str
    source_library_id: str
    source_message_id: str
    target_create_library_id: str
    source_account_label: str | None = None


@dataclass(frozen=True)
class CrossAccountEosRepairResult:
    source_survey_id: str
    target_survey_id: str
    target_refs_total: int
    source_refs_total: int
    missing_refs: int
    planned_rewire_count: int
    planned_imports: list[CrossAccountPlannedImport]
    created_pairs: dict[tuple[str, str], tuple[str, str]]
    replacements: dict[tuple[str, str], tuple[str, str]]
    updated_flow_ids: list[str]
    pulled_paths: list[Path]
    warnings: list[str]
    dry_run: bool


@dataclass(frozen=True)
class EosSourceAccount:
    label: str
    base_url: str
    headers: dict[str, str]


@dataclass(frozen=True)
class EosBestEffortPullResult:
    pulled_paths: list[Path]
    warnings: list[str]


def extract_eos_message_refs(
    survey_id: str, survey_payload: dict
) -> list[EndSurveyMessageRef]:
    """Extract (libraryId, messageId) references from SurveyFlow EndSurvey nodes."""

    result = survey_payload.get("result", {}) or {}
    flow = result.get("SurveyFlow") or result.get("Flow") or {}
    if not isinstance(flow, dict):
        return []

    refs: list[EndSurveyMessageRef] = []

    def walk(node: object) -> None:
        if node is None:
            return
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        if not isinstance(node, dict):
            return

        if str(node.get("Type") or "") == "EndSurvey":
            opts = node.get("Options") or {}
            if (opts.get("SurveyTermination") or "") == "DisplayMessage":
                lib_id = str(opts.get("EOSMessageLibrary") or "").strip()
                msg_id = str(opts.get("EOSMessage") or "").strip()
                if lib_id and msg_id:
                    refs.append(
                        EndSurveyMessageRef(
                            survey_id=survey_id,
                            flow_id=str(node.get("FlowID") or "") or None,
                            library_id=lib_id,
                            message_id=msg_id,
                        )
                    )

        for key in ("Flow", "Then", "Else", "ElseFlow"):
            if key in node:
                walk(node.get(key))

    walk(flow.get("Flow"))

    seen: set[tuple[str, str]] = set()
    deduped: list[EndSurveyMessageRef] = []
    for r in refs:
        k = (r.library_id, r.message_id)
        if k in seen:
            continue
        seen.add(k)
        deduped.append(r)
    return deduped


def message_dir(
    library_id: str, message_id: str, *, account: str | None = None
) -> Path:
    root = resolve_root(required=False) or Path.cwd()
    layout = resolve_workspace_layout(root=root)
    if layout == WORKSPACE_LAYOUT_ACCOUNT_ROOT_V1:
        surveys_dir = resolve_scoped_dir("surveys", root=root, account=account)
        return (
            surveys_dir
            / LIB_MESSAGE_DIRNAME_CANONICAL
            / library_id
            / message_id
        )
    contents_dir = resolve_scoped_dir("contents", root=root, account=account)
    return contents_dir / LIB_MESSAGE_DIRNAME_LEGACY / library_id / message_id


def pull_eos_messages(
    *,
    survey_id: str,
    allow_shared: bool,
    include_backups_scan: bool = False,
    check_drift: bool = True,
) -> list[Path]:
    """Pull referenced EOS messages to disk (network)."""

    # Check for drift before pulling
    if check_drift:
        drift_report = run_drift_check(survey_id, dimension="eos", interactive=True)
        if drift_report.has_drift:
            drift_report.display(interactive=False)

    cache = load_cached_survey(survey_id)
    refs = extract_eos_message_refs(survey_id, cache.payload)
    if not refs:
        return []

    # Shared-message enforcement is handled by the CLI for apply/push.
    # For pull, we allow downloading the shared message to disk so users can
    # inspect it. (Pushing still requires explicit override.)

    base_url, headers = get_client_config()
    contexts_by_ref = find_message_contexts(
        refs={(r.library_id, r.message_id) for r in refs},
        include_backups=include_backups_scan,
    )
    written: list[Path] = []
    for ref in refs:
        resp = send_api_request(
            action="qsync.eos.pull.message",
            method="GET",
            base_url=base_url,
            headers=headers,
            path=f"libraries/{ref.library_id}/messages/{ref.message_id}",
            survey_id=survey_id,
            log_event=False,
            timeout=60,
        )
        payload = resp.json()
        target = write_library_message_to_disk(
            library_id=ref.library_id,
            message_id=ref.message_id,
            api_payload=payload,
            contexts=contexts_by_ref.get((ref.library_id, ref.message_id))
            or [ref.to_context_dict()],
        )
        written.append(target)
    return written


def pull_eos_messages_best_effort(
    *,
    survey_id: str,
    base_url: str,
    headers: dict[str, str],
    include_backups_scan: bool = False,
    check_drift: bool = False,
    refs: list[EndSurveyMessageRef] | None = None,
    action: str = "qsync.eos.pull.best_effort.message",
) -> EosBestEffortPullResult:
    """Pull EOS messages while skipping inaccessible refs (400/403/404)."""

    if check_drift:
        drift_report = run_drift_check(survey_id, dimension="eos", interactive=True)
        if drift_report.has_drift:
            drift_report.display(interactive=False)

    effective_refs = refs
    if effective_refs is None:
        cache = load_cached_survey(survey_id)
        effective_refs = extract_eos_message_refs(survey_id, cache.payload)
    deduped_refs: list[EndSurveyMessageRef] = []
    seen_pairs: set[tuple[str, str]] = set()
    for ref in effective_refs or []:
        pair = (ref.library_id, ref.message_id)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        deduped_refs.append(ref)
    effective_refs = deduped_refs
    if not effective_refs:
        return EosBestEffortPullResult(pulled_paths=[], warnings=[])

    contexts_by_ref = find_message_contexts(
        refs={(r.library_id, r.message_id) for r in effective_refs},
        include_backups=include_backups_scan,
    )
    written: list[Path] = []
    warnings: list[str] = []
    for ref in effective_refs:
        try:
            payload = _fetch_library_message(
                base_url=base_url,
                headers=headers,
                survey_id=survey_id,
                library_id=ref.library_id,
                message_id=ref.message_id,
                action=action,
            )
        except Exception as exc:  # noqa: BLE001
            if _is_missing_library_message_error(exc):
                warnings.append(
                    "Skipped inaccessible EOS message "
                    f"{ref.library_id}/{ref.message_id}: {exc}"
                )
                continue
            raise

        target = write_library_message_to_disk(
            library_id=ref.library_id,
            message_id=ref.message_id,
            api_payload=payload,
            contexts=contexts_by_ref.get((ref.library_id, ref.message_id))
            or [ref.to_context_dict()],
        )
        written.append(target)

    return EosBestEffortPullResult(pulled_paths=written, warnings=warnings)


def preview_eos_messages(
    *,
    survey_id: str,
    allow_shared: bool,
    detailed: bool = False,
    include_backups_scan: bool = False,
    check_drift: bool = True,
) -> list[str]:
    """Return human-readable diff lines for referenced messages (network)."""

    # Check for drift before preview
    if check_drift:
        drift_report = run_drift_check(survey_id, dimension="eos", interactive=True)
        if drift_report.has_drift:
            drift_report.display(interactive=False)

    cache = load_cached_survey(survey_id)
    refs = extract_eos_message_refs(survey_id, cache.payload)
    if not refs:
        return ["(No EndSurvey DisplayMessage references found.)"]

    # Shared-message enforcement is handled by the CLI for apply/push.
    # For preview, we allow inspecting differences even if shared.

    base_url, headers = get_client_config()
    contexts_by_ref = find_message_contexts(
        refs={(r.library_id, r.message_id) for r in refs},
        include_backups=include_backups_scan,
    )
    lines: list[str] = []
    for ref in refs:
        live_resp = send_api_request(
            action="qsync.eos.preview.message",
            method="GET",
            base_url=base_url,
            headers=headers,
            path=f"libraries/{ref.library_id}/messages/{ref.message_id}",
            survey_id=survey_id,
            log_event=False,
            timeout=60,
        )
        live = _coerce_result_payload(live_resp.json())
        disk = read_library_message_from_disk(ref.library_id, ref.message_id)
        used_by = _format_used_by(contexts_by_ref.get((ref.library_id, ref.message_id)))
        if disk is None:
            lines.append(
                f"- ({ref.library_id}, {ref.message_id}): not pulled to disk yet{used_by}"
            )
            continue
        diffs = diff_library_messages(disk, live, detailed=detailed)
        if diffs:
            lines.append(f"- ({ref.library_id}, {ref.message_id}): CHANGED{used_by}")
            local_dir = message_dir(ref.library_id, ref.message_id)
            lines.append(
                f"  context: local={local_dir}/messages/*.html, remote=Qualtrics live message"
            )
            lines.extend([f"  {ln}" for ln in diffs])
        else:
            lines.append(f"- ({ref.library_id}, {ref.message_id}): no changes{used_by}")
    return lines


def apply_eos_messages(
    *,
    survey_id: str,
    allow_shared: bool,
    allow_destructive: bool,
    include_backups_scan: bool = False,
    scope_expr: str | None = None,
) -> PendingStagedChanges | None:
    """Stage pending EOS pushes (no API calls)."""

    cache = load_cached_survey(survey_id)
    refs = extract_eos_message_refs(survey_id, cache.payload)
    if not refs:
        raise RuntimeError("No EndSurvey DisplayMessage references found.")

    if not allow_shared:
        shared = detect_shared_messages(
            survey_id=survey_id,
            refs={(r.library_id, r.message_id) for r in refs},
            include_backups=include_backups_scan,
        )
        if shared:
            _raise_shared_message_blocked(
                survey_id=survey_id,
                shared=shared,
                include_backups_scan=include_backups_scan,
                verb="apply",
                override_flag="--allow-shared-message-edit",
            )

    ops: list[EosOperation] = []
    for ref in refs:
        base = message_dir(ref.library_id, ref.message_id)
        messages_dir = base / "messages"
        keys_path = messages_dir / "_keys.json"
        meta_path = base / "meta.json"
        if not meta_path.exists() or not keys_path.exists():
            raise RuntimeError(
                f"Missing local files for ({ref.library_id}, {ref.message_id}); run 'qsync eos pull' first."
            )
        # Validate that referenced message files exist.
        keys = _load_keys(keys_path)
        for entry in keys:
            file_path = messages_dir / entry["file"]
            if not file_path.exists():
                raise RuntimeError(
                    f"Missing message file for key '{entry['key']}' at {file_path}"
                )
        # Idempotency: if there are no local changes since the last pull/backup,
        # do not stage an operation.
        disk = read_library_message_from_disk(ref.library_id, ref.message_id)
        if disk is None:
            raise RuntimeError(
                f"Local message missing for ({ref.library_id}, {ref.message_id}); run 'qsync eos pull' first."
            )
        latest = _latest_backup_result(ref.library_id, ref.message_id)
        if latest is not None and _messages_equal(disk, latest):
            continue

        ops.append(
            EosOperation(
                library_id=ref.library_id,
                message_id=ref.message_id,
                message_dir=str(base),
                keys=None,
                allow_destructive=bool(allow_destructive),
            )
        )

    if ops:
        payload = EosPendingPayload(operations=ops)
        record = PendingStagedChanges(
            survey_id=survey_id,
            dimension="eos",
            payload=payload,
            schema_version=2,
        )
        save_pending(record)
        return record

    return None


def push_eos_messages(
    *,
    survey_id: str,
    record: PendingStagedChanges,
    allow_shared: bool,
    yes: bool,
    include_backups_scan: bool = False,
    dry_run: bool = False,
    force_live: bool = False,
    force_preview: bool = False,
    interactive: bool = True,
    publish: bool = True,
    publish_description: str | None = None,
    allow_drift: bool = False,
    scope_expr: str | None = None,
) -> list[tuple[str, str]]:
    """Execute staged EOS operations via API (network)."""

    if dry_run:
        # No API writes; return the list of operations that would be pushed.
        if not isinstance(record.payload, EosPendingPayload):
            raise RuntimeError(
                f"Expected EOS payload, got {type(record.payload).__name__}"
            )
        return [
            (op.library_id, op.message_id) for op in (record.payload.operations or [])
        ]

    enforce_no_drift(
        survey_id=survey_id,
        dimension="eos",
        allow_drift=allow_drift,
        interactive=interactive,
    )

    if record.survey_id != survey_id:
        raise RuntimeError("Pending record survey_id mismatch.")

    if not isinstance(record.payload, EosPendingPayload):
        raise RuntimeError(f"Expected EOS payload, got {type(record.payload).__name__}")

    operations = record.payload.operations

    # Enforce push safeguards (this was missing for EOS!)
    config = SafeguardConfig(
        survey_id=survey_id,
        dimension="eos",
        force_live=force_live,
        force_preview=force_preview,
        auto_yes=not interactive,
    )
    safeguard_result = enforce_push_safeguards(config)
    if safeguard_result.warnings:
        for warning in safeguard_result.warnings:
            print(f"[qsync:eos] WARNING: {warning}")

    if not allow_shared:
        shared = detect_shared_messages(
            survey_id=survey_id,
            refs={(op.library_id, op.message_id) for op in operations},
            include_backups=include_backups_scan,
        )
        if shared:
            _raise_shared_message_blocked(
                survey_id=survey_id,
                shared=shared,
                include_backups_scan=include_backups_scan,
                verb="push",
                override_flag="--allow-shared-message-edit",
            )

    if not yes:
        raise RuntimeError("Refusing to push without --yes (non-interactive safety).")

    base_url, headers = get_client_config()
    contexts_by_ref = find_message_contexts(
        refs={(op.library_id, op.message_id) for op in operations},
        include_backups=include_backups_scan,
    )
    pushed: list[tuple[str, str]] = []
    for op in operations:
        lib_id = op.library_id
        msg_id = op.message_id

        # Fetch live for destructive checks + backup snapshot
        live_resp = send_api_request(
            action="qsync.eos.push.message.preflight",
            method="GET",
            base_url=base_url,
            headers=headers,
            path=f"libraries/{lib_id}/messages/{msg_id}",
            survey_id=survey_id,
            log_event=False,
            timeout=60,
        )
        live = _coerce_result_payload(live_resp.json())
        _write_backup_snapshot(lib_id, msg_id, live_resp.json())

        disk = read_library_message_from_disk(lib_id, msg_id)
        if disk is None:
            raise RuntimeError(f"Local message missing for ({lib_id}, {msg_id}).")

        # Idempotency: do not PUT if disk matches live.
        if _messages_equal(disk, live):
            continue

        if not op.allow_destructive:
            missing = sorted(
                set((live.get("messages", {}) or {}).keys())
                - set((disk.get("messages", {}) or {}).keys())
            )
            if missing:
                raise RuntimeError(
                    f"Destructive key deletion detected for ({lib_id}, {msg_id}): {missing}. "
                    "Re-run with --allow-destructive to permit."
                )

        payload = _build_put_payload(disk)
        send_api_request(
            action="qsync.eos.push.message",
            method="PUT",
            base_url=base_url,
            headers=headers,
            path=f"libraries/{lib_id}/messages/{msg_id}",
            survey_id=survey_id,
            json=payload,
            timeout=60,
        )
        pushed.append((lib_id, msg_id))

        # Refresh local disk snapshot after successful PUT so stage/push becomes idempotent.
        post_resp = send_api_request(
            action="qsync.eos.push.message.postflight",
            method="GET",
            base_url=base_url,
            headers=headers,
            path=f"libraries/{lib_id}/messages/{msg_id}",
            survey_id=survey_id,
            log_event=False,
            timeout=60,
        )
        write_library_message_to_disk(
            library_id=lib_id,
            message_id=msg_id,
            api_payload=post_resp.json(),
            contexts=contexts_by_ref.get((lib_id, msg_id))
            or [{"survey_id": survey_id}],
        )

    # Auto-publish after successful push
    if publish and pushed:
        auto_publish_after_push(
            survey_id=survey_id,
            dimension="eos",
            count=len(pushed),
            changed_qids=[],  # EOS messages don't have QIDs
            custom_description=publish_description,
            workbook_path=None,
            interactive=interactive,
            context={
                "origin": "qsync.eos.push",
                "message_count": len(pushed),
                "messages": [(lib, msg) for lib, msg in pushed],
            },
        )

    return pushed


@dataclass(frozen=True)
class CloneSharedEosResult:
    replacements: dict[tuple[str, str], str]
    updated_flow_ids: list[str]
    pulled_paths: list[Path]


def clone_shared_eos_messages(
    *,
    survey_id: str,
    include_backups_scan: bool,
    yes: bool,
    allow_non_smoke: bool = False,
    allow_drift: bool = False,
    publish: bool = True,
    publish_description: str | None = None,
) -> CloneSharedEosResult:
    """Clone shared EOS library messages and rewire SurveyFlow to reference the clones.

    This is intended for smoke-safe workflows: it replaces references to shared
    library messages with newly created, survey-specific library messages.
    """

    import re

    interactive = not bool(yes)

    enforce_no_drift(
        survey_id=survey_id,
        dimension="items",
        allow_drift=allow_drift,
        interactive=interactive,
    )

    cache = load_cached_survey(survey_id)
    survey_name = str(cache.payload.get("result", {}).get("SurveyName") or "").strip()
    if not allow_non_smoke and "smoke" not in survey_name.lower():
        raise RuntimeError(
            "Refusing to clone shared EOS messages for a non-smoke survey. "
            "Re-run with --allow-non-smoke to override."
        )

    refs = extract_eos_message_refs(survey_id, cache.payload)
    if not refs:
        raise RuntimeError("No EndSurvey DisplayMessage references found.")

    shared = detect_shared_messages(
        survey_id=survey_id,
        refs={(r.library_id, r.message_id) for r in refs},
        include_backups=include_backups_scan,
    )
    if not shared:
        return CloneSharedEosResult(
            replacements={}, updated_flow_ids=[], pulled_paths=[]
        )

    if not yes:
        raise RuntimeError("Refusing to clone without --yes (non-interactive safety).")

    base_url, headers = get_client_config()

    def _clone_description(description: str) -> str:
        desc = (description or "").strip() or "EOS message"
        desc = re.sub(r"\\s+", " ", desc).strip()
        suffix = f" (cloned for {survey_id})"
        if len(desc) + len(suffix) > 250:
            desc = desc[: (250 - len(suffix) - 1)].rstrip() + "…"
        return desc + suffix

    replacements: dict[tuple[str, str], str] = {}
    for lib_id, msg_id in sorted(shared):
        live_resp = send_api_request(
            action="qsync.eos.clone_shared.get",
            method="GET",
            base_url=base_url,
            headers=headers,
            path=f"libraries/{lib_id}/messages/{msg_id}",
            survey_id=survey_id,
            log_event=False,
            timeout=60,
        )
        live = _coerce_result_payload(live_resp.json())
        messages = dict(live.get("messages") or {})
        category = str(live.get("category") or "endOfSurvey").strip() or "endOfSurvey"
        description = _clone_description(str(live.get("description") or ""))

        create_payload = {
            "category": category,
            "description": description,
            "messages": messages,
        }
        create_resp = send_api_request(
            action="qsync.eos.clone_shared.create",
            method="POST",
            base_url=base_url,
            headers=headers,
            path=f"libraries/{lib_id}/messages",
            survey_id=survey_id,
            json=create_payload,
            timeout=60,
        )
        created = create_resp.json() if hasattr(create_resp, "json") else {}
        created_result = (
            created.get("result") if isinstance(created, dict) else None
        ) or {}
        new_msg_id = None
        for key in ("messageId", "messageID", "MessageID", "id", "ID"):
            val = created_result.get(key)
            if isinstance(val, str) and val.strip():
                new_msg_id = val.strip()
                break
        if not new_msg_id:
            raise RuntimeError(
                f"Unable to parse created message id for {lib_id}/{msg_id} from API response."
            )

        # Ensure messages are set (some APIs may ignore messages on create).
        send_api_request(
            action="qsync.eos.clone_shared.put",
            method="PUT",
            base_url=base_url,
            headers=headers,
            path=f"libraries/{lib_id}/messages/{new_msg_id}",
            survey_id=survey_id,
            json=_build_put_payload({"messages": messages}),
            timeout=60,
        )
        replacements[(lib_id, msg_id)] = new_msg_id

    # Rewrite SurveyFlow references
    flow = cache.payload.get("result", {}).get("SurveyFlow") or {}
    updated_flow_ids: list[str] = []

    def walk(node: object) -> None:
        if node is None:
            return
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        if not isinstance(node, dict):
            return

        if str(node.get("Type") or "") == "EndSurvey":
            opts = node.get("Options") or {}
            if (opts.get("SurveyTermination") or "") == "DisplayMessage":
                lib_id = str(opts.get("EOSMessageLibrary") or "").strip()
                msg_id = str(opts.get("EOSMessage") or "").strip()
                key = (lib_id, msg_id)
                new_id = replacements.get(key)
                if new_id:
                    opts["EOSMessage"] = new_id
                    node["Options"] = opts
                    flow_id = str(node.get("FlowID") or "") or None
                    if flow_id:
                        updated_flow_ids.append(flow_id)

        for key in ("Flow", "Then", "Else", "ElseFlow"):
            if key in node:
                walk(node.get(key))

    walk(flow.get("Flow"))
    if not updated_flow_ids:
        return CloneSharedEosResult(
            replacements=replacements, updated_flow_ids=[], pulled_paths=[]
        )

    from ..qualtrics_client import ensure_backup, push_survey_flow, refresh_survey_cache

    ensure_backup(survey_id)
    push_survey_flow(
        cache,
        context={
            "origin": "qsync.eos.clone_shared",
            "survey_id": survey_id,
            "cloned": [
                {"library_id": lib, "from": old, "to": new}
                for (lib, old), new in sorted(replacements.items())
            ],
            "updated_flow_ids": list(updated_flow_ids),
        },
    )

    if publish:
        auto_publish_after_push(
            survey_id=survey_id,
            dimension="eos",
            count=len(replacements),
            custom_description=publish_description,
            skip_publish=False,
            auto_yes=True,
        )

    refresh_survey_cache(survey_id)

    # Pull the newly referenced messages to disk so the normal EOS workflow can proceed.
    pulled_paths = pull_eos_messages(
        survey_id=survey_id,
        allow_shared=True,
        include_backups_scan=include_backups_scan,
        check_drift=False,
    )

    return CloneSharedEosResult(
        replacements=replacements,
        updated_flow_ids=sorted(set(updated_flow_ids)),
        pulled_paths=pulled_paths,
    )


def repair_eos_messages_from_source_account(
    *,
    target_survey_id: str,
    source_survey_id: str,
    target_base_url: str,
    target_headers: dict[str, str],
    source_base_url: str,
    source_headers: dict[str, str],
    include_backups_scan: bool,
    dry_run: bool,
    publish: bool,
    publish_description: str | None = None,
) -> CrossAccountEosRepairResult:
    """Repair missing target EOS references by importing messages from a source survey/account.

    This is intended for cross-account copy workflows where SurveyFlow EndSurvey
    nodes still point at source-account library/message IDs.
    """

    target_payload = _fetch_survey_definition_for_account(
        base_url=target_base_url,
        headers=target_headers,
        survey_id=target_survey_id,
        action="qsync.eos.repair.cross_account.fetch_target",
    )
    source_payload = _fetch_survey_definition_for_account(
        base_url=source_base_url,
        headers=source_headers,
        survey_id=source_survey_id,
        action="qsync.eos.repair.cross_account.fetch_source",
    )

    target_refs = extract_eos_message_refs(target_survey_id, target_payload)
    source_refs = extract_eos_message_refs(source_survey_id, source_payload)

    warnings: list[str] = []
    if len(source_refs) != len(target_refs):
        warnings.append(
            "Source/target EndSurvey reference counts differ "
            f"({len(source_refs)} vs {len(target_refs)}). "
            "Matching will use FlowID first, then positional fallback."
        )

    if not target_refs:
        return CrossAccountEosRepairResult(
            source_survey_id=source_survey_id,
            target_survey_id=target_survey_id,
            target_refs_total=0,
            source_refs_total=len(source_refs),
            missing_refs=0,
            planned_rewire_count=0,
            planned_imports=[],
            created_pairs={},
            replacements={},
            updated_flow_ids=[],
            pulled_paths=[],
            warnings=warnings,
            dry_run=dry_run,
        )

    if not source_refs:
        raise RuntimeError(
            f"Source survey {source_survey_id} has no EndSurvey DisplayMessage references."
        )

    source_refs_for_target = _map_source_refs_to_target_refs(
        target_refs=target_refs,
        source_refs=source_refs,
    )

    # Keep payloads cached per source ref to avoid redundant API calls.
    source_messages: dict[tuple[str, str], dict] = {}
    # Map a source ref to its created target ref, so duplicates reuse one message.
    created_by_source: dict[tuple[str, str], tuple[str, str]] = {}
    replacements: dict[tuple[str, str], tuple[str, str]] = {}
    planned_imports: list[CrossAccountPlannedImport] = []
    missing_refs = 0

    fallback_library_id: str | None = None
    fallback_library_resolved = False

    for idx, target_ref in enumerate(target_refs):
        source_ref = source_refs_for_target[idx]
        target_pair = (target_ref.library_id, target_ref.message_id)
        source_pair = (source_ref.library_id, source_ref.message_id)

        try:
            _fetch_library_message(
                base_url=target_base_url,
                headers=target_headers,
                survey_id=target_survey_id,
                library_id=target_ref.library_id,
                message_id=target_ref.message_id,
                action="qsync.eos.repair.cross_account.target_get",
            )
            continue
        except Exception as exc:  # noqa: BLE001
            if not _is_missing_library_message_error(exc):
                raise

        missing_refs += 1

        if dry_run:
            planned_imports.append(
                CrossAccountPlannedImport(
                    target_library_id=target_ref.library_id,
                    target_message_id=target_ref.message_id,
                    source_library_id=source_ref.library_id,
                    source_message_id=source_ref.message_id,
                    target_create_library_id=target_ref.library_id or source_ref.library_id,
                )
            )
            continue

        existing = created_by_source.get(source_pair)
        if existing:
            replacements[target_pair] = existing
            continue

        source_message_payload = source_messages.get(source_pair)
        if source_message_payload is None:
            try:
                source_message_payload = _fetch_library_message(
                    base_url=source_base_url,
                    headers=source_headers,
                    survey_id=source_survey_id,
                    library_id=source_ref.library_id,
                    message_id=source_ref.message_id,
                    action="qsync.eos.repair.cross_account.source_get",
                )
            except Exception as source_exc:  # noqa: BLE001
                # Positional mapping can mismatch in edge cases; if so, retry with the
                # target pair before failing hard.
                if source_pair != target_pair:
                    source_message_payload = _fetch_library_message(
                        base_url=source_base_url,
                        headers=source_headers,
                        survey_id=source_survey_id,
                        library_id=target_ref.library_id,
                        message_id=target_ref.message_id,
                        action="qsync.eos.repair.cross_account.source_get_fallback",
                    )
                    source_pair = target_pair
                else:
                    raise RuntimeError(
                        "Unable to fetch EOS message from source account for "
                        f"{source_ref.library_id}/{source_ref.message_id}: {source_exc}"
                    ) from source_exc
            source_messages[source_pair] = source_message_payload

        candidate_libraries: list[str] = []
        for lib_candidate in (
            target_ref.library_id,
            source_ref.library_id,
            source_pair[0],
        ):
            cleaned = str(lib_candidate or "").strip()
            if cleaned and cleaned not in candidate_libraries:
                candidate_libraries.append(cleaned)

        if not fallback_library_resolved:
            fallback_library_resolved = True
            fallback_library_id = _fetch_whoami_user_id(
                base_url=target_base_url,
                headers=target_headers,
                survey_id=target_survey_id,
            )
        if fallback_library_id and fallback_library_id not in candidate_libraries:
            candidate_libraries.append(fallback_library_id)

        if not candidate_libraries:
            raise RuntimeError(
                "Unable to determine a target library for EOS repair."
            )

        category = str(source_message_payload.get("category") or "endOfSurvey").strip()
        if not category:
            category = "endOfSurvey"
        description = str(source_message_payload.get("description") or "").strip()
        messages = dict(source_message_payload.get("messages") or {})
        create_payload = {
            "category": category,
            "description": description or "EOS message (migrated by qsync repair)",
            "messages": messages,
        }

        created_pair: tuple[str, str] | None = None
        create_errors: list[str] = []
        for target_library_id in candidate_libraries:
            try:
                create_resp = send_api_request(
                    action="qsync.eos.repair.cross_account.create",
                    method="POST",
                    base_url=target_base_url,
                    headers=target_headers,
                    path=f"libraries/{target_library_id}/messages",
                    survey_id=target_survey_id,
                    json=create_payload,
                    timeout=60,
                )
                created_raw = create_resp.json() if hasattr(create_resp, "json") else {}
                created_result = (
                    created_raw.get("result") if isinstance(created_raw, dict) else None
                ) or {}
                created_message_id = _parse_created_message_id(
                    created_result=created_result,
                    library_id=target_library_id,
                    source_ref=source_pair,
                )
                send_api_request(
                    action="qsync.eos.repair.cross_account.put",
                    method="PUT",
                    base_url=target_base_url,
                    headers=target_headers,
                    path=f"libraries/{target_library_id}/messages/{created_message_id}",
                    survey_id=target_survey_id,
                    json=_build_put_payload({"messages": messages}),
                    timeout=60,
                )
                created_pair = (target_library_id, created_message_id)
                break
            except Exception as create_exc:  # noqa: BLE001
                create_errors.append(f"{target_library_id}: {create_exc}")

        if created_pair is None:
            raise RuntimeError(
                "Failed to create EOS message in target account for "
                f"{source_pair[0]}/{source_pair[1]}. "
                f"Tried libraries: {', '.join(candidate_libraries)}. "
                f"Errors: {' | '.join(create_errors)}"
            )

        created_by_source[source_pair] = created_pair
        replacements[target_pair] = created_pair

    if dry_run:
        return CrossAccountEosRepairResult(
            source_survey_id=source_survey_id,
            target_survey_id=target_survey_id,
            target_refs_total=len(target_refs),
            source_refs_total=len(source_refs),
            missing_refs=missing_refs,
            planned_rewire_count=len(planned_imports),
            planned_imports=planned_imports,
            created_pairs={},
            replacements={},
            updated_flow_ids=[],
            pulled_paths=[],
            warnings=warnings,
            dry_run=True,
        )

    target_result = target_payload.get("result") or {}
    flow = target_result.get("SurveyFlow") or target_result.get("Flow") or {}
    if not isinstance(flow, dict):
        raise RuntimeError(
            f"Target survey {target_survey_id} has invalid SurveyFlow payload."
        )

    updated_flow_ids, updated_count = _rewrite_end_survey_refs_in_flow(
        flow=flow, replacements=replacements
    )

    if updated_count > 0:
        ensure_backup(target_survey_id)
        send_api_request(
            action="qsync.eos.repair.cross_account.push_flow",
            method="PUT",
            base_url=target_base_url,
            headers=target_headers,
            path=f"survey-definitions/{target_survey_id}/flow",
            survey_id=target_survey_id,
            json=flow,
            timeout=60,
            log_meta={
                "context": {
                    "origin": "qsync.eos.repair.cross_account",
                    "source_survey_id": source_survey_id,
                    "replacements": [
                        {
                            "from_library_id": old_lib,
                            "from_message_id": old_msg,
                            "to_library_id": new_lib,
                            "to_message_id": new_msg,
                        }
                        for (old_lib, old_msg), (new_lib, new_msg) in sorted(
                            replacements.items()
                        )
                    ],
                }
            },
        )

        if publish:
            description = publish_description or (
                f"qsync eos repair cross-account from {source_survey_id}"
            )
            if len(description) > 140:
                description = description[:140]
            publish_survey_definition(
                target_survey_id,
                description=description,
                base_url=target_base_url,
                headers=target_headers,
                context={"origin": "qsync.eos.repair.cross_account"},
            )

    refresh_survey_cache(target_survey_id)
    rewritten_refs = _apply_replacements_to_refs(
        survey_id=target_survey_id,
        refs=target_refs,
        replacements=replacements,
    )
    pull_result = pull_eos_messages_best_effort(
        survey_id=target_survey_id,
        base_url=target_base_url,
        headers=target_headers,
        include_backups_scan=include_backups_scan,
        check_drift=False,
        refs=rewritten_refs,
        action="qsync.eos.repair.cross_account.pull",
    )
    warnings.extend(pull_result.warnings)

    return CrossAccountEosRepairResult(
        source_survey_id=source_survey_id,
        target_survey_id=target_survey_id,
        target_refs_total=len(target_refs),
        source_refs_total=len(source_refs),
        missing_refs=missing_refs,
        planned_rewire_count=0,
        planned_imports=[],
        created_pairs=created_by_source,
        replacements=replacements,
        updated_flow_ids=sorted(set(updated_flow_ids)),
        pulled_paths=pull_result.pulled_paths,
        warnings=warnings,
        dry_run=False,
    )


def repair_eos_messages_from_source_accounts(
    *,
    target_survey_id: str,
    target_base_url: str,
    target_headers: dict[str, str],
    source_accounts: list[EosSourceAccount],
    include_backups_scan: bool,
    dry_run: bool,
    publish: bool,
    publish_description: str | None = None,
) -> CrossAccountEosRepairResult:
    """Repair missing EOS refs by probing multiple source accounts for matching IDs."""

    target_payload = _fetch_survey_definition_for_account(
        base_url=target_base_url,
        headers=target_headers,
        survey_id=target_survey_id,
        action="qsync.eos.repair.auto_source.fetch_target",
    )
    target_refs = extract_eos_message_refs(target_survey_id, target_payload)
    if not target_refs:
        return CrossAccountEosRepairResult(
            source_survey_id="auto-source",
            target_survey_id=target_survey_id,
            target_refs_total=0,
            source_refs_total=0,
            missing_refs=0,
            planned_rewire_count=0,
            planned_imports=[],
            created_pairs={},
            replacements={},
            updated_flow_ids=[],
            pulled_paths=[],
            warnings=[],
            dry_run=dry_run,
        )

    if not source_accounts:
        raise RuntimeError(
            "No source accounts available for auto-source EOS repair."
        )

    warnings: list[str] = []
    fallback_library_id: str | None = None
    fallback_library_resolved = False

    source_messages: dict[tuple[str, str, str], dict] = {}
    created_by_source: dict[tuple[str, str, str], tuple[str, str]] = {}
    replacements: dict[tuple[str, str], tuple[str, str]] = {}
    planned_imports: list[CrossAccountPlannedImport] = []
    missing_refs = 0
    matched_refs = 0

    def _fetch_from_any_source(
        library_id: str, message_id: str
    ) -> tuple[str, dict] | None:
        for source in source_accounts:
            try:
                payload = _fetch_library_message(
                    base_url=source.base_url,
                    headers=source.headers,
                    survey_id=target_survey_id,
                    library_id=library_id,
                    message_id=message_id,
                    action="qsync.eos.repair.auto_source.get_source",
                )
                return source.label, payload
            except Exception as exc:  # noqa: BLE001
                if _is_missing_library_message_error(exc):
                    continue
                warnings.append(
                    f"Source account '{source.label}' lookup failed for "
                    f"{library_id}/{message_id}: {exc}"
                )
        return None

    for target_ref in target_refs:
        target_pair = (target_ref.library_id, target_ref.message_id)
        try:
            _fetch_library_message(
                base_url=target_base_url,
                headers=target_headers,
                survey_id=target_survey_id,
                library_id=target_ref.library_id,
                message_id=target_ref.message_id,
                action="qsync.eos.repair.auto_source.target_get",
            )
            continue
        except Exception as exc:  # noqa: BLE001
            if not _is_missing_library_message_error(exc):
                raise

        missing_refs += 1
        source_hit = _fetch_from_any_source(
            library_id=target_ref.library_id,
            message_id=target_ref.message_id,
        )
        if source_hit is None:
            warnings.append(
                "Missing EOS reference not found in any source account: "
                f"{target_ref.library_id}/{target_ref.message_id}"
            )
            continue

        source_label, source_payload = source_hit
        matched_refs += 1

        if dry_run:
            planned_imports.append(
                CrossAccountPlannedImport(
                    target_library_id=target_ref.library_id,
                    target_message_id=target_ref.message_id,
                    source_library_id=target_ref.library_id,
                    source_message_id=target_ref.message_id,
                    target_create_library_id=target_ref.library_id,
                    source_account_label=source_label,
                )
            )
            continue

        source_key = (source_label, target_ref.library_id, target_ref.message_id)
        source_messages[source_key] = source_payload

        existing = created_by_source.get(source_key)
        if existing:
            replacements[target_pair] = existing
            continue

        if not fallback_library_resolved:
            fallback_library_resolved = True
            fallback_library_id = _fetch_whoami_user_id(
                base_url=target_base_url,
                headers=target_headers,
                survey_id=target_survey_id,
            )

        candidate_libraries: list[str] = []
        for lib_candidate in (target_ref.library_id, fallback_library_id):
            cleaned = str(lib_candidate or "").strip()
            if cleaned and cleaned not in candidate_libraries:
                candidate_libraries.append(cleaned)
        if not candidate_libraries:
            raise RuntimeError(
                "Unable to determine target library for auto-source EOS repair."
            )

        category = str(source_payload.get("category") or "endOfSurvey").strip() or "endOfSurvey"
        description = str(source_payload.get("description") or "").strip()
        messages = dict(source_payload.get("messages") or {})
        create_payload = {
            "category": category,
            "description": description or "EOS message (auto-repaired by qsync)",
            "messages": messages,
        }

        created_pair: tuple[str, str] | None = None
        create_errors: list[str] = []
        for target_library_id in candidate_libraries:
            try:
                create_resp = send_api_request(
                    action="qsync.eos.repair.auto_source.create",
                    method="POST",
                    base_url=target_base_url,
                    headers=target_headers,
                    path=f"libraries/{target_library_id}/messages",
                    survey_id=target_survey_id,
                    json=create_payload,
                    timeout=60,
                )
                created_raw = create_resp.json() if hasattr(create_resp, "json") else {}
                created_result = (
                    created_raw.get("result") if isinstance(created_raw, dict) else None
                ) or {}
                created_message_id = _parse_created_message_id(
                    created_result=created_result,
                    library_id=target_library_id,
                    source_ref=(target_ref.library_id, target_ref.message_id),
                )
                send_api_request(
                    action="qsync.eos.repair.auto_source.put",
                    method="PUT",
                    base_url=target_base_url,
                    headers=target_headers,
                    path=f"libraries/{target_library_id}/messages/{created_message_id}",
                    survey_id=target_survey_id,
                    json=_build_put_payload({"messages": messages}),
                    timeout=60,
                )
                created_pair = (target_library_id, created_message_id)
                break
            except Exception as create_exc:  # noqa: BLE001
                create_errors.append(f"{target_library_id}: {create_exc}")

        if created_pair is None:
            raise RuntimeError(
                "Failed to create EOS message in target account for "
                f"{target_ref.library_id}/{target_ref.message_id}. "
                f"Tried libraries: {', '.join(candidate_libraries)}. "
                f"Errors: {' | '.join(create_errors)}"
            )

        created_by_source[source_key] = created_pair
        replacements[target_pair] = created_pair

    if dry_run:
        return CrossAccountEosRepairResult(
            source_survey_id="auto-source",
            target_survey_id=target_survey_id,
            target_refs_total=len(target_refs),
            source_refs_total=matched_refs,
            missing_refs=missing_refs,
            planned_rewire_count=len(planned_imports),
            planned_imports=planned_imports,
            created_pairs={},
            replacements={},
            updated_flow_ids=[],
            pulled_paths=[],
            warnings=warnings,
            dry_run=True,
        )

    target_result = target_payload.get("result") or {}
    flow = target_result.get("SurveyFlow") or target_result.get("Flow") or {}
    if not isinstance(flow, dict):
        raise RuntimeError(
            f"Target survey {target_survey_id} has invalid SurveyFlow payload."
        )

    updated_flow_ids, updated_count = _rewrite_end_survey_refs_in_flow(
        flow=flow,
        replacements=replacements,
    )

    if updated_count > 0:
        ensure_backup(target_survey_id)
        send_api_request(
            action="qsync.eos.repair.auto_source.push_flow",
            method="PUT",
            base_url=target_base_url,
            headers=target_headers,
            path=f"survey-definitions/{target_survey_id}/flow",
            survey_id=target_survey_id,
            json=flow,
            timeout=60,
            log_meta={
                "context": {
                    "origin": "qsync.eos.repair.auto_source",
                    "replacements": [
                        {
                            "from_library_id": old_lib,
                            "from_message_id": old_msg,
                            "to_library_id": new_lib,
                            "to_message_id": new_msg,
                        }
                        for (old_lib, old_msg), (new_lib, new_msg) in sorted(
                            replacements.items()
                        )
                    ],
                }
            },
        )

        if publish:
            description = publish_description or "qsync eos repair auto-source"
            if len(description) > 140:
                description = description[:140]
            publish_survey_definition(
                target_survey_id,
                description=description,
                base_url=target_base_url,
                headers=target_headers,
                context={"origin": "qsync.eos.repair.auto_source"},
            )

    refresh_survey_cache(target_survey_id)
    rewritten_refs = _apply_replacements_to_refs(
        survey_id=target_survey_id,
        refs=target_refs,
        replacements=replacements,
    )
    pull_result = pull_eos_messages_best_effort(
        survey_id=target_survey_id,
        base_url=target_base_url,
        headers=target_headers,
        include_backups_scan=include_backups_scan,
        check_drift=False,
        refs=rewritten_refs,
        action="qsync.eos.repair.auto_source.pull",
    )
    warnings.extend(pull_result.warnings)

    # created_pairs currently keyed by source-account+source-ref; flatten for display.
    flattened_created: dict[tuple[str, str], tuple[str, str]] = {}
    for (_label, src_lib, src_msg), created_pair in created_by_source.items():
        flattened_created[(src_lib, src_msg)] = created_pair

    return CrossAccountEosRepairResult(
        source_survey_id="auto-source",
        target_survey_id=target_survey_id,
        target_refs_total=len(target_refs),
        source_refs_total=matched_refs,
        missing_refs=missing_refs,
        planned_rewire_count=0,
        planned_imports=[],
        created_pairs=flattened_created,
        replacements=replacements,
        updated_flow_ids=sorted(set(updated_flow_ids)),
        pulled_paths=pull_result.pulled_paths,
        warnings=warnings,
        dry_run=False,
    )


def _fetch_survey_definition_for_account(
    *,
    base_url: str,
    headers: dict[str, str],
    survey_id: str,
    action: str,
) -> dict:
    resp = send_api_request(
        action=action,
        method="GET",
        base_url=base_url,
        headers=headers,
        path=f"survey-definitions/{survey_id}",
        survey_id=survey_id,
        log_event=False,
        timeout=60,
    )
    payload = resp.json()
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        raise RuntimeError(f"survey-definitions/{survey_id} response missing result object.")
    flow = result.get("SurveyFlow") or result.get("Flow") or {}
    if not isinstance(flow, dict):
        raise RuntimeError(f"survey-definitions/{survey_id} response has invalid SurveyFlow.")
    return payload


def _fetch_library_message(
    *,
    base_url: str,
    headers: dict[str, str],
    survey_id: str,
    library_id: str,
    message_id: str,
    action: str,
) -> dict:
    resp = send_api_request(
        action=action,
        method="GET",
        base_url=base_url,
        headers=headers,
        path=f"libraries/{library_id}/messages/{message_id}",
        survey_id=survey_id,
        log_event=False,
        timeout=60,
    )
    return _coerce_result_payload(resp.json())


def _is_missing_library_message_error(exc: Exception) -> bool:
    if not isinstance(exc, requests.HTTPError):
        return False
    response = getattr(exc, "response", None)
    status = int(getattr(response, "status_code", 0) or 0)
    # Cross-account copies can return 403 (library exists in another account but
    # is inaccessible), which is effectively "missing" for repair purposes.
    return status in {400, 403, 404}


def _map_source_refs_to_target_refs(
    *,
    target_refs: list[EndSurveyMessageRef],
    source_refs: list[EndSurveyMessageRef],
) -> list[EndSurveyMessageRef]:
    source_by_flow_id = {r.flow_id: r for r in source_refs if r.flow_id}
    mapped: list[EndSurveyMessageRef] = []
    for idx, target_ref in enumerate(target_refs):
        source_ref = source_by_flow_id.get(target_ref.flow_id)
        if source_ref is None and idx < len(source_refs):
            source_ref = source_refs[idx]
        mapped.append(source_ref or target_ref)
    return mapped


def _fetch_whoami_user_id(
    *,
    base_url: str,
    headers: dict[str, str],
    survey_id: str,
) -> str | None:
    try:
        resp = send_api_request(
            action="qsync.eos.repair.cross_account.whoami",
            method="GET",
            base_url=base_url,
            headers=headers,
            path="whoami",
            survey_id=survey_id,
            log_event=False,
            timeout=30,
        )
        payload = resp.json()
        result = payload.get("result") if isinstance(payload, dict) else {}
        if isinstance(result, dict):
            user_id = str(result.get("userId") or "").strip()
            if user_id:
                return user_id
    except Exception:
        return None
    return None


def _parse_created_message_id(
    *,
    created_result: dict,
    library_id: str,
    source_ref: tuple[str, str],
) -> str:
    for key in ("messageId", "messageID", "MessageID", "id", "ID"):
        value = created_result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise RuntimeError(
        "Unable to parse created EOS message id from API response for "
        f"{library_id} (source {source_ref[0]}/{source_ref[1]})."
    )


def _rewrite_end_survey_refs_in_flow(
    *,
    flow: dict,
    replacements: dict[tuple[str, str], tuple[str, str]],
) -> tuple[list[str], int]:
    updated_flow_ids: list[str] = []
    updated_count = 0

    def walk(node: object) -> None:
        nonlocal updated_count
        if node is None:
            return
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        if not isinstance(node, dict):
            return

        if str(node.get("Type") or "") == "EndSurvey":
            opts = node.get("Options") or {}
            if (opts.get("SurveyTermination") or "") == "DisplayMessage":
                lib_id = str(opts.get("EOSMessageLibrary") or "").strip()
                msg_id = str(opts.get("EOSMessage") or "").strip()
                new_pair = replacements.get((lib_id, msg_id))
                if new_pair and new_pair != (lib_id, msg_id):
                    opts["EOSMessageLibrary"] = new_pair[0]
                    opts["EOSMessage"] = new_pair[1]
                    node["Options"] = opts
                    updated_count += 1
                    flow_id = str(node.get("FlowID") or "").strip()
                    if flow_id:
                        updated_flow_ids.append(flow_id)

        for key in ("Flow", "Then", "Else", "ElseFlow"):
            if key in node:
                walk(node.get(key))

    walk(flow.get("Flow"))
    return updated_flow_ids, updated_count


def _apply_replacements_to_refs(
    *,
    survey_id: str,
    refs: list[EndSurveyMessageRef],
    replacements: dict[tuple[str, str], tuple[str, str]],
) -> list[EndSurveyMessageRef]:
    rewritten: list[EndSurveyMessageRef] = []
    seen: set[tuple[str, str]] = set()
    for ref in refs:
        new_library_id, new_message_id = replacements.get(
            (ref.library_id, ref.message_id),
            (ref.library_id, ref.message_id),
        )
        pair = (new_library_id, new_message_id)
        if pair in seen:
            continue
        seen.add(pair)
        rewritten.append(
            EndSurveyMessageRef(
                survey_id=survey_id,
                flow_id=ref.flow_id,
                library_id=new_library_id,
                message_id=new_message_id,
            )
        )
    return rewritten


def _messages_equal(a: dict, b: dict) -> bool:
    return (a.get("messages") or {}) == (b.get("messages") or {})


def _raise_shared_message_blocked(
    *,
    survey_id: str,
    shared: set[tuple[str, str]],
    include_backups_scan: bool,
    verb: str,
    override_flag: str,
) -> None:
    contexts_by_ref = find_message_contexts(
        refs=set(shared),
        include_backups=include_backups_scan,
    )

    lines: list[str] = []
    for lib_id, msg_id in sorted(shared):
        contexts = contexts_by_ref.get((lib_id, msg_id)) or []
        backups_flag = " --include-backups-scan" if include_backups_scan else ""
        other_surveys = sorted(
            {str(c.get("survey_id") or "") for c in contexts if c.get("survey_id")}
            - {survey_id}
        )
        if other_surveys:
            lines.append(
                f"- {lib_id}/{msg_id} (other surveys: {len(other_surveys)}: {', '.join(other_surveys)})\n"
                f"  Inspect: qsync eos references --library-id {lib_id} --message-id {msg_id}{backups_flag}"
            )
        else:
            lines.append(
                f"- {lib_id}/{msg_id} (other surveys: none found)\n"
                f"  Inspect: qsync eos references --library-id {lib_id} --message-id {msg_id}{backups_flag}"
            )

    scan_scope = (
        "surveys/ + surveys/backups (local scan)"
        if include_backups_scan
        else "surveys/ (local scan)"
    )

    message = (
        f"{ERROR_ID_EOS_SHARED_MESSAGE}: Shared EOS library message detected; refusing to {verb}.\n"
        f"Scan scope: {scan_scope}\n"
        f"Affected message(s):\n" + "\n".join(lines) + "\n\n"
        "Risk: Editing a shared library message updates every survey that references it.\n"
        "Next steps:\n"
        "  1. Inspect references (local scan): run the `qsync eos references ...` command shown above.\n"
        "  2. If you intend the change to apply everywhere, re-run with:\n"
        f"     {override_flag}\n"
        "  3. Otherwise, avoid editing the shared message; create a survey-specific message instead.\n"
    )

    log_push_event(
        action=f"qsync.eos.shared_message_blocked.{verb}",
        method="LOCAL",
        path=f"eos_messages.{verb}",
        survey_id=survey_id,
        status=None,
        error={
            "error_id": ERROR_ID_EOS_SHARED_MESSAGE,
            "message": "Shared EOS library message detected",
            "shared_refs": [
                {"library_id": a, "message_id": b} for a, b in sorted(shared)
            ],
            "include_backups_scan": include_backups_scan,
        },
    )
    raise EosSharedMessageError(message)


def get_eos_message_references(
    *,
    library_id: str,
    message_id: str,
    include_backups_scan: bool,
) -> list[dict[str, Any]]:
    """Return local usage contexts for a single EOS library message ref."""

    contexts_by_ref = find_message_contexts(
        refs={(library_id, message_id)},
        include_backups=include_backups_scan,
    )
    return contexts_by_ref.get((library_id, message_id)) or []


def _latest_backup_result(library_id: str, message_id: str) -> dict | None:
    base = message_dir(library_id, message_id)
    backups = base / "backups"
    if not backups.exists():
        return None
    files = sorted(backups.glob("*.json"))
    if not files:
        return None
    latest_path = files[-1]
    try:
        payload = json.loads(latest_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return _coerce_result_payload(payload)


# ---------------------------
# Local codec
# ---------------------------


def write_library_message_to_disk(
    *,
    library_id: str,
    message_id: str,
    api_payload: dict,
    contexts: list[dict[str, Any]] | None = None,
    account: str | None = None,
) -> Path:
    """Write a library message payload to disk and return its folder path."""

    base = message_dir(library_id, message_id, account=account)
    base.mkdir(parents=True, exist_ok=True)
    (base / "messages").mkdir(parents=True, exist_ok=True)
    (base / "backups").mkdir(parents=True, exist_ok=True)

    result = _coerce_result_payload(api_payload)
    meta = dict(result)
    messages = meta.pop("messages", {}) or {}

    meta_path = base / "meta.json"
    meta_payload = {
        "library_id": library_id,
        "message_id": message_id,
        "pulled_at": _now_iso(),
        "meta": meta,
    }
    meta_path.write_text(json.dumps(meta_payload, indent=2), encoding="utf-8")

    keys_path = base / "messages" / "_keys.json"
    keys_entries: list[dict[str, str]] = []
    for key in messages.keys():
        safe = _encode_key(key)
        keys_entries.append({"key": key, "file": f"{safe}.html"})
    keys_path.write_text(
        json.dumps({"entries": keys_entries}, indent=2), encoding="utf-8"
    )

    for entry in keys_entries:
        key = entry["key"]
        file_name = entry["file"]
        (base / "messages" / file_name).write_text(
            str(messages.get(key) or ""), encoding="utf-8"
        )

    # contexts.json is derived: we update it on pull as a convenience.
    contexts_path = base / "contexts.json"
    contexts_payload = list(contexts or [])
    contexts_path.write_text(json.dumps(contexts_payload, indent=2), encoding="utf-8")

    # Also keep a raw snapshot for rollback/debugging.
    _write_backup_snapshot(library_id, message_id, api_payload)
    return base


def read_library_message_from_disk(library_id: str, message_id: str) -> dict | None:
    base = message_dir(library_id, message_id)
    meta_path = base / "meta.json"
    keys_path = base / "messages" / "_keys.json"
    if not meta_path.exists() or not keys_path.exists():
        return None

    meta_raw = json.loads(meta_path.read_text(encoding="utf-8"))
    meta = meta_raw.get("meta") or {}

    keys = _load_keys(keys_path)
    messages: dict[str, str] = {}
    for entry in keys:
        key = entry["key"]
        file_name = entry["file"]
        content_path = base / "messages" / file_name
        if not content_path.exists():
            continue
        messages[key] = content_path.read_text(encoding="utf-8")

    payload: dict[str, Any] = dict(meta)
    payload["messages"] = messages
    return payload


def diff_library_messages(a: dict, b: dict, *, detailed: bool) -> list[str]:
    """Return unified diffs for changed keys (messages only)."""

    a_msgs = a.get("messages", {}) or {}
    b_msgs = b.get("messages", {}) or {}
    keys = sorted(set(a_msgs.keys()) | set(b_msgs.keys()))
    out: list[str] = []
    for key in keys:
        left = str(a_msgs.get(key) or "")
        right = str(b_msgs.get(key) or "")
        if left == right:
            continue
        left_norm = _normalize_for_preview(left)
        right_norm = _normalize_for_preview(right)
        if left_norm == right_norm:
            out.append(f"key={key}: whitespace-only change (preview normalization)")
        else:
            out.append(f"key={key}: changed")
        if detailed:
            out.extend(
                difflib.unified_diff(
                    right.splitlines(),
                    left.splitlines(),
                    fromfile="remote [Qualtrics]",
                    tofile="local [disk]",
                    lineterm="",
                )
            )
    return out


def _build_put_payload(result_payload: dict) -> dict:
    """Build a PUT payload from a `result`-shaped dict."""

    # Qualtrics rejects unknown fields for this endpoint (e.g. "category").
    # Keep the payload minimal and only send the messages map.
    return {"messages": dict(result_payload.get("messages") or {})}


def _coerce_result_payload(api_payload: dict) -> dict:
    if (
        isinstance(api_payload, dict)
        and "result" in api_payload
        and isinstance(api_payload["result"], dict)
    ):
        return dict(api_payload["result"])
    return dict(api_payload or {})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _encode_key(key: str) -> str:
    raw = key.encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"k_{b64}" if b64 else "k_empty"


def _load_keys(keys_path: Path) -> list[dict[str, str]]:
    data = json.loads(keys_path.read_text(encoding="utf-8"))
    entries = data.get("entries") or []
    out: list[dict[str, str]] = []
    for e in entries:
        key = str(e.get("key") or "")
        file_name = str(e.get("file") or "")
        if not key or not file_name:
            continue
        if "/" in file_name or "\\" in file_name:
            raise RuntimeError(f"Unsafe filename in _keys.json: {file_name}")
        out.append({"key": key, "file": file_name})
    return out


def _write_backup_snapshot(library_id: str, message_id: str, api_payload: dict) -> None:
    base = message_dir(library_id, message_id)
    backups = base / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = backups / f"{stamp}.json"
    path.write_text(json.dumps(api_payload, indent=2), encoding="utf-8")


def detect_shared_messages(
    *,
    survey_id: str,
    refs: set[tuple[str, str]],
    include_backups: bool,
) -> set[tuple[str, str]]:
    """Detect shared messages by scanning locally cached surveys (local-only)."""

    root = resolve_root(required=False) or Path.cwd()
    surveys_dir = resolve_survey_cache_dir(root=root)
    candidates: list[Path] = []
    if surveys_dir.exists():
        candidates.extend(sorted(surveys_dir.glob("*.json")))
        if include_backups:
            backups = surveys_dir / "backups"
            if backups.exists():
                candidates.extend(sorted(backups.glob("*.json")))

    shared: set[tuple[str, str]] = set()
    for path in candidates:
        if not path.name.endswith(".json"):
            continue
        # Best-effort: parse and scan. Ignore malformed JSON.
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        # Skip the initiating survey's own cached file(s) by survey ID suffix match.
        if f"__{survey_id}.json" in path.name:
            continue
        for lib_id, msg_id in _extract_refs_for_scan(payload):
            if (lib_id, msg_id) in refs:
                shared.add((lib_id, msg_id))
    return shared


def _extract_refs_for_scan(payload: dict) -> set[tuple[str, str]]:
    result = payload.get("result", {}) if isinstance(payload, dict) else {}
    flow = result.get("SurveyFlow") or result.get("Flow") or {}
    if not isinstance(flow, dict):
        return set()
    found: set[tuple[str, str]] = set()

    def walk(node: object) -> None:
        if node is None:
            return
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        if not isinstance(node, dict):
            return
        if str(node.get("Type") or "") == "EndSurvey":
            opts = node.get("Options") or {}
            if (opts.get("SurveyTermination") or "") == "DisplayMessage":
                lib_id = str(opts.get("EOSMessageLibrary") or "").strip()
                msg_id = str(opts.get("EOSMessage") or "").strip()
                if lib_id and msg_id:
                    found.add((lib_id, msg_id))
        for key in ("Flow", "Then", "Else", "ElseFlow"):
            if key in node:
                walk(node.get(key))

    walk(flow.get("Flow"))
    return found


def _prompt_confirmation(message: str) -> bool:
    try:
        from ..interactive_menu import confirm

        return confirm(message, default=True)
    except Exception:
        resp = input(f"{message} [Y/n] ").strip().lower()
        if not resp:
            return True
        return resp in {"y", "yes"}


def confirm_shared_override(*, shared: set[tuple[str, str]], yes: bool) -> None:
    """Require explicit confirmation before proceeding with shared edits."""

    if not shared:
        return
    if yes:
        return
    msg = (
        "Shared library message(s) detected via local scan:\n"
        + "\n".join([f"- {a}/{b}" for a, b in sorted(shared)])
        + "\nProceed anyway?"
    )
    if not _prompt_confirmation(msg):
        raise RuntimeError("Aborted.")


def find_message_contexts(
    *,
    refs: set[tuple[str, str]] | None,
    include_backups: bool,
    account: str | None = None,
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Return local usage contexts for message refs by scanning cached surveys.

    This is local-only and based on what's currently present under surveys/.
    """

    root = resolve_root(required=False) or Path.cwd()
    surveys_dir = resolve_survey_cache_dir(root=root, account=account)
    candidates: list[Path] = []
    if surveys_dir.exists():
        candidates.extend(sorted(surveys_dir.glob("*.json")))
        if include_backups:
            backups = surveys_dir / "backups"
            if backups.exists():
                candidates.extend(sorted(backups.glob("*.json")))

    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        sid = _survey_id_from_path(path)
        if not sid:
            continue
        result = payload.get("result", {}) if isinstance(payload, dict) else {}
        flow = result.get("SurveyFlow") or result.get("Flow") or {}
        if not isinstance(flow, dict):
            continue
        for lib_id, msg_id, flow_id in _extract_end_survey_refs_with_flow_ids(flow):
            key = (lib_id, msg_id)
            if refs is not None and key not in refs:
                continue
            out.setdefault(key, []).append(
                {
                    "survey_id": sid,
                    "flow_id": flow_id,
                    "source": SURVEY_SOURCE,
                }
            )

    # Deduplicate contexts per ref (survey_id + flow_id).
    for key, contexts in out.items():
        seen: set[tuple[str, str | None]] = set()
        deduped: list[dict[str, Any]] = []
        for ctx in contexts:
            tup = (str(ctx.get("survey_id") or ""), ctx.get("flow_id"))
            if tup in seen:
                continue
            seen.add(tup)
            deduped.append(ctx)
        out[key] = deduped
    return out


def _extract_end_survey_refs_with_flow_ids(
    flow: dict,
) -> list[tuple[str, str, str | None]]:
    found: list[tuple[str, str, str | None]] = []

    def walk(node: object) -> None:
        if node is None:
            return
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        if not isinstance(node, dict):
            return

        if str(node.get("Type") or "") == "EndSurvey":
            opts = node.get("Options") or {}
            if (opts.get("SurveyTermination") or "") == "DisplayMessage":
                lib_id = str(opts.get("EOSMessageLibrary") or "").strip()
                msg_id = str(opts.get("EOSMessage") or "").strip()
                if lib_id and msg_id:
                    found.append(
                        (lib_id, msg_id, str(node.get("FlowID") or "") or None)
                    )

        for key in ("Flow", "Then", "Else", "ElseFlow"):
            if key in node:
                walk(node.get(key))

    walk(flow.get("Flow"))
    return found


def _survey_id_from_path(path: Path) -> str | None:
    name = path.name
    if name.endswith(".json"):
        name = name[:-5]
    # Common pattern: <SurveyName>__<SurveyID>
    if "__" in name:
        tail = name.split("__")[-1]
        if tail.startswith("SV_"):
            return tail
    # Legacy pattern: <SurveyID>-<SurveyName>
    if name.startswith("SV_") and "-" in name:
        return name.split("-", 1)[0]
    if name.startswith("SV_"):
        return name
    return None


def _format_used_by(contexts: list[dict[str, Any]] | None) -> str:
    if not contexts:
        return ""
    survey_ids = sorted(
        {str(c.get("survey_id") or "") for c in contexts if c.get("survey_id")}
    )
    if not survey_ids:
        return ""
    joined = ", ".join(survey_ids)
    return f" (used by: {joined})"


def _normalize_for_preview(value: str) -> str:
    text = str(value or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in text.split("\n")]
    # Drop trailing blank lines after rstrip normalization.
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)
