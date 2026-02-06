"""YAML <-> JSON conversion for survey flow synchronization.

This module handles bidirectional conversion between Qualtrics SurveyFlow JSON
and a human-readable YAML format. It uses a hybrid approach:
- Simple conditions use readable format (logic_type, field, operator, value)
- Complex conditions use raw_logic escape hatch to preserve exact structure

The YAML format is designed to be:
- Human-readable and editable
- Git-friendly (clean diffs)
- Lossless (round-trip preserves all data)
"""

from __future__ import annotations

import json
from typing import Any, Optional

import yaml


SCHEMA_VERSION = 1


def flow_to_yaml(
    flow: dict,
    survey_id: str,
    blocks: Optional[dict] = None,
    questions: Optional[dict] = None,
) -> str:
    """Convert Qualtrics SurveyFlow JSON to human-readable YAML.

    Args:
        flow: The SurveyFlow dict from survey definition
        survey_id: Survey ID for metadata
        blocks: Optional blocks dict for annotating block names
        questions: Optional questions dict for annotating QIDs

    Returns:
        YAML string representation of the flow
    """
    blocks = blocks or {}
    questions = questions or {}

    flow_list = flow.get("Flow", [])
    converted_nodes = [
        _convert_node_to_yaml(node, blocks, questions) for node in flow_list
    ]

    yaml_doc = {
        "version": SCHEMA_VERSION,
        "survey_id": survey_id,
        "flow_id": flow.get("FlowID", "FL_ROOT"),
        "flow_type": flow.get("Type", "Root"),
        "flow": converted_nodes,
    }

    # Preserve any additional properties
    props = flow.get("Properties")
    if props:
        yaml_doc["properties"] = props

    return yaml.dump(
        yaml_doc,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        width=120,
    )


def yaml_to_flow(yaml_content: str) -> dict:
    """Convert YAML back to Qualtrics SurveyFlow JSON.

    Args:
        yaml_content: YAML string representation

    Returns:
        SurveyFlow dict compatible with Qualtrics API
    """
    data = yaml.safe_load(yaml_content)

    if not isinstance(data, dict):
        raise ValueError("Invalid YAML: expected a dictionary at root level")

    flow_list = data.get("flow", [])
    converted_nodes = [_convert_node_from_yaml(node) for node in flow_list]

    result = {
        "Type": data.get("flow_type", "Root"),
        "FlowID": data.get("flow_id", "FL_ROOT"),
        "Flow": converted_nodes,
    }

    # Restore properties if present
    props = data.get("properties")
    if props:
        result["Properties"] = props

    return result


def _convert_node_to_yaml(
    node: dict, blocks: dict, questions: dict
) -> dict[str, Any]:
    """Convert a single flow node from JSON to YAML format."""
    if not isinstance(node, dict):
        return {"_raw": node}

    node_type = str(node.get("Type", "")).strip()

    # Block / Standard
    if node_type in ("Block", "Standard"):
        block_id = node.get("ID", "")
        flow_id = node.get("FlowID", "")
        result: dict[str, Any] = {
            "type": "Block",
            "id": block_id,
        }
        # Preserve original type if it's "Block" (vs "Standard")
        if node_type == "Block":
            result["original_type"] = "Block"
        # Preserve FlowID if present (Qualtrics assigns both ID and FlowID to blocks)
        if flow_id:
            result["flow_id"] = flow_id
        # Preserve Autofill if present
        if "Autofill" in node:
            result["autofill"] = node["Autofill"]
        # Add human-readable block name if available
        block_info = blocks.get(block_id, {})
        if block_info.get("Description"):
            result["name"] = block_info["Description"]
        return result

    # Branch
    if node_type == "Branch":
        result: dict[str, Any] = {
            "type": "Branch",
            "id": node.get("FlowID", ""),
        }

        # Preserve original Description if present
        if node.get("Description"):
            result["original_description"] = node["Description"]

        # Convert branch logic - always preserve raw_logic for clean round-trip
        # but also generate human-readable description for readability
        branch_logic = node.get("BranchLogic")
        if branch_logic:
            result["description"] = _describe_branch_logic(branch_logic)
            result["raw_logic"] = branch_logic

        # Convert then/else branches
        then_flow = node.get("Flow") or []
        else_flow = node.get("ElseFlow") or []

        if then_flow:
            result["then"] = [
                _convert_node_to_yaml(n, blocks, questions) for n in then_flow
            ]
        if else_flow:
            result["else"] = [
                _convert_node_to_yaml(n, blocks, questions) for n in else_flow
            ]

        return result

    # EmbeddedData
    if node_type == "EmbeddedData":
        fields = []
        embedded_data = node.get("EmbeddedData", [])
        # Known field mappings (Qualtrics key -> YAML key)
        known_fields = {
            "Field": "field",
            "Value": "value",
            "Type": "type",
            "Description": "description",
            "VariableType": "variable_type",
            "DataVisibility": "data_visibility",
            "AnalyzeText": "analyze_text",
        }
        for item in embedded_data:
            if isinstance(item, dict):
                field_entry: dict[str, Any] = {"field": item.get("Field", "")}
                # Map known fields
                for qualtrics_key, yaml_key in known_fields.items():
                    if qualtrics_key in item and qualtrics_key != "Field":
                        field_entry[yaml_key] = item[qualtrics_key]
                # Preserve any unknown fields for future-proofing
                for key, value in item.items():
                    if key not in known_fields:
                        field_entry[f"_raw_{key}"] = value
                fields.append(field_entry)

        return {
            "type": "EmbeddedData",
            "id": node.get("FlowID", ""),
            "fields": fields,
        }

    # BlockRandomizer
    if node_type == "BlockRandomizer":
        sub_flow = node.get("Flow", [])
        result = {
            "type": "BlockRandomizer",
            "id": node.get("FlowID", ""),
            "randomization": {
                "count": node.get("SubSet"),
                "evenly_present": node.get("EvenPresentation", False),
            },
            "blocks": [
                _convert_node_to_yaml(n, blocks, questions) for n in sub_flow
            ],
        }
        return result

    # Group
    if node_type == "Group":
        sub_flow = node.get("Flow", [])
        result = {
            "type": "Group",
            "id": node.get("FlowID", ""),
            "description": node.get("Description", ""),
            "flow": [
                _convert_node_to_yaml(n, blocks, questions) for n in sub_flow
            ],
        }
        return result

    # WebService
    if node_type == "WebService":
        return {
            "type": "WebService",
            "id": node.get("FlowID", ""),
            "url": node.get("URL", ""),
            "method": node.get("Method", "GET"),
            "raw_config": {
                k: v
                for k, v in node.items()
                if k not in ("Type", "FlowID", "URL", "Method")
            },
        }

    # EndSurvey
    if node_type == "EndSurvey":
        options = node.get("Options", {})
        result: dict[str, Any] = {
            "type": "EndSurvey",
            "id": node.get("FlowID", ""),
            "options": {},
        }
        # Preserve EndingType if present
        if node.get("EndingType"):
            result["ending_type"] = node["EndingType"]
        if options.get("SurveyTermination"):
            result["options"]["end_type"] = options["SurveyTermination"]
        if options.get("DisplayMessage"):
            result["options"]["display_message"] = options["DisplayMessage"]
        if options.get("CustomURL"):
            result["options"]["custom_url"] = options["CustomURL"]
        # Preserve any additional options
        for k, v in options.items():
            if k not in ("SurveyTermination", "DisplayMessage", "CustomURL"):
                result["options"][k] = v
        return result

    # Authenticator
    if node_type == "Authenticator":
        return {
            "type": "Authenticator",
            "id": node.get("FlowID", ""),
            "raw_config": {k: v for k, v in node.items() if k not in ("Type", "FlowID")},
        }

    # TableOfContents
    if node_type == "TableOfContents":
        return {
            "type": "TableOfContents",
            "id": node.get("FlowID", ""),
        }

    # Unknown node type - preserve raw JSON
    return {
        "type": "Unknown",
        "original_type": node_type,
        "id": node.get("FlowID") or node.get("ID", ""),
        "_raw": node,
    }


def _convert_node_from_yaml(node: dict) -> dict[str, Any]:
    """Convert a single flow node from YAML back to JSON format."""
    if not isinstance(node, dict):
        return {}

    node_type = str(node.get("type", "")).strip()

    # Block
    if node_type == "Block":
        # Restore original type (Block vs Standard)
        original_type = node.get("original_type", "Standard")
        result: dict[str, Any] = {
            "Type": original_type,
            "ID": node.get("id", ""),
        }
        # Restore FlowID if it was preserved
        if node.get("flow_id"):
            result["FlowID"] = node["flow_id"]
        # Restore Autofill if present
        if "autofill" in node:
            result["Autofill"] = node["autofill"]
        return result

    # Branch
    if node_type == "Branch":
        result: dict[str, Any] = {
            "Type": "Branch",
            "FlowID": node.get("id", ""),
        }

        # Restore original Description if present
        if node.get("original_description"):
            result["Description"] = node["original_description"]

        # Convert condition back to BranchLogic
        if "raw_logic" in node:
            result["BranchLogic"] = node["raw_logic"]
        elif "condition" in node:
            result["BranchLogic"] = _condition_to_branch_logic(node["condition"])

        # Convert then/else branches
        then_nodes = node.get("then", [])
        else_nodes = node.get("else", [])

        if then_nodes:
            result["Flow"] = [_convert_node_from_yaml(n) for n in then_nodes]
        if else_nodes:
            result["ElseFlow"] = [_convert_node_from_yaml(n) for n in else_nodes]

        return result

    # EmbeddedData
    if node_type == "EmbeddedData":
        embedded_data = []
        # Known field mappings (YAML key -> Qualtrics key)
        known_fields = {
            "field": "Field",
            "value": "Value",
            "type": "Type",
            "description": "Description",
            "variable_type": "VariableType",
            "data_visibility": "DataVisibility",
            "analyze_text": "AnalyzeText",
        }
        for field in node.get("fields", []):
            if isinstance(field, dict):
                item: dict[str, Any] = {"Field": field.get("field", "")}
                # Restore known fields
                for yaml_key, qualtrics_key in known_fields.items():
                    if yaml_key in field and yaml_key != "field":
                        item[qualtrics_key] = field[yaml_key]
                # Restore any unknown fields that were preserved
                for key, value in field.items():
                    if key.startswith("_raw_"):
                        original_key = key[5:]  # Remove "_raw_" prefix
                        item[original_key] = value
                embedded_data.append(item)

        return {
            "Type": "EmbeddedData",
            "FlowID": node.get("id", ""),
            "EmbeddedData": embedded_data,
        }

    # BlockRandomizer
    if node_type == "BlockRandomizer":
        randomization = node.get("randomization", {})
        sub_blocks = node.get("blocks", [])

        result = {
            "Type": "BlockRandomizer",
            "FlowID": node.get("id", ""),
            "Flow": [_convert_node_from_yaml(n) for n in sub_blocks],
        }
        if randomization.get("count") is not None:
            result["SubSet"] = randomization["count"]
        if randomization.get("evenly_present"):
            result["EvenPresentation"] = True

        return result

    # Group
    if node_type == "Group":
        sub_flow = node.get("flow", [])
        result = {
            "Type": "Group",
            "FlowID": node.get("id", ""),
            "Flow": [_convert_node_from_yaml(n) for n in sub_flow],
        }
        if node.get("description"):
            result["Description"] = node["description"]
        return result

    # WebService
    if node_type == "WebService":
        result = {
            "Type": "WebService",
            "FlowID": node.get("id", ""),
            "URL": node.get("url", ""),
            "Method": node.get("method", "GET"),
        }
        # Restore raw config
        raw_config = node.get("raw_config", {})
        result.update(raw_config)
        return result

    # EndSurvey
    if node_type == "EndSurvey":
        options_in = node.get("options", {})
        options_out: dict[str, Any] = {}

        if options_in.get("end_type"):
            options_out["SurveyTermination"] = options_in["end_type"]
        if options_in.get("display_message"):
            options_out["DisplayMessage"] = options_in["display_message"]
        if options_in.get("custom_url"):
            options_out["CustomURL"] = options_in["custom_url"]

        # Restore any additional options
        for k, v in options_in.items():
            if k not in ("end_type", "display_message", "custom_url"):
                options_out[k] = v

        result: dict[str, Any] = {
            "Type": "EndSurvey",
            "FlowID": node.get("id", ""),
            "Options": options_out,
        }
        # Restore EndingType if present
        if node.get("ending_type"):
            result["EndingType"] = node["ending_type"]
        return result

    # Authenticator
    if node_type == "Authenticator":
        result = {
            "Type": "Authenticator",
            "FlowID": node.get("id", ""),
        }
        result.update(node.get("raw_config", {}))
        return result

    # TableOfContents
    if node_type == "TableOfContents":
        return {
            "Type": "TableOfContents",
            "FlowID": node.get("id", ""),
        }

    # Unknown - restore from _raw if available
    if node_type == "Unknown" and "_raw" in node:
        return node["_raw"]

    # Fallback: try to reconstruct from available fields
    return {
        "Type": node.get("original_type") or node_type,
        "FlowID": node.get("id", ""),
    }


def _try_simplify_branch_logic(logic: dict) -> Optional[dict[str, Any]]:
    """Try to simplify BranchLogic to readable condition format.

    Returns None if the logic is too complex to simplify.
    """
    if not isinstance(logic, dict):
        return None

    if logic.get("Type") != "BooleanExpression":
        return None

    if_block = logic.get("0")
    if not isinstance(if_block, dict) or if_block.get("Type") != "If":
        return None

    # Extract expressions
    expressions = []
    conjunction = None

    for key, value in if_block.items():
        if not str(key).isdigit():
            continue
        if not isinstance(value, dict) or value.get("Type") != "Expression":
            continue

        expr = _simplify_expression(value)
        if expr is None:
            return None  # Can't simplify this expression

        expressions.append(expr)
        if "Conjuction" in value or "Conjunction" in value:
            conjunction = value.get("Conjuction") or value.get("Conjunction")

    if not expressions:
        return None

    # Single expression
    if len(expressions) == 1:
        return {
            "description": _format_condition_description(expressions[0]),
            "condition": expressions[0],
        }

    # Multiple expressions with conjunction
    return {
        "description": _format_multi_condition_description(expressions, conjunction),
        "condition": {
            "conjunction": conjunction or "And",
            "expressions": expressions,
        },
    }


def _simplify_expression(expr: dict) -> Optional[dict[str, Any]]:
    """Simplify a single Expression to readable format."""
    logic_type = expr.get("LogicType", "")

    if logic_type == "EmbeddedField":
        return {
            "logic_type": "EmbeddedField",
            "field": expr.get("LeftOperand", ""),
            "operator": expr.get("Operator", ""),
            "value": expr.get("RightOperand", ""),
        }

    if logic_type == "Question":
        result: dict[str, Any] = {
            "logic_type": "Question",
            "question_id": expr.get("QuestionID") or expr.get("QuestionIDFromLocator", ""),
            "operator": expr.get("Operator", ""),
        }
        if "ChoiceLocator" in expr:
            result["choice_locator"] = expr["ChoiceLocator"]
        if "RightOperand" in expr:
            result["value"] = expr["RightOperand"]
        return result

    # Can't simplify this logic type
    return None


def _condition_to_branch_logic(condition: dict) -> dict[str, Any]:
    """Convert simplified condition back to Qualtrics BranchLogic."""
    if "expressions" in condition:
        # Multiple expressions with conjunction
        if_block: dict[str, Any] = {"Type": "If"}
        for i, expr in enumerate(condition["expressions"]):
            expr_dict = _expression_to_logic(expr)
            if i > 0 and condition.get("conjunction"):
                expr_dict["Conjuction"] = condition["conjunction"]
            if_block[str(i)] = expr_dict

        return {"Type": "BooleanExpression", "0": if_block}

    # Single expression
    expr_dict = _expression_to_logic(condition)
    return {"Type": "BooleanExpression", "0": {"Type": "If", "0": expr_dict}}


def _expression_to_logic(expr: dict) -> dict[str, Any]:
    """Convert simplified expression back to Qualtrics format."""
    logic_type = expr.get("logic_type", "")

    if logic_type == "EmbeddedField":
        return {
            "Type": "Expression",
            "LogicType": "EmbeddedField",
            "LeftOperand": expr.get("field", ""),
            "Operator": expr.get("operator", ""),
            "RightOperand": expr.get("value", ""),
        }

    if logic_type == "Question":
        result: dict[str, Any] = {
            "Type": "Expression",
            "LogicType": "Question",
            "QuestionID": expr.get("question_id", ""),
            "Operator": expr.get("operator", ""),
        }
        if "choice_locator" in expr:
            result["ChoiceLocator"] = expr["choice_locator"]
        if "value" in expr:
            result["RightOperand"] = expr["value"]
        return result

    # Fallback: return as-is assuming it's already in logic format
    return expr


def _describe_branch_logic(logic: dict) -> str:
    """Generate a human-readable description of branch logic."""
    if not isinstance(logic, dict):
        return "Unknown condition"

    if logic.get("Type") != "BooleanExpression":
        return "Unknown condition type"

    if_block = logic.get("0")
    if not isinstance(if_block, dict):
        return "Unknown condition structure"

    descriptions = []
    conjunction = "AND"

    for key, value in if_block.items():
        if not str(key).isdigit():
            continue
        if not isinstance(value, dict):
            continue

        if value.get("Conjuction") or value.get("Conjunction"):
            conjunction = value.get("Conjuction") or value.get("Conjunction")

        logic_type = value.get("LogicType", "")
        operator = value.get("Operator", "")

        if logic_type == "EmbeddedField":
            field = value.get("LeftOperand", "?")
            val = value.get("RightOperand", "?")
            descriptions.append(f"{field} {operator} {val}")
        elif logic_type == "Question":
            qid = value.get("QuestionID") or value.get("QuestionIDFromLocator", "?")
            descriptions.append(f"Question {qid} {operator}")
        else:
            descriptions.append(f"{logic_type} condition")

    if not descriptions:
        return "Empty condition"

    return f" {conjunction} ".join(descriptions)


def _format_condition_description(condition: dict) -> str:
    """Format a single condition as a human-readable string."""
    logic_type = condition.get("logic_type", "")

    if logic_type == "EmbeddedField":
        field = condition.get("field", "?")
        operator = condition.get("operator", "?")
        value = condition.get("value", "?")
        return f"{field} {operator} {value}"

    if logic_type == "Question":
        qid = condition.get("question_id", "?")
        operator = condition.get("operator", "?")
        return f"Question {qid} {operator}"

    return "Complex condition"


def _format_multi_condition_description(
    expressions: list[dict], conjunction: Optional[str]
) -> str:
    """Format multiple conditions as a human-readable string."""
    conj = conjunction or "AND"
    parts = [_format_condition_description(expr) for expr in expressions]
    return f" {conj} ".join(parts)


def round_trip_test(flow: dict, survey_id: str = "TEST") -> bool:
    """Test that a flow survives YAML round-trip without data loss.

    This is useful for validating conversion logic during development.

    Args:
        flow: Original SurveyFlow dict
        survey_id: Survey ID for metadata

    Returns:
        True if round-trip preserves data, False otherwise
    """
    yaml_str = flow_to_yaml(flow, survey_id)
    restored = yaml_to_flow(yaml_str)

    # Compare normalized JSON
    original_json = json.dumps(flow, sort_keys=True)
    restored_json = json.dumps(restored, sort_keys=True)

    return original_json == restored_json
