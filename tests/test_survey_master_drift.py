"""Tests for survey master drift detection."""

import unittest
from unittest.mock import patch, MagicMock


class SurveyMasterDriftTests(unittest.TestCase):
    """Test drift detection logic."""

    def test_drift_no_changes_no_drift(self) -> None:
        """CSV matches snapshot and live → no drift."""
        from qsync.survey_master import detect_drift

        csv_row = {"SurveyName": "My Survey"}

        snapshot = {
            "schema_version": "20251220-abc123",
            "sections": {
                "metadata": {"data": {"SurveyName": "My Survey"}},
            },
        }

        mapping = {
            "SurveyName": {
                "field_name": "SurveyName",
                "domain": "survey_metadata",
                "survey_master": "write",
                "object_path": "SurveyName",
            },
        }

        def mock_send_api_request(*args, **kwargs):
            resp = MagicMock()
            resp.json.return_value = {"result": {"SurveyName": "My Survey"}}
            return resp

        with patch("qsync.survey_master._parse_mapping_csv", return_value=mapping):
            with patch("qsync.survey_master.load_snapshot", return_value=snapshot):
                with patch(
                    "qsync.survey_master._derive_endpoint", return_value="metadata"
                ):
                    with patch(
                        "qsync.survey_master.send_api_request",
                        side_effect=mock_send_api_request,
                    ):
                        with patch(
                            "qsync.survey_master._compute_schema_version",
                            return_value="20251220-abc123",
                        ):
                            drift_result = detect_drift("SV_001", csv_row)

        self.assertEqual(drift_result.get("drifted_fields", []), [])
        self.assertTrue(drift_result.get("schema_version_matches"))

    def test_drift_detects_metadata_drift(self) -> None:
        """Snapshot ≠ live metadata → drift detected."""
        from qsync.survey_master import detect_drift

        csv_row = {"SurveyName": "New Name"}

        snapshot = {
            "schema_version": "20251220-abc123",
            "sections": {
                "metadata": {"data": {"SurveyName": "Old Name"}},
            },
        }

        mapping = {
            "SurveyName": {
                "field_name": "SurveyName",
                "domain": "survey_metadata",
                "survey_master": "write",
                "object_path": "SurveyName",
            },
        }

        def mock_send_api_request(*args, **kwargs):
            # Return live value that differs from snapshot
            resp = MagicMock()
            resp.json.return_value = {"result": {"SurveyName": "Current Live Name"}}
            return resp

        with patch("qsync.survey_master._parse_mapping_csv", return_value=mapping):
            with patch("qsync.survey_master.load_snapshot", return_value=snapshot):
                with patch(
                    "qsync.survey_master._derive_endpoint", return_value="metadata"
                ):
                    with patch(
                        "qsync.survey_master.send_api_request",
                        side_effect=mock_send_api_request,
                    ):
                        with patch(
                            "qsync.survey_master._compute_schema_version",
                            return_value="20251220-abc123",
                        ):
                            drift_result = detect_drift("SV_001", csv_row)

        self.assertGreater(len(drift_result.get("drifted_fields", [])), 0)
        self.assertTrue(drift_result.get("schema_version_matches"))

    def test_drift_schema_version_mismatch(self) -> None:
        """Different schema_version → warning."""
        from qsync.survey_master import detect_drift

        csv_row = {}

        snapshot = {
            "schema_version": "20251219-old123",  # Old version
        }

        mapping = {}

        with patch("qsync.survey_master._parse_mapping_csv", return_value=mapping):
            with patch("qsync.survey_master.load_snapshot", return_value=snapshot):
                with patch(
                    "qsync.survey_master._compute_schema_version",
                    return_value="20251220-new123",
                ):
                    drift_result = detect_drift("SV_001", csv_row)

        self.assertFalse(drift_result.get("schema_version_matches"))
        self.assertIsNotNone(drift_result.get("schema_mismatch_warning"))

    def test_drift_missing_snapshot_raises(self) -> None:
        """Error when snapshot not found."""
        from qsync.survey_master import detect_drift

        csv_row = {}

        with patch("qsync.survey_master.load_snapshot", side_effect=FileNotFoundError):
            with self.assertRaises(FileNotFoundError):
                detect_drift("SV_NONEXISTENT", csv_row)

    def test_drift_unchanged_fields_not_checked(self) -> None:
        """Only changed fields fetched from API."""
        from qsync.survey_master import detect_drift

        # CSV only changes one field
        csv_row = {"SurveyName": "New Name"}

        snapshot = {
            "schema_version": "20251220-abc123",
            "sections": {
                "metadata": {
                    "data": {"SurveyName": "Old Name", "SurveyStatus": "Active"}
                },
            },
        }

        mapping = {
            "SurveyName": {
                "field_name": "SurveyName",
                "domain": "survey_metadata",
                "survey_master": "write",
                "object_path": "SurveyName",
            },
            "SurveyStatus": {
                "field_name": "SurveyStatus",
                "domain": "survey_metadata",
                "survey_master": "write",
                "object_path": "SurveyStatus",
            },
        }

        api_calls = []

        def mock_send_api_request(*args, **kwargs):
            api_calls.append(kwargs.get("path"))
            resp = MagicMock()
            resp.json.return_value = {
                "result": {"SurveyName": "New Name", "SurveyStatus": "Active"}
            }
            return resp

        with patch("qsync.survey_master._parse_mapping_csv", return_value=mapping):
            with patch("qsync.survey_master.load_snapshot", return_value=snapshot):
                with patch(
                    "qsync.survey_master._derive_endpoint", return_value="metadata"
                ):
                    with patch(
                        "qsync.survey_master.send_api_request",
                        side_effect=mock_send_api_request,
                    ):
                        with patch(
                            "qsync.survey_master._compute_schema_version",
                            return_value="20251220-abc123",
                        ):
                            drift_result = detect_drift("SV_001", csv_row)

        # Only the changed field should be checked for drift.
        drifted_fields = drift_result.get("drifted_fields", [])
        self.assertEqual(len(drifted_fields), 1)
        self.assertEqual(drifted_fields[0].get("field"), "SurveyName")
        self.assertEqual(len(api_calls), 1)


if __name__ == "__main__":
    unittest.main()
