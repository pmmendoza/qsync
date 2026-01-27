import unittest

from qsync import log_reader
from qsync.error_catalog import get_docs_url, get_suggestion, is_recoverable


class ErrorCatalogTests(unittest.TestCase):
    def test_status_suggestions_and_recoverable(self) -> None:
        self.assertIn("survey", get_suggestion(404))
        self.assertTrue(is_recoverable(500))
        self.assertFalse(is_recoverable(404))
        self.assertEqual(get_docs_url(), "appendices/logging_guide.md#troubleshooting")

    def test_exception_recoverable(self) -> None:
        self.assertTrue(is_recoverable(None, exc_type="Timeout"))
        self.assertFalse(is_recoverable(None, exc_type="ValueError"))


class LogReaderFormattingTests(unittest.TestCase):
    def test_format_log_entry_includes_error_context(self) -> None:
        entry = {
            "_log_line": 12,
            "timestamp": "2026-01-10T12:00:00Z",
            "action": "qsync.survey.delete",
            "method": "DELETE",
            "status": 404,
            "error": {
                "type": "HTTPError",
                "message": "404 Not Found",
                "detail": "Missing survey",
                "retry_count": 0,
                "recoverable": False,
                "suggestion": "Verify the survey ID or endpoint.",
                "docs_url": "appendices/logging_guide.md#troubleshooting",
            },
        }

        output = log_reader.format_log_entry(entry, detailed=True)
        self.assertIn("Suggestion:", output)
        self.assertIn("Docs:", output)
        self.assertIn("Retries:", output)
        self.assertIn("Recoverable:", output)


if __name__ == "__main__":
    unittest.main()
