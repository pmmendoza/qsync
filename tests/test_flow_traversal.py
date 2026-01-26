import unittest

from qsync.flow_traversal import FlowTraversalHandlers, walk_flow


def _embedded_field_logic(key: str, value: str) -> dict:
    return {
        "Type": "BooleanExpression",
        "0": {
            "Type": "If",
            "0": {
                "Type": "Expression",
                "LogicType": "EmbeddedField",
                "LeftOperand": key,
                "Operator": "EqualTo",
                "RightOperand": value,
            },
        },
    }


def _selected_logic(question_id: str) -> dict:
    return {
        "Type": "BooleanExpression",
        "0": {
            "Type": "If",
            "0": {
                "Type": "Expression",
                "LogicType": "Question",
                "Operator": "Selected",
                "QuestionID": question_id,
            },
        },
    }


class TestFlowTraversal(unittest.TestCase):
    def test_branch_decision_from_edf(self):
        flow_list = [
            {
                "Type": "Branch",
                "BranchLogic": _embedded_field_logic("DEBUG", "T"),
                "Flow": [{"Type": "Block", "ID": "B1"}],
                "ElseFlow": [{"Type": "Block", "ID": "B2"}],
            }
        ]

        decisions: list[tuple[bool, str]] = []
        visited: list[str] = []

        handlers = FlowTraversalHandlers(
            on_block=lambda n, d: visited.append(str(n.get("ID"))),
            on_branch_decision=lambda n, decision, reason, d: decisions.append(
                (bool(decision), reason)
            ),
        )

        walk_flow(
            flow_list=flow_list,
            handlers=handlers,
            edf_overrides={"DEBUG": "T"},
            asked_qids=set(),
        )

        self.assertEqual(decisions, [(True, "edf")])
        self.assertEqual(visited, ["B1"])

    def test_branch_decision_unasked_selected_false(self):
        flow_list = [
            {
                "Type": "Branch",
                "BranchLogic": _selected_logic("QID1"),
                "Flow": [{"Type": "Block", "ID": "B1"}],
                "ElseFlow": [{"Type": "Block", "ID": "B2"}],
            }
        ]

        decisions: list[tuple[bool, str]] = []
        visited: list[str] = []

        handlers = FlowTraversalHandlers(
            on_block=lambda n, d: visited.append(str(n.get("ID"))),
            on_branch_decision=lambda n, decision, reason, d: decisions.append(
                (bool(decision), reason)
            ),
        )

        walk_flow(
            flow_list=flow_list,
            handlers=handlers,
            edf_overrides={"DEBUG": "T"},
            asked_qids=set(),
        )

        self.assertEqual(decisions, [(False, "unasked_selected")])
        self.assertEqual(visited, ["B2"])


if __name__ == "__main__":
    unittest.main()
