from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest


def _write_inventory(root: Path, *, focal_ids: set[str]) -> None:
    surveys_dir = root / "surveys"
    surveys_dir.mkdir(parents=True, exist_ok=True)
    with (surveys_dir / "inventory.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["id", "name", "focal", "lastModified"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "id": "SV_FOCAL",
                "name": "Focal Survey",
                "focal": "TRUE" if "SV_FOCAL" in focal_ids else "FALSE",
                "lastModified": "2026-02-21T10:00:00Z",
            }
        )
        writer.writerow(
            {
                "id": "SV_OTHER",
                "name": "Other Survey",
                "focal": "TRUE" if "SV_OTHER" in focal_ids else "FALSE",
                "lastModified": "2026-02-20T10:00:00Z",
            }
        )


def _write_cached_survey(
    root: Path, *, survey_id: str, label: str, qid: str, js_file: str
) -> None:
    payload = {
        "result": {
            "Questions": {
                qid: {
                    "QuestionID": qid,
                    "QuestionJS": f"// {js_file}\nconsole.log('{survey_id}');",
                }
            },
            "Blocks": {
                "BL_1": {
                    "Type": "Standard",
                    "BlockElements": [{"Type": "Question", "QuestionID": qid}],
                }
            },
        }
    }
    surveys_dir = root / "surveys"
    surveys_dir.mkdir(parents=True, exist_ok=True)
    (surveys_dir / f"{label}__{survey_id}.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _write_core_js(root: Path) -> None:
    core_dir = root / "survey_js" / "core"
    core_dir.mkdir(parents=True, exist_ok=True)
    (core_dir / "intro.js").write_text("// intro.js\n", encoding="utf-8")


def test_rebuild_mapping_defaults_to_focal_surveys(monkeypatch, tmp_path: Path) -> None:
    from qsync.js_mapping import rebuild_mapping

    root = tmp_path
    monkeypatch.setenv("QSYNC_ROOT", str(root))
    _write_inventory(root, focal_ids={"SV_FOCAL"})
    _write_core_js(root)
    _write_cached_survey(
        root,
        survey_id="SV_FOCAL",
        label="FocalLabel",
        qid="QID1",
        js_file="intro.js",
    )
    _write_cached_survey(
        root,
        survey_id="SV_OTHER",
        label="OtherLabel",
        qid="QID2",
        js_file="intro.js",
    )

    mapping_path = root / "survey_js" / "survey_qid_js_map.csv"
    rebuild_mapping(mapping_path)

    with mapping_path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == ["js_file", "SV_FOCAL-FocalLabel"]
        rows = list(reader)

    intro_row = next(row for row in rows if row["js_file"] == "intro.js")
    assert intro_row["SV_FOCAL-FocalLabel"] == "QID1"


def test_rebuild_mapping_all_surveys_includes_non_focal(
    monkeypatch, tmp_path: Path
) -> None:
    from qsync.js_mapping import rebuild_mapping

    root = tmp_path
    monkeypatch.setenv("QSYNC_ROOT", str(root))
    _write_inventory(root, focal_ids={"SV_FOCAL"})
    _write_core_js(root)
    _write_cached_survey(
        root,
        survey_id="SV_FOCAL",
        label="FocalLabel",
        qid="QID1",
        js_file="intro.js",
    )
    _write_cached_survey(
        root,
        survey_id="SV_OTHER",
        label="OtherLabel",
        qid="QID2",
        js_file="intro.js",
    )

    mapping_path = root / "survey_js" / "survey_qid_js_map.csv"
    rebuild_mapping(mapping_path, focal_only=False)

    with mapping_path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == [
            "js_file",
            "SV_FOCAL-FocalLabel",
            "SV_OTHER-OtherLabel",
        ]
        rows = list(reader)

    intro_row = next(row for row in rows if row["js_file"] == "intro.js")
    assert intro_row["SV_FOCAL-FocalLabel"] == "QID1"
    assert intro_row["SV_OTHER-OtherLabel"] == "QID2"


def test_rebuild_mapping_focal_only_requires_focal_inventory(
    monkeypatch, tmp_path: Path
) -> None:
    from qsync.js_mapping import rebuild_mapping

    root = tmp_path
    monkeypatch.setenv("QSYNC_ROOT", str(root))
    _write_inventory(root, focal_ids=set())
    _write_core_js(root)
    _write_cached_survey(
        root,
        survey_id="SV_FOCAL",
        label="FocalLabel",
        qid="QID1",
        js_file="intro.js",
    )

    mapping_path = root / "survey_js" / "survey_qid_js_map.csv"
    with pytest.raises(RuntimeError, match="No focal surveys found in inventory.csv"):
        rebuild_mapping(mapping_path)
