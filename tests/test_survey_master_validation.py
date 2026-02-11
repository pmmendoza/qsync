"""Tests for survey master validation and diff computation."""

import unittest
from unittest.mock import patch


class SurveyMasterValidationTests(unittest.TestCase):
    """Test CSV validation, diff computation, and dangerous field detection."""

    def test_validate_csv_accepts_valid_columns(self) -> None:
        """All columns in mapping → valid."""
        from qsync.survey_master import validate_master_csv

        headers = ["SurveyID", "SurveyName", "Header"]
        rows = [
            {"SurveyID": "SV_001", "SurveyName": "Test", "Header": "<p>Hi</p>"},
        ]

        mapping = {
            "SurveyID": {"field_name": "SurveyID"},
            "SurveyName": {"field_name": "SurveyName"},
            "Header": {"field_name": "Header"},
        }

        with patch("qsync.survey_master._parse_mapping_csv", return_value=mapping):
            errors = validate_master_csv(headers, rows)

        self.assertEqual(len(errors), 0)

    def test_validate_csv_rejects_unknown_columns(self) -> None:
        """Column not in mapping → error."""
        from qsync.survey_master import validate_master_csv

        headers = ["SurveyID", "UnknownColumn"]
        rows = [
            {"SurveyID": "SV_001", "UnknownColumn": "value"},
        ]

        mapping = {
            "SurveyID": {"field_name": "SurveyID"},
        }

        with patch("qsync.survey_master._parse_mapping_csv", return_value=mapping):
            errors = validate_master_csv(headers, rows)

        self.assertGreater(len(errors), 0)
        self.assertIn("UnknownColumn", str(errors))

    def test_validate_csv_allows_readonly_columns(self) -> None:
        """Read-only columns allowed in CSV."""
        from qsync.survey_master import validate_master_csv

        headers = ["SurveyID", "_versionNumber"]
        rows = [
            {"SurveyID": "SV_001", "_versionNumber": "42"},
        ]

        mapping = {
            "SurveyID": {"field_name": "SurveyID"},
            "_versionNumber": {"field_name": "_versionNumber"},
        }

        with patch("qsync.survey_master._parse_mapping_csv", return_value=mapping):
            errors = validate_master_csv(headers, rows)

        self.assertEqual(len(errors), 0)

    def test_validate_csv_accepts_uppercase_boolean_enums(self) -> None:
        """Spreadsheet-style TRUE/FALSE should be accepted for true/false enum fields."""
        from qsync.survey_master import validate_master_csv

        headers = ["SurveyID", "BackButton", "SaveAndContinue"]
        rows = [
            {"SurveyID": "SV_001", "BackButton": "FALSE", "SaveAndContinue": "TRUE"},
        ]

        mapping = {
            "SurveyID": {"field_name": "SurveyID"},
            "BackButton": {
                "field_name": "BackButton",
                "survey_master": "write",
                "data_type": "string",
                "allowed_values": "true; false",
            },
            "SaveAndContinue": {
                "field_name": "SaveAndContinue",
                "survey_master": "write",
                "data_type": "string",
                "allowed_values": "true; false",
            },
        }

        with patch("qsync.survey_master._parse_mapping_csv", return_value=mapping):
            errors = validate_master_csv(headers, rows)

        self.assertEqual(errors, [])
        # validate_master_csv normalizes these in-place to prevent spurious diffs.
        self.assertEqual(rows[0]["BackButton"], "false")
        self.assertEqual(rows[0]["SaveAndContinue"], "true")

    def test_compute_diff_no_changes(self) -> None:
        """Identical CSV/snapshot → no changes."""
        from qsync.survey_master import compute_diff

        csv_row = {
            "SurveyID": "SV_001",
            "SurveyName": "My Survey",
            "isActive": "true",
        }

        snapshot = {
            "sections": {
                "metadata": {"data": {"SurveyName": "My Survey"}},
                "status": {"data": {"isActive": True}},
            }
        }

        mapping = {
            "SurveyID": {
                "field_name": "SurveyID",
                "domain": "survey_def",
                "order": "1",
            },
            "SurveyName": {
                "field_name": "SurveyName",
                "domain": "survey_metadata",
                "order": "2",
            },
            "isActive": {
                "field_name": "isActive",
                "domain": "survey_detail",
                "order": "",
            },
        }

        with patch("qsync.survey_master._parse_mapping_csv", return_value=mapping):
            with patch("qsync.survey_master.load_snapshot", return_value=snapshot):
                with patch(
                    "qsync.survey_master._derive_endpoint",
                    side_effect=lambda fi: {
                        "survey_metadata": "metadata",
                        "survey_detail": "status",
                        "survey_def": None,
                    }.get(fi.get("domain")),
                ):
                    with patch(
                        "qsync.survey_master._extract_value_from_snapshot",
                        return_value="",
                    ):
                        diff = compute_diff("SV_001", csv_row)

        # Should have empty changes
        self.assertEqual(len(diff.get("changes", [])), 0)

    def test_compute_diff_ignores_boolean_casing_after_validation(self) -> None:
        """If user CSV has TRUE/FALSE, validation normalizes and diff should not show a change."""
        from qsync.survey_master import validate_master_csv, compute_diff

        headers = ["SurveyID", "BackButton"]
        rows = [{"SurveyID": "SV_001", "BackButton": "FALSE"}]

        snapshot = {
            "sections": {
                "options": {"data": {"BackButton": "false"}},
            }
        }

        mapping = {
            "SurveyID": {"field_name": "SurveyID"},
            "BackButton": {
                "field_name": "BackButton",
                "domain": "survey_options",
                "survey_master": "write",
                "data_type": "string",
                "allowed_values": "true; false",
            },
        }

        with patch("qsync.survey_master._parse_mapping_csv", return_value=mapping):
            errors = validate_master_csv(headers, rows)
            self.assertEqual(errors, [])

            with patch("qsync.survey_master.load_snapshot", return_value=snapshot):
                with patch(
                    "qsync.survey_master._derive_endpoint", return_value="options"
                ):
                    with patch(
                        "qsync.survey_master._extract_value_from_snapshot",
                        return_value="false",
                    ):
                        diff = compute_diff("SV_001", rows[0])

        self.assertEqual(diff.get("changes", []), [])

    def test_compute_diff_metadata_change(self) -> None:
        """Changed metadata field detected."""
        from qsync.survey_master import compute_diff

        csv_row = {
            "SurveyName": "New Name",
        }

        snapshot = {
            "sections": {
                "metadata": {"data": {"SurveyName": "Old Name"}},
            }
        }

        mapping = {
            "SurveyName": {
                "field_name": "SurveyName",
                "domain": "survey_metadata",
                "order": "1",
                "survey_master": "write",
                "object_path": "SurveyName",
            },
        }

        with patch("qsync.survey_master._parse_mapping_csv", return_value=mapping):
            with patch("qsync.survey_master.load_snapshot", return_value=snapshot):
                with patch(
                    "qsync.survey_master._derive_endpoint", return_value="metadata"
                ):
                    diff = compute_diff("SV_001", csv_row)

        changes = diff.get("changes", [])
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["field"], "SurveyName")
        self.assertEqual(changes[0]["old_value"], "Old Name")
        self.assertEqual(changes[0]["new_value"], "New Name")

    def test_compute_diff_dangerous_field_flagged(self) -> None:
        """isActive change → dangerous=true."""
        from qsync.survey_master import compute_diff

        csv_row = {
            "isActive": "false",
        }

        snapshot = {
            "sections": {
                "status": {"data": {"isActive": True}},
            }
        }

        mapping = {
            "isActive": {
                "field_name": "isActive",
                "domain": "survey_detail",
                "survey_master": "write",
            },
        }

        with patch("qsync.survey_master._parse_mapping_csv", return_value=mapping):
            with patch("qsync.survey_master.load_snapshot", return_value=snapshot):
                with patch(
                    "qsync.survey_master._derive_endpoint", return_value="status"
                ):
                    diff = compute_diff("SV_001", csv_row)

        changes = diff.get("changes", [])
        self.assertTrue(any(c.get("is_dangerous") for c in changes))

    def test_compute_diff_publish_required_for_metadata(self) -> None:
        """Metadata change → publish_required=true."""
        from qsync.survey_master import compute_diff

        csv_row = {"SurveyName": "New Name"}

        snapshot = {
            "sections": {
                "metadata": {"data": {"SurveyName": "Old Name"}},
            }
        }

        mapping = {
            "SurveyName": {
                "field_name": "SurveyName",
                "domain": "survey_metadata",
                "survey_master": "write",
            },
        }

        with patch("qsync.survey_master._parse_mapping_csv", return_value=mapping):
            with patch("qsync.survey_master.load_snapshot", return_value=snapshot):
                with patch(
                    "qsync.survey_master._derive_endpoint", return_value="metadata"
                ):
                    diff = compute_diff("SV_001", csv_row)

        self.assertTrue(diff.get("publish_required"))

    def test_compute_diff_publish_not_required_for_status(self) -> None:
        """Status change → publish_required=false."""
        from qsync.survey_master import compute_diff

        csv_row = {"isActive": "false"}

        snapshot = {
            "sections": {
                "status": {"data": {"isActive": True}},
            }
        }

        mapping = {
            "isActive": {
                "field_name": "isActive",
                "domain": "survey_detail",
                "survey_master": "write",
            },
        }

        with patch("qsync.survey_master._parse_mapping_csv", return_value=mapping):
            with patch("qsync.survey_master.load_snapshot", return_value=snapshot):
                with patch(
                    "qsync.survey_master._derive_endpoint", return_value="status"
                ):
                    diff = compute_diff("SV_001", csv_row)

        self.assertFalse(diff.get("publish_required"))

    def test_dangerous_fields_complete_list(self) -> None:
        """Dangerous fields set matches requirements."""
        from qsync.survey_master import _get_dangerous_fields

        dangerous = _get_dangerous_fields()

        # Verify required dangerous fields
        required_fields = {
            "isActive",
            "SurveyStatus",
            "EOSRedirectURL",
            "BallotBoxStuffingPreventionURL",
            "RefererURL",
            "PasswordProtection",
        }

        for field in required_fields:
            self.assertIn(field, dangerous, f"Missing dangerous field: {field}")


if __name__ == "__main__":
    unittest.main()
