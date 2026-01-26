import unittest
from unittest.mock import patch


class TestSyncPublishNoop(unittest.TestCase):
    def test_orchestrated_publish_skips_when_no_changes(self):
        import qsync.sync_orchestrator as orchestrator

        dimension_results = {
            "items": orchestrator.DimensionSyncResult(
                dimension="items", success=True, applied_changes=False
            ),
            "translations": orchestrator.DimensionSyncResult(
                dimension="translations", success=True, applied_changes=False
            ),
        }

        with patch("qsync.qualtrics_client.publish_survey_definition") as publish_mock:
            published = orchestrator._orchestrated_publish(
                survey_id="SV_TEST",
                survey_ref="SV_TEST",
                dimension_results=dimension_results,
                skip_publish=False,
                interactive=False,
                auto_yes=True,
            )

        self.assertIsNone(published)
        publish_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()

