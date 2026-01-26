"""Tests for translation language detection."""

from __future__ import annotations

import pytest

from qsync.cli_translations_check import check_translation_language


class TestCheckTranslationLanguage:
    """Tests for the check_translation_language function."""

    def test_french_text_detected_correctly(self):
        """Test that French text is correctly identified as French."""
        text = "Bonjour, comment allez-vous aujourd'hui?"
        is_valid, detected = check_translation_language(text, "FR", allow_single_words=True)
        assert is_valid is True
        assert detected == "fr"

    def test_dutch_text_detected_correctly(self):
        """Test that Dutch text is correctly identified as Dutch."""
        text = "Hallo, hoe gaat het met u vandaag?"
        is_valid, detected = check_translation_language(text, "NL", allow_single_words=True)
        assert is_valid is True
        assert detected == "nl"

    def test_czech_text_detected_correctly(self):
        """Test that Czech text is correctly identified as Czech."""
        text = "Dobrý den, jak se máte dnes?"
        is_valid, detected = check_translation_language(text, "CS", allow_single_words=True)
        assert is_valid is True
        assert detected == "cs"

    def test_english_in_french_block_detected(self):
        """Test that English text in French block is detected as mismatch."""
        text = "Hello, how are you today?"
        is_valid, detected = check_translation_language(text, "FR", allow_single_words=True)
        assert is_valid is False
        assert detected == "en"

    def test_english_in_dutch_block_detected(self):
        """Test that English text in Dutch block is detected as mismatch."""
        text = "Hello, how are you today?"
        is_valid, detected = check_translation_language(text, "NL", allow_single_words=True)
        assert is_valid is False
        assert detected == "en"

    def test_single_word_allowed_in_english(self):
        """Test that single words are allowed to be in English."""
        text = "Debug"
        is_valid, detected = check_translation_language(text, "FR", allow_single_words=True)
        assert is_valid is True
        assert detected is None

    def test_single_word_not_allowed_when_disabled(self):
        """Test that single words are checked when allow_single_words=False."""
        text = "Debug"
        is_valid, detected = check_translation_language(text, "FR", allow_single_words=False)
        # Should detect as English and mark invalid
        assert is_valid is False
        assert detected == "en"

    def test_empty_string_is_valid(self):
        """Test that empty strings are considered valid."""
        text = ""
        is_valid, detected = check_translation_language(text, "FR", allow_single_words=True)
        assert is_valid is True
        assert detected is None

    def test_whitespace_only_is_valid(self):
        """Test that whitespace-only strings are considered valid."""
        text = "   \n   "
        is_valid, detected = check_translation_language(text, "FR", allow_single_words=True)
        assert is_valid is True
        assert detected is None

    def test_html_stripped_before_detection(self):
        """Test that HTML is stripped before language detection."""
        text = "<p>Bonjour, comment <strong>allez-vous</strong> aujourd'hui?</p>"
        is_valid, detected = check_translation_language(text, "FR", allow_single_words=True)
        # Should detect French after HTML is stripped
        assert is_valid is True
        assert detected == "fr"

    def test_french_with_html_entities(self):
        """Test French text with HTML entities."""
        text = "Bonjour&nbsp;! Comment ça va&nbsp;?"
        is_valid, detected = check_translation_language(text, "FR", allow_single_words=True)
        # After decoding HTML entities, should detect as French
        assert is_valid is True

    def test_multiword_english_in_french_detected(self):
        """Test that multi-word English text in French block is caught."""
        text = "Please enter your email address"
        is_valid, detected = check_translation_language(text, "FR", allow_single_words=True)
        assert is_valid is False
        assert detected == "en"

    def test_multiword_dutch_in_french_block_detected(self):
        """Test that Dutch text in French block is detected as mismatch."""
        text = "Voer alstublieft uw e-mailadres in"
        is_valid, detected = check_translation_language(text, "FR", allow_single_words=True)
        assert is_valid is False
        assert detected == "nl"


class TestLanguageDetectionEdgeCases:
    """Test edge cases for language detection."""

    def test_very_short_text_with_multiple_words(self):
        """Test very short text with 2-3 words."""
        text = "Bonjour madame"
        is_valid, detected = check_translation_language(text, "FR", allow_single_words=True)
        assert is_valid is True
        assert detected == "fr"

    def test_mixed_language_text(self):
        """Test text with mixed languages (should detect primary language)."""
        # Mostly French with one English word
        text = "Bonjour, comment allez-vous aujourd'hui? Please note"
        is_valid, detected = check_translation_language(text, "FR", allow_single_words=True)
        # Language detection should detect the dominant language (French)
        # This test might be flaky depending on detection algorithm
        assert detected in ["fr", "en"]

    def test_numbers_and_punctuation_only(self):
        """Test strings with only numbers and punctuation."""
        text = "123 456 789 !@#"
        is_valid, detected = check_translation_language(text, "FR", allow_single_words=True)
        # Should not be able to detect language
        assert is_valid is False
        assert detected is None

    def test_technical_terms_in_translation(self):
        """Test translation with technical English terms."""
        # Dutch sentence with technical terms
        text = "Klik op de button om de API request te starten"
        is_valid, detected = check_translation_language(text, "NL", allow_single_words=True)
        # Should still detect as Dutch despite English terms
        assert detected == "nl"
