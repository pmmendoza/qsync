from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.workspace_helpers import ensure_qsync_workspace, write_inventory_csv


def test_load_account_env_rejects_invalid_name_without_loading_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qsync.config import load_account_env
    from qsync.errors import QsyncValidationError

    def _boom(_path: Path | None) -> dict[str, str]:
        raise AssertionError("load_env_file should not be called for invalid account")

    monkeypatch.setattr("qsync.config.load_env_file", _boom)

    with pytest.raises(QsyncValidationError) as excinfo:
        load_account_env("../../etc/passwd", root=Path("/tmp"))

    assert excinfo.value.error_id == "QSYNC-VALIDATION-ACCOUNT-002"


def test_load_account_env_missing_file_is_actionable(tmp_path: Path) -> None:
    from qsync.config import load_account_env
    from qsync.errors import QsyncConfigError

    with pytest.raises(QsyncConfigError) as excinfo:
        load_account_env("missing", root=tmp_path)

    assert excinfo.value.error_id == "QSYNC-CONFIG-ACCOUNTENV-001"
    assert excinfo.value.exit_code == 1
    assert str(tmp_path / ".env.missing") == excinfo.value.context.get("env_path")


def test_load_account_env_missing_keys_is_actionable(tmp_path: Path) -> None:
    from qsync.config import load_account_env
    from qsync.errors import QsyncConfigError

    env_path = tmp_path / ".env.damian"
    env_path.write_text("QUALTRICS_BASE_URL=iad1.qualtrics.com\n", encoding="utf-8")

    with pytest.raises(QsyncConfigError) as excinfo:
        load_account_env("damian", root=tmp_path)

    assert excinfo.value.error_id == "QSYNC-CONFIG-ACCOUNTENV-002"
    assert excinfo.value.exit_code == 1


def test_load_account_env_rejects_scheme_in_base_url(tmp_path: Path) -> None:
    from qsync.config import load_account_env
    from qsync.errors import QsyncConfigError

    env_path = tmp_path / ".env.damian"
    env_path.write_text(
        "\n".join(
            [
                "QUALTRICS_BASE_URL=https://iad1.qualtrics.com",
                "X-API-TOKEN=secret",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(QsyncConfigError) as excinfo:
        load_account_env("damian", root=tmp_path)

    assert excinfo.value.error_id == "QSYNC-CONFIG-ACCOUNTENV-003"
    assert excinfo.value.exit_code == 1


def test_load_account_env_accepts_target_token_keys(tmp_path: Path) -> None:
    from qsync.config import load_account_env

    env_path = tmp_path / ".env.damian"
    env_path.write_text(
        "\n".join(
            [
                "QUALTRICS_BASE_URL=iad1.qualtrics.com",
                "TARGET_X-API-TOKEN=secret",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    env = load_account_env("damian", root=tmp_path)
    assert env["QUALTRICS_BASE_URL"] == "iad1.qualtrics.com"
    assert env["X-API-TOKEN"] == "secret"


def test_survey_list_account_uses_account_env_and_skips_inventory_ordering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.cli import main
    from qsync import cli_survey

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")
    # Account-scoped inventory (doctor resolves inventory under surveys/.<account>/).
    scoped_inventory = tmp_path / "surveys" / ".damian" / "inventory.csv"
    scoped_inventory.parent.mkdir(parents=True, exist_ok=True)
    scoped_inventory.write_text("id,name,locked\n", encoding="utf-8")

    # Alternate account env file.
    (tmp_path / ".env.damian").write_text(
        "\n".join(
            [
                "QUALTRICS_BASE_URL=syd1.qualtrics.com",
                "X-API-TOKEN=secret",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        cli_survey,
        "_order_surveys_like_inventory",
        lambda _surveys: (_ for _ in ()).throw(
            AssertionError("inventory ordering should be skipped for --account")
        ),
    )

    captured: dict[str, str] = {}

    def _fake_list_surveys(base: str, headers: dict) -> list[dict]:
        captured["base"] = base
        captured["token"] = str(headers.get("X-API-TOKEN") or "")
        return [
            {
                "id": "SV_1",
                "name": "Survey A",
                "isActive": True,
                "creationDate": "2026-01-01T00:00:00Z",
            }
        ]

    monkeypatch.setattr(cli_survey, "list_surveys", _fake_list_surveys)

    main(["--root", str(tmp_path), "survey", "list", "--account", "damian"])

    assert captured["base"] == "syd1.qualtrics.com"
    assert captured["token"] == "secret"


def test_survey_delete_account_uses_account_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.cli import main
    from qsync import cli_survey

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")
    scoped_inventory = tmp_path / "surveys" / ".damian" / "inventory.csv"
    scoped_inventory.parent.mkdir(parents=True, exist_ok=True)
    scoped_inventory.write_text("id,name,locked\n", encoding="utf-8")

    (tmp_path / ".env.damian").write_text(
        "\n".join(
            [
                "QUALTRICS_BASE_URL=syd1.qualtrics.com",
                "X-API-TOKEN=secret",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    calls: list[dict] = []

    class _Resp:
        ok = True

    def _fake_send_api_request(**kwargs):
        calls.append(dict(kwargs))
        return _Resp()

    monkeypatch.setattr(cli_survey, "send_api_request", _fake_send_api_request)
    monkeypatch.setattr(
        cli_survey,
        "_fetch_survey_status",
        lambda *_args, **_kwargs: {
            "id": "SV_1",
            "name": "Survey A",
            "isActive": False,
            "responseCounts": {"auditable": 0, "generated": 0},
        },
    )

    main(
        [
            "--root",
            str(tmp_path),
            "survey",
            "delete",
            "--account",
            "damian",
            "--yes",
            "SV_1",
        ]
    )

    assert calls, "Expected DELETE call"
    assert calls[0]["base_url"] == "syd1.qualtrics.com"
    assert calls[0]["headers"].get("X-API-TOKEN") == "secret"
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["path"] == "surveys/SV_1"


def test_doctor_check_api_account_uses_account_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from qsync.cli import main

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")
    scoped_inventory = tmp_path / "surveys" / ".damian" / "inventory.csv"
    scoped_inventory.parent.mkdir(parents=True, exist_ok=True)
    scoped_inventory.write_text("id,name,locked\n", encoding="utf-8")

    (tmp_path / ".env.damian").write_text(
        "\n".join(
            [
                "QUALTRICS_BASE_URL=iad1.qualtrics.com",
                "X-API-TOKEN=secret",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    calls: list[dict] = []

    class _Resp:
        def json(self):
            return {"result": {"datacenter": "iad1"}}

    def _fake_send_api_request(**kwargs):
        calls.append(dict(kwargs))
        return _Resp()

    monkeypatch.setattr("qsync.api_push.send_api_request", _fake_send_api_request)

    main(
        [
            "--root",
            str(tmp_path),
            "doctor",
            "--json",
            "--check-api",
            "--account",
            "damian",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["account"] == "damian"
    assert payload["qualtrics_base_url"] == "iad1.qualtrics.com"
    assert calls, "Expected /whoami call"
    assert calls[0]["base_url"] == "iad1.qualtrics.com"
    assert calls[0]["path"] == "whoami"


def test_survey_pull_account_uses_account_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.cli import main
    from qsync import cli_survey

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")

    (tmp_path / ".env.damian").write_text(
        "\n".join(
            [
                "QUALTRICS_BASE_URL=syd1.qualtrics.com",
                "X-API-TOKEN=secret",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    captured: dict[str, str | None] = {}

    def _fake_download_survey_definition(
        survey_id: str, *, target_dir: Path | None = None, env: dict[str, str] | None = None
    ) -> Path:
        captured["survey_id"] = survey_id
        captured["base"] = (env or {}).get("QUALTRICS_BASE_URL")
        captured["token"] = (env or {}).get("X-API-TOKEN")
        captured["target_dir"] = str(target_dir)
        return Path(target_dir or Path("surveys")) / f"{survey_id}.json"

    monkeypatch.setattr(cli_survey, "download_survey_definition", _fake_download_survey_definition)

    main(
        [
            "--root",
            str(tmp_path),
            "survey",
            "pull",
            "--account",
            "damian",
            "--survey-id",
            "SV_1",
        ]
    )

    assert captured["survey_id"] == "SV_1"
    assert captured["base"] == "syd1.qualtrics.com"
    assert captured["token"] == "secret"
    assert captured["target_dir"] == str((tmp_path / "surveys" / ".damian").resolve())


def test_survey_pull_account_dest_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.cli import main
    from qsync import cli_survey

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")

    (tmp_path / ".env.damian").write_text(
        "\n".join(
            [
                "QUALTRICS_BASE_URL=syd1.qualtrics.com",
                "X-API-TOKEN=secret",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    explicit_dest = tmp_path / "custom" / "pulls"
    captured: dict[str, str | None] = {}

    def _fake_download_survey_definition(
        survey_id: str, *, target_dir: Path | None = None, env: dict[str, str] | None = None
    ) -> Path:
        captured["target_dir"] = str(target_dir)
        return Path(target_dir or Path("surveys")) / f"{survey_id}.json"

    monkeypatch.setattr(cli_survey, "download_survey_definition", _fake_download_survey_definition)

    main(
        [
            "--root",
            str(tmp_path),
            "survey",
            "pull",
            "--account",
            "damian",
            "--dest",
            str(explicit_dest),
            "--survey-id",
            "SV_1",
        ]
    )

    assert captured["target_dir"] == str(explicit_dest)


def test_survey_pull_default_destination_no_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.cli import main
    from qsync import cli_survey

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")

    captured: dict[str, str | None] = {}

    def _fake_download_survey_definition(
        survey_id: str, *, target_dir: Path | None = None, env: dict[str, str] | None = None
    ) -> Path:
        captured["target_dir"] = str(target_dir)
        return Path(target_dir or Path("surveys")) / f"{survey_id}.json"

    monkeypatch.setattr(cli_survey, "download_survey_definition", _fake_download_survey_definition)

    main(
        [
            "--root",
            str(tmp_path),
            "survey",
            "pull",
            "--survey-id",
            "SV_1",
        ]
    )

    assert captured["target_dir"] == str((tmp_path / "surveys").resolve())


def test_survey_pull_account_prompt_uses_account_live_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.cli import main
    from qsync import cli_survey

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")

    (tmp_path / ".env.damian").write_text(
        "\n".join(
            [
                "QUALTRICS_BASE_URL=syd1.qualtrics.com",
                "X-API-TOKEN=secret",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    captured: dict[str, str | None] = {}

    def _fake_pick_surveys(
        message: str, base_url: str, headers: dict[str, str], **kwargs
    ) -> list[str]:
        captured["base"] = base_url
        captured["token"] = str(headers.get("X-API-TOKEN") or "")
        return ["SV_3"]

    def _fake_download_survey_definition(
        survey_id: str, *, target_dir: Path | None = None, env: dict[str, str] | None = None
    ) -> Path:
        captured["survey_id"] = survey_id
        captured["downloaded_dir"] = str(target_dir)
        return Path(target_dir or Path("surveys")) / f"{survey_id}.json"

    monkeypatch.setattr("qsync.survey_selection.pick_survey_ids_from_api", _fake_pick_surveys)
    monkeypatch.setattr(cli_survey, "download_survey_definition", _fake_download_survey_definition)
    monkeypatch.setattr(
        "qsync.cli_survey.sys.stdin.isatty", lambda: True
    )
    monkeypatch.setattr("qsync.interactive_menu.is_interactive", lambda: True)

    main(
        [
            "--root",
            str(tmp_path),
            "survey",
            "pull",
            "--account",
            "damian",
        ]
    )

    assert captured["survey_id"] == "SV_3"
    assert captured["base"] == "syd1.qualtrics.com"
    assert captured["token"] == "secret"
    assert captured["downloaded_dir"] == str((tmp_path / "surveys" / ".damian").resolve())


def test_export_responses_defaults_to_account_scoped_output_dir(tmp_path: Path) -> None:
    from qsync.cli_survey import _resolve_responses_output_dir

    root = tmp_path.resolve()
    assert _resolve_responses_output_dir(root, None, None) == (root / "responses").resolve()
    assert _resolve_responses_output_dir(root, "damian", None) == (root / "responses" / ".damian").resolve()


def test_translations_pull_account_uses_account_env_and_scoped_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.cli import main

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")

    (tmp_path / ".env.damian").write_text(
        "\n".join(
            [
                "QUALTRICS_BASE_URL=syd1.qualtrics.com",
                "X-API-TOKEN=secret",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    captured: dict[str, str | None] = {}

    from qsync.qualtrics_client import SurveyCache

    def _fake_refresh_survey_cache(
        survey_id: str,
        *,
        surveys_dir: Path | None = None,
        env: dict[str, str] | None = None,
    ):
        captured["survey_id"] = survey_id
        captured["target_dir"] = str(surveys_dir)
        captured["base"] = (env or {}).get("QUALTRICS_BASE_URL")
        captured["token"] = (env or {}).get("X-API-TOKEN")
        return (
            SurveyCache(survey_id=survey_id, path=Path(surveys_dir or tmp_path), payload={}),
            True,
        )

    monkeypatch.setattr(
        "qsync.qualtrics_client.refresh_survey_cache",
        _fake_refresh_survey_cache,
    )

    main(
        [
            "--root",
            str(tmp_path),
            "translations",
            "pull",
            "--account",
            "damian",
            "--survey-id",
            "SV_1",
        ]
    )

    assert captured["survey_id"] == "SV_1"
    assert captured["base"] == "syd1.qualtrics.com"
    assert captured["token"] == "secret"
    assert captured["target_dir"] == str((tmp_path / "surveys" / ".damian").resolve())
