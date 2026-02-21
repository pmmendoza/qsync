from __future__ import annotations

from pathlib import Path

import pytest

from tests.workspace_helpers import ensure_qsync_workspace, write_inventory_csv


def test_sync_command_passes_tui_flag_to_tui_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.cli import main

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")

    called: dict[str, object] = {}

    def _fake_handle_tui(args) -> None:
        called["args"] = args

    monkeypatch.setattr("qsync.cli._handle_tui", _fake_handle_tui)
    monkeypatch.setattr("qsync.terminal_output.is_interactive", lambda _args=None: True)

    main(["--root", str(tmp_path), "sync", "--tui"])

    assert "args" in called
    assert getattr(called["args"], "command", "") == "sync"
    assert str(getattr(called["args"], "tui_mode", "")) == "on"


def test_sync_command_tui_auto_uses_tui_in_interactive_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.cli import main

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")

    called: dict[str, object] = {}

    def _fake_handle_tui(args) -> None:
        called["args"] = args

    monkeypatch.setattr("qsync.cli._handle_tui", _fake_handle_tui)
    monkeypatch.setattr("qsync.terminal_output.is_interactive", lambda _args=None: True)

    main(["--root", str(tmp_path), "sync", "--tui=auto"])

    assert "args" in called
    assert str(getattr(called["args"], "tui_mode", "")) == "auto"


def test_sync_command_tui_auto_falls_back_to_cli_in_noninteractive_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.cli import main

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")

    called = {"tui": 0, "focal": 0}

    def _fake_handle_tui(_args) -> None:
        called["tui"] += 1

    def _fake_sync_focal_surveys(**_kwargs) -> bool:
        called["focal"] += 1
        return True

    monkeypatch.setattr("qsync.cli._handle_tui", _fake_handle_tui)
    monkeypatch.setattr("qsync.terminal_output.is_interactive", lambda _args=None: False)
    monkeypatch.setattr("qsync.sync_orchestrator.sync_focal_surveys", _fake_sync_focal_surveys)

    main(["--root", str(tmp_path), "sync", "--tui=auto", "--yes"])

    assert called["tui"] == 0
    assert called["focal"] == 1


def test_sync_command_no_tui_forces_cli_even_when_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.cli import main

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")

    called = {"tui": 0, "focal": 0}

    def _fake_handle_tui(_args) -> None:
        called["tui"] += 1

    def _fake_sync_focal_surveys(**_kwargs) -> bool:
        called["focal"] += 1
        return True

    monkeypatch.setattr("qsync.cli._handle_tui", _fake_handle_tui)
    monkeypatch.setattr("qsync.terminal_output.is_interactive", lambda _args=None: True)
    monkeypatch.setattr("qsync.sync_orchestrator.sync_focal_surveys", _fake_sync_focal_surveys)

    main(["--root", str(tmp_path), "sync", "--no-tui", "--yes"])

    assert called["tui"] == 0
    assert called["focal"] == 1


def test_help_sync_topic_mentions_sync_tui(capsys: pytest.CaptureFixture[str]) -> None:
    from qsync.cli import main

    main(["help", "sync"])

    out = capsys.readouterr().out
    assert "qsync sync --tui" in out
    assert "qsync sync --tui=auto" in out
    assert "qsync sync --no-tui" in out
