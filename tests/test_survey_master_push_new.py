"""Tests for survey master push functionality (NEW behavior)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class SurveyMasterPushNewTests(unittest.TestCase):
    """Test master push command with new pending-based behavior."""

    def test_push_master_loads_pending(self) -> None:
        """Test that push loads pending records."""
        from qsync.pending_stage import save_pending, PendingStagedChanges, MasterPendingPayload
        from qsync.survey_master import push_master

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "surveys" / "pending" / "master").mkdir(parents=True)
            (root / "surveys" / "qualtrics_master_snapshots").mkdir(parents=True)

            # Create pending
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
                                "survey_name": "Test",
                                "changes": [
                                    {
                                        "field": "SurveyName",
                                        "old_value": "Old",
                                        "new_value": "New",
                                        "endpoint": "metadata",
                                        "is_dangerous": False,
                                    }
                                ],
                                "publish_required": True,
                                "has_dangerous_changes": False,
                            }
                        ],
                    ),
                )
                save_pending(record)

            # Create snapshot for drift detection with required fields
            snapshot = {
                "survey_id": "SV_TEST",
                "survey_name": "Test",
                "sections": {"metadata": {}, "options": {}, "status": {}},
            }
            snapshot_path = root / "surveys" / "qualtrics_master_snapshots" / "SV_TEST.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

            # Mock dependencies
            with patch("qsync.survey_master.resolve_root", return_value=root):
                with patch("qsync.pending_stage.resolve_root", return_value=root):
                    with patch("qsync.survey_inventory.load_focal_snapshot", return_value={"SV_TEST": True}):
                        with patch("qsync.survey_master._compute_snapshot_hash", return_value="test_hash"):
                            with patch("qsync.push_safeguards.enforce_push_safeguards"):
                                with patch("qsync.qualtrics_client.ensure_backup"):
                                    with patch("qsync.survey_master.capture_pre_apply_snapshot"):
                                        with patch("qsync.survey_master.get_client_config", return_value=("http://test", {})):
                                            with patch("qsync.survey_master._write_metadata", return_value=True):
                                                with patch("qsync.qualtrics_client.publish_survey_definition"):
                                                    with patch(
                                                        "qsync.qualtrics_client.refresh_survey_cache",
                                                        return_value=(MagicMock(), True),
                                                    ):
                                                        with patch(
                                                            "qsync.survey_master.refresh_snapshot_from_live",
                                                            return_value=root
                                                            / "surveys"
                                                            / "qualtrics_master_snapshots"
                                                            / "SV_TEST.json",
                                                        ):
                                                            result = push_master()

            # Should have pushed 1 survey
            self.assertEqual(result["surveys_pushed"], 1)
            self.assertEqual(result["surveys_published"], 1)

    def test_push_master_no_pending(self) -> None:
        """Test that push errors when no staged changes."""
        from qsync.survey_master import push_master

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "surveys" / "pending" / "master").mkdir(parents=True)

            with patch("qsync.survey_master.resolve_root", return_value=root):
                with patch("qsync.pending_stage.resolve_root", return_value=root):
                    with patch("qsync.survey_inventory.load_focal_snapshot", return_value={"SV_TEST": True}):
                        result = push_master()

            # Should have error
            self.assertEqual(result["total_surveys"], 0)
            self.assertGreater(len(result["errors"]), 0)
            self.assertIn("No staged master changes", result["errors"][0])

    def test_push_master_drift_detection(self) -> None:
        """Test that push blocks when snapshot hash mismatches."""
        from qsync.pending_stage import save_pending, PendingStagedChanges, MasterPendingPayload
        from qsync.survey_master import push_master

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "surveys" / "pending" / "master").mkdir(parents=True)
            (root / "surveys" / "qualtrics_master_snapshots").mkdir(parents=True)

            # Create pending with old hash
            with patch("qsync.pending_stage.resolve_root", return_value=root):
                record = PendingStagedChanges(
                    survey_id="SV_TEST",
                    dimension="master",
                    payload=MasterPendingPayload(
                        survey_ids=["SV_TEST"],
                        snapshot_hash="old_hash",
                        changes=[
                            {
                                "survey_id": "SV_TEST",
                                "changes": [{"field": "SurveyName"}],
                            }
                        ],
                    ),
                )
                save_pending(record)

            # Create snapshot with required fields
            snapshot = {"survey_id": "SV_TEST", "survey_name": "Test", "sections": {"metadata": {}, "options": {}, "status": {}}}
            snapshot_path = root / "surveys" / "qualtrics_master_snapshots" / "SV_TEST.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

            with patch("qsync.survey_master.resolve_root", return_value=root):
                with patch("qsync.pending_stage.resolve_root", return_value=root):
                    with patch("qsync.survey_inventory.load_focal_snapshot", return_value={"SV_TEST": True}):
                        with patch("qsync.survey_master._compute_snapshot_hash", return_value="new_hash"):
                            result = push_master()

            # Should have failed due to drift
            self.assertEqual(result["surveys_failed"], 1)
            self.assertIn("drift", result["details"][0]["reason"].lower())

    def test_push_master_no_publish_flag(self) -> None:
        """Test that --no-publish skips publish step."""
        from qsync.pending_stage import save_pending, PendingStagedChanges, MasterPendingPayload
        from qsync.survey_master import push_master

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "surveys" / "pending" / "master").mkdir(parents=True)
            (root / "surveys" / "qualtrics_master_snapshots").mkdir(parents=True)

            # Create pending
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
                                "changes": [{"field": "SurveyName", "endpoint": "metadata"}],
                                "publish_required": True,
                            }
                        ],
                    ),
                )
                save_pending(record)

            # Create snapshot with required fields
            snapshot = {"survey_id": "SV_TEST", "survey_name": "Test", "sections": {"metadata": {}, "options": {}, "status": {}}}
            snapshot_path = root / "surveys" / "qualtrics_master_snapshots" / "SV_TEST.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

            publish_mock = MagicMock()

            with patch("qsync.survey_master.resolve_root", return_value=root):
                with patch("qsync.pending_stage.resolve_root", return_value=root):
                    with patch("qsync.survey_inventory.load_focal_snapshot", return_value={"SV_TEST": True}):
                        with patch("qsync.survey_master._compute_snapshot_hash", return_value="test_hash"):
                            with patch("qsync.push_safeguards.enforce_push_safeguards"):
                                with patch("qsync.qualtrics_client.ensure_backup"):
                                    with patch("qsync.survey_master.capture_pre_apply_snapshot"):
                                        with patch("qsync.survey_master.get_client_config", return_value=("http://test", {})):
                                            with patch("qsync.survey_master._write_metadata", return_value=True):
                                                with patch("qsync.qualtrics_client.publish_survey_definition", publish_mock):
                                                    with patch(
                                                        "qsync.qualtrics_client.refresh_survey_cache",
                                                        return_value=(MagicMock(), True),
                                                    ):
                                                        with patch(
                                                            "qsync.survey_master.refresh_snapshot_from_live",
                                                            return_value=root
                                                            / "surveys"
                                                            / "qualtrics_master_snapshots"
                                                            / "SV_TEST.json",
                                                        ):
                                                            result = push_master(no_publish=True)

            # Should have pushed but not published
            self.assertEqual(result["surveys_pushed"], 1)
            self.assertEqual(result["surveys_published"], 0)
            publish_mock.assert_not_called()

    def test_push_master_clears_pending(self) -> None:
        """Test that pending is cleared after successful push."""
        from qsync.pending_stage import save_pending, load_pending, PendingStagedChanges, MasterPendingPayload
        from qsync.survey_master import push_master

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "surveys" / "pending" / "master").mkdir(parents=True)
            (root / "surveys" / "qualtrics_master_snapshots").mkdir(parents=True)

            # Create pending
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
                                "changes": [{"field": "SurveyName", "endpoint": "metadata"}],
                            }
                        ],
                    ),
                )
                save_pending(record)

            # Create snapshot with required fields
            snapshot = {"survey_id": "SV_TEST", "survey_name": "Test", "sections": {"metadata": {}, "options": {}, "status": {}}}
            snapshot_path = root / "surveys" / "qualtrics_master_snapshots" / "SV_TEST.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

            with patch("qsync.survey_master.resolve_root", return_value=root):
                with patch("qsync.pending_stage.resolve_root", return_value=root):
                    with patch("qsync.survey_inventory.load_focal_snapshot", return_value={"SV_TEST": True}):
                        with patch("qsync.survey_master._compute_snapshot_hash", return_value="test_hash"):
                            with patch("qsync.push_safeguards.enforce_push_safeguards"):
                                with patch("qsync.qualtrics_client.ensure_backup"):
                                    with patch("qsync.survey_master.capture_pre_apply_snapshot"):
                                        with patch("qsync.survey_master.get_client_config", return_value=("http://test", {})):
                                            with patch("qsync.survey_master._write_metadata", return_value=True):
                                                with patch(
                                                    "qsync.qualtrics_client.refresh_survey_cache",
                                                    return_value=(MagicMock(), True),
                                                ):
                                                    with patch(
                                                        "qsync.survey_master.refresh_snapshot_from_live",
                                                        return_value=root
                                                        / "surveys"
                                                        / "qualtrics_master_snapshots"
                                                        / "SV_TEST.json",
                                                    ):
                                                        push_master()

            # Pending should be cleared
            with patch("qsync.pending_stage.resolve_root", return_value=root):
                pending = load_pending("SV_TEST", "master")
            self.assertIsNone(pending)

    def test_push_master_multi_survey_from_stage_no_false_drift(self) -> None:
        """Multi-survey stage->push should not fail drift when snapshots are unchanged."""
        from qsync.survey_master import stage_master, push_master

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "surveys" / "pending" / "master").mkdir(parents=True)
            (root / "surveys" / "qualtrics_master_snapshots").mkdir(parents=True)

            snapshot_a = {
                "survey_id": "SV_A",
                "survey_name": "Survey A",
                "sections": {"metadata": {"data": {"SurveyName": "Old A"}}, "options": {"data": {}}, "status": {"data": {}}},
            }
            snapshot_b = {
                "survey_id": "SV_B",
                "survey_name": "Survey B",
                "sections": {"metadata": {"data": {"SurveyName": "Old B"}}, "options": {"data": {}}, "status": {"data": {}}},
            }
            (root / "surveys" / "qualtrics_master_snapshots" / "SV_A.json").write_text(
                json.dumps(snapshot_a), encoding="utf-8"
            )
            (root / "surveys" / "qualtrics_master_snapshots" / "SV_B.json").write_text(
                json.dumps(snapshot_b), encoding="utf-8"
            )

            headers = ["SurveyID", "SurveyName"]
            rows = [
                {"SurveyID": "SV_A", "SurveyName": "New A"},
                {"SurveyID": "SV_B", "SurveyName": "New B"},
            ]

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
                            },
                        }
                        stage_result = stage_master(
                            csv_headers=headers, csv_rows=rows, verbose=False
                        )
                        self.assertEqual(stage_result["staged_surveys"], 2)

                        with patch(
                            "qsync.survey_inventory.load_focal_snapshot",
                            return_value={"SV_A": True, "SV_B": True},
                        ):
                            with patch("qsync.push_safeguards.enforce_push_safeguards"):
                                with patch("qsync.qualtrics_client.ensure_backup"):
                                    with patch("qsync.survey_master.capture_pre_apply_snapshot"):
                                        with patch(
                                            "qsync.survey_master.get_client_config",
                                            return_value=("http://test", {}),
                                        ):
                                            with patch(
                                                "qsync.survey_master._write_metadata",
                                                return_value=True,
                                            ):
                                                with patch(
                                                    "qsync.qualtrics_client.refresh_survey_cache",
                                                    return_value=(MagicMock(), True),
                                                ):
                                                    with patch(
                                                        "qsync.survey_master.refresh_snapshot_from_live",
                                                        return_value=root
                                                        / "surveys"
                                                        / "qualtrics_master_snapshots"
                                                        / "SV_A.json",
                                                    ):
                                                        result = push_master(no_publish=True)

            self.assertEqual(result["surveys_pushed"], 2)
            self.assertEqual(result["surveys_failed"], 0)


if __name__ == "__main__":
    unittest.main()
