"""Deep parity checks for Qualtrics survey-definitions payloads.

Profiles:
- strict: minimal normalization (wrapper noise only)
- cross_account: existing behavior (drop account/timestamp volatility)
- split: cross_account normalization + split-policy gate classification
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import resolve_root, resolve_scoped_dir
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
_SUPPORTED_PROFILES = {"strict", "cross_account", "split"}
_PATH_TOKEN_RE = re.compile(
    r'([A-Za-z_][A-Za-z0-9_]*)|\[(\d+|"(?:[^"\\]|\\.)*"|[^\]]+)\]'
)
_MISSING = object()

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
class DiffEvent:
    path: str
    kind: str


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
    profile: str = "cross_account"
    gate_results: dict[str, bool] = field(default_factory=dict)
    hard_fail_paths: list[str] = field(default_factory=list)
    allowed_by_policy_paths: list[str] = field(default_factory=list)
    warning_paths: list[str] = field(default_factory=list)
    policy_notes: list[str] = field(default_factory=list)
    manifest_path: str | None = None
    timings_ms: dict[str, float] = field(default_factory=dict)


def _normalize_profile(profile: str | None) -> str:
    raw = str(profile or "cross_account").strip().lower().replace("-", "_")
    if raw in {"", "default"}:
        return "cross_account"
    if raw not in _SUPPORTED_PROFILES:
        raise ValueError(
            f"Unsupported parity profile: {profile!r}. "
            f"Expected one of: {', '.join(sorted(_SUPPORTED_PROFILES))}."
        )
    return raw


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


def _survey_result(payload: Mapping[str, Any]) -> dict[str, Any]:
    data = _deep_copy_jsonish(payload)
    result = data.get("result") if isinstance(data.get("result"), dict) else data
    return result if isinstance(result, dict) else {}


def _extract_survey_id(payload: Mapping[str, Any]) -> str | None:
    result = _survey_result(payload)
    raw = str(result.get("SurveyID") or "").strip()
    return raw if _looks_like_survey_id(raw) else None


def _extract_base_language(payload: Mapping[str, Any]) -> str:
    result = _survey_result(payload)
    options = result.get("SurveyOptions")
    if isinstance(options, dict):
        lang = str(options.get("SurveyLanguage") or "").strip().upper()
        if lang:
            return lang
    entry = result.get("SurveyEntry")
    if isinstance(entry, dict):
        lang = str(entry.get("SurveyLanguage") or "").strip().upper()
        if lang:
            return lang
    return "EN"


def _normalize_lang(value: object) -> str:
    text = str(value or "").strip().replace("_", "-").upper()
    return text


def _parse_available_languages(value: object) -> list[str]:
    out: list[str] = []
    if isinstance(value, dict):
        for k, enabled in value.items():
            if enabled in {False, 0, "0", "false", "False", None, ""}:
                continue
            lang = _normalize_lang(k)
            if lang:
                out.append(lang)
    elif isinstance(value, list):
        for item in value:
            lang = _normalize_lang(item)
            if lang:
                out.append(lang)
    seen: set[str] = set()
    deduped: list[str] = []
    for lang in out:
        if lang in seen:
            continue
        seen.add(lang)
        deduped.append(lang)
    return deduped


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
            continue
        id_to_name[rs_id_s] = rs_name_s
        names.append(rs_name_s)

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


def _normalize_webservice_aliases(obj: object) -> None:
    """Normalize RequestURL/RequestType aliases into URL/Method for stable compare."""
    if isinstance(obj, dict):
        node_type = str(obj.get("Type") or "").strip()
        if node_type == "WebService":
            request_url = obj.pop("RequestURL", None)
            request_type = obj.pop("RequestType", None)
            if request_url is not None and "URL" not in obj:
                obj["URL"] = request_url
            if request_type is not None and "Method" not in obj:
                obj["Method"] = request_type
        for value in obj.values():
            _normalize_webservice_aliases(value)
        return
    if isinstance(obj, list):
        for value in obj:
            _normalize_webservice_aliases(value)


def normalize_survey_definition_for_deep_parity(
    payload: Mapping[str, Any],
    *,
    profile: str = "cross_account",
) -> dict[str, Any]:
    """Normalize a survey-definitions payload for deep parity checks."""
    mode = _normalize_profile(profile)
    data = _deep_copy_jsonish(payload)
    result = data.get("result") if isinstance(data.get("result"), dict) else data
    if not isinstance(result, dict):
        return {}

    data.pop("meta", None)

    if mode in {"cross_account", "split"}:
        for k in _TOP_LEVEL_ACCOUNT_KEYS:
            result.pop(k, None)
        for k in _TOP_LEVEL_TIMESTAMP_KEYS:
            result.pop(k, None)

        if _looks_like_survey_id(result.get("SurveyID")):
            result.pop("SurveyID", None)
        result.pop("SurveyName", None)
        result.pop("SurveyStatus", None)

        survey_options = result.get("SurveyOptions")
        if isinstance(survey_options, dict):
            survey_options.pop("SurveyTitle", None)
            survey_options.pop("SurveyCreationDate", None)
            meta = survey_options.get("MetaDataTranslations")
            if meta in (None, [], {}):
                survey_options.pop("MetaDataTranslations", None)

        _normalize_response_sets(result)
        _strip_recursive_volatile_keys(result)
        _drop_unsafe_duplicates(result)

    if mode == "split":
        _normalize_webservice_aliases(result)

    return result


def _path_for_key(path: str, key: object, *, container_key: str | None) -> str:
    key_s = str(key)
    if container_key in {"Questions", "Blocks"}:
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


def _collect_diff_events(a: object, b: object) -> list[DiffEvent]:
    events: list[DiffEvent] = []

    def record(path: str, kind: str) -> None:
        events.append(DiffEvent(path=path, kind=kind))

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
    return events


def _diff_summary_from_events(
    events: list[DiffEvent],
    *,
    max_paths: int,
) -> tuple[int, list[str], dict[str, int]]:
    section_counts: dict[str, int] = {}
    diff_paths: list[str] = []
    for event in events:
        section = _section_for_path(event.path)
        section_counts[section] = section_counts.get(section, 0) + 1
        if len(diff_paths) < max_paths:
            diff_paths.append(f"{event.path} ({event.kind})")
    return len(events), diff_paths, section_counts


def _parse_path_tokens(path: str) -> list[str | int]:
    tokens: list[str | int] = []
    for match in _PATH_TOKEN_RE.finditer(path):
        key = match.group(1)
        bracket = match.group(2)
        if key is not None:
            tokens.append(key)
            continue
        if bracket is None:
            continue
        if bracket.isdigit():
            tokens.append(int(bracket))
            continue
        if bracket.startswith('"') and bracket.endswith('"'):
            try:
                tokens.append(str(json.loads(bracket)))
            except Exception:
                tokens.append(bracket[1:-1])
            continue
        tokens.append(bracket)
    return tokens


def _resolve_path_value(obj: object, path: str) -> object:
    current = obj
    for token in _parse_path_tokens(path):
        if isinstance(token, int):
            if not isinstance(current, list) or token < 0 or token >= len(current):
                return _MISSING
            current = current[token]
            continue
        if not isinstance(current, dict) or token not in current:
            return _MISSING
        current = current[token]
    return current


def _is_translation_question_path(path: str) -> bool:
    if not path.startswith("Questions["):
        return False
    if ".Language" in path:
        return True
    if ".QuestionText" in path:
        return True
    if ".Choices[" in path and path.endswith(".Display"):
        return True
    if ".Answers[" in path and path.endswith(".Display"):
        return True
    if ".Labels[" in path and path.endswith(".Display"):
        return True
    return False


def _is_language_policy_path(path: str) -> bool:
    prefixes = (
        "SurveyOptions.SurveyLanguage",
        "SurveyOptions.AvailableLanguages",
        "SurveyOptions.MetaDataTranslations",
        "SurveyOptions.SurveyMetaDescription",
    )
    return any(path.startswith(prefix) for prefix in prefixes)


def _is_eos_redirect_path(path: str) -> bool:
    return path.endswith(".EOSRedirectURL")


def _is_eos_identity_path(path: str) -> bool:
    return path.endswith(".EOSMessage") or path.endswith(".EOSMessageLibrary")


def _is_country_embedded_value_path(path: str, *, split_payload: Mapping[str, Any]) -> bool:
    if ".EmbeddedData[" not in path or not path.endswith(".Value"):
        return False
    field_path = path[: -len(".Value")] + ".Field"
    field = _resolve_path_value(split_payload, field_path)
    if field is _MISSING:
        return False
    return str(field or "").strip().lower() == "country"


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _load_manifest(
    *,
    manifest: Mapping[str, Any] | None,
    manifest_path: Path | str | None,
) -> tuple[dict[str, Any] | None, str | None, list[str]]:
    notes: list[str] = []
    path_s: str | None = None
    payload: dict[str, Any] | None = None

    if manifest is not None:
        payload = dict(manifest)
    elif manifest_path:
        path = Path(manifest_path).expanduser().resolve()
        path_s = str(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            notes.append(f"manifest: failed to read {path}: {exc}")
            return None, path_s, notes
        if not isinstance(raw, dict):
            notes.append(f"manifest: expected JSON object at {path}")
            return None, path_s, notes
        payload = raw
    return payload, path_s, notes


def _resolve_split_orientation(
    *,
    def_a: Mapping[str, Any],
    def_b: Mapping[str, Any],
    norm_a: Mapping[str, Any],
    norm_b: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    source_id = str(manifest.get("source_survey_id") or "").strip()
    target_id = str(manifest.get("target_survey_id") or manifest.get("new_survey_id") or "").strip()
    a_id = _extract_survey_id(def_a) or ""
    b_id = _extract_survey_id(def_b) or ""

    if source_id and target_id and a_id and b_id:
        if a_id == source_id and b_id == target_id:
            return def_a, def_b, norm_a, norm_b
        if a_id == target_id and b_id == source_id:
            return def_b, def_a, norm_b, norm_a

    return def_a, def_b, norm_a, norm_b


def _compare_translation_gate(
    *,
    canonical_payload: Mapping[str, Any],
    split_payload: Mapping[str, Any],
    target_language: str,
) -> tuple[bool, list[str], str]:
    from .translation_export import build_translation_map_from_cache

    target_lang = _normalize_lang(target_language) or "EN"
    canonical_base = _extract_base_language(canonical_payload)
    split_base = _extract_base_language(split_payload)

    canonical_map = build_translation_map_from_cache(
        dict(canonical_payload),
        language=target_lang,
        base_language=canonical_base,
    )
    split_map = build_translation_map_from_cache(
        dict(split_payload),
        language=split_base,
        base_language=split_base,
    )

    canonical_fp = _hash_json(canonical_map)

    keys_a = set(canonical_map.keys())
    keys_b = set(split_map.keys())
    missing = sorted(keys_a - keys_b)
    extra = sorted(keys_b - keys_a)
    mismatched: list[str] = []
    for key in sorted(keys_a & keys_b):
        if str(canonical_map.get(key) or "") != str(split_map.get(key) or ""):
            mismatched.append(key)

    notes: list[str] = []
    if missing:
        notes.append(f"translation: missing keys in split ({len(missing)}): {missing[:10]}")
    if extra:
        notes.append(f"translation: extra keys in split ({len(extra)}): {extra[:10]}")
    if mismatched:
        notes.append(
            f"translation: value mismatches ({len(mismatched)}): {mismatched[:10]}"
        )

    return not notes, notes, canonical_fp


def _collect_country_values(payload: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    result = _survey_result(payload)
    flow = result.get("SurveyFlow") or result.get("Flow") or {}

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if str(node.get("Type") or "").strip() == "EmbeddedData":
                embedded = node.get("EmbeddedData")
                if isinstance(embedded, list):
                    for item in embedded:
                        if not isinstance(item, dict):
                            continue
                        field = str(item.get("Field") or "").strip().lower()
                        if field == "country":
                            out.append(str(item.get("Value") or "").strip())
            for value in node.values():
                walk(value)
            return
        if isinstance(node, list):
            for value in node:
                walk(value)

    walk(flow)
    return out


def _collect_eos_redirect_urls(payload: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    result = _survey_result(payload)
    survey_options = result.get("SurveyOptions")
    if isinstance(survey_options, dict):
        value = str(survey_options.get("EOSRedirectURL") or "").strip()
        if value:
            out.append(value)

    flow = result.get("SurveyFlow") or result.get("Flow") or {}

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if str(node.get("Type") or "").strip() == "EndSurvey":
                options = node.get("Options")
                if isinstance(options, dict):
                    value = str(options.get("EOSRedirectURL") or "").strip()
                    if value:
                        out.append(value)
            for value in node.values():
                walk(value)
            return
        if isinstance(node, list):
            for value in node:
                walk(value)

    walk(flow)
    return out


def _collect_eos_refs(payload: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    result = _survey_result(payload)
    flow = result.get("SurveyFlow") or result.get("Flow") or {}

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if str(node.get("Type") or "").strip() == "EndSurvey":
                flow_id = str(node.get("FlowID") or "").strip()
                options = node.get("Options")
                if isinstance(options, dict):
                    msg = str(options.get("EOSMessage") or "").strip()
                    lib = str(options.get("EOSMessageLibrary") or "").strip()
                    if msg or lib:
                        out.append((flow_id, msg, lib))
            for value in node.values():
                walk(value)
            return
        if isinstance(node, list):
            for value in node:
                walk(value)

    walk(flow)
    return out


def _check_keep_languages_policy(
    *,
    split_payload: Mapping[str, Any],
    manifest: Mapping[str, Any],
    target_language: str,
) -> tuple[bool, list[str]]:
    result = _survey_result(split_payload)
    survey_options = result.get("SurveyOptions") or {}
    available = set(
        _parse_available_languages(
            survey_options.get("AvailableLanguages")
            if isinstance(survey_options, dict)
            else []
        )
    )
    target = _normalize_lang(target_language)
    policy = manifest.get("keep_languages_policy")
    if policy is None:
        policy = manifest.get("keep_languages_mode", "target-only")

    if isinstance(policy, str):
        raw = policy.strip().lower().replace("_", "-")
        if raw in {"target-only", "target"}:
            expected = {target}
        elif raw == "all":
            expected = None
        else:
            expected = {
                _normalize_lang(part)
                for part in policy.split(",")
                if str(part).strip()
            }
            expected.add(target)
    elif isinstance(policy, list):
        expected = {_normalize_lang(item) for item in policy if str(item).strip()}
        expected.add(target)
    else:
        expected = {target}

    if expected is None:
        return True, []
    if not available:
        return False, ["policy: split survey has no AvailableLanguages entries"]
    if available != expected:
        return (
            False,
            [
                "policy: keep_languages mismatch "
                f"(expected={sorted(expected)}, actual={sorted(available)})"
            ],
        )
    return True, []


def _check_operational_policy(
    *,
    split_payload: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> tuple[dict[str, bool], list[str]]:
    status = {"country": True, "redirect": True, "eos": True}
    notes: list[str] = []

    target_country = str(manifest.get("target_country") or "").strip()
    if target_country:
        country_values = _collect_country_values(split_payload)
        if not country_values:
            status["country"] = False
            notes.append("policy: expected country embedded-data value(s), found none")
        else:
            mismatched = [
                value
                for value in country_values
                if _normalize_lang(value) != _normalize_lang(target_country)
            ]
            if mismatched:
                status["country"] = False
                notes.append(
                    "policy: country mismatch "
                    f"(expected={target_country}, actual={sorted(set(country_values))})"
                )

    redirect_expected = str(manifest.get("completion_redirect_url") or "").strip()
    if redirect_expected:
        redirect_values = _collect_eos_redirect_urls(split_payload)
        if not redirect_values:
            status["redirect"] = False
            notes.append("policy: expected EOS redirect URL(s), found none")
        else:
            mismatched = [
                value for value in redirect_values if str(value).strip() != redirect_expected
            ]
            if mismatched:
                status["redirect"] = False
                notes.append(
                    "policy: redirect URL mismatch "
                    f"(expected={redirect_expected}, actual={sorted(set(redirect_values))})"
                )

    eos_policy = manifest.get("eos_policy")
    if eos_policy:
        refs = _collect_eos_refs(split_payload)
        if not refs:
            status["eos"] = False
            notes.append("policy: eos_policy provided but no EOS refs found")
        elif isinstance(eos_policy, dict):
            by_flow = eos_policy.get("by_flow_id")
            by_flow = by_flow if isinstance(by_flow, dict) else {}
            allowed_ids = eos_policy.get("allowed_ids")
            if not isinstance(allowed_ids, list):
                allowed_ids = eos_policy.get("eos_message_ids")
            allowed_ids_set = {
                str(value).strip()
                for value in (allowed_ids or [])
                if str(value).strip()
            }
            allowed_libs = eos_policy.get("allowed_libraries")
            if not isinstance(allowed_libs, list):
                allowed_libs = eos_policy.get("eos_message_libraries")
            allowed_libs_set = {
                str(value).strip()
                for value in (allowed_libs or [])
                if str(value).strip()
            }

            eos_failures: list[str] = []
            for flow_id, msg_id, lib_id in refs:
                expected = by_flow.get(flow_id) if isinstance(by_flow, dict) else None
                if isinstance(expected, dict):
                    expected_msg = str(
                        expected.get("eos_message") or expected.get("EOSMessage") or ""
                    ).strip()
                    expected_lib = str(
                        expected.get("eos_message_library")
                        or expected.get("EOSMessageLibrary")
                        or ""
                    ).strip()
                    if expected_msg and msg_id != expected_msg:
                        eos_failures.append(
                            f"flow {flow_id}: EOSMessage expected {expected_msg}, got {msg_id or '(empty)'}"
                        )
                    if expected_lib and lib_id != expected_lib:
                        eos_failures.append(
                            f"flow {flow_id}: EOSMessageLibrary expected {expected_lib}, got {lib_id or '(empty)'}"
                        )
                    continue
                if allowed_ids_set and msg_id and msg_id not in allowed_ids_set:
                    eos_failures.append(
                        f"flow {flow_id}: EOSMessage {msg_id} not in allowed_ids"
                    )
                if allowed_libs_set and lib_id and lib_id not in allowed_libs_set:
                    eos_failures.append(
                        f"flow {flow_id}: EOSMessageLibrary {lib_id} not in allowed_libraries"
                    )

            if eos_failures:
                status["eos"] = False
                notes.append(
                    "policy: EOS policy mismatch: "
                    + "; ".join(eos_failures[:5])
                    + ("" if len(eos_failures) <= 5 else f" (+{len(eos_failures)-5} more)")
                )
        elif isinstance(eos_policy, str):
            expected = eos_policy.strip()
            if expected:
                mismatched = [msg_id for _, msg_id, _ in refs if msg_id != expected]
                if mismatched:
                    status["eos"] = False
                    notes.append(
                        "policy: EOSMessage mismatch "
                        f"(expected={expected}, actual={sorted(set(mismatched))})"
                    )

    return status, notes


def _classify_split_events(
    *,
    events: list[DiffEvent],
    split_payload: Mapping[str, Any],
    translation_ok: bool,
    keep_lang_ok: bool,
    policy_status: Mapping[str, bool],
    manifest: Mapping[str, Any],
) -> tuple[list[str], list[str], list[str], list[str]]:
    allowed: list[str] = []
    hard_fail: list[str] = []
    structural_fail: list[str] = []
    operational_fail: list[str] = []

    has_country_policy = bool(str(manifest.get("target_country") or "").strip())
    has_redirect_policy = bool(str(manifest.get("completion_redirect_url") or "").strip())
    has_eos_policy = bool(manifest.get("eos_policy"))

    for event in events:
        path = event.path
        label = f"{path} ({event.kind})"
        if _is_translation_question_path(path):
            if translation_ok:
                allowed.append(path)
            else:
                hard_fail.append(label)
            continue

        if _is_language_policy_path(path):
            if keep_lang_ok:
                allowed.append(path)
            else:
                hard_fail.append(label)
                operational_fail.append(label)
            continue

        if _is_country_embedded_value_path(path, split_payload=split_payload):
            if has_country_policy and policy_status.get("country", False):
                allowed.append(path)
            else:
                hard_fail.append(label)
                operational_fail.append(label)
            continue

        if _is_eos_redirect_path(path):
            if has_redirect_policy and policy_status.get("redirect", False):
                allowed.append(path)
            else:
                hard_fail.append(label)
                operational_fail.append(label)
            continue

        if _is_eos_identity_path(path):
            if has_eos_policy and policy_status.get("eos", False):
                allowed.append(path)
            else:
                hard_fail.append(label)
                operational_fail.append(label)
            continue

        hard_fail.append(label)
        structural_fail.append(label)

    return (
        _dedupe(allowed),
        _dedupe(hard_fail),
        _dedupe(structural_fail),
        _dedupe(operational_fail),
    )


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
    out_dir = (out_dir or resolve_scoped_dir("tmp", root=root)).resolve()
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
    profile: str = "cross_account",
    manifest: Mapping[str, Any] | None = None,
    manifest_path: Path | str | None = None,
) -> DeepParityReport:
    """Compare two survey-definitions payloads for deep parity."""
    started = time.perf_counter()
    timings_ms: dict[str, float] = {}

    t_manifest = time.perf_counter()
    mode = _normalize_profile(profile)
    manifest_payload, manifest_path_resolved, manifest_notes = _load_manifest(
        manifest=manifest,
        manifest_path=manifest_path,
    )
    timings_ms["manifest_load"] = (time.perf_counter() - t_manifest) * 1000.0

    t_normalize = time.perf_counter()
    norm_a = normalize_survey_definition_for_deep_parity(def_a, profile=mode)
    norm_b = normalize_survey_definition_for_deep_parity(def_b, profile=mode)
    timings_ms["normalize"] = (time.perf_counter() - t_normalize) * 1000.0

    t_hash = time.perf_counter()
    hash_a = _hash_json(norm_a)
    hash_b = _hash_json(norm_b)
    timings_ms["hash"] = (time.perf_counter() - t_hash) * 1000.0

    t_diff = time.perf_counter()
    events = _collect_diff_events(norm_a, norm_b)
    diff_count, diff_paths, section_counts = _diff_summary_from_events(
        events,
        max_paths=max_diff_paths,
    )
    timings_ms["diff"] = (time.perf_counter() - t_diff) * 1000.0

    t_flow = time.perf_counter()
    flow_a = norm_a.get("SurveyFlow") or norm_a.get("Flow") or {}
    flow_b = norm_b.get("SurveyFlow") or norm_b.get("Flow") or {}
    flow_changes: list[FlowChange] = []
    if isinstance(flow_a, dict) and isinstance(flow_b, dict) and (flow_a or flow_b):
        flow_changes = diff_flows(flow_a, flow_b)
    timings_ms["flow_semantic_diff"] = (time.perf_counter() - t_flow) * 1000.0

    if mode != "split":
        artifacts: dict[str, str] | None = None
        ok = hash_a == hash_b
        t_artifacts = time.perf_counter()
        if write_artifacts_on_mismatch and not ok:
            paths = write_deep_parity_artifacts(
                normalized_a=norm_a,
                normalized_b=norm_b,
                survey_a=survey_a,
                survey_b=survey_b,
            )
            artifacts = {k: str(v) for k, v in paths.items()}
        timings_ms["artifact_write"] = (time.perf_counter() - t_artifacts) * 1000.0
        timings_ms["total"] = (time.perf_counter() - started) * 1000.0
        return DeepParityReport(
            ok=ok,
            hash_a=hash_a,
            hash_b=hash_b,
            diff_count=diff_count,
            diff_paths=diff_paths,
            section_counts=section_counts,
            flow_changes=flow_changes,
            artifacts=artifacts,
            profile=mode,
            timings_ms=timings_ms,
        )

    policy_notes = list(manifest_notes)
    warnings: list[str] = []
    hard_fail_paths: list[str] = []
    allowed_by_policy_paths: list[str] = []
    gate_results: dict[str, bool] = {
        "structural": True,
        "translation": True,
        "operational_policy": True,
    }

    if manifest_payload is None:
        hard_fail_paths.append(
            "manifest: split profile requires --manifest (or manifest payload)"
        )
        gate_results = {
            "structural": False,
            "translation": False,
            "operational_policy": False,
        }
    else:
        t_required = time.perf_counter()
        required_keys = [
            "source_survey_id",
            "target_survey_id",
            "target_language",
        ]
        missing_required = [
            key for key in required_keys if not str(manifest_payload.get(key) or "").strip()
        ]
        if missing_required:
            hard_fail_paths.append(
                "manifest: missing required keys " + ", ".join(missing_required)
            )
            gate_results["operational_policy"] = False
        timings_ms["manifest_required_keys"] = (time.perf_counter() - t_required) * 1000.0

        t_orientation = time.perf_counter()
        canonical_def, split_def, _canonical_norm, split_norm = _resolve_split_orientation(
            def_a=def_a,
            def_b=def_b,
            norm_a=norm_a,
            norm_b=norm_b,
            manifest=manifest_payload,
        )
        timings_ms["split_orientation"] = (time.perf_counter() - t_orientation) * 1000.0

        target_language = str(manifest_payload.get("target_language") or "").strip()
        if not target_language:
            target_language = _extract_base_language(split_def)

        t_translation = time.perf_counter()
        translation_ok, translation_notes, canonical_fp = _compare_translation_gate(
            canonical_payload=canonical_def,
            split_payload=split_def,
            target_language=target_language,
        )
        policy_notes.extend(translation_notes)
        gate_results["translation"] = translation_ok

        expected_fp = str(
            manifest_payload.get("canonical_translation_fingerprint") or ""
        ).strip()
        if expected_fp and canonical_fp != expected_fp:
            gate_results["translation"] = False
            policy_notes.append(
                "manifest: canonical translation fingerprint is stale "
                f"(expected {expected_fp}, got {canonical_fp})"
            )
        timings_ms["translation_gate"] = (time.perf_counter() - t_translation) * 1000.0

        t_keep_languages = time.perf_counter()
        keep_lang_ok, keep_lang_notes = _check_keep_languages_policy(
            split_payload=split_def,
            manifest=manifest_payload,
            target_language=target_language,
        )
        policy_notes.extend(keep_lang_notes)
        timings_ms["keep_languages_policy"] = (
            time.perf_counter() - t_keep_languages
        ) * 1000.0

        t_operational = time.perf_counter()
        op_status, op_notes = _check_operational_policy(
            split_payload=split_def,
            manifest=manifest_payload,
        )
        policy_notes.extend(op_notes)
        operational_ok = keep_lang_ok and all(op_status.values())
        gate_results["operational_policy"] = operational_ok
        timings_ms["operational_policy_gate"] = (
            time.perf_counter() - t_operational
        ) * 1000.0

        t_classify = time.perf_counter()
        allowed, hard, structural_failures, _operational_failures = _classify_split_events(
            events=events,
            split_payload=split_norm,
            translation_ok=gate_results["translation"],
            keep_lang_ok=keep_lang_ok,
            policy_status=op_status,
            manifest=manifest_payload,
        )
        allowed_by_policy_paths.extend(allowed)
        hard_fail_paths.extend(hard)

        if not gate_results["translation"]:
            hard_fail_paths.append("translation gate failed")
        if not gate_results["operational_policy"]:
            hard_fail_paths.append("operational policy gate failed")

        gate_results["structural"] = len(structural_failures) == 0
        timings_ms["split_classification"] = (time.perf_counter() - t_classify) * 1000.0

    hard_fail_paths = _dedupe(hard_fail_paths)
    allowed_by_policy_paths = _dedupe(allowed_by_policy_paths)
    policy_notes = _dedupe(policy_notes)
    warnings = _dedupe(warnings)

    ok = all(gate_results.values()) and not hard_fail_paths
    artifacts: dict[str, str] | None = None
    t_artifacts = time.perf_counter()
    if write_artifacts_on_mismatch and not ok:
        paths = write_deep_parity_artifacts(
            normalized_a=norm_a,
            normalized_b=norm_b,
            survey_a=survey_a,
            survey_b=survey_b,
        )
        artifacts = {k: str(v) for k, v in paths.items()}
    timings_ms["artifact_write"] = (time.perf_counter() - t_artifacts) * 1000.0
    timings_ms["total"] = (time.perf_counter() - started) * 1000.0

    return DeepParityReport(
        ok=ok,
        hash_a=hash_a,
        hash_b=hash_b,
        diff_count=diff_count,
        diff_paths=diff_paths,
        section_counts=section_counts,
        flow_changes=flow_changes,
        artifacts=artifacts,
        profile=mode,
        gate_results=gate_results,
        hard_fail_paths=hard_fail_paths,
        allowed_by_policy_paths=allowed_by_policy_paths,
        warning_paths=warnings,
        policy_notes=policy_notes,
        manifest_path=manifest_path_resolved,
        timings_ms=timings_ms,
    )
