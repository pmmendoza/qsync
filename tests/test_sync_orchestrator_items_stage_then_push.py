from __future__ import annotations

from pathlib import Path


def test_sync_items_stages_then_pushes_when_only_workbook_diffs_exist(
    monkeypatch, tmp_path: Path
) -> None:
    from qsync.sync_orchestrator import sync_dimension
    from qsync.sync_core import ApplyResult, PreviewChange

    # Ensure pending files are written under tmp_path, not the real repo.
    monkeypatch.setattr("qsync.pending_stage.resolve_root", lambda required=False: tmp_path)

    wb = tmp_path / "excel.xlsx"
    wb.write_bytes(b"dummy")

    # Make the workbook resolver point to our temp workbook.
    monkeypatch.setattr("qsync.workbook_resolver.WorkbookResolver.resolve", lambda self, survey_id: wb)

    # Pretend there is an unstaged workbook diff.
    fake_preview = lambda survey_id, xlsx_path, **kwargs: [
        PreviewChange(kind="question", qid="QID1", old_html="a", new_html="b")
    ]
    monkeypatch.setattr("qsync.dimensions.items.preview_changes", fake_preview)
    monkeypatch.setattr("qsync.dimensions.items_core.preview_changes", fake_preview)
    monkeypatch.setattr("qsync.dimensions.items.enforce_no_drift", lambda *a, **k: None)
    monkeypatch.setattr("qsync.dimensions.items.load_cached_survey", lambda *a, **k: type("S", (), {"payload": {"result": {"Questions": {}, "SurveyFlow": {"Flow": []}}}})())
    monkeypatch.setattr("qsync.dimensions.items._collect_embedded_data_changes", lambda *a, **k: [])

    pushed: dict[str, object] = {}

    def fake_push_staged_changes(*, survey_id: str, qids: list[str], **kwargs) -> None:
        pushed["survey_id"] = survey_id
        pushed["qids"] = list(qids)

    monkeypatch.setattr("qsync.dimensions.items.push_staged_changes", fake_push_staged_changes)

    result = sync_dimension(
        survey_id="SV_TEST",
        dimension="items",
        interactive=False,
        force_live=False,
        force_preview=False,
        auto_yes=True,
        allow_drift=False,
        skip_publish=False,
        scope=None,
    )

    assert result.success is True
    assert result.applied_changes is True
    assert pushed["survey_id"] == "SV_TEST"
    assert pushed["qids"] == ["QID1"]
