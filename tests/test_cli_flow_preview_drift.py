from __future__ import annotations

from pathlib import Path

from tests.workspace_helpers import ensure_qsync_workspace, write_inventory_csv


def test_flow_preview_runs_drift_check(monkeypatch, tmp_path: Path) -> None:
    from qsync.cli import main

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")

    captured: dict[str, object] = {}
    preview_calls: list[tuple[str, bool, bool]] = []

    def _fake_confirm_preview_drift(
        survey_id: str,
        dimension: str,
        *,
        allow_drift: bool,
        interactive: bool,
        update_cache,
        context=None,
    ):
        captured["survey_id"] = survey_id
        captured["dimension"] = dimension
        captured["allow_drift"] = allow_drift
        captured["interactive"] = interactive
        captured["update_cache"] = update_cache
        captured["context"] = context
        return None

    monkeypatch.setattr(
        "qsync.drift_check.confirm_preview_drift",
        _fake_confirm_preview_drift,
    )
    monkeypatch.setattr(
        "qsync.dimensions.flow.preview",
        lambda survey_id, *, verbose=False, visual=False: preview_calls.append(
            (survey_id, bool(verbose), bool(visual))
        )
        or [],
    )

    main(
        [
            "--root",
            str(tmp_path),
            "flow",
            "preview",
            "--survey-id",
            "SV_TEST",
            "--allow-drift",
        ]
    )

    assert captured["survey_id"] == "SV_TEST"
    assert captured["dimension"] == "flow"
    assert captured["allow_drift"] is True
    assert captured["interactive"] is False
    assert captured["update_cache"] is None
    assert preview_calls == [("SV_TEST", False, False)]


def test_flow_preview_drift_check_defaults_allow_drift_false(
    monkeypatch, tmp_path: Path
) -> None:
    from qsync.cli import main

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")

    captured: dict[str, object] = {}

    def _fake_confirm_preview_drift(
        survey_id: str,
        dimension: str,
        *,
        allow_drift: bool,
        interactive: bool,
        update_cache,
        context=None,
    ):
        captured["allow_drift"] = allow_drift
        captured["interactive"] = interactive
        return None

    monkeypatch.setattr(
        "qsync.drift_check.confirm_preview_drift",
        _fake_confirm_preview_drift,
    )
    monkeypatch.setattr(
        "qsync.dimensions.flow.preview",
        lambda *_args, **_kwargs: [],
    )

    main(
        [
            "--root",
            str(tmp_path),
            "flow",
            "preview",
            "--survey-id",
            "SV_TEST",
            "--yes",
        ]
    )

    assert captured["allow_drift"] is False
    assert captured["interactive"] is False
