import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class _ExistsPath:
    def exists(self) -> bool:
        return True


class CliProlificAuthTests(unittest.TestCase):
    @patch("qsync.cli_survey._prompt_for_survey_id_api_if_needed")
    @patch("qsync.qualtrics_client.refresh_survey_cache")
    @patch("qsync.qualtrics_client.ensure_backup")
    @patch("qsync.cli_survey.get_client_config")
    @patch("qsync.cli_survey.send_api_request")
    def test_auth_uses_provided_survey_id_without_menu_selection(
        self,
        mock_send,
        mock_config,
        mock_backup,
        mock_refresh,
        mock_prompt_survey_id,
    ) -> None:
        mock_prompt_survey_id.return_value = "SV_DIRECT"
        mock_config.return_value = ("example.qualtrics.com", {"X-API-TOKEN": "x"})
        mock_backup.return_value = Path("surveys/backups/test.json")

        get_resp = MagicMock()
        get_resp.ok = True
        get_resp.json.return_value = {"result": {"Header": ""}}

        options_put_resp = MagicMock()
        options_put_resp.ok = True

        status_resp = MagicMock()
        status_resp.ok = True
        status_resp.json.return_value = {"result": {"isActive": True}}

        mock_send.side_effect = [get_resp, options_put_resp, status_resp]

        from qsync.cli_survey import handle_prolific_auth

        args = MagicMock()
        args.survey_id = "SV_DIRECT"
        args.snippet = "<script>/* hi */</script>"
        args.file = None
        args.mode = "replace"
        args.yes = True
        args.dry_run = False
        args.no_validate = True
        args.print_current = False
        args.no_publish = True
        args.no_activate = False

        handle_prolific_auth(args)

        mock_prompt_survey_id.assert_called_once_with(
            survey_id="SV_DIRECT",
            args=args,
            message="Select a survey for prolific-auth:",
        )
        first_call = mock_send.call_args_list[0]
        self.assertEqual(
            first_call.kwargs.get("path"),
            "survey-definitions/SV_DIRECT/options",
        )
        mock_backup.assert_called_once_with("SV_DIRECT")
        mock_refresh.assert_called_once_with("SV_DIRECT")

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

        options_put_resp = MagicMock()
        options_put_resp.ok = True
        options_put_resp.reason = "OK"

        status_resp = MagicMock()
        status_resp.ok = True
        status_resp.json.return_value = {"result": {"isActive": False}}

        activate_put_resp = MagicMock()
        activate_put_resp.ok = True
        activate_put_resp.reason = "OK"

        mock_send.side_effect = [
            get_resp,
            options_put_resp,
            status_resp,
            activate_put_resp,
        ]

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
        args.no_publish = True
        args.no_activate = False

        handle_prolific_auth(args)

        put_calls = [
            call
            for call in mock_send.call_args_list
            if call.kwargs.get("method") == "PUT"
        ]
        self.assertEqual(len(put_calls), 2)
        self.assertEqual(
            put_calls[0].kwargs.get("path"),
            "survey-definitions/SV_TEST/options",
        )
        self.assertEqual(
            put_calls[1].kwargs.get("path"),
            "surveys/SV_TEST",
        )
        self.assertEqual(
            put_calls[1].kwargs.get("json"),
            {"isActive": True},
        )
        payload = put_calls[0].kwargs.get("json")
        self.assertIsInstance(payload, dict)
        self.assertIn("Header", payload)
        self.assertIn(
            "assets.prolific.com/assets/js/qualtrics/qualtrics.min.js",
            payload["Header"],
        )

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

        options_put_resp = MagicMock()
        options_put_resp.ok = True

        status_resp = MagicMock()
        status_resp.ok = True
        status_resp.json.return_value = {"result": {"isActive": False}}

        activate_put_resp = MagicMock()
        activate_put_resp.ok = True

        mock_send.side_effect = [
            get_resp,
            options_put_resp,
            status_resp,
            activate_put_resp,
        ]

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
        args.no_publish = True
        args.no_activate = False

        handle_prolific_auth(args)

        put_payload = None
        for call in mock_send.call_args_list:
            if (
                call.kwargs.get("method") == "PUT"
                and call.kwargs.get("path") == "survey-definitions/SV_TEST/options"
            ):
                put_payload = call.kwargs.get("json")
        self.assertIsNotNone(put_payload)
        self.assertEqual(
            put_payload["Header"],
            "<meta charset='utf-8'>\n<script>/* hi */</script>",
        )
        activate_put_calls = [
            call
            for call in mock_send.call_args_list
            if call.kwargs.get("method") == "PUT"
            and call.kwargs.get("path") == "surveys/SV_TEST"
        ]
        self.assertEqual(len(activate_put_calls), 1)
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
        args.no_publish = True
        args.no_activate = False

        handle_prolific_auth(args)

        put_calls = [
            call
            for call in mock_send.call_args_list
            if call.kwargs.get("method") == "PUT"
        ]
        self.assertEqual(len(put_calls), 0)

    @patch("qsync.qualtrics_client.refresh_survey_cache")
    @patch("qsync.qualtrics_client.ensure_backup")
    @patch("qsync.cli_survey.get_client_config")
    @patch("qsync.cli_survey.send_api_request")
    def test_auth_does_not_activate_when_already_active(
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
        get_resp.json.return_value = {"result": {"Header": ""}}

        options_put_resp = MagicMock()
        options_put_resp.ok = True

        status_resp = MagicMock()
        status_resp.ok = True
        status_resp.json.return_value = {"result": {"isActive": True}}

        mock_send.side_effect = [get_resp, options_put_resp, status_resp]

        from qsync.cli_survey import handle_prolific_auth

        args = MagicMock()
        args.survey_id = "SV_TEST"
        args.snippet = "<script>/* hi */</script>"
        args.file = None
        args.mode = "replace"
        args.yes = True
        args.dry_run = False
        args.no_validate = True
        args.print_current = False
        args.no_publish = True
        args.no_activate = False

        handle_prolific_auth(args)

        activate_put_calls = [
            call
            for call in mock_send.call_args_list
            if call.kwargs.get("method") == "PUT"
            and call.kwargs.get("path") == "surveys/SV_TEST"
        ]
        self.assertEqual(len(activate_put_calls), 0)
        mock_backup.assert_called_once_with("SV_TEST")
        mock_refresh.assert_called_once_with("SV_TEST")

    @patch("qsync.cli_survey.get_client_config")
    @patch("qsync.survey_selection.pick_survey_ids_from_api")
    @patch("qsync.interactive_menu.is_interactive", return_value=True)
    def test_prompt_for_any_survey_id_resolves_partial_autocomplete_input(
        self, mock_is_interactive, mock_pick, mock_client_config
    ) -> None:
        from qsync.cli_survey import _prompt_for_survey_id_api_if_needed

        mock_client_config.return_value = ("iad1.qualtrics.com", {"X-API-TOKEN": "x"})
        mock_pick.return_value = ["SV_AAA"]
        args = MagicMock()
        args.account = None
        args.survey_id = None

        selected = _prompt_for_survey_id_api_if_needed(
            survey_id=None,
            args=args,
            message="Select a survey to pull (cache JSON):",
        )
        self.assertEqual(selected, "SV_AAA")
        mock_pick.assert_called_once_with(
            message="Select a survey to pull (cache JSON):",
            base_url="iad1.qualtrics.com",
            headers={"X-API-TOKEN": "x"},
            include_back=False,
            allow_multiple=False,
        )

    @patch("qsync.cli_survey.get_client_config")
    @patch("qsync.survey_selection.pick_survey_ids_from_api")
    @patch("qsync.interactive_menu.is_interactive", return_value=True)
    def test_prompt_for_any_survey_id_disambiguates_partial_input(
        self, mock_is_interactive, mock_pick, mock_client_config
    ) -> None:
        from qsync.cli_survey import _prompt_for_survey_id_api_if_needed

        mock_client_config.return_value = ("iad1.qualtrics.com", {"X-API-TOKEN": "x"})
        mock_pick.return_value = ["SV_BBB"]
        args = MagicMock()
        args.account = None
        args.survey_id = None

        selected = _prompt_for_survey_id_api_if_needed(
            survey_id=None,
            args=args,
            message="Select a survey to pull (cache JSON):",
        )
        self.assertEqual(selected, "SV_BBB")
        mock_pick.assert_called_once_with(
            message="Select a survey to pull (cache JSON):",
            base_url="iad1.qualtrics.com",
            headers={"X-API-TOKEN": "x"},
            include_back=False,
            allow_multiple=False,
        )


if __name__ == "__main__":
    unittest.main()
