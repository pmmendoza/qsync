"""
Tests for drift_check module.
"""

from unittest.mock import MagicMock, patch

import pytest

from qsync.drift_check import (
    DriftReport,
    check_drift,
    enforce_no_drift,
)


class TestCheckDrift:
    """Test check_drift function."""

    def test_no_drift_when_cache_matches_api(self):
        """No drift when cached survey matches API."""
        mock_cached = MagicMock()
        mock_cached.payload = {"result": {"SurveyID": "SV_123"}}

        mock_live = {"result": {"SurveyID": "SV_123"}}

        with (
            patch("qsync.drift_check.load_cached_survey", return_value=mock_cached),
            patch(
                "qsync.drift_check.fetch_survey_definition_live", return_value=mock_live
            ),
        ):

            report = check_drift("SV_123", "items")

            assert report.has_drift is False
            assert report.changed_count == 0
            assert "up to date" in report.recommendation.lower()

    def test_drift_detected_when_cache_differs_from_api(self):
        """Drift detected when cached survey differs from API."""
        mock_cached = MagicMock()
        mock_cached.payload = {"result": {"SurveyID": "SV_123", "old": "value"}}

        mock_live = {"result": {"SurveyID": "SV_123", "new": "value"}}

        with (
            patch("qsync.drift_check.load_cached_survey", return_value=mock_cached),
            patch(
                "qsync.drift_check.fetch_survey_definition_live", return_value=mock_live
            ),
        ):

            report = check_drift("SV_123", "items")

            assert report.has_drift is True
            assert report.changed_count > 0
            assert len(report.diff_lines) > 0
            assert "pull" in report.recommendation.lower()

    def test_missing_cache_treated_as_drift(self):
        """Missing cached survey is treated as drift."""
        with patch(
            "qsync.drift_check.load_cached_survey", side_effect=FileNotFoundError()
        ):

            report = check_drift("SV_123", "items")

            assert report.has_drift is True
            assert "No cached survey" in report.summary
            assert "pull" in report.recommendation.lower()

    def test_error_checking_drift_proceeds_safely(self):
        """Errors during drift check proceed safely without blocking."""
        with patch(
            "qsync.drift_check.load_cached_survey", side_effect=Exception("API error")
        ):

            report = check_drift("SV_123", "items")

            # Should not raise, should return safe report
            assert report.has_drift is False
            assert "Unable to check drift" in report.summary

    def test_diff_lines_parsed_correctly(self):
        """Diff text is split into lines correctly."""
        mock_cached = MagicMock()
        mock_cached.payload = {"result": {"a": "1"}}

        mock_live = {"result": {"a": "2", "b": "3", "c": "4"}}

        with (
            patch("qsync.drift_check.load_cached_survey", return_value=mock_cached),
            patch(
                "qsync.drift_check.fetch_survey_definition_live", return_value=mock_live
            ),
        ):

            report = check_drift("SV_123", "items")

            # Should have multiple diff lines showing the differences
            assert len(report.diff_lines) > 0
            assert report.has_drift is True


class TestEnforceNoDrift:
    """Test enforce_no_drift function."""

    def test_no_drift_passes(self):
        """No drift allows operation to proceed."""
        mock_report = DriftReport(
            has_drift=False,
            summary="No drift",
            diff_lines=[],
            recommendation="Proceed",
            changed_count=0,
        )

        with (
            patch("qsync.drift_check.check_drift", return_value=mock_report),
            patch("qsync.drift_check._warn_possible_drift", lambda *a, **k: None),
        ):
            result = enforce_no_drift("SV_123", "items")

            assert result.has_drift is False

    def test_drift_with_allow_drift_flag_proceeds(self, capsys):
        """Drift with --allow-drift flag proceeds with warning."""
        mock_report = DriftReport(
            has_drift=True,
            summary="Drift detected",
            diff_lines=["change1"],
            recommendation="Run pull",
            changed_count=1,
        )

        with (
            patch("qsync.drift_check.check_drift", return_value=mock_report),
            patch("qsync.drift_check._warn_possible_drift", lambda *a, **k: None),
        ):
            result = enforce_no_drift("SV_123", "items", allow_drift=True)

            assert result.has_drift is True
            captured = capsys.readouterr()
            assert "WARNING" in captured.out
            assert "allow-drift" in captured.out

    def test_drift_without_allow_drift_blocks_non_interactive(self):
        """Drift without --allow-drift blocks in non-interactive mode."""
        mock_report = DriftReport(
            has_drift=True,
            summary="Drift detected",
            diff_lines=["change1"],
            recommendation="Run pull",
            changed_count=1,
        )

        with (
            patch("qsync.drift_check.check_drift", return_value=mock_report),
            patch("qsync.drift_check._warn_possible_drift", lambda *a, **k: None),
        ):

            with pytest.raises(SystemExit, match="ERROR.*Drift detected"):
                enforce_no_drift("SV_123", "items", interactive=False)

    def test_drift_interactive_user_confirms_proceeds(self):
        """Drift in interactive mode still blocks without --allow-drift."""
        mock_report = DriftReport(
            has_drift=True,
            summary="Drift detected",
            diff_lines=["change1"],
            recommendation="Run pull",
            changed_count=1,
        )

        with (
            patch("qsync.drift_check.check_drift", return_value=mock_report),
            patch("qsync.drift_check._warn_possible_drift", lambda *a, **k: None),
        ):
            with pytest.raises(SystemExit, match="ERROR.*Drift detected"):
                enforce_no_drift("SV_123", "items", interactive=True)

    def test_drift_interactive_user_declines_blocks(self):
        """Drift in interactive mode with user declining blocks."""
        mock_report = DriftReport(
            has_drift=True,
            summary="Drift detected",
            diff_lines=["change1"],
            recommendation="Run pull",
            changed_count=1,
        )

        with (
            patch("qsync.drift_check.check_drift", return_value=mock_report),
            patch("qsync.drift_check._warn_possible_drift", lambda *a, **k: None),
            patch("builtins.input", return_value="no"),
        ):

            with pytest.raises(SystemExit, match="ERROR.*Drift detected"):
                enforce_no_drift("SV_123", "items", interactive=True)


class TestDriftReport:
    """Test DriftReport display."""

    def test_display_no_drift(self, capsys):
        """Display shows no drift message."""
        report = DriftReport(
            has_drift=False,
            summary="No drift",
            diff_lines=[],
            recommendation="Proceed",
            changed_count=0,
        )

        report.display()

        captured = capsys.readouterr()
        assert "No drift detected" in captured.out

    def test_display_drift_shows_summary(self, capsys):
        """Display shows drift summary."""
        report = DriftReport(
            has_drift=True,
            summary="Cache out of sync",
            diff_lines=["line1", "line2"],
            recommendation="Run pull",
            changed_count=2,
        )

        report.display()

        captured = capsys.readouterr()
        assert "DRIFT DETECTED" in captured.out
        assert "2 change(s) detected" in captured.out

    def test_display_truncates_long_diffs(self, capsys):
        """Display truncates diffs longer than 50 lines and shows first 10 as sample."""
        long_diff = [f"line{i}" for i in range(100)]

        report = DriftReport(
            has_drift=True,
            summary="Many changes",
            diff_lines=long_diff,
            recommendation="Run pull",
            changed_count=100,
        )

        report.display(interactive=True)

        captured = capsys.readouterr()
        assert "Diff available (100 lines)" in captured.out
        assert "drift menu" in captured.out

    def test_display_non_interactive_shows_summary_only(self, capsys):
        """Non-interactive display shows summary without full diff."""
        report = DriftReport(
            has_drift=True,
            summary="Drift detected",
            diff_lines=["line1", "line2", "line3"],
            recommendation="Run pull",
            changed_count=3,
        )

        report.display(interactive=False)

        captured = capsys.readouterr()
        assert "DRIFT DETECTED" in captured.out
        # Should not show the actual diff lines
        assert "line1" not in captured.out
        assert "Diff available" in captured.out
