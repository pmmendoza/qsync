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

from ..api_push import send_api_request
from ..config import get_client_config, resolve_root
from ..pending_stage import (
    PendingStagedChanges,
    EosPendingPayload,
    EosOperation,
    save_pending,
)
from ..push_logger import log_push_event
from ..qualtrics_client import load_cached_survey
from ..drift_check import check_drift as run_drift_check, enforce_no_drift
from ..push_safeguards import enforce_push_safeguards, SafeguardConfig
from ..auto_publish import auto_publish_after_push

CONTENTS_DIR = "contents"
LIB_MESSAGE_DIR = Path(CONTENTS_DIR) / "qualtrics_library_messages"
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


def message_dir(library_id: str, message_id: str) -> Path:
    root = resolve_root(required=False) or Path.cwd()
    return root / LIB_MESSAGE_DIR / library_id / message_id


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
) -> Path:
    """Write a library message payload to disk and return its folder path."""

    base = message_dir(library_id, message_id)
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
    surveys_dir = root / "surveys"
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
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Return local usage contexts for message refs by scanning cached surveys.

    This is local-only and based on what's currently present under surveys/.
    """

    root = resolve_root(required=False) or Path.cwd()
    surveys_dir = root / "surveys"
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
