"""
Tests for auto_publish module.
"""

from unittest.mock import patch

import pytest

from qsync.auto_publish import (
    PublishSkipped,
    auto_publish_after_push,
    validate_publish_description,
)


class TestAutoPublishAfterPush:
    """Test auto_publish_after_push function."""

    def test_skip_publish_flag_raises(self):
        """--no-publish flag skips publishing."""
        with pytest.raises(PublishSkipped, match="--no-publish"):
            auto_publish_after_push(
                survey_id="SV_123",
                dimension="items",
                skip_publish=True,
            )

    def test_auto_yes_uses_default_description(self, capsys):
        """--yes flag uses default description without prompting."""
        with patch("qsync.auto_publish.publish_survey_definition") as mock_publish:
            auto_publish_after_push(
                survey_id="SV_123",
                dimension="items",
                auto_yes=True,
                changed_qids=["QID1", "QID2"],
                count=2,
            )

            # Should call publish with default description
            mock_publish.assert_called_once()
            args, kwargs = mock_publish.call_args
            assert kwargs["survey_id"] == "SV_123"
            assert "qsync" in kwargs["description"].lower()
            assert kwargs["published"] is True

            # Should show auto-publish message
            captured = capsys.readouterr()
            assert "Auto-publishing" in captured.out

    def test_interactive_empty_input_uses_default(self):
        """Empty input in interactive mode uses default description."""
        with (
            patch("qsync.auto_publish.publish_survey_definition") as mock_publish,
            patch("builtins.input", return_value=""),
        ):

            result = auto_publish_after_push(
                survey_id="SV_123",
                dimension="items",
                auto_yes=False,
                changed_qids=["QID1"],
                count=1,
            )

            mock_publish.assert_called_once()
            assert result is not None

    def test_interactive_skip_input_raises(self):
        """'skip' input in interactive mode skips publishing."""
        with patch("builtins.input", return_value="skip"):

            with pytest.raises(PublishSkipped, match="User requested to skip"):
                auto_publish_after_push(
                    survey_id="SV_123",
                    dimension="items",
                    auto_yes=False,
                )

    def test_interactive_custom_description(self):
        """Custom description in interactive mode is used."""
        custom_desc = "My custom publish description"

        with (
            patch("qsync.auto_publish.publish_survey_definition") as mock_publish,
            patch("builtins.input", return_value=custom_desc),
        ):

            result = auto_publish_after_push(
                survey_id="SV_123",
                dimension="items",
                auto_yes=False,
            )

            mock_publish.assert_called_once()
            args, kwargs = mock_publish.call_args
            assert kwargs["description"] == custom_desc
            assert result == custom_desc

    def test_interactive_too_long_description_raises(self):
        """Description exceeding max length raises error."""
        long_desc = "x" * 200  # Exceeds 140 char limit

        with patch("builtins.input", return_value=long_desc):

            with pytest.raises(ValueError, match="must be <= 140 characters"):
                auto_publish_after_push(
                    survey_id="SV_123",
                    dimension="items",
                    auto_yes=False,
                )

    def test_items_dimension_default_description(self):
        """Items dimension generates appropriate default description."""
        with patch("qsync.auto_publish.publish_survey_definition") as mock_publish:
            auto_publish_after_push(
                survey_id="SV_123",
                dimension="items",
                auto_yes=True,
                changed_qids=["QID1", "QID2", "QID3"],
                count=3,
            )

            args, kwargs = mock_publish.call_args
            desc = kwargs["description"]
            assert "update items" in desc.lower() or "qsync" in desc.lower()
            assert "3" in desc or "question" in desc.lower()

    def test_js_dimension_default_description(self):
        """JS dimension generates appropriate default description."""
        with patch("qsync.auto_publish.publish_survey_definition") as mock_publish:
            auto_publish_after_push(
                survey_id="SV_123",
                dimension="js",
                auto_yes=True,
                changed_qids=["QID1"],
                count=1,
            )

            args, kwargs = mock_publish.call_args
            desc = kwargs["description"]
            assert "js" in desc.lower() or "qsync" in desc.lower()

    def test_translations_dimension_with_languages(self):
        """Translations dimension includes language list."""
        with patch("qsync.auto_publish.publish_survey_definition") as mock_publish:
            auto_publish_after_push(
                survey_id="SV_123",
                dimension="translations",
                auto_yes=True,
                languages=["FR", "NL", "DE"],
            )

            args, kwargs = mock_publish.call_args
            desc = kwargs["description"]
            assert "translations" in desc.lower()
            # Should include at least some languages
            assert any(lang in desc for lang in ["FR", "NL", "DE"])

    def test_translations_dimension_many_languages_truncates(self):
        """Translations dimension with many languages shows +N indicator."""
        with patch("qsync.auto_publish.publish_survey_definition") as mock_publish:
            auto_publish_after_push(
                survey_id="SV_123",
                dimension="translations",
                auto_yes=True,
                languages=["FR", "NL", "DE", "ES", "IT", "PL", "CZ"],
            )

            args, kwargs = mock_publish.call_args
            desc = kwargs["description"]
            # Should show +N for remaining languages
            assert "+" in desc

    def test_eos_dimension_default_description(self):
        """EOS dimension generates appropriate default description."""
        with patch("qsync.auto_publish.publish_survey_definition") as mock_publish:
            auto_publish_after_push(
                survey_id="SV_123",
                dimension="eos",
                auto_yes=True,
                count=5,
            )

            args, kwargs = mock_publish.call_args
            desc = kwargs["description"]
            assert "eos" in desc.lower()
            assert "5" in desc or "message" in desc.lower()

    def test_publishes_to_correct_survey(self):
        """Publish is called with correct survey ID."""
        with patch("qsync.auto_publish.publish_survey_definition") as mock_publish:
            auto_publish_after_push(
                survey_id="SV_XYZ789",
                dimension="items",
                auto_yes=True,
            )

            args, kwargs = mock_publish.call_args
            assert kwargs["survey_id"] == "SV_XYZ789"


class TestValidatePublishDescription:
    """Test validate_publish_description function."""

    def test_valid_description_passes(self):
        """Valid description passes validation."""
        validate_publish_description("This is a valid description")
        # Should not raise

    def test_empty_description_raises(self):
        """Empty description raises error."""
        with pytest.raises(ValueError, match="cannot be empty"):
            validate_publish_description("")

    def test_none_description_raises(self):
        """None description raises error."""
        with pytest.raises(ValueError, match="cannot be empty"):
            validate_publish_description(None)

    def test_too_long_description_raises(self):
        """Description exceeding 140 chars raises error."""
        long_desc = "x" * 200

        with pytest.raises(ValueError, match="must be <= 140 characters"):
            validate_publish_description(long_desc)

    def test_exactly_140_chars_passes(self):
        """Description of exactly 140 chars passes."""
        desc = "x" * 140
        validate_publish_description(desc)
        # Should not raise

    def test_whitespace_only_raises(self):
        """Whitespace-only description raises error."""
        # Note: Current implementation checks truthiness after strip is done in calling code
        # This test documents expected behavior
        with pytest.raises(ValueError):
            validate_publish_description("   ")
