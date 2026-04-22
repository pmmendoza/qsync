"""Helpers for Qualtrics survey response exports.

The Qualtrics response export API supports `csv`, `tsv`, `spss`, `json`,
`ndjson`, and `xml` for the export formats used by `qsync`.

One API wrinkle matters here: Qualtrics rejects several tabular-export options
for `json` and `ndjson`. This module keeps that rule in one place so the CLI can
offer every supported format while still sending a valid start-export payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
import zipfile
from typing import Any, Callable, Iterable, Mapping

DEFAULT_RESPONSE_EXPORT_FORMAT = "csv"
SUPPORTED_RESPONSE_EXPORT_FORMATS = (
    "csv",
    "tsv",
    "spss",
    "json",
    "ndjson",
    "xml",
)
JSON_RESPONSE_EXPORT_FORMATS = frozenset({"json", "ndjson"})


class ResponseExportError(Exception):
    """Raised when a Qualtrics response export workflow fails."""


def normalize_response_export_format(value: str | None) -> str:
    """Return a validated lower-case response export format.

    The CLI accepts mixed-case input, but the Qualtrics API expects the lower-
    case wire values.
    """

    export_format = (value or DEFAULT_RESPONSE_EXPORT_FORMAT).strip().lower()
    if export_format not in SUPPORTED_RESPONSE_EXPORT_FORMATS:
        allowed = ", ".join(SUPPORTED_RESPONSE_EXPORT_FORMATS)
        raise ValueError(
            f"Unsupported response export format '{value}'. Choose one of: {allowed}."
        )
    return export_format


def build_response_export_payload(
    *,
    export_format: str,
    include_display_order: bool = False,
) -> dict[str, Any]:
    """Build a start-export payload valid for the requested Qualtrics format.

    For tabular formats we keep the historical qsync defaults:
    - `useLabels=True`
    - `seenUnansweredRecode=999`
    - `timeZone="UTC"`

    Qualtrics documents those options as invalid for `json` and `ndjson`, so
    those formats get a minimal payload containing only the requested format.
    """

    normalized = normalize_response_export_format(export_format)
    if include_display_order and normalized in JSON_RESPONSE_EXPORT_FORMATS:
        raise ValueError(
            "`includeDisplayOrder` is not valid for JSON/NDJSON response exports."
        )
    payload: dict[str, Any] = {"format": normalized}
    if normalized not in JSON_RESPONSE_EXPORT_FORMATS:
        payload.update(
            {
                "useLabels": True,
                "seenUnansweredRecode": 999,
                "timeZone": "UTC",
            }
        )
        if include_display_order:
            payload["includeDisplayOrder"] = True
    return payload


def safe_response_export_name(value: str) -> str:
    """Return a filesystem-safe survey name for response export artifacts."""

    return "".join(c if c.isalnum() or c in " -_" else "_" for c in value).strip()


@dataclass(frozen=True)
class ResponseExportFile:
    """Metadata for one downloaded Qualtrics response export file."""

    export_format: str
    payload: dict[str, Any]
    zip_path: Path | None
    extracted_paths: tuple[Path, ...]
    primary_path: Path
    progress_id: str
    file_id: str


RequestFunc = Callable[..., Any]


def _iter_response_content(response: Any) -> Iterable[bytes]:
    iterator = getattr(response, "iter_content", None)
    if callable(iterator):
        yield from iterator(chunk_size=8192)
        return
    content = getattr(response, "content", None)
    if content is not None:
        yield content


def download_response_export_file(
    *,
    base_url: str,
    headers: Mapping[str, str],
    survey_id: str,
    survey_name: str,
    output_dir: Path,
    export_format: str,
    request_func: RequestFunc,
    include_display_order: bool = False,
    zip_stem: str | None = None,
    keep_zip: bool = True,
    primary_filename: str | None = None,
    poll_interval_seconds: float = 2.0,
) -> ResponseExportFile:
    """Run Qualtrics' start/poll/download response export workflow.

    The function intentionally logs no respondent values. It only returns file
    paths and export metadata for the caller to report or include in manifests.
    """

    normalized = normalize_response_export_format(export_format)
    payload = build_response_export_payload(
        export_format=normalized,
        include_display_order=include_display_order,
    )

    response = request_func(
        action="qsync.survey.export.responses.start",
        method="POST",
        base_url=base_url,
        headers=headers,
        path=f"surveys/{survey_id}/export-responses",
        log_event=False,
        json=payload,
        timeout=60,
    )
    progress_id = response.json()["result"]["progressId"]

    progress_status = "inProgress"
    file_id = None
    while progress_status not in ("complete", "failed"):
        check_response = request_func(
            action="qsync.survey.export.responses.poll",
            method="GET",
            base_url=base_url,
            headers=headers,
            path=f"surveys/{survey_id}/export-responses/{progress_id}",
            log_event=False,
            timeout=60,
        )
        result = check_response.json()["result"]
        progress_status = result["status"]

        if progress_status == "failed":
            raise ResponseExportError(f"Response export failed for {survey_id}")

        if progress_status == "complete":
            file_id = result["fileId"]
        else:
            time.sleep(poll_interval_seconds)

    if not file_id:
        raise ResponseExportError(
            f"Response export completed without fileId for {survey_id}"
        )

    download_response = request_func(
        action="qsync.survey.export.responses.download",
        method="GET",
        base_url=base_url,
        headers=headers,
        path=f"surveys/{survey_id}/export-responses/{file_id}/file",
        log_event=False,
        stream=True,
        timeout=120,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = safe_response_export_name(survey_name) or survey_id
    stem = zip_stem or f"{safe_name}_{survey_id}_{normalized}"
    zip_path = output_dir / f"{stem}.zip"
    with zip_path.open("wb") as f:
        for chunk in _iter_response_content(download_response):
            if chunk:
                f.write(chunk)

    extracted_paths: list[Path] = []
    primary_path: Path | None = None
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        names = zip_ref.namelist()
        if not names:
            raise ResponseExportError(f"Response export zip was empty for {survey_id}")
        if primary_filename is not None:
            primary_path = output_dir / primary_filename
            with zip_ref.open(names[0]) as source, primary_path.open("wb") as target:
                target.write(source.read())
            extracted_paths.append(primary_path)
        else:
            for name in names:
                zip_ref.extract(name, output_dir)
                extracted_paths.append(output_dir / name)
            primary_path = extracted_paths[0]

    final_zip_path: Path | None = zip_path
    if not keep_zip:
        zip_path.unlink(missing_ok=True)
        final_zip_path = None

    return ResponseExportFile(
        export_format=normalized,
        payload=payload,
        zip_path=final_zip_path,
        extracted_paths=tuple(extracted_paths),
        primary_path=primary_path,
        progress_id=progress_id,
        file_id=file_id,
    )
