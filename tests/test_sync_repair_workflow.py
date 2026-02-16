from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from qsync.dimensions.types import DimensionChanges


def _empty_dimensions() -> dict[str, DimensionChanges]:
    return {
        "items": DimensionChanges("items", False, "No changes", set()),
        "edf": DimensionChanges("edf", False, "No changes", set()),
        "js": DimensionChanges("js", False, "No changes", set()),
        "translations": DimensionChanges("translations", False, "No changes", set()),
        "eos": DimensionChanges("eos", False, "No changes", set()),
        "flow": DimensionChanges("flow", False, "No changes", set()),
        "master": DimensionChanges("master", False, "No changes", set()),
    }


def test_parse_fix_selector_and_type_filter() -> None:
    import qsync.sync_orchestrator as orchestrator

    mode, issue_type = orchestrator._parse_fix_selector("safe")
    assert mode == "safe"
    assert issue_type is None

    mode, issue_type = orchestrator._parse_fix_selector("type:flow_not_initialized")
    assert mode == "type"
    assert issue_type == "FLOW_NOT_INITIALIZED"


def test_single_survey_repair_menu_reachable_when_only_issues() -> None:
    import qsync.sync_orchestrator as orchestrator

    initial_changes = SimpleNamespace(
        survey_name="Survey One",
        dimensions=_empty_dimensions(),
    )
    fixable_unstaged = _empty_dimensions()
    fixable_unstaged["flow"] = DimensionChanges(
        "flow",
        False,
        "Not initialized",
        set(),
        error_detail="Run: qsync flow pull --survey-id SV_TEST",
        safe_to_autofix=True,
        status_kind="error",
    )

    with (
        patch.object(orchestrator, "detect_survey_changes", return_value=initial_changes),
        patch.object(orchestrator, "list_pending", return_value={}),
        patch.object(
            orchestrator,
            "_detect_unstaged_changes",
            side_effect=[fixable_unstaged, _empty_dimensions()],
        ),
        patch.object(orchestrator, "_display_survey_overview"),
        patch.object(orchestrator, "_get_inventory_cached", return_value={}),
        patch("qsync.interactive_menu.confirm", return_value=True),
        patch(
            "qsync.interactive_menu.select_from_list",
            side_effect=["🔧 Repair issues", "Fix all safe issues"],
        ),
        patch.object(orchestrator, "_run_autofix", return_value="ok") as mock_run_autofix,
    ):
        result = orchestrator.sync_survey(
            survey_id="SV_TEST",
            dimensions=None,
            interactive=True,
            auto_yes=False,
        )

    assert result is None
    mock_run_autofix.assert_called_once_with("flow", "SV_TEST")


def test_focal_noninteractive_fix_type_repairs_only_matching_type() -> None:
    import qsync.sync_orchestrator as orchestrator

    flow_issue = orchestrator.SurveyChanges(
        survey_id="SV_FLOW",
        survey_name="Flow Survey",
        dimensions={
            **_empty_dimensions(),
            "flow": DimensionChanges(
                "flow",
                False,
                "Not initialized",
                set(),
                error_detail="Run: qsync flow pull --survey-id SV_FLOW",
                safe_to_autofix=True,
                status_kind="error",
            ),
        },
    )
    translation_issue = orchestrator.SurveyChanges(
        survey_id="SV_TR",
        survey_name="Trans Survey",
        dimensions={
            **_empty_dimensions(),
            "translations": DimensionChanges(
                "translations",
                False,
                "No changes",
                set(),
                warning_detail=(
                    "Workbook not found at /tmp/workbook.xlsx. "
                    "Run: qsync items pull --survey-id SV_TR"
                ),
                safe_to_autofix=True,
                status_kind="none",
            ),
        },
    )
    by_id = {"SV_FLOW": flow_issue, "SV_TR": translation_issue}

    with (
        patch.object(orchestrator, "get_focal_survey_ids", return_value=["SV_FLOW", "SV_TR"]),
        patch.object(
            orchestrator, "detect_survey_changes", side_effect=lambda sid: by_id[sid]
        ),
        patch.object(orchestrator, "_get_inventory_cached", return_value={"lastModified": "z"}),
        patch.object(orchestrator, "display_change_detection_table"),
        patch.object(orchestrator, "display_sync_summary_table"),
        patch.object(orchestrator, "display_recovery_instructions"),
        patch.object(orchestrator, "sync_survey", return_value=None),
        patch.object(orchestrator, "_run_autofix", return_value="ok") as mock_run_autofix,
    ):
        result = orchestrator.sync_focal_surveys(
            interactive=False,
            auto_yes=True,
            fix="type:FLOW_NOT_INITIALIZED",
        )

    assert result is True
    mock_run_autofix.assert_called_once_with("flow", "SV_FLOW")
