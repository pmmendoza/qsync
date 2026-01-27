"""
Tests for unified push_safeguards module.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from qsync.push_policy import PushContext
from qsync.push_safeguards import (
    SafeguardConfig,
    SafeguardResult,
    enforce_push_safeguards,
)
from qsync.survey_lock import SurveyLockedError


@pytest.fixture
def mock_push_context():
    """Create a mock push context with no responses."""
    return PushContext(
        survey_id="SV_123",
        survey_name="Test Survey",
        preview_count=0,
        response_count=0,
        counts_source="inventory",
        generated_at=datetime.now(timezone.utc),
        stale=False,
        counts_unknown=False,
    )


class TestEnforcePushSafeguards:
    """Test enforce_push_safeguards function."""
    
    def test_unlocked_survey_no_responses_passes(self, mock_push_context):
        """Unlocked survey with no responses passes all safeguards."""
        with patch("qsync.push_safeguards.ensure_unlocked"), \
             patch("qsync.push_safeguards.load_push_context", return_value=mock_push_context):
            
            config = SafeguardConfig(
                survey_id="SV_123",
                dimension="items",
            )
            result = enforce_push_safeguards(config)
            
            assert isinstance(result, SafeguardResult)
            assert result.push_context == mock_push_context
            assert not result.blocked
            assert len(result.warnings) == 0
    
    def test_locked_survey_blocks_without_allow_locked(self, mock_push_context):
        """Locked survey blocks push without --allow-locked."""
        with patch("qsync.push_safeguards.ensure_unlocked", side_effect=SurveyLockedError("Survey is locked")), \
             patch("qsync.push_safeguards.load_push_context", return_value=mock_push_context), \
             patch("qsync.push_safeguards.log_push_event"):
            
            config = SafeguardConfig(
                survey_id="SV_123",
                dimension="items",
                allow_locked=False,
            )
            
            with pytest.raises(SystemExit, match="ERROR:.*locked"):
                enforce_push_safeguards(config)
    
    def test_locked_survey_passes_with_allow_locked(self, mock_push_context):
        """Locked survey passes with --allow-locked."""
        # Even with allow_locked, ensure_unlocked is skipped
        with patch("qsync.push_safeguards.load_push_context", return_value=mock_push_context):
            
            config = SafeguardConfig(
                survey_id="SV_123",
                dimension="items",
                allow_locked=True,  # Override
            )
            
            result = enforce_push_safeguards(config)
            assert result.push_context == mock_push_context
    
    def test_unknown_counts_blocks_without_force_live(self, mock_push_context):
        """Unknown response counts block without --force-live."""
        mock_push_context.counts_unknown = True
        
        with patch("qsync.push_safeguards.ensure_unlocked"), \
             patch("qsync.push_safeguards.load_push_context", return_value=mock_push_context):
            
            config = SafeguardConfig(
                survey_id="SV_123",
                dimension="items",
            )
            
            with pytest.raises(SystemExit, match="Unable to verify response counts"):
                enforce_push_safeguards(config)
    
    def test_unknown_counts_passes_with_force_live(self, mock_push_context):
        """Unknown response counts pass with --force-live."""
        mock_push_context.counts_unknown = True
        
        with patch("qsync.push_safeguards.ensure_unlocked"), \
             patch("qsync.push_safeguards.load_push_context", return_value=mock_push_context):
            
            config = SafeguardConfig(
                survey_id="SV_123",
                dimension="items",
                force_live=True,
            )
            
            result = enforce_push_safeguards(config)
            assert result.push_context == mock_push_context
    
    def test_live_responses_blocks_without_force_live(self, mock_push_context):
        """Live responses block without --force-live."""
        mock_push_context.response_count = 5
        
        with patch("qsync.push_safeguards.ensure_unlocked"), \
             patch("qsync.push_safeguards.load_push_context", return_value=mock_push_context):
            
            config = SafeguardConfig(
                survey_id="SV_123",
                dimension="items",
            )
            
            with pytest.raises(SystemExit, match=r"has 5 finished response"):
                enforce_push_safeguards(config)
    
    def test_live_responses_warns_with_force_live_interactive_abort(self, mock_push_context, capsys):
        """Live responses with --force-live warns and prompts, aborts on 'n'."""
        mock_push_context.response_count = 3
        
        with patch("qsync.push_safeguards.ensure_unlocked"), \
             patch("qsync.push_safeguards.load_push_context", return_value=mock_push_context), \
             patch("qsync.push_safeguards._prompt_confirmation", return_value=False):
            
            config = SafeguardConfig(
                survey_id="SV_123",
                dimension="items",
                force_live=True,
            )
            
            with pytest.raises(SystemExit, match="Aborted by user"):
                enforce_push_safeguards(config)
            
            captured = capsys.readouterr()
            assert "WARNING: pushing" in captured.out
            assert "despite live responses" in captured.out
    
    def test_live_responses_proceeds_with_force_live_auto_yes(self, mock_push_context):
        """Live responses with --force-live and --yes proceeds without prompt."""
        mock_push_context.response_count = 3
        
        with patch("qsync.push_safeguards.ensure_unlocked"), \
             patch("qsync.push_safeguards.load_push_context", return_value=mock_push_context):
            
            config = SafeguardConfig(
                survey_id="SV_123",
                dimension="items",
                force_live=True,
                auto_yes=True,
            )
            
            result = enforce_push_safeguards(config)
            assert result.push_context == mock_push_context
            assert len(result.warnings) > 0
            assert any("WARNING" in w for w in result.warnings)
    
    def test_preview_only_warns_without_force_preview(self, mock_push_context, capsys):
        """Preview-only responses warn without --force-preview."""
        mock_push_context.preview_count = 2
        
        with patch("qsync.push_safeguards.ensure_unlocked"), \
             patch("qsync.push_safeguards.load_push_context", return_value=mock_push_context), \
             patch("qsync.push_safeguards._prompt_confirmation", return_value=True):
            
            config = SafeguardConfig(
                survey_id="SV_123",
                dimension="items",
            )
            
            result = enforce_push_safeguards(config)
            assert result.push_context == mock_push_context
            
            captured = capsys.readouterr()
            assert "WARNING" in captured.out
            assert "preview/test response" in captured.out
    
    def test_preview_only_suppresses_warning_with_force_preview(self, mock_push_context, capsys):
        """Preview-only responses with --force-preview suppresses warning."""
        mock_push_context.preview_count = 2
        
        with patch("qsync.push_safeguards.ensure_unlocked"), \
             patch("qsync.push_safeguards.load_push_context", return_value=mock_push_context):
            
            config = SafeguardConfig(
                survey_id="SV_123",
                dimension="items",
                force_preview=True,
            )
            
            result = enforce_push_safeguards(config)
            assert result.push_context == mock_push_context
            assert len(result.warnings) == 0
            
            captured = capsys.readouterr()
            # Should not show preview warning
            assert "preview" not in captured.out.lower()
    
    def test_preview_only_aborts_on_no(self, mock_push_context):
        """Preview-only responses abort if user declines prompt."""
        mock_push_context.preview_count = 2
        
        with patch("qsync.push_safeguards.ensure_unlocked"), \
             patch("qsync.push_safeguards.load_push_context", return_value=mock_push_context), \
             patch("qsync.push_safeguards._prompt_confirmation", return_value=False):
            
            config = SafeguardConfig(
                survey_id="SV_123",
                dimension="js",
            )
            
            with pytest.raises(SystemExit, match="Aborted by user"):
                enforce_push_safeguards(config)
    
    def test_stale_inventory_shows_note(self, mock_push_context, capsys):
        """Stale inventory timestamp shows note but doesn't block."""
        mock_push_context.stale = True
        
        with patch("qsync.push_safeguards.ensure_unlocked"), \
             patch("qsync.push_safeguards.load_push_context", return_value=mock_push_context):
            
            config = SafeguardConfig(
                survey_id="SV_123",
                dimension="items",
            )
            
            result = enforce_push_safeguards(config)
            assert result.push_context == mock_push_context
            
            captured = capsys.readouterr()
            assert "NOTE:" in captured.out
            assert "inventory timestamp is older" in captured.out
    
    def test_dimension_specific_prompts(self, mock_push_context):
        """Different dimensions show appropriate confirmation prompts."""
        mock_push_context.preview_count = 1
        
        dimensions_and_prompts = [
            ("items", "Push item wording anyway?"),
            ("js", "Continue with JS push?"),
            ("translations", "Continue with translation push?"),
            ("eos", "Continue with EOS message push?"),
        ]
        
        for dimension, expected_prompt in dimensions_and_prompts:
            with patch("qsync.push_safeguards.ensure_unlocked"), \
                 patch("qsync.push_safeguards.load_push_context", return_value=mock_push_context), \
                 patch("qsync.push_safeguards._prompt_confirmation", return_value=True) as mock_prompt:
                
                config = SafeguardConfig(
                    survey_id="SV_123",
                    dimension=dimension,
                )
                
                result = enforce_push_safeguards(config)
                
                # Verify the prompt was called with the right message
                mock_prompt.assert_called_once_with(expected_prompt)
    
    def test_all_dimensions_enforce_consistently(self, mock_push_context):
        """All dimensions (items, js, translations, eos) enforce safeguards consistently."""
        mock_push_context.response_count = 5
        
        for dimension in ["items", "js", "translations", "eos"]:
            with patch("qsync.push_safeguards.ensure_unlocked"), \
                 patch("qsync.push_safeguards.load_push_context", return_value=mock_push_context):
                
                config = SafeguardConfig(
                    survey_id="SV_123",
                    dimension=dimension,
                )
                
                # All dimensions should block on live responses
                with pytest.raises(SystemExit, match="finished response"):
                    enforce_push_safeguards(config)
    
    def test_force_live_overrides_preview_warning(self, mock_push_context):
        """--force-live also suppresses preview warnings."""
        mock_push_context.preview_count = 2
        
        with patch("qsync.push_safeguards.ensure_unlocked"), \
             patch("qsync.push_safeguards.load_push_context", return_value=mock_push_context):
            
            config = SafeguardConfig(
                survey_id="SV_123",
                dimension="items",
                force_live=True,  # Should suppress preview warnings too
            )
            
            result = enforce_push_safeguards(config)
            # Should pass without prompting
            assert result.push_context == mock_push_context


class TestPromptConfirmation:
    """Test _prompt_confirmation helper."""
    
    @pytest.mark.parametrize("response,expected", [
        ("y", True),
        ("yes", True),
        ("Y", True),
        ("YES", True),
        ("n", False),
        ("no", False),
        ("", True),
        ("maybe", False),
    ])
    def test_prompt_responses(self, response, expected):
        """Test various user input responses."""
        from qsync.push_safeguards import _prompt_confirmation
        
        with patch("builtins.input", return_value=response):
            result = _prompt_confirmation("Test prompt?")
            assert result == expected
