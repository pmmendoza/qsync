from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.workspace_helpers import ensure_qsync_workspace


def _write_account_env(root: Path, account: str, *, base_url: str) -> Path:
    path = root / f".env.{account}"
    path.write_text(
        "\n".join(
            [
                f"QUALTRICS_BASE_URL={base_url}",
                "X-API-TOKEN=secret",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _write_workspace_prefs(root: Path, *, active_account: str | None) -> Path:
    state_dir = root / ".qsync"
    state_dir.mkdir(parents=True, exist_ok=True)
    prefs_path = state_dir / "preferences.json"
    payload: dict[str, object] = {}
    if active_account is not None:
        payload["active_account"] = active_account
    prefs_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return prefs_path


def _touch_env_for_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    # qsync.cli.main mutates process env; ensure these keys get restored.
    for key in (
        "QSYNC_ACCOUNT",
        "QSYNC_ROOT",
        "QSYNC_ENV_PATH",
        "QSYNC_JSON_MODE",
        "QSYNC_ALLOW_LOCKED",
    ):
        monkeypatch.delenv(key, raising=False)


def test_workspace_active_account_applies_when_no_flag_or_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from qsync.cli import main

    _touch_env_for_restore(monkeypatch)
    monkeypatch.chdir(tmp_path)

    ensure_qsync_workspace(tmp_path)
    _write_account_env(tmp_path, "damian", base_url="syd1.qualtrics.com")
    _write_workspace_prefs(tmp_path, active_account="damian")

    # doctor expects account-scoped inventory when an account is active.
    scoped_inventory = tmp_path / "surveys" / ".damian" / "inventory.csv"
    scoped_inventory.parent.mkdir(parents=True, exist_ok=True)
    scoped_inventory.write_text("id,name,locked\n", encoding="utf-8")

    main(["--root", str(tmp_path), "doctor", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["account"] == "damian"
    assert payload["surveys_dir"] == str(tmp_path / "surveys" / ".damian")


def test_account_flag_overrides_workspace_active_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from qsync.cli import main

    _touch_env_for_restore(monkeypatch)
    monkeypatch.chdir(tmp_path)

    ensure_qsync_workspace(tmp_path)
    _write_account_env(tmp_path, "damian", base_url="syd1.qualtrics.com")
    _write_account_env(tmp_path, "bob", base_url="iad1.qualtrics.com")
    _write_workspace_prefs(tmp_path, active_account="damian")

    scoped_inventory = tmp_path / "surveys" / ".bob" / "inventory.csv"
    scoped_inventory.parent.mkdir(parents=True, exist_ok=True)
    scoped_inventory.write_text("id,name,locked\n", encoding="utf-8")

    main(["--root", str(tmp_path), "--account", "bob", "doctor", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["account"] == "bob"
    assert payload["surveys_dir"] == str(tmp_path / "surveys" / ".bob")


def test_external_env_overrides_workspace_active_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from qsync.cli import main

    _touch_env_for_restore(monkeypatch)
    monkeypatch.chdir(tmp_path)

    ensure_qsync_workspace(tmp_path)
    _write_account_env(tmp_path, "damian", base_url="syd1.qualtrics.com")
    _write_account_env(tmp_path, "bob", base_url="iad1.qualtrics.com")
    _write_workspace_prefs(tmp_path, active_account="damian")

    scoped_inventory = tmp_path / "surveys" / ".bob" / "inventory.csv"
    scoped_inventory.parent.mkdir(parents=True, exist_ok=True)
    scoped_inventory.write_text("id,name,locked\n", encoding="utf-8")

    monkeypatch.setenv("QSYNC_ACCOUNT", "bob")

    main(["--root", str(tmp_path), "doctor", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["account"] == "bob"


def test_account_use_and_clear_persist_preferences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from qsync.cli import main

    _touch_env_for_restore(monkeypatch)
    monkeypatch.chdir(tmp_path)

    ensure_qsync_workspace(tmp_path)
    _write_account_env(tmp_path, "damian", base_url="syd1.qualtrics.com")

    main(["--root", str(tmp_path), "account", "use", "damian", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["active_account"] == "damian"

    prefs_path = tmp_path / ".qsync" / "preferences.json"
    prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
    assert prefs.get("active_account") == "damian"

    main(["--root", str(tmp_path), "account", "clear", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["active_account"] is None

    prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
    assert "active_account" not in prefs


def test_account_use_bootstraps_env_default_alias_when_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qsync.cli import main

    _touch_env_for_restore(monkeypatch)
    monkeypatch.chdir(tmp_path)

    ensure_qsync_workspace(tmp_path)
    _write_account_env(tmp_path, "damian", base_url="syd1.qualtrics.com")
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "QUALTRICS_BASE_URL=iad1.qualtrics.com",
                "X-API-TOKEN=default-secret",
                "EXTRA_SHOULD_NOT_COPY=1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    main(["--root", str(tmp_path), "account", "use", "damian"])

    env_default = tmp_path / ".env.default"
    assert env_default.exists()
    assert env_default.read_text(encoding="utf-8") == (
        "\n".join(
            [
                "QUALTRICS_BASE_URL=iad1.qualtrics.com",
                "X-API-TOKEN=default-secret",
            ]
        )
        + "\n"
    )


def test_account_use_default_bootstraps_env_default_alias_when_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qsync.cli import main

    _touch_env_for_restore(monkeypatch)
    monkeypatch.chdir(tmp_path)

    ensure_qsync_workspace(tmp_path)
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "QUALTRICS_BASE_URL=iad1.qualtrics.com",
                "X-API-TOKEN=default-secret",
                "EXTRA_SHOULD_NOT_COPY=1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    main(["--root", str(tmp_path), "account", "use", "default"])

    env_default = tmp_path / ".env.default"
    assert env_default.exists()
    assert env_default.read_text(encoding="utf-8") == (
        "\n".join(
            [
                "QUALTRICS_BASE_URL=iad1.qualtrics.com",
                "X-API-TOKEN=default-secret",
            ]
        )
        + "\n"
    )
    prefs = json.loads((tmp_path / ".qsync" / "preferences.json").read_text(encoding="utf-8"))
    assert prefs.get("active_account") == "default"


def test_account_ensure_default_alias_creates_minimal_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from qsync.cli import main

    _touch_env_for_restore(monkeypatch)
    monkeypatch.chdir(tmp_path)

    ensure_qsync_workspace(tmp_path)
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "QUALTRICS_BASE_URL=iad1.qualtrics.com",
                "QUALTRICS_API_KEY=default-secret",
                "EXTRA_SHOULD_NOT_COPY=1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    main(["--root", str(tmp_path), "account", "ensure-default-alias", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["created"] is True

    assert (tmp_path / ".env.default").read_text(encoding="utf-8") == (
        "\n".join(
            [
                "QUALTRICS_BASE_URL=iad1.qualtrics.com",
                "X-API-TOKEN=default-secret",
            ]
        )
        + "\n"
    )


def test_account_use_does_not_overwrite_existing_env_default_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qsync.cli import main

    _touch_env_for_restore(monkeypatch)
    monkeypatch.chdir(tmp_path)

    ensure_qsync_workspace(tmp_path)
    _write_account_env(tmp_path, "damian", base_url="syd1.qualtrics.com")
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "QUALTRICS_BASE_URL=iad1.qualtrics.com",
                "X-API-TOKEN=default-secret",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / ".env.default").write_text(
        "\n".join(
            [
                "QUALTRICS_BASE_URL=eu1.qualtrics.com",
                "X-API-TOKEN=custom-default",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    main(["--root", str(tmp_path), "account", "use", "damian"])

    assert (tmp_path / ".env.default").read_text(encoding="utf-8") == (
        "\n".join(
            [
                "QUALTRICS_BASE_URL=eu1.qualtrics.com",
                "X-API-TOKEN=custom-default",
            ]
        )
        + "\n"
    )


def test_account_adopt_moves_allowlisted_artifacts_and_preserves_shared_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qsync.cli import main

    _touch_env_for_restore(monkeypatch)
    monkeypatch.chdir(tmp_path)

    ensure_qsync_workspace(tmp_path)
    _write_account_env(tmp_path, "damian", base_url="syd1.qualtrics.com")

    # Unscoped artifacts to adopt.
    inventory = tmp_path / "surveys" / "inventory.csv"
    inventory.write_text("id,name,locked\nSV_1,Test,\n", encoding="utf-8")
    mapping = tmp_path / "surveys" / "qualtrics_api_key_mapping.csv"
    mapping.write_text("field_name,survey_master\nSurveyName,write\n", encoding="utf-8")
    legacy_snapshot = (
        tmp_path
        / "surveys"
        / "translation_key_snapshots"
        / "SV_1"
        / "before_EN.json"
    )
    legacy_snapshot.parent.mkdir(parents=True, exist_ok=True)
    legacy_snapshot.write_text("{}", encoding="utf-8")
    xlsx = tmp_path / "excel" / "SV_1-test.xlsx"
    xlsx.write_bytes(b"not-a-real-xlsx")

    # Dry-run should not move anything.
    main(["--root", str(tmp_path), "account", "adopt", "damian", "--dry-run", "--json"])
    assert inventory.exists()
    assert mapping.exists()
    assert xlsx.exists()

    # Real adoption moves allowlisted artifacts and can set the active account.
    main(
        [
            "--root",
            str(tmp_path),
            "account",
            "adopt",
            "damian",
            "--yes",
            "--no-copy-env",
            "--use",
        ]
    )

    assert not inventory.exists()
    assert (tmp_path / "surveys" / ".damian" / "inventory.csv").exists()

    assert not xlsx.exists()
    assert (tmp_path / "excel" / ".damian" / xlsx.name).exists()
    assert not legacy_snapshot.exists()
    assert (
        tmp_path
        / "contents"
        / ".damian"
        / "qualtrics_survey_translations"
        / "Test-SV_1"
        / "key_snapshots"
        / "before_EN.json"
    ).exists()

    # Shared mapping stays unscoped.
    assert mapping.exists()
    assert not (tmp_path / "surveys" / ".damian" / mapping.name).exists()

    # Lockfile should be cleaned up.
    assert not (tmp_path / ".qsync" / "account-adopt.lock").exists()

    prefs = json.loads((tmp_path / ".qsync" / "preferences.json").read_text(encoding="utf-8"))
    assert prefs.get("active_account") == "damian"


def test_account_cache_dir_show_set_and_clear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from qsync.cli import main

    _touch_env_for_restore(monkeypatch)
    monkeypatch.chdir(tmp_path)

    ensure_qsync_workspace(tmp_path)

    # Default behavior: resolved subdir is `caches`, but fallback is surveys/.
    main(["--root", str(tmp_path), "account", "cache-dir", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["survey_cache_subdir_pref"] is None
    assert payload["survey_cache_subdir_resolved"] == "caches"
    assert payload["effective_source"] == "surveys_root_fallback"

    # Set workspace preference.
    main(["--root", str(tmp_path), "account", "cache-dir", "defs", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["survey_cache_subdir_pref"] == "defs"
    assert payload["survey_cache_subdir_resolved"] == "defs"
    assert payload["effective_source"] == "surveys_root_fallback"

    prefs = json.loads((tmp_path / ".qsync" / "preferences.json").read_text(encoding="utf-8"))
    assert prefs.get("survey_cache_subdir") == "defs"

    # Once the folder exists, effective path flips to subdir mode.
    (tmp_path / "surveys" / "defs").mkdir(parents=True, exist_ok=True)
    main(["--root", str(tmp_path), "account", "cache-dir", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["effective_source"] == "subdir"
    assert payload["effective_cache_dir"].endswith("/surveys/defs")

    # Clear preference and return to default.
    main(["--root", str(tmp_path), "account", "cache-dir", "--clear", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["survey_cache_subdir_pref"] is None


def test_account_cache_dir_account_root_prefers_state_cache_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from qsync.cli import main

    _touch_env_for_restore(monkeypatch)
    monkeypatch.chdir(tmp_path)

    (tmp_path / ".qsync").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".qsync" / "preferences.json").write_text(
        json.dumps({"workspace_layout": "account_root_v1"}, indent=2) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "accounts" / "default" / "state").mkdir(parents=True, exist_ok=True)

    main(["--root", str(tmp_path), "account", "cache-dir", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["survey_cache_subdir_resolved"] == "cache"
    assert payload["preferred_cache_dir"].endswith("/accounts/default/state/cache")
    assert payload["effective_cache_dir"].endswith("/accounts/default/state")
    assert payload["effective_source"] == "surveys_root_fallback"

    (tmp_path / "accounts" / "default" / "state" / "cache").mkdir(
        parents=True, exist_ok=True
    )
    main(["--root", str(tmp_path), "account", "cache-dir", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["effective_cache_dir"].endswith("/accounts/default/state/cache")
    assert payload["effective_source"] == "subdir"


def test_workspace_active_account_applies_to_survey_pull_when_no_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qsync.cli import main
    from qsync import cli_survey

    _touch_env_for_restore(monkeypatch)
    monkeypatch.chdir(tmp_path)

    ensure_qsync_workspace(tmp_path)
    _write_account_env(tmp_path, "damian", base_url="syd1.qualtrics.com")
    _write_workspace_prefs(tmp_path, active_account="damian")

    captured: dict[str, str | None] = {}

    def _fake_download(
        survey_id: str,
        *,
        target_dir: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> Path:
        captured["survey_id"] = survey_id
        captured["target_dir"] = str(target_dir)
        captured["base"] = (env or {}).get("QUALTRICS_BASE_URL")
        captured["token"] = (env or {}).get("X-API-TOKEN")
        return (target_dir or tmp_path / "surveys") / f"{survey_id}.json"

    monkeypatch.setattr(cli_survey, "download_survey_definition", _fake_download)

    main(["--root", str(tmp_path), "survey", "pull", "--survey-id", "SV_1"])

    assert captured["survey_id"] == "SV_1"
    assert captured["base"] == "syd1.qualtrics.com"
    assert captured["token"] == "secret"
    assert captured["target_dir"] == str((tmp_path / "surveys" / ".damian").resolve())


def test_survey_pull_account_default_bypasses_workspace_active_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qsync.cli import main
    from qsync import cli_survey

    _touch_env_for_restore(monkeypatch)
    monkeypatch.chdir(tmp_path)

    ensure_qsync_workspace(tmp_path)
    _write_account_env(tmp_path, "damian", base_url="syd1.qualtrics.com")
    _write_workspace_prefs(tmp_path, active_account="damian")

    monkeypatch.setattr(
        cli_survey,
        "load_account_env",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("load_account_env should not be called for --account default")
        ),
    )

    captured: dict[str, str | None] = {}

    def _fake_download(
        survey_id: str,
        *,
        target_dir: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> Path:
        captured["survey_id"] = survey_id
        captured["target_dir"] = str(target_dir)
        captured["env"] = "set" if env else None
        return (target_dir or tmp_path / "surveys") / f"{survey_id}.json"

    monkeypatch.setattr(cli_survey, "download_survey_definition", _fake_download)

    main(
        [
            "--root",
            str(tmp_path),
            "survey",
            "pull",
            "--account",
            "default",
            "--survey-id",
            "SV_1",
        ]
    )

    assert captured["survey_id"] == "SV_1"
    assert captured["env"] is None
    assert captured["target_dir"] == str((tmp_path / "surveys").resolve())


def test_workspace_active_account_missing_env_fails_pull_strictly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from qsync.cli import main

    _touch_env_for_restore(monkeypatch)
    monkeypatch.chdir(tmp_path)

    ensure_qsync_workspace(tmp_path)
    _write_workspace_prefs(tmp_path, active_account="damian")

    with pytest.raises(SystemExit) as excinfo:
        main(["--root", str(tmp_path), "survey", "pull", "--survey-id", "SV_1"])

    assert excinfo.value.code == 1
    assert "QSYNC-CONFIG-ACCOUNTENV-001" in capsys.readouterr().err
