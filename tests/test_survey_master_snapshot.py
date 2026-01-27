"""Tests for survey master snapshot functions."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class SurveyMasterSnapshotTests(unittest.TestCase):
    """Test snapshot creation, I/O, and version reduction."""

    def test_reduce_latest_published_selects_max_version(self) -> None:
        """Picks highest versionNumber where published=true."""
        from qsync.survey_master import _reduce_to_latest_published

        versions_list = [
            {
                "metadata": {
                    "versionID": "V_1",
                    "versionNumber": 1,
                    "published": True,
                }
            },
            {
                "metadata": {
                    "versionID": "V_3",
                    "versionNumber": 3,
                    "published": True,
                }
            },
            {
                "metadata": {
                    "versionID": "V_2",
                    "versionNumber": 2,
                    "published": True,
                }
            },
        ]

        result = _reduce_to_latest_published(versions_list)

        self.assertEqual(result["versionID"], "V_3")
        self.assertEqual(result["versionNumber"], 3)

    def test_reduce_latest_published_no_published_returns_empty(self) -> None:
        """Returns {} when no published versions."""
        from qsync.survey_master import _reduce_to_latest_published

        versions_list = [
            {"metadata": {"versionID": "V_1", "published": False}},
            {"metadata": {"versionID": "V_2", "published": False}},
        ]

        result = _reduce_to_latest_published(versions_list)

        self.assertEqual(result, {})

    def test_reduce_latest_published_ignores_unpublished(self) -> None:
        """Filters published=false versions."""
        from qsync.survey_master import _reduce_to_latest_published

        versions_list = [
            {
                "metadata": {
                    "versionID": "V_1",
                    "versionNumber": 1,
                    "published": True,
                }
            },
            {
                "metadata": {
                    "versionID": "V_2",
                    "versionNumber": 2,
                    "published": False,
                }
            },
        ]

        result = _reduce_to_latest_published(versions_list)

        self.assertEqual(result["versionID"], "V_1")

    def test_create_snapshot_structure(self) -> None:
        """Snapshot has all required keys."""
        from qsync.survey_master import create_snapshot

        status_data = {"id": "SV_123", "isActive": True}
        metadata_data = {"SurveyName": "Test"}
        options_data = {"Header": "<p>Hello</p>"}
        versions_data = {"versionNumber": 1}

        with patch(
            "qsync.survey_master._compute_schema_version",
            return_value="20251220-abc123",
        ):
            snapshot = create_snapshot(
                survey_id="SV_123",
                survey_name="Test Survey",
                status_data=status_data,
                metadata_data=metadata_data,
                options_data=options_data,
                versions_data=versions_data,
            )

        # Check top-level keys
        self.assertIn("survey_id", snapshot)
        self.assertIn("survey_name", snapshot)
        self.assertIn("schema_version", snapshot)
        self.assertIn("pulled_at", snapshot)
        self.assertIn("sections", snapshot)

        # Check sections
        self.assertIn("status", snapshot["sections"])
        self.assertIn("metadata", snapshot["sections"])
        self.assertIn("options", snapshot["sections"])
        self.assertIn("versions", snapshot["sections"])

        # Verify survey_id
        self.assertEqual(snapshot["survey_id"], "SV_123")
        self.assertEqual(snapshot["survey_name"], "Test Survey")

    def test_create_snapshot_sections_have_timestamps(self) -> None:
        """Each section has pulled_at timestamp."""
        from qsync.survey_master import create_snapshot

        with patch(
            "qsync.survey_master._compute_schema_version",
            return_value="20251220-abc123",
        ):
            snapshot = create_snapshot(
                survey_id="SV_123",
                survey_name="Test",
                status_data={},
                metadata_data={},
                options_data={},
                versions_data={},
            )

        for section_name in ["status", "metadata", "options", "versions"]:
            section = snapshot["sections"][section_name]
            self.assertIn("pulled_at", section)
            # Should be ISO format timestamp
            self.assertRegex(section["pulled_at"], r"^\d{4}-\d{2}-\d{2}T")

    def test_create_snapshot_sections_have_source_endpoint(self) -> None:
        """Each section has source_endpoint."""
        from qsync.survey_master import create_snapshot

        with patch(
            "qsync.survey_master._compute_schema_version",
            return_value="20251220-abc123",
        ):
            snapshot = create_snapshot(
                survey_id="SV_123",
                survey_name="Test",
                status_data={},
                metadata_data={},
                options_data={},
                versions_data={},
            )

        expected_endpoints = {
            "status": "GET /surveys/{surveyId}",
            "metadata": "GET /survey-definitions/{surveyId}/metadata",
            "options": "GET /survey-definitions/{surveyId}/options",
            "versions": "GET /survey-definitions/{surveyId}/versions",
        }

        for section_name, expected_endpoint in expected_endpoints.items():
            section = snapshot["sections"][section_name]
            self.assertIn("source_endpoint", section)
            self.assertIn(expected_endpoint.split()[1], section["source_endpoint"])

    def test_save_snapshot_creates_directory(self) -> None:
        """Creates snapshots directory if missing."""
        from qsync.survey_master import save_snapshot

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshots_dir = root / "surveys" / "qualtrics_master_snapshots"

            snapshot = {
                "survey_id": "SV_123",
                "survey_name": "Test",
                "sections": {},
            }

            with patch(
                "qsync.survey_master._snapshots_dir", return_value=snapshots_dir
            ):
                save_snapshot("SV_123", snapshot)

            # Directory should exist
            self.assertTrue(snapshots_dir.exists())
            # Snapshot file should exist
            self.assertTrue((snapshots_dir / "SV_123.json").exists())

    def test_save_snapshot_writes_valid_json(self) -> None:
        """JSON is parseable."""
        from qsync.survey_master import save_snapshot

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshots_dir = root / "surveys" / "qualtrics_master_snapshots"

            snapshot = {
                "survey_id": "SV_123",
                "survey_name": "Test",
                "sections": {"status": {"data": {}}},
            }

            with patch(
                "qsync.survey_master._snapshots_dir", return_value=snapshots_dir
            ):
                save_snapshot("SV_123", snapshot)

                # Load and verify JSON is valid
                with open(snapshots_dir / "SV_123.json") as f:
                    loaded = json.load(f)

                self.assertEqual(loaded["survey_id"], "SV_123")

    def test_load_snapshot_missing_file_returns_none(self) -> None:
        """Missing snapshot returns None."""
        from qsync.survey_master import load_snapshot

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshots_dir = root / "surveys" / "qualtrics_master_snapshots"
            snapshots_dir.mkdir(parents=True, exist_ok=True)

            with patch(
                "qsync.survey_master._snapshots_dir", return_value=snapshots_dir
            ):
                self.assertIsNone(load_snapshot("SV_NONEXISTENT"))

    def test_load_snapshot_corrupted_json_returns_none(self) -> None:
        """Corrupted JSON returns None."""
        from qsync.survey_master import load_snapshot

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshots_dir = root / "surveys" / "qualtrics_master_snapshots"
            snapshots_dir.mkdir(parents=True, exist_ok=True)

            # Write corrupted JSON
            snapshot_file = snapshots_dir / "SV_123.json"
            snapshot_file.write_text("{ invalid json", encoding="utf-8")

            with patch(
                "qsync.survey_master._snapshots_dir", return_value=snapshots_dir
            ):
                self.assertIsNone(load_snapshot("SV_123"))


if __name__ == "__main__":
    unittest.main()
