from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from qsync.dimensions.types import DimensionChanges


def _no_change(dimension: str) -> DimensionChanges:
    return DimensionChanges(
        dimension=dimension,
        has_changes=False,
        change_summary="No changes",
        affected_qids=set(),
    )


def _survey_with_translation_warning(idx: int):
    import qsync.sync_orchestrator as orchestrator

    survey_id = f"SV_WARN_{idx:02d}"
    survey_name = f"WarnSurvey_{idx:02d}"
    warning = (
        f"Workbook not found at /tmp/workbook_{idx:02d}.xlsx. "
        f"Run: qsync items pull --survey-id {survey_id}"
    )
    dimensions = {
        "items": _no_change("items"),
        "edf": _no_change("edf"),
        "js": _no_change("js"),
        "translations": DimensionChanges(
            dimension="translations",
            has_changes=False,
            change_summary="No changes",
            affected_qids=set(),
            warning_detail=warning,
            safe_to_autofix=True,
        ),
        "eos": _no_change("eos"),
        "flow": _no_change("flow"),
        "master": _no_change("master"),
    }
    return orchestrator.SurveyChanges(
        survey_id=survey_id,
        survey_name=survey_name,
        dimensions=dimensions,
    )


class TestSyncChangeDetectionDisplay(unittest.TestCase):
    def setUp(self) -> None:
        import qsync.sync_orchestrator as orchestrator

        orchestrator._ISSUE_KEYS_SEEN.clear()

    def test_long_warning_list_can_be_hidden_behind_menu(self):
        import qsync.sync_orchestrator as orchestrator

        all_changes = [_survey_with_translation_warning(i) for i in range(11)]
        buf = io.StringIO()

        with (
            redirect_stdout(buf),
            patch(
                "qsync.interactive_menu.select_from_list",
                return_value="Continue without warnings",
            ) as mock_select,
        ):
            orchestrator.display_change_detection_table(
                all_changes,
                show_all=True,
                interactive=True,
                issue_detail_threshold=10,
            )

        output = buf.getvalue()
        self.assertIn("⚠️  Warnings:", output)
        self.assertIn("11 warnings hidden. Continue to survey selection.", output)
        self.assertNotIn("Workbook not found at /tmp/workbook_", output)
        mock_select.assert_called_once()

    def test_long_warning_list_can_show_first_threshold_entries(self):
        import qsync.sync_orchestrator as orchestrator

        all_changes = [_survey_with_translation_warning(i) for i in range(11)]
        buf = io.StringIO()

        with (
            redirect_stdout(buf),
            patch(
                "qsync.interactive_menu.select_from_list",
                return_value="Show first 10 warnings",
            ) as mock_select,
        ):
            orchestrator.display_change_detection_table(
                all_changes,
                show_all=True,
                interactive=True,
                issue_detail_threshold=10,
            )

        output = buf.getvalue()
        self.assertIn("⚠️  Warnings:", output)
        self.assertEqual(output.count("Workbook not found at /tmp/workbook_"), 10)
        self.assertIn("… 1 more warnings hidden", output)
        mock_select.assert_called_once()

    def test_non_interactive_still_prints_all_warning_details(self):
        import qsync.sync_orchestrator as orchestrator

        all_changes = [_survey_with_translation_warning(i) for i in range(11)]
        buf = io.StringIO()

        with (
            redirect_stdout(buf),
            patch("qsync.interactive_menu.select_from_list") as mock_select,
        ):
            orchestrator.display_change_detection_table(
                all_changes,
                show_all=True,
                interactive=False,
                issue_detail_threshold=10,
            )

        output = buf.getvalue()
        self.assertIn("⚠️  Warnings:", output)
        self.assertEqual(output.count("Workbook not found at /tmp/workbook_"), 11)
        mock_select.assert_not_called()


if __name__ == "__main__":
    unittest.main()
