from __future__ import annotations

import io
from contextlib import redirect_stdout
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

    mode, issue_type = orchestrator._parse_fix_selector("all")
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


def test_single_survey_view_issue_details_reachable_for_non_fixable_issues() -> None:
    import qsync.sync_orchestrator as orchestrator

    initial_changes = SimpleNamespace(
        survey_name="Survey One",
        dimensions=_empty_dimensions(),
    )
    non_fixable_unstaged = _empty_dimensions()
    non_fixable_unstaged["js"] = DimensionChanges(
        "js",
        False,
        "No changes",
        set(),
        error_detail="Mapping CSV missing a column for this survey.",
        safe_to_autofix=False,
        status_kind="error",
    )

    output = io.StringIO()
    with (
        redirect_stdout(output),
        patch.object(orchestrator, "detect_survey_changes", return_value=initial_changes),
        patch.object(orchestrator, "list_pending", return_value={}),
        patch.object(
            orchestrator,
            "_detect_unstaged_changes",
            return_value=non_fixable_unstaged,
        ),
        patch.object(orchestrator, "_display_survey_overview"),
        patch.object(orchestrator, "_get_inventory_cached", return_value={}),
        patch("qsync.interactive_menu.confirm", return_value=False),
        patch(
            "qsync.interactive_menu.select_from_list",
            side_effect=["📋 View issue details", "↩ Exit sync"],
        ),
        patch.object(orchestrator, "_run_autofix") as mock_run_autofix,
    ):
        result = orchestrator.sync_survey(
            survey_id="SV_TEST",
            dimensions=None,
            interactive=True,
            auto_yes=False,
        )

    assert result is None
    assert "Manual resolution required for: js" in output.getvalue()
    mock_run_autofix.assert_not_called()


def test_display_survey_overview_lists_fixable_and_manual_issue_actions() -> None:
    import qsync.sync_orchestrator as orchestrator

    unstaged = _empty_dimensions()
    unstaged["translations"] = DimensionChanges(
        "translations",
        False,
        "No changes",
        set(),
        warning_detail=(
            "Workbook not found at /tmp/workbook.xlsx. "
            "Run: qsync items pull --survey-id SV_TEST"
        ),
        safe_to_autofix=True,
    )
    unstaged["js"] = DimensionChanges(
        "js",
        False,
        "No changes",
        set(),
        error_detail="Mapping CSV missing a column for this survey.",
        safe_to_autofix=False,
        status_kind="error",
    )

    output = io.StringIO()
    with redirect_stdout(output):
        orchestrator._display_survey_overview(
            "SV_TEST",
            "Survey One (SV_TEST)",
            staged={dim: "none" for dim in _empty_dimensions()},
            unstaged=unstaged,
            has_pending=False,
        )

    text = output.getvalue()
    assert "Repair safe translations issues" in text
    assert "qsync items pull --survey-id SV_TEST" in text
    assert "Review issue details for manual resolution: js" in text


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
