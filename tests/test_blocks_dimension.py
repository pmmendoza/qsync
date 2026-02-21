from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

from qsync.dimensions import blocks
from qsync.pending_stage import load_pending


def _payload() -> dict:
    return {
        "result": {
            "SurveyID": "SV_TEST",
            "SurveyName": "Blocks Demo",
            "Blocks": {
                "BL_A": {
                    "Type": "Standard",
                    "Description": "Main",
                    "BlockElements": [
                        {"Type": "Question", "QuestionID": "QID1"},
                        {"Type": "Question", "QuestionID": "QID2"},
                    ],
                },
                "BL_B": {
                    "Type": "Standard",
                    "Description": "Second",
                    "BlockElements": [
                        {"Type": "Question", "QuestionID": "QID3"},
                    ],
                },
                "BL_TRASH": {
                    "Type": "Trash",
                    "Description": "Trash",
                    "BlockElements": [],
                },
            },
            "SurveyFlow": {
                "Type": "Root",
                "Flow": [
                    {"Type": "Block", "ID": "BL_A"},
                    {"Type": "Block", "ID": "BL_B"},
                ],
            },
        }
    }


def test_pull_writes_blocks_surface(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("QSYNC_ROOT", str(tmp_path))
    monkeypatch.setenv("QSYNC_WORKSPACE_LAYOUT", "legacy")

    monkeypatch.setattr(
        "qsync.qualtrics_client.refresh_survey_cache",
        lambda survey_id: (SimpleNamespace(payload=_payload()), None),
    )

    yaml_path = blocks.pull("SV_TEST")

    assert yaml_path.exists()
    baseline_path = yaml_path.with_name("blocks_baseline.json")
    assert baseline_path.exists()
    text = yaml_path.read_text(encoding="utf-8")
    assert "BL_A" in text
    assert "QID1" in text


def test_move_qid_stage_creates_pending(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("QSYNC_ROOT", str(tmp_path))
    monkeypatch.setenv("QSYNC_WORKSPACE_LAYOUT", "legacy")

    monkeypatch.setattr(
        "qsync.qualtrics_client.refresh_survey_cache",
        lambda survey_id: (SimpleNamespace(payload=_payload()), None),
    )
    monkeypatch.setattr(
        "qsync.qualtrics_client.fetch_survey_definition_live",
        lambda survey_id: _payload(),
    )

    survey_id = "SV_TEST"
    blocks.pull(survey_id)

    result = blocks.move_qid(
        survey_id,
        qids=["QID2"],
        target_block_id="BL_A",
        before_qid="QID1",
    )
    assert result["block_id"] == "BL_A"

    changes = blocks.detect_changes(survey_id)
    assert changes.has_changes is True
    assert changes.status_kind == "unstaged"

    staged = blocks.stage(survey_id, allow_drift=False, interactive=False)
    assert staged is True

    pending = load_pending(survey_id, "blocks")
    assert pending is not None
    assert list(getattr(pending.payload, "block_ids", [])) == ["BL_A"]


def test_remove_qid_moves_to_trash(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("QSYNC_ROOT", str(tmp_path))
    monkeypatch.setenv("QSYNC_WORKSPACE_LAYOUT", "legacy")

    monkeypatch.setattr(
        "qsync.qualtrics_client.refresh_survey_cache",
        lambda survey_id: (SimpleNamespace(payload=_payload()), None),
    )

    survey_id = "SV_TEST"
    yaml_path = blocks.pull(survey_id)

    result = blocks.remove_qid(survey_id, qids=["QID3"], move_to_trash=True)
    assert result["moved_to_trash"] is True
    assert result["trash_block_id"] == "BL_TRASH"

    text = yaml_path.read_text(encoding="utf-8")
    assert "BL_TRASH" in text
    assert "QID3" in text


def test_preview_verbose_renders_unified_diff(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("QSYNC_ROOT", str(tmp_path))
    monkeypatch.setenv("QSYNC_WORKSPACE_LAYOUT", "legacy")

    monkeypatch.setattr(
        "qsync.qualtrics_client.refresh_survey_cache",
        lambda survey_id: (SimpleNamespace(payload=_payload()), None),
    )

    survey_id = "SV_TEST"
    blocks.pull(survey_id)
    blocks.move_qid(
        survey_id,
        qids=["QID2"],
        target_block_id="BL_A",
        before_qid="QID1",
    )

    changes = blocks.preview(survey_id, verbose=True)
    assert len(changes) == 1
    out = capsys.readouterr().out
    assert "--- baseline" in out
    assert "+++ blocks.yaml" in out
    assert "@@" in out
    assert "-[000] QID1" in out or "-[001] QID2" in out


def test_push_warns_when_content_changes_since_stage_same_block_ids(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("QSYNC_ROOT", str(tmp_path))
    monkeypatch.setenv("QSYNC_WORKSPACE_LAYOUT", "legacy")

    payload = _payload()
    payload["result"]["Blocks"]["BL_A"]["BlockElements"].append(
        {"Type": "Question", "QuestionID": "QID4"}
    )

    monkeypatch.setattr(
        "qsync.qualtrics_client.refresh_survey_cache",
        lambda survey_id: (SimpleNamespace(payload=copy.deepcopy(payload)), None),
    )
    monkeypatch.setattr(
        "qsync.qualtrics_client.fetch_survey_definition_live",
        lambda survey_id: copy.deepcopy(payload),
    )
    monkeypatch.setattr("qsync.qualtrics_client.ensure_backup", lambda survey_id: None)
    monkeypatch.setattr(
        "qsync.dimensions.blocks.enforce_push_safeguards",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "qsync.dimensions.blocks.get_client_config",
        lambda: ("example.qualtrics.com", {"X-API-TOKEN": "x"}),
    )
    monkeypatch.setattr(
        "qsync.qualtrics_client.publish_survey_definition",
        lambda *_a, **_k: None,
    )

    writes: list[tuple[str, str]] = []

    def _send_api_request(*, method: str, path: str, **kwargs):
        writes.append((method, path))
        return SimpleNamespace(json=lambda: {"result": {"ok": True}})

    monkeypatch.setattr("qsync.dimensions.blocks.send_api_request", _send_api_request)

    survey_id = "SV_TEST"
    blocks.pull(survey_id)
    blocks.move_qid(
        survey_id,
        qids=["QID4"],
        target_block_id="BL_A",
        before_qid="QID1",
    )
    assert blocks.stage(survey_id, allow_drift=False, interactive=False) is True

    # Change staged content but keep the same changed block ID set (BL_A).
    blocks.move_qid(
        survey_id,
        qids=["QID2"],
        target_block_id="BL_A",
        before_qid="QID1",
    )

    ok = blocks.push(
        survey_id,
        interactive=False,
        auto_yes=True,
        skip_publish=True,
    )
    assert ok is True
    out = capsys.readouterr().out
    assert "block content changed since staging" in out
    assert any(path.endswith("/blocks/BL_A") for _, path in writes)
