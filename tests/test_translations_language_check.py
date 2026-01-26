"""Tests for translation language detection."""

from __future__ import annotations

from qsync.cli_translations_check import check_translation_language

MIN_CONFIDENCE = 0.85
MIN_MARGIN = 0.15


class TestCheckTranslationLanguage:
    """Tests for the check_translation_language function."""

    def test_french_text_detected_correctly(self):
        """Test that French text is correctly identified as French."""
        text = "Bonjour, comment allez-vous aujourd'hui?"
        decision = check_translation_language(
            text,
            "FR",
            allow_single_words=True,
            min_confidence=MIN_CONFIDENCE,
            min_margin=MIN_MARGIN,
        )
        assert decision.status == "pass"
        assert decision.detected == "fr"

    def test_dutch_text_detected_correctly(self):
        """Test that Dutch text is correctly identified as Dutch."""
        text = "Hallo, hoe gaat het met u vandaag?"
        decision = check_translation_language(
            text,
            "NL",
            allow_single_words=True,
            min_confidence=MIN_CONFIDENCE,
            min_margin=MIN_MARGIN,
        )
        assert decision.status == "pass"
        assert decision.detected == "nl"

    def test_czech_text_detected_correctly(self):
        """Test that Czech text is correctly identified as Czech."""
        text = "Dobrý den, jak se máte dnes?"
        decision = check_translation_language(
            text,
            "CS",
            allow_single_words=True,
            min_confidence=MIN_CONFIDENCE,
            min_margin=MIN_MARGIN,
        )
        assert decision.status == "pass"
        assert decision.detected == "cs"

    def test_english_in_french_block_detected(self):
        """Test that English text in French block is detected as mismatch."""
        text = "Hello, how are you today?"
        decision = check_translation_language(
            text,
            "FR",
            allow_single_words=True,
            min_confidence=MIN_CONFIDENCE,
            min_margin=MIN_MARGIN,
        )
        assert decision.status == "fail"
        assert decision.detected == "en"

    def test_english_in_dutch_block_detected(self):
        """Test that English text in Dutch block is detected as mismatch."""
        text = "Hello, how are you today?"
        decision = check_translation_language(
            text,
            "NL",
            allow_single_words=True,
            min_confidence=MIN_CONFIDENCE,
            min_margin=MIN_MARGIN,
        )
        assert decision.status == "fail"
        assert decision.detected == "en"

    def test_single_word_allowed_in_english(self):
        """Test that single words are allowed to be in English."""
        text = "Debug"
        decision = check_translation_language(
            text,
            "FR",
            allow_single_words=True,
            min_confidence=MIN_CONFIDENCE,
            min_margin=MIN_MARGIN,
        )
        assert decision.status == "skip"
        assert decision.detected is None

    def test_single_word_not_allowed_when_disabled(self):
        """Test that single words are checked when allow_single_words=False."""
        text = "Debug"
        decision = check_translation_language(
            text,
            "FR",
            allow_single_words=False,
            min_confidence=MIN_CONFIDENCE,
            min_margin=MIN_MARGIN,
        )
        assert decision.status in {"fail", "uncertain"}
        assert decision.detected in {"en", "fr"}

    def test_empty_string_is_valid(self):
        """Test that empty strings are considered valid."""
        text = ""
        decision = check_translation_language(
            text,
            "FR",
            allow_single_words=True,
            min_confidence=MIN_CONFIDENCE,
            min_margin=MIN_MARGIN,
        )
        assert decision.status == "skip"
        assert decision.detected is None

    def test_whitespace_only_is_valid(self):
        """Test that whitespace-only strings are considered valid."""
        text = "   \n   "
        decision = check_translation_language(
            text,
            "FR",
            allow_single_words=True,
            min_confidence=MIN_CONFIDENCE,
            min_margin=MIN_MARGIN,
        )
        assert decision.status == "skip"
        assert decision.detected is None

    def test_html_stripped_before_detection(self):
        """Test that HTML is stripped before language detection."""
        text = "<p>Bonjour, comment <strong>allez-vous</strong> aujourd'hui?</p>"
        decision = check_translation_language(
            text,
            "FR",
            allow_single_words=True,
            min_confidence=MIN_CONFIDENCE,
            min_margin=MIN_MARGIN,
        )
        assert decision.status == "pass"
        assert decision.detected == "fr"

    def test_french_with_html_entities(self):
        """Test French text with HTML entities."""
        text = "Bonjour&nbsp;! Comment ça va&nbsp;?"
        decision = check_translation_language(
            text,
            "FR",
            allow_single_words=True,
            min_confidence=MIN_CONFIDENCE,
            min_margin=MIN_MARGIN,
        )
        assert decision.status in {"pass", "uncertain"}

    def test_multiword_english_in_french_detected(self):
        """Test that multi-word English text in French block is caught."""
        text = "Please enter your email address"
        decision = check_translation_language(
            text,
            "FR",
            allow_single_words=True,
            min_confidence=MIN_CONFIDENCE,
            min_margin=MIN_MARGIN,
        )
        assert decision.status == "fail"
        assert decision.detected == "en"

    def test_multiword_dutch_in_french_block_detected(self):
        """Test that Dutch text in French block is detected as mismatch."""
        text = "Voer alstublieft uw e-mailadres in"
        decision = check_translation_language(
            text,
            "FR",
            allow_single_words=True,
            min_confidence=MIN_CONFIDENCE,
            min_margin=MIN_MARGIN,
        )
        assert decision.status == "fail"
        assert decision.detected == "nl"


class TestLanguageDetectionEdgeCases:
    """Test edge cases for language detection."""

    def test_very_short_text_with_multiple_words(self):
        """Test very short text with 2-3 words."""
        text = "Bonjour madame"
        decision = check_translation_language(
            text,
            "FR",
            allow_single_words=True,
            min_confidence=MIN_CONFIDENCE,
            min_margin=MIN_MARGIN,
        )
        assert decision.status in {"pass", "uncertain"}
        assert decision.detected in {"fr", "en"}

    def test_mixed_language_text(self):
        """Test text with mixed languages (should detect primary language)."""
        # Mostly French with one English word
        text = "Bonjour, comment allez-vous aujourd'hui? Please note"
        decision = check_translation_language(
            text,
            "FR",
            allow_single_words=True,
            min_confidence=MIN_CONFIDENCE,
            min_margin=MIN_MARGIN,
        )
        assert decision.status in {"pass", "uncertain", "fail"}

    def test_numbers_and_punctuation_only(self):
        """Test strings with only numbers and punctuation."""
        text = "123 456 789 !@#"
        decision = check_translation_language(
            text,
            "FR",
            allow_single_words=True,
            min_confidence=MIN_CONFIDENCE,
            min_margin=MIN_MARGIN,
        )
        assert decision.status == "skip"
        assert decision.detected is None

    def test_technical_terms_in_translation(self):
        """Test translation with technical English terms."""
        # Dutch sentence with technical terms
        text = "Klik op de button om de API request te starten"
        decision = check_translation_language(
            text,
            "NL",
            allow_single_words=True,
            min_confidence=MIN_CONFIDENCE,
            min_margin=MIN_MARGIN,
        )
        assert decision.detected in {"nl", "en"}
