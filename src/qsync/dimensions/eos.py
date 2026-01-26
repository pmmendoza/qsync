from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .types import DimensionChanges
from .eos_core import (
    _latest_backup_result,
    apply_eos_messages,
    extract_eos_message_refs,
    message_dir,
    push_eos_messages,
    read_library_message_from_disk,
)
from ..qualtrics_client import load_cached_survey
from ..pending_stage import EosPendingPayload, clear_pending, load_pending
from ..scope_filter import ScopeFilter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _EosCacheEntry:
    signature: tuple
    payload_hash: str


_EOS_DISK_CACHE: dict[tuple[str, str], _EosCacheEntry] = {}
_EOS_BASELINE_CACHE: dict[tuple[str, str], _EosCacheEntry] = {}


def _path_signature(path: Path) -> tuple[str, int, int]:
    stat = path.stat()
    return (str(path), stat.st_mtime_ns, stat.st_size)


def _load_keys_from_path(path: Path) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    results: list[dict[str, str]] = []
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("file"), str):
            results.append(entry)
    return results


def _disk_message_signature(library_id: str, message_id: str) -> tuple | None:
    base = message_dir(library_id, message_id)
    meta_path = base / "meta.json"
    keys_path = base / "messages" / "_keys.json"
    if not meta_path.exists() or not keys_path.exists():
        return None
    try:
        keys = _load_keys_from_path(keys_path)
    except Exception:
        return None
    message_sigs: list[tuple[str, int, int]] = []
    for entry in keys:
        file_name = entry.get("file")
        if not file_name:
            continue
        file_path = base / "messages" / file_name
        if not file_path.exists():
            return None
        stat = file_path.stat()
        message_sigs.append((str(file_path), stat.st_mtime_ns, stat.st_size))
    message_sigs.sort()
    return (_path_signature(meta_path), _path_signature(keys_path), tuple(message_sigs))


def _latest_backup_path(library_id: str, message_id: str) -> Path | None:
    base = message_dir(library_id, message_id)
    backups = base / "backups"
    if not backups.exists():
        return None
    files = sorted(backups.glob("*.json"))
    if not files:
        return None
    return files[-1]


def _cached_disk_hash(library_id: str, message_id: str) -> str | None:
    signature = _disk_message_signature(library_id, message_id)
    if signature is None:
        return None
    cache_key = (library_id, message_id)
    cached = _EOS_DISK_CACHE.get(cache_key)
    if cached and cached.signature == signature:
        return cached.payload_hash

    payload = read_library_message_from_disk(library_id, message_id)
    if payload is None:
        return None
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    payload_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    _EOS_DISK_CACHE[cache_key] = _EosCacheEntry(
        signature=signature, payload_hash=payload_hash
    )
    return payload_hash


def _cached_baseline_hash(library_id: str, message_id: str) -> str | None:
    latest_path = _latest_backup_path(library_id, message_id)
    if latest_path is None:
        return None
    signature = _path_signature(latest_path)
    cache_key = (library_id, message_id)
    cached = _EOS_BASELINE_CACHE.get(cache_key)
    if cached and cached.signature == signature:
        return cached.payload_hash

    payload = _latest_backup_result(library_id, message_id)
    if payload is None:
        return None
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    payload_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    _EOS_BASELINE_CACHE[cache_key] = _EosCacheEntry(
        signature=signature, payload_hash=payload_hash
    )
    return payload_hash


def _format_missing_refs(refs: list[tuple[str, str]], *, label: str) -> str:
    sample = ", ".join([f"{lib}/{msg}" for lib, msg in refs[:3]])
    suffix = f" e.g. {sample}" if sample else ""
    return f"{label} ({len(refs)} missing).{suffix}"


def detect_unstaged_changes(survey_id: str) -> DimensionChanges:
    """Detect unstaged EOS message changes based on local content vs backups."""
    try:
        cache = load_cached_survey(survey_id)
        refs = extract_eos_message_refs(survey_id, cache.payload)
        if not refs:
            return DimensionChanges(
                dimension="eos",
                has_changes=False,
                change_summary="No changes",
                affected_qids=set(),
            )

        missing_messages: list[tuple[str, str]] = []
        missing_baselines: list[tuple[str, str]] = []
        changed_count = 0

        for ref in refs:
            disk_hash = _cached_disk_hash(ref.library_id, ref.message_id)
            if disk_hash is None:
                missing_messages.append((ref.library_id, ref.message_id))
                continue
            baseline_hash = _cached_baseline_hash(ref.library_id, ref.message_id)
            if baseline_hash is None:
                missing_baselines.append((ref.library_id, ref.message_id))
                continue
            if disk_hash != baseline_hash:
                changed_count += 1

        if missing_messages:
            return DimensionChanges(
                dimension="eos",
                has_changes=False,
                change_summary="✗ Error",
                affected_qids=set(),
                error_detail=(
                    f"{_format_missing_refs(missing_messages, label='EOS message files not found')} "
                    f"Run: qsync eos pull --survey-id {survey_id}"
                ),
                safe_to_autofix=True,
            )

        if missing_baselines:
            return DimensionChanges(
                dimension="eos",
                has_changes=False,
                change_summary="✗ Error",
                affected_qids=set(),
                error_detail=(
                    f"{_format_missing_refs(missing_baselines, label='EOS baselines not found')} "
                    f"Run: qsync eos pull --survey-id {survey_id}"
                ),
                safe_to_autofix=True,
            )

        if changed_count:
            return DimensionChanges(
                dimension="eos",
                has_changes=True,
                change_summary=f"⚡ Unstaged: {changed_count} message(s)",
                affected_qids=set(),
            )

    except Exception as e:
        return DimensionChanges(
            dimension="eos",
            has_changes=False,
            change_summary="✗ Error",
            affected_qids=set(),
            error_detail=f"EOS detection failed: {str(e).split(chr(10))[0]}",
            safe_to_autofix=False,
        )

    return DimensionChanges(
        dimension="eos",
        has_changes=False,
        change_summary="No changes",
        affected_qids=set(),
    )


def detect_changes(survey_id: str) -> DimensionChanges:
    """Detect staged or unstaged EOS message changes for a survey."""
    pending = load_pending(survey_id, "eos")
    if pending and isinstance(pending.payload, EosPendingPayload):
        count = len(pending.payload.operations) if pending.payload.operations else 0
        return DimensionChanges(
            dimension="eos",
            has_changes=True,
            change_summary=f"✓ Staged: {count} operations",
            affected_qids=set(),
        )

    return detect_unstaged_changes(survey_id)


def stage(
    survey_id: str,
    *,
    scope: Optional[ScopeFilter] = None,
    allow_drift: bool = False,
    interactive: bool = True,
) -> bool:
    """Stage EOS message changes."""
    apply_eos_messages(
        survey_id=survey_id,
        allow_shared=False,
        allow_destructive=False,
    )
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
    """Push staged EOS message changes."""
    pending = load_pending(survey_id, "eos")
    if pending is None:
        print("[sync:eos] No staged EOS changes found.")
        return True

    push_eos_messages(
        survey_id=survey_id,
        record=pending,
        allow_shared=False,
        yes=True,
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
        clear_pending(survey_id, "eos")
    except Exception as exc:
        warn(
            "[qsync:eos]",
            f"Push succeeded but cache refresh failed: {exc}",
        )
    return True
