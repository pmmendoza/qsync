import io
import json
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
