"""Tests for master dimension sync orchestrator integration."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class SyncMasterIntegrationTests(unittest.TestCase):
    """Test master integration with sync orchestrator."""

    def test_detect_master_changes_staged(self) -> None:
        """Test detection of staged master changes."""
        from qsync.pending_stage import save_pending, PendingStagedChanges, MasterPendingPayload
        from qsync.dimensions.master_detect import detect_changes

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "surveys" / "pending" / "master").mkdir(parents=True)

            # Create staged pending
            with patch("qsync.pending_stage.resolve_root", return_value=root):
                record = PendingStagedChanges(
                    survey_id="SV_TEST",
                    dimension="master",
                    payload=MasterPendingPayload(
                        survey_ids=["SV_TEST"],
                        snapshot_hash="test_hash",
                        changes=[
                            {
                                "survey_id": "SV_TEST",
                                "changes": [
                                    {"field": "SurveyName", "old_value": "Old", "new_value": "New"}
                                ],
                            }
                        ],
                    ),
                )
                save_pending(record)

                result = detect_changes("SV_TEST")

        self.assertTrue(result.has_changes)
        self.assertEqual(result.status_kind, "staged")
        self.assertEqual(result.dimension, "master")
        self.assertIn("Staged", result.change_summary)

    def test_detect_master_changes_unstaged(self) -> None:
        """Test detection of unstaged master changes (CSV diffs)."""
        from qsync.dimensions.master_detect import detect_changes

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "surveys" / "qualtrics_master_snapshots").mkdir(parents=True)
            csv_path = root / "surveys" / "qualtrics_master.csv"

            # Create snapshot
            snapshot = {
                "survey_id": "SV_TEST",
                "survey_name": "Test",
                "sections": {
                    "metadata": {"data": {"SurveyName": "Old Name"}},
                    "options": {"data": {}},
                    "status": {"data": {}},
                },
            }
            snapshot_path = root / "surveys" / "qualtrics_master_snapshots" / "SV_TEST.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

            # Create CSV with change
            csv_content = "SurveyID,SurveyName\nSV_TEST,New Name\n"
            csv_path.write_text(csv_content, encoding="utf-8")

            with patch("qsync.survey_master.resolve_root", return_value=root):
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
                        },
                    }
                    result = detect_changes("SV_TEST")

        self.assertTrue(result.has_changes)
        self.assertEqual(result.status_kind, "unstaged")
        self.assertIn("Unstaged", result.change_summary)

    def test_detect_master_changes_none(self) -> None:
        """Test detection when no master changes exist."""
        from qsync.dimensions.master_detect import detect_changes

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "surveys" / "qualtrics_master_snapshots").mkdir(parents=True)
            csv_path = root / "surveys" / "qualtrics_master.csv"

            # Create snapshot
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

            # Create CSV matching snapshot
            csv_content = "SurveyID,SurveyName\nSV_TEST,Test\n"
            csv_path.write_text(csv_content, encoding="utf-8")

            with patch("qsync.survey_master.resolve_root", return_value=root):
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
                        },
                    }
                    result = detect_changes("SV_TEST")

        self.assertFalse(result.has_changes)
        self.assertEqual(result.status_kind, "none")

    def test_detect_master_conflicts(self) -> None:
        """Test master conflict detection with translations."""
        from qsync.sync_orchestrator import detect_master_conflicts, SurveyChanges
        from qsync.dimensions.types import DimensionChanges

        # Create survey changes with both master and translations staged
        changes = SurveyChanges(
            survey_id="SV_TEST",
            survey_name="Test",
            dimensions={
                "master": DimensionChanges(
                    dimension="master",
                    has_changes=True,
                    change_summary="✓ Staged: 1 field(s)",
                    affected_qids=set(),
                    status_kind="staged",
                    edit_count=1,
                ),
                "translations": DimensionChanges(
                    dimension="translations",
                    has_changes=True,
                    change_summary="✓ Staged: 2 QIDs",
                    affected_qids={"QID1", "QID2"},
                    status_kind="staged",
                    edit_count=2,
                ),
            },
        )

        warnings = detect_master_conflicts(changes)

        # Should warn about master + translations both staged
        self.assertGreater(len(warnings), 0)
        self.assertTrue(any("translations" in w for w in warnings))

    def test_detect_dimension_changes_master(self) -> None:
        """Test that sync orchestrator can detect master changes."""
        from qsync.sync_orchestrator import detect_dimension_changes

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "surveys" / "qualtrics_master_snapshots").mkdir(parents=True)
            csv_path = root / "surveys" / "qualtrics_master.csv"

            # Create snapshot + CSV with no changes
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

            csv_content = "SurveyID,SurveyName\nSV_TEST,Test\n"
            csv_path.write_text(csv_content, encoding="utf-8")

            with patch("qsync.survey_master.resolve_root", return_value=root):
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
                        },
                    }
                    result = detect_dimension_changes("SV_TEST", "master")

        self.assertEqual(result.dimension, "master")
        self.assertFalse(result.has_changes)


if __name__ == "__main__":
    unittest.main()
