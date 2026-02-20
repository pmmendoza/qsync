from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.workspace_helpers import ensure_qsync_workspace, write_inventory_csv


def _touch_env_for_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "QSYNC_ACCOUNT",
        "QSYNC_ROOT",
        "QSYNC_ENV_PATH",
        "QSYNC_JSON_MODE",
        "QSYNC_ALLOW_LOCKED",
    ):
        monkeypatch.delenv(key, raising=False)


def test_discover_account_env_files_filters_invalid_envs(tmp_path: Path) -> None:
    from qsync.cli_survey import _discover_account_env_files

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")

    # Valid account env file.
    (tmp_path / ".env.damian").write_text(
        "\n".join(
            [
                "QUALTRICS_BASE_URL=iad1.qualtrics.com",
                "X-API-TOKEN=secret",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    # Invalid/missing keys should be ignored.
    (tmp_path / ".env.bad").write_text(
        "QUALTRICS_BASE_URL=iad1.qualtrics.com\n",
        encoding="utf-8",
    )

    # Templates/examples should be ignored.
    (tmp_path / ".env.example").write_text(
        "QUALTRICS_BASE_URL=iad1.qualtrics.com\nX-API-TOKEN=secret\n",
        encoding="utf-8",
    )
    (tmp_path / ".env.template").write_text(
        "QUALTRICS_BASE_URL=iad1.qualtrics.com\nX-API-TOKEN=secret\n",
        encoding="utf-8",
    )

    assert _discover_account_env_files(root=tmp_path) == ["damian"]


@pytest.mark.parametrize(
    ("typed", "expected", "ok"),
    [
        ("delete", "delete", True),
        (" delete ", "delete", True),
        ("Delete", "delete", False),
        ("nope", "delete", False),
        ("", "delete", False),
    ],
)
def test_typed_confirmation_exact_match(typed: str, expected: str, ok: bool) -> None:
    from qsync.cli_survey import _typed_confirmation

    assert (
        _typed_confirmation(prompt="Type: ", expected=expected, input_fn=lambda _p: typed)
        is ok
    )


def test_survey_menu_requires_tty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from qsync.cli import main

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")

    # Ensure menu exits early without prompting.
    monkeypatch.setattr("qsync.interactive_menu.is_interactive", lambda: False)

    with pytest.raises(SystemExit) as excinfo:
        main(["--root", str(tmp_path), "survey", "menu"])

    assert "Interactive TTY required" in str(excinfo.value)


def test_survey_menu_check_api_includes_active_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from qsync.cli import main

    _touch_env_for_restore(monkeypatch)
    ensure_qsync_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)

    (tmp_path / ".env.damian").write_text(
        "\n".join(
            [
                "QUALTRICS_BASE_URL=iad1.qualtrics.com",
                "X-API-TOKEN=secret",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    class _FakeResponse:
        def json(self):
            return {"result": {"datacenter": "iad1", "userId": "UR_beWPdGZTMK6nHuK"}}

    with (
        patch(
            "qsync.interactive_menu.is_interactive",
            return_value=True,
        ),
        patch(
            "qsync.interactive_menu.select_from_list",
            side_effect=["Workspace & Account — account, API, inventory, prepare", "Check API (/whoami)", "Exit"],
        ),
        patch("qsync.cli_survey.send_api_request", return_value=_FakeResponse()),
    ):
        main(["--root", str(tmp_path), "--account", "damian", "survey", "menu"])

    captured = capsys.readouterr().out
    assert "[survey-menu] whoami account=damian" in captured


def test_survey_menu_workspace_can_set_cache_subfolder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.cli import main

    _touch_env_for_restore(monkeypatch)
    ensure_qsync_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)

    with (
        patch("qsync.interactive_menu.is_interactive", return_value=True),
        patch(
            "qsync.interactive_menu.select_from_list",
            side_effect=[
                "Workspace & Account — account, API, inventory, prepare",
                "Configure survey cache folder",
                "Set cache subfolder name",
                "↩ Back",
                "↩ Back",
                "Exit",
            ],
        ),
        patch("builtins.input", return_value="defs"),
    ):
        main(["--root", str(tmp_path), "survey", "menu"])

    prefs = json.loads((tmp_path / ".qsync" / "preferences.json").read_text(encoding="utf-8"))
    assert prefs.get("survey_cache_subdir") == "defs"


def test_survey_menu_copy_cross_account_uses_explicit_default_source_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync import interactive_menu
    from qsync.cli_survey import handle_menu

    _touch_env_for_restore(monkeypatch)
    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QSYNC_ACCOUNT", "damian")

    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "QUALTRICS_BASE_URL=example.qualtrics.com",
                "X-API-TOKEN=default-token",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / ".env.damian").write_text(
        "\n".join(
            [
                "QUALTRICS_BASE_URL=example.qualtrics.com",
                "X-API-TOKEN=damian-token",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("qsync.cli_survey._workspace_root", lambda: tmp_path.resolve())
    monkeypatch.setattr(interactive_menu, "is_interactive", lambda: True)

    def _get_client_config(env=None):
        base = str((env or {}).get("QUALTRICS_BASE_URL") or "example.qualtrics.com")
        token = str((env or {}).get("X-API-TOKEN") or "")
        return base, {"X-API-TOKEN": token}

    monkeypatch.setattr("qsync.cli_survey.get_client_config", _get_client_config)

    def _list_surveys(base: str, headers: dict) -> list[dict]:
        token = str(headers.get("X-API-TOKEN") or "")
        if token == "default-token":
            return [
                {"id": "SV_DEFAULT_ONLY", "name": "Default Source", "creationDate": "2026-01-02"}
            ]
        if token == "damian-token":
            return [
                {"id": "SV_DAMIAN_ONLY", "name": "Damian Source", "creationDate": "2026-01-01"}
            ]
        raise AssertionError(f"unexpected token: {token}")

    monkeypatch.setattr("qsync.cli_survey.list_surveys", _list_surveys)

    seen: dict[str, object] = {}

    def _pick_survey_id_from_records(*, message: str, records: list[dict]):
        seen["records"] = list(records)
        return "SV_DEFAULT_ONLY"

    monkeypatch.setattr(
        "qsync.cli_survey._pick_survey_id_from_records",
        _pick_survey_id_from_records,
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": "Copied Survey")

    def _capture_copy(ns):
        seen["copy_args"] = ns

    monkeypatch.setattr("qsync.cli_survey.handle_copy_cross_account", _capture_copy)

    state = {"top": 0}

    def _select_from_list(message: str, choices, instruction=None, default=None):
        if message.startswith("qsync survey menu"):
            state["top"] += 1
            return (
                "Copy, Slice & Compare — derive and verify surveys"
                if state["top"] == 1
                else "Exit"
            )
        if message == "Copy, Slice & Compare":
            return "Copy cross-account"
        if message == "Select source account:":
            return "default (.env)"
        if message == "Select target account:":
            return "damian"
        return "↩ Back"

    monkeypatch.setattr(interactive_menu, "select_from_list", _select_from_list)

    handle_menu(
        argparse.Namespace(
            tui=False,
            structural_edit=False,
            add_question_interactive=False,
            move_question_interactive=False,
            survey_id=None,
            account=None,
            quick_action=None,
        )
    )

    picked_records = seen.get("records")
    assert isinstance(picked_records, list)
    assert [r.get("id") for r in picked_records] == ["SV_DEFAULT_ONLY"]

    copy_args = seen.get("copy_args")
    assert copy_args is not None
    assert getattr(copy_args, "source_account") == "default"
    assert getattr(copy_args, "target_account") == "damian"


def test_survey_menu_quick_action_workspace_show_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from qsync import interactive_menu
    from qsync.cli_survey import handle_menu

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")

    monkeypatch.setattr("qsync.cli_survey._workspace_root", lambda: tmp_path.resolve())
    monkeypatch.setattr(interactive_menu, "is_interactive", lambda: True)
    monkeypatch.setattr(
        "qsync.cli_survey.get_client_config",
        lambda env=None: ("example.qualtrics.com", {"X-API-TOKEN": "token"}),
    )

    handle_menu(
        argparse.Namespace(
            tui=False,
            structural_edit=False,
            add_question_interactive=False,
            move_question_interactive=False,
            survey_id=None,
            account=None,
            quick_action="workspace-show-account",
        )
    )

    out = capsys.readouterr().out
    assert "[survey-menu] account=default base_url=" in out
    assert "[survey-menu] resolved_base_url=example.qualtrics.com token_present=True" in out


def test_survey_menu_quick_action_refresh_question_bank_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from qsync import interactive_menu
    from qsync.cli_survey import handle_menu

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")

    cached_payload = tmp_path / "survey_cached.json"
    cached_payload.write_text(
        json.dumps(
            {
                "result": {
                    "Questions": {
                        "QID1": {
                            "QuestionID": "QID1",
                            "QuestionText": "Indexed question",
                        }
                    },
                    "Blocks": {
                        "BL_1": {
                            "Type": "Standard",
                            "BlockElements": [
                                {"Type": "Question", "QuestionID": "QID1"}
                            ],
                        }
                    },
                    "SurveyFlow": {"Flow": [{"Type": "Block", "ID": "BL_1"}]},
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("qsync.cli_survey._workspace_root", lambda: tmp_path.resolve())
    monkeypatch.setattr(interactive_menu, "is_interactive", lambda: True)
    monkeypatch.setattr(
        "qsync.cli_survey.get_client_config",
        lambda env=None: ("example.qualtrics.com", {"X-API-TOKEN": "token"}),
    )
    monkeypatch.setattr(
        "qsync.cli_survey.list_surveys",
        lambda base, headers: [
            {"id": "SV_1", "name": "Survey One", "creationDate": "2026-01-01"}
        ],
    )
    monkeypatch.setattr(
        "qsync.cli_survey.find_cached_survey_file",
        lambda survey_id, base_dir=None: cached_payload if survey_id == "SV_1" else None,
    )

    state = {"scope_prompt": 0}

    def _select_from_list(message: str, choices, instruction=None, default=None):
        if message == "Refresh question-bank index for which account?":
            state["scope_prompt"] += 1
            return "Current menu account (default)"
        return "↩ Back"

    monkeypatch.setattr(interactive_menu, "select_from_list", _select_from_list)

    handle_menu(
        argparse.Namespace(
            tui=False,
            structural_edit=False,
            add_question_interactive=False,
            move_question_interactive=False,
            page_break_interactive=False,
            survey_id=None,
            account=None,
            quick_action="workspace-refresh-question-bank",
        )
    )

    out = capsys.readouterr().out
    assert "Indexed question bank updated" in out
    assert state["scope_prompt"] == 1

    index_path = tmp_path / ".qsync" / "question_bank_index__default.json"
    assert index_path.exists()
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert payload.get("survey_count") == 1
    surveys = payload.get("surveys") or []
    assert surveys and surveys[0].get("id") == "SV_1"


def test_survey_menu_quick_action_unknown_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync import interactive_menu
    from qsync.cli_survey import handle_menu

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")

    monkeypatch.setattr("qsync.cli_survey._workspace_root", lambda: tmp_path.resolve())
    monkeypatch.setattr(interactive_menu, "is_interactive", lambda: True)

    with pytest.raises(SystemExit) as excinfo:
        handle_menu(
            argparse.Namespace(
                tui=False,
                structural_edit=False,
                add_question_interactive=False,
                move_question_interactive=False,
                survey_id=None,
                account=None,
                quick_action="__unknown__",
            )
        )

    assert "unknown quick action '__unknown__'" in str(excinfo.value)
