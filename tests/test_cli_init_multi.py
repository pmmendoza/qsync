from __future__ import annotations

from pathlib import Path

import pytest

from tests.workspace_helpers import ensure_qsync_workspace, write_inventory_csv


def test_init_accepts_repeatable_survey_id_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.cli import main

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")

    calls: list[tuple[str, Path, list[str] | None, bool]] = []

    monkeypatch.setattr(
        "qsync.cli._default_xlsx_path",
        lambda survey_id: tmp_path / "excel" / f"{survey_id}.xlsx",
    )

    def _fake_init(
        survey_id: str,
        xlsx_path: Path,
        *,
        languages=None,
        prune_orphans: bool = False,
    ) -> None:
        calls.append((survey_id, xlsx_path, languages, prune_orphans))

    monkeypatch.setattr("qsync.sync_core.init_survey_to_excel", _fake_init)

    main(
        [
            "--root",
            str(tmp_path),
            "init",
            "--survey-id",
            "SV_A",
            "--survey-id",
            "SV_B",
        ]
    )

    assert calls == [
        ("SV_A", tmp_path / "excel" / "SV_A.xlsx", None, False),
        ("SV_B", tmp_path / "excel" / "SV_B.xlsx", None, False),
    ]


def test_init_rejects_xlsx_with_multiple_survey_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.cli import main

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")

    monkeypatch.setattr("qsync.sync_core.init_survey_to_excel", lambda *_a, **_k: None)

    with pytest.raises(SystemExit, match="--xlsx cannot be used with multiple"):
        main(
            [
                "--root",
                str(tmp_path),
                "init",
                "--survey-id",
                "SV_A,SV_B",
                "--xlsx",
                str(tmp_path / "excel" / "shared.xlsx"),
            ]
        )
