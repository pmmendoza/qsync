from __future__ import annotations

import json
from pathlib import Path

from tests.workspace_helpers import ensure_qsync_workspace, write_inventory_csv


def test_survey_slugged_key_uses_inventory_name(tmp_path: Path) -> None:
    from qsync.survey_naming import survey_slugged_key

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\nSV_TEST,Demo Survey,false\n")

    assert survey_slugged_key("SV_TEST", root=tmp_path) == "Demo_Survey-SV_TEST"


def test_translation_dir_migrates_legacy_survey_id_folder(tmp_path: Path) -> None:
    from qsync.translations_paths import translation_dir, translations_root

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\nSV_TEST,Demo Survey,false\n")

    legacy_dir = translations_root(tmp_path) / "SV_TEST"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / "EN.json").write_text("{}", encoding="utf-8")

    resolved = translation_dir("SV_TEST", root=tmp_path)

    assert resolved.name == "Demo_Survey-SV_TEST"
    assert resolved.exists()
    assert (resolved / "EN.json").exists()
    assert not legacy_dir.exists()


def test_translation_key_snapshot_path_migrates_legacy_location(tmp_path: Path) -> None:
    from qsync.translations_paths import (
        translation_key_snapshot_path,
        translations_root,
    )

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\nSV_TEST,Demo Survey,false\n")

    legacy_path = (
        tmp_path
        / "surveys"
        / "translation_key_snapshots"
        / "SV_TEST"
        / "before_EN.json"
    )
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"keys":["QID1"]}', encoding="utf-8")

    resolved = translation_key_snapshot_path("SV_TEST", "before", "EN", root=tmp_path)
    expected = (
        translations_root(tmp_path)
        / "Demo_Survey-SV_TEST"
        / "key_snapshots"
        / "before_EN.json"
    )

    assert resolved == expected
    assert resolved.exists()
    assert resolved.read_text(encoding="utf-8") == '{"keys":["QID1"]}'
    assert not legacy_path.exists()


def test_pending_stage_save_migrates_legacy_filename(tmp_path: Path, monkeypatch) -> None:
    import qsync.pending_stage as pending_stage

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\nSV_TEST,Demo Survey,false\n")
    monkeypatch.setattr(pending_stage, "resolve_root", lambda required=False: tmp_path)

    pending_dir = tmp_path / "surveys" / "pending" / "js"
    pending_dir.mkdir(parents=True, exist_ok=True)
    legacy_path = pending_dir / "SV_TEST.json"
    legacy_path.write_text("{}", encoding="utf-8")

    record = pending_stage.PendingStagedChanges(
        survey_id="SV_TEST",
        dimension="js",
        payload=pending_stage.JsPendingPayload(
            entries=[{"qid": "QID1", "js_file": "core.js", "status": "updated"}]
        ),
    )
    pending_stage.save_pending(record)

    expected_path = pending_dir / "Demo_Survey-SV_TEST.json"
    assert expected_path.exists()
    assert not legacy_path.exists()

    loaded = pending_stage.load_pending("SV_TEST", "js")
    assert loaded is not None
    assert loaded.survey_id == "SV_TEST"
    assert loaded.dimension == "js"


def test_slice_batch_manifest_uses_slugged_source_prefix(tmp_path: Path) -> None:
    from qsync.survey_slice_language import write_batch_manifest

    ensure_qsync_workspace(tmp_path)
    path = write_batch_manifest(
        tmp_path,
        source_survey_id="SV_SRC",
        source_survey_name="Source Survey",
        source_base_language="EN",
        slices=[{"target_language": "DE", "new_survey_id": "SV_DE"}],
        qsync_version="0.0.0",
    )

    assert path.name.startswith("batch__Source_Survey-SV_SRC__")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["source_survey_id"] == "SV_SRC"
