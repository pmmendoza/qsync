from __future__ import annotations

import json
from pathlib import Path


def test_resolve_survey_cache_dir_falls_back_to_surveys(
    tmp_path: Path, monkeypatch
) -> None:
    from qsync.config import resolve_survey_cache_dir, resolve_survey_cache_subdir

    monkeypatch.delenv("QSYNC_SURVEY_CACHE_SUBDIR", raising=False)
    surveys = tmp_path / "surveys"
    surveys.mkdir(parents=True, exist_ok=True)

    assert resolve_survey_cache_subdir(root=tmp_path) == "caches"
    assert resolve_survey_cache_dir(root=tmp_path) == surveys.resolve()


def test_resolve_survey_cache_dir_uses_existing_default_subdir(
    tmp_path: Path, monkeypatch
) -> None:
    from qsync.config import resolve_survey_cache_dir

    monkeypatch.delenv("QSYNC_SURVEY_CACHE_SUBDIR", raising=False)
    cache_dir = tmp_path / "surveys" / "caches"
    cache_dir.mkdir(parents=True, exist_ok=True)

    assert resolve_survey_cache_dir(root=tmp_path) == cache_dir.resolve()


def test_resolve_survey_cache_dir_uses_workspace_preference(
    tmp_path: Path, monkeypatch
) -> None:
    from qsync.config import resolve_survey_cache_dir, resolve_survey_cache_subdir

    monkeypatch.delenv("QSYNC_SURVEY_CACHE_SUBDIR", raising=False)
    prefs_dir = tmp_path / ".qsync"
    prefs_dir.mkdir(parents=True, exist_ok=True)
    (prefs_dir / "preferences.json").write_text(
        json.dumps({"survey_cache_subdir": "defs"}, indent=2),
        encoding="utf-8",
    )
    cache_dir = tmp_path / "surveys" / "defs"
    cache_dir.mkdir(parents=True, exist_ok=True)

    assert resolve_survey_cache_subdir(root=tmp_path) == "defs"
    assert resolve_survey_cache_dir(root=tmp_path) == cache_dir.resolve()


def test_resolve_survey_cache_dir_respects_account_scope(
    tmp_path: Path, monkeypatch
) -> None:
    from qsync.config import resolve_survey_cache_dir

    monkeypatch.delenv("QSYNC_SURVEY_CACHE_SUBDIR", raising=False)
    account_surveys = tmp_path / "surveys" / ".damian"
    account_surveys.mkdir(parents=True, exist_ok=True)

    assert resolve_survey_cache_dir(
        root=tmp_path, account="damian"
    ) == account_surveys.resolve()

    account_cache_dir = account_surveys / "caches"
    account_cache_dir.mkdir(parents=True, exist_ok=True)
    assert resolve_survey_cache_dir(
        root=tmp_path, account="damian"
    ) == account_cache_dir.resolve()


def test_qualtrics_client_find_cached_survey_file_honors_cache_subdir(
    tmp_path: Path, monkeypatch
) -> None:
    from qsync.qualtrics_client import find_cached_survey_file

    surveys = tmp_path / "surveys"
    cache_dir = surveys / "caches"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached_file = cache_dir / "Example__SV_TEST.json"
    cached_file.write_text("{}", encoding="utf-8")

    monkeypatch.setattr("qsync.qualtrics_client._workspace_root", lambda: tmp_path)
    found = find_cached_survey_file("SV_TEST")
    assert found == cached_file
