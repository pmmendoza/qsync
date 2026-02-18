from __future__ import annotations

import pytest


def test_resolve_allow_externally_managed_supports_all_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qsync.dimensions import items_core

    monkeypatch.setenv("QSYNC_ITEMS_ALLOW_EXTERNALLY_MANAGED_QIDS", "all")
    allow = items_core._resolve_allow_externally_managed_qids(survey_id="SV_1")

    assert "*" in allow
    assert (
        items_core._should_skip_externally_managed(
            qid="QID99",
            data_export_tag="newsmem_recognition",
            externally_managed_by=None,
            allow_qids=allow,
        )
        is False
    )


def test_scoped_all_applies_only_to_matching_survey(monkeypatch: pytest.MonkeyPatch) -> None:
    from qsync.dimensions import items_core

    monkeypatch.setenv(
        "QSYNC_ITEMS_ALLOW_EXTERNALLY_MANAGED_QIDS",
        "SV_2:all SV_1:QID7",
    )

    allow_sv1 = items_core._resolve_allow_externally_managed_qids(survey_id="SV_1")
    allow_sv2 = items_core._resolve_allow_externally_managed_qids(survey_id="SV_2")

    assert "QID7" in allow_sv1
    assert "*" not in allow_sv1
    assert "*" in allow_sv2
