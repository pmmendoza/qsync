from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.workspace_helpers import ensure_qsync_workspace


def _write_layout_pref(root: Path, layout: str) -> None:
    state_dir = root / ".qsync"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "preferences.json").write_text(
        json.dumps({"workspace_layout": layout}, indent=2) + "\n",
        encoding="utf-8",
    )


def _touch_env_for_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "QSYNC_ACCOUNT",
        "QSYNC_ROOT",
        "QSYNC_ENV_PATH",
        "QSYNC_JSON_MODE",
        "QSYNC_ALLOW_LOCKED",
        "QSYNC_WORKSPACE_LAYOUT",
        "QSYNC_SURVEY_CACHE_SUBDIR",
    ):
        monkeypatch.delenv(key, raising=False)


def test_resolve_scoped_dir_account_root_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.config import resolve_scoped_dir

    _touch_env_for_restore(monkeypatch)
    _write_layout_pref(tmp_path, "account_root_v1")

    assert resolve_scoped_dir("surveys", root=tmp_path) == (
        tmp_path / "accounts" / "default"
    ).resolve()
    assert resolve_scoped_dir("survey_js", root=tmp_path, account="damian") == (
        tmp_path / "accounts" / "damian" / "js"
    ).resolve()
    assert resolve_scoped_dir("export", root=tmp_path, account="damian") == (
        tmp_path / "accounts" / "damian" / "derived" / "export"
    ).resolve()
    assert resolve_scoped_dir("responses", root=tmp_path, account="damian") == (
        tmp_path / "accounts" / "damian" / "derived" / "responses"
    ).resolve()


def test_resolve_survey_cache_dir_account_root_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.config import resolve_survey_cache_dir, resolve_survey_cache_subdir

    _touch_env_for_restore(monkeypatch)
    _write_layout_pref(tmp_path, "account_root_v1")

    state_root = tmp_path / "accounts" / "default" / "state"
    state_root.mkdir(parents=True, exist_ok=True)

    assert resolve_survey_cache_subdir(root=tmp_path) == "cache"
    assert resolve_survey_cache_dir(root=tmp_path) == state_root.resolve()

    cache_dir = state_root / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    assert resolve_survey_cache_dir(root=tmp_path) == cache_dir.resolve()


def test_build_setup_moves_routes_legacy_survey_cache_into_state_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.doctor_setup import build_setup_moves

    _touch_env_for_restore(monkeypatch)
    ensure_qsync_workspace(tmp_path)

    (tmp_path / "surveys" / "caches").mkdir(parents=True, exist_ok=True)
    (tmp_path / "surveys" / "caches" / "Cached__SV_CACHED.json").write_text(
        "{}",
        encoding="utf-8",
    )
    (tmp_path / "surveys" / "Root__SV_ROOT.json").write_text(
        "{}",
        encoding="utf-8",
    )
    (tmp_path / "surveys" / "backups").mkdir(parents=True, exist_ok=True)
    (tmp_path / "surveys" / "backups" / "Backup__SV_BKP.json").write_text(
        "{}",
        encoding="utf-8",
    )

    plan = build_setup_moves(tmp_path, target_account="default")
    move_map = {
        str(m.src.relative_to(tmp_path)): str(m.dst.relative_to(tmp_path))
        for m in plan
    }

    assert move_map["surveys/caches"] == "accounts/default/state/cache"
    assert (
        move_map["surveys/Root__SV_ROOT.json"]
        == "accounts/default/state/cache/Root__SV_ROOT.json"
    )
    assert move_map["surveys/backups"] == "accounts/default/state/cache/backups"


def test_build_setup_moves_does_not_treat_archive_like_dirs_as_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.doctor_setup import build_setup_moves

    _touch_env_for_restore(monkeypatch)
    ensure_qsync_workspace(tmp_path)

    (tmp_path / "surveys" / "slices").mkdir(parents=True, exist_ok=True)
    (tmp_path / "surveys" / "slices" / "Slice__SV_SLICE.json").write_text(
        "{}",
        encoding="utf-8",
    )
    (tmp_path / "surveys" / "archive").mkdir(parents=True, exist_ok=True)
    (tmp_path / "surveys" / "archive" / "Archive__SV_ARC.json").write_text(
        "{}",
        encoding="utf-8",
    )
    (tmp_path / "surveys" / "_archive_duplicates_2026_02_21").mkdir(
        parents=True, exist_ok=True
    )
    (
        tmp_path
        / "surveys"
        / "_archive_duplicates_2026_02_21"
        / "Duplicate__SV_DUP.json"
    ).write_text("{}", encoding="utf-8")

    plan = build_setup_moves(tmp_path, target_account="default")
    move_map = {
        str(m.src.relative_to(tmp_path)): str(m.dst.relative_to(tmp_path))
        for m in plan
    }

    assert move_map["surveys/slices"] == "accounts/default/slices"
    assert move_map["surveys/archive"] == "accounts/default/archive"
    assert (
        move_map["surveys/_archive_duplicates_2026_02_21"]
        == "accounts/default/_archive_duplicates_2026_02_21"
    )


def test_doctor_setup_dry_run_apply_and_undo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from qsync.cli import main

    _touch_env_for_restore(monkeypatch)
    monkeypatch.chdir(tmp_path)
    ensure_qsync_workspace(tmp_path)
    (tmp_path / "contents").mkdir(parents=True, exist_ok=True)
    (tmp_path / "export").mkdir(parents=True, exist_ok=True)
    (tmp_path / "responses").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tmp").mkdir(parents=True, exist_ok=True)

    (tmp_path / "surveys" / "inventory.csv").write_text(
        "id,name,locked\nSV_1,Example,\n", encoding="utf-8"
    )
    (tmp_path / "surveys" / "pending" / "items").mkdir(parents=True, exist_ok=True)
    (tmp_path / "surveys" / "pending" / "items" / "SV_1.json").write_text(
        "{}", encoding="utf-8"
    )
    (tmp_path / "excel" / "SV_1.xlsx").write_bytes(b"xlsx")
    (tmp_path / "survey_js" / "core").mkdir(parents=True, exist_ok=True)
    (tmp_path / "survey_js" / "core" / "core_logic.js").write_text(
        "console.log('x');", encoding="utf-8"
    )
    (tmp_path / "survey_js" / "survey_qid_js_map.csv").write_text(
        "js_file,SV_1-Example\ncore_logic.js,QID1\n",
        encoding="utf-8",
    )
    (tmp_path / "contents" / "qualtrics_survey_translations" / "SV_1").mkdir(
        parents=True, exist_ok=True
    )
    (tmp_path / "contents" / "qualtrics_survey_translations" / "SV_1" / "EN.json").write_text(
        "{}", encoding="utf-8"
    )
    (
        tmp_path
        / "contents"
        / "qualtrics_library_messages"
        / "UR_TEST"
        / "MS_TEST"
        / "messages"
    ).mkdir(parents=True, exist_ok=True)
    (
        tmp_path
        / "contents"
        / "qualtrics_library_messages"
        / "UR_TEST"
        / "MS_TEST"
        / "meta.json"
    ).write_text("{}", encoding="utf-8")
    (
        tmp_path
        / "contents"
        / "qualtrics_library_messages"
        / "UR_TEST"
        / "MS_TEST"
        / "messages"
        / "_keys.json"
    ).write_text("{}", encoding="utf-8")
    (tmp_path / "export" / "example.docx").write_text("doc", encoding="utf-8")
    (tmp_path / "responses" / "example.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    main(["--root", str(tmp_path), "doctor", "setup", "--json"])
    dry_payload = json.loads(capsys.readouterr().out)
    assert dry_payload["mode"] == "dry-run"
    assert dry_payload["planned_moves"] > 0
    assert (tmp_path / "surveys" / "inventory.csv").exists()

    main(
        [
            "--root",
            str(tmp_path),
            "doctor",
            "setup",
            "--apply",
            "--yes",
            "--json",
        ]
    )
    apply_payload = json.loads(capsys.readouterr().out)
    assert apply_payload["mode"] == "apply"
    assert apply_payload["moved"] > 0
    assert Path(apply_payload["manifest"]).exists()
    assert Path(apply_payload["undo_manifest"]).exists()

    assert (tmp_path / "accounts" / "default" / "inventory.csv").exists()
    assert (tmp_path / "accounts" / "default" / "pending" / "items" / "SV_1.json").exists()
    assert (tmp_path / "accounts" / "default" / "js" / "core" / "core_logic.js").exists()
    assert (tmp_path / "accounts" / "default" / "js" / "survey_qid_js_map.csv").exists()
    assert (tmp_path / "accounts" / "default" / "translations" / "SV_1" / "EN.json").exists()
    assert (
        tmp_path
        / "accounts"
        / "default"
        / "library_messages"
        / "UR_TEST"
        / "MS_TEST"
        / "meta.json"
    ).exists()
    assert (tmp_path / "accounts" / "default" / "derived" / "export" / "example.docx").exists()
    assert (tmp_path / "accounts" / "default" / "derived" / "responses" / "example.csv").exists()

    prefs = json.loads((tmp_path / ".qsync" / "preferences.json").read_text(encoding="utf-8"))
    assert prefs["workspace_layout"] == "account_root_v1"
    assert prefs["survey_cache_subdir"] == "cache"

    # Migration idempotency: once applied, dry-run should have no remaining moves.
    main(["--root", str(tmp_path), "doctor", "setup", "--json"])
    post_apply_payload = json.loads(capsys.readouterr().out)
    assert post_apply_payload["planned_moves"] == 0

    undo_manifest = str(apply_payload["undo_manifest"])
    main(
        [
            "--root",
            str(tmp_path),
            "doctor",
            "setup",
            "--undo",
            undo_manifest,
            "--yes",
            "--json",
        ]
    )
    undo_payload = json.loads(capsys.readouterr().out)
    assert undo_payload["mode"] == "undo"
    assert undo_payload["restored"] > 0

    assert (tmp_path / "surveys" / "inventory.csv").exists()
    assert (tmp_path / "survey_js" / "core" / "core_logic.js").exists()
    assert (tmp_path / "contents" / "qualtrics_survey_translations" / "SV_1" / "EN.json").exists()


def test_doctor_setup_apply_errors_do_not_flip_workspace_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from qsync.cli import main
    import qsync.doctor_setup as doctor_setup

    _touch_env_for_restore(monkeypatch)
    monkeypatch.chdir(tmp_path)
    ensure_qsync_workspace(tmp_path)

    (tmp_path / "surveys" / "inventory.csv").write_text(
        "id,name,locked\nSV_1,Example,\n", encoding="utf-8"
    )
    (tmp_path / "excel" / "SV_1.xlsx").write_bytes(b"xlsx")

    def _failing_apply_moves(_moves):
        return [], [], [{"src": "a", "dst": "b", "error": "forced failure"}]

    monkeypatch.setattr(doctor_setup, "_apply_moves", _failing_apply_moves)

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "--root",
                str(tmp_path),
                "doctor",
                "setup",
                "--apply",
                "--yes",
                "--json",
            ]
        )
    assert exc.value.code == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    prefs_path = tmp_path / ".qsync" / "preferences.json"
    assert not prefs_path.exists()
    assert not (tmp_path / ".qsync" / "active_account.txt").exists()
