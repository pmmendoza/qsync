"""Compute basic response stats by exporting responses via the Qualtrics API."""

from __future__ import annotations

import io
import json
import time
import warnings
import zipfile
from typing import Any, Dict, Tuple

from .api_push import send_api_request, send_api_request_bytes
from .config import get_client_config


class ResponseStatsError(RuntimeError):
    """Raised when Qualtrics response exports fail."""


def _start_export(base_url: str, headers: Dict[str, str], survey_id: str) -> str:
    payload = {
        "format": "json",
        "compress": True,
    }
    response = send_api_request(
        action="qsync.response.stats.export.start",
        method="POST",
        base_url=base_url,
        headers=headers,
        path=f"surveys/{survey_id}/export-responses",
        log_event=False,
        json=payload,
        timeout=30,
    )
    result = response.json().get("result") or {}
    progress_id = result.get("progressId")
    if not progress_id:
        raise ResponseStatsError(
            f"export-responses call for {survey_id} did not return progressId"
        )
    return progress_id


def _poll_export(
    base_url: str, headers: Dict[str, str], survey_id: str, progress_id: str
) -> str:
    for _ in range(30):
        response = send_api_request(
            action="qsync.response.stats.export.poll",
            method="GET",
            base_url=base_url,
            headers=headers,
            path=f"surveys/{survey_id}/export-responses/{progress_id}",
            log_event=False,
            timeout=30,
        )
        result = response.json().get("result") or {}
        status = (result.get("status") or "").lower()
        if status == "complete":
            file_id = result.get("fileId")
            if not file_id:
                raise ResponseStatsError(
                    f"export-responses for {survey_id} completed without fileId"
                )
            return file_id
        if status == "failed":
            raise ResponseStatsError(f"export-responses for {survey_id} failed")
        time.sleep(2)
    raise ResponseStatsError(f"Timed out waiting for response export for {survey_id}")


def _download_export(
    base_url: str, headers: Dict[str, str], survey_id: str, file_id: str
) -> bytes:
    return send_api_request_bytes(
        action="qsync.response.stats.export.download",
        method="GET",
        base_url=base_url,
        headers=headers,
        path=f"surveys/{survey_id}/export-responses/{file_id}/file",
        log_event=False,
        timeout=60,
    )


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "y", "yes", "t"}


def _classify(values: Dict[str, Any]) -> Tuple[bool, bool]:
    finished = _as_bool(
        values.get("Finished") or values.get("finished") or values.get("status")
    )
    if not finished:
        return False, False
    response_type = (
        values.get("ResponseType") or values.get("responseType") or ""
    ).strip()
    if not response_type:
        channel = (values.get("distributionChannel") or "").strip().lower()
        if channel == "preview":
            response_type = "Survey Preview"
    is_preview = response_type.lower() == "survey preview"
    return True, is_preview


def _count_from_bytes(data: bytes) -> Tuple[int, int]:
    preview = 0
    live = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in archive.namelist():
            with archive.open(name) as fh:
                payload = json.load(fh)
            for response in payload.get("responses", []):
                values = response.get("values") or {}
                finished, is_preview = _classify(values)
                if not finished:
                    continue
                if is_preview:
                    preview += 1
                else:
                    live += 1
    return preview, live


def fetch_finished_counts(survey_id: str) -> Tuple[int, int]:
    """Return `(preview_count, live_count)` for finished responses in a survey."""

    warnings.warn(
        "fetch_finished_counts() is deprecated; use GET /surveys/{id} responseCounts "
        "(qsync.push_policy.load_push_context live-check) instead. This export-based "
        "counting will be removed.",
        DeprecationWarning,
        stacklevel=2,
    )
    base_url, headers = get_client_config()
    progress_id = _start_export(base_url, headers, survey_id)
    file_id = _poll_export(base_url, headers, survey_id, progress_id)
    data = _download_export(base_url, headers, survey_id, file_id)
    return _count_from_bytes(data)
