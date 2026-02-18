from __future__ import annotations

import argparse
from pathlib import Path

import pytest


def _fake_survey_cache_payload() -> dict:
    return {
        "result": {
            "Questions": {
                "QID1": {"DataExportTag": "newsmem_recognition"},
                "QID2": {"DataExportTag": "newsmem_salience"},
                "QID3": {"DataExportTag": "plain_tag"},
            }
        }
    }


def _setup_menu_dependencies(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from qsync import interactive_menu
    from qsync.qualtrics_client import SurveyCache

    monkeypatch.setattr("qsync.cli_survey._workspace_root", lambda: tmp_path.resolve())
    monkeypatch.setattr(interactive_menu, "is_interactive", lambda: True)
    monkeypatch.setattr(interactive_menu, "confirm", lambda *args, **kwargs: True)
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
        "qsync.qualtrics_client.load_cached_survey",
        lambda survey_id, surveys_dir=None, env=None: SurveyCache(
            survey_id=survey_id,
            path=tmp_path / "surveys" / "Survey-One__SV_1.json",
            payload=_fake_survey_cache_payload(),
        ),
    )
    monkeypatch.setattr(
        "qsync.dimensions.items_structural.external_owner_for",
        lambda *, qid, data_export_tag: {
            "newsmem_recognition": "scripts/update_newsmem_recognition.py",
            "newsmem_salience": "scripts/update_salience_items.py",
        }.get(data_export_tag),
    )


def test_menu_toggle_protected_qids_updates_scoped_preference(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from qsync import interactive_menu
    from qsync.cli_survey import handle_menu
    from qsync.workspace_prefs import (
        get_workspace_items_allow_externally_managed_qids,
        set_workspace_items_allow_externally_managed_qids,
    )

    _setup_menu_dependencies(monkeypatch, tmp_path)
    set_workspace_items_allow_externally_managed_qids(
        tmp_path.resolve(), "SV_1:QID2"
    )

    monkeypatch.setattr(
        interactive_menu,
        "multi_select_from_list",
        lambda message, choices, instruction=None, default=None: [str(choices[0])],
    )

    state = {"top": 0, "workspace": 0, "external": 0}

    def _select_from_list(message: str, choices, instruction=None, default=None):
        if message.startswith("qsync survey menu"):
            state["top"] += 1
            return (
                "Workspace & Account — account, API, inventory, prepare"
                if state["top"] == 1
                else "Exit"
            )
        if message == "Workspace & Account":
            state["workspace"] += 1
            return (
                "Configure externally managed item overrides"
                if state["workspace"] == 1
                else "↩ Back"
            )
        if message == "Externally managed overrides":
            state["external"] += 1
            return (
                "Toggle allowed protected QIDs for one survey (multi-select)"
                if state["external"] == 1
                else "↩ Back"
            )
        return "Exit"

    monkeypatch.setattr(interactive_menu, "select_from_list", _select_from_list)

    handle_menu(argparse.Namespace(tui=False))

    assert (
        get_workspace_items_allow_externally_managed_qids(tmp_path.resolve())
        == "SV_1:QID1"
    )


def test_menu_show_protected_qids_prints_current_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from qsync import interactive_menu
    from qsync.cli_survey import handle_menu
    from qsync.workspace_prefs import set_workspace_items_allow_externally_managed_qids

    _setup_menu_dependencies(monkeypatch, tmp_path)
    set_workspace_items_allow_externally_managed_qids(
        tmp_path.resolve(), "SV_1:QID2"
    )

    state = {"top": 0, "workspace": 0, "external": 0}

    def _select_from_list(message: str, choices, instruction=None, default=None):
        if message.startswith("qsync survey menu"):
            state["top"] += 1
            return (
                "Workspace & Account — account, API, inventory, prepare"
                if state["top"] == 1
                else "Exit"
            )
        if message == "Workspace & Account":
            state["workspace"] += 1
            return (
                "Configure externally managed item overrides"
                if state["workspace"] == 1
                else "↩ Back"
            )
        if message == "Externally managed overrides":
            state["external"] += 1
            return (
                "Show protected QIDs for one survey"
                if state["external"] == 1
                else "↩ Back"
            )
        return "Exit"

    monkeypatch.setattr(interactive_menu, "select_from_list", _select_from_list)

    handle_menu(argparse.Namespace(tui=False))

    out = capsys.readouterr().out
    assert "QID1 [PROTECTED]" in out
    assert "QID2 [ALLOWED]" in out
