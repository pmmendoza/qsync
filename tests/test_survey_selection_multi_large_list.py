from __future__ import annotations


def test_pick_survey_ids_large_list_allows_full_list_then_multi_select(
    monkeypatch,
) -> None:
    from qsync.survey_selection import pick_survey_ids_from_records

    records = [
        {"id": f"SV_{idx:03d}", "name": f"Survey {idx}", "isActive": True}
        for idx in range(1, 80)
    ]

    monkeypatch.setattr(
        "qsync.interactive_menu.autocomplete_from_list",
        lambda **_kwargs: "→ Continue to multi-select (show full list)",
    )

    def _pick_many(*, choices, **_kwargs):
        assert len(choices) == len(records)
        return [choices[0], choices[1]]

    monkeypatch.setattr(
        "qsync.interactive_menu.multi_select_from_list",
        _pick_many,
    )

    selected = pick_survey_ids_from_records(
        message="Select surveys:",
        records=records,
        allow_multiple=True,
    )

    assert selected == ["SV_001", "SV_002"]


def test_pick_survey_id_large_list_allows_full_list_then_single_select(
    monkeypatch,
) -> None:
    from qsync.survey_selection import pick_survey_id_from_records

    records = [
        {"id": f"SV_{idx:03d}", "name": f"Survey {idx}", "isActive": True}
        for idx in range(1, 80)
    ]

    sentinel = "→ Continue to full list (show all)"

    def _autocomplete(**kwargs):
        assert kwargs["choices"][0] == sentinel
        return sentinel

    monkeypatch.setattr(
        "qsync.interactive_menu.autocomplete_from_list",
        _autocomplete,
    )

    def _select_one(*, choices, **_kwargs):
        assert any(getattr(choice, "value", None) == "SV_001" for choice in choices)
        return "SV_001"

    monkeypatch.setattr(
        "qsync.interactive_menu.select_from_list",
        _select_one,
    )

    selected = pick_survey_id_from_records(
        message="Select survey:",
        records=records,
    )

    assert selected == "SV_001"
