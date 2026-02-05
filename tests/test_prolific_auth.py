import unittest


class ProlificAuthTests(unittest.TestCase):
    def test_validate_ok_for_expected_script(self) -> None:
        from qsync.prolific_auth import validate_prolific_auth_snippet

        snippet = (
            '<script src="https://assets.prolific.com/assets/js/qualtrics/qualtrics.min.js'
            '?rid=${e://Field/ResponseID}&t=TOKEN"></script>'
        )
        result = validate_prolific_auth_snippet(snippet)
        self.assertTrue(result.ok)

    def test_redacts_t_token(self) -> None:
        from qsync.prolific_auth import redact_prolific_token

        snippet = (
            '<script src="https://assets.prolific.com/assets/js/qualtrics/qualtrics.min.js'
            '?rid=${e://Field/ResponseID}&t=SECRET123"></script>'
        )
        redacted = redact_prolific_token(snippet)
        self.assertIn("t=[REDACTED]", redacted)
        self.assertNotIn("SECRET123", redacted)


if __name__ == "__main__":
    unittest.main()

