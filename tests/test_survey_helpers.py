"""
Unit tests for survey helper functions (QSYNC-XACCT-005).

Tests for:
- prepare_qsf_for_import()
- upload_qsf_to_account()
- activate_survey()
"""

from unittest.mock import Mock, patch

import pytest

from qsync.cli_survey import (
    prepare_qsf_for_import,
    upload_qsf_to_account,
    activate_survey,
)


class TestPrepareQsfForImport:
    """Tests for prepare_qsf_for_import() helper."""

    def test_basic_preparation(self):
        """Test basic QSF preparation with default values."""
        qsf = {
            "SurveyEntry": {
                "SurveyID": "SV_old123",
                "SurveyName": "Old Name",
                "SurveyStatus": "Active",
                "SurveyLanguage": "FR",
            }
        }

        result = prepare_qsf_for_import(qsf, "New Name")

        assert result["SurveyEntry"]["SurveyName"] == "New Name"
        assert result["SurveyEntry"]["SurveyStatus"] == "Inactive"
        assert "SurveyID" not in result["SurveyEntry"]
        assert result["SurveyEntry"]["SurveyLanguage"] == "FR"  # Preserved

    def test_language_override(self):
        """Test explicit language override."""
        qsf = {
            "SurveyEntry": {
                "SurveyName": "Test",
                "SurveyLanguage": "FR",
            }
        }

        result = prepare_qsf_for_import(qsf, "New", language="NL")

        assert result["SurveyEntry"]["SurveyLanguage"] == "NL"

    def test_default_language_when_missing(self):
        """Test default language is added when missing."""
        qsf = {
            "SurveyEntry": {
                "SurveyName": "Test",
            }
        }

        result = prepare_qsf_for_import(qsf, "New")

        assert result["SurveyEntry"]["SurveyLanguage"] == "EN"

    def test_status_override(self):
        """Test status can be set to Active."""
        qsf = {
            "SurveyEntry": {
                "SurveyName": "Test",
                "SurveyStatus": "Inactive",
            }
        }

        result = prepare_qsf_for_import(qsf, "New", status="Active")

        assert result["SurveyEntry"]["SurveyStatus"] == "Active"

    def test_missing_survey_entry(self):
        """Test graceful handling when SurveyEntry is missing."""
        qsf = {"SomeOtherKey": "value"}

        result = prepare_qsf_for_import(qsf, "New")

        assert result == qsf  # Unchanged

    def test_modifies_in_place_and_returns(self):
        """Test function modifies in-place and returns for chaining."""
        qsf = {
            "SurveyEntry": {
                "SurveyName": "Old",
            }
        }

        result = prepare_qsf_for_import(qsf, "New")

        assert result is qsf  # Same object
        assert qsf["SurveyEntry"]["SurveyName"] == "New"


class TestUploadQsfToAccount:
    """Tests for upload_qsf_to_account() helper."""

    @patch("qsync.cli_survey.send_api_request")
    def test_successful_upload(self, mock_send):
        """Test successful QSF upload."""
        mock_response = Mock()
        mock_response.json.return_value = {"result": {"id": "SV_new123"}}
        mock_send.return_value = mock_response

        qsf = {"SurveyEntry": {"SurveyName": "Test"}}
        base_url = "example.qualtrics.com"
        headers = {"X-API-TOKEN": "test"}

        result = upload_qsf_to_account(qsf, "Test Survey", base_url, headers)

        assert result == "SV_new123"

        # Verify API call
        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args[1]
        assert call_kwargs["method"] == "POST"
        assert call_kwargs["base_url"] == base_url
        assert call_kwargs["path"] == "surveys"
        assert "file" in call_kwargs["files"]
        assert call_kwargs["files"]["name"] == (None, "Test Survey")

    @patch("qsync.cli_survey.send_api_request")
    def test_upload_with_custom_action(self, mock_send):
        """Test upload with custom action identifier."""
        mock_response = Mock()
        mock_response.json.return_value = {"result": {"id": "SV_new456"}}
        mock_send.return_value = mock_response

        qsf = {"SurveyEntry": {}}
        result = upload_qsf_to_account(
            qsf,
            "Test",
            "base.url",
            {"key": "val"},
            action="custom.action",
        )

        assert result == "SV_new456"
        call_kwargs = mock_send.call_args[1]
        assert call_kwargs["action"] == "custom.action"

    @patch("qsync.cli_survey.send_api_request")
    def test_upload_with_log_meta(self, mock_send):
        """Test upload includes log metadata."""
        mock_response = Mock()
        mock_response.json.return_value = {"result": {"id": "SV_new789"}}
        mock_send.return_value = mock_response

        qsf = {"SurveyEntry": {}}
        meta = {"source": "SV_old123", "context": "test"}

        upload_qsf_to_account(
            qsf,
            "Test",
            "base.url",
            {"key": "val"},
            log_meta=meta,
        )

        call_kwargs = mock_send.call_args[1]
        assert call_kwargs["log_meta"] == meta

    @patch("qsync.cli_survey.send_api_request")
    def test_upload_removes_content_type_header(self, mock_send):
        """Test upload removes Content-Type from headers."""
        mock_response = Mock()
        mock_response.json.return_value = {"result": {"id": "SV_new999"}}
        mock_send.return_value = mock_response

        qsf = {"SurveyEntry": {}}
        headers = {"X-API-TOKEN": "test", "Content-Type": "application/json"}

        upload_qsf_to_account(qsf, "Test", "base.url", headers)

        call_kwargs = mock_send.call_args[1]
        assert "Content-Type" not in call_kwargs["headers"]
        assert call_kwargs["headers"]["X-API-TOKEN"] == "test"
        # Original headers unchanged
        assert headers["Content-Type"] == "application/json"

    @patch("qsync.cli_survey.send_api_request")
    def test_upload_no_survey_id_raises_error(self, mock_send):
        """Test upload raises error when no survey ID returned."""
        mock_response = Mock()
        mock_response.json.return_value = {"result": {}}
        mock_send.return_value = mock_response

        qsf = {"SurveyEntry": {}}

        with pytest.raises(RuntimeError, match="did not return a new Survey ID"):
            upload_qsf_to_account(qsf, "Test", "base.url", {"key": "val"})


class TestActivateSurvey:
    """Tests for activate_survey() helper."""

    @patch("qsync.cli_survey.send_api_request")
    def test_activate_success(self, mock_send):
        """Test successful survey activation."""
        mock_response = Mock()
        mock_response.ok = True
        mock_send.return_value = mock_response

        base_url = "example.qualtrics.com"
        headers = {"X-API-TOKEN": "test"}
        survey_id = "SV_test123"

        # Should not raise
        activate_survey(survey_id, base_url, headers, active=True)

        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args[1]
        assert call_kwargs["action"] == "qsync.survey.activate"
        assert call_kwargs["method"] == "PUT"
        assert call_kwargs["base_url"] == base_url
        assert call_kwargs["headers"] == headers
        assert call_kwargs["path"] == f"surveys/{survey_id}"
        assert call_kwargs["survey_id"] == survey_id
        assert call_kwargs["json"] == {"isActive": True}

    @patch("qsync.cli_survey.send_api_request")
    def test_deactivate_success(self, mock_send):
        """Test successful survey deactivation."""
        mock_response = Mock()
        mock_response.ok = True
        mock_send.return_value = mock_response

        activate_survey("SV_123", "base.url", {"key": "val"}, active=False)

        call_kwargs = mock_send.call_args[1]
        assert call_kwargs["action"] == "qsync.survey.deactivate"
        assert call_kwargs["json"] == {"isActive": False}

    @patch("qsync.cli_survey.send_api_request")
    def test_activate_with_log_meta(self, mock_send):
        """Test activation includes log metadata."""
        mock_response = Mock()
        mock_response.ok = True
        mock_send.return_value = mock_response

        meta = {"context": "cross-account", "source": "SV_old"}
        activate_survey(
            "SV_123",
            "base.url",
            {"key": "val"},
            log_meta=meta,
        )

        call_kwargs = mock_send.call_args[1]
        assert call_kwargs["log_meta"] == meta

    @patch("qsync.cli_survey.send_api_request")
    def test_activate_failure_raises_error(self, mock_send):
        """Test activation raises error on failure."""
        mock_response = Mock()
        mock_response.ok = False
        mock_response.status_code = 403
        mock_response.reason = "Forbidden"
        mock_send.return_value = mock_response

        with pytest.raises(
            RuntimeError, match="Failed to activate survey: 403 Forbidden"
        ):
            activate_survey("SV_123", "base.url", {"key": "val"})

    @patch("qsync.cli_survey.send_api_request")
    def test_deactivate_failure_raises_error(self, mock_send):
        """Test deactivation raises error on failure."""
        mock_response = Mock()
        mock_response.ok = False
        mock_response.status_code = 404
        mock_response.reason = "Not Found"
        mock_send.return_value = mock_response

        with pytest.raises(
            RuntimeError, match="Failed to deactivate survey: 404 Not Found"
        ):
            activate_survey("SV_123", "base.url", {"key": "val"}, active=False)
