from __future__ import annotations

from pathlib import Path

import pytest

from tests.workspace_helpers import ensure_qsync_workspace, write_inventory_csv


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

