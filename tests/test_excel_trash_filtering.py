"""Test that Trash block questions are properly filtered from all Excel sheets."""

from qsync.excel_io import (
    build_option_rows,
    build_question_rows,
    build_subitem_rows,
)


def test_trash_questions_excluded_from_all_sheets():
    """Verify that questions in Trash blocks are excluded from Questions, Options, and Subitems sheets."""

    # Create a minimal survey payload with both Standard and Trash blocks
    survey_payload = {
        "result": {
            "SurveyID": "SV_TEST",
            "Blocks": {
                "BL_STANDARD": {
                    "Type": "Standard",
                    "Description": "Main Questions",
                    "BlockElements": [
                        {"Type": "Question", "QuestionID": "QID1"},
                        {"Type": "Question", "QuestionID": "QID2"},
                    ],
                },
                "BL_TRASH": {
                    "Type": "Trash",
                    "Description": "Trash / Unused Questions",
                    "BlockElements": [
                        {"Type": "Question", "QuestionID": "QID_TRASH_1"},
                        {"Type": "Question", "QuestionID": "QID_TRASH_2"},
                    ],
                },
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "MC",
                    "DataExportTag": "Q1",
                    "QuestionText": "Active Question 1",
                    "Choices": {
                        "1": {"Display": "Choice 1"},
                        "2": {"Display": "Choice 2"},
                    },
                },
                "QID2": {
                    "QuestionType": "Matrix",
                    "DataExportTag": "Q2",
                    "QuestionText": "Active Matrix Question",
                    "Choices": {
                        "1": {"Display": "Row 1"},
                        "2": {"Display": "Row 2"},
                    },
                    "Answers": {
                        "1": {"Display": "Strongly Disagree"},
                        "2": {"Display": "Disagree"},
                        "3": {"Display": "Agree"},
                    },
                },
                "QID_TRASH_1": {
                    "QuestionType": "MC",
                    "DataExportTag": "TRASH_Q1",
                    "QuestionText": "Trashed Question 1",
                    "Choices": {
                        "1": {"Display": "Trash Choice 1"},
                        "2": {"Display": "Trash Choice 2"},
                        "3": {"Display": "Trash Choice 3"},
                    },
                },
                "QID_TRASH_2": {
                    "QuestionType": "Matrix",
                    "DataExportTag": "TRASH_Q2",
                    "QuestionText": "Trashed Matrix Question",
                    "Choices": {
                        "1": {"Display": "Trash Row 1"},
                    },
                    "Answers": {
                        "1": {"Display": "Trash Answer 1"},
                        "2": {"Display": "Trash Answer 2"},
                    },
                },
            },
            "SurveyFlow": {
                "Flow": [
                    {"ID": "BL_STANDARD", "Type": "Block"},
                ],
            },
        }
    }

    # Build all sheets
    question_rows = build_question_rows("SV_TEST", survey_payload)
    option_rows = build_option_rows("SV_TEST", survey_payload)
    subitem_rows = build_subitem_rows("SV_TEST", survey_payload)

    # Verify Questions sheet includes only non-Trash questions
    assert "QID1" in question_rows, "Active question QID1 should be in Questions sheet"
    assert "QID2" in question_rows, "Active question QID2 should be in Questions sheet"
    assert (
        "QID_TRASH_1" not in question_rows
    ), "Trash question QID_TRASH_1 should NOT be in Questions sheet"
    assert (
        "QID_TRASH_2" not in question_rows
    ), "Trash question QID_TRASH_2 should NOT be in Questions sheet"

    # Verify Options sheet includes only options for non-Trash questions
    option_qids = {qid for qid, _ in option_rows.keys()}

    assert "QID1" in option_qids, "Options for QID1 should be present"
    assert "QID2" in option_qids, "Options for QID2 (Matrix answers) should be present"
    assert (
        "QID_TRASH_1" not in option_qids
    ), "Options for QID_TRASH_1 should NOT be present"
    assert (
        "QID_TRASH_2" not in option_qids
    ), "Options for QID_TRASH_2 should NOT be present"

    # Count options for active questions
    qid1_options = [key for key in option_rows.keys() if key[0] == "QID1"]
    qid2_options = [key for key in option_rows.keys() if key[0] == "QID2"]

    assert len(qid1_options) == 2, "QID1 should have 2 choices"
    assert len(qid2_options) == 3, "QID2 Matrix should have 3 answers"

    # Verify Subitems sheet includes only subitems for non-Trash questions
    subitem_qids = {qid for qid, _, _ in subitem_rows.keys()}

    assert "QID2" in subitem_qids, "Subitems for QID2 (Matrix rows) should be present"
    assert (
        "QID_TRASH_2" not in subitem_qids
    ), "Subitems for QID_TRASH_2 should NOT be present"

    # Count subitems for active Matrix question
    qid2_subitems = [key for key in subitem_rows.keys() if key[0] == "QID2"]
    assert len(qid2_subitems) == 2, "QID2 Matrix should have 2 subitems (rows)"

    # Verify total counts
    assert len(question_rows) == 2, "Should have exactly 2 questions (not 4)"

    # Options: QID1 has 2 choices + QID2 has 3 answers = 5 total
    assert (
        len(option_rows) == 5
    ), "Should have 5 option rows (not including trash questions)"

    # Subitems: QID2 has 2 matrix rows
    assert (
        len(subitem_rows) == 2
    ), "Should have 2 subitem rows (not including trash questions)"


def test_trash_filtering_with_no_trash_blocks():
    """Verify that filtering works correctly when there are no Trash blocks."""

    survey_payload = {
        "result": {
            "SurveyID": "SV_TEST2",
            "Blocks": {
                "BL_STANDARD": {
                    "Type": "Standard",
                    "Description": "Main Questions",
                    "BlockElements": [
                        {"Type": "Question", "QuestionID": "QID1"},
                    ],
                },
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "MC",
                    "DataExportTag": "Q1",
                    "QuestionText": "Question 1",
                    "Choices": {
                        "1": {"Display": "Choice 1"},
                    },
                },
            },
            "SurveyFlow": {
                "Flow": [
                    {"ID": "BL_STANDARD", "Type": "Block"},
                ],
            },
        }
    }

    question_rows = build_question_rows("SV_TEST2", survey_payload)
    option_rows = build_option_rows("SV_TEST2", survey_payload)

    assert len(question_rows) == 1, "Should have 1 question"
    assert len(option_rows) == 1, "Should have 1 option"
    assert "QID1" in question_rows


def test_trash_filtering_empty_survey():
    """Verify that filtering works correctly with an empty survey."""

    survey_payload = {
        "result": {
            "SurveyID": "SV_EMPTY",
            "Blocks": {},
            "Questions": {},
            "SurveyFlow": {"Flow": []},
        }
    }

    question_rows = build_question_rows("SV_EMPTY", survey_payload)
    option_rows = build_option_rows("SV_EMPTY", survey_payload)
    subitem_rows = build_subitem_rows("SV_EMPTY", survey_payload)

    assert len(question_rows) == 0
    assert len(option_rows) == 0
    assert len(subitem_rows) == 0


def test_referential_integrity():
    """Verify that all QIDs in Options and Subitems sheets exist in Questions sheet."""

    survey_payload = {
        "result": {
            "SurveyID": "SV_INTEGRITY",
            "Blocks": {
                "BL_1": {
                    "Type": "Standard",
                    "Description": "Block 1",
                    "BlockElements": [
                        {"Type": "Question", "QuestionID": "QID1"},
                        {"Type": "Question", "QuestionID": "QID2"},
                    ],
                },
                "BL_TRASH": {
                    "Type": "Trash",
                    "Description": "Trash",
                    "BlockElements": [
                        {"Type": "Question", "QuestionID": "QID_TRASH"},
                    ],
                },
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "MC",
                    "QuestionText": "Q1",
                    "Choices": {"1": {"Display": "C1"}},
                },
                "QID2": {
                    "QuestionType": "Matrix",
                    "QuestionText": "Q2",
                    "Choices": {"1": {"Display": "R1"}},
                    "Answers": {"1": {"Display": "A1"}},
                },
                "QID_TRASH": {
                    "QuestionType": "MC",
                    "QuestionText": "Trash",
                    "Choices": {"1": {"Display": "TC1"}, "2": {"Display": "TC2"}},
                },
            },
            "SurveyFlow": {"Flow": [{"ID": "BL_1", "Type": "Block"}]},
        }
    }

    question_rows = build_question_rows("SV_INTEGRITY", survey_payload)
    option_rows = build_option_rows("SV_INTEGRITY", survey_payload)
    subitem_rows = build_subitem_rows("SV_INTEGRITY", survey_payload)

    question_qids = set(question_rows.keys())
    option_qids = {qid for qid, _ in option_rows.keys()}
    subitem_qids = {qid for qid, _, _ in subitem_rows.keys()}

    # All QIDs in Options must exist in Questions
    for qid in option_qids:
        assert qid in question_qids, f"Option QID {qid} not found in Questions sheet"

    # All QIDs in Subitems must exist in Questions
    for qid in subitem_qids:
        assert qid in question_qids, f"Subitem QID {qid} not found in Questions sheet"

    # Verify trash question is not in any sheet
    assert "QID_TRASH" not in question_qids
    assert "QID_TRASH" not in option_qids
    assert "QID_TRASH" not in subitem_qids
