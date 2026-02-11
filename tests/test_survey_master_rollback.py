"""Tests for survey master rollback snapshot capture and restore workflow."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from .test_fixtures import SurveyMasterTestBase


class SurveyMasterRollbackTests(SurveyMasterTestBase):
    """Validate rollback snapshot and restore behavior."""

    def _write_rollback_snapshot(
        self,
        survey_id: str,
        filename: str,
        *,
        pre_apply_value: str,
        target_value: str,
    ) -> Path:
        survey_dir = self.surveys_dir / "qualtrics_master_rollback" / survey_id
        survey_dir.mkdir(parents=True, exist_ok=True)
        path = survey_dir / filename
        payload = {
            "survey_id": survey_id,
            "survey_name": "Test Survey",
            "sections": {"metadata": {"data": {"SurveyName": pre_apply_value}}},
            "rollback": {
                "captured_at": "2026-02-05T10:00:00Z",
                "applied_changes": [
                    {
                        "field": "SurveyName",
                        "endpoint": "metadata",
                        "is_dangerous": False,
                        "pre_apply_value": pre_apply_value,
                        "target_value": target_value,
                    }
                ],
            },
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_capture_pre_apply_snapshot_writes_snapshot_with_change_metadata(
        self,
    ) -> None:
        """Snapshot capture stores field-level before/after metadata."""
        from qsync.survey_master import capture_pre_apply_snapshot

        changes = [
            {
                "field": "SurveyName",
                "endpoint": "metadata",
                "new_value": "New Survey Name",
                "is_dangerous": False,
            }
        ]

        def _mock_fetch_endpoint(
            _base_url: str, _headers: dict, _survey_id: str, endpoint: str
        ) -> tuple[dict, str]:
            if endpoint == "metadata":
                return {"SurveyName": "Old Survey Name"}, "ts"
            raise AssertionError(endpoint)

        with patch(
            "qsync.survey_master._fetch_endpoint", side_effect=_mock_fetch_endpoint
        ):
            with patch(
                "qsync.survey_master.get_client_config",
                return_value=("example.qualtrics.com", {"X-API-TOKEN": "test"}),
            ):
                snapshot_path = capture_pre_apply_snapshot("SV_001", changes)

        self.assertTrue(snapshot_path.exists())
        data = json.loads(snapshot_path.read_text(encoding="utf-8"))
        rollback = data.get("rollback", {})
        self.assertEqual(rollback.get("snapshot_type"), "pre_apply")
        self.assertEqual(len(rollback.get("applied_changes", [])), 1)
        self.assertEqual(
            rollback["applied_changes"][0]["pre_apply_value"], "Old Survey Name"
        )
        self.assertEqual(
            rollback["applied_changes"][0]["target_value"], "New Survey Name"
        )

    def test_list_rollback_versions_orders_newest_first(self) -> None:
        """Rollback version 1 should be the newest snapshot."""
        from qsync.survey_master import list_rollback_versions

        self._write_rollback_snapshot(
            "SV_001",
            "20260205T090000Z-pre-apply.json",
            pre_apply_value="Old 1",
            target_value="New 1",
        )
        self._write_rollback_snapshot(
            "SV_001",
            "20260205T100000Z-pre-apply.json",
            pre_apply_value="Old 2",
            target_value="New 2",
        )

        versions = list_rollback_versions("SV_001")
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0]["version"], 1)
        self.assertTrue(
            str(versions[0]["path"]).endswith("20260205T100000Z-pre-apply.json")
        )
        self.assertEqual(versions[1]["version"], 2)

    def test_rollback_master_dry_run_returns_changes(self) -> None:
        """Dry-run rollback computes restore diffs without writing."""
        from qsync.survey_master import rollback_master

        self._write_rollback_snapshot(
            "SV_001",
            "20260205T100000Z-pre-apply.json",
            pre_apply_value="Old Name",
            target_value="New Name",
        )

        def _mock_fetch_endpoint(
            _base_url: str, _headers: dict, _survey_id: str, endpoint: str
        ) -> tuple[dict, str]:
            if endpoint == "metadata":
                return {"SurveyName": "New Name"}, "ts"
            if endpoint == "options":
                return {}, "ts"
            if endpoint == "status":
                return {"isActive": True}, "ts"
            if endpoint == "versions":
                return {}, "ts"
            raise AssertionError(endpoint)

        with patch("qsync.survey_master._fetch_survey_name", return_value="Survey"):
            with patch(
                "qsync.survey_master._fetch_endpoint", side_effect=_mock_fetch_endpoint
            ):
                with patch(
                    "qsync.survey_master.get_client_config",
                    return_value=("example.qualtrics.com", {"X-API-TOKEN": "test"}),
                ):
                    result = rollback_master(
                        "SV_001",
                        dry_run=True,
                        force=False,
                        allow_dangerous=True,
                        publish=False,
                    )

        self.assertIsNone(result.get("error"))
        self.assertTrue(result.get("applied"))
        self.assertEqual(len(result.get("changes", [])), 1)
        self.assertEqual(result["changes"][0]["old_value"], "New Name")
        self.assertEqual(result["changes"][0]["new_value"], "Old Name")

    def test_rollback_master_blocks_drift_without_force(self) -> None:
        """Rollback refuses when current values drift from expected post-apply values."""
        from qsync.survey_master import rollback_master

        self._write_rollback_snapshot(
            "SV_001",
            "20260205T100000Z-pre-apply.json",
            pre_apply_value="Old Name",
            target_value="Expected Post Apply",
        )

        def _mock_fetch_endpoint(
            _base_url: str, _headers: dict, _survey_id: str, endpoint: str
        ) -> tuple[dict, str]:
            if endpoint == "metadata":
                return {"SurveyName": "Manual Edit"}, "ts"
            if endpoint == "options":
                return {}, "ts"
            if endpoint == "status":
                return {"isActive": True}, "ts"
            if endpoint == "versions":
                return {}, "ts"
            raise AssertionError(endpoint)

        with patch("qsync.survey_master._fetch_survey_name", return_value="Survey"):
            with patch(
                "qsync.survey_master._fetch_endpoint", side_effect=_mock_fetch_endpoint
            ):
                with patch(
                    "qsync.survey_master.get_client_config",
                    return_value=("example.qualtrics.com", {"X-API-TOKEN": "test"}),
                ):
                    blocked = rollback_master(
                        "SV_001",
                        dry_run=True,
                        force=False,
                        allow_dangerous=True,
                        publish=False,
                    )
                    forced = rollback_master(
                        "SV_001",
                        dry_run=True,
                        force=True,
                        allow_dangerous=True,
                        publish=False,
                    )

        self.assertIn("Drift detected", blocked.get("error", ""))
        self.assertTrue(blocked.get("drifted_fields"))
        self.assertIsNone(forced.get("error"))
        self.assertTrue(forced.get("changes"))

    def test_apply_master_aborts_when_snapshot_capture_fails(self) -> None:
        """Master apply should not write endpoints if rollback snapshot capture fails."""
        from qsync.survey_master import apply_master

        csv_headers = ["SurveyID", "SurveyName"]
        csv_rows = [{"SurveyID": "SV_001", "SurveyName": "New Name"}]
        diff = {
            "survey_id": "SV_001",
            "survey_name": "Test Survey",
            "changes": [
                {
                    "field": "SurveyName",
                    "old_value": "Old Name",
                    "new_value": "New Name",
                    "endpoint": "metadata",
                    "is_dangerous": False,
                }
            ],
            "publish_required": True,
            "has_dangerous_changes": False,
            "error": None,
        }

        with patch(
            "qsync.survey_master.load_master_csv", return_value=(csv_headers, csv_rows)
        ):
            with patch("qsync.survey_master.validate_master_csv", return_value=[]):
                with patch("qsync.survey_master.compute_diff", return_value=diff):
                    with patch(
                        "qsync.survey_master.detect_drift",
                        return_value={
                            "drifted_fields": [],
                            "schema_version_matches": True,
                            "schema_mismatch_warning": None,
                        },
                    ):
                        with patch(
                            "qsync.survey_master.capture_pre_apply_snapshot",
                            side_effect=RuntimeError("snapshot failed"),
                        ):
                            with patch(
                                "qsync.survey_master._write_metadata"
                            ) as mock_write:
                                result = apply_master(
                                    allow_dangerous=False,
                                    force=False,
                                    dry_run=False,
                                    verbose=False,
                                )

        self.assertEqual(result["surveys_applied"], 0)
        self.assertEqual(result["surveys_failed"], 1)
        self.assertFalse(mock_write.called)
