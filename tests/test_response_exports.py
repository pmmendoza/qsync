from __future__ import annotations

import argparse
import io
import json
import zipfile
from pathlib import Path

import pytest


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


def test_build_response_export_payload_can_include_display_order() -> None:
    from qsync.response_exports import build_response_export_payload

    assert build_response_export_payload(
        export_format="csv",
        include_display_order=True,
    ) == {
        "format": "csv",
        "useLabels": True,
        "seenUnansweredRecode": 999,
        "timeZone": "UTC",
        "includeDisplayOrder": True,
    }


def test_build_response_export_payload_rejects_json_display_order() -> None:
    from qsync.response_exports import build_response_export_payload

    with pytest.raises(ValueError, match="includeDisplayOrder"):
        build_response_export_payload(
            export_format="ndjson",
            include_display_order=True,
        )


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


def test_handle_export_responses_can_request_display_order(
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

    zip_bytes = _zip_payload("responses.csv", "ResponseId\nR_1\n")

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
        export_format="csv",
        include_display_order=True,
    )

    cli_survey.handle_export_responses(args)

    assert seen_payloads == [
        {
            "format": "csv",
            "useLabels": True,
            "seenUnansweredRecode": 999,
            "timeZone": "UTC",
            "includeDisplayOrder": True,
        }
    ]


def test_handle_export_responses_analysis_bundle(
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
    monkeypatch.setattr(
        cli_survey,
        "fetch_survey_definition",
        lambda *_args, **_kwargs: {
            "Questions": {
                "QID1": {
                    "DataExportTag": "question_1",
                    "QuestionType": "MC",
                    "Selector": "SAVR",
                    "QuestionText": "Question 1?",
                    "Choices": {"1": {"Display": "Yes", "Recode": "1"}},
                }
            }
        },
    )

    ndjson = (
        json.dumps(
            {
                "responseId": "R_1",
                "values": {"_recordId": "R_1", "QID1": 1},
                "labels": {"QID1": "Yes"},
                "displayedFields": ["QID1"],
                "displayedValues": {"QID1": [1]},
            }
        )
        + "\n"
    )
    display_csv = "\n".join(
        [
            "ResponseId,question_1,question_1_DO_1",
            "Response ID,Question 1,Question 1 Display Order 1",
            '"{""ImportId"":""_recordId""}","{""ImportId"":""QID1""}","{""ImportId"":""QID1_DO"",""choiceId"":""1""}"',
            "R_1,Yes,1",
        ]
    )
    payload_by_progress = {
        "PG_NDJSON": _zip_payload("responses.ndjson", ndjson),
        "PG_CSV": _zip_payload("responses.csv", display_csv),
    }
    progress_by_file = {"FILE_PG_NDJSON": "PG_NDJSON", "FILE_PG_CSV": "PG_CSV"}

    def _fake_send_api_request(*, method: str, path: str, json=None, **_kwargs):
        if method == "POST" and path.endswith("/export-responses"):
            seen_payloads.append(json)
            if json["format"] == "ndjson":
                return _Resp({"result": {"progressId": "PG_NDJSON"}})
            if json["format"] == "csv":
                return _Resp({"result": {"progressId": "PG_CSV"}})
        if method == "GET" and "/export-responses/PG_" in path:
            progress_id = path.split("/")[-1]
            return _Resp(
                {"result": {"status": "complete", "fileId": f"FILE_{progress_id}"}}
            )
        if method == "GET" and path.endswith("/file"):
            file_id = path.split("/")[-2]
            progress_id = progress_by_file[file_id]
            return _Resp(content=payload_by_progress[progress_id])
        raise AssertionError(f"Unexpected call: {method} {path}")

    monkeypatch.setattr(cli_survey, "send_api_request", _fake_send_api_request)

    args = argparse.Namespace(
        survey_id=["SV_ONE"],
        output=str(tmp_path),
        account=None,
        export_format="csv",
        analysis_bundle=True,
        analysis_formats="csv",
        keep_json=False,
    )

    cli_survey.handle_export_responses(args)

    bundle_dir = tmp_path / "Survey One__SV_ONE__responses_bundle"
    assert (bundle_dir / "responses_enriched.csv").exists()
    assert (bundle_dir / "codebook.csv").exists()
    assert (bundle_dir / "manifest.json").exists()
    assert (bundle_dir / "raw" / "responses.ndjson").exists()
    assert (bundle_dir / "raw" / "qualtrics-display-order.csv").exists()
    assert not (bundle_dir / "raw" / "responses.json").exists()
    assert seen_payloads == [
        {"format": "ndjson"},
        {
            "format": "csv",
            "useLabels": True,
            "seenUnansweredRecode": 999,
            "timeZone": "UTC",
            "includeDisplayOrder": True,
        },
    ]
