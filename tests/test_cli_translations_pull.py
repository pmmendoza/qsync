import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class CliTranslationsPullTests(unittest.TestCase):
    @patch("qsync.qualtrics_client.refresh_survey_cache")
    def test_translations_pull_refreshes_cache(self, mock_refresh) -> None:
        cache = MagicMock()
        cache.path = Path("surveys/Test__SV_TEST.json")
        mock_refresh.return_value = (cache, True)

        from qsync.cli_survey import handle_translations_pull

        args = MagicMock()
        args.survey_id = "SV_TEST"
        args.language = None
        args.languages = None

        handle_translations_pull(args)

        mock_refresh.assert_called_once_with("SV_TEST")


if __name__ == "__main__":
    unittest.main()
