from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.workspace_helpers import ensure_qsync_workspace, write_inventory_csv


def test_sync_accepts_repeatable_survey_id_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.cli import main

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")

    called: list[str] = []

    def _fake_sync_survey(*, survey_id: str, **_kwargs):
        called.append(survey_id)
        return SimpleNamespace(success=True)

    monkeypatch.setattr("qsync.sync_orchestrator.sync_survey", _fake_sync_survey)
    monkeypatch.setattr(
        "qsync.sync_orchestrator.sync_focal_surveys",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("focal sync should not run")),
    )

    main(
        [
            "--root",
            str(tmp_path),
            "sync",
            "--survey-id",
            "SV_A",
            "--survey-id",
            "SV_B",
            "--yes",
        ]
    )

    assert called == ["SV_A", "SV_B"]


def test_sync_accepts_comma_separated_survey_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.cli import main

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")

    called: list[str] = []

    def _fake_sync_survey(*, survey_id: str, **_kwargs):
        called.append(survey_id)
        return SimpleNamespace(success=True)

    monkeypatch.setattr("qsync.sync_orchestrator.sync_survey", _fake_sync_survey)
    monkeypatch.setattr(
        "qsync.sync_orchestrator.sync_focal_surveys",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("focal sync should not run")),
    )

    main(
        [
            "--root",
            str(tmp_path),
            "sync",
            "--survey-id",
            "SV_A,SV_B",
            "--yes",
        ]
    )

    assert called == ["SV_A", "SV_B"]


def test_sync_without_survey_id_uses_focal_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.cli import main

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")

    called = {"focal": 0}

    def _fake_sync_focal_surveys(**_kwargs) -> bool:
        called["focal"] += 1
        return True

    monkeypatch.setattr("qsync.sync_orchestrator.sync_focal_surveys", _fake_sync_focal_surveys)
    monkeypatch.setattr(
        "qsync.sync_orchestrator.sync_survey",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("single-survey sync should not run")),
    )

    main(["--root", str(tmp_path), "sync", "--yes"])

    assert called["focal"] == 1
