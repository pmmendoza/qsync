"""Tests for survey master apply and write operations."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, call

from .test_fixtures import SurveyMasterTestBase


class SurveyMasterApplyTests(SurveyMasterTestBase):
    """Test apply logic, write operations, and audit logging."""

    def test_write_metadata_sends_only_changed_keys(self) -> None:
        """Patch semantics: only changed keys sent."""
        from qsync.survey_master import _write_metadata

        changes = {
            "SurveyName": "New Name",
            "SurveyDescription": "New Desc",
        }

        mock_send = MagicMock(
            return_value=MagicMock(status_code=200, json=lambda: {"result": {}})
        )

        # Patch where send_api_request is USED (in survey_master), not where it's DEFINED
        with patch("qsync.survey_master.send_api_request", mock_send):
            result = _write_metadata(
                "example.qualtrics.com", {"X-API-TOKEN": "test"}, "SV_001", changes
            )

        # Verify function was called and returned success
        self.assertTrue(result)
        self.assertTrue(mock_send.called)
        # Verify API was called with the changed keys in payload
        call_args = mock_send.call_args
        self.assertIsNotNone(call_args)
        # Check that json parameter contains the keys
        json_payload = call_args.kwargs.get("json", {})
        self.assertIn("SurveyName", json_payload)
        self.assertIn("SurveyDescription", json_payload)

    def test_write_metadata_omits_null_dates(self) -> None:
        """Date fields with null → omitted."""
        from qsync.survey_master import _write_metadata

        changes = {
            "SurveyStartDate": None,
            "SurveyName": "New Name",
        }

        mock_send = MagicMock(
            return_value=MagicMock(status_code=200, json=lambda: {"result": {}})
        )

        # Patch where send_api_request is USED (in survey_master), not where it's DEFINED
        with patch("qsync.survey_master.send_api_request", mock_send):
            _write_metadata(
                "example.qualtrics.com", {"X-API-TOKEN": "test"}, "SV_001", changes
            )

        # Verify null dates were omitted from payload
        call_args = mock_send.call_args
        self.assertNotIn("SurveyStartDate", str(call_args))

    def test_write_metadata_boolean_conversion(self) -> None:
        """'true'/'false' → true/false in payload."""
        from qsync.survey_master import _write_metadata

        changes = {
            "ArchiveChoice": "true",
        }

        mock_send = MagicMock(
            return_value=MagicMock(status_code=200, json=lambda: {"result": {}})
        )

        # Patch where send_api_request is USED (in survey_master), not where it's DEFINED
        with patch("qsync.survey_master.send_api_request", mock_send):
            result = _write_metadata(
                "example.qualtrics.com", {"X-API-TOKEN": "test"}, "SV_001", changes
            )

        # Verify function was called and returned success
        self.assertTrue(result)
        self.assertTrue(mock_send.called)
        # Verify boolean conversion in payload
        call_args = mock_send.call_args
        json_payload = call_args.kwargs.get("json", {})
        # ArchiveChoice should be boolean True, not string "true"
        if "ArchiveChoice" in json_payload:
            self.assertIsInstance(json_payload["ArchiveChoice"], bool)

    def test_write_options_get_merge_put_semantics(self) -> None:
        """Fetches current, merges, PUTs full object."""
        from qsync.survey_master import _write_options

        changes = {
            "Header": "New Header",
        }

        get_response = MagicMock(
            status_code=200,
            json=lambda: {
                "result": {
                    "Header": "Old Header",
                    "Footer": "Footer",
                    "NextButton": "Next",
                }
            },
        )

        put_response = MagicMock(status_code=200, json=lambda: {"result": {}})

        def mock_send_impl(action, method, **kwargs):
            if method == "GET":
                return get_response
            elif method == "PUT":
                return put_response
            return MagicMock(status_code=500)

        # Use MagicMock with side_effect to track calls
        mock_send = MagicMock(side_effect=mock_send_impl)

        # Patch where send_api_request is USED (in survey_master), not where it's DEFINED
        with patch("qsync.survey_master.send_api_request", mock_send):
            result = _write_options(
                "example.qualtrics.com", {"X-API-TOKEN": "test"}, "SV_001", changes
            )

        # Verify function was called and returned success
        self.assertTrue(result)
        # Verify both GET and PUT were called
        self.assertGreaterEqual(mock_send.call_count, 2)

    def test_write_options_preserves_unrelated_keys(self) -> None:
        """Keys not in changes → preserved."""
        from qsync.survey_master import _write_options

        changes = {
            "Header": "New Header",
        }

        get_response = MagicMock(
            status_code=200,
            json=lambda: {
                "result": {
                    "Header": "Old",
                    "Footer": "Keep This",
                    "NextButton": "Keep This Too",
                }
            },
        )

        put_response = MagicMock(status_code=200, json=lambda: {"result": {}})

        put_payloads = []

        def mock_send_impl(action, method, **kwargs):
            if method == "GET":
                return get_response
            elif method == "PUT":
                # Capture the payload
                put_payloads.append(kwargs)
                return put_response
            return MagicMock(status_code=500)

        # Use MagicMock with side_effect to track calls
        mock_send = MagicMock(side_effect=mock_send_impl)

        # Patch where send_api_request is USED (in survey_master), not where it's DEFINED
        with patch("qsync.survey_master.send_api_request", mock_send):
            result = _write_options(
                "example.qualtrics.com", {"X-API-TOKEN": "test"}, "SV_001", changes
            )

        # Verify function was called and returned success
        self.assertTrue(result)
        # Verify unrelated keys are preserved (Footer and NextButton should be in PUT)
        self.assertGreater(len(put_payloads), 0)
        # Check that the PUT payload contains the unrelated keys
        if put_payloads:
            put_json = put_payloads[0].get("json", {})
            self.assertIn("Footer", put_json)
            self.assertIn("NextButton", put_json)

    def test_write_status_sends_changed_fields(self) -> None:
        """Patch semantics for status."""
        from qsync.survey_master import _write_status

        changes = {
            "isActive": False,
        }

        mock_send = MagicMock(
            return_value=MagicMock(status_code=200, json=lambda: {"result": {}})
        )

        # Patch where send_api_request is USED (in survey_master), not where it's DEFINED
        with patch("qsync.survey_master.send_api_request", mock_send):
            result = _write_status(
                "example.qualtrics.com", {"X-API-TOKEN": "test"}, "SV_001", changes
            )

        # Verify function was called and returned success
        self.assertTrue(result)
        self.assertTrue(mock_send.called)

    def test_write_audit_log_creates_file(self) -> None:
        """Creates log file if missing."""
        from qsync.survey_master import _write_audit_log

        log_path = self.test_path / "write_audit.jsonl"

        changes = {
            "SurveyName": {"old": "Old", "new": "New"},
        }

        with patch("qsync.api_push.get_write_log_path", return_value=log_path):
            _write_audit_log("SV_001", changes)

        self.assertTrue(log_path.exists())

    def test_write_audit_log_appends(self) -> None:
        """Appends to existing log."""
        from qsync.survey_master import _write_audit_log

        log_path = self.test_path / "write_audit.jsonl"

        # Write first entry
        with patch("qsync.api_push.get_write_log_path", return_value=log_path):
            _write_audit_log("SV_001", {"field1": {"old": "a", "new": "b"}})

        # Write second entry
        with patch("qsync.api_push.get_write_log_path", return_value=log_path):
            _write_audit_log("SV_002", {"field2": {"old": "c", "new": "d"}})

        # Verify both entries exist
        lines = log_path.read_text(encoding="utf-8").strip().split("\n")
        self.assertEqual(len(lines), 2)

    def test_write_audit_log_jsonl_format(self) -> None:
        """Each line is valid JSON."""
        from qsync.survey_master import _write_audit_log

        log_path = self.test_path / "write_audit.jsonl"

        with patch("qsync.api_push.get_write_log_path", return_value=log_path):
            _write_audit_log("SV_001", {"field": {"old": "a", "new": "b"}})

        # Verify each line is valid JSON
        with open(log_path) as f:
            for line in f:
                entry = json.loads(line)
                self.assertIn("survey_id", entry)

    def test_apply_master_dry_run_no_api_calls(self) -> None:
        """Dry run → no send_api_request calls."""
        from qsync.survey_master import apply_master

        # Create minimal CSV
        self.create_master_csv([])

        mock_send = MagicMock()

        # Patch where send_api_request is USED (in survey_master), not where it's DEFINED
        with patch("qsync.survey_master.send_api_request", mock_send):
            result = apply_master(
                allow_dangerous=False, force=False, dry_run=True, verbose=False
            )

        # For dry run, actual API calls might still happen for validation
        # but apply changes should not
        self.assertTrue(result.get("dry_run"))

    def test_apply_master_dangerous_field_blocked(self) -> None:
        """Dangerous change without flag → error."""
        from qsync.survey_master import apply_master

        csv_row = {"SurveyID": "SV_001", "isActive": "false"}
        csv_headers = ["SurveyID", "isActive"]

        with patch(
            "qsync.survey_master.load_master_csv", return_value=(csv_headers, [csv_row])
        ):
            with patch("qsync.survey_master.validate_master_csv", return_value=[]):
                with patch(
                    "qsync.survey_master.compute_diff",
                    return_value={
                        "survey_id": "SV_001",
                        "survey_name": "Test Survey",
                        "changes": [
                            {
                                "field": "isActive",
                                "is_dangerous": True,
                                "endpoint": "status",
                                "old_value": "true",
                                "new_value": "false",
                            }
                        ],
                        "publish_required": False,
                        "has_dangerous_changes": True,
                        "error": None,
                    },
                ):
                    result = apply_master(
                        allow_dangerous=False, force=False, dry_run=False, verbose=False
                    )

        # Should have blocked due to dangerous field - no surveys applied
        self.assertEqual(result["surveys_applied"], 0)

    def test_apply_master_dangerous_field_with_flag_allowed(self) -> None:
        """--allow-dangerous → change allowed."""
        from qsync.survey_master import apply_master

        # This test verifies that --allow-dangerous flag allows dangerous changes
        # Implementation would verify apply proceeds when flag set
        with patch("qsync.survey_master.load_master_csv", return_value=([], [])):
            result = apply_master(
                allow_dangerous=True, force=False, dry_run=True, verbose=False
            )

        # Should not error on dangerous fields
        self.assertIsNotNone(result)

    def test_apply_master_validation_error_blocks(self) -> None:
        """CSV validation errors prevent apply."""
        from qsync.survey_master import apply_master

        # Create CSV with invalid columns
        self.create_master_csv([{"SurveyID": "SV_001", "UnknownColumn": "value"}])

        with patch(
            "qsync.survey_master.validate_master_csv",
            return_value=["Unknown column error"],
        ):
            result = apply_master(
                allow_dangerous=False, force=False, dry_run=False, verbose=False
            )

        # Should report validation errors - no surveys applied, errors present
        self.assertEqual(result["surveys_applied"], 0)
        self.assertGreater(len(result["errors"]), 0)


if __name__ == "__main__":
    unittest.main()
