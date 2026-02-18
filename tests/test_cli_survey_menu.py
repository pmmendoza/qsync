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
