"""Tests for flow validation."""

import pytest

from qsync.dimensions.flow_validate import (
    FlowValidationError,
    validate_flow,
    validate_yaml_structure,
)


class TestValidateFlow:
    """Tests for validate_flow function."""

    def test_valid_simple_flow(self):
        """Test that a valid simple flow passes validation."""
        flow = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [
                {"Type": "Standard", "ID": "BL_intro"},
                {"Type": "Standard", "ID": "BL_main"},
            ],
        }
        blocks = {"BL_intro": {}, "BL_main": {}}

        # Should not raise
        validate_flow(flow, "SV_test", blocks=blocks)

    def test_missing_block_reference(self):
        """Test that missing block reference raises error."""
        flow = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [{"Type": "Standard", "ID": "BL_nonexistent"}],
        }
        blocks = {"BL_intro": {}}  # BL_nonexistent not here

        with pytest.raises(FlowValidationError) as exc_info:
            validate_flow(flow, "SV_test", blocks=blocks)

        assert "BL_nonexistent" in str(exc_info.value)
        assert "does not exist" in str(exc_info.value)

    def test_duplicate_node_ids(self):
        """Test that duplicate node IDs raise error."""
        flow = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [
                {"Type": "Standard", "ID": "BL_same"},
                {"Type": "Standard", "ID": "BL_same"},  # Duplicate!
            ],
        }

        with pytest.raises(FlowValidationError) as exc_info:
            validate_flow(flow, "SV_test")

        assert "Duplicate" in str(exc_info.value)
        assert "BL_same" in str(exc_info.value)

    def test_embedded_data_missing_field(self):
        """Test that embedded data without field name raises error."""
        flow = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [
                {
                    "Type": "EmbeddedData",
                    "FlowID": "FL_1",
                    "EmbeddedData": [{"Value": "test"}],  # Missing "Field"!
                },
            ],
        }

        with pytest.raises(FlowValidationError) as exc_info:
            validate_flow(flow, "SV_test")

        assert "Field" in str(exc_info.value)

    def test_randomizer_subset_too_large(self):
        """Test that randomizer SubSet > block count raises error."""
        flow = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [
                {
                    "Type": "BlockRandomizer",
                    "FlowID": "FL_3",
                    "SubSet": 5,  # Too large!
                    "Flow": [
                        {"Type": "Standard", "ID": "BL_a"},
                        {"Type": "Standard", "ID": "BL_b"},
                    ],
                },
            ],
        }
        blocks = {"BL_a": {}, "BL_b": {}}

        with pytest.raises(FlowValidationError) as exc_info:
            validate_flow(flow, "SV_test", blocks=blocks)

        assert "SubSet" in str(exc_info.value)
        assert "greater than" in str(exc_info.value)

    def test_branch_with_missing_question_reference(self):
        """Test that branch referencing nonexistent question raises error."""
        flow = {
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
                                "LogicType": "Question",
                                "QuestionID": "QID_nonexistent",
                                "Operator": "Selected",
                            },
                        },
                    },
                    "Flow": [{"Type": "Standard", "ID": "BL_then"}],
                },
            ],
        }
        questions = {"QID1": {}, "QID2": {}}  # QID_nonexistent not here
        blocks = {"BL_then": {}}

        with pytest.raises(FlowValidationError) as exc_info:
            validate_flow(flow, "SV_test", blocks=blocks, questions=questions)

        assert "QID_nonexistent" in str(exc_info.value)
        assert "does not exist" in str(exc_info.value)

    def test_valid_nested_branches(self):
        """Test that valid nested branches pass validation."""
        flow = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [
                {
                    "Type": "Branch",
                    "FlowID": "FL_outer",
                    "BranchLogic": {"Type": "BooleanExpression"},
                    "Flow": [
                        {
                            "Type": "Branch",
                            "FlowID": "FL_inner",
                            "BranchLogic": {"Type": "BooleanExpression"},
                            "Flow": [{"Type": "Standard", "ID": "BL_nested"}],
                        }
                    ],
                },
            ],
        }
        blocks = {"BL_nested": {}}

        # Should not raise
        validate_flow(flow, "SV_test", blocks=blocks)


class TestValidateYamlStructure:
    """Tests for validate_yaml_structure function."""

    def test_valid_yaml_structure(self):
        """Test that valid YAML structure passes."""
        yaml_data = {
            "version": 1,
            "survey_id": "SV_test",
            "flow": [
                {"type": "Block", "id": "BL_intro"},
            ],
        }

        # Should not raise
        validate_yaml_structure(yaml_data)

    def test_missing_version(self):
        """Test that missing version raises error."""
        yaml_data = {
            "survey_id": "SV_test",
            "flow": [],
        }

        with pytest.raises(FlowValidationError) as exc_info:
            validate_yaml_structure(yaml_data)

        assert "version" in str(exc_info.value)

    def test_missing_flow(self):
        """Test that missing flow raises error."""
        yaml_data = {
            "version": 1,
            "survey_id": "SV_test",
        }

        with pytest.raises(FlowValidationError) as exc_info:
            validate_yaml_structure(yaml_data)

        assert "flow" in str(exc_info.value)

    def test_flow_not_list(self):
        """Test that non-list flow raises error."""
        yaml_data = {
            "version": 1,
            "survey_id": "SV_test",
            "flow": "not a list",
        }

        with pytest.raises(FlowValidationError) as exc_info:
            validate_yaml_structure(yaml_data)

        assert "list" in str(exc_info.value)

    def test_node_missing_type(self):
        """Test that node without type raises error."""
        yaml_data = {
            "version": 1,
            "survey_id": "SV_test",
            "flow": [{"id": "BL_test"}],  # Missing 'type'
        }

        with pytest.raises(FlowValidationError) as exc_info:
            validate_yaml_structure(yaml_data)

        assert "type" in str(exc_info.value)

    def test_node_missing_id(self):
        """Test that node without id raises error."""
        yaml_data = {
            "version": 1,
            "survey_id": "SV_test",
            "flow": [{"type": "Block"}],  # Missing 'id'
        }

        with pytest.raises(FlowValidationError) as exc_info:
            validate_yaml_structure(yaml_data)

        assert "id" in str(exc_info.value)

    def test_branch_missing_condition(self):
        """Test that branch without condition raises error."""
        yaml_data = {
            "version": 1,
            "survey_id": "SV_test",
            "flow": [
                {
                    "type": "Branch",
                    "id": "FL_2",
                    # Missing 'condition' or 'raw_logic'
                    "then": [{"type": "Block", "id": "BL_then"}],
                }
            ],
        }

        with pytest.raises(FlowValidationError) as exc_info:
            validate_yaml_structure(yaml_data)

        assert "condition" in str(exc_info.value) or "raw_logic" in str(exc_info.value)

    def test_valid_branch_with_condition(self):
        """Test that branch with condition passes."""
        yaml_data = {
            "version": 1,
            "survey_id": "SV_test",
            "flow": [
                {
                    "type": "Branch",
                    "id": "FL_2",
                    "condition": {
                        "logic_type": "EmbeddedField",
                        "field": "arm",
                        "operator": "EqualTo",
                        "value": "A",
                    },
                    "then": [{"type": "Block", "id": "BL_then"}],
                }
            ],
        }

        # Should not raise
        validate_yaml_structure(yaml_data)

    def test_valid_branch_with_raw_logic(self):
        """Test that branch with raw_logic passes."""
        yaml_data = {
            "version": 1,
            "survey_id": "SV_test",
            "flow": [
                {
                    "type": "Branch",
                    "id": "FL_2",
                    "raw_logic": {"Type": "BooleanExpression"},
                    "then": [{"type": "Block", "id": "BL_then"}],
                }
            ],
        }

        # Should not raise
        validate_yaml_structure(yaml_data)
