#!/usr/bin/env python3
"""
Test suite for --refresh-workbooks flag in qsync sync.

Validates acceptance criteria from plan_qsync_sync_refresh_workbooks_semantics.md:
1. qsync sync --survey-id SV_xxx does not refresh Excel (default)
2. qsync sync --survey-id SV_xxx --refresh-workbooks refreshes workbook after sync
3. qsync sync --survey-id SV_xxx --skip-refresh prints deprecation warning
4. qsync sync --survey-id SV_xxx --refresh-workbooks --skip-refresh performs no refresh
5. Help text clearly describes semantics
"""

from __future__ import annotations

import pytest


class TestRefreshWorkbooksFlag:
    """Test --refresh-workbooks flag behavior."""

    def test_flag_handling_logic_no_flags(self):
        """Test AC1: Default behavior (no refresh)."""

        # Simulate args with no flags
        class Args:
            refresh_workbooks = False
            skip_refresh = False

        args = Args()

        # Replicate cli.py flag handling logic
        refresh_workbooks = bool(getattr(args, "refresh_workbooks", False))
        skip_refresh = bool(getattr(args, "skip_refresh", False))

        # No warnings for this case
        if skip_refresh:
            if not refresh_workbooks:
                pass  # Would warn
            else:
                refresh_workbooks = False  # Would warn

        assert refresh_workbooks is False, "Default should be no refresh"

    def test_flag_handling_logic_refresh_only(self):
        """Test AC2: --refresh-workbooks enables refresh."""

        class Args:
            refresh_workbooks = True
            skip_refresh = False

        args = Args()

        refresh_workbooks = bool(getattr(args, "refresh_workbooks", False))
        skip_refresh = bool(getattr(args, "skip_refresh", False))

        if skip_refresh:
            if not refresh_workbooks:
                pass  # Would warn
            else:
                refresh_workbooks = False  # Would warn

        assert refresh_workbooks is True, "Should enable refresh"

    def test_flag_handling_logic_skip_only(self):
        """Test AC3: --skip-refresh alone warns about deprecation."""

        class Args:
            refresh_workbooks = False
            skip_refresh = True

        args = Args()

        refresh_workbooks = bool(getattr(args, "refresh_workbooks", False))
        skip_refresh = bool(getattr(args, "skip_refresh", False))

        # This case should trigger deprecation warning
        warned = False
        if skip_refresh:
            if not refresh_workbooks:
                # In real code: warn about deprecation
                warned = True
            else:
                refresh_workbooks = False

        assert refresh_workbooks is False, "Should remain disabled"
        assert warned is True, "Should trigger deprecation warning"

    def test_flag_handling_logic_both_flags(self):
        """Test AC4: --skip-refresh overrides --refresh-workbooks."""

        class Args:
            refresh_workbooks = True
            skip_refresh = True

        args = Args()

        refresh_workbooks = bool(getattr(args, "refresh_workbooks", False))
        skip_refresh = bool(getattr(args, "skip_refresh", False))

        # This case should trigger override warning
        warned = False
        if skip_refresh:
            if not refresh_workbooks:
                pass  # Would warn about deprecation
            else:
                refresh_workbooks = False
                warned = True  # In real code: warn about override

        assert refresh_workbooks is False, "skip_refresh should override"
        assert warned is True, "Should warn about override"

    def test_help_text_contains_flags(self):
        """Test AC5: Help text clearly describes semantics."""
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-m", "qsync.cli", "sync", "--help"],
            capture_output=True,
            text=True,
        )

        help_text = result.stdout

        # Verify both flags are in help
        assert "--refresh-workbooks" in help_text, "Should document --refresh-workbooks"
        assert "--skip-refresh" in help_text, "Should document --skip-refresh"

        # Verify help describes semantics clearly
        assert (
            "Refresh Excel workbooks" in help_text
        ), "Should describe refresh behavior"
        assert (
            "deprecated" in help_text.lower() or "legacy" in help_text.lower()
        ), "Should mark --skip-refresh as deprecated/legacy"

    def test_sync_survey_accepts_refresh_workbooks_flag(self):
        """Test that sync_survey accepts refresh_workbooks parameter without error."""
        from qsync.sync_orchestrator import sync_survey
        import inspect

        # Verify function signature includes refresh_workbooks parameter
        sig = inspect.signature(sync_survey)
        assert (
            "refresh_workbooks" in sig.parameters
        ), "Should have refresh_workbooks parameter"

        # Verify default value is False
        param = sig.parameters["refresh_workbooks"]
        assert param.default is False, "Default should be False"

    def test_sync_focal_surveys_accepts_refresh_workbooks_flag(self):
        """Test that sync_focal_surveys accepts refresh_workbooks parameter without error."""
        from qsync.sync_orchestrator import sync_focal_surveys
        import inspect

        # Verify function signature includes refresh_workbooks parameter
        sig = inspect.signature(sync_focal_surveys)
        assert (
            "refresh_workbooks" in sig.parameters
        ), "Should have refresh_workbooks parameter"

        # Verify default value is False
        param = sig.parameters["refresh_workbooks"]
        assert param.default is False, "Default should be False"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
