import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class CliProlificAuthTests(unittest.TestCase):
    @patch("qsync.qualtrics_client.refresh_survey_cache")
    @patch("qsync.qualtrics_client.ensure_backup")
    @patch("qsync.cli_survey.get_client_config")
    @patch("qsync.cli_survey.send_api_request")
    def test_auth_replaces_header(
        self,
        mock_send,
        mock_config,
        mock_backup,
        mock_refresh,
    ) -> None:
        mock_config.return_value = ("example.qualtrics.com", {"X-API-TOKEN": "x"})
        mock_backup.return_value = Path("surveys/backups/test.json")

        get_resp = MagicMock()
        get_resp.ok = True
        get_resp.json.return_value = {"result": {"SurveyTitle": "T", "Header": ""}}

        put_resp = MagicMock()
        put_resp.ok = True
        put_resp.reason = "OK"

        mock_send.side_effect = [get_resp, put_resp]

        from qsync.cli_survey import handle_prolific_auth

        args = MagicMock()
        args.survey_id = "SV_TEST"
        args.snippet = '<script src="https://assets.prolific.com/assets/js/qualtrics/qualtrics.min.js?rid=${e://Field/ResponseID}&t=TOKEN"></script>'
        args.file = None
        args.mode = "replace"
        args.yes = True
        args.dry_run = False
        args.no_validate = False
        args.print_current = False

        handle_prolific_auth(args)

        put_calls = [
            call
            for call in mock_send.call_args_list
            if call.kwargs.get("method") == "PUT"
        ]
        self.assertEqual(len(put_calls), 1)
        self.assertEqual(
            put_calls[0].kwargs.get("path"),
            "survey-definitions/SV_TEST/options",
        )
        payload = put_calls[0].kwargs.get("json")
        self.assertIsInstance(payload, dict)
        self.assertIn("Header", payload)
        self.assertIn("assets.prolific.com/assets/js/qualtrics/qualtrics.min.js", payload["Header"])

        mock_backup.assert_called_once_with("SV_TEST")
        mock_refresh.assert_called_once_with("SV_TEST")

    @patch("qsync.qualtrics_client.refresh_survey_cache")
    @patch("qsync.qualtrics_client.ensure_backup")
    @patch("qsync.cli_survey.get_client_config")
    @patch("qsync.cli_survey.send_api_request")
    def test_auth_appends_header(
        self,
        mock_send,
        mock_config,
        mock_backup,
        mock_refresh,
    ) -> None:
        mock_config.return_value = ("example.qualtrics.com", {"X-API-TOKEN": "x"})
        mock_backup.return_value = Path("surveys/backups/test.json")

        get_resp = MagicMock()
        get_resp.ok = True
        get_resp.json.return_value = {"result": {"Header": "<meta charset='utf-8'>"}}

        put_resp = MagicMock()
        put_resp.ok = True

        mock_send.side_effect = [get_resp, put_resp]

        from qsync.cli_survey import handle_prolific_auth

        args = MagicMock()
        args.survey_id = "SV_TEST"
        args.snippet = "<script>/* hi */</script>"
        args.file = None
        args.mode = "append"
        args.yes = True
        args.dry_run = False
        args.no_validate = True
        args.print_current = False

        handle_prolific_auth(args)

        put_payload = None
        for call in mock_send.call_args_list:
            if call.kwargs.get("method") == "PUT":
                put_payload = call.kwargs.get("json")
        self.assertIsNotNone(put_payload)
        self.assertEqual(
            put_payload["Header"],
            "<meta charset='utf-8'>\n<script>/* hi */</script>",
        )
        mock_backup.assert_called_once_with("SV_TEST")
        mock_refresh.assert_called_once_with("SV_TEST")

    @patch("qsync.qualtrics_client.refresh_survey_cache")
    @patch("qsync.qualtrics_client.ensure_backup")
    @patch("qsync.cli_survey.get_client_config")
    @patch("qsync.cli_survey.send_api_request")
    def test_auth_noop_when_snippet_present(
        self,
        mock_send,
        mock_config,
        _mock_backup,
        _mock_refresh,
    ) -> None:
        mock_config.return_value = ("example.qualtrics.com", {"X-API-TOKEN": "x"})

        snippet = "<script>/* hi */</script>"
        get_resp = MagicMock()
        get_resp.ok = True
        get_resp.json.return_value = {"result": {"Header": f"X\n{snippet}\nY"}}
        mock_send.return_value = get_resp

        from qsync.cli_survey import handle_prolific_auth

        args = MagicMock()
        args.survey_id = "SV_TEST"
        args.snippet = snippet
        args.file = None
        args.mode = "replace"
        args.yes = True
        args.dry_run = False
        args.no_validate = True
        args.print_current = False

        handle_prolific_auth(args)

        put_calls = [
            call
            for call in mock_send.call_args_list
            if call.kwargs.get("method") == "PUT"
        ]
        self.assertEqual(len(put_calls), 0)


if __name__ == "__main__":
    unittest.main()
