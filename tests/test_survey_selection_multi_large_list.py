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

