"""Shared test fixtures for survey master tests.

Provides base test classes and fixture setup for consistent test environments.
"""

import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class SurveyMasterTestBase(unittest.TestCase):
    """Base test class with fixture setup for survey master tests.

    Provides:
    - Temporary workspace directory
    - Temporary mapping CSV
    - Temporary snapshots directory
    - Mocked path functions
    """

    def setUp(self) -> None:
        """Set up test workspace with temporary directories and files."""
        # Create temporary workspace
        self.test_dir = tempfile.mkdtemp(prefix="qsync_test_")
        self.test_path = Path(self.test_dir)

        # Create directory structure
        self.surveys_dir = self.test_path / "surveys"
        self.surveys_dir.mkdir(parents=True, exist_ok=True)

        self.mapping_dir = self.surveys_dir

        self.snapshots_dir = self.surveys_dir / "qualtrics_master_snapshots"
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

        # Create default mapping CSV with sample fields
        self._create_default_mapping_csv()

        # Start mocking path functions
        self._setup_path_mocks()

    def tearDown(self) -> None:
        """Clean up temporary workspace."""
        if Path(self.test_dir).exists():
            shutil.rmtree(self.test_dir)
        self._cleanup_path_mocks()

    def _setup_path_mocks(self) -> None:
        """Set up mocks for workspace path functions."""
        # Patch the path functions to return test paths
        self.workspace_root_patcher = patch(
            "qsync.survey_master._workspace_root",
            return_value=self.test_path,
        )
        self.workspace_root_patcher.start()

        self.surveys_dir_patcher = patch(
            "qsync.survey_master._surveys_dir",
            return_value=self.surveys_dir,
        )
        self.surveys_dir_patcher.start()

        self.snapshots_dir_patcher = patch(
            "qsync.survey_master._snapshots_dir",
            return_value=self.snapshots_dir,
        )
        self.snapshots_dir_patcher.start()

        self.mapping_csv_patcher = patch(
            "qsync.survey_master._mapping_csv_path",
            return_value=self.mapping_dir / "qualtrics_api_key_mapping.csv",
        )
        self.mapping_csv_patcher.start()

        # Also patch validation module paths
        self.config_resolve_patcher = patch(
            "qsync.config.resolve_root",
            return_value=self.test_path,
        )
        self.config_resolve_patcher.start()

    def _cleanup_path_mocks(self) -> None:
        """Stop all path mocks."""
        self.workspace_root_patcher.stop()
        self.surveys_dir_patcher.stop()
        self.snapshots_dir_patcher.stop()
        self.mapping_csv_patcher.stop()
        self.config_resolve_patcher.stop()

    def _create_default_mapping_csv(self) -> None:
        """Create a default mapping CSV with sample fields."""
        mapping_path = self.mapping_dir / "qualtrics_api_key_mapping.csv"

        # Create sample mapping data
        rows = [
            {
                "id": "1",
                "field_name": "SurveyName",
                "domain": "survey_metadata",
                "survey_master": "write",
                "data_type": "string",
                "allowed_values": "",
                "format_notes": "",
                "description": "Survey name",
                "order": "1",
                "object_path": "result.SurveyName",
            },
            {
                "id": "2",
                "field_name": "SurveyDescription",
                "domain": "survey_metadata",
                "survey_master": "write",
                "data_type": "string",
                "allowed_values": "",
                "format_notes": "",
                "description": "Survey description",
                "order": "2",
                "object_path": "result.SurveyDescription",
            },
            {
                "id": "3",
                "field_name": "Header",
                "domain": "survey_options",
                "survey_master": "write",
                "data_type": "string",
                "allowed_values": "",
                "format_notes": "",
                "description": "Survey header HTML",
                "order": "3",
                "object_path": "result.Header",
            },
            {
                "id": "4",
                "field_name": "isActive",
                "domain": "survey_detail",
                "survey_master": "write",
                "data_type": "bool",
                "allowed_values": "",
                "format_notes": "",
                "description": "Active state",
                "order": "4",
                "object_path": "result.isActive",
            },
            {
                "id": "5",
                "field_name": "ArchiveChoice",
                "domain": "survey_metadata",
                "survey_master": "write",
                "data_type": "bool",
                "allowed_values": "",
                "format_notes": "",
                "description": "Archive choice",
                "order": "5",
                "object_path": "result.ArchiveChoice",
            },
            {
                "id": "6",
                "field_name": "SurveyStartDate",
                "domain": "survey_metadata",
                "survey_master": "write",
                "data_type": "datetime",
                "allowed_values": "",
                "format_notes": "ISO 8601",
                "description": "Survey start date",
                "order": "6",
                "object_path": "result.SurveyStartDate",
            },
        ]

        fieldnames = [
            "id",
            "field_name",
            "domain",
            "survey_master",
            "data_type",
            "allowed_values",
            "format_notes",
            "description",
            "order",
            "object_path",
        ]

        with open(mapping_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def create_mapping_csv(self, rows: list, fieldnames: list = None) -> Path:
        """Create a custom mapping CSV for testing.

        Args:
            rows: List of row dicts
            fieldnames: Column names (defaults to standard mapping CSV columns)

        Returns:
            Path to created CSV file
        """
        if fieldnames is None:
            fieldnames = [
                "id",
                "field_name",
                "domain",
                "survey_master",
                "data_type",
                "allowed_values",
                "format_notes",
                "description",
                "order",
                "object_path",
            ]

        mapping_path = self.mapping_dir / "qualtrics_api_key_mapping.csv"

        with open(mapping_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, restval="")
            writer.writeheader()
            writer.writerows(rows)

        return mapping_path

    def create_master_csv(self, rows: list, survey_ids: list = None) -> Path:
        """Create a master CSV for testing.

        Args:
            rows: List of row dicts
            survey_ids: List of survey IDs (auto-generates if not provided)

        Returns:
            Path to created CSV file
        """
        if not rows:
            return self.surveys_dir / "qualtrics_master.csv"

        # Get all unique field names from rows
        fieldnames = sorted(set(field for row in rows for field in row.keys()))

        # Ensure SurveyID is first
        if "SurveyID" in fieldnames:
            fieldnames.remove("SurveyID")
            fieldnames = ["SurveyID"] + fieldnames

        master_path = self.surveys_dir / "qualtrics_master.csv"

        with open(master_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, restval="")
            writer.writeheader()
            writer.writerows(rows)

        return master_path

    def create_snapshot(self, survey_id: str, snapshot_data: dict = None) -> Path:
        """Create a master snapshot for testing.

        Args:
            survey_id: Survey ID
            snapshot_data: Snapshot dict (auto-generates if not provided)

        Returns:
            Path to created snapshot file
        """
        if snapshot_data is None:
            snapshot_data = {
                "survey_id": survey_id,
                "schema_version": "test_v1",
                "sections": {
                    "metadata": {
                        "pulled_at": "2025-12-20T12:00:00Z",
                        "source_endpoint": "metadata",
                        "data": {
                            "SurveyName": f"Test Survey {survey_id}",
                            "SurveyDescription": "Test description",
                        },
                    },
                    "options": {
                        "pulled_at": "2025-12-20T12:00:00Z",
                        "source_endpoint": "options",
                        "data": {"Header": "Test Header", "Footer": "Test Footer"},
                    },
                    "detail": {
                        "pulled_at": "2025-12-20T12:00:00Z",
                        "source_endpoint": "detail",
                        "data": {"isActive": True},
                    },
                },
            }

        snapshot_path = self.snapshots_dir / f"{survey_id}.json"

        with open(snapshot_path, "w", encoding="utf-8") as f:
            json.dump(snapshot_data, f, indent=2)

        return snapshot_path
