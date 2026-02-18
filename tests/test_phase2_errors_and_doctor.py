from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.workspace_helpers import ensure_qsync_workspace, write_inventory_csv


def test_config_missing_base_url_raises_structured_error() -> None:
    from qsync.config import get_client_config
    from qsync.errors import QsyncConfigError

    with pytest.raises(QsyncConfigError) as excinfo:
        get_client_config(env={"X-API-TOKEN": "x"})
    assert excinfo.value.error_id == "QSYNC-CONFIG-BASEURL-001"


def test_config_missing_token_raises_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qsync.config import get_client_config
    from qsync.errors import QsyncConfigError

    monkeypatch.setattr(
        "qsync.secrets.get_qualtrics_api_token_from_keyring",
        lambda _env: None,
        raising=False,
    )
    with pytest.raises(QsyncConfigError) as excinfo:
        get_client_config(env={"QUALTRICS_BASE_URL": "iad1.qualtrics.com"})
    assert excinfo.value.error_id == "QSYNC-CONFIG-TOKEN-001"


def test_doctor_json_includes_credential_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from qsync.cli import main

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")

    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "QUALTRICS_BASE_URL=iad1.qualtrics.com",
                "X-API-TOKEN=secret",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    # Run doctor in JSON mode; should not require network.
    main(["--root", str(tmp_path), "--env-path", str(env_path), "doctor", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["qualtrics_base_url"] == "iad1.qualtrics.com"
    assert payload["qualtrics_token_present"] is True


def test_doctor_check_api_datacenter_mismatch_logic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from qsync.cli import main

    ensure_qsync_workspace(tmp_path)
    write_inventory_csv(tmp_path, "id,name,locked\n")

    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "QUALTRICS_BASE_URL=iad1.qualtrics.com",
                "X-API-TOKEN=secret",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    class _Resp:
        def json(self):
            return {"result": {"datacenter": "iad1"}}

    def _fake_send_api_request(**_kwargs):
        return _Resp()

    monkeypatch.setattr("qsync.api_push.send_api_request", _fake_send_api_request)

    main(
        [
            "--root",
            str(tmp_path),
            "--env-path",
            str(env_path),
            "doctor",
            "--json",
            "--check-api",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["datacenter"] == "iad1"
    assert payload["datacenter_mismatch"] is False
