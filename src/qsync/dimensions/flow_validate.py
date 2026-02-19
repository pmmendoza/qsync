"""Flow validation for survey flow synchronization.

This module validates flow structures before push to ensure:
1. Structural validity - correct node types, required fields
2. Reference validity - block IDs exist, QIDs in branch logic exist
3. Semantic validity - no orphan nodes, valid conditions
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional


class FlowValidationError(Exception):
    """Raised when flow validation fails."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


@dataclass
class ValidationContext:
    """Context for flow validation with survey data."""

    survey_id: str
    blocks: dict[str, dict]
    questions: dict[str, dict]
    flow_qids: set[str]


def validate_flow(
    flow: dict,
    survey_id: str,
    blocks: Optional[dict] = None,
    questions: Optional[dict] = None,
) -> None:
    """Validate flow structure and references.

    Args:
        flow: SurveyFlow dict to validate
        survey_id: Survey ID for error messages
        blocks: Optional blocks dict for reference validation
        questions: Optional questions dict for reference validation

    Raises:
        FlowValidationError: If validation fails, contains list of all errors
    """
    errors: list[str] = []

    # Validate root structure
    if not isinstance(flow, dict):
        errors.append("Flow must be a dictionary")
        raise FlowValidationError(errors)

    flow_list = flow.get("Flow", [])
    if not isinstance(flow_list, list):
        errors.append("Flow.Flow must be a list")
        raise FlowValidationError(errors)

    blocks_map: dict[str, dict] = blocks or {}
    questions_map: dict[str, dict] = questions or {}

    ctx = ValidationContext(
        survey_id=survey_id,
        blocks=blocks_map,
        questions=questions_map,
        flow_qids=_collect_qids_in_flow(flow_list, blocks_map),
    )

    # Track seen IDs for duplicate detection
    seen_ids: set[str] = set()

    # Validate each node recursively
    _validate_node_list(flow_list, "flow", ctx, errors, seen_ids)

    if errors:
        raise FlowValidationError(errors)


def validate_yaml_structure(yaml_data: dict) -> None:
    """Validate YAML structure before conversion.

    This is a quick structural check before attempting YAML -> JSON conversion.

    Args:
        yaml_data: Parsed YAML data

    Raises:
        FlowValidationError: If structure is invalid
    """
    errors: list[str] = []

    if not isinstance(yaml_data, dict):
        errors.append("YAML root must be a dictionary")
        raise FlowValidationError(errors)

    version = yaml_data.get("version")
    if version is None:
        errors.append("Missing required field: version")

    flow_list = yaml_data.get("flow")
    if flow_list is None:
        errors.append("Missing required field: flow")
    elif not isinstance(flow_list, list):
        errors.append("'flow' must be a list")

    if errors:
        raise FlowValidationError(errors)

    # Validate each node in the flow list
    for i, node in enumerate(flow_list or []):
        _validate_yaml_node(node, f"flow[{i}]", errors)

    if errors:
        raise FlowValidationError(errors)


def _validate_yaml_node(node: Any, path: str, errors: list[str]) -> None:
    """Validate a single YAML node structure."""
    if not isinstance(node, dict):
        errors.append(f"{path}: Node must be a dictionary")
        return

    node_type = node.get("type")
    if not node_type:
        errors.append(f"{path}: Missing required field 'type'")
        return

    node_id = node.get("id")
    if not node_id and node_type not in ("Unknown",):
        errors.append(f"{path}: Missing required field 'id'")

    # Validate type-specific structure
    if node_type == "Block":
        pass  # Block just needs id

    elif node_type == "Branch":
        # Branch needs condition (or raw_logic) and at least then or else
        has_condition = "condition" in node or "raw_logic" in node
        if not has_condition:
            errors.append(f"{path}: Branch requires 'condition' or 'raw_logic'")

        # Validate then/else branches if present
        if "then" in node:
            if not isinstance(node["then"], list):
                errors.append(f"{path}.then: Must be a list")
            else:
                for i, child in enumerate(node["then"]):
                    _validate_yaml_node(child, f"{path}.then[{i}]", errors)

        if "else" in node:
            if not isinstance(node["else"], list):
                errors.append(f"{path}.else: Must be a list")
            else:
                for i, child in enumerate(node["else"]):
                    _validate_yaml_node(child, f"{path}.else[{i}]", errors)

    elif node_type == "EmbeddedData":
        fields = node.get("fields")
        if fields is not None and not isinstance(fields, list):
            errors.append(f"{path}.fields: Must be a list")

    elif node_type == "BlockRandomizer":
        blocks = node.get("blocks")
        if blocks is not None:
            if not isinstance(blocks, list):
                errors.append(f"{path}.blocks: Must be a list")
            else:
                for i, child in enumerate(blocks):
                    _validate_yaml_node(child, f"{path}.blocks[{i}]", errors)

    elif node_type == "Group":
        sub_flow = node.get("flow")
        if sub_flow is not None:
            if not isinstance(sub_flow, list):
                errors.append(f"{path}.flow: Must be a list")
            else:
                for i, child in enumerate(sub_flow):
                    _validate_yaml_node(child, f"{path}.flow[{i}]", errors)

    elif node_type == "EndSurvey":
        pass  # EndSurvey is flexible

    elif node_type == "WebService":
        pass  # WebService is flexible

    elif node_type == "Unknown":
        # Unknown nodes should have _raw for restoration
        if "_raw" not in node:
            errors.append(f"{path}: Unknown node should have '_raw' field")


def _validate_node_list(
    nodes: list,
    path: str,
    ctx: ValidationContext,
    errors: list[str],
    seen_ids: set[str],
) -> None:
    """Validate a list of flow nodes."""
    for i, node in enumerate(nodes):
        _validate_node(node, f"{path}[{i}]", ctx, errors, seen_ids)


def _validate_node(
    node: Any,
    path: str,
    ctx: ValidationContext,
    errors: list[str],
    seen_ids: set[str],
) -> None:
    """Validate a single flow node."""
    if not isinstance(node, dict):
        errors.append(f"{path}: Node must be a dictionary")
        return

    node_type = str(node.get("Type", "")).strip()
    if not node_type:
        errors.append(f"{path}: Missing required field 'Type'")
        return

    # Get node ID
    node_id = node.get("FlowID") or node.get("ID", "")

    # Check for duplicate IDs
    if node_id:
        if node_id in seen_ids:
            errors.append(f"{path}: Duplicate node ID '{node_id}'")
        seen_ids.add(node_id)

    # Validate by node type
    if node_type in ("Block", "Standard"):
        _validate_block_node(node, path, ctx, errors)

    elif node_type == "Branch":
        _validate_branch_node(node, path, ctx, errors, seen_ids)

    elif node_type == "EmbeddedData":
        _validate_embedded_data_node(node, path, errors)

    elif node_type == "BlockRandomizer":
        _validate_randomizer_node(node, path, ctx, errors, seen_ids)

    elif node_type == "Group":
        _validate_group_node(node, path, ctx, errors, seen_ids)

    elif node_type == "EndSurvey":
        _validate_end_survey_node(node, path, errors)

    elif node_type == "WebService":
        _validate_web_service_node(node, path, errors)

    elif node_type == "Authenticator":
        pass  # Authenticator nodes are flexible

    elif node_type == "TableOfContents":
        pass  # TableOfContents nodes are simple

    else:
        # Unknown node type - warn but don't error
        pass


def _validate_block_node(
    node: dict, path: str, ctx: ValidationContext, errors: list[str]
) -> None:
    """Validate a Block/Standard node."""
    block_id = node.get("ID", "")

    if not block_id:
        errors.append(f"{path}: Block node missing 'ID' field")
        return

    # Check if block exists (if we have block data)
    if ctx.blocks and block_id not in ctx.blocks:
        errors.append(f"{path}: Block '{block_id}' does not exist in survey")


def _validate_branch_node(
    node: dict,
    path: str,
    ctx: ValidationContext,
    errors: list[str],
    seen_ids: set[str],
) -> None:
    """Validate a Branch node."""
    # Validate branch logic
    logic = node.get("BranchLogic")
    if logic:
        _validate_branch_logic(logic, f"{path}.BranchLogic", ctx, errors)

    # Validate then branch
    then_flow = node.get("Flow", [])
    if then_flow:
        _validate_node_list(then_flow, f"{path}.then", ctx, errors, seen_ids)

    # Validate else branch
    else_flow = node.get("ElseFlow", [])
    if else_flow:
        _validate_node_list(else_flow, f"{path}.else", ctx, errors, seen_ids)


def _validate_branch_logic(
    logic: Any, path: str, ctx: ValidationContext, errors: list[str]
) -> None:
    """Validate BranchLogic structure and references."""
    if not isinstance(logic, dict):
        errors.append(f"{path}: BranchLogic must be a dictionary")
        return

    logic_type = logic.get("Type")
    if logic_type != "BooleanExpression":
        # Could be other formats, just warn
        return

    if_block = logic.get("0")
    if not isinstance(if_block, dict):
        return

    # Validate expressions
    for key, value in if_block.items():
        if not str(key).isdigit():
            continue
        if not isinstance(value, dict):
            continue

        expr_type = value.get("LogicType", "")

        if expr_type == "Question":
            qid = _extract_logic_qid(value)
            if not qid:
                errors.append(
                    f"{path}: Question condition is missing QuestionID/QuestionIDFromLocator"
                )
                continue

            if ctx.questions and qid not in ctx.questions:
                errors.append(
                    f"{path}: Question '{qid}' referenced in condition does not exist"
                )
                continue

            if ctx.flow_qids and qid not in ctx.flow_qids:
                errors.append(
                    f"{path}: Question '{qid}' referenced in condition is not in SurveyFlow"
                )
                continue

            q = ctx.questions.get(qid) or {}
            _validate_question_choice_reference(
                expr=value,
                qid=qid,
                question=q,
                path=path,
                errors=errors,
            )


def _validate_embedded_data_node(node: dict, path: str, errors: list[str]) -> None:
    """Validate an EmbeddedData node."""
    embedded_data = node.get("EmbeddedData", [])

    if not isinstance(embedded_data, list):
        errors.append(f"{path}.EmbeddedData: Must be a list")
        return

    for i, item in enumerate(embedded_data):
        if not isinstance(item, dict):
            errors.append(f"{path}.EmbeddedData[{i}]: Must be a dictionary")
            continue

        field = item.get("Field")
        if not field:
            errors.append(f"{path}.EmbeddedData[{i}]: Missing 'Field' name")


def _validate_randomizer_node(
    node: dict,
    path: str,
    ctx: ValidationContext,
    errors: list[str],
    seen_ids: set[str],
) -> None:
    """Validate a BlockRandomizer node."""
    sub_flow = node.get("Flow", [])

    if not isinstance(sub_flow, list):
        errors.append(f"{path}.Flow: Must be a list")
        return

    # Validate child nodes
    _validate_node_list(sub_flow, f"{path}.blocks", ctx, errors, seen_ids)

    # Validate SubSet count
    subset = node.get("SubSet")
    if subset is not None:
        if not isinstance(subset, int) or subset < 0:
            errors.append(f"{path}.SubSet: Must be a non-negative integer")
        elif subset > len(sub_flow):
            errors.append(
                f"{path}.SubSet: Cannot be greater than number of blocks ({len(sub_flow)})"
            )


def _validate_group_node(
    node: dict,
    path: str,
    ctx: ValidationContext,
    errors: list[str],
    seen_ids: set[str],
) -> None:
    """Validate a Group node."""
    sub_flow = node.get("Flow", [])

    if not isinstance(sub_flow, list):
        errors.append(f"{path}.Flow: Must be a list")
        return

    _validate_node_list(sub_flow, f"{path}.flow", ctx, errors, seen_ids)


def _validate_end_survey_node(node: dict, path: str, errors: list[str]) -> None:
    """Validate an EndSurvey node."""
    options = node.get("Options", {})

    if not isinstance(options, dict):
        errors.append(f"{path}.Options: Must be a dictionary")
        return

    # Validate termination type if present
    term_type = options.get("SurveyTermination")
    valid_term_types = {
        "Default",
        "DisplayMessage",
        "Redirect",
        "DefaultMessage",
        "ScreenOutMessage",
        "CustomMessage",
    }
    if term_type and term_type not in valid_term_types:
        # Don't error, Qualtrics may have more types
        pass


def _validate_web_service_node(node: dict, path: str, errors: list[str]) -> None:
    """Validate a WebService node."""
    url = node.get("URL")
    if url is not None and not isinstance(url, str):
        errors.append(f"{path}.URL: Must be a string")

    method = node.get("Method")
    if method is not None and method not in ("GET", "POST", "PUT", "DELETE", "PATCH"):
        # Don't error, just warn
        pass


def _collect_qids_in_flow(flow_list: list, blocks: dict[str, dict]) -> set[str]:
    """Collect QIDs from non-trash blocks that appear in SurveyFlow."""

    block_ids: set[str] = set()
    qids: set[str] = set()

    def walk(nodes: Any) -> None:
        if isinstance(nodes, list):
            for node in nodes:
                walk(node)
            return
        if not isinstance(nodes, dict):
            return

        node_type = str(nodes.get("Type") or "").strip()
        if node_type in {"Block", "Standard"}:
            block_id = str(nodes.get("ID") or "").strip()
            if block_id:
                block_ids.add(block_id)

        for key in ("Flow", "Then", "Else", "ElseFlow"):
            child = nodes.get(key)
            if isinstance(child, (list, dict)):
                walk(child)

    walk(flow_list)

    for block_id in block_ids:
        block = blocks.get(block_id) or {}
        if str(block.get("Type") or "").strip().lower() == "trash":
            continue
        elements = block.get("BlockElements") or block.get("Elements") or []
        if not isinstance(elements, list):
            continue
        for elem in elements:
            if not isinstance(elem, dict):
                continue
            elem_type = str(elem.get("Type") or "").strip()
            if elem_type not in {"", "Question"}:
                continue
            qid = str(elem.get("QuestionID") or "").strip()
            if qid:
                qids.add(qid)

    return qids


def _extract_logic_qid(expr: dict) -> str:
    return str(
        expr.get("QuestionID") or expr.get("QuestionIDFromLocator") or ""
    ).strip()


def _parse_qid_from_choice_locator(locator: str) -> str:
    text = str(locator or "").strip()
    if not text:
        return ""
    match = re.search(r"q://([^/]+)/", text, flags=re.IGNORECASE)
    if not match:
        return ""
    return str(match.group(1) or "").strip()


def _parse_choice_id_from_locator(locator: str) -> str:
    text = str(locator or "").strip()
    if not text:
        return ""
    patterns = (
        r"/SelectableChoice/([^/]+)\s*$",
        r"/Choice/([^/]+)\s*$",
        r"/Answer/([^/]+)\s*$",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return str(match.group(1) or "").strip()
    return ""


def _expression_choice_id(expr: dict) -> str:
    locator = str(expr.get("ChoiceLocator") or "").strip()
    if locator:
        from_locator = _parse_choice_id_from_locator(locator)
        if from_locator:
            return from_locator

    right_operand = str(expr.get("RightOperand") or "").strip()
    if right_operand and "/" not in right_operand:
        return right_operand
    return ""


def _question_category_ids(question: dict) -> set[str]:
    ids: set[str] = set()
    for key in ("Choices", "Answers"):
        container = question.get(key) or {}
        if not isinstance(container, dict):
            continue
        for item_id in container.keys():
            item = str(item_id or "").strip()
            if item:
                ids.add(item)
    return ids


def _validate_question_choice_reference(
    *,
    expr: dict,
    qid: str,
    question: dict,
    path: str,
    errors: list[str],
) -> None:
    operator = str(expr.get("Operator") or "").strip()
    locator = str(expr.get("ChoiceLocator") or "").strip()
    choice_id = _expression_choice_id(expr)

    if locator:
        locator_qid = _parse_qid_from_choice_locator(locator)
        if locator_qid and locator_qid != qid:
            errors.append(
                f"{path}: ChoiceLocator question '{locator_qid}' does not match QuestionID '{qid}'"
            )

    if operator not in {"Selected", "NotSelected"} and not locator:
        return
    if not choice_id:
        return

    categories = _question_category_ids(question)
    if categories and choice_id not in categories:
        errors.append(
            f"{path}: Question '{qid}' condition references missing category '{choice_id}'"
        )
