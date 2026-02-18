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

    main(["--root", str(tmp_path), "sync", "--tui"])

    assert "args" in called
    assert getattr(called["args"], "command", "") == "sync"
    assert bool(getattr(called["args"], "tui", False)) is True


def test_help_sync_topic_mentions_sync_tui(capsys: pytest.CaptureFixture[str]) -> None:
    from qsync.cli import main

    main(["help", "sync"])

    out = capsys.readouterr().out
    assert "qsync sync --tui" in out
