from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.workspace_helpers import ensure_qsync_workspace


def test_eos_repair_batch_defaults_to_focal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.cli import main

    ensure_qsync_workspace(tmp_path)

    called: list[str] = []

    monkeypatch.setattr(
        "qsync.config.get_client_config",
        lambda env=None: ("target.qualtrics.test", {"X-API-TOKEN": "token"}),
    )
    monkeypatch.setattr(
        "qsync.survey_inventory.get_focal_survey_ids",
        lambda: ["SV_FOCAL_A", "SV_FOCAL_B"],
    )

    def _fake_pull_eos_messages(
        *,
        survey_id: str,
        base_url: str,
        headers: dict[str, str],
        include_backups_scan: bool,
        check_drift: bool = False,
        refs=None,
        action: str = "",
    ):
        del base_url, headers, refs, action
        called.append(survey_id)
        return SimpleNamespace(pulled_paths=[], warnings=[])

    monkeypatch.setattr(
        "qsync.eos_messages.pull_eos_messages_best_effort",
        _fake_pull_eos_messages,
    )

    main(["--root", str(tmp_path), "eos", "repair", "--batch"])

    assert called == ["SV_FOCAL_A", "SV_FOCAL_B"]


def test_eos_repair_batch_all_surveys_uses_api_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.cli import main

    ensure_qsync_workspace(tmp_path)

    called: list[str] = []

    monkeypatch.setattr(
        "qsync.config.get_client_config",
        lambda env=None: ("target.qualtrics.test", {"X-API-TOKEN": "token"}),
    )
    monkeypatch.setattr(
        "qsync.survey_inventory.fetch_surveys",
        lambda base, headers: [
            {"id": "SV_ALL_A"},
            {"id": "SV_ALL_B"},
        ],
    )
    monkeypatch.setattr(
        "qsync.survey_inventory.get_focal_survey_ids",
        lambda: (_ for _ in ()).throw(AssertionError("focal path should not be used")),
    )

    def _fake_pull_eos_messages(
        *,
        survey_id: str,
        base_url: str,
        headers: dict[str, str],
        include_backups_scan: bool,
        check_drift: bool = False,
        refs=None,
        action: str = "",
    ):
        del base_url, headers, refs, action
        called.append(survey_id)
        return SimpleNamespace(pulled_paths=[], warnings=[])

    monkeypatch.setattr(
        "qsync.eos_messages.pull_eos_messages_best_effort",
        _fake_pull_eos_messages,
    )

    main(["--root", str(tmp_path), "eos", "repair", "--batch", "--all-surveys"])

    assert called == ["SV_ALL_A", "SV_ALL_B"]


def test_eos_repair_batch_all_surveys_conflicts_with_survey_id(
    tmp_path: Path,
) -> None:
    from qsync.cli import main

    ensure_qsync_workspace(tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "--root",
                str(tmp_path),
                "eos",
                "repair",
                "--batch",
                "--all-surveys",
                "--survey-id",
                "SV_X",
            ]
        )

    assert excinfo.value.code == 2
