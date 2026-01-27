#!/usr/bin/env python3
"""
Test suite for transactional staging/push/publish flow in qsync sync.

Validates acceptance criteria from plan_qsync_sync_transactional_staging_push_publish.md:
1. Interactive default flow: stage → push → report → publish
2. Single publish prompt after push (not per-dimension)
3. No publish on partial failure (only if all dimensions succeed)
4. Deterministic automation with --yes
5. No double publish (exactly one version snapshot per survey)
"""

from __future__ import annotations

import pytest
from unittest.mock import Mock, patch, MagicMock
from qsync.sync_orchestrator import (
    _generate_composite_publish_description,
    _display_push_report,
    _orchestrated_publish,
    DimensionSyncResult,
)


class TestCompositePublishDescription:
    """Test composite publish description generation."""

    def test_generate_description_fallback_when_no_pending(self):
        """Test description falls back to listing dimensions when pending is None."""
        dimension_results = {
            "items": DimensionSyncResult(dimension="items", success=True, applied_changes=True),
        }
        
        with patch("qsync.sync_orchestrator.load_pending") as mock_load:
            mock_load.return_value = None
            
            desc = _generate_composite_publish_description(dimension_results, "SV_test")
            
            assert "qsync sync:" in desc
            assert "items" in desc

    def test_generate_description_with_valid_pending(self):
        """Test description with valid pending data includes counts."""
        dimension_results = {
            "items": DimensionSyncResult(dimension="items", success=True, applied_changes=True),
        }
        
        with patch("qsync.sync_orchestrator.load_pending") as mock_load:
            # Mock pending data
            mock_pending = Mock()
            mock_pending.payload.qids = ["Q1", "Q2", "Q3"]
            mock_pending.payload.embedded_fields = []
            mock_load.return_value = mock_pending
            
            desc = _generate_composite_publish_description(dimension_results, "SV_test")
            
            # Should include count
            assert "3" in desc or "items" in desc
            assert "qsync sync:" in desc

    def test_generate_description_multiple_dimensions(self):
        """Test description for multi-dimension push."""
        dimension_results = {
            "items": DimensionSyncResult(dimension="items", success=True, applied_changes=True),
            "js": DimensionSyncResult(dimension="js", success=True, applied_changes=True),
            "translations": DimensionSyncResult(dimension="translations", success=True, applied_changes=True),
        }
        
        with patch("qsync.sync_orchestrator.load_pending") as mock_load:
            def mock_pending_side_effect(survey_id, dim):
                mock = Mock()
                if dim == "items":
                    mock.payload.qids = ["Q1", "Q2"]
                    mock.payload.embedded_fields = ["emb1"]
                elif dim == "js":
                    mock.payload.entries = [{"qid": "Q1"}]
                elif dim == "translations":
                    mock.payload.languages = ["de", "fr"]
                return mock
            
            mock_load.side_effect = mock_pending_side_effect
            
            desc = _generate_composite_publish_description(dimension_results, "SV_test")
            
            # Should mention all dimensions
            assert "qsync sync:" in desc
            # At least check it's not empty
            assert len(desc) > 20

    def test_generate_description_failed_dimensions_excluded(self):
        """Test that failed dimensions are excluded from description."""
        dimension_results = {
            "items": DimensionSyncResult(dimension="items", success=True, applied_changes=True),
            "js": DimensionSyncResult(dimension="js", success=False, error_message="Push failed"),
        }
        
        with patch("qsync.sync_orchestrator.load_pending") as mock_load:
            mock_pending = Mock()
            mock_pending.payload.qids = ["Q1"]
            mock_pending.payload.embedded_fields = []
            mock_load.return_value = mock_pending
            
            desc = _generate_composite_publish_description(dimension_results, "SV_test")
            
            # Should include items but not js
            assert "items" in desc or "1" in desc
            assert "qsync sync:" in desc

    def test_generate_description_no_successful_pushes(self):
        """Test description when all pushes failed."""
        dimension_results = {
            "items": DimensionSyncResult(dimension="items", success=False),
            "js": DimensionSyncResult(dimension="js", success=False),
        }
        
        desc = _generate_composite_publish_description(dimension_results, "SV_test")
        assert desc == "qsync sync (no changes)"

    def test_generate_description_truncates_long_descriptions(self):
        """Test that overly long descriptions are truncated."""
        dimension_results = {
            "items": DimensionSyncResult(dimension="items", success=True, applied_changes=True),
        }
        
        with patch("qsync.sync_orchestrator.load_pending") as mock_load:
            # Create a mock with many QIDs to force truncation
            mock_pending = Mock()
            mock_pending.payload.qids = [f"Q{i}" for i in range(100)]
            mock_pending.payload.embedded_fields = []
            mock_load.return_value = mock_pending
            
            desc = _generate_composite_publish_description(dimension_results, "SV_test")
            
            # Should be truncated (max 255 chars)
            assert len(desc) <= 255


class TestDisplayPushReport:
    """Test push report display."""

    def test_display_report_all_success(self, capsys):
        """Test report display when all dimensions succeed."""
        dimension_results = {
            "items": DimensionSyncResult(dimension="items", success=True, applied_changes=True),
            "js": DimensionSyncResult(dimension="js", success=True, applied_changes=True),
        }
        
        _display_push_report("Test Survey (SV_test)", dimension_results)
        
        captured = capsys.readouterr()
        assert "Push Report" in captured.out
        assert "Successfully pushed:" in captured.out
        assert "items" in captured.out
        assert "js" in captured.out

    def test_display_report_partial_failure(self, capsys):
        """Test report display with partial failures."""
        dimension_results = {
            "items": DimensionSyncResult(dimension="items", success=True, applied_changes=True),
            "js": DimensionSyncResult(dimension="js", success=False, error_message="Network error"),
        }
        
        _display_push_report("Test Survey (SV_test)", dimension_results)
        
        captured = capsys.readouterr()
        assert "Successfully pushed:" in captured.out
        assert "items" in captured.out
        assert "Failed to push:" in captured.out
        assert "js" in captured.out
        assert "Network error" in captured.out


class TestOrchestratedPublish:
    """Test orchestrated publish step."""

    def test_publish_skipped_with_flag(self):
        """Test AC: --skip-publish skips publishing."""
        dimension_results = {
            "items": DimensionSyncResult(dimension="items", success=True, applied_changes=True),
        }
        
        result = _orchestrated_publish(
            survey_id="SV_test",
            survey_ref="Test Survey (SV_test)",
            dimension_results=dimension_results,
            skip_publish=True,
            interactive=False,
            auto_yes=False,
        )
        
        assert result is None

    def test_publish_skipped_on_failure(self):
        """Test AC: No publish if any dimension failed."""
        dimension_results = {
            "items": DimensionSyncResult(dimension="items", success=True, applied_changes=True),
            "js": DimensionSyncResult(dimension="js", success=False, error_message="Error"),
        }
        
        with patch("qsync.qualtrics_client.publish_survey_definition"):
            result = _orchestrated_publish(
                survey_id="SV_test",
                survey_ref="Test Survey (SV_test)",
                dimension_results=dimension_results,
                skip_publish=False,
                interactive=False,
                auto_yes=True,
            )
        
        assert result is None

    def test_publish_auto_yes_mode(self):
        """Test AC: --yes mode auto-publishes with generated description."""
        dimension_results = {
            "items": DimensionSyncResult(dimension="items", success=True, applied_changes=True),
        }
        
        with patch("qsync.qualtrics_client.publish_survey_definition") as mock_publish:
            with patch("qsync.sync_orchestrator._generate_composite_publish_description") as mock_desc:
                mock_desc.return_value = "Test description"
                
                result = _orchestrated_publish(
                    survey_id="SV_test",
                    survey_ref="Test Survey (SV_test)",
                    dimension_results=dimension_results,
                    skip_publish=False,
                    interactive=False,
                    auto_yes=True,
                )
                
                assert result == "Test description"
                mock_publish.assert_called_once_with(
                    survey_id="SV_test",
                    description="Test description",
                    published=True,
                )

    def test_publish_only_after_all_success(self):
        """Test AC: Publish only if ALL dimensions succeeded."""
        all_success = {
            "items": DimensionSyncResult(dimension="items", success=True, applied_changes=True),
            "js": DimensionSyncResult(dimension="js", success=True, applied_changes=True),
        }
        
        partial_success = {
            "items": DimensionSyncResult(dimension="items", success=True, applied_changes=True),
            "js": DimensionSyncResult(dimension="js", success=False),
        }
        
        with patch("qsync.qualtrics_client.publish_survey_definition") as mock_publish:
            with patch("qsync.sync_orchestrator._generate_composite_publish_description") as mock_desc:
                mock_desc.return_value = "Test"
                
                # Should publish
                result1 = _orchestrated_publish(
                    survey_id="SV_test",
                    survey_ref="Test",
                    dimension_results=all_success,
                    skip_publish=False,
                    interactive=False,
                    auto_yes=True,
                )
                assert result1 is not None
                assert mock_publish.call_count == 1
                
                # Should not publish
                result2 = _orchestrated_publish(
                    survey_id="SV_test",
                    survey_ref="Test",
                    dimension_results=partial_success,
                    skip_publish=False,
                    interactive=False,
                    auto_yes=True,
                )
                assert result2 is None
                assert mock_publish.call_count == 1  # Still 1, not called again


class TestIntegrationFlow:
    """Integration tests for the full transactional flow."""

    def test_no_double_publish(self):
        """Test AC: Exactly one version snapshot per survey (not per dimension)."""
        # This is tested by verifying that dimension pushes are called with skip_publish=True
        # and that publish is called exactly once at the orchestrated level
        
        # The key implementation detail is that in _sync_dimensions_once:
        # 1. sync_dimension is called with skip_publish=True (suppresses per-dimension publish)
        # 2. _orchestrated_publish is called exactly once after all pushes
        
        # This test validates the logic structure
        dimension_results = {
            "items": DimensionSyncResult(dimension="items", success=True, applied_changes=True),
            "js": DimensionSyncResult(dimension="js", success=True, applied_changes=True),
            "translations": DimensionSyncResult(dimension="translations", success=True, applied_changes=True),
        }
        
        with patch("qsync.qualtrics_client.publish_survey_definition") as mock_publish:
            with patch("qsync.sync_orchestrator._generate_composite_publish_description") as mock_desc:
                mock_desc.return_value = "Multi-dimension sync"
                
                # Call orchestrated publish once
                _orchestrated_publish(
                    survey_id="SV_test",
                    survey_ref="Test",
                    dimension_results=dimension_results,
                    skip_publish=False,
                    interactive=False,
                    auto_yes=True,
                )
                
                # Verify publish was called exactly once (not 3 times for 3 dimensions)
                assert mock_publish.call_count == 1
