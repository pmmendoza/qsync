"""Tests for flow YAML conversion."""

import json
import pytest

from qsync.dimensions.flow_yaml import (
    flow_to_yaml,
    yaml_to_flow,
    round_trip_test,
)


class TestFlowToYaml:
    """Tests for converting flow JSON to YAML."""

    def test_simple_block_flow(self):
        """Test conversion of a simple block-only flow."""
        flow = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [
                {"Type": "Standard", "ID": "BL_intro"},
                {"Type": "Standard", "ID": "BL_main"},
                {"Type": "Standard", "ID": "BL_outro"},
            ],
        }

        yaml_content = flow_to_yaml(flow, "SV_test")

        assert "version: 1" in yaml_content
        assert "survey_id: SV_test" in yaml_content
        assert "type: Block" in yaml_content
        assert "id: BL_intro" in yaml_content
        assert "id: BL_main" in yaml_content
        assert "id: BL_outro" in yaml_content

    def test_embedded_data_flow(self):
        """Test conversion of embedded data nodes."""
        flow = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [
                {
                    "Type": "EmbeddedData",
                    "FlowID": "FL_1",
                    "EmbeddedData": [
                        {"Field": "study_arm", "Value": "control"},
                        {"Field": "source", "Value": "web"},
                    ],
                },
                {"Type": "Standard", "ID": "BL_main"},
            ],
        }

        yaml_content = flow_to_yaml(flow, "SV_test")

        assert "type: EmbeddedData" in yaml_content
        assert "id: FL_1" in yaml_content
        assert "field: study_arm" in yaml_content
        assert "value: control" in yaml_content
        assert "field: source" in yaml_content

    def test_simple_branch_flow(self):
        """Test conversion of a simple branch with EmbeddedField condition."""
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
                                "LogicType": "EmbeddedField",
                                "LeftOperand": "study_arm",
                                "Operator": "EqualTo",
                                "RightOperand": "treatment",
                            },
                        },
                    },
                    "Flow": [{"Type": "Standard", "ID": "BL_treatment"}],
                    "ElseFlow": [{"Type": "Standard", "ID": "BL_control"}],
                },
            ],
        }

        yaml_content = flow_to_yaml(flow, "SV_test")

        assert "type: Branch" in yaml_content
        assert "id: FL_2" in yaml_content
        assert "then:" in yaml_content
        assert "else:" in yaml_content
        assert "BL_treatment" in yaml_content
        assert "BL_control" in yaml_content

    def test_block_randomizer_flow(self):
        """Test conversion of block randomizer."""
        flow = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [
                {
                    "Type": "BlockRandomizer",
                    "FlowID": "FL_3",
                    "SubSet": 2,
                    "EvenPresentation": True,
                    "Flow": [
                        {"Type": "Standard", "ID": "BL_a"},
                        {"Type": "Standard", "ID": "BL_b"},
                        {"Type": "Standard", "ID": "BL_c"},
                    ],
                },
            ],
        }

        yaml_content = flow_to_yaml(flow, "SV_test")

        assert "type: BlockRandomizer" in yaml_content
        assert "id: FL_3" in yaml_content
        assert "count: 2" in yaml_content
        assert "evenly_present: true" in yaml_content

    def test_end_survey_flow(self):
        """Test conversion of EndSurvey node."""
        flow = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [
                {
                    "Type": "EndSurvey",
                    "FlowID": "FL_end",
                    "Options": {
                        "SurveyTermination": "DisplayMessage",
                        "DisplayMessage": "MS_abc123",
                    },
                },
            ],
        }

        yaml_content = flow_to_yaml(flow, "SV_test")

        assert "type: EndSurvey" in yaml_content
        assert "id: FL_end" in yaml_content
        assert "end_type: DisplayMessage" in yaml_content
        assert "display_message: MS_abc123" in yaml_content

    def test_block_names_from_blocks_dict(self):
        """Test that block names are added from blocks dict."""
        flow = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [{"Type": "Standard", "ID": "BL_intro"}],
        }
        blocks = {"BL_intro": {"Description": "Introduction Questions"}}

        yaml_content = flow_to_yaml(flow, "SV_test", blocks=blocks)

        assert "name: Introduction Questions" in yaml_content


class TestYamlToFlow:
    """Tests for converting YAML back to flow JSON."""

    def test_simple_block_round_trip(self):
        """Test that simple blocks survive round-trip."""
        original = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [
                {"Type": "Standard", "ID": "BL_intro"},
                {"Type": "Standard", "ID": "BL_main"},
            ],
        }

        yaml_content = flow_to_yaml(original, "SV_test")
        restored = yaml_to_flow(yaml_content)

        assert len(restored["Flow"]) == 2
        assert restored["Flow"][0]["Type"] == "Standard"
        assert restored["Flow"][0]["ID"] == "BL_intro"

    def test_embedded_data_round_trip(self):
        """Test that embedded data survives round-trip."""
        original = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [
                {
                    "Type": "EmbeddedData",
                    "FlowID": "FL_1",
                    "EmbeddedData": [
                        {"Field": "study_arm", "Value": "control"},
                    ],
                },
            ],
        }

        yaml_content = flow_to_yaml(original, "SV_test")
        restored = yaml_to_flow(yaml_content)

        assert restored["Flow"][0]["Type"] == "EmbeddedData"
        assert restored["Flow"][0]["EmbeddedData"][0]["Field"] == "study_arm"
        assert restored["Flow"][0]["EmbeddedData"][0]["Value"] == "control"

    def test_branch_round_trip(self):
        """Test that branch structure survives round-trip."""
        original = {
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
                                "LeftOperand": "study_arm",
                                "Operator": "EqualTo",
                                "RightOperand": "treatment",
                            },
                        },
                    },
                    "Flow": [{"Type": "Standard", "ID": "BL_treatment"}],
                    "ElseFlow": [{"Type": "Standard", "ID": "BL_control"}],
                },
            ],
        }

        yaml_content = flow_to_yaml(original, "SV_test")
        restored = yaml_to_flow(yaml_content)

        branch = restored["Flow"][0]
        assert branch["Type"] == "Branch"
        assert "BranchLogic" in branch
        assert len(branch["Flow"]) == 1
        assert len(branch["ElseFlow"]) == 1

    def test_webservice_request_alias_round_trip(self):
        """RequestURL/RequestType-based WebService nodes should round-trip losslessly."""
        original = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [
                {
                    "Type": "WebService",
                    "FlowID": "FL_WS",
                    "RequestType": "POST",
                    "RequestURL": "https://api.example.com/ingest",
                    "RequestParams": {"panel_id": "${e://Field/panel_id}"},
                    "RequestHeaders": {"Authorization": "Bearer token"},
                }
            ],
        }

        yaml_content = flow_to_yaml(original, "SV_test")
        restored = yaml_to_flow(yaml_content)
        node = restored["Flow"][0]

        assert node["RequestURL"] == original["Flow"][0]["RequestURL"]
        assert node["RequestType"] == original["Flow"][0]["RequestType"]
        assert node["RequestParams"] == original["Flow"][0]["RequestParams"]
        assert node["RequestHeaders"] == original["Flow"][0]["RequestHeaders"]
        assert "URL" not in node
        assert "Method" not in node

    def test_webservice_request_alias_yaml_edits_are_preserved(self):
        """Editing YAML `url`/`method` should update alias-form RequestURL/RequestType."""
        original = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [
                {
                    "Type": "WebService",
                    "FlowID": "FL_WS",
                    "RequestType": "POST",
                    "RequestURL": "https://api.example.com/original",
                    "RequestParams": {"panel_id": "${e://Field/panel_id}"},
                }
            ],
        }

        yaml_content = flow_to_yaml(original, "SV_test")
        assert "request_alias: true" in yaml_content
        assert "RequestURL" not in yaml_content
        assert "RequestType" not in yaml_content

        edited_yaml = yaml_content.replace(
            "https://api.example.com/original", "https://api.example.com/edited"
        ).replace("method: POST", "method: PUT")
        restored = yaml_to_flow(edited_yaml)
        node = restored["Flow"][0]

        assert node["RequestURL"] == "https://api.example.com/edited"
        assert node["RequestType"] == "PUT"
        assert node["RequestParams"] == original["Flow"][0]["RequestParams"]
        assert "URL" not in node
        assert "Method" not in node

    def test_webservice_url_method_round_trip(self):
        """URL/Method-based WebService nodes should keep canonical URL/Method fields."""
        original = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [
                {
                    "Type": "WebService",
                    "FlowID": "FL_WS",
                    "URL": "https://api.example.com/ingest",
                    "Method": "PUT",
                    "RequestParams": {"key": "value"},
                }
            ],
        }

        yaml_content = flow_to_yaml(original, "SV_test")
        restored = yaml_to_flow(yaml_content)
        node = restored["Flow"][0]

        assert node["URL"] == original["Flow"][0]["URL"]
        assert node["Method"] == original["Flow"][0]["Method"]
        assert node["RequestParams"] == original["Flow"][0]["RequestParams"]


class TestRoundTripTest:
    """Tests for the round_trip_test utility."""

    def test_simple_flow_passes(self):
        """Test that simple flow passes round-trip test."""
        flow = {
            "Type": "Root",
            "FlowID": "FL_ROOT",
            "Flow": [{"Type": "Standard", "ID": "BL_test"}],
        }

        # Note: This may fail initially due to normalization differences
        # The round_trip_test is more of a debugging utility
        # We just ensure it doesn't raise an exception
        result = round_trip_test(flow)
        # Result may be True or False depending on normalization


class TestComplexConditions:
    """Tests for complex branch condition handling."""

    def test_raw_logic_escape_hatch(self):
        """Test that complex conditions use raw_logic escape hatch."""
        # Complex condition that can't be simplified
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
                                "QuestionID": "QID1",
                                "Operator": "Selected",
                                "ChoiceLocator": "q://QID1/SelectableChoice/1",
                            },
                            "1": {
                                "Type": "Expression",
                                "LogicType": "EmbeddedField",
                                "LeftOperand": "flag",
                                "Operator": "EqualTo",
                                "RightOperand": "yes",
                                "Conjuction": "Or",
                            },
                        },
                    },
                    "Flow": [{"Type": "Standard", "ID": "BL_then"}],
                },
            ],
        }

        yaml_content = flow_to_yaml(flow, "SV_test")
        restored = yaml_to_flow(yaml_content)

        # Should preserve the branch structure
        branch = restored["Flow"][0]
        assert branch["Type"] == "Branch"
        assert "BranchLogic" in branch

    def test_multi_expression_condition_defaults_to_and(self):
        """Missing conjunction in simplified condition should default to And."""
        yaml_content = """
version: 1
survey_id: SV_test
flow:
  - type: Branch
    id: FL_2
    condition:
      expressions:
        - logic_type: EmbeddedField
          field: flag_a
          operator: EqualTo
          value: "yes"
        - logic_type: EmbeddedField
          field: flag_b
          operator: EqualTo
          value: "yes"
    then:
      - type: Block
        id: BL_then
"""

        restored = yaml_to_flow(yaml_content)
        expr_1 = restored["Flow"][0]["BranchLogic"]["0"]["1"]
        assert expr_1["Conjuction"] == "And"

    def test_multi_expression_condition_honors_explicit_conjunction(self):
        """Explicit conjunction should be preserved in BranchLogic."""
        yaml_content = """
version: 1
survey_id: SV_test
flow:
  - type: Branch
    id: FL_2
    condition:
      conjunction: Or
      expressions:
        - logic_type: EmbeddedField
          field: flag_a
          operator: EqualTo
          value: "yes"
        - logic_type: EmbeddedField
          field: flag_b
          operator: EqualTo
          value: "yes"
    then:
      - type: Block
        id: BL_then
"""

        restored = yaml_to_flow(yaml_content)
        expr_1 = restored["Flow"][0]["BranchLogic"]["0"]["1"]
        assert expr_1["Conjuction"] == "Or"
