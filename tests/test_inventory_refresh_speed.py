import unittest
from unittest.mock import patch


class SurveyInventoryRefreshSpeedTests(unittest.TestCase):
    @patch("qsync.survey_inventory.fetch_surveys")
    @patch("qsync.survey_inventory.fetch_survey_flow_payload")
    @patch("qsync.survey_inventory.fetch_current_user", return_value={"userId": "UR_X"})
    @patch(
        "qsync.survey_inventory.load_existing_metadata",
        return_value={
            "SV_TEST": {
                "locked": False,
                "focal": False,
                "component": "pre",
                "stage": "main",
                "cntry": "US",
            }
        },
    )
    @patch("qsync.survey_inventory.load_focal_snapshot", return_value={})
    @patch("qsync.survey_inventory.load_cached_inventory_records")
    def test_full_refresh_skips_flow_when_last_modified_unchanged(
        self,
        mock_previous_records,
        _mock_focal,
        _mock_existing_meta,
        _mock_current_user,
        mock_fetch_flow,
        mock_fetch_surveys,
    ) -> None:
        from qsync.survey_inventory import refresh_inventory

        mock_previous_records.return_value = {
            "SV_TEST": {
                "id": "SV_TEST",
                "lastModified": "2026-02-01T00:00:00Z",
                "cntry": "US",
            }
        }
        mock_fetch_surveys.return_value = [
            {
                "id": "SV_TEST",
                "name": "Test Survey",
                "ownerId": "UR_X",
                "isActive": True,
                "creationDate": "2026-01-01T00:00:00Z",
                "lastModified": "2026-02-01T00:00:00Z",
            }
        ]
        mock_fetch_flow.side_effect = AssertionError(
            "SurveyFlow should not be fetched for unchanged surveys with cached cntry."
        )

        inventory, _ = refresh_inventory(
            "example.qualtrics.com",
            {"X-API-TOKEN": "x"},
            dry_run=True,
            quiet=True,
        )

        self.assertEqual(len(inventory), 1)
        self.assertEqual(inventory[0]["cntry"], "US")
        mock_fetch_flow.assert_not_called()

    @patch("qsync.survey_inventory.fetch_surveys")
    @patch("qsync.survey_inventory.fetch_survey_flow_payload")
    @patch("qsync.survey_inventory.fetch_current_user", return_value={"userId": "UR_X"})
    @patch(
        "qsync.survey_inventory.load_existing_metadata",
        return_value={
            "SV_TEST": {
                "locked": False,
                "focal": False,
                "component": "pre",
                "stage": "main",
                "cntry": "US",
            }
        },
    )
    @patch("qsync.survey_inventory.load_focal_snapshot", return_value={})
    @patch("qsync.survey_inventory.load_cached_inventory_records")
    def test_full_refresh_fetches_flow_when_last_modified_changes(
        self,
        mock_previous_records,
        _mock_focal,
        _mock_existing_meta,
        _mock_current_user,
        mock_fetch_flow,
        mock_fetch_surveys,
    ) -> None:
        from qsync.survey_inventory import refresh_inventory

        mock_previous_records.return_value = {
            "SV_TEST": {
                "id": "SV_TEST",
                "lastModified": "2026-01-01T00:00:00Z",
                "cntry": "US",
            }
        }
        mock_fetch_surveys.return_value = [
            {
                "id": "SV_TEST",
                "name": "Test Survey",
                "ownerId": "UR_X",
                "isActive": True,
                "creationDate": "2026-01-01T00:00:00Z",
                "lastModified": "2026-02-01T00:00:00Z",
            }
        ]
        mock_fetch_flow.return_value = {
            "result": {
                "SurveyFlow": {
                    "Flow": [
                        {
                            "Type": "EmbeddedData",
                            "EmbeddedData": [{"Field": "country", "Value": "IE"}],
                        }
                    ]
                }
            }
        }

        inventory, _ = refresh_inventory(
            "example.qualtrics.com",
            {"X-API-TOKEN": "x"},
            dry_run=True,
            quiet=True,
        )

        self.assertEqual(len(inventory), 1)
        self.assertEqual(inventory[0]["cntry"], "IE")
        mock_fetch_flow.assert_called_once_with(
            "example.qualtrics.com",
            {"X-API-TOKEN": "x"},
            "SV_TEST",
        )


if __name__ == "__main__":
    unittest.main()
