from __future__ import annotations

import csv
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch


def _prepare_account_root_workspace(root: Path) -> None:
    (root / ".qsync").mkdir(parents=True, exist_ok=True)
    (root / ".qsync" / "preferences.json").write_text(
        json.dumps(
            {"workspace_layout": "account_root_v1", "survey_cache_subdir": "cache"},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "accounts" / "default" / "js" / "core").mkdir(parents=True, exist_ok=True)
    (root / "accounts" / "default" / "state").mkdir(parents=True, exist_ok=True)
    (root / "accounts" / "default" / "inventory.csv").write_text(
        "id,name,focal\nSV_TEST123,Smoke Survey,TRUE\n",
        encoding="utf-8",
    )


def _write_smoke_cache(root: Path) -> Path:
    payload = {
        "result": {
            "Questions": {
                "QID1": {
                    "QuestionID": "QID1",
                    "DataExportTag": "intro_tag",
                    "QuestionJS": "// intro.js\nconsole.log('cached');",
                }
            },
            "Blocks": {
                "BL_1": {
                    "Type": "Standard",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
                }
            },
        }
    }
    cache_path = root / "accounts" / "default" / "state" / "Smoke__SV_TEST123.json"
    cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return cache_path


def _write_mapping(root: Path) -> Path:
    mapping_path = (
        root / "accounts" / "default" / "js" / "survey_qid_js_map.csv"
    )
    with mapping_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["js_file", "SV_TEST123-Smoke Survey"])
        writer.writeheader()
        writer.writerow({"js_file": "intro.js", "SV_TEST123-Smoke Survey": "QID1"})
    return mapping_path


def test_js_preview_uses_account_root_core_dir() -> None:
    from qsync.dimensions.js_preview import preview_differences

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _prepare_account_root_workspace(root)
        _write_smoke_cache(root)
        mapping_path = _write_mapping(root)
        (root / "accounts" / "default" / "js" / "core" / "intro.js").write_text(
            "// intro.js\nconsole.log('cached');",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"QSYNC_ROOT": str(root)}, clear=False):
            rows = preview_differences(
                "SV_TEST123",
                mapping_path,
                show_equal=True,
                detailed=False,
                interactive=False,
                verbose=False,
                check_drift=False,
            )

        assert rows
        match = [r for r in rows if r.qid == "QID1" and r.js_file == "intro.js"]
        assert match
        assert match[0].status == "match"


def test_js_sync_updates_cache_from_account_root_core_dir() -> None:
    from qsync.dimensions.js_sync import sync_js_with_cached

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _prepare_account_root_workspace(root)
        cache_path = _write_smoke_cache(root)
        mapping_path = _write_mapping(root)
        (root / "accounts" / "default" / "js" / "core" / "intro.js").write_text(
            "// intro.js\nconsole.log('local-new');",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"QSYNC_ROOT": str(root)}, clear=False):
            updates = sync_js_with_cached(
                "SV_TEST123",
                mapping_path,
                dry_run=False,
                include_match=True,
                allow_diff=True,
            )

        assert updates
        updated = json.loads(cache_path.read_text(encoding="utf-8"))
        q = updated["result"]["Questions"]["QID1"]
        assert "local-new" in q.get("QuestionJS", "")


def test_survey_prepare_ensure_workspace_dirs_account_root() -> None:
    from qsync.survey_prepare import ensure_workspace_dirs

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / ".qsync").mkdir(parents=True, exist_ok=True)
        (root / ".qsync" / "preferences.json").write_text(
            json.dumps(
                {"workspace_layout": "account_root_v1", "survey_cache_subdir": "cache"}
            ),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"QSYNC_ROOT": str(root)}, clear=False):
            ensure_workspace_dirs(root)

        assert (root / "accounts" / "default" / "js" / "core").exists()
        assert (root / "accounts" / "default" / "excel").exists()
        assert (root / "accounts" / "default").exists()

