from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.workspace_helpers import ensure_qsync_workspace


def _write_stale_inventory(root: Path, *, age_minutes: float = 45.0) -> Path:
    ensure_qsync_workspace(root)
    inventory = root / "surveys" / "inventory.csv"
    inventory.write_text(
        "id,name,focal,generated_at\nSV_A,Survey A,TRUE,2026-02-21T00:00:00Z\n",
        encoding="utf-8",
    )
    stale_ts = time.time() - (age_minutes * 60.0)
    os.utime(inventory, (stale_ts, stale_ts))
    return inventory


def test_sync_auto_refreshes_stale_inventory_before_focal_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from qsync.cli import main

    _write_stale_inventory(tmp_path, age_minutes=75)

    seen: dict[str, object] = {}

    monkeypatch.setattr(
        "qsync.config.get_client_config",
        lambda env=None: ("example.qualtrics.com", {"X-API-TOKEN": "token"}),
    )

    def _fake_refresh(base_url: str, headers: dict, **kwargs):
        seen["base_url"] = base_url
        seen["headers"] = headers
        seen.update(kwargs)
        return ([], [])

    monkeypatch.setattr("qsync.survey_inventory.refresh_inventory", _fake_refresh)
    monkeypatch.setattr(
        "qsync.sync_orchestrator.sync_focal_surveys",
        lambda **_kwargs: True,
    )

    main(["--root", str(tmp_path), "sync", "--all-focal", "--yes"])

    assert seen["base_url"] == "example.qualtrics.com"
    assert seen["survey_filter"] is None
    assert seen["counts_scope"] == "focal"


def test_sync_refresh_inventory_targets_selected_survey_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from qsync.cli import main

    _write_stale_inventory(tmp_path, age_minutes=75)

    seen: dict[str, object] = {}

    monkeypatch.setattr(
        "qsync.config.get_client_config",
        lambda env=None: ("example.qualtrics.com", {"X-API-TOKEN": "token"}),
    )

    def _fake_refresh(base_url: str, headers: dict, **kwargs):
        seen["base_url"] = base_url
        seen["headers"] = headers
        seen.update(kwargs)
        return ([], [])

    monkeypatch.setattr("qsync.survey_inventory.refresh_inventory", _fake_refresh)
    monkeypatch.setattr(
        "qsync.sync_orchestrator.sync_survey",
        lambda **_kwargs: SimpleNamespace(success=True),
    )

    main(
        [
            "--root",
            str(tmp_path),
            "sync",
            "--survey-id",
            "SV_A,SV_B",
            "--yes",
        ]
    )

    assert seen["survey_filter"] == ["SV_A", "SV_B"]
    assert seen["counts_scope"] is None


def test_sync_no_refresh_inventory_skips_preflight_refresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from qsync.cli import main

    _write_stale_inventory(tmp_path, age_minutes=75)

    def _never_refresh(*args, **kwargs):
        raise AssertionError("refresh_inventory should not be called")

    monkeypatch.setattr("qsync.survey_inventory.refresh_inventory", _never_refresh)
    monkeypatch.setattr(
        "qsync.sync_orchestrator.sync_focal_surveys",
        lambda **_kwargs: True,
    )

    main(
        [
            "--root",
            str(tmp_path),
            "sync",
            "--all-focal",
            "--yes",
            "--no-refresh-inventory",
        ]
    )

    captured = capsys.readouterr()
    assert "--no-refresh-inventory" in captured.err


def test_sync_refresh_inventory_flag_fails_fast_on_refresh_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from qsync.cli import main

    _write_stale_inventory(tmp_path, age_minutes=1)

    monkeypatch.setattr(
        "qsync.config.get_client_config",
        lambda env=None: ("example.qualtrics.com", {"X-API-TOKEN": "token"}),
    )

    def _broken_refresh(*_args, **_kwargs):
        raise RuntimeError("refresh failed")

    monkeypatch.setattr("qsync.survey_inventory.refresh_inventory", _broken_refresh)

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "--root",
                str(tmp_path),
                "sync",
                "--refresh-inventory",
                "--all-focal",
                "--yes",
            ]
        )

    assert exc.value.code == 1

def test_sync_help_includes_inventory_refresh_flags() -> None:
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "qsync.cli", "sync", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    help_text = result.stdout
    assert "--refresh-inventory" in help_text
    assert "--no-refresh-inventory" in help_text
