import io
import unittest
from unittest.mock import MagicMock, patch


class CliActivateTests(unittest.TestCase):
    @patch("qsync.cli_survey.ensure_unlocked")
    @patch("qsync.cli_survey.load_push_context")
    @patch("qsync.cli_survey.get_client_config")
    @patch("qsync.cli_survey.send_api_request")
    def test_activate_puts_isActive_true(
        self,
        mock_send,
        mock_config,
        mock_ctx,
        mock_lock,
    ) -> None:
        ctx = MagicMock()
        ctx.survey_name = "Test Survey"
        ctx.response_count = 0
        ctx.preview_count = 0
        ctx.counts_unknown = False
        ctx.describe_counts.return_value = "Responses: 0 live / 0 preview"
        mock_ctx.return_value = ctx

        mock_config.return_value = ("example.qualtrics.com", {"X-API-TOKEN": "x"})

        def _make_status(active: bool) -> MagicMock:
            resp = MagicMock()
            resp.ok = True
            resp.json.return_value = {
                "result": {"isActive": active, "name": "Test Survey"}
            }
            return resp

        put_resp = MagicMock()
        put_resp.ok = True
        put_resp.reason = "OK"

        mock_send.side_effect = [
            _make_status(False),  # pre-check
            put_resp,  # PUT
            _make_status(True),  # post-check
        ]

        from qsync.cli_survey import handle_activate

        args = MagicMock()
        args.survey_id = ["SV_TEST"]
        args.dry_run = False
        args.force_live = False
        args.yes = True

        handle_activate(args)

        put_calls = [
            call
            for call in mock_send.call_args_list
            if call.kwargs.get("method") == "PUT"
        ]
        self.assertEqual(len(put_calls), 1)
        self.assertEqual(
            put_calls[0].kwargs.get("json"),
            {"isActive": True},
        )
        self.assertEqual(
            put_calls[0].kwargs.get("action"),
            "qsync.survey.activate",
        )
        mock_lock.assert_called_once_with("SV_TEST")

    @patch("qsync.cli_survey.ensure_unlocked")
    @patch("qsync.cli_survey.load_push_context")
    @patch("qsync.cli_survey.get_client_config")
    @patch("qsync.cli_survey.send_api_request")
    def test_activate_idempotent_no_put(
        self,
        mock_send,
        mock_config,
        mock_ctx,
        _mock_lock,
    ) -> None:
        ctx = MagicMock()
        ctx.survey_name = "Test Survey"
        ctx.response_count = 0
        ctx.preview_count = 0
        ctx.counts_unknown = False
        ctx.describe_counts.return_value = "Responses: 0 live / 0 preview"
        mock_ctx.return_value = ctx

        mock_config.return_value = ("example.qualtrics.com", {"X-API-TOKEN": "x"})

        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = {"result": {"isActive": True, "name": "Test Survey"}}
        mock_send.return_value = resp

        from qsync.cli_survey import handle_activate

        args = MagicMock()
        args.survey_id = ["SV_TEST"]
        args.dry_run = False
        args.force_live = False
        args.yes = True

        handle_activate(args)

        put_calls = [
            call
            for call in mock_send.call_args_list
            if call.kwargs.get("method") == "PUT"
        ]
        self.assertEqual(len(put_calls), 0)

    @patch("qsync.cli_survey.ensure_unlocked")
    @patch("qsync.cli_survey.load_push_context")
    @patch("qsync.cli_survey.get_client_config")
    @patch("qsync.cli_survey.send_api_request")
    def test_activate_dry_run_no_put(
        self,
        mock_send,
        mock_config,
        mock_ctx,
        _mock_lock,
    ) -> None:
        ctx = MagicMock()
        ctx.survey_name = "Test Survey"
        ctx.response_count = 0
        ctx.preview_count = 0
        ctx.counts_unknown = False
        ctx.describe_counts.return_value = "Responses: 0 live / 0 preview"
        mock_ctx.return_value = ctx

        mock_config.return_value = ("example.qualtrics.com", {"X-API-TOKEN": "x"})

        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = {"result": {"isActive": False, "name": "Test Survey"}}
        mock_send.return_value = resp

        from qsync.cli_survey import handle_activate

        args = MagicMock()
        args.survey_id = ["SV_TEST"]
        args.dry_run = True
        args.force_live = False
        args.yes = True

        handle_activate(args)

        put_calls = [
            call
            for call in mock_send.call_args_list
            if call.kwargs.get("method") == "PUT"
        ]
        self.assertEqual(len(put_calls), 0)

    @patch("qsync.cli_survey.ensure_unlocked")
    @patch("qsync.cli_survey.load_push_context")
    @patch("qsync.cli_survey.get_client_config")
    @patch("qsync.cli_survey.send_api_request")
    def test_activate_blocks_when_live_without_force(
        self,
        mock_send,
        mock_config,
        mock_ctx,
        _mock_lock,
    ) -> None:
        ctx = MagicMock()
        ctx.survey_name = "Test Survey"
        ctx.response_count = 3
        ctx.preview_count = 0
        ctx.counts_unknown = False
        ctx.describe_counts.return_value = "Responses: 3 live / 0 preview"
        mock_ctx.return_value = ctx

        mock_config.return_value = ("example.qualtrics.com", {"X-API-TOKEN": "x"})

        from qsync.cli_survey import handle_activate

        args = MagicMock()
        args.survey_id = ["SV_TEST"]
        args.dry_run = False
        args.force_live = False
        args.yes = True

        with self.assertRaises(SystemExit):
            handle_activate(args)

        put_calls = [
            call
            for call in mock_send.call_args_list
            if call.kwargs.get("method") == "PUT"
        ]
        self.assertEqual(len(put_calls), 0)

    @patch("qsync.cli_survey.ensure_unlocked")
    @patch("qsync.cli_survey.load_push_context")
    @patch("qsync.cli_survey.get_client_config")
    @patch("qsync.cli_survey.send_api_request")
    def test_activate_non_ok_response_raises(
        self,
        mock_send,
        mock_config,
        mock_ctx,
        _mock_lock,
    ) -> None:
        ctx = MagicMock()
        ctx.survey_name = "Test Survey"
        ctx.response_count = 0
        ctx.preview_count = 0
        ctx.counts_unknown = False
        ctx.describe_counts.return_value = "Responses: 0 live / 0 preview"
        mock_ctx.return_value = ctx

        mock_config.return_value = ("example.qualtrics.com", {"X-API-TOKEN": "x"})

        def _make_status(active: bool) -> MagicMock:
            resp = MagicMock()
            resp.ok = True
            resp.json.return_value = {
                "result": {"isActive": active, "name": "Test Survey"}
            }
            return resp

        put_resp = MagicMock()
        put_resp.ok = False
        put_resp.status_code = 403
        put_resp.reason = "Forbidden"

        mock_send.side_effect = [
            _make_status(False),
            put_resp,
        ]

        from qsync.cli_survey import handle_activate

        args = MagicMock()
        args.survey_id = ["SV_TEST"]
        args.dry_run = False
        args.force_live = False
        args.yes = True

        with self.assertRaises(SystemExit):
            handle_activate(args)

    @patch("qsync.cli_survey.load_push_context")
    @patch("qsync.cli_survey.get_client_config")
    @patch("qsync.cli_survey.send_api_request")
    def test_activate_lock_blocks(
        self,
        mock_send,
        _mock_config,
        _mock_ctx,
    ) -> None:
        from qsync.cli_survey import handle_activate

        _mock_config.return_value = ("example.qualtrics.com", {"X-API-TOKEN": "x"})

        args = MagicMock()
        args.survey_id = ["SV_TEST"]
        args.dry_run = False
        args.force_live = False
        args.yes = True

        with patch(
            "qsync.cli_survey.ensure_unlocked", side_effect=RuntimeError("locked")
        ):
            with self.assertRaises(SystemExit):
                handle_activate(args)

        put_calls = [
            call
            for call in mock_send.call_args_list
            if call.kwargs.get("method") == "PUT"
        ]
        self.assertEqual(len(put_calls), 0)

    @patch("qsync.cli_survey.ensure_unlocked")
    @patch("qsync.cli_survey.load_push_context")
    @patch("qsync.cli_survey.get_client_config")
    @patch("qsync.cli_survey.send_api_request")
    def test_activate_requires_confirmation_non_tty(
        self,
        mock_send,
        mock_config,
        mock_ctx,
        _mock_lock,
    ) -> None:
        ctx = MagicMock()
        ctx.survey_name = "Test Survey"
        ctx.response_count = 0
        ctx.preview_count = 0
        ctx.counts_unknown = False
        ctx.describe_counts.return_value = "Responses: 0 live / 0 preview"
        mock_ctx.return_value = ctx

        mock_config.return_value = ("example.qualtrics.com", {"X-API-TOKEN": "x"})

        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = {"result": {"isActive": False, "name": "Test Survey"}}
        mock_send.return_value = resp

        from qsync.cli_survey import handle_activate

        args = MagicMock()
        args.survey_id = ["SV_TEST"]
        args.dry_run = False
        args.force_live = False
        args.yes = False

        with patch("qsync.cli_survey.sys.stdin.isatty", return_value=False):
            with self.assertRaises(SystemExit):
                handle_activate(args)

    @patch("qsync.cli_survey.publish_survey_definition")
    @patch("qsync.cli_survey.ensure_unlocked")
    @patch("qsync.cli_survey.load_push_context")
    @patch("qsync.cli_survey.get_client_config")
    @patch("qsync.cli_survey.send_api_request")
    def test_activate_publish_after(
        self,
        mock_send,
        mock_config,
        mock_ctx,
        _mock_lock,
        mock_publish,
    ) -> None:
        ctx = MagicMock()
        ctx.survey_name = "Test Survey"
        ctx.response_count = 0
        ctx.preview_count = 0
        ctx.counts_unknown = False
        ctx.describe_counts.return_value = "Responses: 0 live / 0 preview"
        mock_ctx.return_value = ctx

        mock_config.return_value = ("example.qualtrics.com", {"X-API-TOKEN": "x"})

        def _make_status(active: bool) -> MagicMock:
            resp = MagicMock()
            resp.ok = True
            resp.json.return_value = {
                "result": {"isActive": active, "name": "Test Survey"}
            }
            return resp

        put_resp = MagicMock()
        put_resp.ok = True
        put_resp.reason = "OK"

        mock_send.side_effect = [
            _make_status(False),
            put_resp,
            _make_status(True),
        ]

        from qsync.cli_survey import handle_activate

        args = MagicMock()
        args.survey_id = ["SV_TEST"]
        args.dry_run = False
        args.force_live = False
        args.yes = True
        args.publish = True
        args.publish_description = "Activation publish"

        handle_activate(args)

        mock_publish.assert_called_once()

    @patch("qsync.cli_survey.ensure_unlocked")
    @patch("qsync.cli_survey.load_push_context")
    @patch("qsync.cli_survey.get_client_config")
    @patch("qsync.cli_survey.send_api_request")
    def test_activate_batch_processing(
        self,
        mock_send,
        mock_config,
        mock_ctx,
        _mock_lock,
    ) -> None:
        ctx = MagicMock()
        ctx.survey_name = "Test Survey"
        ctx.response_count = 0
        ctx.preview_count = 0
        ctx.counts_unknown = False
        ctx.describe_counts.return_value = "Responses: 0 live / 0 preview"
        mock_ctx.return_value = ctx

        mock_config.return_value = ("example.qualtrics.com", {"X-API-TOKEN": "x"})

        def _make_status(active: bool) -> MagicMock:
            resp = MagicMock()
            resp.ok = True
            resp.json.return_value = {
                "result": {"isActive": active, "name": "Test Survey"}
            }
            return resp

        put_resp = MagicMock()
        put_resp.ok = True
        put_resp.reason = "OK"

        mock_send.side_effect = [
            _make_status(False),
            put_resp,
            _make_status(True),
            _make_status(False),
            put_resp,
            _make_status(True),
        ]

        from qsync.cli_survey import handle_activate

        args = MagicMock()
        args.survey_id = ["SV_A", "SV_B"]
        args.dry_run = False
        args.force_live = False
        args.yes = True

        handle_activate(args)

        put_calls = [
            call
            for call in mock_send.call_args_list
            if call.kwargs.get("method") == "PUT"
        ]
        self.assertEqual(len(put_calls), 2)

    @patch("qsync.cli_survey.ensure_unlocked")
    @patch("qsync.cli_survey.load_push_context")
    @patch("qsync.cli_survey.get_client_config")
    @patch("qsync.cli_survey.send_api_request")
    def test_activation_requires_force_live_env(
        self,
        _mock_send,
        _mock_config,
        _mock_ctx,
        _mock_lock,
    ) -> None:
        from qsync.cli_survey import handle_activate

        args = MagicMock()
        args.survey_id = ["SV_TEST"]
        args.dry_run = False
        args.force_live = False
        args.yes = True

        with patch.dict("os.environ", {"QSYNC_ACTIVATION_REQUIRE_FORCE_LIVE": "1"}):
            with self.assertRaises(SystemExit):
                handle_activate(args)

    @patch("qsync.cli_survey.ensure_unlocked")
    @patch("qsync.cli_survey.load_push_context")
    @patch("qsync.cli_survey.get_client_config")
    @patch("qsync.cli_survey.send_api_request")
    def test_activation_auto_confirm_disabled_ignores_yes(
        self,
        mock_send,
        mock_config,
        mock_ctx,
        _mock_lock,
    ) -> None:
        ctx = MagicMock()
        ctx.survey_name = "Test Survey"
        ctx.response_count = 0
        ctx.preview_count = 0
        ctx.counts_unknown = False
        ctx.describe_counts.return_value = "Responses: 0 live / 0 preview"
        mock_ctx.return_value = ctx

        mock_config.return_value = ("example.qualtrics.com", {"X-API-TOKEN": "x"})

        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = {"result": {"isActive": False, "name": "Test Survey"}}
        mock_send.return_value = resp

        from qsync.cli_survey import handle_activate

        args = MagicMock()
        args.survey_id = ["SV_TEST"]
        args.dry_run = False
        args.force_live = False
        args.yes = True

        with patch.dict("os.environ", {"QSYNC_ACTIVATION_AUTO_CONFIRM": "false"}):
            with patch("qsync.cli_survey.sys.stdin.isatty", return_value=False):
                with self.assertRaises(SystemExit):
                    handle_activate(args)

    @patch("qsync.cli_survey.ensure_unlocked")
    @patch("qsync.cli_survey.load_push_context")
    @patch("qsync.cli_survey.get_client_config")
    @patch("qsync.cli_survey.send_api_request")
    def test_activation_counts_unknown_allows_force_live(
        self,
        mock_send,
        mock_config,
        mock_ctx,
        _mock_lock,
    ) -> None:
        ctx = MagicMock()
        ctx.survey_name = "Test Survey"
        ctx.response_count = 0
        ctx.preview_count = 0
        ctx.counts_unknown = True
        ctx.describe_counts.return_value = "Responses: unknown"
        mock_ctx.return_value = ctx

        mock_config.return_value = ("example.qualtrics.com", {"X-API-TOKEN": "x"})

        def _make_status(active: bool) -> MagicMock:
            resp = MagicMock()
            resp.ok = True
            resp.json.return_value = {
                "result": {"isActive": active, "name": "Test Survey"}
            }
            return resp

        put_resp = MagicMock()
        put_resp.ok = True
        put_resp.reason = "OK"

        mock_send.side_effect = [
            _make_status(False),
            put_resp,
            _make_status(True),
        ]

        from qsync.cli_survey import handle_activate

        args = MagicMock()
        args.survey_id = ["SV_TEST"]
        args.dry_run = False
        args.force_live = True
        args.yes = True

        handle_activate(args)

        put_calls = [
            call
            for call in mock_send.call_args_list
            if call.kwargs.get("method") == "PUT"
        ]
        self.assertEqual(len(put_calls), 1)

    def test_activation_error_message_unauthorized(self) -> None:
        from qsync.cli_survey import _activation_error_message

        response = MagicMock()
        response.status_code = 401
        response.reason = "Unauthorized"

        exc = RuntimeError("boom")
        exc.response = response

        message = _activation_error_message(exc)
        self.assertIn("Unauthorized (401)", message)
        self.assertIn("qsync doctor", message)

    @patch("qsync.cli_survey.ensure_unlocked")
    @patch("qsync.cli_survey.load_push_context")
    @patch("qsync.cli_survey.get_client_config")
    @patch("qsync.cli_survey.send_api_request")
    def test_activate_output_includes_status_lines(
        self,
        mock_send,
        mock_config,
        mock_ctx,
        _mock_lock,
    ) -> None:
        ctx = MagicMock()
        ctx.survey_name = "Test Survey"
        ctx.response_count = 0
        ctx.preview_count = 0
        ctx.counts_unknown = False
        ctx.describe_counts.return_value = "Responses: 0 live / 0 preview"
        mock_ctx.return_value = ctx

        mock_config.return_value = ("example.qualtrics.com", {"X-API-TOKEN": "x"})

        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = {"result": {"isActive": False, "name": "Test Survey"}}
        mock_send.return_value = resp

        from qsync import terminal_colors
        from qsync.cli_survey import handle_activate

        args = MagicMock()
        args.survey_id = ["SV_TEST"]
        args.dry_run = True
        args.force_live = False
        args.yes = True

        was_enabled = terminal_colors.colors_enabled()
        terminal_colors.disable_colors()

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch("qsync.terminal_output.sys.stdout", stdout),
            patch("qsync.terminal_output.sys.stderr", stderr),
        ):
            handle_activate(args)

        terminal_colors.enable_colors(was_enabled)

        output = stdout.getvalue()
        self.assertIn("Current status:", output)
        self.assertIn("Target status:", output)
        self.assertIn("DRY-RUN", output)


if __name__ == "__main__":
    unittest.main()
