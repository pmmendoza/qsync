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


def test_embedded_options_structural_route_reaches_wizard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from qsync import interactive_menu
    from qsync.cli_survey import handle_menu
    from qsync.dimensions.items_structural import ItemsStructuralError

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

    class _DummySurvey:
        survey_id = "SV_1"
        payload = {"result": {"Questions": {"QID1": {"QuestionText": "x"}}}}

        @property
        def questions(self):
            return self.payload["result"]["Questions"]

    monkeypatch.setattr(
        "qsync.qualtrics_client.refresh_survey_cache",
        lambda survey_id: (_DummySurvey(), False),
    )
    monkeypatch.setattr(
        "qsync.qualtrics_client.load_cached_survey",
        lambda survey_id: _DummySurvey(),
    )
    monkeypatch.setattr("qsync.workbook_resolver.WorkbookResolver.resolve", lambda self, survey_id: tmp_path / "dummy.xlsx")

    def _raise_stop(**kwargs):
        raise ItemsStructuralError("TEST_WIZARD_REACHED")

    monkeypatch.setattr(
        "qsync.dimensions.items_structural.interactive_choice_wizard",
        _raise_stop,
    )

    steps = iter(
        [
            "Embedded & Options",
            "Items: structural edits (stage → preview → push)",
            "SV_1 - Survey One",
            "Exit",
        ]
    )

    def _select_from_list(message: str, choices, instruction=None, default=None):
        return next(steps)

    monkeypatch.setattr(interactive_menu, "select_from_list", _select_from_list)

    handle_menu(argparse.Namespace(tui=False))

    out = capsys.readouterr().out
    assert "TEST_WIZARD_REACHED" in out
