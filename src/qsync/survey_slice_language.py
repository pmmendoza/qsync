"""Slice a multilingual survey into a single-language (rebased) survey (QSF workflow)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .survey_naming import survey_slugged_key
from .translations_utils import normalize_language_code, normalize_language_list
from .dimensions.translations_language_blocks import (
    read_answer_display,
    read_choice_display,
    read_label_display,
    read_question_text,
    read_subquestion_description,
    read_choicegroup_description,
    write_answer_display,
    write_choice_display,
    write_label_display,
    write_question_text,
    write_subquestion_description,
    write_choicegroup_description,
)

_DEFAULT_BASE_LANGUAGE = "EN"


@dataclass(frozen=True)
class SliceCoverageReport:
    base_language: str
    target_language: str
    scanned_total: int
    required_total: int
    missing_required: list[str]
    missing_required_by_type: dict[str, list[str]]
    active_qids_total: int | None = None
    inactive_qids_total: int | None = None

    @property
    def ok_required_total(self) -> int:
        return max(0, self.required_total - len(self.missing_required))

    @property
    def missing_required_total(self) -> int:
        return len(self.missing_required)

    @property
    def pct_required_ok(self) -> float:
        if self.required_total <= 0:
            return 100.0
        return 100.0 * (self.ok_required_total / self.required_total)

    def to_json(self) -> dict[str, Any]:
        sample = self.missing_required[:10]
        payload = {
            "base_language": self.base_language,
            "target_language": self.target_language,
            "scanned_total": self.scanned_total,
            "required_total": self.required_total,
            "ok_required_total": self.ok_required_total,
            "missing_required_total": self.missing_required_total,
            "missing_required_sample": sample,
            "missing_required": list(self.missing_required),
            "missing_required_by_type": {
                k: list(v) for k, v in sorted(self.missing_required_by_type.items())
            },
        }
        if self.active_qids_total is not None:
            payload["active_qids_total"] = self.active_qids_total
        if self.inactive_qids_total is not None:
            payload["inactive_qids_total"] = self.inactive_qids_total
        return payload


@dataclass(frozen=True)
class SliceTransformResult:
    qsf: dict[str, Any]
    base_language_before: str
    base_language_after: str
    kept_languages: list[str]
    warnings: list[str]


def _qsf_survey_entry(qsf: Mapping[str, Any]) -> dict[str, Any]:
    entry = qsf.get("SurveyEntry")
    return dict(entry) if isinstance(entry, dict) else {}


def _ensure_qsf_survey_entry(qsf: dict[str, Any]) -> dict[str, Any]:
    entry = qsf.get("SurveyEntry")
    if not isinstance(entry, dict):
        entry = {}
        qsf["SurveyEntry"] = entry
    return entry


def _qsf_elements(qsf: Mapping[str, Any]) -> list[dict[str, Any]]:
    elements = qsf.get("SurveyElements")
    if isinstance(elements, list):
        return [e for e in elements if isinstance(e, dict)]
    return []


def _find_element(qsf: Mapping[str, Any], element_type: str) -> dict[str, Any] | None:
    for elem in _qsf_elements(qsf):
        if str(elem.get("Element") or "").strip().upper() == element_type.upper():
            return elem
    return None


def _ensure_options_payload(qsf: dict[str, Any]) -> dict[str, Any]:
    elem = _find_element(qsf, "SO")
    if elem is None:
        elem = {
            "Element": "SO",
            "PrimaryAttribute": "Survey Options",
            "SecondaryAttribute": None,
            "TertiaryAttribute": None,
            "Payload": {},
        }
        qsf.setdefault("SurveyElements", [])
        if isinstance(qsf["SurveyElements"], list):
            qsf["SurveyElements"].append(elem)
        else:
            qsf["SurveyElements"] = [elem]
    payload = elem.get("Payload")
    if not isinstance(payload, dict):
        payload = {}
        elem["Payload"] = payload
    return payload


def _active_qids_in_qsf(qsf: Mapping[str, Any]) -> set[str]:
    """Best-effort set of QIDs that appear in SurveyFlow non-Trash blocks."""

    # Build BlockID -> QIDs map (skip Trash blocks).
    block_elem = _find_element(qsf, "BL")
    block_payload = block_elem.get("Payload") if isinstance(block_elem, dict) else None
    if not isinstance(block_payload, dict):
        return set()

    blocks = (
        block_payload.get("Blocks")
        if isinstance(block_payload.get("Blocks"), dict)
        else block_payload
    )
    if not isinstance(blocks, dict):
        return set()

    block_qids: dict[str, list[str]] = {}
    for block_key, block in blocks.items():
        if not isinstance(block, dict):
            continue
        if str(block.get("Type") or "").strip().lower() == "trash":
            continue
        block_id = str(block.get("ID") or block.get("BlockID") or block_key).strip()
        if not block_id:
            continue
        qids: list[str] = []
        for item in block.get("BlockElements") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("Type") or "").strip() != "Question":
                continue
            qid = str(item.get("QuestionID") or "").strip()
            if qid:
                qids.append(qid)
        if qids:
            block_qids[block_id] = qids

    if not block_qids:
        return set()

    flow_elem = _find_element(qsf, "FL")
    flow_payload = flow_elem.get("Payload") if isinstance(flow_elem, dict) else None
    flow = flow_payload.get("Flow") if isinstance(flow_payload, dict) else None
    if not isinstance(flow, list):
        return set()

    active: set[str] = set()

    def _walk(node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                _walk(child)
            return
        if not isinstance(node, dict):
            return

        if str(node.get("Type") or "").strip() == "Block":
            block_id = str(
                node.get("ID") or node.get("BlockID") or node.get("IDString") or ""
            ).strip()
            if block_id and block_id in block_qids:
                active.update(block_qids[block_id])

        for key in ("Flow", "Then", "Else", "ElseFlow"):
            child = node.get(key)
            if isinstance(child, list):
                _walk(child)

    _walk(flow)
    return active


def _iter_question_payloads(
    qsf: Mapping[str, Any],
) -> Iterable[tuple[str, dict[str, Any]]]:
    for elem in _qsf_elements(qsf):
        if str(elem.get("Element") or "").strip().upper() != "SQ":
            continue
        qid = str(elem.get("PrimaryAttribute") or "").strip()
        payload = elem.get("Payload")
        if not qid:
            if isinstance(payload, dict):
                qid = str(
                    payload.get("QuestionID") or payload.get("QuestionId") or ""
                ).strip()
        if not qid or not isinstance(payload, dict):
            continue
        yield qid, payload


def _resolve_base_language_from_qsf(qsf: Mapping[str, Any]) -> str:
    options = _find_element(qsf, "SO")
    if isinstance(options, dict):
        payload = options.get("Payload")
        if isinstance(payload, dict):
            lang = normalize_language_code(str(payload.get("SurveyLanguage") or ""))
            if lang:
                return lang
    entry = _qsf_survey_entry(qsf)
    lang = normalize_language_code(str(entry.get("SurveyLanguage") or ""))
    return lang or _DEFAULT_BASE_LANGUAGE


def _empty(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def _is_numeric_value(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return False
        try:
            float(text)
        except ValueError:
            return False
        return True
    return False


def _get_survey_metadata_base(
    options: Mapping[str, Any], entry: Mapping[str, Any]
) -> dict[str, str]:
    title = str(options.get("SurveyTitle") or "")
    desc = str(
        options.get("SurveyMetaDescription")
        or options.get("SurveyDescription")
        or entry.get("SurveyDescription")
        or ""
    )
    return {"SurveyTitle": title, "SurveyMetaDescription": desc}


def _metadata_translations(options: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    meta = options.get("MetaDataTranslations")
    if not isinstance(meta, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for lang, entry in meta.items():
        if not isinstance(entry, dict):
            continue
        norm = normalize_language_code(str(lang or ""))
        if not norm:
            continue
        out[norm] = dict(entry)
    return out


def _target_metadata_value(meta: Mapping[str, Any]) -> dict[str, str]:
    title = str(meta.get("SurveyTitle") or "")
    desc_raw = meta.get("SurveyMetaDescription")
    if desc_raw is None:
        desc_raw = meta.get("SurveyDescription")
    desc = str(desc_raw or "")
    return {"SurveyTitle": title, "SurveyMetaDescription": desc}


def _write_metadata_base_fields(
    options: dict[str, Any],
    entry: dict[str, Any],
    *,
    title: str | None,
    description: str | None,
) -> None:
    if title is not None and title.strip():
        options["SurveyTitle"] = title
    if description is not None and description.strip():
        options["SurveyMetaDescription"] = description


def _key(type_: str, qid: str | None = None, item_id: str | None = None) -> str:
    if not qid:
        return type_
    if item_id:
        return f"{qid}_{type_}{item_id}"
    return f"{qid}_{type_}"


def _parse_missing_key(key: str) -> tuple[str | None, str | None, str | None]:
    if key in {"SurveyTitle", "SurveyMetaDescription"}:
        return ("Meta", None, key)
    if "_" not in key:
        return (None, None, None)
    qid, rest = key.split("_", 1)
    for type_name in (
        "QuestionText",
        "Choice",
        "Answer",
        "Label",
        "SubQuestion",
        "ChoiceGroup",
    ):
        if rest.startswith(type_name):
            item_id = rest[len(type_name) :] or None
            return (type_name, qid, item_id)
    return (None, None, None)


def compute_slice_coverage(
    qsf: Mapping[str, Any], *, target_language: str
) -> SliceCoverageReport:
    base_language = _resolve_base_language_from_qsf(qsf)
    target = normalize_language_code(target_language)
    if not target:
        raise ValueError("target_language must be non-empty")

    active_qids = _active_qids_in_qsf(qsf)
    active_qids_total: int | None = None
    inactive_qids_total: int | None = None
    use_active_filter = bool(active_qids)

    missing: list[str] = []
    missing_by_type: dict[str, list[str]] = {}
    scanned_total = 0
    required_total = 0
    total_qids = 0

    for qid, question in _iter_question_payloads(qsf):
        total_qids += 1
        if use_active_filter and qid not in active_qids:
            continue
        base_text = str(question.get("QuestionText") or "")
        scanned_total += 1
        if not _empty(base_text):
            required_total += 1
            target_text = read_question_text(question, target)
            if _empty(target_text):
                k = _key("QuestionText", qid)
                missing.append(k)
                missing_by_type.setdefault("QuestionText", []).append(k)

        choices = question.get("Choices") or {}
        if isinstance(choices, dict):
            for cid, choice in choices.items():
                scanned_total += 1
                base_raw = None
                base_val = ""
                if isinstance(choice, dict):
                    base_raw = choice.get("Display")
                    if _is_numeric_value(base_raw):
                        continue
                    base_val = str(base_raw or "")
                if _empty(base_val):
                    continue
                required_total += 1
                target_val = read_choice_display(question, target, str(cid))
                if _empty(target_val):
                    k = _key("Choice", qid, str(cid))
                    missing.append(k)
                    missing_by_type.setdefault("Choice", []).append(k)

        answers = question.get("Answers") or {}
        if isinstance(answers, dict):
            for aid, answer in answers.items():
                scanned_total += 1
                base_raw = None
                base_val = ""
                if isinstance(answer, dict):
                    base_raw = answer.get("Display")
                    if _is_numeric_value(base_raw):
                        continue
                    base_val = str(base_raw or "")
                if _empty(base_val):
                    continue
                required_total += 1
                target_val = read_answer_display(question, target, str(aid))
                if _empty(target_val):
                    k = _key("Answer", qid, str(aid))
                    missing.append(k)
                    missing_by_type.setdefault("Answer", []).append(k)

        labels = question.get("Labels") or {}
        if isinstance(labels, dict):
            for lid, label in labels.items():
                scanned_total += 1
                base_raw = None
                base_val = ""
                if isinstance(label, dict):
                    base_raw = label.get("Display")
                    if _is_numeric_value(base_raw):
                        continue
                    base_val = str(base_raw or "")
                if _empty(base_val):
                    continue
                required_total += 1
                target_val = read_label_display(question, target, str(lid))
                if _empty(target_val):
                    k = _key("Label", qid, str(lid))
                    missing.append(k)
                    missing_by_type.setdefault("Label", []).append(k)

        sub_questions = question.get("SubQuestions") or {}
        if isinstance(sub_questions, dict):
            for sid, subq in sub_questions.items():
                scanned_total += 1
                base_raw = None
                base_val = ""
                if isinstance(subq, dict):
                    base_raw = subq.get("Description")
                    if _is_numeric_value(base_raw):
                        continue
                    base_val = str(base_raw or "")
                if _empty(base_val):
                    continue
                required_total += 1
                target_val = read_subquestion_description(question, target, str(sid))
                if _empty(target_val):
                    k = _key("SubQuestion", qid, str(sid))
                    missing.append(k)
                    missing_by_type.setdefault("SubQuestion", []).append(k)

        choice_groups = question.get("ChoiceGroups") or {}
        if isinstance(choice_groups, dict):
            for gid, group in choice_groups.items():
                scanned_total += 1
                base_raw = None
                base_val = ""
                if isinstance(group, dict):
                    base_raw = group.get("Description")
                    if _is_numeric_value(base_raw):
                        continue
                    base_val = str(base_raw or "")
                if _empty(base_val):
                    continue
                required_total += 1
                target_val = read_choicegroup_description(question, target, str(gid))
                if _empty(target_val):
                    k = _key("ChoiceGroup", qid, str(gid))
                    missing.append(k)
                    missing_by_type.setdefault("ChoiceGroup", []).append(k)

    options_elem = _find_element(qsf, "SO")
    options = {}
    if isinstance(options_elem, dict) and isinstance(options_elem.get("Payload"), dict):
        options = options_elem["Payload"]
    entry = _qsf_survey_entry(qsf)
    base_meta = _get_survey_metadata_base(options, entry)
    meta_trans = _metadata_translations(options)
    meta_target = meta_trans.get(target) or {}
    target_meta = (
        _target_metadata_value(meta_target) if isinstance(meta_target, dict) else {}
    )

    scanned_total += 1
    if not _empty(base_meta.get("SurveyTitle")):
        required_total += 1
        if _empty(target_meta.get("SurveyTitle")):
            k = "SurveyTitle"
            missing.append(k)
            missing_by_type.setdefault("Meta", []).append(k)

    scanned_total += 1
    if not _empty(base_meta.get("SurveyMetaDescription")):
        required_total += 1
        if _empty(target_meta.get("SurveyMetaDescription")):
            k = "SurveyMetaDescription"
            missing.append(k)
            missing_by_type.setdefault("Meta", []).append(k)

    missing_sorted = sorted(set(missing))
    missing_by_type_sorted: dict[str, list[str]] = {
        k: sorted(set(v)) for k, v in missing_by_type.items()
    }
    if use_active_filter:
        active_qids_total = len(active_qids)
        inactive_qids_total = max(0, total_qids - active_qids_total)
    return SliceCoverageReport(
        base_language=base_language,
        target_language=target,
        scanned_total=scanned_total,
        required_total=required_total,
        missing_required=missing_sorted,
        missing_required_by_type=missing_by_type_sorted,
        active_qids_total=active_qids_total,
        inactive_qids_total=inactive_qids_total,
    )


def apply_fallback_translations(
    qsf: dict[str, Any],
    *,
    target_language: str,
    missing_keys: Iterable[str],
) -> list[str]:
    """Fill missing target-language values with base-language text."""

    target = normalize_language_code(target_language)
    if not target:
        raise ValueError("target_language must be non-empty")

    missing_list = list(missing_keys)
    if not missing_list:
        return []

    options = _ensure_options_payload(qsf)
    entry = _ensure_qsf_survey_entry(qsf)
    base_meta = _get_survey_metadata_base(options, entry)
    meta_trans = _metadata_translations(options)
    target_meta = meta_trans.get(target) or {}
    if not isinstance(target_meta, dict):
        target_meta = {}

    filled: list[str] = []

    if "SurveyTitle" in missing_list and base_meta.get("SurveyTitle", "").strip():
        target_meta["SurveyTitle"] = base_meta["SurveyTitle"]
        filled.append("SurveyTitle")
    if (
        "SurveyMetaDescription" in missing_list
        and base_meta.get("SurveyMetaDescription", "").strip()
    ):
        target_meta["SurveyMetaDescription"] = base_meta["SurveyMetaDescription"]
        filled.append("SurveyMetaDescription")

    if target_meta:
        meta_trans[target] = target_meta
        options["MetaDataTranslations"] = meta_trans

    qid_map = {qid: payload for qid, payload in _iter_question_payloads(qsf)}

    for key in missing_list:
        type_name, qid, item_id = _parse_missing_key(key)
        if type_name in {None, "Meta"} or not qid:
            continue
        question = qid_map.get(qid)
        if not isinstance(question, dict):
            continue

        if type_name == "QuestionText":
            base_val = str(question.get("QuestionText") or "")
            if base_val.strip():
                write_question_text(question, target, base_val)
                filled.append(key)
            continue

        if type_name == "Choice":
            if not item_id:
                continue
            choice = (question.get("Choices") or {}).get(item_id)
            if isinstance(choice, dict):
                base_val = str(choice.get("Display") or "")
                if base_val.strip():
                    write_choice_display(question, target, item_id, base_val)
                    filled.append(key)
            continue

        if type_name == "Answer":
            if not item_id:
                continue
            answer = (question.get("Answers") or {}).get(item_id)
            if isinstance(answer, dict):
                base_val = str(answer.get("Display") or "")
                if base_val.strip():
                    write_answer_display(question, target, item_id, base_val)
                    filled.append(key)
            continue

        if type_name == "Label":
            if not item_id:
                continue
            label = (question.get("Labels") or {}).get(item_id)
            if isinstance(label, dict):
                base_val = str(label.get("Display") or "")
                if base_val.strip():
                    write_label_display(question, target, item_id, base_val)
                    filled.append(key)
            continue

        if type_name == "SubQuestion":
            if not item_id:
                continue
            subq = (question.get("SubQuestions") or {}).get(item_id)
            if isinstance(subq, dict):
                base_val = str(subq.get("Description") or "")
                if base_val.strip():
                    write_subquestion_description(question, target, item_id, base_val)
                    filled.append(key)
            continue

        if type_name == "ChoiceGroup":
            if not item_id:
                continue
            group = (question.get("ChoiceGroups") or {}).get(item_id)
            if isinstance(group, dict):
                base_val = str(group.get("Description") or "")
                if base_val.strip():
                    write_choicegroup_description(question, target, item_id, base_val)
                    filled.append(key)
            continue

    return filled


FLOW_TEXT_KEYS: tuple[str, ...] = (
    "Message",
    "CustomMessage",
    "EndMessage",
    "Text",
)

# Some node types (notably WebService) can use generic keys like "Text" for
# request config, not participant-facing copy. Never rewrite those during
# language slicing.
FLOW_TEXT_EXCLUDED_TYPES: tuple[str, ...] = ("WebService",)


def _iter_flow_nodes(qsf: Mapping[str, Any]) -> Iterable[dict[str, Any]]:
    elem = _find_element(qsf, "FL")
    if not isinstance(elem, dict):
        return []
    payload = elem.get("Payload")
    if not isinstance(payload, dict):
        return []
    flow = payload.get("Flow")
    if not isinstance(flow, list):
        return []

    nodes: list[dict[str, Any]] = []

    def _walk(node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                _walk(child)
            return
        if not isinstance(node, dict):
            return
        nodes.append(node)
        for key in ("Flow", "Then", "Else", "ElseFlow"):
            child = node.get(key)
            if isinstance(child, list):
                _walk(child)

    _walk(flow)
    return nodes


def _looks_like_language_map(value: Mapping[str, Any]) -> bool:
    if not value:
        return False
    for key in value.keys():
        norm = normalize_language_code(str(key or ""))
        if norm:
            return True
    return False


def _extract_lang_value(value: Any, *, target: str) -> str | None:
    if isinstance(value, dict):
        for key, entry in value.items():
            if normalize_language_code(str(key or "")) == target:
                if isinstance(entry, dict):
                    text_val = entry.get("Text") or entry.get("Message")
                    if isinstance(text_val, str) and text_val.strip():
                        return text_val
                if isinstance(entry, str) and entry.strip():
                    return entry
    if isinstance(value, str) and value.strip():
        return value
    return None


def _filter_language_map(
    value: Mapping[str, Any],
    *,
    kept_languages: Sequence[str],
    target: str,
    target_value: str | None,
) -> dict[str, Any]:
    keep_set = {normalize_language_code(lang) for lang in kept_languages}
    keep_set.discard("")
    filtered: dict[str, Any] = {}
    for key, entry in value.items():
        norm = normalize_language_code(str(key or ""))
        if norm and norm in keep_set:
            filtered[key] = entry
    if target_value and target not in {
        normalize_language_code(str(k or "")) for k in filtered.keys()
    }:
        filtered[target] = target_value
    return filtered


def _rebase_flow_text(
    qsf: dict[str, Any],
    *,
    target_language: str,
    kept_languages: Sequence[str],
    allow_rebase: bool,
) -> list[str]:
    """Rebase participant-visible SurveyFlow text fields to the target language."""

    target = normalize_language_code(target_language)
    if not target:
        return []
    warnings: list[str] = []
    keep_other = len(kept_languages) > 1

    for node in _iter_flow_nodes(qsf):
        node_type = str(node.get("Type") or "").strip()
        if node_type in FLOW_TEXT_EXCLUDED_TYPES:
            continue
        for key in FLOW_TEXT_KEYS:
            if key not in node:
                continue
            value = node.get(key)
            if not allow_rebase:
                if isinstance(value, str) and value.strip():
                    warnings.append(
                        f"SurveyFlow node Type={node_type} has {key}; not rebased (--no-flow-text)."
                    )
                elif isinstance(value, dict) and _looks_like_language_map(value):
                    warnings.append(
                        f"SurveyFlow node Type={node_type} has {key} translations; not rebased (--no-flow-text)."
                    )
                continue

            if isinstance(value, dict) and _looks_like_language_map(value):
                target_val = _extract_lang_value(value, target=target)
                if target_val:
                    if keep_other:
                        node[key] = _filter_language_map(
                            value,
                            kept_languages=kept_languages,
                            target=target,
                            target_value=target_val,
                        )
                    else:
                        node[key] = target_val
                else:
                    warnings.append(
                        f"SurveyFlow node Type={node_type} has {key} translations but no {target} value."
                    )
                continue

            if isinstance(value, str):
                lang_block = node.get("Language")
                if isinstance(lang_block, dict):
                    target_entry = None
                    for lang, entry in lang_block.items():
                        if normalize_language_code(
                            str(lang or "")
                        ) == target and isinstance(entry, dict):
                            target_entry = entry
                            break
                    if target_entry and isinstance(target_entry.get(key), str):
                        node[key] = str(target_entry.get(key) or "")
                        if keep_other:
                            keep_set = {
                                normalize_language_code(lang) for lang in kept_languages
                            }
                            filtered: dict[str, Any] = {}
                            for lang, entry in lang_block.items():
                                if normalize_language_code(str(lang or "")) in keep_set:
                                    filtered[lang] = entry
                            node["Language"] = filtered
                        else:
                            node["Language"] = {target: target_entry}
                    else:
                        warnings.append(
                            f"SurveyFlow node Type={node_type} has {key} but no {target} translation."
                        )
                elif value.strip():
                    warnings.append(
                        f"SurveyFlow node Type={node_type} has {key} text with no translations."
                    )

    return warnings


def warn_if_flow_text_present(qsf: Mapping[str, Any]) -> list[str]:
    """Best-effort detector for SurveyFlow nodes that may contain participant-visible text."""

    warnings: list[str] = []
    for node in _iter_flow_nodes(qsf):
        t = str(node.get("Type") or "").strip()
        if t in FLOW_TEXT_EXCLUDED_TYPES:
            continue
        fields: list[str] = []
        for key in FLOW_TEXT_KEYS:
            if key not in node:
                continue
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                fields.append(key)
            elif isinstance(value, dict) and _looks_like_language_map(value):
                fields.append(key)
        if fields:
            warnings.append(
                f"SurveyFlow node Type={t} contains text fields ({', '.join(fields)})."
            )

    return warnings


def resolve_keep_languages(
    enabled_languages: Iterable[str],
    *,
    target_language: str,
    base_language: str,
    keep_languages_raw: str,
) -> list[str]:
    """Resolve the final enabled-language list for the sliced survey.

    `keep_languages_raw` supports:
    - "target-only" (default): keep only the target language
    - "all": keep all enabled languages
    - "DE,FR,NL": explicit comma list of languages to keep (target and old base are always included)
    """

    target = normalize_language_code(target_language)
    base = normalize_language_code(base_language)
    enabled = normalize_language_list(enabled_languages)
    raw = (keep_languages_raw or "target-only").strip().lower()

    if raw in {"target-only", "target", "mono", "monolingual"}:
        return [target]

    keep: list[str]
    if raw == "all":
        keep = list(enabled)
    else:
        keep = normalize_language_list(
            [part.strip() for part in keep_languages_raw.split(",") if part.strip()]
        )

    # Ensure target and old base are always present when keeping multiple languages.
    keep_set = {target, base, *keep}
    keep_set.discard("")
    keep_ordered = [target] + sorted(lang for lang in keep_set if lang != target)
    return keep_ordered


def _update_available_languages(
    options: dict[str, Any],
    *,
    kept_languages: list[str],
) -> None:
    current = options.get("AvailableLanguages")
    if isinstance(current, dict):
        filtered: dict[str, Any] = {}
        for lang in kept_languages:
            if lang in current:
                filtered[lang] = current[lang]
            else:
                filtered[lang] = True
        options["AvailableLanguages"] = filtered
        return
    options["AvailableLanguages"] = list(kept_languages)


def _ensure_language_block(question: dict[str, Any], language: str) -> dict[str, Any]:
    lang = normalize_language_code(language)
    if not lang:
        return {}
    block = question.get("Language")
    if not isinstance(block, dict):
        block = {}
        question["Language"] = block
    entry = block.get(lang)
    if not isinstance(entry, dict):
        entry = {}
        block[lang] = entry
    return entry


def _materialize_base_field_to_lang(
    question: dict[str, Any],
    *,
    base_language: str,
    warnings: list[str],
) -> None:
    lang = normalize_language_code(base_language)
    if not lang:
        return

    base_block = _ensure_language_block(question, lang)

    def _fill_str_field(field: str, base_value: str) -> None:
        existing = base_block.get(field)
        if isinstance(existing, str) and existing.strip():
            if base_value.strip() and existing.strip() != base_value.strip():
                warnings.append(
                    f"{question.get('QuestionID') or ''}: Language[{lang}].{field} differs from base; "
                    "leaving existing translation as-is."
                )
            return
        if base_value.strip():
            base_block[field] = base_value

    _fill_str_field("QuestionText", str(question.get("QuestionText") or ""))

    for section, key_name in (
        ("Choices", "Display"),
        ("Answers", "Display"),
        ("Labels", "Display"),
    ):
        base_section = question.get(section) or {}
        if not isinstance(base_section, dict):
            continue
        dest = base_block.get(section)
        if not isinstance(dest, dict):
            dest = {}
            base_block[section] = dest
        for item_id, item in base_section.items():
            if not isinstance(item, dict):
                continue
            base_val = str(item.get(key_name) or "")
            if not base_val.strip():
                continue
            existing_item = dest.get(str(item_id))
            if not isinstance(existing_item, dict):
                existing_item = {}
            existing_val = existing_item.get(key_name)
            if isinstance(existing_val, str) and existing_val.strip():
                if existing_val.strip() != base_val.strip():
                    warnings.append(
                        f"{question.get('QuestionID') or ''}: Language[{lang}].{section}[{item_id}].{key_name} differs from base; "
                        "leaving existing translation as-is."
                    )
                dest[str(item_id)] = existing_item
                continue
            existing_item[key_name] = base_val
            dest[str(item_id)] = existing_item

    for section, key_name in (
        ("SubQuestions", "Description"),
        ("ChoiceGroups", "Description"),
    ):
        base_section = question.get(section) or {}
        if not isinstance(base_section, dict):
            continue
        dest = base_block.get(section)
        if not isinstance(dest, dict):
            dest = {}
            base_block[section] = dest
        for item_id, item in base_section.items():
            if not isinstance(item, dict):
                continue
            base_val = str(item.get(key_name) or "")
            if not base_val.strip():
                continue
            existing_item = dest.get(str(item_id))
            if not isinstance(existing_item, dict):
                existing_item = {}
            existing_val = existing_item.get(key_name)
            if isinstance(existing_val, str) and existing_val.strip():
                if existing_val.strip() != base_val.strip():
                    warnings.append(
                        f"{question.get('QuestionID') or ''}: Language[{lang}].{section}[{item_id}].{key_name} differs from base; "
                        "leaving existing translation as-is."
                    )
                dest[str(item_id)] = existing_item
                continue
            existing_item[key_name] = base_val
            dest[str(item_id)] = existing_item


def slice_qsf_to_language(
    qsf: dict[str, Any],
    *,
    target_language: str,
    kept_languages: list[str],
    rebase_flow_text: bool = True,
) -> SliceTransformResult:
    """Rebase the QSF so `target_language` becomes the base language.

    This mutates `qsf` in-place and returns it for chaining.
    """

    target = normalize_language_code(target_language)
    if not target:
        raise ValueError("target_language must be non-empty")

    options = _ensure_options_payload(qsf)
    entry = _ensure_qsf_survey_entry(qsf)

    old_base = normalize_language_code(
        str(options.get("SurveyLanguage") or entry.get("SurveyLanguage") or "")
    )
    base_before = old_base or _DEFAULT_BASE_LANGUAGE

    kept = normalize_language_list(kept_languages)
    if not kept or kept[0] != target:
        kept = [target, *[lang for lang in kept if lang != target]]

    warnings: list[str] = []
    keep_other = len(kept) > 1

    # Update base language markers.
    options["SurveyLanguage"] = target
    entry["SurveyLanguage"] = target

    _update_available_languages(options, kept_languages=kept)

    # Handle survey-level metadata rebasing + translations preservation.
    base_meta = _get_survey_metadata_base(options, entry)
    meta_trans = _metadata_translations(options)

    if keep_other and target != base_before:
        old_entry = meta_trans.get(base_before) or {}
        if not isinstance(old_entry, dict):
            old_entry = {}
        if base_meta["SurveyTitle"].strip() and _empty(old_entry.get("SurveyTitle")):
            old_entry["SurveyTitle"] = base_meta["SurveyTitle"]
        desc_existing = old_entry.get("SurveyMetaDescription")
        if desc_existing is None:
            desc_existing = old_entry.get("SurveyDescription")
        if base_meta["SurveyMetaDescription"].strip() and _empty(desc_existing):
            old_entry["SurveyMetaDescription"] = base_meta["SurveyMetaDescription"]
        meta_trans[base_before] = old_entry

    target_meta_raw = meta_trans.get(target) or {}
    if isinstance(target_meta_raw, dict):
        target_meta = _target_metadata_value(target_meta_raw)
        _write_metadata_base_fields(
            options,
            entry,
            title=target_meta.get("SurveyTitle"),
            description=target_meta.get("SurveyMetaDescription"),
        )

    meta_trans.pop(target, None)
    if not keep_other:
        options["MetaDataTranslations"] = {}
    else:
        options["MetaDataTranslations"] = {
            lang: entry
            for lang, entry in meta_trans.items()
            if lang in set(kept) and lang != target and isinstance(entry, dict)
        }

    # Rebase questions.
    for _qid, question in _iter_question_payloads(qsf):
        old_lang_block = question.get("Language")
        lang_block = old_lang_block if isinstance(old_lang_block, dict) else {}

        if keep_other and target != base_before:
            _materialize_base_field_to_lang(
                question,
                base_language=base_before,
                warnings=warnings,
            )

        if target != base_before:
            # QuestionText
            t = read_question_text(question, target)
            if isinstance(t, str) and t.strip():
                question["QuestionText"] = t

            for section, reader, base_key in (
                ("Choices", read_choice_display, "Display"),
                ("Answers", read_answer_display, "Display"),
                ("Labels", read_label_display, "Display"),
            ):
                base_section = question.get(section) or {}
                if not isinstance(base_section, dict):
                    continue
                for item_id in list(base_section.keys()):
                    value = reader(question, target, str(item_id))
                    if isinstance(value, str) and value.strip():
                        base_section_item = base_section.get(item_id)
                        if isinstance(base_section_item, dict):
                            if _is_numeric_value(base_section_item.get(base_key)):
                                continue
                            base_section_item[base_key] = value

            sub_questions = question.get("SubQuestions") or {}
            if isinstance(sub_questions, dict):
                for sub_id in list(sub_questions.keys()):
                    value = read_subquestion_description(question, target, str(sub_id))
                    if isinstance(value, str) and value.strip():
                        base_item = sub_questions.get(sub_id) or {}
                        if isinstance(base_item, dict):
                            if _is_numeric_value(base_item.get("Description")):
                                continue
                            base_item["Description"] = value

            choice_groups = question.get("ChoiceGroups") or {}
            if isinstance(choice_groups, dict):
                for group_id in list(choice_groups.keys()):
                    value = read_choicegroup_description(
                        question, target, str(group_id)
                    )
                    if isinstance(value, str) and value.strip():
                        base_item = choice_groups.get(group_id) or {}
                        if isinstance(base_item, dict):
                            if _is_numeric_value(base_item.get("Description")):
                                continue
                            base_item["Description"] = value

            # Now redundant (target is base).
            if keep_other and isinstance(lang_block, dict):
                for key in list(lang_block.keys()):
                    if normalize_language_code(key) == target:
                        lang_block.pop(key, None)

        if not keep_other:
            if isinstance(lang_block, dict):
                target_entry = None
                for lang, lang_entry in lang_block.items():
                    if normalize_language_code(lang) == target and isinstance(
                        lang_entry, dict
                    ):
                        target_entry = lang_entry
                        break
                if target_entry:
                    question["Language"] = {target: target_entry}
                else:
                    question.pop("Language", None)
            else:
                question.pop("Language", None)
        else:
            if isinstance(lang_block, dict):
                keep_set = set(kept)
                filtered: dict[str, Any] = {}
                for lang, lang_entry in lang_block.items():
                    norm = normalize_language_code(lang)
                    if (
                        norm
                        and norm in keep_set
                        and norm != target
                        and isinstance(lang_entry, dict)
                    ):
                        filtered[norm] = lang_entry
                if filtered:
                    question["Language"] = filtered
                else:
                    question.pop("Language", None)

    # Rebase SurveyFlow participant-visible text (best-effort).
    warnings.extend(
        _rebase_flow_text(
            qsf,
            target_language=target,
            kept_languages=kept,
            allow_rebase=rebase_flow_text,
        )
    )

    return SliceTransformResult(
        qsf=qsf,
        base_language_before=base_before,
        base_language_after=target,
        kept_languages=kept,
        warnings=warnings,
    )


def sha256_of_qsf_upload_bytes(qsf: Mapping[str, Any]) -> str:
    """Hash the exact bytes we send to Qualtrics for QSF import (best-effort)."""
    data = json.dumps(dict(qsf)).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def sha256_of_json(payload: Mapping[str, Any]) -> str:
    """Stable SHA256 for JSON payloads used in slice manifests/baselines."""

    canonical = json.dumps(
        dict(payload),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def infer_target_country(target_language: str) -> str:
    """Best-effort country hint from language code (e.g., DE or FR-CA -> DE/CA)."""

    lang = normalize_language_code(target_language)
    if "-" in lang:
        suffix = lang.split("-", 1)[1].strip()
        return suffix.upper() if suffix else ""
    return lang.upper() if len(lang) == 2 else ""


def write_json_with_backup(path: Path, payload: Mapping[str, Any]) -> None:
    serialized = json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True) + "\n"
    if path.exists():
        backup_path = path.with_suffix(path.suffix + ".bak")
        backup_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized, encoding="utf-8")


def default_slices_dir(root: Path) -> Path:
    from .config import resolve_scoped_dir

    surveys_dir = resolve_scoped_dir("surveys", root=root)
    return surveys_dir / "slices"


def write_coverage_report(
    root: Path,
    *,
    source_survey_id: str,
    target_language: str,
    report: SliceCoverageReport,
    source_survey_name: str | None = None,
) -> Path:
    slices = default_slices_dir(root)
    source_ref = survey_slugged_key(
        source_survey_id,
        survey_name=source_survey_name,
        root=root,
    )
    path = (
        slices
        / f"coverage__{source_ref}__{normalize_language_code(target_language)}.json"
    )
    payload = dict(report.to_json())
    payload["source_survey_id"] = source_survey_id
    payload["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    write_json_with_backup(path, payload)
    return path


def write_slice_manifest(
    root: Path,
    *,
    source_survey_id: str,
    source_survey_name: str,
    source_base_language: str,
    target_language: str,
    new_survey_id: str,
    new_survey_name: str,
    keep_languages_mode: str,
    kept_languages: list[str],
    allow_incomplete: bool,
    allow_fallback: bool = False,
    fallback_filled_total: int | None = None,
    fallback_filled_sample: list[str] | None = None,
    coverage_report_path: Path,
    report: SliceCoverageReport,
    qsf_sha256: str,
    qsync_version: str,
    manifest_version: int = 2,
    parity_profile: str = "split",
    target_country: str | None = None,
    keep_languages_policy: str | None = None,
    completion_redirect_url: str | None = None,
    eos_policy: Mapping[str, Any] | None = None,
    expected_flow_overrides: Sequence[Mapping[str, Any]] | None = None,
    canonical_translation_fingerprint: str | None = None,
    baseline_snapshot_ref: str | None = None,
) -> Path:
    slices = default_slices_dir(root)
    source_ref = survey_slugged_key(
        source_survey_id,
        survey_name=source_survey_name,
        root=root,
    )
    path = (
        slices / f"{source_ref}__{normalize_language_code(target_language)}.json"
    )
    payload: dict[str, Any] = {
        "manifest_version": int(manifest_version),
        "parity_profile": str(parity_profile or "split"),
        "source_survey_id": source_survey_id,
        "source_survey_name": source_survey_name,
        "source_base_language": source_base_language,
        "target_survey_id": new_survey_id,
        "target_survey_name": new_survey_name,
        "target_language": normalize_language_code(target_language),
        "target_country": str(
            target_country
            if target_country is not None
            else infer_target_country(target_language)
        ),
        "keep_languages_policy": str(keep_languages_policy or keep_languages_mode),
        "new_survey_id": new_survey_id,
        "new_survey_name": new_survey_name,
        "keep_languages_mode": keep_languages_mode,
        "kept_languages": list(kept_languages),
        "allow_incomplete": bool(allow_incomplete),
        "allow_fallback": bool(allow_fallback),
        "completion_redirect_url": str(completion_redirect_url or ""),
        "eos_policy": dict(eos_policy or {}),
        "expected_flow_overrides": [dict(entry) for entry in (expected_flow_overrides or [])],
        "canonical_translation_fingerprint": str(canonical_translation_fingerprint or ""),
        "baseline_snapshot_ref": str(baseline_snapshot_ref or ""),
        "fallback_filled_total": int(fallback_filled_total or 0),
        "fallback_filled_sample": list(fallback_filled_sample or []),
        "coverage": {
            "required_total": report.required_total,
            "missing_required_total": report.missing_required_total,
            "missing_required_sample": report.missing_required[:10],
            "pct_required_ok": report.pct_required_ok,
        },
        "coverage_report_path": str(coverage_report_path),
        "qsf_sha256": qsf_sha256,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "qsync_version": qsync_version,
    }
    write_json_with_backup(path, payload)
    return path


def write_split_baseline_snapshot(
    root: Path,
    *,
    source_survey_id: str,
    source_survey_name: str,
    target_language: str,
    canonical_definition: Mapping[str, Any],
    canonical_translation_projection: Mapping[str, Any],
) -> Path:
    from .survey_deep_parity import normalize_survey_definition_for_deep_parity

    slices = default_slices_dir(root)
    source_ref = survey_slugged_key(
        source_survey_id,
        survey_name=source_survey_name,
        root=root,
    )
    path = (
        slices / f"baseline__{source_ref}__{normalize_language_code(target_language)}.json"
    )
    normalized = normalize_survey_definition_for_deep_parity(
        canonical_definition,
        profile="cross_account",
    )
    projection_payload = dict(canonical_translation_projection)
    payload: dict[str, Any] = {
        "source_survey_id": source_survey_id,
        "source_survey_name": source_survey_name,
        "target_language": normalize_language_code(target_language),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "canonical_translation_fingerprint": sha256_of_json(projection_payload),
        "canonical_projection_total": len(projection_payload),
        "canonical_projection": projection_payload,
        "normalized_definition_sha256": sha256_of_json(
            normalized if isinstance(normalized, dict) else {}
        ),
    }
    write_json_with_backup(path, payload)
    return path


def write_batch_manifest(
    root: Path,
    *,
    source_survey_id: str,
    source_survey_name: str,
    source_base_language: str,
    slices: Sequence[Mapping[str, Any]],
    qsync_version: str,
) -> Path:
    slices_dir = default_slices_dir(root)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    source_ref = survey_slugged_key(
        source_survey_id,
        survey_name=source_survey_name,
        root=root,
    )
    path = slices_dir / f"batch__{source_ref}__{stamp}.json"
    payload: dict[str, Any] = {
        "source_survey_id": source_survey_id,
        "source_survey_name": source_survey_name,
        "source_base_language": source_base_language,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "qsync_version": qsync_version,
        "slices": [dict(entry) for entry in slices],
    }
    write_json_with_backup(path, payload)
    return path


def write_dry_run_qsf(
    root: Path,
    *,
    source_survey_id: str,
    target_language: str,
    qsf: Mapping[str, Any],
    source_survey_name: str | None = None,
) -> Path:
    slices = default_slices_dir(root)
    source_ref = survey_slugged_key(
        source_survey_id,
        survey_name=source_survey_name,
        root=root,
    )
    path = (
        slices
        / f"dryrun__{source_ref}__{normalize_language_code(target_language)}.qsf.json"
    )
    write_json_with_backup(path, dict(qsf))
    return path
