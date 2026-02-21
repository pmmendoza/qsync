import unittest
from unittest.mock import MagicMock, patch


class ApiPushLogMetaFromResponseTests(unittest.TestCase):
    @patch("qsync.api_push.ensure_unlocked")
    @patch("qsync.api_push.log_push_event")
    @patch("qsync.api_push.requests.request")
    def test_merges_response_meta_into_log(
        self, mock_request, mock_log, mock_unlock
    ) -> None:
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        resp.reason = "OK"
        resp.json.return_value = {
            "result": {"metadata": {"versionID": "VER_1", "versionNumber": 1}}
        }
        mock_request.return_value = resp

        from qsync.api_push import send_api_request

        def extract_meta(r):
            data = r.json()
            md = data["result"]["metadata"]
            return {
                "version_id": md["versionID"],
                "version_number": md["versionNumber"],
            }

        send_api_request(
            action="test.action",
            method="POST",
            base_url="example.qualtrics.com",
            headers={"X-API-TOKEN": "x"},
            path="survey-definitions/SV_TEST/versions",
            survey_id="SV_TEST",
            log_meta={"description": "hello"},
            log_meta_from_response=extract_meta,
            json={"Description": "hello", "Published": True},
        )

        self.assertTrue(mock_log.called)
        _, kwargs = mock_log.call_args
        meta = kwargs.get("meta") or {}
        self.assertEqual(meta["description"], "hello")
        self.assertEqual(meta["version_id"], "VER_1")
        self.assertEqual(meta["version_number"], 1)

    @patch("qsync.api_push.ensure_unlocked")
    @patch("qsync.api_push.logger")
    @patch("qsync.api_push.log_push_event")
    @patch("qsync.api_push.requests.request")
    def test_extractor_error_does_not_break_request(
        self, mock_request, mock_log, mock_logger, mock_unlock
    ) -> None:
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        resp.reason = "OK"
        mock_request.return_value = resp

        from qsync.api_push import send_api_request

        def extract_meta(_):
            raise RuntimeError("boom")

        send_api_request(
            action="test.action",
            method="POST",
            base_url="example.qualtrics.com",
            headers={"X-API-TOKEN": "x"},
            path="survey-definitions/SV_TEST/versions",
            survey_id="SV_TEST",
            log_meta={"description": "hello"},
            log_meta_from_response=extract_meta,
            json={"Description": "hello", "Published": True},
        )

        self.assertTrue(mock_log.called)
        _, kwargs = mock_log.call_args
        meta = kwargs.get("meta") or {}
        self.assertEqual(meta["description"], "hello")
        self.assertNotIn("version_id", meta)
        self.assertTrue(mock_logger.warning.called)

    @patch("qsync.api_push.ensure_unlocked")
    @patch("qsync.api_push.log_push_event")
    @patch("qsync.api_push.requests.request")
    def test_logs_include_duration_ms(
        self, mock_request, mock_log, mock_unlock
    ) -> None:
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        resp.reason = "OK"
        mock_request.return_value = resp

        from qsync.api_push import send_api_request

        send_api_request(
            action="test.duration",
            method="POST",
            base_url="example.qualtrics.com",
            headers={"X-API-TOKEN": "x"},
            path="survey-definitions/SV_TEST/versions",
            survey_id="SV_TEST",
            json={"Description": "hello", "Published": True},
        )

        self.assertTrue(mock_log.called)
        _, kwargs = mock_log.call_args
        self.assertIn("duration_ms", kwargs)
        self.assertGreaterEqual(float(kwargs["duration_ms"]), 0.0)


if __name__ == "__main__":
    unittest.main()
