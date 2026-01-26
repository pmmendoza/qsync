import unittest

from qsync import cli_translations_check


class TestTranslationsCheckLanguage(unittest.TestCase):
    def test_strip_html_and_unescape(self):
        raw = "<strong>Inputs</strong><br><br> Voeg \\u00e9\\u00e9n detail toe\\u2026"
        normalized = cli_translations_check._strip_formatting_to_plain_text(raw)
        self.assertNotIn("<", normalized)
        self.assertIn("Inputs", normalized)
        self.assertIn("é", normalized)
        self.assertIn("…", normalized)
        self.assertNotIn("-", normalized)

    def test_numeric_only_with_currency(self):
        self.assertTrue(cli_translations_check._is_numeric_only("17 701–22 400 Kč"))

    def test_single_word_is_skipped(self):
        decision = cli_translations_check.check_translation_language(
            "Bonjour",
            "FR",
            allow_single_words=True,
            min_confidence=0.85,
            min_margin=0.15,
        )
        self.assertEqual(decision.status, "skip")

    def test_placeholder_is_skipped(self):
        decision = cli_translations_check.check_translation_language(
            "Click to write the question text",
            "FR",
            allow_single_words=False,
            min_confidence=0.85,
            min_margin=0.15,
        )
        self.assertEqual(decision.status, "skip")

    def test_gender_parens_normalization(self):
        text = "Extraverti(e), enthousiaste."
        normalized = cli_translations_check._strip_formatting_to_plain_text(text)
        self.assertIn("Extraverti e", normalized)

    def test_binary_hypothesis_passes_expected_language(self):
        decision = cli_translations_check.check_translation_language(
            "This is a longer English sentence used for language detection accuracy.",
            "EN",
            allow_single_words=False,
            min_confidence=0.5,
            min_margin=0.1,
        )
        self.assertEqual(decision.status, "pass")

    def test_binary_hypothesis_fails_wrong_language(self):
        decision = cli_translations_check.check_translation_language(
            "This is a longer English sentence used for language detection accuracy.",
            "FR",
            allow_single_words=False,
            min_confidence=0.5,
            min_margin=0.1,
        )
        self.assertIn(decision.status, {"fail", "uncertain"})


if __name__ == "__main__":
    unittest.main()
