"""Shared SurveyFlow traversal helpers for export and translation checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class FlowTraversalHandlers:
    on_block: Callable[[dict, int], None] | None = None
    on_group: Callable[[dict, int], None] | None = None
    on_embedded_data: Callable[[dict, int], None] | None = None
    on_web_service: Callable[[dict, int], None] | None = None
    on_randomizer: Callable[[dict, int], None] | None = None
    on_branch_decision: Callable[[dict, bool, str, int], None] | None = None
    on_branch_open: Callable[[dict, int], None] | None = None
    on_branch_then: Callable[[dict, int], None] | None = None
    on_branch_else: Callable[[dict, int], None] | None = None
    on_branch_end: Callable[[dict, int], None] | None = None
    on_end_survey: Callable[[dict, int], None] | None = None
    on_unknown: Callable[[dict, int], None] | None = None


def eval_boolean_expression(
    logic: object, edf_overrides: dict[str, str] | None
) -> bool | None:
    """Best-effort evaluator for Qualtrics BranchLogic on EmbeddedField expressions.

    Conservative: returns None when unsure; may return True/False when provably decidable.
    """

    if not edf_overrides:
        return None
    if not isinstance(logic, dict):
        return None
    if (logic.get("Type") or "") != "BooleanExpression":
        return None
    if_block = logic.get("0")
    if not isinstance(if_block, dict):
        return None
    if (if_block.get("Type") or "") != "If":
        return None

    exprs: list[dict] = []
    for k, v in if_block.items():
        if (
            str(k).isdigit()
            and isinstance(v, dict)
            and (v.get("Type") or "") == "Expression"
        ):
            exprs.append(v)
    if not exprs:
        return None

    results: list[bool | None] = []
    conj: str | None = None
    for e in exprs:
        conj = conj or (e.get("Conjuction") or e.get("Conjunction") or None)
        results.append(_eval_expression(e, edf_overrides))

    conj_norm = (str(conj or "And")).strip().lower()
    is_and = conj_norm != "or"

    if is_and and any(r is False for r in results):
        return False
    if (not is_and) and any(r is True for r in results):
        return True

    if all(r is not None for r in results):
        if is_and:
            return all(bool(r) for r in results)
        return any(bool(r) for r in results)

    return None


def _eval_expression(expr: dict, edf_overrides: dict[str, str]) -> bool | None:
    if (expr.get("LogicType") or "") != "EmbeddedField":
        return None
    left = (expr.get("LeftOperand") or "").strip()
    op = (expr.get("Operator") or "").strip()
    right = expr.get("RightOperand") or ""
    right_s = str(right).strip()
    if not left or not op:
        return None

    val = _lookup_edf_value(edf_overrides, left)
    if val is None:
        return None

    op_l = op.lower()
    if op_l == "equalto":
        return val == right_s
    if op_l == "notequalto":
        return val != right_s
    if op_l == "contains":
        return right_s in val
    if op_l == "doesnotcontain":
        return right_s not in val
    return None


def eval_boolean_expression_with_unasked_selected_false(
    logic: object, edf_overrides: dict[str, str], asked_qids: set[str]
) -> bool | None:
    """Evaluate BooleanExpression, treating unasked Selected checks as False."""

    if not edf_overrides:
        return None
    if not isinstance(logic, dict):
        return None
    if (logic.get("Type") or "") != "BooleanExpression":
        return None
    if_block = logic.get("0")
    if not isinstance(if_block, dict):
        return None
    if (if_block.get("Type") or "") != "If":
        return None

    exprs: list[dict] = []
    conj: str | None = None
    for k, v in if_block.items():
        if not (str(k).isdigit() and isinstance(v, dict)):
            continue
        if (v.get("Type") or "") != "Expression":
            continue
        exprs.append(v)
        conj = conj or (v.get("Conjuction") or v.get("Conjunction") or None)
    if not exprs:
        return None

    results: list[bool | None] = []
    for e in exprs:
        results.append(
            _eval_expression_with_unasked_selected_false(e, edf_overrides, asked_qids)
        )

    conj_norm = (str(conj or "And")).strip().lower()
    is_and = conj_norm != "or"

    if is_and and any(r is False for r in results):
        return False
    if (not is_and) and any(r is True for r in results):
        return True

    if all(r is not None for r in results):
        if is_and:
            return all(bool(r) for r in results)
        return any(bool(r) for r in results)
    return None


def _eval_expression_with_unasked_selected_false(
    expr: dict, edf_overrides: dict[str, str], asked_qids: set[str]
) -> bool | None:
    logic_type = (expr.get("LogicType") or "").strip()
    op = (expr.get("Operator") or "").strip()

    if logic_type == "EmbeddedField":
        return _eval_expression(expr, edf_overrides)

    if logic_type == "Question" and op == "Selected":
        qid = (
            expr.get("QuestionID") or expr.get("QuestionIDFromLocator") or ""
        ).strip()
        if qid and qid not in asked_qids:
            return False
        return None

    return None


def _lookup_edf_value(edf_overrides: dict[str, str], key: str) -> str | None:
    if key in edf_overrides:
        return str(edf_overrides[key])
    return None


def walk_flow(
    *,
    flow_list: list,
    handlers: FlowTraversalHandlers,
    edf_overrides: dict[str, str] | None = None,
    asked_qids: set[str] | None = None,
    depth: int = 0,
    eval_branch: Callable[[object, dict[str, str] | None], bool | None]
    | None = None,
    eval_branch_with_asked: Callable[[object, dict[str, str], set[str]], bool | None]
    | None = None,
) -> None:
    """Traverse SurveyFlow with optional EDF pruning and handler callbacks."""

    eval_branch = eval_branch or eval_boolean_expression
    eval_branch_with_asked = eval_branch_with_asked or (
        eval_boolean_expression_with_unasked_selected_false
    )

    for node in flow_list:
        if not isinstance(node, dict):
            continue
        node_type = str(node.get("Type") or "").strip()

        if node_type in {"Block", "Standard"} and node.get("ID"):
            if handlers.on_block:
                handlers.on_block(node, depth)
            continue

        if node_type == "Group":
            if handlers.on_group:
                handlers.on_group(node, depth)
            sub = node.get("Flow")
            if isinstance(sub, list):
                walk_flow(
                    flow_list=sub,
                    handlers=handlers,
                    edf_overrides=edf_overrides,
                    asked_qids=asked_qids,
                    depth=depth + 1,
                    eval_branch=eval_branch,
                    eval_branch_with_asked=eval_branch_with_asked,
                )
            continue

        if node_type == "EmbeddedData":
            if handlers.on_embedded_data:
                handlers.on_embedded_data(node, depth)
            continue

        if node_type == "WebService":
            if handlers.on_web_service:
                handlers.on_web_service(node, depth)
            continue

        if node_type == "BlockRandomizer":
            if handlers.on_randomizer:
                handlers.on_randomizer(node, depth)
            sub = node.get("Flow")
            if isinstance(sub, list):
                walk_flow(
                    flow_list=sub,
                    handlers=handlers,
                    edf_overrides=edf_overrides,
                    asked_qids=asked_qids,
                    depth=depth + 1,
                    eval_branch=eval_branch,
                    eval_branch_with_asked=eval_branch_with_asked,
                )
            continue

        if node_type == "Branch":
            then_flow = node.get("Flow")
            else_flow = node.get("ElseFlow")
            if isinstance(node.get("Then"), list):
                then_flow = node.get("Then")
            if isinstance(node.get("Else"), list):
                else_flow = node.get("Else")

            branch_eval = (
                eval_branch(node.get("BranchLogic"), edf_overrides)
                if edf_overrides
                else None
            )

            if edf_overrides and branch_eval is not None:
                if handlers.on_branch_decision:
                    handlers.on_branch_decision(
                        node,
                        bool(branch_eval),
                        "edf",
                        depth,
                    )
                chosen = then_flow if branch_eval is True else else_flow
                if isinstance(chosen, list) and chosen:
                    walk_flow(
                        flow_list=chosen,
                        handlers=handlers,
                        edf_overrides=edf_overrides,
                        asked_qids=asked_qids,
                        depth=depth,
                        eval_branch=eval_branch,
                        eval_branch_with_asked=eval_branch_with_asked,
                    )
                continue

            if edf_overrides and branch_eval is None and asked_qids is not None:
                branch_eval2 = eval_branch_with_asked(
                    node.get("BranchLogic"), edf_overrides, asked_qids
                )
                if branch_eval2 is not None:
                    if handlers.on_branch_decision:
                        handlers.on_branch_decision(
                            node,
                            bool(branch_eval2),
                            "unasked_selected",
                            depth,
                        )
                    chosen = then_flow if branch_eval2 is True else else_flow
                    if isinstance(chosen, list) and chosen:
                        walk_flow(
                            flow_list=chosen,
                            handlers=handlers,
                            edf_overrides=edf_overrides,
                            asked_qids=asked_qids,
                            depth=depth,
                            eval_branch=eval_branch,
                            eval_branch_with_asked=eval_branch_with_asked,
                        )
                    continue

            if handlers.on_branch_open:
                handlers.on_branch_open(node, depth)

            if isinstance(then_flow, list) and then_flow:
                if handlers.on_branch_then:
                    handlers.on_branch_then(node, depth)
                asked_then = set(asked_qids) if asked_qids is not None else None
                walk_flow(
                    flow_list=then_flow,
                    handlers=handlers,
                    edf_overrides=edf_overrides,
                    asked_qids=asked_then,
                    depth=depth + 1,
                    eval_branch=eval_branch,
                    eval_branch_with_asked=eval_branch_with_asked,
                )
            else:
                asked_then = None

            if isinstance(else_flow, list) and else_flow:
                if handlers.on_branch_else:
                    handlers.on_branch_else(node, depth)
                asked_else = set(asked_qids) if asked_qids is not None else None
                walk_flow(
                    flow_list=else_flow,
                    handlers=handlers,
                    edf_overrides=edf_overrides,
                    asked_qids=asked_else,
                    depth=depth + 1,
                    eval_branch=eval_branch,
                    eval_branch_with_asked=eval_branch_with_asked,
                )
            else:
                asked_else = None

            if asked_qids is not None:
                if asked_then is not None:
                    asked_qids.update(asked_then)
                if asked_else is not None:
                    asked_qids.update(asked_else)

            if handlers.on_branch_end:
                handlers.on_branch_end(node, depth)
            continue

        if node_type == "EndSurvey":
            if handlers.on_end_survey:
                handlers.on_end_survey(node, depth)
            continue

        if node_type:
            if handlers.on_unknown:
                handlers.on_unknown(node, depth)
            sub = node.get("Flow")
            if isinstance(sub, list):
                walk_flow(
                    flow_list=sub,
                    handlers=handlers,
                    edf_overrides=edf_overrides,
                    asked_qids=asked_qids,
                    depth=depth + 1,
                    eval_branch=eval_branch,
                    eval_branch_with_asked=eval_branch_with_asked,
                )


def flow_order_map(payload: dict) -> dict[str, int]:
    """Return a QID->index mapping based on SurveyFlow order."""
    result = payload.get("result", {})
    questions = result.get("Questions", {}) or {}
    blocks = result.get("Blocks", {}) or {}
    flow = result.get("SurveyFlow", {}) or {}

    ordered_block_ids: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("Type") in {"Standard", "Block"} and "ID" in node:
                bid = str(node["ID"])
                if bid not in ordered_block_ids:
                    ordered_block_ids.append(bid)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                if isinstance(value, (dict, list)):
                    walk(value)

    walk(flow.get("Flow", []))

    ordered_qids: list[str] = []
    seen = set()
    for block_id in ordered_block_ids:
        block = blocks.get(block_id) or {}
        for elem in block.get("BlockElements", []) or []:
            if (elem.get("Type") or "") != "Question":
                continue
            qid = elem.get("QuestionID")
            if not qid or qid not in questions:
                continue
            if qid in seen:
                continue
            seen.add(qid)
            ordered_qids.append(str(qid))

    for qid in questions.keys():
        qid_str = str(qid)
        if qid_str in seen:
            continue
        seen.add(qid_str)
        ordered_qids.append(qid_str)

    return {qid: idx for idx, qid in enumerate(ordered_qids)}


def scenario_qid_order(payload: dict, edf_overrides: dict[str, str]) -> list[str]:
    """Return QIDs reachable under EDF pruning, in flow order.

    Uses flow traversal + DisplayLogic visibility with the same unasked-selected
    heuristic as export traversal.
    """
    result = payload.get("result", {}) or {}
    questions = result.get("Questions", {}) or {}
    blocks = result.get("Blocks", {}) or {}
    flow = result.get("SurveyFlow", {}) or {}
    flow_list = flow.get("Flow") or []

    if not isinstance(flow_list, list):
        return list(flow_order_map(payload).keys())

    ordered: list[str] = []
    seen: set[str] = set()
    asked_qids: set[str] | None = set() if edf_overrides else None

    def add_qid(qid: str) -> None:
        if qid in seen:
            return
        seen.add(qid)
        ordered.append(qid)

    def _question_visible(question: dict, asked: set[str]) -> bool | None:
        display_logic = question.get("DisplayLogic")
        if not display_logic:
            return True
        if not isinstance(display_logic, dict):
            return None
        return eval_boolean_expression_with_unasked_selected_false(
            display_logic, edf_overrides, asked
        )

    def on_block(node: dict, _depth: int) -> None:
        block_id = str(node.get("ID") or "").strip()
        if not block_id:
            return
        block = blocks.get(block_id) or {}
        if (block.get("Type") or "").strip() == "Trash":
            return
        elements = block.get("BlockElements", []) or []
        if not isinstance(elements, list):
            return

        if asked_qids is None:
            for elem in elements:
                if not isinstance(elem, dict):
                    continue
                if (elem.get("Type") or "") != "Question":
                    continue
                qid = elem.get("QuestionID")
                if qid and qid in questions:
                    add_qid(str(qid))
            return

        asked_sim = set(asked_qids)
        for elem in elements:
            if not isinstance(elem, dict):
                continue
            if (elem.get("Type") or "") != "Question":
                continue
            qid = elem.get("QuestionID")
            if not qid or qid not in questions:
                continue
            visible = _question_visible(questions.get(qid) or {}, asked_sim)
            if visible is False:
                continue
            add_qid(str(qid))
            asked_sim.add(str(qid))
        asked_qids.update(asked_sim)

    handlers = FlowTraversalHandlers(on_block=on_block)
    walk_flow(
        flow_list=flow_list,
        handlers=handlers,
        edf_overrides=edf_overrides,
        asked_qids=asked_qids,
    )

    if not ordered:
        return list(flow_order_map(payload).keys())
    return ordered
