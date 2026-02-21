from __future__ import annotations

import subprocess
from pathlib import Path


def _run_make(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["make", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout + result.stderr


def test_make_fullsync_delegates_to_qsync_sync_with_single_survey() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    out = _run_make(
        repo_root,
        "fullsync",
        "QSYNC=echo",
        "SURVEY=SV_TEST",
        "YES=1",
    )
    assert "sync --survey-id SV_TEST --yes" in out


def test_make_fullsync_maps_legacy_flags_to_sync_flags() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    out = _run_make(
        repo_root,
        "fullsync",
        "QSYNC=echo",
        "SURVEYS=SV_A SV_B",
        "LIVE=1",
        "PREVIEW_ITEMS=1",
        "PER_DIMENSION=1",
        "SKIP_PUBLISH=1",
        "REFRESH_INVENTORY=1",
        "DIMENSIONS=items,translations",
    )
    assert "--survey-id SV_A" in out
    assert "--survey-id SV_B" in out
    assert "--force-live" in out
    assert "--force-preview" in out
    assert "--per-dimension" in out
    assert "--skip-publish" in out
    assert "--refresh-inventory" in out
    assert "--dimensions items,translations" in out
