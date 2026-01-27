import unittest
from unittest.mock import MagicMock, patch


class PublishSurveyDefinitionTests(unittest.TestCase):
    @patch("qsync.qualtrics_client.send_api_request")
    @patch("qsync.qualtrics_client.get_client_config")
    def test_publish_payload_uses_titlecase_fields(
        self, mock_config, mock_send
    ) -> None:
        mock_config.return_value = ("example.qualtrics.com", {"X-API-TOKEN": "test"})

        resp = MagicMock()
        resp.json.return_value = {"result": {"ok": True}}
        mock_send.return_value = resp

        from qsync.qualtrics_client import publish_survey_definition

        payload = publish_survey_definition(
            "SV_TEST",
            description="Hello",
            published=True,
            context={"origin": "test"},
        )
        self.assertEqual(payload, {"result": {"ok": True}})

        _, kwargs = mock_send.call_args
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(kwargs["path"], "survey-definitions/SV_TEST/versions")
        self.assertEqual(kwargs["json"], {"Description": "Hello", "Published": True})
        self.assertIn("log_meta_from_response", kwargs)

    @patch("qsync.qualtrics_client.send_api_request")
    def test_publish_rejects_long_descriptions(self, mock_send) -> None:
        from qsync.qualtrics_client import (
            SURVEY_VERSION_DESCRIPTION_MAX_CHARS,
            publish_survey_definition,
        )

        too_long = "x" * (SURVEY_VERSION_DESCRIPTION_MAX_CHARS + 1)
        with self.assertRaises(ValueError):
            publish_survey_definition("SV_TEST", description=too_long)
        mock_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
