"""
Unified pending record schema for all qsync dimensions.

Consolidates separate pending files (pending_push.py, pending_translations.py,
pending_eos.py) into a single, dimension-aware schema with automatic migration.

Storage: surveys/pending/{dimension}/{survey-id}.json

Where dimension is one of: items, edf, js, translations, eos, flow, master
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from .config import resolve_root

DimensionType = Literal["items", "js", "translations", "eos", "flow"]


def _now_iso() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ItemsPendingPayload:
    """Payload for pending items/EDF changes."""

    qids: list[str]
    embedded_fields: list[dict[str, Any]] = field(default_factory=list)
    workbook: Optional[str] = None
    filter_column: Optional[str] = None
    filter_value: Optional[str] = None
    structural_ops: list[dict[str, Any]] = field(default_factory=list)
    structural_summary: dict[str, dict[str, int]] = field(default_factory=dict)
    push_journal: dict[str, Any] = field(default_factory=dict)
    changes: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ItemsPendingPayload":
        return cls(
            qids=list(data.get("qids") or []),
            embedded_fields=list(data.get("embedded_fields") or []),
            workbook=data.get("workbook"),
            filter_column=data.get("filter_column"),
            filter_value=data.get("filter_value"),
            structural_ops=list(data.get("structural_ops") or []),
            structural_summary=dict(data.get("structural_summary") or {}),
            push_journal=dict(data.get("push_journal") or {}),
            changes=list(data.get("changes") or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "qids": list(self.qids),
            "embedded_fields": list(self.embedded_fields),
            "workbook": self.workbook,
            "filter_column": self.filter_column,
            "filter_value": self.filter_value,
            "structural_ops": list(self.structural_ops),
            "structural_summary": dict(self.structural_summary),
            "push_journal": dict(self.push_journal),
            "changes": list(self.changes),
        }


@dataclass
class JsPendingPayload:
    """Payload for pending JS changes."""

    entries: list[dict[str, str]]  # each: {qid, js_file, status}

    @property
    def qids(self) -> list[str]:
        """Extract QIDs from entries."""
        return [entry.get("qid", "") for entry in self.entries]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JsPendingPayload":
        return cls(entries=list(data.get("entries") or []))

    def to_dict(self) -> dict[str, Any]:
        return {"entries": list(self.entries)}


@dataclass
class TranslationsPendingPayload:
    """Payload for pending translation changes."""

    qids: list[str]
    languages: list[str]
    metadata_keys: list[str] = field(default_factory=list)
    staged_last_modified: Optional[str] = None
    changes: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranslationsPendingPayload":
        return cls(
            qids=list(data.get("qids") or []),
            languages=list(data.get("languages") or []),
            metadata_keys=list(data.get("metadata_keys") or []),
            staged_last_modified=data.get("staged_last_modified"),
            changes=list(data.get("changes") or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "qids": list(self.qids),
            "languages": list(self.languages),
            "metadata_keys": list(self.metadata_keys),
            "staged_last_modified": self.staged_last_modified,
            "changes": list(self.changes),
        }


@dataclass
class EosOperation:
    """Single EOS operation (for library message push)."""

    library_id: str
    message_id: str
    message_dir: str
    keys: Optional[list[str]] = None
    allow_destructive: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EosOperation":
        return cls(
            library_id=str(data.get("library_id") or ""),
            message_id=str(data.get("message_id") or ""),
            message_dir=str(data.get("message_dir") or ""),
            keys=list(data.get("keys") or []) if data.get("keys") else None,
            allow_destructive=bool(data.get("allow_destructive", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "library_id": self.library_id,
            "message_id": self.message_id,
            "message_dir": self.message_dir,
            "keys": list(self.keys or []),
            "allow_destructive": self.allow_destructive,
        }


@dataclass
class EosPendingPayload:
    """Payload for pending EOS changes."""

    operations: list[EosOperation]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EosPendingPayload":
        ops = [EosOperation.from_dict(d) for d in (data.get("operations") or [])]
        return cls(operations=ops)

    def to_dict(self) -> dict[str, Any]:
        return {"operations": [op.to_dict() for op in self.operations]}


@dataclass
class FlowPendingPayload:
    """Payload for pending flow changes.

    Stores the path to the edited YAML file, a hash of the baseline JSON
    for integrity checking, and a list of semantic changes detected.
    """

    flow_yaml_path: str
    baseline_hash: str
    changes: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FlowPendingPayload":
        return cls(
            flow_yaml_path=str(data.get("flow_yaml_path") or ""),
            baseline_hash=str(data.get("baseline_hash") or ""),
            changes=list(data.get("changes") or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "flow_yaml_path": self.flow_yaml_path,
            "baseline_hash": self.baseline_hash,
            "changes": list(self.changes),
        }


@dataclass
class PendingStagedChanges:
    """Unified pending record for any dimension."""

    survey_id: str
    dimension: DimensionType
    payload: (
        ItemsPendingPayload
        | JsPendingPayload
        | TranslationsPendingPayload
        | EosPendingPayload
        | FlowPendingPayload
    )
    created_at: Optional[str] = None
    schema_version: int = 1

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingStagedChanges":
        """Parse unified pending record from dict."""
        survey_id = str(data.get("survey_id") or "")
        dimension = str(data.get("dimension") or "")
        created_at = data.get("created_at")
        schema_version = int(data.get("schema_version") or 1)
        payload_data = data.get("payload") or {}

        # Parse dimension-specific payload
        if dimension in {"items", "edf"}:
            payload = ItemsPendingPayload.from_dict(payload_data)
        elif dimension == "js":
            payload = JsPendingPayload.from_dict(payload_data)
        elif dimension == "translations":
            payload = TranslationsPendingPayload.from_dict(payload_data)
        elif dimension == "eos":
            payload = EosPendingPayload.from_dict(payload_data)
        elif dimension == "flow":
            payload = FlowPendingPayload.from_dict(payload_data)
        else:
            raise ValueError(f"Unknown dimension: {dimension}")

        return cls(
            survey_id=survey_id,
            dimension=dimension,  # type: ignore
            payload=payload,
            created_at=created_at,
            schema_version=schema_version,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "survey_id": self.survey_id,
            "dimension": self.dimension,
            "payload": self.payload.to_dict(),
            "created_at": self.created_at or _now_iso(),
            "schema_version": self.schema_version,
        }


def _unified_pending_path(survey_id: str, dimension: DimensionType) -> Path:
    """Get path to unified pending file."""
    root = resolve_root(required=False) or Path.cwd()
    pending_dir = root / "surveys" / "pending" / dimension
    safe_id = survey_id.strip() or "unknown"
    return pending_dir / f"{safe_id}.json"


def _legacy_pending_paths(survey_id: str, dimension: DimensionType) -> list[Path]:
    """Get legacy pending file paths for migration detection."""
    root = resolve_root(required=False) or Path.cwd()
    safe_id = survey_id.strip() or "unknown"

    if dimension == "items":
        return [root / "surveys" / "pending" / f"{safe_id}.json"]
    elif dimension == "edf":
        return [root / "surveys" / "pending" / "edf" / f"{safe_id}.json"]
    elif dimension == "js":
        return [root / "surveys" / "pending" / "js" / f"{safe_id}.json"]
    elif dimension == "translations":
        return [root / "surveys" / "pending" / "translations" / f"{safe_id}.json"]
    elif dimension == "eos":
        return [root / "surveys" / "pending" / "eos" / f"{safe_id}.json"]
    elif dimension == "flow":
        return []

    return []


def _migrate_legacy_pending(
    survey_id: str, dimension: DimensionType
) -> Optional[PendingStagedChanges]:
    """
    Detect and migrate legacy pending files to unified format.

    Returns:
        Migrated record if legacy file found, None otherwise
    """
    for legacy_path in _legacy_pending_paths(survey_id, dimension):
        if not legacy_path.exists():
            continue

        try:
            data = json.loads(legacy_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        # Migrate based on dimension
        if dimension in {"items", "edf"}:
            # Legacy: PendingPushRecord
            payload = ItemsPendingPayload.from_dict(data)
        elif dimension == "js":
            # Legacy: PendingJsRecord
            payload = JsPendingPayload.from_dict(data)
        elif dimension == "translations":
            # Legacy: PendingTranslationsRecord
            payload = TranslationsPendingPayload.from_dict(data)
        elif dimension == "eos":
            # Legacy: PendingEosRecord
            payload = EosPendingPayload.from_dict(data)
        elif dimension == "flow":
            # No legacy flow pending format.
            continue
        else:
            continue

        record = PendingStagedChanges(
            survey_id=survey_id,
            dimension=dimension,
            payload=payload,
            created_at=data.get("created_at"),
        )

        # Save to new location (migration)
        save_pending(record)

        # Optionally delete old file after migration
        try:
            legacy_path.unlink()
            warnings.warn(
                f"Migrated legacy pending file: {legacy_path} -> {_unified_pending_path(survey_id, dimension)}",
                DeprecationWarning,
                stacklevel=2,
            )
        except OSError:
            pass

        return record

    return None


def save_pending(record: PendingStagedChanges) -> None:
    """Save pending record to unified location."""
    path = _unified_pending_path(record.survey_id, record.dimension)
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = record.to_dict()
    if not payload.get("created_at"):
        payload["created_at"] = _now_iso()

    # Compatibility shim: include legacy top-level fields for older tooling.
    if record.dimension == "js":
        payload["entries"] = list((payload.get("payload") or {}).get("entries") or [])
    elif record.dimension == "translations":
        payload["languages"] = list(
            (payload.get("payload") or {}).get("languages") or []
        )
    elif record.dimension == "eos":
        payload["operations"] = list(
            (payload.get("payload") or {}).get("operations") or []
        )

    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_pending(
    survey_id: str, dimension: DimensionType
) -> Optional[PendingStagedChanges]:
    """
    Load pending record with automatic legacy migration.

    Args:
        survey_id: Survey ID
        dimension: Dimension type

    Returns:
        Pending record if found, None otherwise
    """
    # Try unified path first
    path = _unified_pending_path(survey_id, dimension)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return PendingStagedChanges.from_dict(data)
        except (json.JSONDecodeError, ValueError, OSError):
            pass

    # Try legacy migration
    migrated = _migrate_legacy_pending(survey_id, dimension)
    if migrated:
        return migrated

    return None


def clear_pending(survey_id: str, dimension: DimensionType) -> None:
    """Clear pending record for dimension."""
    path = _unified_pending_path(survey_id, dimension)
    try:
        path.unlink()
    except FileNotFoundError:
        pass

    # Also try to clean up any legacy files
    for legacy_path in _legacy_pending_paths(survey_id, dimension):
        try:
            legacy_path.unlink()
        except FileNotFoundError:
            pass


def list_pending(survey_id: str) -> dict[DimensionType, PendingStagedChanges]:
    """
    List all pending changes across dimensions for a survey.

    Args:
        survey_id: Survey ID

    Returns:
        Dict mapping dimension to pending record
    """
    result: dict[DimensionType, PendingStagedChanges] = {}

    for dimension in ["items", "js", "translations", "eos", "flow"]:
        record = load_pending(survey_id, dimension)  # type: ignore
        if record:
            result[dimension] = record  # type: ignore

    return result
