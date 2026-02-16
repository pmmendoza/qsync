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

    seen_embedded_choices: list[list[str]] = []
    steps = iter(["Embedded & Options", "↩ Back", "Exit"])

    def _select_from_list(message: str, choices, instruction=None, default=None):
        normalized = [str(c) for c in choices]
        if message == "Embedded & Options":
            seen_embedded_choices.append(normalized)
        return next(steps)

    monkeypatch.setattr(interactive_menu, "select_from_list", _select_from_list)

    handle_menu(argparse.Namespace(tui=False))

    assert seen_embedded_choices, "Embedded & Options menu was not shown."
    assert any(
        "Items: structural edits (stage → preview → push)" in menu_choices
        for menu_choices in seen_embedded_choices
    )
    assert any(
        "Prolific wiring (Prolific ↔ Qualtrics)" in menu_choices
        for menu_choices in seen_embedded_choices
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
            return "Embedded & Options" if state_menu["top_visits"] == 1 else "Exit"
        if message == "Embedded & Options":
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
