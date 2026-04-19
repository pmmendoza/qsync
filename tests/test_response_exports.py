from __future__ import annotations

import argparse
import io
import zipfile
from pathlib import Path


def _zip_payload(filename: str, content: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(filename, content)
    return buf.getvalue()


def test_build_response_export_payload_tabular_formats_include_text_options() -> None:
    from qsync.response_exports import build_response_export_payload

    for export_format in ("csv", "tsv", "spss", "xml"):
        assert build_response_export_payload(export_format=export_format) == {
            "format": export_format,
            "useLabels": True,
            "seenUnansweredRecode": 999,
            "timeZone": "UTC",
        }


def test_build_response_export_payload_json_formats_omit_restricted_options() -> None:
    from qsync.response_exports import build_response_export_payload

    for export_format in ("json", "ndjson"):
        assert build_response_export_payload(export_format=export_format) == {
            "format": export_format,
        }


def test_handle_export_responses_uses_requested_json_format(
    monkeypatch, tmp_path: Path
) -> None:
    from qsync import cli_survey

    seen_payloads: list[dict[str, object]] = []

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
        "list_surveys",
        lambda *_args, **_kwargs: [{"id": "SV_ONE", "name": "Survey One"}],
    )
    monkeypatch.setattr(
        cli_survey,
        "get_client_config",
        lambda *_args, **_kwargs: ("example.qualtrics.com", {"X-API-TOKEN": "x"}),
    )

    zip_bytes = _zip_payload("responses.json", '{"responses":[]}\n')

    def _fake_send_api_request(*, method: str, path: str, json=None, **_kwargs):
        if method == "POST" and path.endswith("/export-responses"):
            seen_payloads.append(json)
            return _Resp({"result": {"progressId": "PG_SV_ONE"}})
        if method == "GET" and "/export-responses/PG_" in path:
            return _Resp({"result": {"status": "complete", "fileId": "FILE_SV_ONE"}})
        if method == "GET" and path.endswith("/file"):
            return _Resp(content=zip_bytes)
        raise AssertionError(f"Unexpected call: {method} {path}")

    monkeypatch.setattr(cli_survey, "send_api_request", _fake_send_api_request)

    args = argparse.Namespace(
        survey_id=["SV_ONE"],
        output=str(tmp_path),
        account=None,
        export_format="json",
    )

    cli_survey.handle_export_responses(args)

    assert seen_payloads == [{"format": "json"}]
    assert (tmp_path / "responses.json").exists()
    assert (tmp_path / "Survey One_SV_ONE_json.zip").exists()
