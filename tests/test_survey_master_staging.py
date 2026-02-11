"""Tests for survey master staging functionality."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class SurveyMasterStagingTests(unittest.TestCase):
    """Test master stage command."""

    def test_stage_master_validates_csv(self) -> None:
        """Test that stage validates CSV before staging."""
        from qsync.survey_master import stage_master

        # Invalid CSV: missing required fields
        headers = ["InvalidField"]
        rows = [{"InvalidField": "value"}]

        result = stage_master(csv_headers=headers, csv_rows=rows)

        self.assertEqual(result["staged_surveys"], 0)
        self.assertGreater(len(result["validation_errors"]), 0)

    def test_stage_master_creates_pending(self) -> None:
        """Test that stage creates pending records."""
        from qsync.pending_stage import load_pending, MasterPendingPayload
        from qsync.survey_master import stage_master

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "surveys" / "qualtrics_master_snapshots").mkdir(parents=True)
            (root / "surveys" / "pending" / "master").mkdir(parents=True)

            # Create mock snapshot with all required fields
            snapshot = {
                "survey_id": "SV_TEST",
                "survey_name": "Test Survey",
                "schema_version": "test",
                "pulled_at": "2026-01-01T00:00:00Z",
                "sections": {
                    "metadata": {"data": {"SurveyName": "Old Name"}},
                    "options": {"data": {}},
                    "status": {"data": {}},
                },
            }
            snapshot_path = root / "surveys" / "qualtrics_master_snapshots" / "SV_TEST.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

            # Valid CSV with change
            headers = ["SurveyID", "SurveyName"]
            rows = [{"SurveyID": "SV_TEST", "SurveyName": "New Name"}]

            with patch("qsync.survey_master.resolve_root", return_value=root):
                with patch("qsync.pending_stage.resolve_root", return_value=root):
                    with patch("qsync.survey_master._parse_mapping_csv") as mock_mapping:
                        mock_mapping.return_value = {
                            "SurveyID": {
                                "field_name": "SurveyID",
                                "domain": "survey_metadata",
                                "survey_master": "read",
                                "object_path": "survey_id",
                            },
                            "SurveyName": {
                                "field_name": "SurveyName",
                                "domain": "survey_metadata",
                                "survey_master": "write",
                                "object_path": "SurveyName",
                            }
                        }
                        result = stage_master(csv_headers=headers, csv_rows=rows, verbose=False)

            # Should have staged 1 survey
            self.assertEqual(result["staged_surveys"], 1)
            self.assertEqual(result["total_changes"], 1)

            # Check pending was created
            with patch("qsync.pending_stage.resolve_root", return_value=root):
                pending = load_pending("SV_TEST", "master")

            self.assertIsNotNone(pending)
            self.assertEqual(pending.dimension, "master")
            self.assertIsInstance(pending.payload, MasterPendingPayload)
            self.assertEqual(len(pending.payload.changes), 1)
            self.assertIn("SV_TEST", pending.payload.survey_ids)

    def test_stage_master_snapshot_hash(self) -> None:
        """Test that snapshot hash is computed correctly."""
        from qsync.survey_master import _compute_snapshot_hash

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "surveys" / "qualtrics_master_snapshots").mkdir(parents=True)

            # Create snapshot with required fields
            snapshot = {
                "survey_id": "SV_TEST",
                "survey_name": "Test",
                "sections": {
                    "metadata": {"data": {"SurveyName": "Test"}},
                    "options": {"data": {}},
                    "status": {"data": {}},
                },
            }
            snapshot_path = root / "surveys" / "qualtrics_master_snapshots" / "SV_TEST.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

            with patch("qsync.survey_master.resolve_root", return_value=root):
                hash1 = _compute_snapshot_hash(["SV_TEST"])
                hash2 = _compute_snapshot_hash(["SV_TEST"])

            # Same snapshot should produce same hash
            self.assertEqual(hash1, hash2)
            self.assertEqual(len(hash1), 64)  # SHA256 hex digest

    def test_stage_master_clears_stale_pending(self) -> None:
        """Test that stage clears pending when no changes."""
        from qsync.pending_stage import save_pending, load_pending, PendingStagedChanges, MasterPendingPayload
        from qsync.survey_master import stage_master

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "surveys" / "qualtrics_master_snapshots").mkdir(parents=True)
            (root / "surveys" / "pending" / "master").mkdir(parents=True)

            # Create snapshot with required fields
            snapshot = {
                "survey_id": "SV_TEST",
                "survey_name": "Test",
                "sections": {
                    "metadata": {"data": {"SurveyName": "Test"}},
                    "options": {"data": {}},
                    "status": {"data": {}},
                },
            }
            snapshot_path = root / "surveys" / "qualtrics_master_snapshots" / "SV_TEST.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

            # Create stale pending
            with patch("qsync.pending_stage.resolve_root", return_value=root):
                stale_record = PendingStagedChanges(
                    survey_id="SV_TEST",
                    dimension="master",
                    payload=MasterPendingPayload(
                        survey_ids=["SV_TEST"],
                        snapshot_hash="old_hash",
                        changes=[],
                    ),
                )
                save_pending(stale_record)

            # CSV matches snapshot (no changes)
            headers = ["SurveyID", "SurveyName"]
            rows = [{"SurveyID": "SV_TEST", "SurveyName": "Test"}]

            with patch("qsync.survey_master.resolve_root", return_value=root):
                with patch("qsync.pending_stage.resolve_root", return_value=root):
                    with patch("qsync.survey_master._parse_mapping_csv") as mock_mapping:
                        mock_mapping.return_value = {
                            "SurveyID": {
                                "field_name": "SurveyID",
                                "domain": "survey_metadata",
                                "survey_master": "read",
                                "object_path": "survey_id",
                            },
                            "SurveyName": {
                                "field_name": "SurveyName",
                                "domain": "survey_metadata",
                                "survey_master": "write",
                                "object_path": "SurveyName",
                            }
                        }
                        result = stage_master(csv_headers=headers, csv_rows=rows)

            # Should not stage anything
            self.assertEqual(result["staged_surveys"], 0)

            # Pending should be cleared
            with patch("qsync.pending_stage.resolve_root", return_value=root):
                pending = load_pending("SV_TEST", "master")
            self.assertIsNone(pending)


if __name__ == "__main__":
    unittest.main()
