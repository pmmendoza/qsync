from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from qsync.dimensions.types import DimensionChanges


def _survey_changes(
    *,
    survey_id: str,
    survey_name: str,
    items_changed: bool,
    edf_warning: str | None = None,
    translations_error: str | None = None,
    edf_fixable: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        survey_id=survey_id,
        survey_name=survey_name,
        dimensions={
            "items": DimensionChanges(
                "items",
                items_changed,
                "⚡ Unstaged: 1 change" if items_changed else "No changes",
                {"QID1"} if items_changed else set(),
            ),
            "edf": DimensionChanges(
                "edf",
                False,
                "No changes",
                set(),
                warning_detail=edf_warning,
                safe_to_autofix=edf_fixable,
            ),
            "js": DimensionChanges("js", False, "No changes", set()),
            "translations": DimensionChanges(
                "translations",
                False,
                "✗ Error" if translations_error else "No changes",
                set(),
                error_detail=translations_error,
            ),
            "eos": DimensionChanges("eos", False, "No changes", set()),
        },
        has_any_changes=items_changed,
        changed_dimensions=["items"] if items_changed else [],
        has_any_issues=bool(edf_warning or translations_error),
    )


class TestSyncFocalMenuFlow(unittest.TestCase):
    def test_fix_selection_continues_to_sync_selected_survey(self):
        import qsync.sync_orchestrator as orchestrator

        survey_id = "SV_FIX"
        survey_name = "BSKY_main_post"
        fix_warning = "Embedded_Data worksheet is inconsistent."
        fix_choice = (
            "fix BSKY_main_post " "(⚠ edf: Embedded_Data worksheet is inconsistent)"
        )

        initial = _survey_changes(
            survey_id=survey_id,
            survey_name=survey_name,
            items_changed=False,
            edf_warning=fix_warning,
            edf_fixable=True,
        )
        post_fix = _survey_changes(
            survey_id=survey_id,
            survey_name=survey_name,
            items_changed=True,
        )

        with (
            patch.object(
                orchestrator, "get_focal_survey_ids", return_value=[survey_id]
            ),
            patch.object(
                orchestrator, "detect_survey_changes", side_effect=[initial, post_fix]
            ) as mock_detect,
            patch.object(
                orchestrator,
                "_get_inventory_cached",
                return_value={"lastModified": "z"},
            ),
            patch("qsync.rich_support.should_use_rich", return_value=False),
            patch.object(orchestrator, "display_change_detection_table"),
            patch.object(orchestrator, "display_sync_summary_table"),
            patch.object(orchestrator, "display_recovery_instructions"),
            patch("qsync.interactive_menu.select_from_list", return_value=fix_choice),
            patch("qsync.interactive_menu.confirm", return_value=True),
            patch.object(orchestrator, "_run_autofix", return_value="ok"),
            patch.object(
                orchestrator,
                "sync_survey",
                return_value=SimpleNamespace(success=True),
            ) as mock_sync_survey,
        ):
            result = orchestrator.sync_focal_surveys(
                interactive=True,
                auto_yes=False,
            )

        self.assertTrue(result)
        self.assertEqual(mock_detect.call_count, 2)
        mock_sync_survey.assert_called_once()
        self.assertEqual(mock_sync_survey.call_args.args[0], survey_id)

    def test_issues_selection_reports_and_exits_without_recursive_restart(self):
        import qsync.sync_orchestrator as orchestrator

        survey_id = "SV_ISSUE"
        survey_name = "BSKY_main_payout_15"
        issue_detail = "Translations detection failed"
        issue_choice = "issues BSKY_main_payout_15 (translations)"

        issue_only = _survey_changes(
            survey_id=survey_id,
            survey_name=survey_name,
            items_changed=False,
            translations_error=issue_detail,
        )

        with (
            patch.object(
                orchestrator, "get_focal_survey_ids", return_value=[survey_id]
            ),
            patch.object(
                orchestrator, "detect_survey_changes", return_value=issue_only
            ) as mock_detect,
            patch.object(
                orchestrator,
                "_get_inventory_cached",
                return_value={"lastModified": "z"},
            ),
            patch("qsync.rich_support.should_use_rich", return_value=False),
            patch.object(orchestrator, "display_change_detection_table"),
            patch("qsync.interactive_menu.select_from_list", return_value=issue_choice),
            patch.object(orchestrator, "sync_survey") as mock_sync_survey,
        ):
            result = orchestrator.sync_focal_surveys(
                interactive=True,
                auto_yes=False,
            )

        self.assertTrue(result)
        self.assertEqual(mock_detect.call_count, 1)
        self.assertFalse(mock_sync_survey.called)


if __name__ == "__main__":
    unittest.main()
