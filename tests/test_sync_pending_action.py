import unittest
from unittest.mock import patch


class TestSyncPendingAction(unittest.TestCase):
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
            with self.assertRaises(SystemExit):
                orchestrator.sync_survey(
                    survey_id="SV_TEST",
                    interactive=False,
                    auto_yes=True,
                    pending_action="abort",
                )


if __name__ == "__main__":
    unittest.main()
