from __future__ import annotations

import builtins

import pytest


def _feed_inputs(monkeypatch: pytest.MonkeyPatch, responses: list[str]) -> None:
    it = iter(responses)

    def _fake_input(_prompt: str = "") -> str:
        return next(it)

    monkeypatch.setattr(builtins, "input", _fake_input)


def test_fallback_select_cancel_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    from qsync.interactive_menu import select_from_list

    monkeypatch.setenv("QSYNC_USE_QUESTIONARY", "0")
    _feed_inputs(monkeypatch, ["q"])

    selected = select_from_list("Pick", ["one", "─" * 10, "two"])
    assert selected is None


def test_fallback_select_blank_picks_first_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from qsync.interactive_menu import MenuItem, select_from_list

    monkeypatch.setenv("QSYNC_USE_QUESTIONARY", "0")
    _feed_inputs(monkeypatch, [""])

    selected = select_from_list(
        "Pick",
        [
            MenuItem.separator("─" * 10),
            MenuItem(label="A", value="a"),
            MenuItem(label="B", value="b"),
        ],
    )
    assert selected == "a"


def test_fallback_select_disabled_items_not_selectable(monkeypatch: pytest.MonkeyPatch) -> None:
    from qsync.interactive_menu import MenuItem, select_from_list

    monkeypatch.setenv("QSYNC_USE_QUESTIONARY", "0")
    _feed_inputs(monkeypatch, ["1"])

    selected = select_from_list(
        "Pick",
        [
            MenuItem(label="Disabled", value="d", enabled=False, disabled_reason="nope"),
            MenuItem(label="Enabled", value="e", enabled=True),
        ],
    )
    assert selected == "e"


def test_qsync_tui_missing_dependency_prints_hint(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from qsync.cli import main

    real_import = builtins.__import__

    def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name in {"qsync.tui.app", "tui.app"} or name.endswith(".tui.app"):
            raise ImportError("forced missing tui dependency for test")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    # Ensure we don't fail early on non-interactive detection for this test.
    monkeypatch.setattr("qsync.interactive_menu.is_interactive", lambda: True)
    monkeypatch.setenv("QSYNC_ROOT", str(tmp_path))

    with pytest.raises(SystemExit) as excinfo:
        main(["tui"])

    assert excinfo.value.code == 1
    out = capsys.readouterr()
    combined = (out.out or "") + (out.err or "")
    assert "TUI dependencies are not installed" in combined
    assert "qsync[tui]" in combined
