"""Deep parity checks for Qualtrics survey-definitions payloads (cross-account safe).

This module is intended for cross-account copy verification:
- Compare `GET /survey-definitions/{id}` JSON payloads (the `result` dict).
- Normalize out fields that differ by definition across accounts (IDs, timestamps, etc).
- Gate output via stable hashing; only compute/render diffs on mismatch.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import resolve_root
from .dimensions.flow_diff import FlowChange, diff_flows


_SURVEY_ID_RE = re.compile(r"^SV_[A-Za-z0-9]+$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}$"
)
_RESPONSE_SET_ID_RE = re.compile(r"^RS_[A-Za-z0-9]+$")

_TOP_LEVEL_ACCOUNT_KEYS = {
    "BrandBaseURL",
    "BrandID",
    "CreatorID",
    "DivisionID",
    "OwnerID",
    "ProjectInfo",
}
_TOP_LEVEL_TIMESTAMP_KEYS = {
    "LastModified",
    "LastAccessed",
    "LastActivated",
    "LastModifiedDate",
    "lastModified",
    "lastModifiedDate",
}


@dataclass(frozen=True)
class DeepParityReport:
    ok: bool
    hash_a: str
    hash_b: str
    diff_count: int
    diff_paths: list[str]
    section_counts: dict[str, int]
    flow_changes: list[FlowChange]
    artifacts: dict[str, str] | None = None


def _looks_like_survey_id(value: object) -> bool:
    return isinstance(value, str) and bool(_SURVEY_ID_RE.match(value.strip()))


def _looks_like_uuid(value: object) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value.strip()))


def _canonical_json(obj: object) -> str:
    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _hash_json(obj: object) -> str:
    return hashlib.sha256(_canonical_json(obj).encode("utf-8")).hexdigest()


def _deep_copy_jsonish(payload: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(payload))
    except Exception:
        # Best-effort: shallow copy to avoid mutating callers.
        return dict(payload)


def _drop_unsafe_duplicates(obj: object) -> None:
    """Drop `*_Unsafe` keys when they are identical to the safe key."""
    if isinstance(obj, dict):
        to_delete: list[str] = []
        for k, v in obj.items():
            if not isinstance(k, str) or not k.endswith("_Unsafe"):
                continue
            base = k[: -len("_Unsafe")]
            if base in obj and obj.get(base) == v:
                to_delete.append(k)
        for k in to_delete:
            obj.pop(k, None)
        for v in obj.values():
            _drop_unsafe_duplicates(v)
        return
    if isinstance(obj, list):
        for item in obj:
            _drop_unsafe_duplicates(item)


def _strip_recursive_volatile_keys(obj: object) -> None:
    """Strip volatile keys anywhere in the payload (pattern-based)."""
    if isinstance(obj, dict):
        to_delete: list[str] = []
        for k, v in obj.items():
            if k == "SurveyID" and _looks_like_survey_id(v):
                to_delete.append(k)
                continue
            if k == "PreviewID" and _looks_like_uuid(v):
                to_delete.append(k)
                continue
        for k in to_delete:
            obj.pop(k, None)
        for v in obj.values():
            _strip_recursive_volatile_keys(v)
        return
    if isinstance(obj, list):
        for item in obj:
            _strip_recursive_volatile_keys(item)


def _normalize_response_sets(result: dict[str, Any]) -> None:
    """Normalize ResponseSet IDs so cross-account regeneration doesn't cause false diffs."""
    raw = result.get("ResponseSets")
    if not isinstance(raw, dict) or not raw:
        return

    id_to_name: dict[str, str] = {}
    names: list[str] = []
    for rs_id, rs_name in raw.items():
        rs_id_s = str(rs_id or "").strip()
        rs_name_s = str(rs_name or "").strip()
        if not rs_id_s or not rs_name_s:
            continue
        if not _RESPONSE_SET_ID_RE.match(rs_id_s):
            # Keep non-RS keys untouched (defensive).
            continue
        id_to_name[rs_id_s] = rs_name_s
        names.append(rs_name_s)

    # Replace mapping with stable list of names (order-insensitive).
    if names:
        result["ResponseSets"] = sorted(names)

    survey_options = result.get("SurveyOptions")
    if not isinstance(survey_options, dict):
        return
    active = survey_options.get("ActiveResponseSet")
    if isinstance(active, str):
        active = active.strip()
    if active and isinstance(active, str) and active in id_to_name:
        survey_options["ActiveResponseSet"] = id_to_name[active]


def normalize_survey_definition_for_deep_parity(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize a survey-definitions payload (result dict) for deep parity checks."""
    data = _deep_copy_jsonish(payload)
    # Accept either full API wrapper {"result": {...}} or raw result dict.
    result = data.get("result") if isinstance(data.get("result"), dict) else data
    if not isinstance(result, dict):
        return {}

    # Drop wrapper noise if present.
    data.pop("meta", None)

    # Top-level volatility (cross-account identity + timestamps).
    for k in _TOP_LEVEL_ACCOUNT_KEYS:
        result.pop(k, None)
    for k in _TOP_LEVEL_TIMESTAMP_KEYS:
        result.pop(k, None)

    # Survey identity fields differ by definition.
    if _looks_like_survey_id(result.get("SurveyID")):
        result.pop("SurveyID", None)
    result.pop("SurveyName", None)
    result.pop("SurveyStatus", None)

    # Cross-account copy often renames the survey (e.g., smoke runs). Ignore title/name
    # fields so parity focuses on survey semantics.
    survey_options = result.get("SurveyOptions")
    if isinstance(survey_options, dict):
        survey_options.pop("SurveyTitle", None)
        # Always differs for newly imported surveys.
        survey_options.pop("SurveyCreationDate", None)
        # Qualtrics sometimes returns `MetaDataTranslations=[]` when absent.
        # Treat empty containers as equivalent to missing.
        meta = survey_options.get("MetaDataTranslations")
        if meta in (None, [], {}):
            survey_options.pop("MetaDataTranslations", None)

    _normalize_response_sets(result)

    # Pattern-based recursion for volatile IDs.
    _strip_recursive_volatile_keys(result)

    # Treat safe/unsafe duplicates as equivalent when identical.
    _drop_unsafe_duplicates(result)

    return result


def _path_for_key(path: str, key: object, *, container_key: str | None) -> str:
    key_s = str(key)
    if container_key in {"Questions", "Blocks"}:
        # Prefer a stable bracketed path for keyed mappings.
        return f"{path}[{key_s}]"

    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key_s or ""):
        return f"{path}.{key_s}" if path else key_s

    escaped = key_s.replace("\\", "\\\\").replace('"', '\\"')
    return f'{path}["{escaped}"]' if path else f'["{escaped}"]'


def _path_for_index(path: str, idx: int) -> str:
    return f"{path}[{idx}]"


def _section_for_path(path: str) -> str:
    if not path:
        return "Other"
    if path.startswith("Questions"):
        return "Questions"
    if path.startswith("Blocks"):
        return "Blocks"
    if path.startswith("SurveyOptions"):
        return "SurveyOptions"
    if path.startswith("SurveyFlow") or path.startswith("Flow"):
        return "SurveyFlow"
    return "Other"


def _diff_summary(
    a: object,
    b: object,
    *,
    max_paths: int,
) -> tuple[int, list[str], dict[str, int]]:
    diff_count = 0
    diff_paths: list[str] = []
    section_counts: dict[str, int] = {}

    def record(path: str, kind: str) -> None:
        nonlocal diff_count
        diff_count += 1
        section = _section_for_path(path)
        section_counts[section] = section_counts.get(section, 0) + 1
        if len(diff_paths) < max_paths:
            diff_paths.append(f"{path} ({kind})" if kind else path)

    def walk(x: object, y: object, *, path: str, container_key: str | None) -> None:
        if x is y:
            return
        if type(x) is not type(y):
            record(path, "type_mismatch")
            return

        if isinstance(x, dict):
            keys = sorted(set(x.keys()) | set(y.keys()), key=lambda k: str(k))
            for k in keys:
                child_path = _path_for_key(path, k, container_key=container_key)
                if k not in x:
                    record(child_path, "missing_in_a")
                    continue
                if k not in y:
                    record(child_path, "missing_in_b")
                    continue
                walk(
                    x.get(k),
                    y.get(k),
                    path=child_path,
                    container_key=str(k) if isinstance(k, str) else None,
                )
            return

        if isinstance(x, list):
            if len(x) != len(y):
                record(path, "len_mismatch")
            n = min(len(x), len(y))
            for i in range(n):
                walk(
                    x[i],
                    y[i],
                    path=_path_for_index(path, i),
                    container_key=container_key,
                )
            for i in range(n, len(x)):
                record(_path_for_index(path, i), "missing_in_b")
            for i in range(n, len(y)):
                record(_path_for_index(path, i), "missing_in_a")
            return

        if x != y:
            record(path, "value_mismatch")

    walk(a, b, path="", container_key=None)
    return diff_count, diff_paths, section_counts


def write_deep_parity_artifacts(
    *,
    normalized_a: Mapping[str, Any],
    normalized_b: Mapping[str, Any],
    survey_a: str,
    survey_b: str,
    out_dir: Path | None = None,
    write_unified_diff: bool = True,
) -> dict[str, Path]:
    root = resolve_root(required=False) or Path.cwd()
    out_dir = (out_dir or (root / "tmp")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = f"survey_deep_parity_{survey_a}__{survey_b}_{ts}"

    path_a = out_dir / f"{base}_A.json"
    path_b = out_dir / f"{base}_B.json"
    path_a.write_text(
        json.dumps(normalized_a, indent=2, sort_keys=True, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    path_b.write_text(
        json.dumps(normalized_b, indent=2, sort_keys=True, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    paths: dict[str, Path] = {"a": path_a, "b": path_b}

    if write_unified_diff:
        diff_path = out_dir / f"{base}.diff"
        a_txt = path_a.read_text(encoding="utf-8").splitlines()
        b_txt = path_b.read_text(encoding="utf-8").splitlines()
        udiff = difflib.unified_diff(
            a_txt,
            b_txt,
            fromfile=f"{survey_a} (normalized)",
            tofile=f"{survey_b} (normalized)",
            lineterm="",
            n=2,
        )
        diff_path.write_text("\n".join(udiff) + "\n", encoding="utf-8")
        paths["diff"] = diff_path

    return paths


def compare_survey_definition_deep_parity(
    def_a: Mapping[str, Any],
    def_b: Mapping[str, Any],
    *,
    survey_a: str = "A",
    survey_b: str = "B",
    max_diff_paths: int = 50,
    write_artifacts_on_mismatch: bool = False,
) -> DeepParityReport:
    """Compare two survey-definitions payloads for deep parity.

    Args:
        def_a: Survey definition (result dict or wrapper with result).
        def_b: Survey definition (result dict or wrapper with result).
        survey_a: Label/id for A (used in artifact filenames).
        survey_b: Label/id for B (used in artifact filenames).
        max_diff_paths: Max diff paths to include inline in the report.
        write_artifacts_on_mismatch: If True, write normalized A/B snapshots (and diff)
            to tmp/ and include paths in the report.
    """
    norm_a = normalize_survey_definition_for_deep_parity(def_a)
    norm_b = normalize_survey_definition_for_deep_parity(def_b)

    hash_a = _hash_json(norm_a)
    hash_b = _hash_json(norm_b)
    if hash_a == hash_b:
        return DeepParityReport(
            ok=True,
            hash_a=hash_a,
            hash_b=hash_b,
            diff_count=0,
            diff_paths=[],
            section_counts={},
            flow_changes=[],
            artifacts=None,
        )

    diff_count, diff_paths, section_counts = _diff_summary(
        norm_a, norm_b, max_paths=max_diff_paths
    )

    flow_a = norm_a.get("SurveyFlow") or norm_a.get("Flow") or {}
    flow_b = norm_b.get("SurveyFlow") or norm_b.get("Flow") or {}
    flow_changes: list[FlowChange] = []
    if isinstance(flow_a, dict) and isinstance(flow_b, dict) and (flow_a or flow_b):
        flow_changes = diff_flows(flow_a, flow_b)

    artifacts: dict[str, str] | None = None
    if write_artifacts_on_mismatch:
        paths = write_deep_parity_artifacts(
            normalized_a=norm_a,
            normalized_b=norm_b,
            survey_a=survey_a,
            survey_b=survey_b,
        )
        artifacts = {k: str(v) for k, v in paths.items()}

    return DeepParityReport(
        ok=False,
        hash_a=hash_a,
        hash_b=hash_b,
        diff_count=diff_count,
        diff_paths=diff_paths,
        section_counts=section_counts,
        flow_changes=flow_changes,
        artifacts=artifacts,
    )
