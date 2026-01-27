from __future__ import annotations


def test_items_push_maps_interactive_to_safeguards_auto_yes(monkeypatch) -> None:
    from qsync.sync_core import push_staged_changes

    class DummySurvey:
        payload = {"result": {"Questions": {}, "SurveyFlow": {"Flow": []}}}

        def save(self) -> None:
            return

    # Avoid touching disk/network.
    monkeypatch.setattr(
        "qsync.dimensions.items_core.ensure_backup", lambda survey_id: None
    )
    monkeypatch.setattr(
        "qsync.dimensions.items_core.load_cached_survey",
        lambda survey_id: DummySurvey(),
    )
    monkeypatch.setattr(
        "qsync.dimensions.items_core.push_questions", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "qsync.dimensions.items_core.push_survey_flow", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "qsync.dimensions.items_core.auto_publish_after_push", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "qsync.dimensions.items_core.enforce_no_drift", lambda *a, **k: None
    )

    captured = {}

    def fake_enforce(config):
        captured["auto_yes"] = getattr(config, "auto_yes", None)
        captured["dimension"] = getattr(config, "dimension", None)
        return type("R", (), {"warnings": [], "blocked": False})()

    monkeypatch.setattr(
        "qsync.dimensions.items_core.enforce_push_safeguards", fake_enforce
    )

    push_staged_changes(
        survey_id="SV_TEST",
        qids=["QID1"],
        interactive=False,
        publish=False,
    )

    assert captured["dimension"] == "items"
    assert captured["auto_yes"] is True
