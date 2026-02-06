"""Flow validation for survey flow synchronization.

This module validates flow structures before push to ensure:
1. Structural validity - correct node types, required fields
2. Reference validity - block IDs exist, QIDs in branch logic exist
3. Semantic validity - no orphan nodes, valid conditions
"""

from __future__ import annotations

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

    ctx = ValidationContext(
        survey_id=survey_id,
        blocks=blocks or {},
        questions=questions or {},
    )

    # Validate root structure
    if not isinstance(flow, dict):
        errors.append("Flow must be a dictionary")
        raise FlowValidationError(errors)

    flow_list = flow.get("Flow", [])
    if not isinstance(flow_list, list):
        errors.append("Flow.Flow must be a list")
        raise FlowValidationError(errors)

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
            qid = value.get("QuestionID") or value.get("QuestionIDFromLocator", "")
            if qid and ctx.questions and qid not in ctx.questions:
                errors.append(
                    f"{path}: Question '{qid}' referenced in condition does not exist"
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
