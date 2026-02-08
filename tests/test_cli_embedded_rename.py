import argparse
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch


class CliRenameEmbeddedFieldTests(unittest.TestCase):
    @patch("qsync.cli._prompt_for_survey_id_if_needed", return_value="SV_TEST")
    @patch("qsync.sync_core.stage_rename_embedded_field")
    @patch("qsync.cli_survey._merge_embedded_rename_pending")
    def test_handle_rename_embedded_field_dry_run(
        self,
        mock_merge_pending,
        mock_stage_rename,
        _mock_prompt,
    ) -> None:
        from qsync.cli_survey import handle_rename_embedded_field

        mock_stage_rename.return_value = [
            {"flow_id": "FL_1", "from_field": "OLD", "field": "NEW"}
        ]

        args = argparse.Namespace(
            survey_id="SV_TEST",
            from_field="OLD",
            to_field="NEW",
            flow_id=None,
            all_occurrences=False,
            dry_run=True,
        )

        buf = io.StringIO()
        with redirect_stdout(buf):
            handle_rename_embedded_field(args)

        out = buf.getvalue()
        self.assertIn("DRY RUN", out)
        self.assertIn("'OLD' -> 'NEW'", out)
        mock_merge_pending.assert_not_called()
        mock_stage_rename.assert_called_once()

    @patch("qsync.cli._prompt_for_survey_id_if_needed", return_value="SV_TEST")
    @patch("qsync.sync_core.stage_rename_embedded_field")
    @patch("qsync.cli_survey._merge_embedded_rename_pending")
    def test_handle_rename_embedded_field_stages_pending(
        self,
        mock_merge_pending,
        mock_stage_rename,
        _mock_prompt,
    ) -> None:
        from qsync.cli_survey import handle_rename_embedded_field

        mock_stage_rename.return_value = [
            {"flow_id": "FL_1", "from_field": "OLD", "field": "NEW"}
        ]

        args = argparse.Namespace(
            survey_id="SV_TEST",
            from_field="OLD",
            to_field="NEW",
            flow_id=None,
            all_occurrences=False,
            dry_run=False,
        )

        buf = io.StringIO()
        with redirect_stdout(buf):
            handle_rename_embedded_field(args)

        out = buf.getvalue()
        self.assertIn("Staged rename", out)
        self.assertIn("qsync push --survey-id SV_TEST", out)
        mock_stage_rename.assert_called_once()
        mock_merge_pending.assert_called_once_with(
            "SV_TEST",
            [{"flow_id": "FL_1", "from_field": "OLD", "field": "NEW"}],
        )


if __name__ == "__main__":
    unittest.main()
