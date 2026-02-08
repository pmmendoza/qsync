from __future__ import annotations

import json
import os


def test_load_focal_snapshot_refreshes_from_inventory_when_newer(tmp_path, monkeypatch):
    import qsync.survey_inventory as survey_inventory

    inventory_path = tmp_path / "inventory.csv"
    snapshot_path = tmp_path / ".focal_snapshot.json"

    inventory_path.write_text(
        "id,name,focal\nSV_1,One,true\nSV_2,Two,false\n",
        encoding="utf-8",
    )
    snapshot_path.write_text(
        json.dumps({"SV_1": False, "SV_2": True}, indent=2),
        encoding="utf-8",
    )

    # Force deterministic mtimes so inventory appears newer than the snapshot.
    os.utime(snapshot_path, (1, 1))
    os.utime(inventory_path, (2, 2))

    monkeypatch.setattr(survey_inventory, "FOCAL_SNAPSHOT", snapshot_path)
    monkeypatch.setattr(
        survey_inventory,
        "resolve_inventory_csv_path",
        lambda *, required=False: inventory_path,
    )

    snapshot = survey_inventory.load_focal_snapshot()

    assert snapshot == {"SV_1": True, "SV_2": False}
    assert json.loads(snapshot_path.read_text(encoding="utf-8")) == {
        "SV_1": True,
        "SV_2": False,
    }
