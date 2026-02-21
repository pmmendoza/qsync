from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pytest


def test_embedded_options_menu_includes_structural_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from qsync import interactive_menu
    from qsync.cli_survey import handle_menu

    monkeypatch.setattr("qsync.cli_survey._workspace_root", lambda: tmp_path.resolve())
    monkeypatch.setattr(interactive_menu, "is_interactive", lambda: True)
    monkeypatch.setattr(interactive_menu, "confirm", lambda *args, **kwargs: False)

    seen_edit_choices: list[list[str]] = []
    steps = iter(["Edit Questions & Content — structural item edits", "↩ Back", "Exit"])

    def _select_from_list(message: str, choices, instruction=None, default=None):
        normalized = [str(c) for c in choices]
        if message == "Edit Questions & Content":
            seen_edit_choices.append(normalized)
        return next(steps)

    monkeypatch.setattr(interactive_menu, "select_from_list", _select_from_list)

    handle_menu(argparse.Namespace(tui=False))

    assert seen_edit_choices, "Edit Questions & Content menu was not shown."
    assert any(
        "Items: structural edits (stage → preview → push)" in menu_choices
        for menu_choices in seen_edit_choices
    )
    assert any(
        "Add question(s) (clone template, insert in flow)" in menu_choices
        for menu_choices in seen_edit_choices
    )
    assert any(
        "Move question(s) (reorder / move across blocks)" in menu_choices
        for menu_choices in seen_edit_choices
    )
    assert any(
        "Remove question(s) (move selected QIDs to Trash)" in menu_choices
        for menu_choices in seen_edit_choices
    )
    assert any(
        "Replace question payload (source survey/QID → target QID)" in menu_choices
        for menu_choices in seen_edit_choices
    )
    assert any(
        "Page breaks (add/remove in block flow)" in menu_choices
        for menu_choices in seen_edit_choices
    )


def test_edit_menu_add_question_routes_to_handler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from qsync import interactive_menu
    from qsync.cli_survey import handle_menu

    monkeypatch.setattr("qsync.cli_survey._workspace_root", lambda: tmp_path.resolve())
    monkeypatch.setattr(interactive_menu, "is_interactive", lambda: True)
    monkeypatch.setattr(interactive_menu, "confirm", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        "qsync.cli_survey.get_client_config",
        lambda env=None: ("example.qualtrics.com", {"X-API-TOKEN": "token"}),
    )
    monkeypatch.setattr(
        "qsync.cli_survey.list_surveys",
        lambda base, headers: [{"id": "SV_1", "name": "Survey One"}],
    )
    monkeypatch.setattr(
        "qsync.cli_survey._pick_survey_id_from_records",
        lambda **kwargs: "SV_1",
    )
    monkeypatch.setattr(
        "qsync.cli_survey.fetch_survey_definition",
        lambda base, headers, survey_id: {
            "Questions": {"QID1": {"QuestionText": "Intro"}},
            "Blocks": {"BL_1": {"Type": "Standard", "BlockElements": []}},
            "SurveyFlow": {"Flow": [{"Type": "Block", "ID": "BL_1"}]},
        },
    )

    called = {}

    def _capture(ns):
        called["args"] = ns

    monkeypatch.setattr("qsync.cli_survey.handle_add_question", _capture)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")

    state = {"top_visits": 0}

    def _select_from_list(message: str, choices, instruction=None, default=None):
        if message.startswith("qsync survey menu"):
            state["top_visits"] += 1
            return (
                "Edit Questions & Content — structural item edits"
                if state["top_visits"] == 1
                else "Exit"
            )
        if message == "Edit Questions & Content":
            return "Add question(s) (clone template, insert in flow)"
        if message == "Question template source:":
            return "Clone existing question(s) from question bank"
        if message == "Clone source account:":
            return "Current account (default)"
        if message == "Question bank lookup mode:":
            return "Live survey definitions (default)"
        if message == "Question text behavior:":
            return "Keep source question text(s)"
        if message == "Choose target block:":
            return str(choices[0])
        if message == "Choose where to insert new question(s) in the selected block:":
            return "slot:0"
        if message == "Page break handling for inserted question(s):":
            return "No extra page breaks"
        if message == "Dry run?":
            return "Yes"
        return "Exit"

    monkeypatch.setattr(interactive_menu, "select_from_list", _select_from_list)

    handle_menu(argparse.Namespace(tui=False))

    assert "args" in called
    ns = called["args"]
    assert ns.survey_id == "SV_1"
    assert ns.from_question_id is None
    assert ns.source_survey_id == "SV_1"
    assert ns.source_question_id == ["QID1"]
    assert ns.target_block_id == "BL_1"
    assert ns.insert_index == 0
    assert ns.after_qid is None
    assert ns.page_break_mode == "none"
    assert ns.dry_run is True


def test_edit_menu_replace_question_routes_to_handler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from qsync import interactive_menu
    from qsync.cli_survey import handle_menu

    monkeypatch.setattr("qsync.cli_survey._workspace_root", lambda: tmp_path.resolve())
    monkeypatch.setattr(interactive_menu, "is_interactive", lambda: True)
    monkeypatch.setattr(interactive_menu, "confirm", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        "qsync.cli_survey.get_client_config",
        lambda env=None: ("example.qualtrics.com", {"X-API-TOKEN": "token"}),
    )
    monkeypatch.setattr(
        "qsync.cli_survey.list_surveys",
        lambda base, headers: [{"id": "SV_1", "name": "Survey One"}],
    )
    monkeypatch.setattr(
        "qsync.cli_survey._pick_survey_id_from_records",
        lambda **kwargs: "SV_1",
    )
    monkeypatch.setattr(
        "qsync.cli_survey.fetch_survey_definition",
        lambda base, headers, survey_id: {
            "Questions": {"QID1": {"QuestionText": "Only question"}},
            "Blocks": {"BL_1": {"Type": "Standard", "BlockElements": []}},
            "SurveyFlow": {"Flow": [{"Type": "Block", "ID": "BL_1"}]},
        },
    )

    called = {}

    def _capture(ns):
        called["args"] = ns

    monkeypatch.setattr("qsync.cli_survey.handle_replace_question", _capture)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")

    state = {"top_visits": 0}

    def _select_from_list(message: str, choices, instruction=None, default=None):
        if message.startswith("qsync survey menu"):
            state["top_visits"] += 1
            return (
                "Edit Questions & Content — structural item edits"
                if state["top_visits"] == 1
                else "Exit"
            )
        if message == "Edit Questions & Content":
            return "Replace question payload (source survey/QID → target QID)"
        if message == "Replacement source account:":
            return "Current account (default)"
        if message == "Replacement source survey:":
            return "Use same survey (SV_1)"
        if message == "Replace target DataExportTag with source tag?":
            return "No (keep target DataExportTag)"
        if message == "Dry run?":
            return "Yes"
        return str(choices[0]) if choices else "Exit"

    monkeypatch.setattr(interactive_menu, "select_from_list", _select_from_list)

    handle_menu(argparse.Namespace(tui=False))

    assert "args" in called
    ns = called["args"]
    assert ns.survey_id == "SV_1"
    assert ns.question_id == "QID1"
    assert ns.source_survey_id == "SV_1"
    assert ns.source_question_id == "QID1"
    assert ns.replace_data_export_tag is False
    assert ns.dry_run is True


def test_edit_menu_add_question_reprompts_when_no_source_questions_selected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from qsync import interactive_menu
    from qsync.cli_survey import handle_menu

    monkeypatch.setattr("qsync.cli_survey._workspace_root", lambda: tmp_path.resolve())
    monkeypatch.setattr(interactive_menu, "is_interactive", lambda: True)
    monkeypatch.setattr(interactive_menu, "confirm", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        "qsync.cli_survey.get_client_config",
        lambda env=None: ("example.qualtrics.com", {"X-API-TOKEN": "token"}),
    )
    monkeypatch.setattr(
        "qsync.cli_survey.list_surveys",
        lambda base, headers: [{"id": "SV_1", "name": "Survey One"}],
    )
    monkeypatch.setattr(
        "qsync.cli_survey._pick_survey_id_from_records",
        lambda **kwargs: "SV_1",
    )
    monkeypatch.setattr(
        "qsync.cli_survey.fetch_survey_definition",
        lambda base, headers, survey_id: {
            "Questions": {
                "QID1": {"QuestionText": "First"},
                "QID2": {"QuestionText": "Second"},
            },
            "Blocks": {"BL_1": {"Type": "Standard", "BlockElements": []}},
            "SurveyFlow": {"Flow": [{"Type": "Block", "ID": "BL_1"}]},
        },
    )

    called = {}
    multi_calls = {"count": 0}

    def _capture(ns):
        called["args"] = ns

    def _multi_select(message: str, choices, instruction=None, default=None):
        multi_calls["count"] += 1
        if multi_calls["count"] == 1:
            return []
        return [str(choices[0])]

    monkeypatch.setattr("qsync.cli_survey.handle_add_question", _capture)
    monkeypatch.setattr(interactive_menu, "multi_select_from_list", _multi_select)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")

    state = {"top_visits": 0}

    def _select_from_list(message: str, choices, instruction=None, default=None):
        if message.startswith("qsync survey menu"):
            state["top_visits"] += 1
            return (
                "Edit Questions & Content — structural item edits"
                if state["top_visits"] == 1
                else "Exit"
            )
        if message == "Edit Questions & Content":
            return "Add question(s) (clone template, insert in flow)"
        if message == "Question template source:":
            return "Clone existing question(s) from question bank"
        if message == "Clone source account:":
            return "Current account (default)"
        if message == "Question bank lookup mode:":
            return "Live survey definitions (default)"
        if message == "No source questions selected.":
            return "Choose source question(s) again"
        if message == "Question text behavior:":
            return "Keep source question text(s)"
        if message == "Choose target block:":
            return str(choices[0])
        if message == "Choose where to insert new question(s) in the selected block:":
            return "slot:0"
        if message == "Page break handling for inserted question(s):":
            return "No extra page breaks"
        if message == "Dry run?":
            return "Yes"
        return "Exit"

    monkeypatch.setattr(interactive_menu, "select_from_list", _select_from_list)

    handle_menu(argparse.Namespace(tui=False))

    assert multi_calls["count"] == 2
    assert "args" in called
    ns = called["args"]
    assert ns.source_question_id == ["QID1"]
    assert ns.dry_run is True


def test_edit_menu_move_question_routes_to_handler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from qsync import interactive_menu
    from qsync.cli_survey import handle_menu

    monkeypatch.setattr("qsync.cli_survey._workspace_root", lambda: tmp_path.resolve())
    monkeypatch.setattr(interactive_menu, "is_interactive", lambda: True)
    monkeypatch.setattr(interactive_menu, "confirm", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        "qsync.cli_survey.get_client_config",
        lambda env=None: ("example.qualtrics.com", {"X-API-TOKEN": "token"}),
    )
    monkeypatch.setattr(
        "qsync.cli_survey.list_surveys",
        lambda base, headers: [{"id": "SV_1", "name": "Survey One"}],
    )
    monkeypatch.setattr(
        "qsync.cli_survey._pick_survey_id_from_records",
        lambda **kwargs: "SV_1",
    )
    monkeypatch.setattr(
        "qsync.cli_survey.fetch_survey_definition",
        lambda base, headers, survey_id: {
            "Questions": {"QID1": {"QuestionText": "Only question"}},
            "Blocks": {"BL_1": {"Type": "Standard", "BlockElements": []}},
            "SurveyFlow": {"Flow": [{"Type": "Block", "ID": "BL_1"}]},
        },
    )

    called = {}

    def _capture(ns):
        called["args"] = ns

    monkeypatch.setattr("qsync.cli_survey.handle_move_question", _capture)

    state = {"top_visits": 0}

    def _select_from_list(message: str, choices, instruction=None, default=None):
        if message.startswith("qsync survey menu"):
            state["top_visits"] += 1
            return (
                "Edit Questions & Content — structural item edits"
                if state["top_visits"] == 1
                else "Exit"
            )
        if message == "Edit Questions & Content":
            return "Move question(s) (reorder / move across blocks)"
        if message == "Choose target block:":
            return str(choices[0])
        if message == "Choose where to move selected question(s) in the target block:":
            return "slot:0"
        if message == "Dry run?":
            return "Yes"
        return "Exit"

    monkeypatch.setattr(interactive_menu, "select_from_list", _select_from_list)

    handle_menu(argparse.Namespace(tui=False))

    assert "args" in called
    ns = called["args"]
    assert ns.survey_id == "SV_1"
    assert ns.question_id == ["QID1"]
    assert ns.target_block_id == "BL_1"
    assert ns.insert_index == 0
    assert ns.position == "append"
    assert ns.dry_run is True


def test_edit_menu_move_question_reprompts_when_no_questions_selected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from qsync import interactive_menu
    from qsync.cli_survey import handle_menu

    monkeypatch.setattr("qsync.cli_survey._workspace_root", lambda: tmp_path.resolve())
    monkeypatch.setattr(interactive_menu, "is_interactive", lambda: True)
    monkeypatch.setattr(interactive_menu, "confirm", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        "qsync.cli_survey.get_client_config",
        lambda env=None: ("example.qualtrics.com", {"X-API-TOKEN": "token"}),
    )
    monkeypatch.setattr(
        "qsync.cli_survey.list_surveys",
        lambda base, headers: [{"id": "SV_1", "name": "Survey One"}],
    )
    monkeypatch.setattr(
        "qsync.cli_survey._pick_survey_id_from_records",
        lambda **kwargs: "SV_1",
    )
    monkeypatch.setattr(
        "qsync.cli_survey.fetch_survey_definition",
        lambda base, headers, survey_id: {
            "Questions": {
                "QID1": {"QuestionText": "First question"},
                "QID2": {"QuestionText": "Second question"},
            },
            "Blocks": {"BL_1": {"Type": "Standard", "BlockElements": []}},
            "SurveyFlow": {"Flow": [{"Type": "Block", "ID": "BL_1"}]},
        },
    )

    called = {}
    multi_calls = {"count": 0}

    def _capture(ns):
        called["args"] = ns

    def _multi_select(message: str, choices, instruction=None, default=None):
        multi_calls["count"] += 1
        if multi_calls["count"] == 1:
            return []
        return [str(choices[0])]

    monkeypatch.setattr(interactive_menu, "multi_select_from_list", _multi_select)
    monkeypatch.setattr("qsync.cli_survey.handle_move_question", _capture)

    state = {"top_visits": 0}

    def _select_from_list(message: str, choices, instruction=None, default=None):
        if message.startswith("qsync survey menu"):
            state["top_visits"] += 1
            return (
                "Edit Questions & Content — structural item edits"
                if state["top_visits"] == 1
                else "Exit"
            )
        if message == "Edit Questions & Content":
            return "Move question(s) (reorder / move across blocks)"
        if message == "No questions selected to move.":
            return "Choose question(s) again"
        if message == "Choose target block:":
            return str(choices[0])
        if message == "Choose where to move selected question(s) in the target block:":
            return "slot:0"
        if message == "Dry run?":
            return "Yes"
        return "Exit"

    monkeypatch.setattr(interactive_menu, "select_from_list", _select_from_list)

    handle_menu(argparse.Namespace(tui=False))

    assert multi_calls["count"] == 2
    assert "args" in called
    ns = called["args"]
    assert ns.survey_id == "SV_1"
    assert ns.question_id == ["QID1"]
    assert ns.target_block_id == "BL_1"
    assert ns.insert_index == 0
    assert ns.position == "append"
    assert ns.dry_run is True


def test_edit_menu_remove_question_routes_to_handler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from qsync import interactive_menu
    from qsync.cli_survey import handle_menu

    monkeypatch.setattr("qsync.cli_survey._workspace_root", lambda: tmp_path.resolve())
    monkeypatch.setattr(interactive_menu, "is_interactive", lambda: True)
    monkeypatch.setattr(interactive_menu, "confirm", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        "qsync.cli_survey.get_client_config",
        lambda env=None: ("example.qualtrics.com", {"X-API-TOKEN": "token"}),
    )
    monkeypatch.setattr(
        "qsync.cli_survey.list_surveys",
        lambda base, headers: [{"id": "SV_1", "name": "Survey One"}],
    )
    monkeypatch.setattr(
        "qsync.cli_survey._pick_survey_id_from_records",
        lambda **kwargs: "SV_1",
    )
    monkeypatch.setattr(
        "qsync.cli_survey.fetch_survey_definition",
        lambda base, headers, survey_id: {
            "Questions": {"QID1": {"QuestionText": "Only question"}},
            "Blocks": {"BL_1": {"Type": "Standard", "BlockElements": []}},
            "SurveyFlow": {"Flow": [{"Type": "Block", "ID": "BL_1"}]},
        },
    )

    called = {}

    def _capture(ns):
        called["args"] = ns

    monkeypatch.setattr("qsync.cli_survey.handle_remove_question", _capture)

    state = {"top_visits": 0}

    def _select_from_list(message: str, choices, instruction=None, default=None):
        if message.startswith("qsync survey menu"):
            state["top_visits"] += 1
            return (
                "Edit Questions & Content — structural item edits"
                if state["top_visits"] == 1
                else "Exit"
            )
        if message == "Edit Questions & Content":
            return "Remove question(s) (move selected QIDs to Trash)"
        if message == "Dry run?":
            return "Yes"
        return "Exit"

    monkeypatch.setattr(interactive_menu, "select_from_list", _select_from_list)

    handle_menu(argparse.Namespace(tui=False))

    assert "args" in called
    ns = called["args"]
    assert ns.survey_id == "SV_1"
    assert ns.question_id == ["QID1"]
    assert ns.dry_run is True


def test_edit_menu_add_page_break_routes_to_handler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from qsync import interactive_menu
    from qsync.cli_survey import handle_menu

    monkeypatch.setattr("qsync.cli_survey._workspace_root", lambda: tmp_path.resolve())
    monkeypatch.setattr(interactive_menu, "is_interactive", lambda: True)
    monkeypatch.setattr(interactive_menu, "confirm", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        "qsync.cli_survey.get_client_config",
        lambda env=None: ("example.qualtrics.com", {"X-API-TOKEN": "token"}),
    )
    monkeypatch.setattr(
        "qsync.cli_survey.list_surveys",
        lambda base, headers: [{"id": "SV_1", "name": "Survey One"}],
    )
    monkeypatch.setattr(
        "qsync.cli_survey._pick_survey_id_from_records",
        lambda **kwargs: "SV_1",
    )
    monkeypatch.setattr(
        "qsync.cli_survey.fetch_survey_definition",
        lambda base, headers, survey_id: {
            "Questions": {
                "QID1": {"QuestionText": "Q1"},
                "QID2": {"QuestionText": "Q2"},
            },
            "Blocks": {
                "BL_1": {
                    "Type": "Standard",
                    "BlockElements": [
                        {"Type": "Question", "QuestionID": "QID1"},
                        {"Type": "Question", "QuestionID": "QID2"},
                    ],
                }
            },
            "SurveyFlow": {"Flow": [{"Type": "Block", "ID": "BL_1"}]},
        },
    )

    called = {}

    def _capture(ns):
        called["args"] = ns

    monkeypatch.setattr("qsync.cli_survey.handle_add_page_break", _capture)

    state = {"top_visits": 0}

    def _select_from_list(message: str, choices, instruction=None, default=None):
        if message.startswith("qsync survey menu"):
            state["top_visits"] += 1
            return (
                "Edit Questions & Content — structural item edits"
                if state["top_visits"] == 1
                else "Exit"
            )
        if message == "Edit Questions & Content":
            return "Page breaks (add/remove in block flow)"
        if message == "Page break action:":
            return "Add page break"
        if message == "Choose target block:":
            return str(choices[0])
        if message == "Choose where to insert page break in the selected block:":
            return "slot:1"
        if message == "Dry run?":
            return "Yes"
        return "Exit"

    monkeypatch.setattr(interactive_menu, "select_from_list", _select_from_list)

    handle_menu(argparse.Namespace(tui=False))

    assert "args" in called
    ns = called["args"]
    assert ns.survey_id == "SV_1"
    assert ns.target_block_id == "BL_1"
    assert ns.insert_index == 1
    assert ns.after_qid is None
    assert ns.before_qid is None
    assert ns.position == "append"
    assert ns.dry_run is True


def test_edit_menu_remove_page_break_routes_to_handler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from qsync import interactive_menu
    from qsync.cli_survey import handle_menu

    monkeypatch.setattr("qsync.cli_survey._workspace_root", lambda: tmp_path.resolve())
    monkeypatch.setattr(interactive_menu, "is_interactive", lambda: True)
    monkeypatch.setattr(interactive_menu, "confirm", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        "qsync.cli_survey.get_client_config",
        lambda env=None: ("example.qualtrics.com", {"X-API-TOKEN": "token"}),
    )
    monkeypatch.setattr(
        "qsync.cli_survey.list_surveys",
        lambda base, headers: [{"id": "SV_1", "name": "Survey One"}],
    )
    monkeypatch.setattr(
        "qsync.cli_survey._pick_survey_id_from_records",
        lambda **kwargs: "SV_1",
    )
    monkeypatch.setattr(
        "qsync.cli_survey.fetch_survey_definition",
        lambda base, headers, survey_id: {
            "Questions": {
                "QID1": {"QuestionText": "Q1"},
                "QID2": {"QuestionText": "Q2"},
                "QID3": {"QuestionText": "Q3"},
            },
            "Blocks": {
                "BL_1": {
                    "Type": "Standard",
                    "BlockElements": [
                        {"Type": "Question", "QuestionID": "QID1"},
                        {"Type": "Page Break"},
                        {"Type": "Question", "QuestionID": "QID2"},
                        {"Type": "Page Break"},
                        {"Type": "Question", "QuestionID": "QID3"},
                    ],
                }
            },
            "SurveyFlow": {"Flow": [{"Type": "Block", "ID": "BL_1"}]},
        },
    )

    called = {}
    seen_multi_labels: list[str] = []

    def _capture(ns):
        called["args"] = ns

    def _multi_select(message: str, choices, instruction=None, default=None):
        seen_multi_labels[:] = list(choices)
        return list(choices)

    monkeypatch.setattr(interactive_menu, "multi_select_from_list", _multi_select)
    monkeypatch.setattr("qsync.cli_survey.handle_remove_page_break", _capture)

    state = {"top_visits": 0}

    def _select_from_list(message: str, choices, instruction=None, default=None):
        if message.startswith("qsync survey menu"):
            state["top_visits"] += 1
            return (
                "Edit Questions & Content — structural item edits"
                if state["top_visits"] == 1
                else "Exit"
            )
        if message == "Edit Questions & Content":
            return "Page breaks (add/remove in block flow)"
        if message == "Page break action:":
            return "Remove page break(s)"
        if message == "Choose block containing page break(s):":
            return str(choices[0])
        if message == "How do you want to select page break(s) to remove?":
            return "Pick multiple page breaks"
        if message == "Dry run?":
            return "Yes"
        return "Exit"

    monkeypatch.setattr(interactive_menu, "select_from_list", _select_from_list)

    handle_menu(argparse.Namespace(tui=False))

    assert seen_multi_labels
    assert all(label.startswith("[") and "--- PB ---" in label for label in seen_multi_labels)
    assert "args" in called
    ns = called["args"]
    assert ns.survey_id == "SV_1"
    assert ns.target_block_id == "BL_1"
    assert ns.element_index == ["1", "3"]
    assert ns.dry_run is True


def test_edit_menu_add_question_clone_uses_flow_order_and_explicit_clone_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from qsync import interactive_menu
    from qsync.cli_survey import handle_menu

    monkeypatch.setattr("qsync.cli_survey._workspace_root", lambda: tmp_path.resolve())
    monkeypatch.setattr(interactive_menu, "is_interactive", lambda: True)
    monkeypatch.setattr(interactive_menu, "confirm", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        "qsync.cli_survey.get_client_config",
        lambda env=None: ("example.qualtrics.com", {"X-API-TOKEN": "token"}),
    )
    monkeypatch.setattr(
        "qsync.cli_survey.list_surveys",
        lambda base, headers: [{"id": "SV_1", "name": "Survey One"}],
    )
    monkeypatch.setattr(
        "qsync.cli_survey._pick_survey_id_from_records",
        lambda **kwargs: "SV_1",
    )
    monkeypatch.setattr(
        "qsync.cli_survey.fetch_survey_definition",
        lambda base, headers, survey_id: {
            "Questions": {
                "QID1": {"QuestionText": "First"},
                "QID2": {"QuestionText": "Second"},
            },
            "Blocks": {
                "BL_1": {
                    "Type": "Standard",
                    "BlockElements": [
                        {"Type": "Question", "QuestionID": "QID2"},
                        {"Type": "Question", "QuestionID": "QID1"},
                    ],
                }
            },
            "SurveyFlow": {"Flow": [{"Type": "Block", "ID": "BL_1"}]},
        },
    )

    called = {}

    def _capture(ns):
        called["args"] = ns

    monkeypatch.setattr("qsync.cli_survey.handle_add_question", _capture)

    seen_choices: list[str] = []
    seen_order_choices: list[str] = []

    def _multi_select(message: str, choices, instruction=None, default=None):
        seen_choices[:] = list(choices)
        # Pretend user selected both entries.
        return list(choices)

    monkeypatch.setattr(interactive_menu, "multi_select_from_list", _multi_select)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")

    state = {"top_visits": 0}

    def _select_from_list(message: str, choices, instruction=None, default=None):
        if message.startswith("qsync survey menu"):
            state["top_visits"] += 1
            return (
                "Edit Questions & Content — structural item edits"
                if state["top_visits"] == 1
                else "Exit"
            )
        if message == "Edit Questions & Content":
            return "Add question(s) (clone template, insert in flow)"
        if message == "Question template source:":
            return "Clone existing question(s) from question bank"
        if message == "Clone source account:":
            return "Current account (default)"
        if message == "Question bank lookup mode:":
            return "Live survey definitions (default)"
        if message.startswith("Pick clone order position 1"):
            seen_order_choices[:] = [str(choice) for choice in choices]
            return next(
                (choice for choice in choices if "QID1 - First" in str(choice)),
                str(choices[0]),
            )
        if message.startswith("Pick clone order position 2"):
            return next(
                (choice for choice in choices if "QID2 - Second" in str(choice)),
                str(choices[0]),
            )
        if message == "Question text behavior:":
            return "Keep source question text(s)"
        if message == "Choose target block:":
            return str(choices[0])
        if message == "Choose where to insert new question(s) in the selected block:":
            return "slot:2"
        if message == "Page break handling for inserted question(s):":
            return "No extra page breaks"
        if message == "Dry run?":
            return "Yes"
        return "Exit"

    monkeypatch.setattr(interactive_menu, "select_from_list", _select_from_list)

    handle_menu(argparse.Namespace(tui=False))

    assert seen_choices == ["QID2 - Second", "QID1 - First"]
    assert any(choice.startswith("[ ] ") for choice in seen_order_choices)
    assert "args" in called
    ns = called["args"]
    assert ns.source_question_id == ["QID1", "QID2"]
    assert ns.insert_index == 2
    assert ns.position == "append"
    assert ns.dry_run is True


def test_direct_add_question_mode_accepts_preselected_survey(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from qsync import interactive_menu
    from qsync.cli_survey import handle_menu

    monkeypatch.setattr("qsync.cli_survey._workspace_root", lambda: tmp_path.resolve())
    monkeypatch.setattr(interactive_menu, "is_interactive", lambda: True)
    monkeypatch.setattr(
        "qsync.cli_survey.get_client_config",
        lambda env=None: ("example.qualtrics.com", {"X-API-TOKEN": "token"}),
    )
    monkeypatch.setattr(
        "qsync.cli_survey.fetch_survey_definition",
        lambda base, headers, survey_id: {
            "Questions": {"QID1": {"QuestionText": "Intro"}},
            "Blocks": {"BL_1": {"Type": "Standard", "BlockElements": []}},
            "SurveyFlow": {"Flow": [{"Type": "Block", "ID": "BL_1"}]},
        },
    )

    called = {}

    def _capture(ns):
        called["args"] = ns

    monkeypatch.setattr("qsync.cli_survey.handle_add_question", _capture)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")

    def _select_from_list(message: str, choices, instruction=None, default=None):
        if message == "Question template source:":
            return "Clone existing question(s) from question bank"
        if message == "Clone source account:":
            return "Current account (default)"
        if message == "Question bank lookup mode:":
            return "Live survey definitions (default)"
        if message == "Question text behavior:":
            return "Keep source question text(s)"
        if message == "Choose target block:":
            return str(choices[0])
        if message == "Choose where to insert new question(s) in the selected block:":
            return "slot:0"
        if message == "Page break handling for inserted question(s):":
            return "No extra page breaks"
        if message == "Dry run?":
            return "Yes"
        return "Exit"

    monkeypatch.setattr(interactive_menu, "select_from_list", _select_from_list)

    handle_menu(
        argparse.Namespace(
            tui=False,
            structural_edit=False,
            add_question_interactive=True,
            move_question_interactive=False,
            survey_id="SV_1",
            account=None,
        )
    )

    assert "args" in called
    ns = called["args"]
    assert ns.survey_id == "SV_1"
    assert ns.from_question_id is None
    assert ns.source_survey_id == "SV_1"
    assert ns.source_question_id == ["QID1"]
    assert ns.target_block_id == "BL_1"
    assert ns.insert_index == 0
    assert ns.after_qid is None
    assert ns.dry_run is True


def test_direct_replace_question_mode_accepts_preselected_survey(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from qsync import interactive_menu
    from qsync.cli_survey import handle_menu

    monkeypatch.setattr("qsync.cli_survey._workspace_root", lambda: tmp_path.resolve())
    monkeypatch.setattr(interactive_menu, "is_interactive", lambda: True)
    monkeypatch.setattr(
        "qsync.cli_survey.get_client_config",
        lambda env=None: ("example.qualtrics.com", {"X-API-TOKEN": "token"}),
    )
    monkeypatch.setattr(
        "qsync.cli_survey.fetch_survey_definition",
        lambda base, headers, survey_id: {
            "Questions": {"QID1": {"QuestionText": "Only question"}},
            "Blocks": {"BL_1": {"Type": "Standard", "BlockElements": []}},
            "SurveyFlow": {"Flow": [{"Type": "Block", "ID": "BL_1"}]},
        },
    )

    called = {}

    def _capture(ns):
        called["args"] = ns

    monkeypatch.setattr("qsync.cli_survey.handle_replace_question", _capture)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")

    def _select_from_list(message: str, choices, instruction=None, default=None):
        if message == "Replacement source account:":
            return "Current account (default)"
        if message == "Replacement source survey:":
            return "Use same survey (SV_1)"
        if message == "Replace target DataExportTag with source tag?":
            return "No (keep target DataExportTag)"
        if message == "Dry run?":
            return "Yes"
        return str(choices[0]) if choices else "Exit"

    monkeypatch.setattr(interactive_menu, "select_from_list", _select_from_list)

    handle_menu(
        argparse.Namespace(
            tui=False,
            structural_edit=False,
            add_question_interactive=False,
            move_question_interactive=False,
            remove_question_interactive=False,
            replace_question_interactive=True,
            page_break_interactive=False,
            survey_id="SV_1",
            account=None,
        )
    )

    assert "args" in called
    ns = called["args"]
    assert ns.survey_id == "SV_1"
    assert ns.question_id == "QID1"
    assert ns.source_survey_id == "SV_1"
    assert ns.source_question_id == "QID1"
    assert ns.dry_run is True


def test_edit_menu_add_question_clone_indexed_uses_cached_question_bank(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from qsync import interactive_menu
    from qsync.cli_survey import handle_menu

    monkeypatch.setattr("qsync.cli_survey._workspace_root", lambda: tmp_path.resolve())
    monkeypatch.setattr(interactive_menu, "is_interactive", lambda: True)
    monkeypatch.setattr(interactive_menu, "confirm", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        "qsync.cli_survey.get_client_config",
        lambda env=None: ("example.qualtrics.com", {"X-API-TOKEN": "token"}),
    )
    monkeypatch.setattr(
        "qsync.cli_survey.list_surveys",
        lambda base, headers: [
            {"id": "SV_TARGET", "name": "Target Survey"},
            {"id": "SV_SOURCE", "name": "Source Survey"},
        ],
    )

    def _pick_from_records(message: str, records):
        if "source survey" in message.lower():
            return "SV_SOURCE"
        return "SV_TARGET"

    monkeypatch.setattr(
        "qsync.cli_survey._pick_survey_id_from_records",
        _pick_from_records,
    )

    def _fetch_definition(base, headers, survey_id):
        if survey_id != "SV_TARGET":
            raise AssertionError("source definition should come from indexed cache")
        return {
            "Questions": {"QID1": {"QuestionText": "Target Intro"}},
            "Blocks": {"BL_1": {"Type": "Standard", "BlockElements": []}},
            "SurveyFlow": {"Flow": [{"Type": "Block", "ID": "BL_1"}]},
        }

    monkeypatch.setattr("qsync.cli_survey.fetch_survey_definition", _fetch_definition)

    index_path = tmp_path / ".qsync" / "question_bank_index__default.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(
            {
                "version": 1,
                "account": "default",
                "generated_at_epoch": time.time(),
                "surveys": [
                    {
                        "id": "SV_SOURCE",
                        "name": "Source Survey",
                        "question_labels": [
                            "QID9 - Indexed source one",
                            "QID10 - Indexed source two",
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    called = {}

    def _capture(ns):
        called["args"] = ns

    monkeypatch.setattr("qsync.cli_survey.handle_add_question", _capture)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    monkeypatch.setattr(
        interactive_menu,
        "multi_select_from_list",
        lambda message, choices, instruction=None, default=None: list(choices),
    )

    state = {"top_visits": 0}

    def _select_from_list(message: str, choices, instruction=None, default=None):
        if message.startswith("qsync survey menu"):
            state["top_visits"] += 1
            return (
                "Edit Questions & Content — structural item edits"
                if state["top_visits"] == 1
                else "Exit"
            )
        if message == "Edit Questions & Content":
            return "Add question(s) (clone template, insert in flow)"
        if message == "Question template source:":
            return "Clone existing question(s) from question bank"
        if message == "Clone source account:":
            return "Current account (default)"
        if message == "Question bank lookup mode:":
            return "Indexed local question bank cache (fast, uses pulled files)"
        if message.startswith("Pick clone order position 1"):
            return next(
                (choice for choice in choices if "QID9 - Indexed source one" in str(choice)),
                str(choices[0]),
            )
        if message.startswith("Pick clone order position 2"):
            return next(
                (choice for choice in choices if "QID10 - Indexed source two" in str(choice)),
                str(choices[0]),
            )
        if message == "Question text behavior:":
            return "Keep source question text(s)"
        if message == "Choose target block:":
            return str(choices[0])
        if message == "Choose where to insert new question(s) in the selected block:":
            return "slot:0"
        if message == "Page break handling for inserted question(s):":
            return "No extra page breaks"
        if message == "Dry run?":
            return "Yes"
        return "Exit"

    monkeypatch.setattr(interactive_menu, "select_from_list", _select_from_list)

    handle_menu(argparse.Namespace(tui=False))

    assert "args" in called
    ns = called["args"]
    assert ns.source_survey_id == "SV_SOURCE"
    assert ns.source_question_id == ["QID9", "QID10"]
    assert ns.insert_index == 0
    assert ns.dry_run is True


def test_flow_integrations_menu_includes_prolific_wiring(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from qsync import interactive_menu
    from qsync.cli_survey import handle_menu

    monkeypatch.setattr("qsync.cli_survey._workspace_root", lambda: tmp_path.resolve())
    monkeypatch.setattr(interactive_menu, "is_interactive", lambda: True)
    monkeypatch.setattr(interactive_menu, "confirm", lambda *args, **kwargs: False)

    seen_flow_choices: list[list[str]] = []
    steps = iter(
        [
            "Flow, Embedded Data & Integrations — embedded fields + Prolific",
            "↩ Back",
            "Exit",
        ]
    )

    def _select_from_list(message: str, choices, instruction=None, default=None):
        normalized = [str(c) for c in choices]
        if message == "Flow, Embedded Data & Integrations":
            seen_flow_choices.append(normalized)
        return next(steps)

    monkeypatch.setattr(interactive_menu, "select_from_list", _select_from_list)

    handle_menu(argparse.Namespace(tui=False))

    assert seen_flow_choices, "Flow, Embedded Data & Integrations menu was not shown."
    assert any(
        "Prolific wiring (Prolific ↔ Qualtrics)" in menu_choices
        for menu_choices in seen_flow_choices
    )


def test_embedded_options_structural_route_reaches_wizard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from qsync import interactive_menu
    from qsync.cli_survey import handle_menu
    from qsync.dimensions.items_structural import ItemsStructuralError

    monkeypatch.setenv("QSYNC_ACCOUNT", "damian")
    (tmp_path / ".env.damian").write_text(
        "\n".join(
            [
                "QUALTRICS_BASE_URL=iad1.qualtrics.com",
                "X-API-TOKEN=token",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("qsync.cli_survey._workspace_root", lambda: tmp_path.resolve())
    monkeypatch.setattr(interactive_menu, "is_interactive", lambda: True)
    monkeypatch.setattr(interactive_menu, "confirm", lambda *args, **kwargs: False)

    monkeypatch.setattr(
        "qsync.cli_survey.get_client_config",
        lambda env=None: ("example.qualtrics.com", {"X-API-TOKEN": "token"}),
    )
    monkeypatch.setattr(
        "qsync.cli_survey.list_surveys",
        lambda base, headers: [{"id": "SV_0869BstwT0iWHwq", "name": "Survey One"}],
    )

    class _DummySurvey:
        survey_id = "SV_0869BstwT0iWHwq"
        payload = {"result": {"Questions": {"QID1": {"QuestionText": "x"}}}}

        @property
        def questions(self):
            return self.payload["result"]["Questions"]
    
    monkeypatch.setattr(
        "qsync.qualtrics_client.refresh_survey_cache",
        lambda survey_id, **_: (_DummySurvey(), False),
    )
    monkeypatch.setattr(
        "qsync.qualtrics_client.load_cached_survey",
        lambda survey_id, **_: _DummySurvey(),
    )
    monkeypatch.setattr("qsync.workbook_resolver.WorkbookResolver.resolve", lambda self, survey_id: tmp_path / "dummy.xlsx")

    state = {
        "reached": False,
        "env": None,
        "surveys_dir": None,
    }

    def _raise_stop(**kwargs):
        state["reached"] = True
        state["env"] = kwargs.get("env")
        state["surveys_dir"] = kwargs.get("surveys_dir")
        raise ItemsStructuralError("TEST_WIZARD_REACHED")

    monkeypatch.setattr(
        "qsync.dimensions.items_structural.interactive_choice_wizard",
        _raise_stop,
    )

    state_menu = {"top_visits": 0}

    def _select_from_list(message: str, choices, instruction=None, default=None):
        if message.startswith("Pick a survey for structural edits"):
            return "SV_0869BstwT0iWHwq - Survey One"
        if message.startswith("qsync survey menu"):
            state_menu["top_visits"] += 1
            return (
                "Edit Questions & Content — structural item edits"
                if state_menu["top_visits"] == 1
                else "Exit"
            )
        if message == "Edit Questions & Content":
            return "Items: structural edits (stage → preview → push)"
        if message == "Items: structural edits (stage → preview → push)":
            return "SV_0869BstwT0iWHwq - Survey One"
        if message.startswith("Select a QID to edit"):
            return "QID1 - x"
        if message.startswith("How do you want to select a QID?"):
            return "Browse active-in-flow (arrow list)"
        return "Exit"

    monkeypatch.setattr(interactive_menu, "select_from_list", _select_from_list)

    handle_menu(argparse.Namespace(tui=False))

    out = capsys.readouterr().out
    assert state["reached"]
    assert "TEST_WIZARD_REACHED" in out
    assert state["env"]
    assert state["env"].get("QUALTRICS_BASE_URL") == "iad1.qualtrics.com"
    assert str(state["surveys_dir"]).endswith("/surveys/.damian")


def test_edit_menu_includes_inspect_push_and_stage_by_qid_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from qsync import interactive_menu
    from qsync.cli_survey import handle_menu

    monkeypatch.setattr("qsync.cli_survey._workspace_root", lambda: tmp_path.resolve())
    monkeypatch.setattr(interactive_menu, "is_interactive", lambda: True)
    monkeypatch.setattr(interactive_menu, "confirm", lambda *args, **kwargs: False)

    seen_edit_choices: list[list[str]] = []
    steps = iter(["Edit Questions & Content — structural item edits", "↩ Back", "Exit"])

    def _select_from_list(message: str, choices, instruction=None, default=None):
        normalized = [str(c) for c in choices]
        if message == "Edit Questions & Content":
            seen_edit_choices.append(normalized)
        return next(steps)

    monkeypatch.setattr(interactive_menu, "select_from_list", _select_from_list)

    handle_menu(argparse.Namespace(tui=False))

    assert seen_edit_choices, "Edit Questions & Content menu was not shown."
    assert any(
        "Inspect question payload (local cache)" in menu_choices
        for menu_choices in seen_edit_choices
    )
    assert any(
        "Push one question from local cache" in menu_choices
        for menu_choices in seen_edit_choices
    )
    assert any(
        "Stage by QID (items/js/translations)" in menu_choices
        for menu_choices in seen_edit_choices
    )


def test_edit_menu_stage_by_qid_runs_sync_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from qsync import interactive_menu
    from qsync.cli_survey import handle_menu

    monkeypatch.setattr("qsync.cli_survey._workspace_root", lambda: tmp_path.resolve())
    monkeypatch.setattr(interactive_menu, "is_interactive", lambda: True)
    monkeypatch.setattr(interactive_menu, "confirm", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        "qsync.cli_survey.get_client_config",
        lambda env=None: ("example.qualtrics.com", {"X-API-TOKEN": "token"}),
    )
    monkeypatch.setattr(
        "qsync.cli_survey.list_surveys",
        lambda base, headers: [{"id": "SV_1", "name": "Survey One"}],
    )
    monkeypatch.setattr(
        "qsync.cli_survey._pick_survey_id_from_records",
        lambda **kwargs: "SV_1",
    )
    monkeypatch.setattr(
        "qsync.cli_survey.fetch_survey_definition",
        lambda base, headers, survey_id: {
            "Questions": {
                "QID1": {"QuestionText": "Question one"},
                "QID2": {"QuestionText": "Question two"},
            },
            "Blocks": {
                "BL_1": {
                    "Type": "Standard",
                    "BlockElements": [
                        {"Type": "Question", "QuestionID": "QID1"},
                        {"Type": "Question", "QuestionID": "QID2"},
                    ],
                }
            },
            "SurveyFlow": {"Flow": [{"Type": "Block", "ID": "BL_1"}]},
        },
    )

    invoked: dict[str, list[str]] = {}

    def _fake_main_impl(argv: list[str]) -> None:
        invoked["argv"] = list(argv)

    monkeypatch.setattr("qsync.cli._main_impl", _fake_main_impl)

    state = {"top_visits": 0}

    def _select_from_list(message: str, choices, instruction=None, default=None):
        if message.startswith("qsync survey menu"):
            state["top_visits"] += 1
            return (
                "Edit Questions & Content — structural item edits"
                if state["top_visits"] == 1
                else "Exit"
            )
        if message == "Edit Questions & Content":
            return "Stage by QID (items/js/translations)"
        return "Exit"

    def _multi_select_from_list(message: str, choices, instruction=None, default=None):
        if message == "Pick one or more QIDs to stage:":
            return [str(choices[0]), str(choices[1])]
        if message == "Dimensions to stage for selected QIDs:":
            return ["items", "translations"]
        return []

    monkeypatch.setattr(interactive_menu, "select_from_list", _select_from_list)
    monkeypatch.setattr(
        interactive_menu, "multi_select_from_list", _multi_select_from_list
    )

    handle_menu(argparse.Namespace(tui=False))

    argv = invoked.get("argv") or []
    assert argv
    assert "sync" in argv
    assert "--survey-id" in argv and "SV_1" in argv
    assert "--dimensions" in argv and "items,translations" in argv
    assert "--scope" in argv and "qid:QID1 OR qid:QID2" in argv
    assert "--pending-action" in argv and "stage" in argv
    assert "--yes" in argv


def test_edit_menu_add_question_uses_data_export_tag_name_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from qsync import interactive_menu
    from qsync.cli_survey import handle_menu

    monkeypatch.setattr("qsync.cli_survey._workspace_root", lambda: tmp_path.resolve())
    monkeypatch.setattr(interactive_menu, "is_interactive", lambda: True)
    monkeypatch.setattr(interactive_menu, "confirm", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        "qsync.cli_survey.get_client_config",
        lambda env=None: ("example.qualtrics.com", {"X-API-TOKEN": "token"}),
    )
    monkeypatch.setattr(
        "qsync.cli_survey.list_surveys",
        lambda base, headers: [{"id": "SV_1", "name": "Survey One"}],
    )
    monkeypatch.setattr(
        "qsync.cli_survey._pick_survey_id_from_records",
        lambda **kwargs: "SV_1",
    )
    monkeypatch.setattr(
        "qsync.cli_survey.fetch_survey_definition",
        lambda base, headers, survey_id: {
            "Questions": {
                "QID1": {
                    "QuestionText": "",
                    "QuestionDescription": "",
                    "DataExportTag": "tag_fallback",
                },
                "QID2": {
                    "QuestionText": "Second question",
                    "DataExportTag": "tag_second",
                },
            },
            "Blocks": {"BL_1": {"Type": "Standard", "BlockElements": []}},
            "SurveyFlow": {"Flow": [{"Type": "Block", "ID": "BL_1"}]},
        },
    )

    called = {}
    seen_source_labels: list[str] = []

    def _capture(ns):
        called["args"] = ns

    def _multi_select_from_list(message: str, choices, instruction=None, default=None):
        if message == "Choose source question(s) to clone:":
            seen_source_labels.extend(str(choice) for choice in choices)
            return [str(choices[0])]
        return []

    monkeypatch.setattr("qsync.cli_survey.handle_add_question", _capture)
    monkeypatch.setattr(
        interactive_menu, "multi_select_from_list", _multi_select_from_list
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")

    state = {"top_visits": 0}

    def _select_from_list(message: str, choices, instruction=None, default=None):
        if message.startswith("qsync survey menu"):
            state["top_visits"] += 1
            return (
                "Edit Questions & Content — structural item edits"
                if state["top_visits"] == 1
                else "Exit"
            )
        if message == "Edit Questions & Content":
            return "Add question(s) (clone template, insert in flow)"
        if message == "Question template source:":
            return "Clone existing question(s) from question bank"
        if message == "Clone source account:":
            return "Current account (default)"
        if message == "Question bank lookup mode:":
            return "Live survey definitions (default)"
        if message == "Question text behavior:":
            return "Keep source question text(s)"
        if message == "Choose target block:":
            return str(choices[0])
        if message == "Choose where to insert new question(s) in the selected block:":
            return "slot:0"
        if message == "Page break handling for inserted question(s):":
            return "No extra page breaks"
        if message == "Dry run?":
            return "Yes"
        return "Exit"

    monkeypatch.setattr(interactive_menu, "select_from_list", _select_from_list)

    handle_menu(argparse.Namespace(tui=False))

    assert seen_source_labels
    assert any("tag_fallback" in label for label in seen_source_labels)
    assert "args" in called
    assert called["args"].source_question_id == ["QID1"]


def test_bulk_master_quick_action_routes_to_master_pull(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from qsync import interactive_menu
    from qsync.cli_survey import handle_menu

    monkeypatch.setattr("qsync.cli_survey._workspace_root", lambda: tmp_path.resolve())
    monkeypatch.setattr(interactive_menu, "is_interactive", lambda: True)
    monkeypatch.setattr(
        "qsync.cli_survey.get_client_config",
        lambda env=None: ("example.qualtrics.com", {"X-API-TOKEN": "token"}),
    )

    captured: dict[str, argparse.Namespace] = {}

    def _capture(ns: argparse.Namespace) -> None:
        captured["args"] = ns

    monkeypatch.setattr("qsync.cli_survey.handle_master_pull", _capture)

    def _select_from_list(message: str, choices, instruction=None, default=None):
        if message == "Survey master":
            return "Pull focal snapshots + master CSV"
        if message == "Force overwrite existing master CSV?":
            return "No"
        return "↩ Back"

    monkeypatch.setattr(interactive_menu, "select_from_list", _select_from_list)

    handle_menu(argparse.Namespace(tui=False, quick_action="bulk-master"))

    assert "args" in captured
    assert captured["args"].force_overwrite is False
