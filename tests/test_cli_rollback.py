import unittest
from unittest.mock import MagicMock, patch


class CliRollbackTests(unittest.TestCase):
    @patch("qsync.cli_survey.publish_survey_definition")
    @patch("qsync.cli_survey.send_api_request")
    @patch("qsync.cli_survey.fetch_survey_version")
    @patch("qsync.cli_survey.get_client_config")
    @patch("qsync.cli_survey.load_push_context")
    def test_rollback_puts_questions_and_publishes(
        self,
        mock_ctx,
        mock_config,
        mock_fetch,
        mock_send,
        mock_publish,
    ) -> None:
        ctx = MagicMock()
        ctx.survey_name = "Test"
        ctx.response_count = 0
        ctx.preview_count = 0
        ctx.counts_unknown = False
        ctx.describe_counts.return_value = "0 live / 0 preview"
        mock_ctx.return_value = ctx

        mock_config.return_value = ("example.qualtrics.com", {"X-API-TOKEN": "x"})

        mock_fetch.return_value = {
            "result": {
                "Questions": {
                    "QID1": {"QuestionText": "A"},
                    "QID2": {"QuestionText": "B"},
                }
            }
        }

        from qsync.cli_survey import handle_rollback

        args = MagicMock()
        args.survey_id = "SV_TEST"
        args.version_id = "VER_123"
        args.question_id = "QID1,QID2"
        args.dry_run = False
        args.no_publish = False
        args.force_live = False
        args.yes = True
        args.description = ""

        handle_rollback(args)

        self.assertEqual(mock_send.call_count, 2)
        self.assertTrue(mock_publish.called)


if __name__ == "__main__":
    unittest.main()
