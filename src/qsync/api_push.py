"""Low-level helpers for making Qualtrics API requests with logging and locks."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping

import requests
from tenacity import Retrying, RetryCallState, stop_after_attempt
from tenacity import retry_if_exception_type, retry_if_result, wait_exponential

from .config import resolve_root
from .rich_support import progress_active, rich_status, should_use_rich
from .push_logger import log_push_event
from .error_catalog import get_docs_url, get_suggestion, is_recoverable
from .survey_lock import ensure_unlocked

_ERROR_BODY_MAX_BYTES = 4096
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

_DEFAULT_RETRY_MAX_ATTEMPTS = 3
_DEFAULT_RETRY_MIN_SECONDS = 2
_DEFAULT_RETRY_MAX_SECONDS = 30
_DEFAULT_RETRY_MULTIPLIER = 2


logger = logging.getLogger(__name__)
_sleep = time.sleep


def get_write_log_path() -> Path:
    """Return the path to the write operations log file (JSONL format).

    Respects environment variables:
    - QSYNC_LOG_DIR or NEWSFLOWS_LOG_DIR: override log directory
    - Defaults to {workspace_root}/logs/qualtrics_write.log
    """
    override = os.environ.get("QSYNC_LOG_DIR") or os.environ.get("NEWSFLOWS_LOG_DIR")
    if override:
        log_dir = Path(override).expanduser()
    else:
        log_dir = (resolve_root(required=False) or Path.cwd()) / "logs"
    return log_dir / "qualtrics_write.log"


def _normalise_path(path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    trimmed = path.lstrip("/")
    if trimmed.startswith("API/"):
        trimmed = trimmed[4:]
    if trimmed.startswith("v3/"):
        trimmed = trimmed[3:]
    return trimmed


def _build_url(base_url: str, path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    trimmed = _normalise_path(path)
    return f"https://{base_url}/API/v3/{trimmed}"


def _parse_retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return min(seconds, float(_DEFAULT_RETRY_MAX_SECONDS))


def _should_retry_response(response: requests.Response | None) -> bool:
    if response is None:
        return False
    should_retry = response.status_code in _RETRYABLE_STATUS_CODES
    if should_retry:
        try:
            response.close()
        except Exception:
            pass
    return should_retry


def _wait_for_retry(retry_state: RetryCallState) -> float:
    outcome = retry_state.outcome
    if outcome is not None and not outcome.failed:
        response = outcome.result()
        if isinstance(response, requests.Response) and response.status_code == 429:
            retry_after = _parse_retry_after_seconds(
                response.headers.get("Retry-After")
            )
            if retry_after is not None:
                return retry_after
    return wait_exponential(
        multiplier=_DEFAULT_RETRY_MULTIPLIER,
        min=_DEFAULT_RETRY_MIN_SECONDS,
        max=_DEFAULT_RETRY_MAX_SECONDS,
    )(retry_state)


def _error_context_from_status(
    status: int | None,
    *,
    reason: str | None = None,
    detail: Any | None = None,
    retry_count: int = 0,
) -> dict[str, Any]:
    suggestion = get_suggestion(status)
    recoverable = is_recoverable(status)
    message = f"{status} {reason}".strip() if status is not None else (reason or "")
    return {
        "type": "HTTPError",
        "message": message,
        "reason": reason,
        "detail": detail,
        "retry_count": max(retry_count, 0),
        "recoverable": recoverable,
        "suggestion": suggestion,
        "docs_url": get_docs_url(),
    }


def _error_context_from_exception(
    exc: Exception, *, retry_count: int = 0
) -> dict[str, Any]:
    exc_type = type(exc).__name__
    suggestion = get_suggestion(None, exc_type=exc_type)
    recoverable = is_recoverable(None, exc_type=exc_type)
    return {
        "type": exc_type,
        "message": str(exc),
        "retry_count": max(retry_count, 0),
        "recoverable": recoverable,
        "suggestion": suggestion,
        "docs_url": get_docs_url(),
    }


def _log_before_sleep(retry_state: RetryCallState) -> None:
    attempt = retry_state.attempt_number
    next_action = retry_state.next_action
    wait_seconds = getattr(next_action, "sleep", None)

    outcome = retry_state.outcome
    if outcome is not None and outcome.failed:
        exc = outcome.exception()
        reason = f"{type(exc).__name__}: {exc}"
    else:
        response = outcome.result() if outcome is not None else None
        status = getattr(response, "status_code", None)
        reason = f"HTTP {status}" if status is not None else "unknown"

    if wait_seconds is None:
        logger.warning("[qsync.retry] attempt=%s reason=%s", attempt, reason)
    else:
        logger.warning(
            "[qsync.retry] attempt=%s wait=%.2fs reason=%s",
            attempt,
            float(wait_seconds),
            reason,
        )


def _retrying() -> Retrying:
    return Retrying(
        reraise=True,
        stop=stop_after_attempt(_DEFAULT_RETRY_MAX_ATTEMPTS),
        retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError))
        | retry_if_result(_should_retry_response),
        wait=_wait_for_retry,
        before_sleep=_log_before_sleep,
        sleep=_sleep,
        retry_error_callback=lambda retry_state: retry_state.outcome.result(),
    )


def send_api_request(
    *,
    action: str,
    method: str,
    base_url: str,
    headers: Mapping[str, str],
    path: str,
    survey_id: str | None = None,
    allow_locked: bool = False,
    log_event: bool = True,
    log_meta: Mapping[str, Any] | None = None,
    log_meta_from_response: (
        Callable[[requests.Response], Mapping[str, Any]] | None
    ) = None,
    timeout: int = 60,
    **request_kwargs,
) -> requests.Response:
    """Send a Qualtrics API request and record the outcome to the push log."""

    method = method.upper()
    url = _build_url(base_url, path)
    attempts = 0

    if survey_id and not allow_locked:
        ensure_unlocked(survey_id)

    request_headers: dict[str, str] = dict(headers)
    if request_kwargs.get("files") is not None:
        # Let `requests` set the correct multipart boundary; an explicit JSON
        # Content-Type will break uploads.
        request_headers.pop("Content-Type", None)

    def _do_request() -> requests.Response:
        nonlocal attempts
        attempts += 1
        return requests.request(
            method,
            url,
            headers=request_headers,
            timeout=timeout,
            **request_kwargs,
        )

    def _format_spinner_label() -> str:
        trimmed = path
        if trimmed.startswith("http://") or trimmed.startswith("https://"):
            trimmed = _normalise_path(trimmed)
        return f"{method} {trimmed}"

    try:
        if should_use_rich() and not progress_active():
            with rich_status(f"{_format_spinner_label()}..."):
                response = _retrying()(_do_request)
        else:
            response = _retrying()(_do_request)
    except Exception as exc:
        if log_event:
            error_context = _error_context_from_exception(
                exc,
                retry_count=max(attempts - 1, 0),
            )
            log_push_event(
                action,
                method=method,
                path=url,
                survey_id=survey_id,
                status=None,
                error=error_context,
                meta=log_meta,
            )
        raise

    if not response.ok:
        if log_event:
            detail: Any
            content_type = (response.headers.get("Content-Type") or "").lower()
            wants_json = ("application/json" in content_type) or content_type.endswith(
                "+json"
            )
            if request_kwargs.get("stream"):
                wants_json = False

            if wants_json:
                try:
                    detail = response.json()
                except ValueError:
                    detail = None
            else:
                detail = None

            if detail is None:
                raw: bytes = b""
                try:
                    remaining = _ERROR_BODY_MAX_BYTES
                    chunks: list[bytes] = []
                    for chunk in response.iter_content(chunk_size=min(8192, remaining)):
                        if not chunk:
                            continue
                        chunks.append(chunk[:remaining])
                        remaining -= len(chunks[-1])
                        if remaining <= 0:
                            break
                    raw = b"".join(chunks)
                except Exception:
                    raw = b""

                encoding = response.encoding or "utf-8"
                try:
                    text = raw.decode(encoding, errors="replace")
                except LookupError:
                    text = raw.decode("utf-8", errors="replace")

                truncated = len(raw) >= _ERROR_BODY_MAX_BYTES
                detail = (
                    text + f"... <truncated to {_ERROR_BODY_MAX_BYTES} bytes>"
                    if truncated
                    else text
                )

            error_context = _error_context_from_status(
                response.status_code,
                reason=response.reason,
                detail=detail,
                retry_count=max(attempts - 1, 0),
            )
            log_push_event(
                action,
                method=method,
                path=url,
                survey_id=survey_id,
                status=response.status_code,
                error=error_context,
                meta=log_meta,
            )
        response.raise_for_status()

    if log_event:
        final_meta: dict[str, Any] | None = dict(log_meta) if log_meta else None
        if log_meta_from_response is not None:
            try:
                extra = log_meta_from_response(response)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[qsync.log_meta_from_response] action=%s error=%s:%s",
                    action,
                    type(exc).__name__,
                    exc,
                )
                extra = None
            if extra:
                if final_meta is None:
                    final_meta = {}
                final_meta.update(dict(extra))

        log_push_event(
            action,
            method=method,
            path=url,
            survey_id=survey_id,
            status=response.status_code,
            meta=final_meta,
        )
    return response


def send_api_request_bytes(**kwargs) -> bytes:
    """Convenience wrapper around `send_api_request()` for binary responses."""

    response = send_api_request(**kwargs)
    return response.content
