from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import requests


def _json_response(status: int, payload: dict) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp.reason = "Bad Request" if status == 400 else "Forbidden"
    resp.headers["Content-Type"] = "application/json"
    resp._content = json.dumps(payload).encode("utf-8")
    return resp


class SendApiRequestErrorMessageTests(unittest.TestCase):
    @patch("qsync.api_push.requests.request")
    def test_qval3_translation_error_is_interpretable(self, mock_request) -> None:
        from qsync.api_push import send_api_request

        mock_request.return_value = _json_response(
            400,
            {
                "meta": {
                    "error": {
                        "errorCode": "QVAL_3",
                        "errorMessage": "Parameter QID30_QuestionText exceeds maximum length of 10000.",
                    }
                }
            },
        )

        with self.assertRaises(requests.HTTPError) as excinfo:
            send_api_request(
                action="test.translations.push",
                method="PUT",
                base_url="example.qualtrics.com",
                headers={"X-API-TOKEN": "x"},
                path="surveys/SV_TEST/translations/CS",
                log_event=False,
            )

        message = str(excinfo.exception)
        self.assertIn("Qualtrics API error 400 Bad Request (QVAL_3)", message)
        self.assertIn("Endpoint: PUT surveys/SV_TEST/translations/CS", message)
        self.assertIn("legacy translations endpoint has a hard 10,000-character", message)
        self.assertIn("use survey-definition question updates", message)

    @patch("qsync.api_push.requests.request")
    def test_403_includes_account_context_guidance(self, mock_request) -> None:
        from qsync.api_push import send_api_request

        mock_request.return_value = _json_response(
            403,
            {
                "meta": {
                    "error": {
                        "errorCode": "QAUTH_1",
                        "errorMessage": "Forbidden",
                    }
                }
            },
        )

        with (
            patch.dict("os.environ", {"QSYNC_ACCOUNT": "damian"}, clear=False),
            self.assertRaises(requests.HTTPError) as excinfo,
        ):
            send_api_request(
                action="test.account.context",
                method="GET",
                base_url="vuamsterdam.eu.qualtrics.com",
                headers={"X-API-TOKEN": "x"},
                path="survey-definitions/SV_TEST",
                log_event=False,
            )

        message = str(excinfo.exception)
        self.assertIn("Qualtrics API error 403 Forbidden (QAUTH_1)", message)
        self.assertIn("base_url=vuamsterdam.eu.qualtrics.com", message)
        self.assertIn("account=damian", message)
        self.assertIn("qsync account status", message)
        self.assertIn("QSYNC_ACCOUNT=<name>", message)


if __name__ == "__main__":
    unittest.main()
