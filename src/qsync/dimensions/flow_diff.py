"""Structural diff algorithm for survey flow synchronization.

This module provides semantic diffing between two SurveyFlow structures,
generating human-readable descriptions of changes rather than raw JSON diffs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import Any, Literal, Optional


@dataclass
class FlowChange:
    """Represents a single detected change in the flow structure."""

    change_type: Literal["added", "removed", "modified", "reordered"]
    node_id: str
    node_type: str
    description: str
    path: str = ""
    old_value: Optional[str] = None
    new_value: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FlowChange":
        """Create from dictionary."""
        return cls(
            change_type=data.get("change_type", "modified"),
            node_id=data.get("node_id", ""),
            node_type=data.get("node_type", ""),
            description=data.get("description", ""),
            path=data.get("path", ""),
            old_value=data.get("old_value"),
            new_value=data.get("new_value"),
        )


def diff_flows(baseline: dict, edited: dict) -> list[FlowChange]:
    """Generate semantic diff between two flow structures.

    Args:
        baseline: The original SurveyFlow dict (from baseline.json)
        edited: The modified SurveyFlow dict (from edited YAML)

    Returns:
        List of FlowChange objects describing all differences
    """
    changes: list[FlowChange] = []

    # Build node maps for both flows
    baseline_nodes = _build_node_map(baseline)
    edited_nodes = _build_node_map(edited)

    # Detect removals (in baseline but not in edited)
    for node_id, (node, path) in baseline_nodes.items():
        if node_id not in edited_nodes:
            node_type = _get_node_type(node)
            changes.append(
                FlowChange(
                    change_type="removed",
                    node_id=node_id,
                    node_type=node_type,
                    description=f"Removed {node_type} node",
                    path=path,
                    old_value=json.dumps(node, indent=2),
                )
            )

    # Detect additions (in edited but not in baseline)
    for node_id, (node, path) in edited_nodes.items():
        if node_id not in baseline_nodes:
            node_type = _get_node_type(node)
            changes.append(
                FlowChange(
                    change_type="added",
                    node_id=node_id,
                    node_type=node_type,
                    description=f"Added {node_type} node",
                    path=path,
                    new_value=json.dumps(node, indent=2),
                )
            )

    # Detect modifications (same ID but different content)
    for node_id in baseline_nodes.keys() & edited_nodes.keys():
        old_node, old_path = baseline_nodes[node_id]
        new_node, new_path = edited_nodes[node_id]

        # Normalize for comparison (ignore path differences)
        old_normalized = _normalize_node(old_node)
        new_normalized = _normalize_node(new_node)

        if old_normalized != new_normalized:
            node_type = _get_node_type(old_node)
            desc = _describe_modification(old_node, new_node, node_type)
            changes.append(
                FlowChange(
                    change_type="modified",
                    node_id=node_id,
                    node_type=node_type,
                    description=desc,
                    path=new_path,
                    old_value=json.dumps(old_node, indent=2),
                    new_value=json.dumps(new_node, indent=2),
                )
            )

    # Detect reordering at the top level (even if content is identical)
    reorder_changes = _detect_reordering(baseline, edited)
    changes.extend(reorder_changes)

    return changes


def _detect_reordering(baseline: dict, edited: dict) -> list[FlowChange]:
    """Detect if nodes have been reordered within flow lists.

    Args:
        baseline: The original SurveyFlow dict
        edited: The modified SurveyFlow dict

    Returns:
        List of FlowChange objects for reordered nodes
    """
    changes: list[FlowChange] = []

    # Check top-level flow ordering
    baseline_order = _get_flow_order(baseline.get("Flow", []))
    edited_order = _get_flow_order(edited.get("Flow", []))

    # Only report if the sets are equal but order differs
    if set(baseline_order) == set(edited_order) and baseline_order != edited_order:
        # Find which nodes moved
        for i, (old_id, new_id) in enumerate(zip(baseline_order, edited_order)):
            if old_id != new_id:
                # Find where old_id went in the new order
                new_pos = edited_order.index(old_id) if old_id in edited_order else -1
                if new_pos != -1 and new_pos != i:
                    changes.append(
                        FlowChange(
                            change_type="reordered",
                            node_id=old_id,
                            node_type="Flow",
                            description=f"Moved from position {i} to {new_pos}",
                            path=f"flow[{i}] -> flow[{new_pos}]",
                        )
                    )
                break  # Only report the first reordering to avoid duplicates

    # Recursively check nested flows (branches, groups, randomizers)
    _check_nested_reordering(
        baseline.get("Flow", []), edited.get("Flow", []), "flow", changes
    )

    return changes


def _get_flow_order(flow_list: list) -> list[str]:
    """Extract ordered list of node IDs from a flow list."""
    order = []
    for node in flow_list:
        if isinstance(node, dict):
            node_id = _get_node_id(node)
            if node_id:
                order.append(node_id)
    return order


def _check_nested_reordering(
    baseline_list: list, edited_list: list, path: str, changes: list[FlowChange]
) -> None:
    """Recursively check for reordering in nested flow structures."""
    # Build maps of node_id -> node for both lists
    baseline_map = {_get_node_id(n): n for n in baseline_list if isinstance(n, dict)}
    edited_map = {_get_node_id(n): n for n in edited_list if isinstance(n, dict)}

    # Check common nodes for nested reordering
    for node_id in baseline_map.keys() & edited_map.keys():
        old_node = baseline_map[node_id]
        new_node = edited_map[node_id]
        node_type = _get_node_type(old_node)

        if node_type == "Branch":
            # Check then branch
            old_then = old_node.get("Flow", [])
            new_then = new_node.get("Flow", [])
            _check_list_reordering(old_then, new_then, f"{path}.{node_id}.then", changes)

            # Check else branch
            old_else = old_node.get("ElseFlow", [])
            new_else = new_node.get("ElseFlow", [])
            _check_list_reordering(old_else, new_else, f"{path}.{node_id}.else", changes)

        elif node_type in ("BlockRandomizer", "Group"):
            old_flow = old_node.get("Flow", [])
            new_flow = new_node.get("Flow", [])
            _check_list_reordering(old_flow, new_flow, f"{path}.{node_id}.flow", changes)


def _check_list_reordering(
    old_list: list, new_list: list, path: str, changes: list[FlowChange]
) -> None:
    """Check if two lists have the same items but in different order."""
    old_order = _get_flow_order(old_list)
    new_order = _get_flow_order(new_list)

    if set(old_order) == set(new_order) and old_order != new_order:
        changes.append(
            FlowChange(
                change_type="reordered",
                node_id=path,
                node_type="FlowList",
                description=f"Node order changed: {old_order} -> {new_order}",
                path=path,
            )
        )

    # Recurse into nested structures
    _check_nested_reordering(old_list, new_list, path, changes)


def _build_node_map(flow: dict, path: str = "flow") -> dict[str, tuple[dict, str]]:
    """Build a map of node_id -> (node, path) for all nodes in the flow.

    Args:
        flow: SurveyFlow dict
        path: Current path in the flow structure

    Returns:
        Dict mapping node IDs to (node_dict, path_string) tuples
    """
    nodes: dict[str, tuple[dict, str]] = {}

    flow_list = flow.get("Flow", [])
    _traverse_nodes(flow_list, path, nodes)

    return nodes


def _traverse_nodes(
    node_list: list, path: str, nodes: dict[str, tuple[dict, str]]
) -> None:
    """Recursively traverse flow nodes and collect them into the map."""
    for i, node in enumerate(node_list):
        if not isinstance(node, dict):
            continue

        current_path = f"{path}[{i}]"
        node_id = _get_node_id(node)

        if node_id:
            nodes[node_id] = (node, current_path)

        # Recurse into child nodes
        node_type = _get_node_type(node)

        if node_type == "Branch":
            then_flow = node.get("Flow", [])
            else_flow = node.get("ElseFlow", [])
            if then_flow:
                _traverse_nodes(then_flow, f"{current_path}.then", nodes)
            if else_flow:
                _traverse_nodes(else_flow, f"{current_path}.else", nodes)

        elif node_type in ("BlockRandomizer", "Group"):
            sub_flow = node.get("Flow", [])
            if sub_flow:
                _traverse_nodes(sub_flow, f"{current_path}.flow", nodes)


def _get_node_id(node: dict) -> str:
    """Extract the node ID from a flow node."""
    # FlowID is used by most node types
    flow_id = node.get("FlowID")
    if flow_id:
        return str(flow_id)

    # ID is used by Block/Standard nodes
    node_id = node.get("ID")
    if node_id:
        return str(node_id)

    return ""


def _get_node_type(node: dict) -> str:
    """Extract the node type from a flow node."""
    node_type = node.get("Type", "")
    # Normalize Block/Standard to Block
    if node_type in ("Block", "Standard"):
        return "Block"
    return node_type


def _normalize_node(node: dict) -> dict:
    """Normalize a node for comparison, removing path-specific data."""
    # Create a copy without position-specific fields
    normalized = dict(node)

    # For comparison, we care about content not position
    # FlowID/ID identify the node, Type and content matter
    return normalized


def _describe_modification(old_node: dict, new_node: dict, node_type: str) -> str:
    """Generate a human-readable description of how a node was modified."""
    descriptions = []

    if node_type == "Branch":
        # Check if branch logic changed
        old_logic = old_node.get("BranchLogic", {})
        new_logic = new_node.get("BranchLogic", {})
        if old_logic != new_logic:
            old_desc = _summarize_branch_logic(old_logic)
            new_desc = _summarize_branch_logic(new_logic)
            descriptions.append(f"Condition changed ({old_desc} -> {new_desc})")

        # Check if then/else branches changed structurally
        old_then = old_node.get("Flow", [])
        new_then = new_node.get("Flow", [])
        old_else = old_node.get("ElseFlow", [])
        new_else = new_node.get("ElseFlow", [])

        if len(old_then) != len(new_then):
            descriptions.append(
                f"Then branch: {len(old_then)} -> {len(new_then)} nodes"
            )
        elif old_then != new_then:
            # Same count but different content
            descriptions.append("Then branch content changed")

        if len(old_else) != len(new_else):
            descriptions.append(
                f"Else branch: {len(old_else)} -> {len(new_else)} nodes"
            )
        elif old_else != new_else:
            # Same count but different content
            descriptions.append("Else branch content changed")

    elif node_type == "EmbeddedData":
        old_fields = old_node.get("EmbeddedData", [])
        new_fields = new_node.get("EmbeddedData", [])

        old_field_names = {f.get("Field") for f in old_fields if isinstance(f, dict)}
        new_field_names = {f.get("Field") for f in new_fields if isinstance(f, dict)}

        added = new_field_names - old_field_names
        removed = old_field_names - new_field_names

        if added:
            descriptions.append(f"Added fields: {', '.join(sorted(added))}")
        if removed:
            descriptions.append(f"Removed fields: {', '.join(sorted(removed))}")

        # Check for value and metadata changes in existing fields
        for field in old_field_names & new_field_names:
            old_field_data = next(
                (f for f in old_fields if f.get("Field") == field), {}
            )
            new_field_data = next(
                (f for f in new_fields if f.get("Field") == field), {}
            )

            old_val = old_field_data.get("Value")
            new_val = new_field_data.get("Value")
            if old_val != new_val:
                descriptions.append(f"Field {field}: value changed")

            # Check metadata changes
            old_type = old_field_data.get("Type")
            new_type = new_field_data.get("Type")
            if old_type != new_type:
                descriptions.append(f"Field {field}: type changed ({old_type} -> {new_type})")

            old_var_type = old_field_data.get("VariableType")
            new_var_type = new_field_data.get("VariableType")
            if old_var_type != new_var_type:
                descriptions.append(f"Field {field}: variable type changed")

    elif node_type == "BlockRandomizer":
        old_count = old_node.get("SubSet")
        new_count = new_node.get("SubSet")
        if old_count != new_count:
            descriptions.append(f"Randomization count: {old_count} -> {new_count}")

        old_even = old_node.get("EvenPresentation", False)
        new_even = new_node.get("EvenPresentation", False)
        if old_even != new_even:
            descriptions.append(f"Even presentation: {old_even} -> {new_even}")

    elif node_type == "EndSurvey":
        old_opts = old_node.get("Options", {})
        new_opts = new_node.get("Options", {})

        old_term = old_opts.get("SurveyTermination")
        new_term = new_opts.get("SurveyTermination")
        if old_term != new_term:
            descriptions.append(f"End type: {old_term} -> {new_term}")

    elif node_type == "WebService":
        old_url = old_node.get("URL", "")
        new_url = new_node.get("URL", "")
        if old_url != new_url:
            descriptions.append(f"URL changed")

        old_method = old_node.get("Method", "GET")
        new_method = new_node.get("Method", "GET")
        if old_method != new_method:
            descriptions.append(f"Method: {old_method} -> {new_method}")

    if not descriptions:
        descriptions.append("Content modified")

    return "; ".join(descriptions)


def _summarize_branch_logic(logic: dict) -> str:
    """Generate a short summary of branch logic for diff descriptions."""
    if not isinstance(logic, dict):
        return "unknown"

    if logic.get("Type") != "BooleanExpression":
        return "unknown"

    if_block = logic.get("0")
    if not isinstance(if_block, dict):
        return "unknown"

    # Extract first expression for summary
    for key, value in if_block.items():
        if not str(key).isdigit():
            continue
        if not isinstance(value, dict):
            continue

        logic_type = value.get("LogicType", "")
        operator = value.get("Operator", "")

        if logic_type == "EmbeddedField":
            field = value.get("LeftOperand", "?")
            val = value.get("RightOperand", "?")
            return f"{field} {operator} {val}"
        elif logic_type == "Question":
            qid = value.get("QuestionID") or value.get("QuestionIDFromLocator", "?")
            return f"Q:{qid} {operator}"

    return "complex"


def format_diff_for_display(
    changes: list[FlowChange], verbose: bool = False
) -> list[str]:
    """Format flow changes for terminal display.

    Args:
        changes: List of FlowChange objects
        verbose: If True, include old/new values in output

    Returns:
        List of formatted strings for display
    """
    if not changes:
        return ["No changes detected"]

    lines = []

    # Group by change type for cleaner output
    added = [c for c in changes if c.change_type == "added"]
    removed = [c for c in changes if c.change_type == "removed"]
    modified = [c for c in changes if c.change_type == "modified"]
    reordered = [c for c in changes if c.change_type == "reordered"]

    if added:
        for change in added:
            symbol = "+"
            line = f"  {symbol} {change.node_type} [{change.node_id}]: {change.description}"
            lines.append(line)
            if verbose and change.new_value:
                for val_line in change.new_value.split("\n"):
                    lines.append(f"      {val_line}")

    if removed:
        for change in removed:
            symbol = "-"
            line = f"  {symbol} {change.node_type} [{change.node_id}]: {change.description}"
            lines.append(line)
            if verbose and change.old_value:
                for val_line in change.old_value.split("\n"):
                    lines.append(f"      {val_line}")

    if modified:
        for change in modified:
            symbol = "~"
            line = f"  {symbol} {change.node_type} [{change.node_id}]: {change.description}"
            lines.append(line)

    if reordered:
        for change in reordered:
            symbol = "↕"
            line = f"  {symbol} {change.node_type} [{change.node_id}]: {change.description}"
            lines.append(line)

    return lines


def format_diff_summary(changes: list[FlowChange]) -> str:
    """Generate a one-line summary of changes.

    Args:
        changes: List of FlowChange objects

    Returns:
        Summary string like "2 added, 1 modified, 0 removed"
    """
    added = sum(1 for c in changes if c.change_type == "added")
    removed = sum(1 for c in changes if c.change_type == "removed")
    modified = sum(1 for c in changes if c.change_type == "modified")
    reordered = sum(1 for c in changes if c.change_type == "reordered")

    parts = []
    if added:
        parts.append(f"{added} added")
    if modified:
        parts.append(f"{modified} modified")
    if removed:
        parts.append(f"{removed} removed")
    if reordered:
        parts.append(f"{reordered} reordered")

    if not parts:
        return "No changes"

    return ", ".join(parts)
