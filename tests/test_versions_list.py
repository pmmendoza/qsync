import unittest
from unittest.mock import MagicMock, patch


class ListSurveyVersionsTests(unittest.TestCase):
    @patch("qsync.qualtrics_client.send_api_request")
    @patch("qsync.qualtrics_client.get_client_config")
    def test_marks_current_published_version(self, mock_config, mock_send) -> None:
        mock_config.return_value = ("example.qualtrics.com", {"X-API-TOKEN": "test"})

        resp = MagicMock()
        resp.json.return_value = {
            "result": {
                "elements": [
                    {
                        "metadata": {
                            "versionID": "VER_3",
                            "versionNumber": 3,
                            "published": False,
                            "description": "snapshot",
                        }
                    },
                    {
                        "metadata": {
                            "versionID": "VER_2",
                            "versionNumber": 2,
                            "published": True,
                            "description": "published v2",
                        }
                    },
                    {
                        "metadata": {
                            "versionID": "VER_1",
                            "versionNumber": 1,
                            "published": True,
                            "description": "published v1",
                        }
                    },
                ]
            }
        }
        mock_send.return_value = resp

        from qsync.qualtrics_client import list_survey_versions

        out = list_survey_versions("SV_TEST")
        self.assertEqual(out["survey_id"], "SV_TEST")
        self.assertEqual(out["current_published_version_id"], "VER_2")

        versions = out["versions"]
        self.assertEqual(len(versions), 3)
        self.assertFalse(versions[0]["current_published"])
        self.assertTrue(versions[1]["current_published"])
        self.assertFalse(versions[2]["current_published"])

        _, kwargs = mock_send.call_args
        self.assertEqual(kwargs["method"], "GET")
        self.assertEqual(kwargs["path"], "survey-definitions/SV_TEST/versions")
        self.assertEqual(kwargs["log_event"], False)


if __name__ == "__main__":
    unittest.main()
