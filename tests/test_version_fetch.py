import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class FetchSurveyVersionTests(unittest.TestCase):
    @patch("qsync.qualtrics_client.send_api_request")
    @patch("qsync.qualtrics_client.get_client_config")
    def test_fetch_version_uses_expected_endpoint(self, mock_config, mock_send) -> None:
        mock_config.return_value = ("example.qualtrics.com", {"X-API-TOKEN": "test"})

        resp = MagicMock()
        resp.json.return_value = {"result": {"SurveyID": "SV_TEST", "Questions": {}}}
        mock_send.return_value = resp

        from qsync.qualtrics_client import fetch_survey_version

        payload = fetch_survey_version("SV_TEST", version_id="VER_123", fmt="json")
        self.assertEqual(payload["result"]["SurveyID"], "SV_TEST")

        _, kwargs = mock_send.call_args
        self.assertEqual(kwargs["method"], "GET")
        self.assertEqual(kwargs["path"], "survey-definitions/SV_TEST/versions/VER_123")
        self.assertEqual(kwargs["log_event"], False)
        self.assertEqual(kwargs.get("params"), None)

    @patch("qsync.cli_survey.fetch_survey_version")
    def test_cli_version_fetch_writes_output_file(self, mock_fetch) -> None:
        mock_fetch.return_value = {"result": {"SurveyID": "SV_TEST", "Questions": {}}}

        from qsync.cli_survey import handle_version_fetch

        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "version.json"
            args = MagicMock()
            args.survey_id = "SV_TEST"
            args.version_id = "VER_123"
            args.format = "json"
            args.output = str(out_path)
            args.json = False

            handle_version_fetch(args)

            self.assertTrue(out_path.exists())
            data = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(data["result"]["SurveyID"], "SV_TEST")


if __name__ == "__main__":
    unittest.main()
