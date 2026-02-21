from __future__ import annotations

import argparse
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

from tests.workspace_helpers import ensure_qsync_workspace


def _zip_payload(filename: str, content: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(filename, content)
    return buf.getvalue()


def test_survey_pull_supports_multiple_surveys(monkeypatch, tmp_path: Path) -> None:
    from qsync import cli_survey

    pulled: list[str] = []
    monkeypatch.setattr(
        cli_survey,
        "_prompt_for_survey_ids_api_if_needed",
        lambda **_kwargs: ["SV_ONE", "SV_TWO"],
    )
    monkeypatch.setattr(
        cli_survey,
        "download_survey_definition",
        lambda survey_id, **_kwargs: pulled.append(survey_id)
        or (tmp_path / f"{survey_id}.json"),
    )

    args = argparse.Namespace(
        survey_id=None,
        dest=None,
        account=None,
    )

    cli_survey.handle_pull(args)
    assert pulled == ["SV_ONE", "SV_TWO"]


def test_translations_pull_supports_multiple_surveys(monkeypatch) -> None:
    from qsync import cli_survey

    refreshed: list[str] = []
    monkeypatch.setattr(
        "qsync.qualtrics_client.refresh_survey_cache",
        lambda survey_id, **_kwargs: (
            refreshed.append(survey_id)
            or (SimpleNamespace(path=Path(f"/tmp/{survey_id}.json")), True)
        ),
    )

    args = argparse.Namespace(
        survey_id=["SV_ONE", "SV_TWO"],
        account=None,
        language=None,
        languages=None,
    )

    cli_survey.handle_translations_pull(args)
    assert refreshed == ["SV_ONE", "SV_TWO"]


def test_export_responses_supports_multiple_surveys(
    monkeypatch, tmp_path: Path
) -> None:
    from qsync import cli_survey

    class _Resp:
        def __init__(self, payload=None, content: bytes | None = None) -> None:
            self._payload = payload or {}
            self._content = content or b""

        def json(self):
            return self._payload

        def iter_content(self, chunk_size: int = 8192):  # noqa: ARG002
            yield self._content

    monkeypatch.setattr(
        cli_survey,
        "_prompt_for_survey_ids_api_if_needed",
        lambda **_kwargs: ["SV_ONE", "SV_TWO"],
    )
    monkeypatch.setattr(
        cli_survey,
        "list_surveys",
        lambda *_args, **_kwargs: [
            {"id": "SV_ONE", "name": "Survey One"},
            {"id": "SV_TWO", "name": "Survey Two"},
        ],
    )
    monkeypatch.setattr(
        cli_survey,
        "get_client_config",
        lambda *_args, **_kwargs: ("example.qualtrics.com", {"X-API-TOKEN": "x"}),
    )

    zip_bytes = _zip_payload("responses.csv", "id,value\n1,a\n")

    def _fake_send_api_request(*, method: str, path: str, **_kwargs):
        if method == "POST" and path.endswith("/export-responses"):
            survey_id = path.split("/")[1]
            return _Resp({"result": {"progressId": f"PG_{survey_id}"}})
        if method == "GET" and "/export-responses/PG_" in path:
            survey_id = path.split("/")[1]
            return _Resp({"result": {"status": "complete", "fileId": f"FILE_{survey_id}"}})
        if method == "GET" and path.endswith("/file"):
            return _Resp(content=zip_bytes)
        raise AssertionError(f"Unexpected call: {method} {path}")

    monkeypatch.setattr(cli_survey, "send_api_request", _fake_send_api_request)

    args = argparse.Namespace(
        survey_id=None,
        output=str(tmp_path),
        account=None,
    )

    cli_survey.handle_export_responses(args)
    zipped = sorted(tmp_path.glob("*SV_*.zip"))
    assert len(zipped) == 2


def test_prepare_resolve_target_surveys_accepts_multiple_ids() -> None:
    from qsync.survey_prepare import resolve_target_surveys

    ids = resolve_target_surveys(
        survey_id=["SV_ONE,SV_TWO", "SV_TWO"],
        focal=False,
        all_surveys=False,
        interactive=False,
        yes=False,
    )
    assert ids == ["SV_ONE", "SV_TWO"]


def test_prepare_resolve_target_surveys_uses_explicit_account_inventory(
    tmp_path: Path,
) -> None:
    from qsync.config import resolve_scoped_dir
    from qsync.survey_prepare import resolve_target_surveys

    ensure_qsync_workspace(tmp_path)
    scoped_surveys_dir = resolve_scoped_dir("surveys", root=tmp_path, account="damian")
    scoped_surveys_dir.mkdir(parents=True, exist_ok=True)
    (scoped_surveys_dir / "inventory.csv").write_text(
        "id,name,focal,locked\nSV_DAMIAN,Survey Damian,TRUE,FALSE\n",
        encoding="utf-8",
    )

    ids = resolve_target_surveys(
        survey_id=None,
        focal=True,
        all_surveys=False,
        interactive=False,
        yes=False,
        root=tmp_path,
        account="damian",
    )
    assert ids == ["SV_DAMIAN"]
