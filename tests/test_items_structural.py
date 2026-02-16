import unittest
from unittest import mock


from qsync.dimensions import items_structural


class TestItemsStructuralHelpers(unittest.TestCase):
    def test_external_override_prompt_cached_per_session(self):
        items_structural._EXTERNAL_OVERRIDE_APPROVALS.clear()
        with mock.patch.object(
            items_structural,
            "external_owner_for",
            return_value="scripts/update_newsmem_recognition.py",
        ):
            with mock.patch.object(items_structural, "warn", return_value=None):
                with mock.patch.object(
                    items_structural, "confirm", return_value=True
                ) as confirm_mock:
                    items_structural._require_external_override_if_needed(
                        qid="QID15",
                        data_export_tag="newsmem_recognition",
                        interactive=True,
                        phase="edit",
                    )
                    items_structural._require_external_override_if_needed(
                        qid="QID15",
                        data_export_tag="newsmem_recognition",
                        interactive=True,
                        phase="edit",
                    )
        self.assertEqual(confirm_mock.call_count, 1)

    def test_iter_all_qids_sorted(self):
        survey = items_structural.SurveyCache(
            survey_id="SV_TEST",
            path=items_structural.Path("dummy.json"),
            payload={
                "result": {
                    "Questions": {
                        "QID3": {},
                        "QID1": {},
                        "QID2": {},
                    }
                }
            },
        )
        self.assertEqual(
            items_structural.iter_all_qids(survey), ["QID1", "QID2", "QID3"]
        )

    def test_iter_active_qids_in_flow_yields_question_ids_not_block_ids(self):
        survey = items_structural.SurveyCache(
            survey_id="SV_TEST",
            path=items_structural.Path("dummy.json"),
            payload={
                "result": {
                    "Questions": {
                        "QID1": {},
                        "QID2": {},
                    },
                    "Blocks": {
                        "BL_A": {
                            "Type": "Standard",
                            "BlockElements": [
                                {"Type": "Question", "QuestionID": "QID2"},
                                {"Type": "Question", "QuestionID": "QIDX_UNKNOWN"},
                            ],
                        },
                        "BL_B": {
                            "Type": "Standard",
                            "BlockElements": [
                                {"Type": "Question", "QuestionID": "QID1"},
                                {"Type": "Question", "QuestionID": "QID2"},
                            ],
                        },
                        "BL_TRASH": {
                            "Type": "Trash",
                            "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
                        },
                    },
                    "SurveyFlow": {
                        "Flow": [
                            {"Type": "Block", "ID": "BL_A"},
                            {"Type": "Branch", "Then": [{"Type": "Block", "ID": "BL_B"}]},
                            {"Type": "Block", "ID": "BL_TRASH"},
                        ]
                    },
                }
            },
        )
        self.assertEqual(
            list(items_structural.iter_active_qids_in_flow(survey)),
            ["QID2", "QID1"],
        )

    def test_wizard_manual_filter_selects_active_qid(self):
        qids = [f"QID{i}" for i in range(1, 36)]
        questions = {
            qid: {
                "QuestionText": f"<p>{qid} text</p>",
                "DataExportTag": f"TAG_{qid}",
                "Choices": {"1": {"Display": "A"}},
            }
            for qid in qids
        }
        survey = items_structural.SurveyCache(
            survey_id="SV_TEST",
            path=items_structural.Path("dummy.json"),
            payload={"result": {"Questions": questions}},
        )

        def _select_from_list(message, choices, instruction=None, default=None):
            if message.startswith("How do you want to select a QID?"):
                return "Search active by tag/text (manual filter)"
            if message.startswith("Select a QID to edit"):
                for choice in choices:
                    if str(choice).startswith("QID15 "):
                        return choice
                return choices[0]
            raise AssertionError(f"Unexpected select prompt: {message}")

        def _stop_on_qid(*, survey_id, qid):
            self.assertEqual(survey_id, "SV_TEST")
            self.assertEqual(qid, "QID15")
            raise items_structural.ItemsStructuralError("STOP_AFTER_QID")

        with mock.patch.object(items_structural, "load_cached_survey", return_value=survey):
            with mock.patch.object(items_structural, "iter_active_qids_in_flow", return_value=qids):
                with mock.patch.object(items_structural, "iter_all_qids", return_value=qids):
                    with mock.patch.object(items_structural, "select_from_list", side_effect=_select_from_list):
                        with mock.patch.object(items_structural, "text_input", return_value="QID15"):
                            with mock.patch.object(items_structural, "_qid_workbook_diffs", side_effect=_stop_on_qid):
                                with self.assertRaises(items_structural.ItemsStructuralError) as excinfo:
                                    items_structural.interactive_choice_wizard(
                                        survey_id="SV_TEST",
                                        qid=None,
                                        allow_delete=False,
                                        experimental_unsupported=False,
                                    )
        self.assertIn("STOP_AFTER_QID", str(excinfo.exception))

    def test_wizard_manual_filter_no_match_is_actionable(self):
        qids = [f"QID{i}" for i in range(1, 36)]
        questions = {
            qid: {
                "QuestionText": f"<p>{qid} text</p>",
                "DataExportTag": f"TAG_{qid}",
                "Choices": {"1": {"Display": "A"}},
            }
            for qid in qids
        }
        survey = items_structural.SurveyCache(
            survey_id="SV_TEST",
            path=items_structural.Path("dummy.json"),
            payload={"result": {"Questions": questions}},
        )

        def _select_from_list(message, choices, instruction=None, default=None):
            if message.startswith("How do you want to select a QID?"):
                return "Search active by tag/text (manual filter)"
            raise AssertionError(f"Unexpected select prompt: {message}")

        with mock.patch.object(items_structural, "load_cached_survey", return_value=survey):
            with mock.patch.object(items_structural, "iter_active_qids_in_flow", return_value=qids):
                with mock.patch.object(items_structural, "iter_all_qids", return_value=qids):
                    with mock.patch.object(items_structural, "select_from_list", side_effect=_select_from_list):
                        with mock.patch.object(items_structural, "text_input", return_value="NO_MATCH_999"):
                            with self.assertRaises(items_structural.ItemsStructuralError) as excinfo:
                                items_structural.interactive_choice_wizard(
                                    survey_id="SV_TEST",
                                    qid=None,
                                    allow_delete=False,
                                    experimental_unsupported=False,
                                )
        self.assertIn("No active questions matched that filter.", str(excinfo.exception))

    def test_wizard_uses_all_qids_when_no_active_flow(self):
        qids = ["QID1", "QID2", "QID3"]
        questions = {
            qid: {
                "QuestionText": f"<p>{qid} text</p>",
                "DataExportTag": f"TAG_{qid}",
                "Choices": {"1": {"Display": "A"}},
            }
            for qid in qids
        }
        survey = items_structural.SurveyCache(
            survey_id="SV_TEST",
            path=items_structural.Path("dummy.json"),
            payload={"result": {"Questions": questions}},
        )

        def _select_from_list(message, choices, instruction=None, default=None):
            if message.startswith("Select a QID to edit"):
                for choice in choices:
                    if str(choice).startswith("QID2 "):
                        return choice
                return choices[0]
            raise AssertionError(f"Unexpected select prompt: {message}")

        def _stop_on_qid(*, survey_id, qid):
            self.assertEqual(survey_id, "SV_TEST")
            self.assertEqual(qid, "QID2")
            raise items_structural.ItemsStructuralError("STOP_AFTER_QID")

        with mock.patch.object(items_structural, "load_cached_survey", return_value=survey):
            with mock.patch.object(
                items_structural,
                "iter_active_qids_in_flow",
                return_value=[],
            ):
                with mock.patch.object(
                    items_structural,
                    "iter_all_qids",
                    return_value=qids,
                ):
                    with mock.patch.object(
                        items_structural,
                        "select_from_list",
                        side_effect=_select_from_list,
                    ):
                        with mock.patch.object(
                            items_structural,
                            "_qid_workbook_diffs",
                            side_effect=_stop_on_qid,
                        ):
                            with self.assertRaises(items_structural.ItemsStructuralError) as excinfo:
                                items_structural.interactive_choice_wizard(
                                    survey_id="SV_TEST",
                                    qid=None,
                                    allow_delete=False,
                                    experimental_unsupported=False,
                                )
        self.assertIn("STOP_AFTER_QID", str(excinfo.exception))

    def test_allocate_choice_id_uses_nextchoiceid_and_bumps(self):
        q = {
            "Choices": {"1": {"Display": "A"}, "2": {"Display": "B"}},
            "ChoiceOrder": ["1", "2"],
            "NextChoiceId": 3,
        }
        cid = items_structural._allocate_choice_id(q)
        self.assertEqual(cid, "3")
        self.assertEqual(q["NextChoiceId"], 4)

    def test_allocate_choice_id_skips_existing(self):
        q = {
            "Choices": {"1": {}, "2": {}, "3": {}},
            "NextChoiceId": 3,
        }
        cid = items_structural._allocate_choice_id(q)
        self.assertEqual(cid, "4")
        self.assertEqual(q["NextChoiceId"], 5)

    def test_allocate_answer_id_uses_nextanswerid_and_bumps(self):
        q = {
            "Answers": {"1": {"Display": "A"}, "2": {"Display": "B"}},
            "AnswerOrder": ["1", "2"],
            "NextAnswerId": 3,
        }
        aid = items_structural._allocate_answer_id(q)
        self.assertEqual(aid, "3")
        self.assertEqual(q["NextAnswerId"], 4)

    def test_append_choice_order_preserves_int_list(self):
        q = {"ChoiceOrder": [1, 2]}
        items_structural._append_choice_order(q, "3")
        self.assertEqual(q["ChoiceOrder"], [1, 2, 3])

    def test_append_choice_order_preserves_string_list(self):
        q = {"ChoiceOrder": ["1", "2"]}
        items_structural._append_choice_order(q, "3")
        self.assertEqual(q["ChoiceOrder"], ["1", "2", "3"])

    def test_remove_choice_from_order(self):
        q = {"ChoiceOrder": ["1", "2", "3", "2"]}
        items_structural._remove_choice_from_order(q, "2")
        self.assertEqual(q["ChoiceOrder"], ["1", "3"])

    def test_cleanup_choice_translations_prunes_empty(self):
        q = {
            "Language": {
                "ES": {"Choices": {"1": {"Display": "Sí"}, "2": {"Display": "No"}}},
                "DE": {"Choices": {"2": {"Display": "Nein"}}},
            }
        }
        items_structural._cleanup_choice_translations(q, "2", ["ES", "DE"])
        self.assertEqual(q["Language"]["ES"]["Choices"], {"1": {"Display": "Sí"}})
        self.assertNotIn("DE", q["Language"])

    def test_cleanup_answer_translations_prunes_empty(self):
        q = {
            "Language": {
                "ES": {"Answers": {"1": {"Display": "Sí"}, "2": {"Display": "No"}}},
                "DE": {"Answers": {"2": {"Display": "Nein"}}},
            }
        }
        items_structural._cleanup_answer_translations(q, "2", ["ES", "DE"])
        self.assertEqual(q["Language"]["ES"]["Answers"], {"1": {"Display": "Sí"}})
        self.assertNotIn("DE", q["Language"])

    def test_summarize_structural_ops_counts(self):
        ops = [
            {"qid": "QID1", "op": "choice_add"},
            {"qid": "QID1", "op": "choice_edit"},
            {"qid": "QID2", "op": "choice_remove"},
            {"qid": "QID2", "op": "choice_remove"},
            {"qid": "QID2", "op": "answer_add"},
            {"qid": "QID3", "op": "unknown_op"},
        ]
        summary = items_structural.summarize_structural_ops(ops)
        self.assertEqual(summary["QID1"]["add"], 1)
        self.assertEqual(summary["QID1"]["edit"], 1)
        self.assertEqual(summary["QID2"]["add"], 1)
        self.assertEqual(summary["QID2"]["remove"], 2)
        self.assertEqual(summary["QID3"]["other"], 1)

    def test_push_structural_ops_requires_allow_delete_noninteractive(self):
        ops = [{"qid": "QID1", "op": "answer_remove", "answer_id": "1"}]
        with self.assertRaises(items_structural.ItemsStructuralError):
            items_structural.push_structural_ops(
                survey_id="SV_TEST",
                payload={},
                structural_ops=ops,
                push_journal={},
                interactive=False,
                allow_delete=False,
                force_live=False,
                force_preview=False,
                publish=False,
                dry_run=True,
                refresh_cache=False,
                save_journal_cb=lambda _: None,
            )

    def test_push_structural_ops_skips_already_pushed_qids(self):
        ops = [
            {"qid": "QID1", "op": "choice_edit", "choice_id": "1", "html": "<b>A</b>"},
            {"qid": "QID2", "op": "choice_edit", "choice_id": "1", "html": "<b>B</b>"},
        ]
        push_journal = {"pushed_qids": ["QID1"]}

        class DummySafeguardResult:
            blocked = False
            message = ""
            warnings = []

        class DummySurvey:
            def __init__(self):
                self.payload = {
                    "result": {
                        "Questions": {
                            "QID1": {"Choices": {"1": {"Display": "A"}}},
                            "QID2": {"Choices": {"1": {"Display": "B"}}},
                        }
                    }
                }

            @property
            def questions(self):
                return self.payload["result"]["Questions"]

            def save(self):
                return None

        pushed: list[list[str]] = []

        with mock.patch.object(
            items_structural,
            "enforce_push_safeguards",
            return_value=DummySafeguardResult(),
        ):
            with mock.patch.object(
                items_structural, "ensure_backup", return_value=None
            ):
                with mock.patch.object(
                    items_structural, "refresh_survey_cache", return_value=(None, False)
                ):
                    with mock.patch.object(
                        items_structural,
                        "load_cached_survey",
                        return_value=DummySurvey(),
                    ):
                        with mock.patch.object(
                            items_structural, "list_enabled_languages", return_value=[]
                        ):
                            with mock.patch.object(
                                items_structural, "info", return_value=None
                            ):
                                with mock.patch.object(
                                    items_structural, "warn", return_value=None
                                ):
                                    with mock.patch.object(
                                        items_structural,
                                        "log_push_event",
                                        return_value=None,
                                    ):
                                        with mock.patch.object(
                                            items_structural,
                                            "push_questions",
                                            side_effect=lambda _survey, qids, context=None: pushed.append(
                                                list(qids)
                                            ),
                                        ):
                                            items_structural.push_structural_ops(
                                                survey_id="SV_TEST",
                                                payload={},
                                                structural_ops=ops,
                                                push_journal=push_journal,
                                                interactive=False,
                                                allow_delete=True,
                                                force_live=False,
                                                force_preview=False,
                                                publish=False,
                                                dry_run=False,
                                                refresh_cache=False,
                                                save_journal_cb=lambda _: None,
                                            )

        self.assertEqual(pushed, [["QID2"]])

    def test_push_structural_ops_persists_journal_on_failure(self):
        ops = [
            {"qid": "QID1", "op": "choice_edit", "choice_id": "1", "html": "<b>A</b>"},
            {"qid": "QID2", "op": "choice_edit", "choice_id": "1", "html": "<b>B</b>"},
        ]
        push_journal: dict = {}
        saved: list[dict] = []

        class DummySafeguardResult:
            blocked = False
            message = ""
            warnings = []

        class DummySurvey:
            def __init__(self):
                self.payload = {
                    "result": {
                        "Questions": {
                            "QID1": {"Choices": {"1": {"Display": "A"}}},
                            "QID2": {"Choices": {"1": {"Display": "B"}}},
                        }
                    }
                }

            @property
            def questions(self):
                return self.payload["result"]["Questions"]

            def save(self):
                return None

        def save_cb(journal: dict):
            saved.append(dict(journal))

        def push_questions_stub(_survey, qids, context=None):
            if list(qids) == ["QID2"]:
                raise RuntimeError("boom")
            return None

        with mock.patch.object(
            items_structural,
            "enforce_push_safeguards",
            return_value=DummySafeguardResult(),
        ):
            with mock.patch.object(
                items_structural, "ensure_backup", return_value=None
            ):
                with mock.patch.object(
                    items_structural, "refresh_survey_cache", return_value=(None, False)
                ):
                    with mock.patch.object(
                        items_structural,
                        "load_cached_survey",
                        return_value=DummySurvey(),
                    ):
                        with mock.patch.object(
                            items_structural, "list_enabled_languages", return_value=[]
                        ):
                            with mock.patch.object(
                                items_structural, "info", return_value=None
                            ):
                                with mock.patch.object(
                                    items_structural, "warn", return_value=None
                                ):
                                    with mock.patch.object(
                                        items_structural,
                                        "log_push_event",
                                        return_value=None,
                                    ):
                                        with mock.patch.object(
                                            items_structural,
                                            "push_questions",
                                            side_effect=push_questions_stub,
                                        ):
                                            with self.assertRaises(
                                                items_structural.ItemsStructuralError
                                            ):
                                                items_structural.push_structural_ops(
                                                    survey_id="SV_TEST",
                                                    payload={},
                                                    structural_ops=ops,
                                                    push_journal=push_journal,
                                                    interactive=False,
                                                    allow_delete=True,
                                                    force_live=False,
                                                    force_preview=False,
                                                    publish=False,
                                                    dry_run=False,
                                                    refresh_cache=False,
                                                    save_journal_cb=save_cb,
                                                )

        # QID1 should be recorded before failure on QID2, and saved journal reflects that.
        self.assertIn({"pushed_qids": ["QID1"]}, saved)
        self.assertEqual(push_journal.get("pushed_qids"), ["QID1"])

    def test_push_structural_ops_applies_question_text_edit(self):
        ops = [
            {
                "qid": "QID1",
                "op": "question_text_edit",
                "html": "<strong>Hello</strong>",
            },
        ]
        push_journal: dict = {}

        class DummySafeguardResult:
            blocked = False
            message = ""
            warnings = []

        class DummySurvey:
            def __init__(self):
                self.payload = {
                    "result": {
                        "Questions": {
                            "QID1": {
                                "QuestionText": "Old",
                                "QuestionText_Unsafe": "Old",
                            },
                        }
                    }
                }

            @property
            def questions(self):
                return self.payload["result"]["Questions"]

            def save(self):
                return None

        survey = DummySurvey()

        def push_questions_stub(_survey, qids, context=None):
            self.assertEqual(list(qids), ["QID1"])
            self.assertEqual(
                _survey.questions["QID1"]["QuestionText"], "<strong>Hello</strong>"
            )
            self.assertEqual(
                _survey.questions["QID1"]["QuestionText_Unsafe"],
                "<strong>Hello</strong>",
            )
            return None

        with mock.patch.object(
            items_structural,
            "enforce_push_safeguards",
            return_value=DummySafeguardResult(),
        ):
            with mock.patch.object(
                items_structural, "ensure_backup", return_value=None
            ):
                with mock.patch.object(
                    items_structural, "refresh_survey_cache", return_value=(None, False)
                ):
                    with mock.patch.object(
                        items_structural, "load_cached_survey", return_value=survey
                    ):
                        with mock.patch.object(
                            items_structural, "list_enabled_languages", return_value=[]
                        ):
                            with mock.patch.object(
                                items_structural, "info", return_value=None
                            ):
                                with mock.patch.object(
                                    items_structural, "warn", return_value=None
                                ):
                                    with mock.patch.object(
                                        items_structural,
                                        "log_push_event",
                                        return_value=None,
                                    ):
                                        with mock.patch.object(
                                            items_structural,
                                            "push_questions",
                                            side_effect=push_questions_stub,
                                        ):
                                            items_structural.push_structural_ops(
                                                survey_id="SV_TEST",
                                                payload={},
                                                structural_ops=ops,
                                                push_journal=push_journal,
                                                interactive=False,
                                                allow_delete=True,
                                                force_live=False,
                                                force_preview=False,
                                                publish=False,
                                                dry_run=False,
                                                refresh_cache=False,
                                                save_journal_cb=lambda _: None,
                                            )


if __name__ == "__main__":
    unittest.main()
