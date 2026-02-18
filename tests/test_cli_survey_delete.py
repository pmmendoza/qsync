import unittest
from unittest.mock import MagicMock, patch


def _status_payload(*, live: int, preview: int = 0) -> dict:
    return {
        "id": "SV_TEST",
        "name": "Delete Me",
        "isActive": True,
        "responseCounts": {
            "auditable": live,
            "generated": preview,
        },
    }


class CliSurveyDeleteTests(unittest.TestCase):
    @patch("qsync.cli_survey._fetch_survey_status")
    @patch("qsync.cli_survey._get_client_config_for_args")
    @patch("qsync.cli_survey.send_api_request")
    def test_delete_defaults_to_dry_run_without_yes(
        self,
        mock_send,
        mock_config,
        mock_status,
    ) -> None:
        mock_config.return_value = ("example.qualtrics.com", {"X-API-TOKEN": "x"})
        mock_status.return_value = _status_payload(live=0, preview=0)

        from qsync.cli_survey import handle_delete

        args = MagicMock()
        args.survey_ids = ["SV_TEST"]
        args.yes = False
        args.force_live = False

        with patch("qsync.cli_survey.sys.stdin.isatty", return_value=False), patch(
            "qsync.cli_survey.sys.stdout.isatty", return_value=False
        ):
            handle_delete(args)

        delete_calls = [
            call for call in mock_send.call_args_list if call.kwargs.get("method") == "DELETE"
        ]
        self.assertEqual(len(delete_calls), 0)

    @patch("qsync.cli_survey._fetch_survey_status")
    @patch("qsync.cli_survey._get_client_config_for_args")
    @patch("qsync.cli_survey.send_api_request")
    def test_delete_yes_deletes_when_no_live_responses(
        self,
        mock_send,
        mock_config,
        mock_status,
    ) -> None:
        mock_config.return_value = ("example.qualtrics.com", {"X-API-TOKEN": "x"})
        mock_status.return_value = _status_payload(live=0, preview=0)

        from qsync.cli_survey import handle_delete

        args = MagicMock()
        args.survey_ids = ["SV_TEST"]
        args.yes = True
        args.force_live = False

        handle_delete(args)

        delete_calls = [
            call for call in mock_send.call_args_list if call.kwargs.get("method") == "DELETE"
        ]
        self.assertEqual(len(delete_calls), 1)
        self.assertEqual(delete_calls[0].kwargs.get("path"), "surveys/SV_TEST")

    @patch("qsync.cli_survey._fetch_survey_status")
    @patch("qsync.cli_survey._get_client_config_for_args")
    @patch("qsync.cli_survey.send_api_request")
    def test_delete_yes_blocks_with_live_responses_without_force_live(
        self,
        mock_send,
        mock_config,
        mock_status,
    ) -> None:
        mock_config.return_value = ("example.qualtrics.com", {"X-API-TOKEN": "x"})
        mock_status.return_value = _status_payload(live=4, preview=0)

        from qsync.cli_survey import handle_delete

        args = MagicMock()
        args.survey_ids = ["SV_TEST"]
        args.yes = True
        args.force_live = False

        handle_delete(args)

        delete_calls = [
            call for call in mock_send.call_args_list if call.kwargs.get("method") == "DELETE"
        ]
        self.assertEqual(len(delete_calls), 0)

    @patch("qsync.cli_survey._fetch_survey_status")
    @patch("qsync.cli_survey._get_client_config_for_args")
    @patch("qsync.cli_survey.send_api_request")
    def test_delete_yes_force_live_allows_live_responses(
        self,
        mock_send,
        mock_config,
        mock_status,
    ) -> None:
        mock_config.return_value = ("example.qualtrics.com", {"X-API-TOKEN": "x"})
        mock_status.return_value = _status_payload(live=4, preview=0)

        from qsync.cli_survey import handle_delete

        args = MagicMock()
        args.survey_ids = ["SV_TEST"]
        args.yes = True
        args.force_live = True

        handle_delete(args)

        delete_calls = [
            call for call in mock_send.call_args_list if call.kwargs.get("method") == "DELETE"
        ]
        self.assertEqual(len(delete_calls), 1)

    @patch("qsync.cli_survey._typed_confirmation", return_value=True)
    @patch("qsync.cli_survey._confirm_interactive_gate", return_value=True)
    @patch("qsync.cli_survey._fetch_survey_status")
    @patch("qsync.cli_survey._get_client_config_for_args")
    @patch("qsync.cli_survey.send_api_request")
    def test_delete_interactive_guided_path_can_execute_without_yes(
        self,
        mock_send,
        mock_config,
        mock_status,
        _mock_gate,
        _mock_typed,
    ) -> None:
        mock_config.return_value = ("example.qualtrics.com", {"X-API-TOKEN": "x"})
        mock_status.return_value = _status_payload(live=0, preview=0)

        from qsync.cli_survey import handle_delete

        args = MagicMock()
        args.survey_ids = ["SV_TEST"]
        args.yes = False
        args.force_live = False

        with patch("qsync.cli_survey.sys.stdin.isatty", return_value=True), patch(
            "qsync.cli_survey.sys.stdout.isatty", return_value=True
        ):
            handle_delete(args)

        delete_calls = [
            call for call in mock_send.call_args_list if call.kwargs.get("method") == "DELETE"
        ]
        self.assertEqual(len(delete_calls), 1)


if __name__ == "__main__":
    unittest.main()
