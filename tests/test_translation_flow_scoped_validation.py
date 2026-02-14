import unittest


class TranslationFlowScopedValidationTests(unittest.TestCase):
    def test_active_qids_in_flow_excludes_trash_blocks(self) -> None:
        from qsync.translation_export import active_qids_in_flow

        payload = {
            "result": {
                "Questions": {
                    "QID1": {"QuestionID": "QID1", "QuestionType": "TE"},
                    "QID2": {"QuestionID": "QID2", "QuestionType": "TE"},
                },
                "Blocks": {
                    "BL_FLOW": {
                        "Type": "Standard",
                        "ID": "BL_FLOW",
                        "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
                    },
                    "BL_TRASH": {
                        "Type": "Trash",
                        "ID": "BL_TRASH",
                        "BlockElements": [{"Type": "Question", "QuestionID": "QID2"}],
                    },
                },
                "SurveyFlow": {
                    "Flow": [
                        {"Type": "Block", "ID": "BL_FLOW"},
                        {"Type": "Block", "ID": "BL_TRASH"},
                    ]
                },
            }
        }

        self.assertEqual(active_qids_in_flow(payload), {"QID1"})

    def test_expected_keys_and_scoping_drop_out_of_flow_questions(self) -> None:
        from qsync.translation_export import (
            active_qids_in_flow,
            expected_translation_keys_for_qids,
        )

        payload = {
            "result": {
                "Questions": {
                    "QID1": {
                        "QuestionID": "QID1",
                        "QuestionType": "TE",
                        "Choices": {"1": {"Display": "A"}, "2": {"Display": "B"}},
                    },
                    "QID2": {
                        "QuestionID": "QID2",
                        "QuestionType": "TE",
                        "Choices": {"1": {"Display": "C"}},
                    },
                },
                "Blocks": {
                    "BL_FLOW": {
                        "Type": "Standard",
                        "ID": "BL_FLOW",
                        "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
                    },
                    "BL_TRASH": {
                        "Type": "Trash",
                        "ID": "BL_TRASH",
                        "BlockElements": [{"Type": "Question", "QuestionID": "QID2"}],
                    },
                },
                "SurveyFlow": {"Flow": [{"Type": "Block", "ID": "BL_FLOW"}]},
            }
        }

        active = active_qids_in_flow(payload)
        expected = expected_translation_keys_for_qids(payload, qids=active)
        expected_set = set(expected)

        self.assertIn("QID1_QuestionText", expected_set)
        self.assertIn("QID1_Choice1", expected_set)
        self.assertIn("QID1_Choice2", expected_set)

        # Out-of-flow/trash question should not contribute to expected keys.
        self.assertNotIn("QID2_QuestionText", expected_set)
        self.assertNotIn("QID2_Choice1", expected_set)

        # Simulate coverage check input: out-of-flow keys may exist in the map, but
        # scoping to `expected` should hide them from validation.
        target_full = {
            "QID1_QuestionText": "Bonjour",
            "QID1_Choice1": "A",
            "QID1_Choice2": "B",
            "QID2_QuestionText": "",
            "QID2_Choice1": "",
        }
        base_full = {
            "QID1_QuestionText": "Hello",
            "QID1_Choice1": "A",
            "QID1_Choice2": "B",
        }
        scoped_target = {k: target_full.get(k, "") for k in expected}
        allowed_empty = {
            k for k, v in base_full.items() if not isinstance(v, str) or not v.strip()
        }
        empties = [
            k
            for k, v in scoped_target.items()
            if not str(v or "").strip() and k not in allowed_empty
        ]
        self.assertEqual(empties, [])

