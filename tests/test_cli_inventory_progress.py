import argparse
import unittest
from unittest.mock import patch


class CliInventoryProgressTests(unittest.TestCase):
    @patch("qsync.cli_survey.refresh_inventory", return_value=([], []))
    @patch(
        "qsync.cli_survey.get_client_config",
        return_value=("example.qualtrics.com", {"X-API-TOKEN": "x"}),
    )
    def test_full_inventory_defaults_to_progress(
        self,
        _mock_config,
        mock_refresh_inventory,
    ) -> None:
        from qsync.cli_survey import handle_inventory

        args = argparse.Namespace(
            quiet=False,
            progress=False,
            progress_only=False,
            survey_ids=None,
            dry_run=True,
            counts_scope=None,
        )

        with patch("builtins.print"):
            handle_inventory(args)

        self.assertTrue(mock_refresh_inventory.call_args.kwargs["progress"])

    @patch("qsync.cli_survey.refresh_inventory", return_value=([], []))
    @patch(
        "qsync.cli_survey.get_client_config",
        return_value=("example.qualtrics.com", {"X-API-TOKEN": "x"}),
    )
    def test_single_targeted_inventory_skips_auto_progress(
        self,
        _mock_config,
        mock_refresh_inventory,
    ) -> None:
        from qsync.cli_survey import handle_inventory

        args = argparse.Namespace(
            quiet=False,
            progress=False,
            progress_only=False,
            survey_ids=["SV_123"],
            dry_run=True,
            counts_scope=None,
        )

        with patch("builtins.print"):
            handle_inventory(args)

        self.assertFalse(mock_refresh_inventory.call_args.kwargs["progress"])

    @patch("qsync.cli_survey.refresh_inventory", return_value=([], []))
    @patch(
        "qsync.cli_survey.get_client_config",
        return_value=("example.qualtrics.com", {"X-API-TOKEN": "x"}),
    )
    def test_multi_targeted_inventory_enables_auto_progress(
        self,
        _mock_config,
        mock_refresh_inventory,
    ) -> None:
        from qsync.cli_survey import handle_inventory

        args = argparse.Namespace(
            quiet=False,
            progress=False,
            progress_only=False,
            survey_ids=["SV_123,SV_456"],
            dry_run=True,
            counts_scope=None,
        )

        with patch("builtins.print"):
            handle_inventory(args)

        self.assertTrue(mock_refresh_inventory.call_args.kwargs["progress"])


if __name__ == "__main__":
    unittest.main()
