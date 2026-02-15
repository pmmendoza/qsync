"""Qualtrics survey cache helpers (download, backup, and question pushes)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

from .api_push import send_api_request
from .config import get_client_config, resolve_root

SURVEY_VERSION_DESCRIPTION_MAX_CHARS = 140


def _workspace_root() -> Path:
    return resolve_root(required=False) or Path.cwd()


def _surveys_dir() -> Path:
    return _workspace_root() / "surveys"


def _backups_dir() -> Path:
    return _surveys_dir() / "backups"


# Re-export for backward compatibility if needed, though usage should migrate to config.py
def build_api_session(env: Dict[str, str] | None = None) -> Tuple[str, Dict[str, str]]:
    """Return (base_url, headers) for Qualtrics API calls.

    Deprecated: Use qsync.config.get_client_config instead.
    """
    return get_client_config(env)


def _sanitize_filename(name: str) -> str:
    """Allow alphanumerics plus ._- by replacing other chars with underscores."""

    import re

    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_.")
    return sanitized or "survey"


def _fetch_survey_name(base_url: str, headers: Dict[str, str], survey_id: str) -> str:
    resp = send_api_request(
        action="qsync.survey.fetch.name",
        method="GET",
        base_url=base_url,
        headers=headers,
        path=f"surveys/{survey_id}",
        log_event=False,
        timeout=30,
    )
    result = resp.json().get("result", {})
    return result.get("name", survey_id)


def _fetch_survey_definition(
    base_url: str, headers: Dict[str, str], survey_id: str
) -> dict:
    resp = send_api_request(
        action="qsync.survey.fetch.definition",
        method="GET",
        base_url=base_url,
        headers=headers,
        path=f"survey-definitions/{survey_id}",
        log_event=False,
        timeout=60,
    )
    return resp.json()


def _save_definition(dir_: Path, name: str, survey_id: str, payload: dict) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    filename = f"{_sanitize_filename(name)}__{survey_id}.json"
    target = dir_ / filename
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return target


def find_cached_survey_file(survey_id: str, *, in_backups: bool = False) -> Path | None:
    """Return the first matching cached survey JSON for a given SurveyID, if any."""

    base = _backups_dir() if in_backups else _surveys_dir()
    if not base.exists():
        return None
    candidates = sorted(base.glob(f"*__{survey_id}.json"))
    return candidates[0] if candidates else None


def download_survey_definition(
    survey_id: str,
    *,
    target_dir: Path | None = None,
    env: dict[str, str] | None = None,
) -> Path:
    """Download the full survey definition JSON and save it under target_dir.

    Returns the path to the saved file.
    """

    base_url, headers = get_client_config(env)
    target_dir = target_dir or _surveys_dir()

    name = _fetch_survey_name(base_url, headers, survey_id)
    definition = _fetch_survey_definition(base_url, headers, survey_id)
    return _save_definition(target_dir, name, survey_id, definition)


def fetch_survey_definition_live(survey_id: str) -> dict:
    """Fetch the live survey definition without writing to disk."""
    base_url, headers = get_client_config()
    return _fetch_survey_definition(base_url, headers, survey_id)


def ensure_backup(survey_id: str) -> Path:
    """Ensure a backup JSON exists in BACKUPS_DIR for this survey.

    If none exists, downloads a fresh copy from Qualtrics.
    Returns the backup path.
    """

    existing = find_cached_survey_file(survey_id, in_backups=True)
    if existing:
        return existing
    return download_survey_definition(survey_id, target_dir=_backups_dir())


@dataclass
class SurveyCache:
    """Container for a loaded survey definition and its on-disk path."""

    survey_id: str
    path: Path
    payload: dict

    @property
    def questions(self) -> Dict[str, dict]:
        """Return the `Questions` mapping from the cached survey payload."""

        return self.payload.get("result", {}).get("Questions", {})

    @property
    def blocks(self) -> Dict[str, dict]:
        """Return the `Blocks` mapping from the cached survey payload."""

        return self.payload.get("result", {}).get("Blocks", {})

    def save(self) -> None:
        """Write the survey payload back to `self.path`."""

        self.path.write_text(json.dumps(self.payload, indent=2), encoding="utf-8")


def load_cached_survey(survey_id: str) -> SurveyCache:
    """Load the cached survey JSON for a survey, downloading if necessary."""

    surveys_dir = _surveys_dir()
    surveys_dir.mkdir(parents=True, exist_ok=True)
    cached = find_cached_survey_file(survey_id, in_backups=False)
    if not cached:
        cached = download_survey_definition(survey_id, target_dir=surveys_dir)
    payload = json.loads(cached.read_text(encoding="utf-8"))
    return SurveyCache(survey_id=survey_id, path=cached, payload=payload)


def _comparable_payload(payload: dict | None) -> dict | None:
    if payload is None:
        return None
    if "result" in payload and isinstance(payload["result"], dict):
        return payload["result"]
    return payload


def refresh_survey_cache(survey_id: str) -> Tuple[SurveyCache, bool]:
    """Refresh the survey cache from Qualtrics and report if it changed.

    Returns (cache, changed_flag).
    """

    surveys_dir = _surveys_dir()
    surveys_dir.mkdir(parents=True, exist_ok=True)
    old_path = find_cached_survey_file(survey_id, in_backups=False)
    old_payload: dict | None = None
    if old_path and old_path.exists():
        old_payload = json.loads(old_path.read_text(encoding="utf-8"))

    new_path = download_survey_definition(survey_id, target_dir=surveys_dir)
    new_payload = json.loads(new_path.read_text(encoding="utf-8"))

    if old_payload is None:
        changed = False
    else:
        changed = _comparable_payload(old_payload) != _comparable_payload(new_payload)
    return SurveyCache(survey_id=survey_id, path=new_path, payload=new_payload), changed


def push_questions(
    survey: SurveyCache,
    qids: Iterable[str],
    *,
    context: Dict[str, Any] | None = None,
) -> None:
    """Push updated question definitions for the given QIDs to Qualtrics."""

    if not qids:
        return

    base_url, headers = get_client_config()

    shared_meta = {"context": context} if context else None

    for qid in qids:
        question = survey.questions.get(qid)
        if not question:
            continue
        meta = {"question_id": qid}
        if shared_meta:
            meta.update(shared_meta)
        send_api_request(
            action="qsync.survey.push.question",
            method="PUT",
            base_url=base_url,
            headers=headers,
            path=f"survey-definitions/{survey.survey_id}/questions/{qid}",
            survey_id=survey.survey_id,
            log_meta=meta,
            json=question,
        )


def push_survey_flow(
    survey: SurveyCache,
    *,
    context: Dict[str, Any] | None = None,
) -> None:
    """Push updated SurveyFlow to Qualtrics."""

    base_url, headers = get_client_config()
    flow = survey.payload.get("result", {}).get("SurveyFlow", {})
    meta = {"context": context} if context else None
    send_api_request(
        action="qsync.survey.push.flow",
        method="PUT",
        base_url=base_url,
        headers=headers,
        path=f"survey-definitions/{survey.survey_id}/flow",
        survey_id=survey.survey_id,
        log_meta=meta,
        json=flow,
    )


def publish_survey_definition(
    survey_id: str,
    *,
    description: str,
    published: bool = True,
    context: Dict[str, Any] | None = None,
    base_url: str | None = None,
    headers: dict | None = None,
) -> dict:
    """Publish staged survey-definition changes by creating a new survey version.

    Qualtrics survey-definition edits are staged until a published version is
    created. This helper calls:

      POST /survey-definitions/{surveyId}/versions

    Args:
        survey_id: Survey ID to publish
        description: Version description (max 140 chars)
        published: Whether to mark as published
        context: Additional context metadata
        base_url: Optional custom base URL (defaults to get_client_config())
        headers: Optional custom headers (defaults to get_client_config())

    Notes:
    - Qualtrics expects title-case field names: `Description`, `Published`.
    - Version descriptions are limited to 140 characters (enforced by qsync).
    - Publishing is distinct from activation (`isActive`); callers should treat
      activation as a separate operation.
    - Stage 0 enhancement (QSYNC-XACCT-005): Added base_url and headers parameters
      for cross-account support while maintaining backward compatibility.
    """

    desc = (description or "").strip()
    if not desc:
        raise ValueError("publish_survey_definition requires a non-empty description")
    if len(desc) > SURVEY_VERSION_DESCRIPTION_MAX_CHARS:
        raise ValueError(
            "publish_survey_definition description must be <= "
            f"{SURVEY_VERSION_DESCRIPTION_MAX_CHARS} characters "
            f"(got {len(desc)})"
        )

    # Use provided credentials or default
    if base_url is None or headers is None:
        base_url, headers = get_client_config()

    meta: Dict[str, Any] = {"description": desc, "published": bool(published)}
    if context:
        meta["context"] = context

    def _log_meta_from_publish_response(resp) -> Dict[str, Any]:
        try:
            payload = resp.json()
        except ValueError:
            return {}
        metadata = (payload.get("result") or {}).get("metadata") or {}
        if not isinstance(metadata, dict):
            return {}
        version_id = metadata.get("versionID")
        version_number = metadata.get("versionNumber")
        if version_id is None and version_number is None:
            return {}
        out: Dict[str, Any] = {}
        if version_id is not None:
            out["version_id"] = version_id
        if version_number is not None:
            out["version_number"] = version_number
        if "creationDate" in metadata:
            out["creation_date"] = metadata.get("creationDate")
        if "wasPublished" in metadata:
            out["was_published"] = metadata.get("wasPublished")
        if "published" in metadata:
            out["published_result"] = metadata.get("published")
        return out

    resp = send_api_request(
        action="qsync.survey.publish.definition",
        method="POST",
        base_url=base_url,
        headers=headers,
        path=f"survey-definitions/{survey_id}/versions",
        survey_id=survey_id,
        log_meta=meta,
        log_meta_from_response=_log_meta_from_publish_response,
        json={"Description": desc, "Published": bool(published)},
    )
    try:
        return resp.json()
    except ValueError:
        return {}


def list_survey_versions(
    survey_id: str,
    *,
    base_url: str | None = None,
    headers: dict | None = None,
) -> dict:
    """List survey-definition versions for a survey.

    Calls:
      GET /survey-definitions/{surveyId}/versions

    Returns a dict with:
    - survey_id
    - current_published_version_id (best-effort)
    - versions: list of `metadata` dicts (newest-first) with an added
      `current_published` boolean flag.
    """

    if base_url is None or headers is None:
        base_url, headers = get_client_config()
    resp = send_api_request(
        action="qsync.survey.versions.list",
        method="GET",
        base_url=base_url,
        headers=headers,
        path=f"survey-definitions/{survey_id}/versions",
        log_event=False,
        timeout=60,
    )
    payload = resp.json()
    result = payload.get("result") or {}
    elements = result.get("elements") or []

    versions: list[dict[str, Any]] = []
    for elem in elements:
        if not isinstance(elem, dict):
            continue
        meta = elem.get("metadata") or {}
        if not isinstance(meta, dict):
            continue
        versions.append(dict(meta))

    current_published_version_id = next(
        (
            v.get("versionID")
            for v in versions
            if v.get("published") is True and v.get("versionID")
        ),
        None,
    )
    for v in versions:
        v["current_published"] = bool(
            current_published_version_id
            and v.get("versionID") == current_published_version_id
        )

    return {
        "survey_id": survey_id,
        "current_published_version_id": current_published_version_id,
        "versions": versions,
    }


def fetch_survey_version(
    survey_id: str,
    *,
    version_id: str,
    fmt: str = "json",
    base_url: str | None = None,
    headers: dict | None = None,
) -> dict:
    """Fetch a specific survey-definition version by VersionID.

    Calls:
      GET /survey-definitions/{surveyId}/versions/{versionId}

    Notes:
    - `fmt="qsf"` maps to `?format=qsf`, which returns JSON (QSF-like payload) in
      observed responses.
    """

    version_id = (version_id or "").strip()
    if not version_id:
        raise ValueError("fetch_survey_version requires a non-empty version_id")

    params: dict[str, str] | None = None
    if fmt == "qsf":
        params = {"format": "qsf"}
    elif fmt != "json":
        raise ValueError("fetch_survey_version fmt must be 'json' or 'qsf'")

    if base_url is None or headers is None:
        base_url, headers = get_client_config()
    resp = send_api_request(
        action="qsync.survey.version.fetch",
        method="GET",
        base_url=base_url,
        headers=headers,
        path=f"survey-definitions/{survey_id}/versions/{version_id}",
        log_event=False,
        params=params,
        timeout=60,
    )
    try:
        return resp.json()
    except ValueError:
        return {"_raw": resp.text}
