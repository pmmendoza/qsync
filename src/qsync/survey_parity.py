"""Parity checks between two Qualtrics surveys (QSF-level)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class ParityReport:
    qids_only_in_a: list[str]
    qids_only_in_b: list[str]
    tags_only_in_a: list[str]
    tags_only_in_b: list[str]
    flow_types_a: list[str]
    flow_types_b: list[str]
    flow_qids_a: list[str]
    flow_qids_b: list[str]
    block_memberships_only_in_a: list[list[str]]
    block_memberships_only_in_b: list[list[str]]
    warnings: list[str]

    @property
    def qids_match(self) -> bool:
        return not self.qids_only_in_a and not self.qids_only_in_b

    @property
    def tags_match(self) -> bool:
        return not self.tags_only_in_a and not self.tags_only_in_b

    @property
    def flow_types_match(self) -> bool:
        return self.flow_types_a == self.flow_types_b

    @property
    def flow_qids_match(self) -> bool:
        return self.flow_qids_a == self.flow_qids_b

    @property
    def block_memberships_match(self) -> bool:
        return not self.block_memberships_only_in_a and not self.block_memberships_only_in_b

    @property
    def ok(self) -> bool:
        return (
            self.qids_match
            and self.tags_match
            and self.flow_types_match
            and self.flow_qids_match
            and self.block_memberships_match
        )


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


def _iter_question_payloads(qsf: Mapping[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    for elem in _qsf_elements(qsf):
        if str(elem.get("Element") or "").strip().upper() != "SQ":
            continue
        qid = str(elem.get("PrimaryAttribute") or "").strip()
        payload = elem.get("Payload")
        if not qid and isinstance(payload, dict):
            qid = str(payload.get("QuestionID") or payload.get("QuestionId") or "").strip()
        if qid and isinstance(payload, dict):
            yield qid, payload


def _block_qids_by_id(qsf: Mapping[str, Any]) -> dict[str, list[str]]:
    block_elem = _find_element(qsf, "BL")
    block_payload = block_elem.get("Payload") if isinstance(block_elem, dict) else None
    if not isinstance(block_payload, dict):
        return {}

    blocks = (
        block_payload.get("Blocks")
        if isinstance(block_payload.get("Blocks"), dict)
        else block_payload
    )
    if not isinstance(blocks, dict):
        return {}

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
    return block_qids


def _collect_flow_signature(
    qsf: Mapping[str, Any], block_qids: Mapping[str, list[str]]
) -> tuple[list[str], list[str], list[str]]:
    flow_elem = _find_element(qsf, "FL")
    payload = flow_elem.get("Payload") if isinstance(flow_elem, dict) else None
    flow = payload.get("Flow") if isinstance(payload, dict) else None
    if not isinstance(flow, list):
        return ([], [], ["Missing SurveyFlow (FL element)."])

    types: list[str] = []
    qids: list[str] = []
    warnings: list[str] = []

    def _walk(nodes: list[Any]) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            ntype = str(node.get("Type") or "").strip()
            types.append(ntype)

            if ntype == "Block":
                block_id = str(
                    node.get("ID") or node.get("BlockID") or node.get("IDString") or ""
                ).strip()
                if block_id and block_id in block_qids:
                    qids.extend(block_qids[block_id])
                elif block_id:
                    warnings.append(f"Block {block_id} not found in BL payload.")

            qid = str(node.get("QuestionID") or node.get("QuestionId") or "").strip()
            if qid:
                qids.append(qid)

            for key in ("Flow", "Then", "Else", "ElseFlow"):
                child = node.get(key)
                if isinstance(child, list):
                    _walk(child)

    _walk(flow)
    return (types, qids, warnings)


def _collect_block_memberships(block_qids: Mapping[str, list[str]]) -> Counter[tuple[str, ...]]:
    counter: Counter[tuple[str, ...]] = Counter()
    for qids in block_qids.values():
        if not qids:
            continue
        counter[tuple(sorted(set(qids)))] += 1
    return counter


def _counter_diff(
    a: Counter[tuple[str, ...]], b: Counter[tuple[str, ...]]
) -> list[list[str]]:
    diff: list[list[str]] = []
    for item, count in (a - b).items():
        for _ in range(count):
            diff.append(list(item))
    return diff


def compare_qsf_parity(qsf_a: Mapping[str, Any], qsf_b: Mapping[str, Any]) -> ParityReport:
    qids_a = {qid for qid, _ in _iter_question_payloads(qsf_a)}
    qids_b = {qid for qid, _ in _iter_question_payloads(qsf_b)}

    tags_a = {
        str(payload.get("DataExportTag") or "").strip()
        for _qid, payload in _iter_question_payloads(qsf_a)
        if str(payload.get("DataExportTag") or "").strip()
    }
    tags_b = {
        str(payload.get("DataExportTag") or "").strip()
        for _qid, payload in _iter_question_payloads(qsf_b)
        if str(payload.get("DataExportTag") or "").strip()
    }

    block_qids_a = _block_qids_by_id(qsf_a)
    block_qids_b = _block_qids_by_id(qsf_b)

    flow_types_a, flow_qids_a, warnings_a = _collect_flow_signature(qsf_a, block_qids_a)
    flow_types_b, flow_qids_b, warnings_b = _collect_flow_signature(qsf_b, block_qids_b)

    blocks_a = _collect_block_memberships(block_qids_a)
    blocks_b = _collect_block_memberships(block_qids_b)

    warnings = warnings_a + warnings_b

    return ParityReport(
        qids_only_in_a=sorted(qids_a - qids_b),
        qids_only_in_b=sorted(qids_b - qids_a),
        tags_only_in_a=sorted(tags_a - tags_b),
        tags_only_in_b=sorted(tags_b - tags_a),
        flow_types_a=list(flow_types_a),
        flow_types_b=list(flow_types_b),
        flow_qids_a=list(flow_qids_a),
        flow_qids_b=list(flow_qids_b),
        block_memberships_only_in_a=_counter_diff(blocks_a, blocks_b),
        block_memberships_only_in_b=_counter_diff(blocks_b, blocks_a),
        warnings=warnings,
    )
