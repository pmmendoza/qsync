from __future__ import annotations

import argparse
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
            return "Clone an existing question in this survey"
        if message == "Choose template question:":
            return "QID1 - Intro"
        if message == "How many questions should be created?":
            return "Use template text (create one question)"
        if message == "Where should the new question(s) be inserted?":
            return "After existing question"
        if message == "Choose anchor question (insert after):":
            return "QID1 - Intro"
        if message == "Dry run?":
            return "Yes"
        return "Exit"

    monkeypatch.setattr(interactive_menu, "select_from_list", _select_from_list)

    handle_menu(argparse.Namespace(tui=False))

    assert "args" in called
    ns = called["args"]
    assert ns.survey_id == "SV_1"
    assert ns.from_question_id == "QID1"
    assert ns.after_qid == "QID1"
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
        if message == "Where should selected question(s) be moved?":
            return "End of block (append)"
        if message == "How should the target block be resolved?":
            return "Auto target block"
        if message == "Dry run?":
            return "Yes"
        return "Exit"

    monkeypatch.setattr(interactive_menu, "select_from_list", _select_from_list)

    handle_menu(argparse.Namespace(tui=False))

    assert "args" in called
    ns = called["args"]
    assert ns.survey_id == "SV_1"
    assert ns.question_id == ["QID1"]
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
            return "Clone an existing question in this survey"
        if message == "Choose template question:":
            return "QID1 - Intro"
        if message == "How many questions should be created?":
            return "Use template text (create one question)"
        if message == "Where should the new question(s) be inserted?":
            return "After existing question"
        if message == "Choose anchor question (insert after):":
            return "QID1 - Intro"
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
    assert ns.from_question_id == "QID1"
    assert ns.after_qid == "QID1"
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
