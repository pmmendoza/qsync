"""Tests for survey master parsing functions."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class SurveyMasterParsingTests(unittest.TestCase):
    """Test mapping CSV parsing and endpoint derivation."""

    def test_parse_mapping_csv_filters_read_write(self) -> None:
        """Only includes fields with survey_master=read or write."""
        from qsync.survey_master import _parse_mapping_csv

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "surveys").mkdir(parents=True, exist_ok=True)

            # Create a test mapping CSV
            csv_content = """id,domain,field_name,survey_master,order
1,survey_metadata,SurveyName,write,1
2,survey_metadata,SurveyStatus,read,2
3,survey_metadata,InternalField,none,
4,survey_options,Header,write,3
5,survey_detail,isActive,
"""
            csv_path = root / "surveys" / "qualtrics_api_key_mapping.csv"
            csv_path.write_text(csv_content, encoding="utf-8")

            with patch("qsync.survey_master._mapping_csv_path", return_value=csv_path):
                fields = _parse_mapping_csv()

            # Should have 3 fields (write + read), not 5
            self.assertEqual(len(fields), 3)
            self.assertIn("SurveyName", fields)
            self.assertIn("SurveyStatus", fields)
            self.assertIn("Header", fields)
            self.assertNotIn("InternalField", fields)
            self.assertNotIn("isActive", fields)

    def test_parse_mapping_csv_skips_empty_field_names(self) -> None:
        """Skips rows with empty field_name."""
        from qsync.survey_master import _parse_mapping_csv

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "surveys").mkdir(parents=True, exist_ok=True)

            csv_content = """id,domain,field_name,survey_master
1,survey_metadata,,write
2,survey_metadata,SurveyName,write
"""
            csv_path = root / "surveys" / "qualtrics_api_key_mapping.csv"
            csv_path.write_text(csv_content, encoding="utf-8")

            with patch("qsync.survey_master._mapping_csv_path", return_value=csv_path):
                fields = _parse_mapping_csv()

            # Should have 1 field, not 2
            self.assertEqual(len(fields), 1)
            self.assertIn("SurveyName", fields)

    def test_parse_mapping_csv_handles_duplicates(self) -> None:
        """Keeps first occurrence of duplicate field_name."""
        from qsync.survey_master import _parse_mapping_csv

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "surveys").mkdir(parents=True, exist_ok=True)

            csv_content = """id,domain,object_path,field_name,survey_master
1,survey_metadata,result.SurveyName,SurveyName,write
2,survey_options,result.SurveyName,SurveyName,write
"""
            csv_path = root / "surveys" / "qualtrics_api_key_mapping.csv"
            csv_path.write_text(csv_content, encoding="utf-8")

            with patch("qsync.survey_master._mapping_csv_path", return_value=csv_path):
                fields = _parse_mapping_csv()

            # Should have 1 field, first occurrence
            self.assertEqual(len(fields), 1)
            field_info = fields["SurveyName"]
            self.assertEqual(field_info["domain"], "survey_metadata")

    def test_parse_mapping_csv_missing_file_raises(self) -> None:
        """Raises FileNotFoundError when CSV missing."""
        from qsync.survey_master import _parse_mapping_csv

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            nonexistent_path = root / "surveys" / "nonexistent.csv"

            with patch(
                "qsync.survey_master._mapping_csv_path", return_value=nonexistent_path
            ):
                with patch.dict(
                    "os.environ", {"QSYNC_MAPPING_CSV": str(nonexistent_path)}
                ):
                    with self.assertRaises(FileNotFoundError):
                        _parse_mapping_csv()

    def test_compute_schema_version_consistent(self) -> None:
        """Same file yields same schema version hash."""
        from qsync.survey_master import _compute_schema_version

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "surveys").mkdir(parents=True, exist_ok=True)

            csv_content = "id,field_name\n1,SurveyName\n"
            csv_path = root / "surveys" / "qualtrics_api_key_mapping.csv"
            csv_path.write_text(csv_content, encoding="utf-8")

            with patch("qsync.survey_master._mapping_csv_path", return_value=csv_path):
                version1 = _compute_schema_version()
                version2 = _compute_schema_version()

            # Both should be equal (same file hash)
            self.assertEqual(version1, version2)
            # Should contain date and hash
            self.assertRegex(version1, r"^\d{8}-[0-9a-f]{8}$")

    def test_compute_schema_version_missing_csv_uses_packaged_json(self) -> None:
        """Falls through to packaged JSON when workspace CSV is missing."""
        from qsync.survey_master import _compute_schema_version

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            nonexistent_path = root / "surveys" / "nonexistent.csv"

            with patch(
                "qsync.survey_master._mapping_csv_path", return_value=nonexistent_path
            ):
                version = _compute_schema_version()

            # Should return a valid hash from packaged JSON, not "unknown"
            self.assertRegex(version, r"^\d{8}-[0-9a-f]{8}$")

    def test_parse_mapping_falls_through_to_packaged_json(self) -> None:
        """Uses packaged JSON when no workspace CSV exists."""
        from qsync.survey_master import _parse_mapping_csv

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            nonexistent_path = root / "surveys" / "nonexistent.csv"

            with patch(
                "qsync.survey_master._mapping_csv_path", return_value=nonexistent_path
            ):
                fields = _parse_mapping_csv()

            # Should load the full packaged mapping (84 entries)
            self.assertGreater(len(fields), 50)
            self.assertIn("SurveyID", fields)
            self.assertIn("SurveyName", fields)
            self.assertIn("isActive", fields)
            self.assertIn("_focal", fields)

    def test_derive_endpoint_metadata(self) -> None:
        """Domain containing 'metadata' → 'metadata' endpoint."""
        from qsync.survey_master import _derive_endpoint

        field_info = {"domain": "survey_metadata"}
        endpoint = _derive_endpoint(field_info)
        self.assertEqual(endpoint, "metadata")

    def test_derive_endpoint_options(self) -> None:
        """Domain containing 'options' → 'options' endpoint."""
        from qsync.survey_master import _derive_endpoint

        field_info = {"domain": "survey_options"}
        endpoint = _derive_endpoint(field_info)
        self.assertEqual(endpoint, "options")

    def test_derive_endpoint_status(self) -> None:
        """Domain containing 'detail' → 'status' endpoint."""
        from qsync.survey_master import _derive_endpoint

        field_info = {"domain": "survey_detail"}
        endpoint = _derive_endpoint(field_info)
        self.assertEqual(endpoint, "status")

    def test_derive_endpoint_readonly(self) -> None:
        """Domain='survey_def' → None (read-only)."""
        from qsync.survey_master import _derive_endpoint

        field_info = {"domain": "survey_def"}
        endpoint = _derive_endpoint(field_info)
        self.assertIsNone(endpoint)

    def test_derive_endpoint_unknown(self) -> None:
        """Unknown domain → None (read-only)."""
        from qsync.survey_master import _derive_endpoint

        field_info = {"domain": "unknown_domain"}
        endpoint = _derive_endpoint(field_info)
        self.assertIsNone(endpoint)


if __name__ == "__main__":
    unittest.main()
