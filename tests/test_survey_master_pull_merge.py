"""Tests for survey master pull merge functionality."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class SurveyMasterPullMergeTests(unittest.TestCase):
    """Test master pull non-destructive merge logic."""

    def test_pull_master_preserves_overrides(self) -> None:
        """Test that pull preserves user edits via merge."""
        from qsync.survey_master import pull_master, generate_master_csv_from_snapshots

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "surveys" / "qualtrics_master_snapshots").mkdir(parents=True)
            csv_path = root / "surveys" / "qualtrics_master.csv"

            # Create existing CSV with user edit
            existing_csv = "SurveyID,SurveyName\nSV_TEST,User Edited Name\n"
            csv_path.write_text(existing_csv, encoding="utf-8")

            # Create snapshot with different baseline
            snapshot = {
                "survey_id": "SV_TEST",
                "survey_name": "Baseline Name",
                "sections": {
                    "metadata": {"SurveyName": "Baseline Name"},
                    "options": {},
                    "status": {},
                },
            }
            snapshot_path = root / "surveys" / "qualtrics_master_snapshots" / "SV_TEST.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

            # Mock API fetch to do nothing (just use existing snapshot)
            with patch("qsync.survey_master.resolve_root", return_value=root):
                with patch("qsync.survey_master.load_focal_snapshot", return_value={"SV_TEST": True}):
                    with patch("qsync.survey_master.get_client_config", return_value=("http://test", {})):
                        with patch("qsync.survey_master._fetch_survey_name", return_value="Test"):
                            with patch("qsync.survey_master._fetch_endpoint", return_value=({}, 200)):
                                with patch("qsync.survey_master._parse_mapping_csv") as mock_mapping:
                                    mock_mapping.return_value = {
                                        "SurveyName": {
                                            "field_name": "SurveyName",
                                            "domain": "survey_metadata",
                                            "object_path": "SurveyName",
                                        }
                                    }
                                    # Mock generate to return baseline
                                    with patch("qsync.survey_master.generate_master_csv_from_snapshots") as mock_gen:
                                        mock_gen.return_value = [
                                            ["SurveyID", "SurveyName"],
                                            ["SV_TEST", "Baseline Name"],
                                        ]
                                        _, result_path = pull_master(survey_ids=["SV_TEST"], force_overwrite=False)

            # Read result
            result_csv = csv_path.read_text(encoding="utf-8")

            # User edit should be preserved
            self.assertIn("User Edited Name", result_csv)
            self.assertNotIn("Baseline Name", result_csv)

    def test_pull_master_creates_backup(self) -> None:
        """Test that backup CSV is created before merge."""
        from qsync.survey_master import pull_master

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "surveys" / "qualtrics_master_snapshots").mkdir(parents=True)
            csv_path = root / "surveys" / "qualtrics_master.csv"
            backup_path = root / "surveys" / "qualtrics_master.csv.bak"

            # Create existing CSV
            existing_csv = "SurveyID,SurveyName\nSV_TEST,Original\n"
            csv_path.write_text(existing_csv, encoding="utf-8")

            # Create snapshot
            snapshot = {
                "survey_id": "SV_TEST",
                "survey_name": "Test",
                "sections": {"metadata": {"SurveyName": "Test"}, "options": {}, "status": {}},
            }
            snapshot_path = root / "surveys" / "qualtrics_master_snapshots" / "SV_TEST.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

            with patch("qsync.survey_master.resolve_root", return_value=root):
                with patch("qsync.survey_master.load_focal_snapshot", return_value={"SV_TEST": True}):
                    with patch("qsync.survey_master.get_client_config", return_value=("http://test", {})):
                        with patch("qsync.survey_master._fetch_survey_name", return_value="Test"):
                            with patch("qsync.survey_master._fetch_endpoint", return_value=({}, 200)):
                                with patch("qsync.survey_master._parse_mapping_csv") as mock_mapping:
                                    mock_mapping.return_value = {}
                                    with patch("qsync.survey_master.generate_master_csv_from_snapshots") as mock_gen:
                                        mock_gen.return_value = [
                                            ["SurveyID", "SurveyName"],
                                            ["SV_TEST", "Test"],
                                        ]
                                        pull_master(survey_ids=["SV_TEST"], force_overwrite=False)

            # Backup should exist
            self.assertTrue(backup_path.exists())
            backup_content = backup_path.read_text(encoding="utf-8")
            self.assertIn("Original", backup_content)

    def test_pull_master_force_overwrite(self) -> None:
        """Test that --force-overwrite skips merge logic."""
        from qsync.survey_master import pull_master

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "surveys" / "qualtrics_master_snapshots").mkdir(parents=True)
            csv_path = root / "surveys" / "qualtrics_master.csv"

            # Create existing CSV with user edit
            existing_csv = "SurveyID,SurveyName\nSV_TEST,User Edit\n"
            csv_path.write_text(existing_csv, encoding="utf-8")

            # Create snapshot
            snapshot = {
                "survey_id": "SV_TEST",
                "survey_name": "Fresh Baseline",
                "sections": {"metadata": {"SurveyName": "Fresh Baseline"}, "options": {}, "status": {}},
            }
            snapshot_path = root / "surveys" / "qualtrics_master_snapshots" / "SV_TEST.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

            with patch("qsync.survey_master.resolve_root", return_value=root):
                with patch("qsync.survey_master.load_focal_snapshot", return_value={"SV_TEST": True}):
                    with patch("qsync.survey_master.get_client_config", return_value=("http://test", {})):
                        with patch("qsync.survey_master._fetch_survey_name", return_value="Test"):
                            with patch("qsync.survey_master._fetch_endpoint", return_value=({}, 200)):
                                with patch("qsync.survey_master._parse_mapping_csv") as mock_mapping:
                                    mock_mapping.return_value = {
                                        "SurveyName": {
                                            "field_name": "SurveyName",
                                            "object_path": "SurveyName",
                                        }
                                    }
                                    with patch("qsync.survey_master.generate_master_csv_from_snapshots") as mock_gen:
                                        mock_gen.return_value = [
                                            ["SurveyID", "SurveyName"],
                                            ["SV_TEST", "Fresh Baseline"],
                                        ]
                                        pull_master(survey_ids=["SV_TEST"], force_overwrite=True)

            # User edit should be discarded
            result_csv = csv_path.read_text(encoding="utf-8")
            self.assertIn("Fresh Baseline", result_csv)
            self.assertNotIn("User Edit", result_csv)

    def test_pull_master_no_existing_csv(self) -> None:
        """Test that pull works correctly when no existing CSV."""
        from qsync.survey_master import pull_master

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "surveys" / "qualtrics_master_snapshots").mkdir(parents=True)
            csv_path = root / "surveys" / "qualtrics_master.csv"

            # No existing CSV
            self.assertFalse(csv_path.exists())

            # Create snapshot
            snapshot = {
                "survey_id": "SV_TEST",
                "survey_name": "Test",
                "sections": {"metadata": {"SurveyName": "Test"}, "options": {}, "status": {}},
            }
            snapshot_path = root / "surveys" / "qualtrics_master_snapshots" / "SV_TEST.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

            with patch("qsync.survey_master.resolve_root", return_value=root):
                with patch("qsync.survey_master.load_focal_snapshot", return_value={"SV_TEST": True}):
                    with patch("qsync.survey_master.get_client_config", return_value=("http://test", {})):
                        with patch("qsync.survey_master._fetch_survey_name", return_value="Test"):
                            with patch("qsync.survey_master._fetch_endpoint", return_value=({}, 200)):
                                with patch("qsync.survey_master._parse_mapping_csv") as mock_mapping:
                                    mock_mapping.return_value = {}
                                    with patch("qsync.survey_master.generate_master_csv_from_snapshots") as mock_gen:
                                        mock_gen.return_value = [
                                            ["SurveyID", "SurveyName"],
                                            ["SV_TEST", "Test"],
                                        ]
                                        _, result_path = pull_master(survey_ids=["SV_TEST"])

            # CSV should be created
            self.assertTrue(csv_path.exists())
            result_csv = csv_path.read_text(encoding="utf-8")
            self.assertIn("Test", result_csv)


if __name__ == "__main__":
    unittest.main()
