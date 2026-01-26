"""Compare two Qualtrics surveys using cached definitions and per-tag diffs."""

from __future__ import annotations

import json
import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .excel_io import EXTERNALLY_MANAGED_TAGS
from .push_logger import log_push_event
from .qualtrics_client import (
    SurveyCache,
    load_cached_survey,
    refresh_survey_cache,
)

# ---------- Data models ----------


@dataclass
class CompareInputs:
    """Inputs for `compare`, including survey IDs and filtering options."""

    source_id: str
    target_id: str
    refresh: bool = True
    include_tags: Optional[Set[str]] = None
    exclude_tags: Optional[Set[str]] = None
    json_output: Optional[Path] = None
    with_diffs: bool = False


@dataclass
class QuestionDiff:
    """Comparison status for a single question/tag between two surveys."""

    tag: str
    source_qid: str
    target_qid: str
    status: str  # match | mismatch | skipped_externally_managed
    mismatches: List[str]
    details: Optional[List[dict]] = None


@dataclass
class CompareResult:
    """Structured result returned by `compare`."""

    source_changed: bool
    target_changed: bool
    question_diffs: List[QuestionDiff]
    missing_in_target: List[str]
    missing_in_source: List[str]
    metadata_diffs: List[str]

    def has_blocking(self) -> bool:
        """Return True if the result contains mismatches or missing items."""

        return bool(
            self.metadata_diffs
            or self.missing_in_target
            or self.missing_in_source
            or [d for d in self.question_diffs if d.status != "match"]
        )


# ---------- Helpers ----------


def _norm(s: str | None) -> str:
    if s is None:
        return ""
    return " ".join(str(s).split())


def _norm_js(s: str | None) -> str:
    if s is None:
        return ""
    lines = [ln.strip() for ln in str(s).splitlines() if ln.strip()]
    return "\n".join(lines)


def _udiff(a: str, b: str, *, n: int = 2) -> List[str]:
    return list(
        difflib.unified_diff(
            (a or "").splitlines(),
            (b or "").splitlines(),
            lineterm="",
            n=n,
            fromfile="source",
            tofile="target",
        )
    )


def _choice_signature(question: dict) -> List[Tuple[str, str]]:
    choices = question.get("Choices") or {}
    ordered_ids = sorted(
        choices.keys(), key=lambda x: int(x) if str(x).isdigit() else str(x)
    )
    return [
        (cid, _norm((choices.get(cid) or {}).get("Display"))) for cid in ordered_ids
    ]


def _question_key(tag: str | None, qid: str) -> str:
    tag = (tag or "").strip()
    return tag.lower() if tag else qid


def _collect_block_ids_from_flow(flow_obj: dict) -> Set[str]:
    """Recursively collect BlockIDs referenced in the Flow (excluding Trash)."""

    ids: Set[str] = set()

    def walk(node: dict | list | None):
        if not node:
            return
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        # node is dict
        node_type = node.get("Type")
        if node_type == "Block" and node.get("ID"):
            ids.add(node["ID"])
        # Branch/Loop/Group contain nested "Flow" arrays
        if "Flow" in node and isinstance(node["Flow"], list):
            walk(node["Flow"])
        # Branch can also have Then/Else
        if "Then" in node:
            walk(node.get("Then"))
        if "Else" in node:
            walk(node.get("Else"))

    if flow_obj and isinstance(flow_obj, dict):
        walk(flow_obj.get("Flow"))
    return ids


def _active_qids(cache: SurveyCache) -> Set[str]:
    """Return QIDs that are placed in non-Trash blocks referenced by Flow."""

    result: Set[str] = set()
    blocks = cache.blocks or {}

    # Identify trash block IDs to exclude
    trash_blocks = {
        bid for bid, b in blocks.items() if (b.get("Type") or "").lower() == "trash"
    }

    result_payload = cache.payload.get("result", {}) or {}
    flow_blocks = _collect_block_ids_from_flow(
        result_payload.get("SurveyFlow") or result_payload.get("Flow") or {}
    )
    for bid in flow_blocks:
        if bid in trash_blocks:
            continue
        block = blocks.get(bid) or {}
        elements = block.get("BlockElements") or []
        for elem in elements:
            qid = elem.get("QuestionID")
            if qid:
                result.add(qid)
    # Fallback: if flow produced nothing, allow all non-trash questions
    if not result:
        for bid, block in blocks.items():
            if bid in trash_blocks:
                continue
            for elem in block.get("BlockElements") or []:
                qid = elem.get("QuestionID")
                if qid:
                    result.add(qid)
    return result


def _load_surveys(inputs: CompareInputs) -> Tuple[SurveyCache, bool, SurveyCache, bool]:
    if inputs.refresh:
        src_cache, src_changed = refresh_survey_cache(inputs.source_id)
        tgt_cache, tgt_changed = refresh_survey_cache(inputs.target_id)
    else:
        src_cache, src_changed = load_cached_survey(inputs.source_id), False
        tgt_cache, tgt_changed = load_cached_survey(inputs.target_id), False
    return src_cache, src_changed, tgt_cache, tgt_changed


# ---------- Comparison ----------


def _build_index(
    cache: SurveyCache,
    include_tags: Optional[Set[str]],
    exclude_tags: Optional[Set[str]],
    active_qids: Set[str],
) -> Dict[str, Tuple[str, dict]]:
    idx: Dict[str, Tuple[str, dict]] = {}
    for qid, q in cache.questions.items():
        if active_qids and qid not in active_qids:
            continue
        tag = (q.get("DataExportTag") or "").strip()
        key = _question_key(tag, qid)
        if (
            include_tags
            and tag
            and tag.lower() not in {t.lower() for t in include_tags}
        ):
            continue
        if exclude_tags and tag and tag.lower() in {t.lower() for t in exclude_tags}:
            continue
        idx[key] = (qid, q)
    return idx


def _compare_questions(
    src_cache: SurveyCache, tgt_cache: SurveyCache, inputs: CompareInputs
) -> Tuple[List[QuestionDiff], List[str], List[str]]:
    src_active = _active_qids(src_cache)
    tgt_active = _active_qids(tgt_cache)

    src_idx = _build_index(
        src_cache, inputs.include_tags, inputs.exclude_tags, src_active
    )
    tgt_idx = _build_index(
        tgt_cache, inputs.include_tags, inputs.exclude_tags, tgt_active
    )

    keys_shared = set(src_idx.keys()) & set(tgt_idx.keys())
    missing_in_tgt = sorted(set(src_idx.keys()) - set(tgt_idx.keys()))
    missing_in_src = sorted(set(tgt_idx.keys()) - set(src_idx.keys()))

    diffs: List[QuestionDiff] = []

    managed_tags = {t.lower() for t in EXTERNALLY_MANAGED_TAGS.keys()}

    for key in sorted(keys_shared):
        src_qid, sq = src_idx[key]
        tgt_qid, tq = tgt_idx[key]
        tag = (sq.get("DataExportTag") or tq.get("DataExportTag") or "").strip()

        if tag and tag.lower() in managed_tags:
            diffs.append(
                QuestionDiff(
                    tag=tag or key,
                    source_qid=src_qid,
                    target_qid=tgt_qid,
                    status="skipped_externally_managed",
                    mismatches=[],
                )
            )
            continue

        mismatches: List[str] = []

        details: List[dict] = []

        if _norm(sq.get("QuestionText")) != _norm(tq.get("QuestionText")):
            mismatches.append("QuestionText")
            if inputs.with_diffs:
                details.append(
                    {
                        "field": "QuestionText",
                        "source": sq.get("QuestionText") or "",
                        "target": tq.get("QuestionText") or "",
                        "diff": _udiff(
                            sq.get("QuestionText") or "", tq.get("QuestionText") or ""
                        ),
                    }
                )

        if _choice_signature(sq) != _choice_signature(tq):
            mismatches.append("Choices")
            if inputs.with_diffs:
                src_choices = {
                    cid: (sq.get("Choices") or {}).get(cid, {}).get("Display")
                    for cid, _ in _choice_signature(sq)
                }
                tgt_choices = {
                    cid: (tq.get("Choices") or {}).get(cid, {}).get("Display")
                    for cid, _ in _choice_signature(tq)
                }
                details.append(
                    {
                        "field": "Choices",
                        "source": src_choices,
                        "target": tgt_choices,
                        "diff": _udiff(
                            json.dumps(src_choices, indent=2, sort_keys=True),
                            json.dumps(tgt_choices, indent=2, sort_keys=True),
                        ),
                    }
                )

        if _norm_js(sq.get("QuestionJS")) != _norm_js(tq.get("QuestionJS")):
            mismatches.append("QuestionJS")
            if inputs.with_diffs:
                details.append(
                    {
                        "field": "QuestionJS",
                        "source": sq.get("QuestionJS") or "",
                        "target": tq.get("QuestionJS") or "",
                        "diff": _udiff(
                            sq.get("QuestionJS") or "", tq.get("QuestionJS") or "", n=3
                        ),
                    }
                )

        # Basic structure fields
        for field in [
            "QuestionType",
            "Selector",
            "SubSelector",
            "Validation",
            "Configuration",
        ]:
            if (sq.get(field) or {}) != (tq.get(field) or {}):
                mismatches.append(field)
                if inputs.with_diffs:
                    details.append(
                        {
                            "field": field,
                            "source": sq.get(field),
                            "target": tq.get(field),
                            "diff": _udiff(
                                json.dumps(sq.get(field), indent=2, sort_keys=True),
                                json.dumps(tq.get(field), indent=2, sort_keys=True),
                                n=1,
                            ),
                        }
                    )

        status = "match" if not mismatches else "mismatch"
        diffs.append(
            QuestionDiff(
                tag=tag or key,
                source_qid=src_qid,
                target_qid=tgt_qid,
                status=status,
                mismatches=mismatches,
                details=details or None,
            )
        )

    return diffs, missing_in_tgt, missing_in_src


def _compare_metadata(src_cache: SurveyCache, tgt_cache: SurveyCache) -> List[str]:
    sres = src_cache.payload.get("result", {})
    tres = tgt_cache.payload.get("result", {})

    diffs: List[str] = []

    def cmp(path: str, a, b):
        if a != b:
            diffs.append(f"{path} differs (source={a!r}, target={b!r})")

    so, to = sres.get("SurveyOptions", {}), tres.get("SurveyOptions", {})
    for k in [
        "ProgressBarDisplay",
        "Header",
        "Footer",
        "Skin",
        "SkinLibrary",
        "SkinType",
        "CollectGeoLocation",
        "AnonymizeResponse",
    ]:
        cmp(f"SurveyOptions.{k}", so.get(k), to.get(k))

    # Basic flow/block order diff
    s_blocks = sres.get("Blocks", {}) or {}
    t_blocks = tres.get("Blocks", {}) or {}
    cmp("Blocks.count", len(s_blocks), len(t_blocks))

    s_flow = sres.get("SurveyFlow") or sres.get("Flow") or {}
    t_flow = tres.get("SurveyFlow") or tres.get("Flow") or {}
    if _norm(json.dumps(s_flow, sort_keys=True)) != _norm(
        json.dumps(t_flow, sort_keys=True)
    ):
        diffs.append("SurveyFlow structure differs")

    return diffs


def compare(inputs: CompareInputs) -> CompareResult:
    """Compare two surveys and return a structured diff summary."""

    src_cache, src_changed, tgt_cache, tgt_changed = _load_surveys(inputs)

    q_diffs, missing_tgt, missing_src = _compare_questions(src_cache, tgt_cache, inputs)

    # Sort question diffs: mismatches first, then matches, then skipped
    def _prio(d: QuestionDiff) -> Tuple[int, str]:
        if d.status == "mismatch":
            return (0, d.tag)
        if d.status == "match":
            return (1, d.tag)
        return (2, d.tag)

    q_diffs = sorted(q_diffs, key=_prio)
    meta_diffs = _compare_metadata(src_cache, tgt_cache)

    result = CompareResult(
        source_changed=src_changed,
        target_changed=tgt_changed,
        question_diffs=q_diffs,
        missing_in_target=missing_tgt,
        missing_in_source=missing_src,
        metadata_diffs=meta_diffs,
    )

    log_push_event(
        action="qsync.compare",
        method="LOCAL",
        path="-",
        survey_id=inputs.target_id,
        status=0,
        meta={
            "source": inputs.source_id,
            "target": inputs.target_id,
            "source_changed": src_changed,
            "target_changed": tgt_changed,
            "question_mismatches": len([d for d in q_diffs if d.status == "mismatch"]),
            "missing_in_target": len(missing_tgt),
            "missing_in_source": len(missing_src),
            "metadata_diffs": len(meta_diffs),
        },
    )

    return result


# ---------- Rendering ----------


def render_report(result: CompareResult) -> str:
    """Render a human-readable text report from a `CompareResult`."""

    lines: List[str] = []
    lines.append("=== Comparison Summary ===")
    lines.append(
        f"Question mismatches: {len([d for d in result.question_diffs if d.status == 'mismatch'])}"
    )
    lines.append(
        f"Skipped (externally managed): {len([d for d in result.question_diffs if d.status == 'skipped_externally_managed'])}"
    )
    lines.append(f"Missing in target: {len(result.missing_in_target)}")
    lines.append(f"Missing in source: {len(result.missing_in_source)}")
    lines.append(f"Metadata diffs: {len(result.metadata_diffs)}")
    lines.append("")

    if result.metadata_diffs:
        lines.append("-- Metadata diffs --")
        lines.extend(f"* {m}" for m in result.metadata_diffs)
        lines.append("")

    mismatches = [d for d in result.question_diffs if d.status == "mismatch"]
    if mismatches:
        lines.append("-- Question mismatches --")
        for d in mismatches:
            lines.append(
                f"* {d.tag} (src {d.source_qid} → tgt {d.target_qid}): {', '.join(d.mismatches)}"
            )
        lines.append("")

    if result.missing_in_target:
        lines.append("-- Present in source only --")
        lines.append(", ".join(result.missing_in_target))
        lines.append("")

    if result.missing_in_source:
        lines.append("-- Present in target only --")
        lines.append(", ".join(result.missing_in_source))
        lines.append("")

    return "\n".join(lines)


def to_jsonable(result: CompareResult) -> dict:
    """Convert a `CompareResult` into a JSON-serializable plain dict."""

    return {
        "source_changed": result.source_changed,
        "target_changed": result.target_changed,
        "question_diffs": [
            {
                "tag": d.tag,
                "source_qid": d.source_qid,
                "target_qid": d.target_qid,
                "status": d.status,
                "mismatches": d.mismatches,
                "details": d.details,
            }
            for d in result.question_diffs
        ],
        "missing_in_target": result.missing_in_target,
        "missing_in_source": result.missing_in_source,
        "metadata_diffs": result.metadata_diffs,
    }
