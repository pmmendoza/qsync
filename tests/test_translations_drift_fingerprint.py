import unittest


from qsync import drift_check


class TestTranslationsDriftFingerprint(unittest.TestCase):
    def test_translation_fingerprint_detects_value_change(self):
        base_payload = {
            "result": {
                "SurveyOptions": {
                    "SurveyLanguage": "EN",
                    "AvailableLanguages": {"EN": True, "DE": True},
                },
                "Questions": {
                    "QID1": {
                        "Language": {
                            "DE": {
                                "QuestionText": "Hallo",
                                "Choices": {"1": {"Display": "A"}},
                            }
                        }
                    }
                },
            }
        }
        changed_payload = {
            "result": {
                "SurveyOptions": {
                    "SurveyLanguage": "EN",
                    "AvailableLanguages": {"EN": True, "DE": True},
                },
                "Questions": {
                    "QID1": {
                        "Language": {
                            "DE": {
                                "QuestionText": "Hallo Welt",
                                "Choices": {"1": {"Display": "A"}},
                            }
                        }
                    }
                },
            }
        }

        cached = drift_check._normalize_payload(base_payload)
        live = drift_check._normalize_payload(changed_payload)

        cached_lines = drift_check._translation_fingerprint_lines(
            cached, languages=None, qids=None
        )
        live_lines = drift_check._translation_fingerprint_lines(
            live, languages=None, qids=None
        )

        diff_lines = list(
            drift_check.difflib.unified_diff(
                cached_lines,
                live_lines,
                fromfile="cache",
                tofile="live",
                lineterm="",
            )
        )
        self.assertTrue(
            any(line.startswith("-QID1\tDE\tQuestionText") for line in diff_lines)
        )
        self.assertTrue(
            any(line.startswith("+QID1\tDE\tQuestionText") for line in diff_lines)
        )

    def test_translation_fingerprint_summary_counts(self):
        cached_lines = [
            "QID1\tDE\tQuestionText\t\t11111111",
            "QID1\tDE\tChoice\t1\t22222222",
            "QID2\tES\tQuestionText\t\t33333333",
            "SurveyOptions\t\tAvailableLanguages\t\t44444444",
        ]
        live_lines = [
            "QID1\tDE\tQuestionText\t\t99999999",
            "QID1\tDE\tChoice\t1\t22222222",
            "QID3\tFR\tQuestionText\t\t55555555",
            "SurveyOptions\t\tAvailableLanguages\t\t44444444",
        ]

        summary, changed_total, added, removed = (
            drift_check._summarize_translation_fingerprint_diff(
                cached_lines, live_lines
            )
        )
        self.assertEqual(changed_total, 3)  # 1 modified, 1 added, 1 removed
        self.assertEqual(added, 1)
        self.assertEqual(removed, 1)
        self.assertIn("3 key(s) changed", summary)
        self.assertIn(
            "across 3 QID(s)", summary
        )  # QID1/QID2/QID3 (SurveyOptions excluded)
        self.assertIn("3 language(s)", summary)  # DE/ES/FR


if __name__ == "__main__":
    unittest.main()
