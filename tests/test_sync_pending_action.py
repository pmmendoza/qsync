import io
import json
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import call, patch

from qsync.dimensions.types import DimensionChanges
from qsync.pending_stage import (
    EosPendingPayload,
    ItemsPendingPayload,
    JsPendingPayload,
    TranslationsPendingPayload,
)


class TestSyncPendingAction(unittest.TestCase):
    @staticmethod
    def _record(payload):
        return SimpleNamespace(payload=payload)

    @staticmethod
    def _changes_with_edf_warning() -> SimpleNamespace:
        return SimpleNamespace(
            survey_name="Test Survey",
            dimensions={
                "items": DimensionChanges("items", False, "No changes", set()),
                "edf": DimensionChanges(
                    "edf",
                    False,
                    "No changes",
                    set(),
                    warning_detail="Embedded_Data worksheet is inconsistent.",
                    safe_to_autofix=True,
                ),
                "js": DimensionChanges("js", False, "No changes", set()),
                "translations": DimensionChanges(
                    "translations", False, "No changes", set()
                ),
                "eos": DimensionChanges("eos", False, "No changes", set()),
            },
        )

    @staticmethod
    def _empty_unstaged() -> dict[str, DimensionChanges]:
        return {
            "items": DimensionChanges("items", False, "No changes", set()),
            "edf": DimensionChanges("edf", False, "No changes", set()),
            "js": DimensionChanges("js", False, "No changes", set()),
            "translations": DimensionChanges("translations", False, "No changes", set()),
            "eos": DimensionChanges("eos", False, "No changes", set()),
            "flow": DimensionChanges("flow", False, "No changes", set()),
            "master": DimensionChanges("master", False, "No changes", set()),
        }

    def test_pending_action_push_calls_sync_with_prefer_pending(self):
        import qsync.sync_orchestrator as orchestrator

        class DummyChanges:
            survey_name = None
            dimensions = {}

        captured: dict[str, object] = {}

        def _fake_sync_dimensions_once(survey_id, dimensions, **kwargs):
            captured["survey_id"] = survey_id
            captured["dimensions"] = list(dimensions)
            captured["prefer_pending"] = kwargs.get("prefer_pending")
            return None

        with (
            patch.object(
                orchestrator, "detect_survey_changes", return_value=DummyChanges()
            ),
            patch.object(
                orchestrator, "list_pending", return_value={"items": object()}
            ),
            patch.object(
                orchestrator,
                "_sync_dimensions_once",
                side_effect=_fake_sync_dimensions_once,
            ),
        ):
            orchestrator.sync_survey(
                survey_id="SV_TEST",
                interactive=False,
                auto_yes=True,
                pending_action="push",
            )

        self.assertEqual(captured.get("survey_id"), "SV_TEST")
        self.assertEqual(captured.get("dimensions"), ["items"])
        self.assertTrue(captured.get("prefer_pending"))

    def test_pending_action_abort_raises(self):
        import qsync.sync_orchestrator as orchestrator

        class DummyChanges:
            survey_name = None
            dimensions = {}

        with (
            patch.object(
                orchestrator, "detect_survey_changes", return_value=DummyChanges()
            ),
            patch.object(
                orchestrator, "list_pending", return_value={"items": object()}
            ),
        ):
            with self.assertRaises(SystemExit) as exc:
                orchestrator.sync_survey(
                    survey_id="SV_TEST",
                    interactive=False,
                    auto_yes=True,
                    pending_action="abort",
                )
        message = str(exc.exception)
        self.assertIn("Pending staged changes detected", message)
        self.assertIn(
            "qsync sync --survey-id SV_TEST --yes --pending-action push", message
        )
        self.assertIn("qsync items push --survey-id SV_TEST --yes", message)

    def test_guidance_items_pending_only(self):
        import qsync.sync_orchestrator as orchestrator

        pending = {
            "items": self._record(
                ItemsPendingPayload(
                    qids=["QID1"],
                    embedded_fields=[],
                    structural_ops=[],
                    structural_summary={},
                )
            )
        }
        message, payload = orchestrator._build_pending_abort_guidance(
            survey_id="SV_TEST",
            pending=pending,
            force_live=False,
            force_preview=False,
            scope_expr=None,
        )

        self.assertIn("items: staged:", message)
        self.assertIn("edf: none", message)
        self.assertIn("js: none", message)
        self.assertIn("translations: none", message)
        self.assertIn("eos: none", message)
        self.assertIn(payload["next_commands"]["interactive_review"], message)
        self.assertIn(payload["next_commands"]["push_all"], message)
        self.assertIn(payload["next_commands"]["discard_all"], message)
        self.assertIn(payload["next_commands"]["pending_inspect"], message)
        self.assertIn(payload["next_commands"]["push_by_dimension"]["items"], message)
        self.assertNotIn("qsync js push", message)
        self.assertNotIn("qsync translations push", message)
        self.assertNotIn("qsync eos push", message)

    def test_guidance_translations_pending_only(self):
        import qsync.sync_orchestrator as orchestrator

        pending = {
            "translations": self._record(
                TranslationsPendingPayload(qids=["Q1"], languages=["FR"])
            )
        }
        message, payload = orchestrator._build_pending_abort_guidance(
            survey_id="SV_TEST",
            pending=pending,
            force_live=False,
            force_preview=False,
            scope_expr=None,
        )

        self.assertIn("translations: staged:", message)
        self.assertIn(
            payload["next_commands"]["push_by_dimension"]["translations"], message
        )
        self.assertNotIn("qsync items push", message)
        self.assertNotIn("qsync js push", message)
        self.assertNotIn("qsync eos push", message)

    def test_guidance_mixed_pending(self):
        import qsync.sync_orchestrator as orchestrator

        pending = {
            "items": self._record(ItemsPendingPayload(qids=["Q1"])),
            "js": self._record(JsPendingPayload(entries=[{"qid": "Q1"}])),
            "translations": self._record(
                TranslationsPendingPayload(qids=["Q1"], languages=["FR"])
            ),
            "eos": self._record(EosPendingPayload(operations=[])),
        }
        message, _ = orchestrator._build_pending_abort_guidance(
            survey_id="SV_TEST",
            pending=pending,
            force_live=False,
            force_preview=False,
            scope_expr=None,
        )

        self.assertIn("qsync items push --survey-id SV_TEST --yes", message)
        self.assertIn("qsync js push --survey-id SV_TEST --yes", message)
        self.assertIn("qsync translations push --survey-id SV_TEST --yes", message)
        self.assertIn("qsync eos push --survey-id SV_TEST --yes", message)

    def test_guidance_forwards_force_and_scope(self):
        import qsync.sync_orchestrator as orchestrator

        pending = {
            "items": self._record(ItemsPendingPayload(qids=["Q1"])),
        }
        message, payload = orchestrator._build_pending_abort_guidance(
            survey_id="SV_TEST",
            pending=pending,
            force_live=True,
            force_preview=True,
            scope_expr="qid:Q1 OR tag:baseline",
        )

        push_all = payload["next_commands"]["push_all"]
        push_dim = payload["next_commands"]["push_by_dimension"]["items"]
        for cmd in (push_all, push_dim):
            self.assertIn("--force-live", cmd)
            self.assertIn("--force-preview", cmd)
            self.assertIn("--scope 'qid:Q1 OR tag:baseline'", cmd)
        self.assertIn("--scope 'qid:Q1 OR tag:baseline'", message)

    def test_pending_action_abort_json(self):
        import qsync.sync_orchestrator as orchestrator

        class DummyChanges:
            survey_name = None
            dimensions = {}

        with (
            patch.object(
                orchestrator, "detect_survey_changes", return_value=DummyChanges()
            ),
            patch.object(
                orchestrator,
                "list_pending",
                return_value={"items": self._record(ItemsPendingPayload(qids=["Q1"]))},
            ),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                with self.assertRaises(SystemExit) as exc:
                    orchestrator.sync_survey(
                        survey_id="SV_TEST",
                        interactive=False,
                        auto_yes=True,
                        pending_action="abort",
                        json_output=True,
                    )

        self.assertEqual(exc.exception.code, 1)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["pending_dims"], ["items"])
        self.assertIn("next_commands", payload)

    def test_interactive_pending_push_retry_force_live(self):
        import qsync.sync_orchestrator as orchestrator

        failed = orchestrator.DimensionSyncResult(
            dimension="eos",
            success=False,
            applied_changes=False,
            error_message=(
                "[qsync:eos] SV_TEST has 8 finished response(s). "
                "Re-run with --force-live after double-checking the diffs."
            ),
        )
        succeeded = orchestrator.DimensionSyncResult(
            dimension="eos",
            success=True,
            applied_changes=True,
            error_message=None,
        )

        with (
            patch(
                "qsync.interactive_menu.select_from_list",
                return_value="🚀 Push staged changes now",
            ),
            patch("qsync.interactive_menu.confirm", return_value=True) as mock_confirm,
            patch.object(
                orchestrator,
                "_get_inventory_cached",
                return_value={"name": "Test Survey"},
            ),
            patch.object(orchestrator, "_display_push_report"),
            patch.object(orchestrator, "_orchestrated_publish"),
            patch.object(
                orchestrator, "sync_dimension", side_effect=[failed, succeeded]
            ) as mock_sync,
        ):
            resolved = orchestrator._resolve_staged_changes_interactive(
                "SV_TEST",
                pending={"eos": object()},
                dimension_results={},
                force_live=False,
                force_preview=False,
                auto_yes=False,
                allow_drift=False,
                skip_publish=False,
            )

        self.assertTrue(resolved)
        self.assertEqual(mock_sync.call_count, 2)
        self.assertEqual(
            mock_sync.call_args_list[0],
            call(
                "SV_TEST",
                "eos",
                interactive=True,
                force_live=False,
                force_preview=False,
                auto_yes=False,
                allow_drift=False,
                skip_publish=True,
                prefer_pending=True,
                allow_structural_delete=False,
                allow_eos_shared_edit=False,
                allow_eos_destructive=False,
                allow_master_dangerous=False,
            ),
        )
        self.assertEqual(
            mock_sync.call_args_list[1],
            call(
                "SV_TEST",
                "eos",
                interactive=True,
                force_live=True,
                force_preview=False,
                auto_yes=False,
                allow_drift=False,
                skip_publish=True,
                prefer_pending=True,
                allow_structural_delete=False,
                allow_eos_shared_edit=False,
                allow_eos_destructive=False,
                allow_master_dangerous=False,
            ),
        )
        mock_confirm.assert_called_once()

    def test_resolve_staged_changes_can_preview_unstaged_master(self):
        import qsync.sync_orchestrator as orchestrator

        unstaged = self._empty_unstaged()
        unstaged["master"] = DimensionChanges(
            "master",
            True,
            "⚡ Unstaged: 2 field(s) changed",
            set(),
            status_kind="unstaged",
        )

        with (
            patch(
                "qsync.interactive_menu.select_from_list",
                side_effect=[
                    "📝 Preview unstaged changes (source vs cache)",
                    "↩ Exit sync",
                ],
            ) as mock_select,
            patch.object(
                orchestrator, "_detect_unstaged_changes", return_value=unstaged
            ),
            patch.object(
                orchestrator, "display_unified_preview", return_value=True
            ) as mock_preview,
        ):
            resolved = orchestrator._resolve_staged_changes_interactive(
                "SV_TEST",
                pending={"items": object()},
                dimension_results={},
                force_live=False,
                force_preview=False,
                auto_yes=False,
                allow_drift=False,
                skip_publish=False,
            )

        self.assertFalse(resolved)
        self.assertEqual(mock_select.call_count, 2)
        mock_preview.assert_called_once()
        self.assertEqual(mock_preview.call_args.kwargs["dimensions"], ["master"])

    def test_resolve_staged_changes_can_repair_unstaged_safe_issues(self):
        import qsync.sync_orchestrator as orchestrator

        unstaged = self._empty_unstaged()
        unstaged["flow"] = DimensionChanges(
            "flow",
            False,
            "Not initialized",
            set(),
            error_detail="Run: qsync flow pull --survey-id SV_TEST",
            safe_to_autofix=True,
            status_kind="error",
        )

        with (
            patch(
                "qsync.interactive_menu.select_from_list",
                side_effect=[
                    "🔧 Repair unstaged safe issues",
                    "Fix all safe issues",
                    "↩ Exit sync",
                ],
            ),
            patch("qsync.interactive_menu.confirm", return_value=True),
            patch.object(
                orchestrator, "_detect_unstaged_changes", return_value=unstaged
            ) as mock_detect,
            patch.object(
                orchestrator, "_get_inventory_cached", return_value={"name": "Test Survey"}
            ),
            patch.object(orchestrator, "_run_autofix", return_value="ok") as mock_run,
        ):
            resolved = orchestrator._resolve_staged_changes_interactive(
                "SV_TEST",
                pending={"items": object()},
                dimension_results={},
                force_live=False,
                force_preview=False,
                auto_yes=False,
                allow_drift=False,
                skip_publish=False,
            )

        self.assertFalse(resolved)
        self.assertGreaterEqual(mock_detect.call_count, 1)
        mock_run.assert_called_once_with("flow", "SV_TEST")

    def test_sync_dimensions_once_blocks_noninteractive_items_without_allow_skip_embedded(
        self,
    ):
        import qsync.sync_orchestrator as orchestrator

        changes = self._changes_with_edf_warning()
        buf = io.StringIO()
        with (
            redirect_stdout(buf),
            patch.object(orchestrator, "detect_survey_changes", return_value=changes),
            patch.object(orchestrator, "detect_conflicts", return_value=[]),
            patch.object(orchestrator, "sync_dimension") as mock_sync_dimension,
        ):
            summary = orchestrator._sync_dimensions_once(
                survey_id="SV_TEST",
                dimensions=["items"],
                interactive=False,
                force_live=False,
                force_preview=False,
                auto_yes=True,
                allow_drift=False,
                skip_publish=True,
                scope=None,
                per_dimension=False,
                allow_skip_embedded=False,
            )

        self.assertIsNone(summary)
        self.assertFalse(mock_sync_dimension.called)
        output = buf.getvalue()
        self.assertIn("--allow-skip-embedded", output)

    def test_sync_dimensions_once_allows_noninteractive_items_with_allow_skip_embedded(
        self,
    ):
        import qsync.sync_orchestrator as orchestrator

        changes = self._changes_with_edf_warning()
        sync_result = orchestrator.DimensionSyncResult(
            dimension="items",
            success=True,
            applied_changes=False,
        )

        with (
            patch.object(orchestrator, "detect_survey_changes", return_value=changes),
            patch.object(orchestrator, "detect_conflicts", return_value=[]),
            patch.object(orchestrator, "_display_push_report"),
            patch.object(orchestrator, "_orchestrated_publish"),
            patch.object(orchestrator, "_get_inventory_cached", return_value={}),
            patch.object(
                orchestrator, "sync_dimension", return_value=sync_result
            ) as mock_sync_dimension,
        ):
            summary = orchestrator._sync_dimensions_once(
                survey_id="SV_TEST",
                dimensions=["items"],
                interactive=False,
                force_live=False,
                force_preview=False,
                auto_yes=True,
                allow_drift=False,
                skip_publish=True,
                scope=None,
                per_dimension=False,
                allow_skip_embedded=True,
            )

        self.assertIsNotNone(summary)
        mock_sync_dimension.assert_called_once()
        self.assertEqual(mock_sync_dimension.call_args.kwargs["ignore_embedded"], True)

    def test_sync_dimensions_once_auto_yes_stages_unstaged_dimensions(self):
        import qsync.sync_orchestrator as orchestrator

        changes = SimpleNamespace(
            survey_name="Test Survey",
            dimensions=self._empty_unstaged(),
        )
        unstaged = self._empty_unstaged()
        unstaged["js"] = DimensionChanges(
            "js",
            True,
            "⚡ Unstaged: 1 JS file(s) changed",
            {"QID1"},
            status_kind="unstaged",
            edit_count=1,
        )
        unstaged["flow"] = DimensionChanges(
            "flow",
            True,
            "⚡ Unstaged: 1 change(s)",
            set(),
            status_kind="unstaged",
            edit_count=1,
        )

        sync_results = {
            "js": orchestrator.DimensionSyncResult(
                dimension="js", success=True, applied_changes=True
            ),
            "flow": orchestrator.DimensionSyncResult(
                dimension="flow", success=True, applied_changes=True
            ),
        }

        def _sync_result(survey_id, dimension, **kwargs):
            return sync_results[dimension]

        with (
            patch.object(orchestrator, "detect_survey_changes", return_value=changes),
            patch.object(orchestrator, "detect_conflicts", return_value=[]),
            patch.object(orchestrator, "detect_master_conflicts", return_value=[]),
            patch.object(orchestrator, "_detect_unstaged_changes", return_value=unstaged),
            patch.object(orchestrator, "_is_dimension_staged", return_value=False),
            patch.object(orchestrator, "stage_dimension", return_value=True) as mock_stage,
            patch.object(orchestrator, "sync_dimension", side_effect=_sync_result) as mock_sync,
            patch.object(orchestrator, "_display_push_report"),
            patch.object(orchestrator, "_orchestrated_publish"),
            patch.object(orchestrator, "_get_inventory_cached", return_value={}),
        ):
            summary = orchestrator._sync_dimensions_once(
                survey_id="SV_TEST",
                dimensions=["js", "flow"],
                interactive=False,
                force_live=False,
                force_preview=False,
                auto_yes=True,
                allow_drift=False,
                skip_publish=True,
                scope=None,
                per_dimension=False,
                allow_skip_embedded=True,
            )

        self.assertIsNotNone(summary)
        self.assertEqual(
            [call.args[1] for call in mock_stage.call_args_list],
            ["js", "flow"],
        )
        for staged_call in mock_stage.call_args_list:
            self.assertFalse(staged_call.kwargs["interactive"])
        for sync_call in mock_sync.call_args_list:
            self.assertTrue(sync_call.kwargs["prefer_pending"])

    def test_sync_dimensions_once_skips_stage_prompt_when_no_selected_changes(self):
        import qsync.sync_orchestrator as orchestrator

        changes = SimpleNamespace(
            survey_name="Test Survey",
            dimensions=self._empty_unstaged(),
        )
        buf = io.StringIO()

        with (
            redirect_stdout(buf),
            patch.object(orchestrator, "detect_survey_changes", return_value=changes),
            patch.object(orchestrator, "detect_conflicts", return_value=[]),
            patch.object(orchestrator, "detect_master_conflicts", return_value=[]),
            patch.object(orchestrator, "display_unified_preview", return_value=True),
            patch.object(
                orchestrator, "_detect_unstaged_changes", return_value=self._empty_unstaged()
            ),
            patch.object(orchestrator, "_is_dimension_staged", return_value=False),
            patch("qsync.interactive_menu.confirm") as mock_confirm,
            patch("qsync.interactive_menu.select_from_list") as mock_select,
            patch.object(orchestrator, "sync_dimension") as mock_sync_dimension,
        ):
            summary = orchestrator._sync_dimensions_once(
                survey_id="SV_TEST",
                dimensions=["master"],
                interactive=True,
                force_live=False,
                force_preview=False,
                auto_yes=False,
                allow_drift=False,
                skip_publish=True,
                scope=None,
                per_dimension=False,
            )

        self.assertIsNone(summary)
        mock_confirm.assert_not_called()
        mock_select.assert_not_called()
        mock_sync_dimension.assert_not_called()
        self.assertIn(
            "No staged or unstaged changes detected for selected dimensions.",
            buf.getvalue(),
        )

    def test_fixable_detail_uses_warning_for_autofixable_warnings(self):
        import qsync.sync_orchestrator as orchestrator

        info = DimensionChanges(
            dimension="edf",
            has_changes=False,
            change_summary="No changes",
            affected_qids=set(),
            warning_detail="repair suggested",
            safe_to_autofix=True,
        )

        self.assertEqual(orchestrator._fixable_detail(info), "repair suggested")

    def test_sync_survey_scopes_fixable_issues_to_selected_dimensions(self):
        import qsync.sync_orchestrator as orchestrator

        changes = SimpleNamespace(
            survey_name="Test Survey",
            dimensions={
                "items": DimensionChanges("items", False, "No changes", set()),
                "edf": DimensionChanges("edf", False, "No changes", set()),
                "js": DimensionChanges("js", False, "No changes", set()),
                "translations": DimensionChanges(
                    "translations",
                    False,
                    "No changes",
                    set(),
                    warning_detail="Workbook not found. Run: qsync items pull --survey-id SV_TEST",
                    safe_to_autofix=True,
                ),
                "eos": DimensionChanges("eos", False, "No changes", set()),
                "flow": DimensionChanges("flow", False, "No changes", set()),
                "master": DimensionChanges(
                    "master",
                    True,
                    "⚡ Unstaged: 1 field(s)",
                    set(),
                    status_kind="unstaged",
                ),
            },
        )
        summary = orchestrator.SurveySyncSummary(
            survey_id="SV_TEST",
            survey_name="Test Survey",
            dimension_results={
                "master": orchestrator.DimensionSyncResult(
                    dimension="master",
                    success=True,
                    applied_changes=True,
                )
            },
        )

        with (
            patch.object(orchestrator, "detect_survey_changes", return_value=changes),
            patch.object(orchestrator, "list_pending", return_value={}),
            patch.object(
                orchestrator, "_detect_unstaged_changes", return_value=self._empty_unstaged()
            ),
            patch.object(orchestrator, "_display_survey_overview"),
            patch.object(orchestrator, "_get_inventory_cached", return_value={}),
            patch.object(
                orchestrator, "_sync_dimensions_once", return_value=summary
            ) as mock_sync_once,
            patch("qsync.interactive_menu.confirm") as mock_confirm,
        ):
            result = orchestrator.sync_survey(
                survey_id="SV_TEST",
                dimensions=["master"],
                interactive=True,
                auto_yes=False,
            )

        self.assertIsNotNone(result)
        self.assertFalse(mock_confirm.called)
        mock_sync_once.assert_called_once()
        self.assertEqual(mock_sync_once.call_args.args[1], ["master"])

    def test_sync_survey_declining_fix_prompt_continues_with_selected_dimensions(self):
        import qsync.sync_orchestrator as orchestrator

        changes = SimpleNamespace(
            survey_name="Test Survey",
            dimensions={
                "items": DimensionChanges("items", False, "No changes", set()),
                "edf": DimensionChanges(
                    "edf",
                    False,
                    "No changes",
                    set(),
                    warning_detail="Embedded_Data worksheet is inconsistent.",
                    safe_to_autofix=True,
                ),
                "js": DimensionChanges("js", False, "No changes", set()),
                "translations": DimensionChanges(
                    "translations", False, "No changes", set()
                ),
                "eos": DimensionChanges("eos", False, "No changes", set()),
                "flow": DimensionChanges("flow", False, "No changes", set()),
                "master": DimensionChanges("master", False, "No changes", set()),
            },
        )
        summary = orchestrator.SurveySyncSummary(
            survey_id="SV_TEST",
            survey_name="Test Survey",
            dimension_results={
                "edf": orchestrator.DimensionSyncResult(
                    dimension="edf",
                    success=True,
                    applied_changes=True,
                )
            },
        )

        with (
            patch.object(orchestrator, "detect_survey_changes", return_value=changes),
            patch.object(orchestrator, "list_pending", return_value={}),
            patch.object(
                orchestrator, "_detect_unstaged_changes", return_value=self._empty_unstaged()
            ),
            patch.object(orchestrator, "_display_survey_overview"),
            patch.object(orchestrator, "_get_inventory_cached", return_value={}),
            patch.object(
                orchestrator, "_sync_dimensions_once", return_value=summary
            ) as mock_sync_once,
            patch("qsync.interactive_menu.confirm", return_value=False) as mock_confirm,
        ):
            result = orchestrator.sync_survey(
                survey_id="SV_TEST",
                dimensions=["edf"],
                interactive=True,
                auto_yes=False,
            )

        self.assertIsNotNone(result)
        mock_confirm.assert_called_once()
        mock_sync_once.assert_called_once()
        self.assertEqual(mock_sync_once.call_args.args[1], ["edf"])

    def test_prompt_dimension_selection_allows_items_translations_pair(self):
        import qsync.sync_orchestrator as orchestrator

        changes = orchestrator.SurveyChanges(
            survey_id="SV_TEST",
            survey_name="Test Survey",
            dimensions={
                "items": DimensionChanges(
                    "items",
                    True,
                    "⚡ Unstaged: 1 change(s)",
                    {"QID1"},
                    status_kind="unstaged",
                    edit_count=1,
                ),
                "edf": DimensionChanges("edf", False, "No changes", set()),
                "js": DimensionChanges(
                    "js",
                    True,
                    "⚡ Unstaged: 1 JS question(s) changed",
                    {"QID1"},
                    status_kind="unstaged",
                    edit_count=1,
                ),
                "translations": DimensionChanges(
                    "translations",
                    True,
                    "⚡ Unstaged: 2 change(s)",
                    {"QID1"},
                    status_kind="unstaged",
                    edit_count=2,
                ),
                "eos": DimensionChanges("eos", False, "No changes", set()),
                "flow": DimensionChanges("flow", False, "No changes", set()),
                "master": DimensionChanges("master", False, "No changes", set()),
            },
        )

        with (
            patch.object(orchestrator, "_is_dimension_staged", return_value=False),
            patch(
                "qsync.interactive_menu.select_from_list",
                return_value="pair:items+translations",
            ),
        ):
            selected = orchestrator.prompt_dimension_selection(changes, interactive=True)

        self.assertEqual(selected, ["items", "translations"])

    def test_qid_mode_selection_allows_items_translations_pair(self):
        import qsync.sync_orchestrator as orchestrator

        unstaged = {
            "items": DimensionChanges(
                "items",
                True,
                "⚡ Unstaged: 1 change(s)",
                {"QID1"},
                status_kind="unstaged",
                edit_count=1,
            ),
            "js": DimensionChanges(
                "js",
                True,
                "⚡ Unstaged: 1 JS question(s) changed",
                {"QID1"},
                status_kind="unstaged",
                edit_count=1,
            ),
            "translations": DimensionChanges(
                "translations",
                True,
                "⚡ Unstaged: 2 change(s)",
                {"QID1"},
                status_kind="unstaged",
                edit_count=2,
            ),
        }

        with patch(
            "qsync.interactive_menu.select_from_list",
            return_value="items + translations (recommended)",
        ):
            selected = orchestrator._prompt_qid_mode_dimension_selection(
                unstaged,
                allow_force=True,
            )

        self.assertEqual(selected, ["items", "translations"])

    def test_conflict_resolution_allows_items_translations_pair(self):
        import qsync.sync_orchestrator as orchestrator

        conflict = orchestrator.Conflict(
            qid="QID1",
            dimensions=["items", "js", "translations"],
            descriptions={
                "items": "item wording changed",
                "js": "question JS changed",
                "translations": "translation text changed",
            },
        )

        with (
            patch("qsync.rich_support.should_use_rich", return_value=False),
            patch(
                "qsync.interactive_menu.select_from_list",
                return_value="apply_pair:items+translations",
            ),
        ):
            selected = orchestrator.resolve_conflict_interactive(conflict)

        self.assertEqual(selected, ["items", "translations"])

    def test_sync_survey_declining_fix_prompt_continues_to_menu_without_dimensions(self):
        import qsync.sync_orchestrator as orchestrator

        changes = SimpleNamespace(
            survey_name="Test Survey",
            dimensions={
                "items": DimensionChanges("items", True, "⚡ Unstaged: 1 change", {"Q1"}),
                "edf": DimensionChanges(
                    "edf",
                    False,
                    "No changes",
                    set(),
                    warning_detail="Embedded_Data worksheet is inconsistent.",
                    safe_to_autofix=True,
                ),
                "js": DimensionChanges("js", False, "No changes", set()),
                "translations": DimensionChanges(
                    "translations", False, "No changes", set()
                ),
                "eos": DimensionChanges("eos", False, "No changes", set()),
                "flow": DimensionChanges("flow", False, "No changes", set()),
                "master": DimensionChanges("master", False, "No changes", set()),
            },
        )
        unstaged = self._empty_unstaged()
        unstaged["items"] = DimensionChanges(
            "items", True, "⚡ Unstaged: 1 change", {"Q1"}, status_kind="unstaged"
        )

        with (
            patch.object(orchestrator, "detect_survey_changes", return_value=changes),
            patch.object(orchestrator, "list_pending", return_value={}),
            patch.object(orchestrator, "_detect_unstaged_changes", return_value=unstaged),
            patch.object(orchestrator, "_display_survey_overview"),
            patch.object(orchestrator, "_get_inventory_cached", return_value={}),
            patch(
                "qsync.interactive_menu.confirm",
                return_value=False,
            ) as mock_confirm,
            patch(
                "qsync.interactive_menu.select_from_list",
                return_value="↩ Exit sync",
            ) as mock_select,
            patch.object(orchestrator, "_sync_dimensions_once") as mock_sync_once,
        ):
            result = orchestrator.sync_survey(
                survey_id="SV_TEST",
                dimensions=None,
                interactive=True,
                auto_yes=False,
            )

        self.assertIsNone(result)
        mock_confirm.assert_called_once()
        mock_select.assert_called_once()
        self.assertFalse(mock_sync_once.called)


if __name__ == "__main__":
    unittest.main()
