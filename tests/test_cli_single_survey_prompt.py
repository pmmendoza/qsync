from __future__ import annotations

import pytest


def test_single_survey_prompt_uses_single_select(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qsync import cli

    calls = {"single": 0, "multi": 0}

    def _fake_prompt_for_survey_id(*, allow_all_surveys: bool, interactive: bool) -> str:
        calls["single"] += 1
        assert allow_all_surveys is True
        return "SV_SINGLE"

    def _fake_prompt_for_survey_ids(
        *, allow_all_surveys: bool, interactive: bool
    ) -> list[str]:
        calls["multi"] += 1
        return ["SV_A", "SV_B"]

    monkeypatch.setattr(
        "qsync.survey_inventory.prompt_for_survey_id",
        _fake_prompt_for_survey_id,
    )
    monkeypatch.setattr(
        "qsync.survey_inventory.prompt_for_survey_ids",
        _fake_prompt_for_survey_ids,
    )

    selected = cli._prompt_for_survey_id_if_needed(None, allow_all_surveys=True)
    assert selected == "SV_SINGLE"
    assert calls == {"single": 1, "multi": 0}


def test_single_survey_prompt_rejects_multiple_explicit_ids() -> None:
    from qsync import cli

    with pytest.raises(SystemExit, match="accepts only one --survey-id value"):
        cli._prompt_for_survey_id_if_needed(["SV_A", "SV_B"])
