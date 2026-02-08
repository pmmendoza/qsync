"""Tests for flow structural diff algorithm."""

from qsync.dimensions.flow_diff import (
    FlowChange,
    diff_flows,
    format_diff_for_display,
    format_diff_summary,
)


class TestDiffFlows:
    """Tests for diff_flows function."""

    def test_no_changes(self):
        """Test that identical flows produce no changes."""
        flow = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [
                {"Type": "Standard", "ID": "BL_intro"},
                {"Type": "Standard", "ID": "BL_main"},
            ],
        }

        changes = diff_flows(flow, flow)
        assert len(changes) == 0

    def test_detect_added_block(self):
        """Test detection of added block."""
        baseline = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [{"Type": "Standard", "ID": "BL_intro"}],
        }
        edited = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [
                {"Type": "Standard", "ID": "BL_intro"},
                {"Type": "Standard", "ID": "BL_new"},
            ],
        }

        changes = diff_flows(baseline, edited)

        assert len(changes) == 1
        assert changes[0].change_type == "added"
        assert changes[0].node_id == "BL_new"
        assert changes[0].node_type == "Block"

    def test_detect_removed_block(self):
        """Test detection of removed block."""
        baseline = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [
                {"Type": "Standard", "ID": "BL_intro"},
                {"Type": "Standard", "ID": "BL_old"},
            ],
        }
        edited = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [{"Type": "Standard", "ID": "BL_intro"}],
        }

        changes = diff_flows(baseline, edited)

        assert len(changes) == 1
        assert changes[0].change_type == "removed"
        assert changes[0].node_id == "BL_old"
        assert changes[0].node_type == "Block"

    def test_detect_modified_embedded_data(self):
        """Test detection of modified embedded data fields."""
        baseline = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [
                {
                    "Type": "EmbeddedData",
                    "FlowID": "FL_1",
                    "EmbeddedData": [{"Field": "study_arm", "Value": "control"}],
                },
            ],
        }
        edited = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [
                {
                    "Type": "EmbeddedData",
                    "FlowID": "FL_1",
                    "EmbeddedData": [{"Field": "study_arm", "Value": "treatment"}],
                },
            ],
        }

        changes = diff_flows(baseline, edited)

        assert len(changes) == 1
        assert changes[0].change_type == "modified"
        assert changes[0].node_id == "FL_1"
        assert "value changed" in changes[0].description.lower()

    def test_detect_modified_branch_logic(self):
        """Test detection of modified branch condition."""
        baseline = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [
                {
                    "Type": "Branch",
                    "FlowID": "FL_2",
                    "BranchLogic": {
                        "Type": "BooleanExpression",
                        "0": {
                            "Type": "If",
                            "0": {
                                "Type": "Expression",
                                "LogicType": "EmbeddedField",
                                "LeftOperand": "arm",
                                "Operator": "EqualTo",
                                "RightOperand": "A",
                            },
                        },
                    },
                    "Flow": [{"Type": "Standard", "ID": "BL_a"}],
                },
            ],
        }
        edited = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [
                {
                    "Type": "Branch",
                    "FlowID": "FL_2",
                    "BranchLogic": {
                        "Type": "BooleanExpression",
                        "0": {
                            "Type": "If",
                            "0": {
                                "Type": "Expression",
                                "LogicType": "EmbeddedField",
                                "LeftOperand": "arm",
                                "Operator": "EqualTo",
                                "RightOperand": "B",  # Changed!
                            },
                        },
                    },
                    "Flow": [{"Type": "Standard", "ID": "BL_a"}],
                },
            ],
        }

        changes = diff_flows(baseline, edited)

        assert len(changes) == 1
        assert changes[0].change_type == "modified"
        assert changes[0].node_id == "FL_2"
        assert "condition changed" in changes[0].description.lower()

    def test_nested_branch_changes(self):
        """Test detection of changes in nested branch flows."""
        baseline = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [
                {
                    "Type": "Branch",
                    "FlowID": "FL_2",
                    "BranchLogic": {"Type": "BooleanExpression"},
                    "Flow": [{"Type": "Standard", "ID": "BL_then"}],
                    "ElseFlow": [],
                },
            ],
        }
        edited = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [
                {
                    "Type": "Branch",
                    "FlowID": "FL_2",
                    "BranchLogic": {"Type": "BooleanExpression"},
                    "Flow": [{"Type": "Standard", "ID": "BL_then"}],
                    "ElseFlow": [{"Type": "Standard", "ID": "BL_else"}],  # Added!
                },
            ],
        }

        changes = diff_flows(baseline, edited)

        # Should detect both the branch modification and the new block
        assert len(changes) >= 1
        node_ids = {c.node_id for c in changes}
        assert "BL_else" in node_ids or "FL_2" in node_ids


class TestFormatDiffForDisplay:
    """Tests for diff display formatting."""

    def test_format_added(self):
        """Test formatting of added changes."""
        changes = [
            FlowChange(
                change_type="added",
                node_id="BL_new",
                node_type="Block",
                description="Added Block node",
            )
        ]

        lines = format_diff_for_display(changes)

        assert len(lines) >= 1
        assert "+" in lines[0]
        assert "BL_new" in lines[0]

    def test_format_removed(self):
        """Test formatting of removed changes."""
        changes = [
            FlowChange(
                change_type="removed",
                node_id="BL_old",
                node_type="Block",
                description="Removed Block node",
            )
        ]

        lines = format_diff_for_display(changes)

        assert len(lines) >= 1
        assert "-" in lines[0]
        assert "BL_old" in lines[0]

    def test_format_modified(self):
        """Test formatting of modified changes."""
        changes = [
            FlowChange(
                change_type="modified",
                node_id="FL_2",
                node_type="Branch",
                description="Condition changed",
            )
        ]

        lines = format_diff_for_display(changes)

        assert len(lines) >= 1
        assert "~" in lines[0]
        assert "FL_2" in lines[0]

    def test_empty_changes(self):
        """Test formatting of empty change list."""
        lines = format_diff_for_display([])
        assert "No changes" in lines[0]


class TestFormatDiffSummary:
    """Tests for diff summary formatting."""

    def test_summary_counts(self):
        """Test that summary includes correct counts."""
        changes = [
            FlowChange(
                change_type="added", node_id="BL_1", node_type="Block", description=""
            ),
            FlowChange(
                change_type="added", node_id="BL_2", node_type="Block", description=""
            ),
            FlowChange(
                change_type="removed", node_id="BL_3", node_type="Block", description=""
            ),
            FlowChange(
                change_type="modified",
                node_id="FL_1",
                node_type="Branch",
                description="",
            ),
        ]

        summary = format_diff_summary(changes)

        assert "2 added" in summary
        assert "1 removed" in summary
        assert "1 modified" in summary

    def test_empty_summary(self):
        """Test summary for empty changes."""
        summary = format_diff_summary([])
        assert "No changes" in summary


class TestFlowChangeDataclass:
    """Tests for FlowChange dataclass."""

    def test_to_dict(self):
        """Test serialization to dict."""
        change = FlowChange(
            change_type="added",
            node_id="BL_test",
            node_type="Block",
            description="Added block",
            path="flow[0]",
        )

        d = change.to_dict()

        assert d["change_type"] == "added"
        assert d["node_id"] == "BL_test"
        assert d["node_type"] == "Block"
        assert d["description"] == "Added block"
        assert d["path"] == "flow[0]"

    def test_from_dict(self):
        """Test deserialization from dict."""
        d = {
            "change_type": "modified",
            "node_id": "FL_2",
            "node_type": "Branch",
            "description": "Condition changed",
        }

        change = FlowChange.from_dict(d)

        assert change.change_type == "modified"
        assert change.node_id == "FL_2"
        assert change.node_type == "Branch"
