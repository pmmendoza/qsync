"""
Tests for workbook_resolver module.
"""

import csv
from pathlib import Path
from unittest.mock import MagicMock, patch


from qsync.workbook_resolver import WorkbookResolver, _slugify


class TestSlugify:
    """Test _slugify helper function."""

    def test_alphanumeric_preserved(self):
        """Alphanumeric characters are preserved."""
        assert _slugify("abc123") == "abc123"

    def test_dashes_underscores_preserved(self):
        """Dashes and underscores are preserved."""
        assert _slugify("my-file_name") == "my-file_name"

    def test_spaces_converted_to_underscores(self):
        """Spaces are converted to underscores."""
        assert _slugify("my survey name") == "my_survey_name"

    def test_special_chars_converted(self):
        """Special characters are converted to underscores."""
        assert _slugify("survey!@#$%^&*()") == "survey__________"

    def test_mixed_case(self):
        """Case is preserved."""
        assert _slugify("MySurvey") == "MySurvey"


class TestWorkbookResolver:
    """Test WorkbookResolver class."""

    def test_explicit_absolute_path(self, tmp_path):
        """Explicit absolute path is returned as-is."""
        resolver = WorkbookResolver(root=tmp_path)
        explicit = Path("/absolute/path/to/workbook.xlsx")

        result = resolver.resolve("SV_123", explicit_path=explicit)
        assert result == explicit

    def test_explicit_relative_path(self, tmp_path):
        """Explicit relative path is made absolute relative to root."""
        resolver = WorkbookResolver(root=tmp_path)
        explicit = Path("custom/workbook.xlsx")

        result = resolver.resolve("SV_123", explicit_path=explicit)
        assert result == (tmp_path / "custom/workbook.xlsx").resolve()

    def test_default_path_format(self, tmp_path):
        """Default path follows format: excel/{slug}-{survey-id}.xlsx."""
        resolver = WorkbookResolver(root=tmp_path)

        result = resolver.default_path("SV_123")

        # Should be in excel/ directory
        assert result.parent == tmp_path / "excel"
        # Should end with survey ID
        assert result.name.endswith("-SV_123.xlsx")
        # Should have .xlsx extension
        assert result.suffix == ".xlsx"

    def test_slug_from_inventory_csv_name(self, tmp_path):
        """Slug derived from 'name' column in inventory CSV."""
        # Create surveys directory and CSV
        surveys_dir = tmp_path / "surveys"
        surveys_dir.mkdir()
        csv_path = surveys_dir / "inventory.csv"

        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["id", "name", "slug"])
            writer.writeheader()
            writer.writerow(
                {"id": "SV_123", "name": "My Test Survey", "slug": "ignored-slug"}
            )

        resolver = WorkbookResolver(root=tmp_path)
        result = resolver.default_path("SV_123")

        # Should use slugified name (format: {slug}-{survey-id}.xlsx)
        assert result.name == "My_Test_Survey-SV_123.xlsx"

    def test_slug_from_cached_survey_title(self, tmp_path):
        """When survey not in inventory CSV, use cached survey title."""
        # Create surveys directory but no matching CSV entry
        surveys_dir = tmp_path / "surveys"
        surveys_dir.mkdir()
        csv_path = surveys_dir / "inventory.csv"

        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["id", "name"])
            writer.writeheader()
            writer.writerow({"id": "SV_999", "name": "Other Survey"})

        # Mock load_cached_survey
        mock_survey = MagicMock()
        mock_survey.payload = {
            "result": {"SurveyOptions": {"SurveyTitle": "Cached Survey Title"}}
        }

        with patch(
            "qsync.qualtrics_client.load_cached_survey", return_value=mock_survey
        ):
            resolver = WorkbookResolver(root=tmp_path)
            result = resolver.default_path("SV_123")

        # Should use slugified title from cached survey (format: {slug}-{survey-id}.xlsx)
        assert result.name == "Cached_Survey_Title-SV_123.xlsx"

    def test_slug_fallback_to_survey_id(self, tmp_path):
        """When survey not in CSV and cache fails, use survey ID as slug."""
        # Create surveys directory but no matching CSV entry
        surveys_dir = tmp_path / "surveys"
        surveys_dir.mkdir()
        csv_path = surveys_dir / "inventory.csv"

        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["id", "name"])
            writer.writeheader()
            writer.writerow({"id": "SV_999", "name": "Other Survey"})

        # No CSV entry for SV_123, and mock load_cached_survey to fail
        with patch(
            "qsync.qualtrics_client.load_cached_survey",
            side_effect=Exception("No cache"),
        ):
            resolver = WorkbookResolver(root=tmp_path)
            result = resolver.default_path("SV_123")

        # Should use survey ID as slug
        assert result.name == "SV_123-SV_123.xlsx"

    def test_slug_empty_name_falls_back(self, tmp_path):
        """Empty 'name' in CSV falls back to next precedence."""
        surveys_dir = tmp_path / "surveys"
        surveys_dir.mkdir()
        csv_path = surveys_dir / "inventory.csv"

        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["id", "name"])
            writer.writeheader()
            writer.writerow({"id": "SV_123", "name": ""})  # Empty name

        mock_survey = MagicMock()
        mock_survey.payload = {
            "result": {"SurveyOptions": {"SurveyTitle": "Fallback Title"}}
        }

        with patch(
            "qsync.qualtrics_client.load_cached_survey", return_value=mock_survey
        ):
            resolver = WorkbookResolver(root=tmp_path)
            result = resolver.default_path("SV_123")

        # Should fall back to cached title (format: {slug}-{survey-id}.xlsx)
        assert result.name == "Fallback_Title-SV_123.xlsx"
        resolver = WorkbookResolver(root=tmp_path)

        result = resolver.resolve("SV_123")
        expected = resolver.default_path("SV_123")

        assert result == expected

    def test_repr(self, tmp_path):
        """Test string representation."""
        resolver = WorkbookResolver(root=tmp_path)

        assert "WorkbookResolver" in repr(resolver)
        assert str(tmp_path) in repr(resolver)

    def test_csv_read_error_handled(self, tmp_path):
        """CSV read errors are handled gracefully."""
        # Create invalid CSV
        surveys_dir = tmp_path / "surveys"
        surveys_dir.mkdir()
        csv_path = surveys_dir / "inventory.csv"

        # Write corrupt CSV
        with csv_path.open("w") as f:
            f.write("invalid\x00content")

        # Should not crash, should fall back
        with patch(
            "qsync.qualtrics_client.load_cached_survey", side_effect=Exception()
        ):
            resolver = WorkbookResolver(root=tmp_path)
            result = resolver.default_path("SV_123")

        # Should fall back to survey ID
        assert result.name == "SV_123-SV_123.xlsx"

    def test_no_csv_file(self, tmp_path):
        """Missing CSV file is handled gracefully."""
        # Don't create surveys directory at all
        with patch(
            "qsync.qualtrics_client.load_cached_survey", side_effect=Exception()
        ):
            resolver = WorkbookResolver(root=tmp_path)
            result = resolver.default_path("SV_123")

        # Should fall back to survey ID
        assert result.name == "SV_123-SV_123.xlsx"
