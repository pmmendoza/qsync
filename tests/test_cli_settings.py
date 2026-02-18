from __future__ import annotations

from pathlib import Path

import pytest

from tests.workspace_helpers import ensure_qsync_workspace, write_inventory_csv


def test_settings_command_dispatches_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.cli import main

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")

    called: dict[str, object] = {}

    def _fake_handle_settings(args) -> None:
        called["args"] = args

    monkeypatch.setattr("qsync.cli_settings.handle_settings", _fake_handle_settings)

    main(["--root", str(tmp_path), "settings"])

    assert "args" in called
    assert bool(getattr(called["args"], "tui", False)) is False


def test_settings_command_passes_tui_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.cli import main

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")

    called: dict[str, object] = {}

    def _fake_handle_settings(args) -> None:
        called["args"] = args

    monkeypatch.setattr("qsync.cli_settings.handle_settings", _fake_handle_settings)

    main(["--root", str(tmp_path), "settings", "--tui"])

    assert "args" in called
    assert bool(getattr(called["args"], "tui", False)) is True


def test_help_settings_topic(capsys: pytest.CaptureFixture[str]) -> None:
    from qsync.cli import main

    main(["help", "settings"])

    out = capsys.readouterr().out
    assert "Settings Command Center" in out
    assert "qsync settings --tui" in out
