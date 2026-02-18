from __future__ import annotations

from pathlib import Path

import pytest

from tests.workspace_helpers import ensure_qsync_workspace, write_inventory_csv


def test_items_preview_prompts_for_survey_id_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.cli import main

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")

    prompt_calls: list[tuple[object, bool]] = []
    preview_calls: list[str] = []

    def _fake_prompt(value: object, *, allow_all_surveys: bool = False) -> str:
        prompt_calls.append((value, allow_all_surveys))
        return "SV_TEST"

    def _fake_preview_changes(survey_id: str, *_args, **_kwargs):
        preview_calls.append(survey_id)
        return []

    monkeypatch.setattr("qsync.cli._prompt_for_survey_id_if_needed", _fake_prompt)
    monkeypatch.setattr("qsync.sync_core.preview_changes", _fake_preview_changes)
    monkeypatch.setattr("qsync.drift_check.confirm_preview_drift", lambda **_kwargs: None)

    main(["--root", str(tmp_path), "items", "preview"])

    assert prompt_calls == [(None, False)]
    assert preview_calls == ["SV_TEST"]


def test_items_stage_prompts_for_survey_id_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.cli import main

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")

    prompt_calls: list[tuple[object, bool]] = []
    staged_survey_ids: list[str] = []
    cleared: list[tuple[str, str]] = []

    def _fake_prompt(value: object, *, allow_all_surveys: bool = False) -> str:
        prompt_calls.append((value, allow_all_surveys))
        return "SV_STAGE"

    def _fake_build_pending(survey_id: str, *_args, **_kwargs):
        staged_survey_ids.append(survey_id)
        return None

    def _fake_clear_pending(survey_id: str, dimension: str) -> None:
        cleared.append((survey_id, dimension))

    monkeypatch.setattr("qsync.cli._prompt_for_survey_id_if_needed", _fake_prompt)
    monkeypatch.setattr(
        "qsync.dimensions.items._build_pending_payload_from_workbook",
        _fake_build_pending,
    )
    monkeypatch.setattr("qsync.pending_stage.clear_pending", _fake_clear_pending)

    main(["--root", str(tmp_path), "items", "stage", "--yes"])

    assert prompt_calls == [(None, False)]
    assert staged_survey_ids == ["SV_STAGE"]
    assert cleared == [("SV_STAGE", "items")]
