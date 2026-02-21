"""CLI commands for Prolific ↔ Qualtrics study wiring workflows."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote_plus

import requests

from .api_push import send_api_request
from .argparse_support import reorder_subparser_choices
from .config import (
    get_active_account,
    get_client_config,
    load_account_env,
    load_env,
    resolve_env_path,
    resolve_root,
    resolve_scoped_dir,
)
from .prolific_auth import (
    PROLIFIC_QUALTRICS_SCRIPT_URL,
    contains_prolific_qualtrics_script,
    merge_header,
)
from .qualtrics_client import publish_survey_definition
from .rich_support import progress_context, should_use_rich
from .survey_inventory import refresh_inventory

PROLIFIC_API_BASE_URL = "https://api.prolific.com/api/v1"
PROLIFIC_COMPLETION_BASE_URL = "https://app.prolific.com/submissions/complete"
PROLIFIC_PID_PLACEHOLDER = "{{%PROLIFIC_PID%}}"
PROLIFIC_STUDY_ID_PLACEHOLDER = "{{%STUDY_ID%}}"
PROLIFIC_SESSION_ID_PLACEHOLDER = "{{%SESSION_ID%}}"
REQUIRED_PROLIFIC_EMBEDDED_FIELDS = ("STUDY_ID", "SESSION_ID", "PROLIFIC_PID")

STUDIES_FIELDNAMES = [
    "prolific_study_id",
    "prolific_internal_name",
    "prolific_study_name",
    "prolific_status",
    "completion_code",
    "redirect_url_current",
    "fetched_at",
]

MATCH_STATES = {"PROPOSED", "APPROVED", "SKIP", "REVIEW_REQUIRED"}
MATCH_MODES = {"prefix_exact", "prefix_unique", "manual", "ambiguous", "none"}
MATCH_CONFIDENCE = {"high", "medium", "low"}

MATCHES_FIELDNAMES = [
    "state",
    "prolific_study_id",
    "prolific_study_name",
    "prolific_internal_name",
    "match_formula",
    "qualtrics_survey_name",
    "qualtrics_survey_id",
    "match_mode",
    "match_confidence",
    "notes",
    "completion_code",
    "name_prefix_key",
    "desired_prolific_redirect_url",
    "desired_qualtrics_eos_redirect_url",
    "last_proposed_at",
]


@dataclass(frozen=True)
class MatchSurvey:
    survey_id: str
    name: str


@dataclass(frozen=True)
class MatchStudy:
    study_id: str
    internal_name: str
    study_name: str
    completion_code: str


@dataclass
class WirePlanRow:
    row: dict[str, str]
    blocked_reason: str | None
    prolific_current_redirect_url: str
    prolific_desired_redirect_url: str
    qualtrics_current_eos_redirect_url: str
    qualtrics_desired_eos_redirect_url: str
    qualtrics_current_header: str
    qualtrics_new_header: str
    qualtrics_first_embedded_flow_id: str
    qualtrics_first_embedded_fields: list[str]
    qualtrics_missing_embedded_fields: list[str]
    options_payload: dict[str, Any] | None


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _workspace_root() -> Path:
    return resolve_root(required=False) or Path.cwd()


def _resolve_account_from_args(args: argparse.Namespace) -> str | None:
    raw = getattr(args, "account", None)
    if isinstance(raw, str):
        name = raw.strip()
        if name:
            return name
    try:
        return get_active_account()
    except Exception:
        return None


def _get_client_config_for_account(account: str | None) -> tuple[str, dict[str, str]]:
    if account:
        env = load_account_env(account, root=_workspace_root())
        return get_client_config(env)
    return get_client_config()


def _scoped_prolific_dir(*, account: str | None) -> Path:
    root = _workspace_root()
    surveys_dir = resolve_scoped_dir("surveys", root=root, account=account)
    return (surveys_dir / "prolific").resolve()


def _default_studies_csv_path(*, account: str | None) -> Path:
    return _scoped_prolific_dir(account=account) / "studies.csv"


def _default_matches_csv_path(*, account: str | None) -> Path:
    return _scoped_prolific_dir(account=account) / "matches.csv"


def _journal_dir(*, account: str | None) -> Path:
    return _scoped_prolific_dir(account=account) / "wire_journal"


def _resolve_studies_csv_path(args: argparse.Namespace, *, account: str | None) -> Path:
    raw = str(getattr(args, "studies", "") or "").strip()
    if raw:
        path = Path(raw)
        if path.is_absolute():
            return path
        return (_workspace_root() / path).resolve()
    return _default_studies_csv_path(account=account)


def _resolve_matches_csv_path(args: argparse.Namespace, *, account: str | None) -> Path:
    raw = str(getattr(args, "matches", "") or "").strip()
    if raw:
        path = Path(raw)
        if path.is_absolute():
            return path
        return (_workspace_root() / path).resolve()
    return _default_matches_csv_path(account=account)


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(line for line in fh if not line.lstrip().startswith("#"))
        return [dict(row) for row in reader]


def _write_csv_rows(
    path: Path,
    *,
    rows: Sequence[dict[str, Any]],
    fieldnames: Sequence[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})


def _normalize_state(value: str | None, *, default: str = "PROPOSED") -> str:
    raw = str(value or "").strip().upper()
    if raw in MATCH_STATES:
        return raw
    return default


def normalize_name(value: str | None) -> str:
    text = str(value or "").strip().casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def prefix_key(value: str | None, *, prefix_tokens: int) -> str:
    token_count = max(int(prefix_tokens or 1), 1)
    tokens = normalize_name(value).split()
    if not tokens:
        return ""
    return " ".join(tokens[:token_count])


def _index_all_prefix_keys(
    *,
    mapping: dict[str, list[Any]],
    value: str | None,
    item: Any,
) -> None:
    tokens = normalize_name(value).split()
    if not tokens:
        return
    for token_count in range(1, len(tokens) + 1):
        key = " ".join(tokens[:token_count])
        mapping.setdefault(key, []).append(item)


def _resolve_prefix_candidates(
    *,
    value: str | None,
    min_prefix_tokens: int,
    source_prefix_map: dict[str, list[Any]],
    target_prefix_map: dict[str, list[Any]],
) -> tuple[str, list[Any], list[Any], bool]:
    tokens = normalize_name(value).split()
    if not tokens:
        return "", [], [], False

    start = max(int(min_prefix_tokens or 1), 1)
    if start > len(tokens):
        start = len(tokens)

    best_key = ""
    best_source_candidates: list[Any] = []
    best_target_candidates: list[Any] = []

    for token_count in range(start, len(tokens) + 1):
        key = " ".join(tokens[:token_count])
        source_candidates = source_prefix_map.get(key, [])
        target_candidates = target_prefix_map.get(key, [])

        if source_candidates and target_candidates:
            best_key = key
            best_source_candidates = source_candidates
            best_target_candidates = target_candidates

        if len(source_candidates) == 1 and len(target_candidates) == 1:
            return key, source_candidates, target_candidates, True

    if best_key:
        return best_key, best_source_candidates, best_target_candidates, False

    fallback_key = " ".join(tokens[:start])
    return (
        fallback_key,
        source_prefix_map.get(fallback_key, []),
        target_prefix_map.get(fallback_key, []),
        False,
    )


def exact_name_key(
    value: str | None,
    *,
    drop_trailing_p: bool = False,
) -> str:
    tokens = normalize_name(value).split()
    if drop_trailing_p and tokens and tokens[-1] == "p":
        tokens = tokens[:-1]
    return " ".join(tokens)


def build_match_formula(
    prolific_internal_name: str | None,
    qualtrics_survey_name: str | None,
) -> str:
    left_tokens = exact_name_key(prolific_internal_name, drop_trailing_p=True).split()
    right_tokens = set(exact_name_key(qualtrics_survey_name, drop_trailing_p=False).split())
    if not left_tokens or not right_tokens:
        return ""
    overlap: list[str] = []
    seen: set[str] = set()
    for token in left_tokens:
        if token in right_tokens and token not in seen:
            overlap.append(token)
            seen.add(token)
    return " ".join(overlap)


def build_qualtrics_form_redirect_url(base_url: str, survey_id: str) -> str:
    base = str(base_url or "").strip()
    sid = str(survey_id or "").strip()
    if not base or not sid:
        return ""
    return (
        f"https://{base}/jfe/form/{sid}?"
        f"PROLIFIC_PID={PROLIFIC_PID_PLACEHOLDER}&"
        f"STUDY_ID={PROLIFIC_STUDY_ID_PLACEHOLDER}&"
        f"SESSION_ID={PROLIFIC_SESSION_ID_PLACEHOLDER}"
    )


def build_prolific_completion_url(completion_code: str) -> str:
    code = str(completion_code or "").strip()
    if not code:
        return ""
    return f"{PROLIFIC_COMPLETION_BASE_URL}?cc={quote_plus(code)}"


def _resolve_prolific_token(args: argparse.Namespace, *, account: str | None) -> str:
    explicit = str(getattr(args, "prolific_token", "") or "").strip()
    if explicit:
        return explicit

    for key in ("PROLIFIC_API_TOKEN", "PROLIFIC_TOKEN"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value

    root = _workspace_root()

    if account:
        try:
            account_env = load_account_env(account, root=root)
            for key in ("PROLIFIC_API_TOKEN", "PROLIFIC_TOKEN"):
                value = str(account_env.get(key) or "").strip()
                if value:
                    return value
        except Exception:
            pass

    default_env = load_env(resolve_env_path(root=root))
    for key in ("PROLIFIC_API_TOKEN", "PROLIFIC_TOKEN"):
        value = str(default_env.get(key) or "").strip()
        if value:
            return value

    raise SystemExit(
        "[qsync:prolific] ERROR: missing Prolific API token. "
        "Set PROLIFIC_API_TOKEN in .env (or pass --prolific-token)."
    )


def _prolific_request(
    *,
    method: str,
    token: str,
    path_or_url: str,
    params: dict[str, Any] | None = None,
    json_payload: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    url = (
        path_or_url
        if path_or_url.startswith("http://") or path_or_url.startswith("https://")
        else f"{PROLIFIC_API_BASE_URL}/{path_or_url.lstrip('/')}"
    )
    headers = {
        "Authorization": f"Token {token}",
        "Accept": "application/json",
    }
    if json_payload is not None:
        headers["Content-Type"] = "application/json"

    response = requests.request(
        method.upper(),
        url,
        headers=headers,
        params=params,
        json=json_payload,
        timeout=timeout,
    )

    if not response.ok:
        detail: Any
        try:
            detail = response.json()
        except ValueError:
            detail = response.text
        raise RuntimeError(
            f"Prolific API request failed ({method.upper()} {url}): "
            f"{response.status_code} {response.reason} | {detail}"
        )

    if response.status_code == 204 or not response.content:
        return {}

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Prolific API returned non-JSON response for {method.upper()} {url}."
        ) from exc

    if not isinstance(payload, dict):
        raise RuntimeError(
            f"Prolific API returned unexpected payload type for {method.upper()} {url}."
        )

    return payload


def _extract_completion_code(study: dict[str, Any]) -> str:
    direct = str(study.get("completion_code") or "").strip()
    if direct:
        return direct

    completion_codes = study.get("completion_codes")
    if isinstance(completion_codes, list):
        for entry in completion_codes:
            if isinstance(entry, dict):
                code = str(entry.get("code") or "").strip()
                if code:
                    return code
            elif isinstance(entry, str):
                code = entry.strip()
                if code:
                    return code
    return ""


def _list_prolific_studies(
    *,
    token: str,
    state: str | None = None,
) -> list[dict[str, Any]]:
    studies: list[dict[str, Any]] = []
    next_url = f"{PROLIFIC_API_BASE_URL}/studies/"
    first_page = True

    while next_url:
        params: dict[str, Any] | None = None
        if first_page and state:
            params = {"state": state}
        payload = _prolific_request(
            method="GET",
            token=token,
            path_or_url=next_url,
            params=params,
        )
        elements = payload.get("results")
        if isinstance(elements, list):
            studies.extend([s for s in elements if isinstance(s, dict)])
        next_raw = payload.get("next")
        next_url = str(next_raw).strip() if next_raw else ""
        first_page = False

    return studies


def _fetch_prolific_study(*, token: str, study_id: str) -> dict[str, Any]:
    return _prolific_request(method="GET", token=token, path_or_url=f"studies/{study_id}/")


def _write_prolific_study_redirect(
    *,
    token: str,
    study_id: str,
    redirect_url: str,
) -> dict[str, Any]:
    return _prolific_request(
        method="PATCH",
        token=token,
        path_or_url=f"studies/{study_id}/",
        json_payload={"external_study_url": redirect_url},
    )


def _resolve_auth_snippet(
    args: argparse.Namespace,
    *,
    account: str | None,
) -> str | None:
    inline = str(getattr(args, "auth_snippet", "") or "").strip()
    if inline:
        return inline

    snippet_file_raw = str(getattr(args, "auth_snippet_file", "") or "").strip()
    if snippet_file_raw:
        snippet_path = Path(snippet_file_raw)
        if not snippet_path.is_absolute():
            snippet_path = (_workspace_root() / snippet_path).resolve()
        return snippet_path.read_text(encoding="utf-8").strip()

    env_sources: list[dict[str, str]] = []
    if account:
        try:
            env_sources.append(load_account_env(account, root=_workspace_root()))
        except Exception:
            pass
    env_sources.append(load_env(resolve_env_path(root=_workspace_root())))

    env_snippet = ""
    for env in env_sources:
        env_snippet = str(env.get("PROLIFIC_AUTH_SNIPPET") or "").strip()
        if env_snippet:
            break
    if env_snippet:
        return env_snippet

    token = str(getattr(args, "auth_token", "") or "").strip()
    if not token:
        for env in env_sources:
            token = str(env.get("PROLIFIC_AUTH_TOKEN") or "").strip()
            if token:
                break
    if not token:
        return None

    # Important: this is a public client-side snippet token, not the Prolific API token.
    return (
        "<script type=\"text/javascript\" "
        f"src=\"{PROLIFIC_QUALTRICS_SCRIPT_URL}?"
        f"rid=${{e://Field/ResponseID}}&t={token}\"></script>"
    )


def _fetch_qualtrics_options(
    *,
    base_url: str,
    headers: dict[str, str],
    survey_id: str,
) -> dict[str, Any]:
    response = send_api_request(
        action="qsync.prolific.qualtrics.options.fetch",
        method="GET",
        base_url=base_url,
        headers=headers,
        path=f"survey-definitions/{survey_id}/options",
        survey_id=survey_id,
        timeout=30,
    )
    payload = response.json().get("result")
    return payload if isinstance(payload, dict) else {}


def _fetch_qualtrics_definition(
    *,
    base_url: str,
    headers: dict[str, str],
    survey_id: str,
) -> dict[str, Any]:
    response = send_api_request(
        action="qsync.prolific.qualtrics.definition.fetch",
        method="GET",
        base_url=base_url,
        headers=headers,
        path=f"survey-definitions/{survey_id}",
        survey_id=survey_id,
        timeout=60,
    )
    payload = response.json().get("result")
    return payload if isinstance(payload, dict) else {}


def _iter_flow_nodes(flow_list: Any) -> Sequence[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    if not isinstance(flow_list, list):
        return nodes

    for node in flow_list:
        if not isinstance(node, dict):
            continue
        nodes.append(node)
        for key in ("Flow", "Then", "Else", "ElseFlow"):
            sub = node.get(key)
            if isinstance(sub, list):
                nodes.extend(_iter_flow_nodes(sub))
    return nodes


def _first_embedded_data_block(
    survey_result: dict[str, Any],
) -> tuple[str, list[str]]:
    survey_flow = survey_result.get("SurveyFlow") or {}
    for node in _iter_flow_nodes(survey_flow.get("Flow")):
        if str(node.get("Type") or "").strip() != "EmbeddedData":
            continue
        flow_id = str(node.get("FlowID") or "").strip()
        fields: list[str] = []
        entries = node.get("EmbeddedData") or []
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                field = str(entry.get("Field") or "").strip()
                if field:
                    fields.append(field)
        return flow_id, fields
    return "", []


def _missing_required_prolific_embedded_fields(fields: Sequence[str]) -> list[str]:
    present = {str(field or "").strip().upper() for field in fields if str(field or "").strip()}
    missing: list[str] = []
    for required in REQUIRED_PROLIFIC_EMBEDDED_FIELDS:
        if required not in present:
            missing.append(required)
    return missing


def _write_qualtrics_options(
    *,
    base_url: str,
    headers: dict[str, str],
    survey_id: str,
    options_payload: dict[str, Any],
) -> None:
    send_api_request(
        action="qsync.prolific.qualtrics.options.write",
        method="PUT",
        base_url=base_url,
        headers=headers,
        path=f"survey-definitions/{survey_id}/options",
        survey_id=survey_id,
        json=options_payload,
        timeout=30,
    )


def _activate_survey(*, base_url: str, headers: dict[str, str], survey_id: str) -> None:
    send_api_request(
        action="qsync.prolific.qualtrics.activate",
        method="PUT",
        base_url=base_url,
        headers=headers,
        path=f"surveys/{survey_id}",
        survey_id=survey_id,
        json={"isActive": True},
        timeout=30,
    )


def _inventory_csv_path(*, account: str | None) -> Path:
    surveys_dir = resolve_scoped_dir("surveys", root=_workspace_root(), account=account)
    canonical = (surveys_dir / "inventory.csv").resolve()
    if canonical.exists():
        return canonical
    legacy = (surveys_dir / "qualtrics_surveys.csv").resolve()
    if legacy.exists():
        return legacy
    return canonical


def _load_qualtrics_surveys(*, account: str | None) -> list[MatchSurvey]:
    path = _inventory_csv_path(account=account)
    if not path.exists():
        raise SystemExit(
            f"[qsync:prolific] ERROR: inventory missing at {path}. "
            "Run `qsync survey inventory` or `qsync prolific propose-matches --qualtrics-inventory-refresh`."
        )

    rows = _read_csv_rows(path)
    surveys: list[MatchSurvey] = []
    for row in rows:
        survey_id = str(row.get("id") or "").strip()
        name = str(row.get("name") or "").strip()
        if not survey_id:
            continue
        surveys.append(MatchSurvey(survey_id=survey_id, name=name))
    return surveys


def build_match_rows(
    *,
    studies: Sequence[dict[str, str]],
    qualtrics_surveys: Sequence[MatchSurvey],
    qualtrics_base_url: str,
    prefix_tokens: int,
    existing_rows: Sequence[dict[str, str]] | None = None,
    proposed_at: str | None = None,
) -> list[dict[str, str]]:
    min_prefix_tokens = max(int(prefix_tokens or 1), 1)

    studies_normalized: list[MatchStudy] = []
    for row in studies:
        study_id = str(row.get("prolific_study_id") or "").strip()
        name = str(row.get("prolific_internal_name") or "").strip()
        study_name = str(row.get("prolific_study_name") or "").strip()
        completion_code = str(row.get("completion_code") or "").strip()
        if not study_id:
            continue
        studies_normalized.append(
            MatchStudy(
                study_id=study_id,
                internal_name=name,
                study_name=study_name,
                completion_code=completion_code,
            )
        )

    study_key_map: dict[str, list[MatchStudy]] = {}
    study_title_key_map: dict[str, list[MatchStudy]] = {}
    for study in studies_normalized:
        _index_all_prefix_keys(
            mapping=study_key_map,
            value=study.internal_name,
            item=study,
        )
        _index_all_prefix_keys(
            mapping=study_title_key_map,
            value=study.study_name,
            item=study,
        )

    survey_key_map: dict[str, list[MatchSurvey]] = {}
    survey_exact_map: dict[str, list[MatchSurvey]] = {}
    surveys_by_id: dict[str, MatchSurvey] = {}
    for survey in qualtrics_surveys:
        surveys_by_id[survey.survey_id] = survey
        _index_all_prefix_keys(
            mapping=survey_key_map,
            value=survey.name,
            item=survey,
        )
        exact_key = exact_name_key(survey.name, drop_trailing_p=False)
        if exact_key:
            survey_exact_map.setdefault(exact_key, []).append(survey)

    study_exact_map: dict[str, list[MatchStudy]] = {}
    study_title_exact_map: dict[str, list[MatchStudy]] = {}
    for study in studies_normalized:
        exact_key = exact_name_key(study.internal_name, drop_trailing_p=True)
        if exact_key:
            study_exact_map.setdefault(exact_key, []).append(study)
        title_exact_key = exact_name_key(study.study_name, drop_trailing_p=True)
        if title_exact_key:
            study_title_exact_map.setdefault(title_exact_key, []).append(study)

    existing_by_study: dict[str, dict[str, str]] = {}
    for row in existing_rows or []:
        study_id = str(row.get("prolific_study_id") or "").strip()
        if not study_id:
            continue
        existing_by_study[study_id] = dict(row)

    proposed_at_value = proposed_at or _now_iso()

    ordered = sorted(
        studies_normalized,
        key=lambda item: (item.internal_name.casefold(), item.study_id),
    )

    rows: list[dict[str, str]] = []
    for study in ordered:
        key, study_candidates, survey_candidates, key_is_unique = _resolve_prefix_candidates(
            value=study.internal_name,
            min_prefix_tokens=min_prefix_tokens,
            source_prefix_map=study_key_map,
            target_prefix_map=survey_key_map,
        )
        exact_key = exact_name_key(study.internal_name, drop_trailing_p=True)
        exact_survey_candidates = survey_exact_map.get(exact_key, []) if exact_key else []
        exact_study_candidates = study_exact_map.get(exact_key, []) if exact_key else []
        (
            title_key,
            title_study_candidates,
            title_survey_candidates,
            title_key_is_unique,
        ) = _resolve_prefix_candidates(
            value=study.study_name,
            min_prefix_tokens=min_prefix_tokens,
            source_prefix_map=study_title_key_map,
            target_prefix_map=survey_key_map,
        )
        title_exact_key = exact_name_key(study.study_name, drop_trailing_p=True)
        title_exact_survey_candidates = (
            survey_exact_map.get(title_exact_key, []) if title_exact_key else []
        )
        title_exact_study_candidates = (
            study_title_exact_map.get(title_exact_key, []) if title_exact_key else []
        )

        state = "REVIEW_REQUIRED"
        match_mode = "none"
        confidence = "low"
        survey_id = ""
        survey_name = ""
        notes = ""
        selected_prefix_key = key

        if (
            exact_key
            and len(exact_study_candidates) == 1
            and len(exact_survey_candidates) == 1
        ):
            candidate = exact_survey_candidates[0]
            survey_id = candidate.survey_id
            survey_name = candidate.name
            state = "PROPOSED"
            match_mode = "prefix_exact"
            confidence = "high"
        elif (
            title_exact_key
            and len(title_exact_study_candidates) == 1
            and len(title_exact_survey_candidates) == 1
        ):
            candidate = title_exact_survey_candidates[0]
            survey_id = candidate.survey_id
            survey_name = candidate.name
            state = "PROPOSED"
            match_mode = "prefix_exact"
            confidence = "high"
            selected_prefix_key = title_key or key
        elif key and key_is_unique:
            candidate = survey_candidates[0]
            survey_id = candidate.survey_id
            survey_name = candidate.name
            state = "PROPOSED"
            match_mode = (
                "prefix_exact"
                if normalize_name(study.internal_name) == normalize_name(candidate.name)
                else "prefix_unique"
            )
            confidence = "high" if match_mode == "prefix_exact" else "medium"
        elif title_key and title_key_is_unique:
            candidate = title_survey_candidates[0]
            survey_id = candidate.survey_id
            survey_name = candidate.name
            state = "PROPOSED"
            match_mode = "prefix_unique"
            confidence = "medium"
            selected_prefix_key = title_key
        elif key and survey_candidates:
            match_mode = "ambiguous"
            candidate_desc = ", ".join(
                [
                    f"{candidate.survey_id}:{candidate.name}"
                    for candidate in survey_candidates[:4]
                ]
            )
            notes = f"Ambiguous prefix '{key}' -> {candidate_desc}"
        elif title_key and title_survey_candidates:
            match_mode = "ambiguous"
            candidate_desc = ", ".join(
                [
                    f"{candidate.survey_id}:{candidate.name}"
                    for candidate in title_survey_candidates[:4]
                ]
            )
            notes = f"Ambiguous prefix '{title_key}' -> {candidate_desc}"
            selected_prefix_key = title_key
        else:
            match_mode = "none"
            notes = f"No unique prefix match for '{key or title_key or '[empty]'}'"
            if not selected_prefix_key:
                selected_prefix_key = title_key

        desired_prolific_redirect_url = build_qualtrics_form_redirect_url(
            qualtrics_base_url,
            survey_id,
        )
        desired_qualtrics_eos_redirect_url = build_prolific_completion_url(
            study.completion_code,
        )

        row = {
            "state": state,
            "prolific_study_id": study.study_id,
            "prolific_study_name": study.study_name,
            "prolific_internal_name": study.internal_name,
            "match_formula": "",
            "qualtrics_survey_name": survey_name,
            "qualtrics_survey_id": survey_id,
            "match_mode": match_mode,
            "match_confidence": confidence,
            "notes": notes,
            "completion_code": study.completion_code,
            "name_prefix_key": selected_prefix_key,
            "desired_prolific_redirect_url": desired_prolific_redirect_url,
            "desired_qualtrics_eos_redirect_url": desired_qualtrics_eos_redirect_url,
            "last_proposed_at": proposed_at_value,
        }

        existing = existing_by_study.get(study.study_id)
        if existing:
            existing_state = _normalize_state(existing.get("state"), default=row["state"])
            sticky_manual_state = existing_state in {"APPROVED", "SKIP"}
            if sticky_manual_state:
                row["state"] = existing_state
                manual_survey_id = str(existing.get("qualtrics_survey_id") or "").strip()
                manual_survey_name = str(existing.get("qualtrics_survey_name") or "").strip()
                if manual_survey_id:
                    row["qualtrics_survey_id"] = manual_survey_id
                    if not manual_survey_name and manual_survey_id in surveys_by_id:
                        manual_survey_name = surveys_by_id[manual_survey_id].name
                    row["qualtrics_survey_name"] = manual_survey_name
                    row["match_mode"] = "manual"
                    row["match_confidence"] = "high"
                    row["desired_prolific_redirect_url"] = (
                        str(existing.get("desired_prolific_redirect_url") or "").strip()
                        or build_qualtrics_form_redirect_url(
                            qualtrics_base_url,
                            manual_survey_id,
                        )
                    )
            existing_notes = str(existing.get("notes") or "").strip()
            if sticky_manual_state and existing_notes:
                row["notes"] = existing_notes

            existing_eos = str(existing.get("desired_qualtrics_eos_redirect_url") or "").strip()
            if existing_eos:
                row["desired_qualtrics_eos_redirect_url"] = existing_eos

            existing_title = str(existing.get("prolific_study_name") or "").strip()
            if existing_title:
                row["prolific_study_name"] = existing_title

            existing_completion_code = str(existing.get("completion_code") or "").strip()
            if existing_completion_code:
                row["completion_code"] = existing_completion_code

        if row["match_mode"] not in MATCH_MODES:
            row["match_mode"] = "manual"
        if row["match_confidence"] not in MATCH_CONFIDENCE:
            row["match_confidence"] = "low"
        row["match_formula"] = build_match_formula(
            row.get("prolific_internal_name"),
            row.get("qualtrics_survey_name"),
        )

        rows.append(row)

    return rows


def iter_rows_for_state(
    rows: Sequence[dict[str, str]],
    *,
    only_state: str,
) -> list[dict[str, str]]:
    target = str(only_state or "APPROVED").strip().upper()
    if target in {"", "ALL", "*"}:
        return [dict(row) for row in rows]
    return [
        dict(row)
        for row in rows
        if _normalize_state(str(row.get("state") or ""), default="") == target
    ]


def _build_wire_plan_rows(
    *,
    selected_rows: Sequence[dict[str, str]],
    base_url: str,
    headers: dict[str, str],
    prolific_token: str,
    auth_snippet: str | None,
) -> list[WirePlanRow]:
    plan_rows: list[WirePlanRow] = []
    embedded_check_by_survey: dict[str, tuple[str, list[str], list[str]]] = {}

    for row in selected_rows:
        study_id = str(row.get("prolific_study_id") or "").strip()
        survey_id = str(row.get("qualtrics_survey_id") or "").strip()

        if not study_id or not survey_id:
            plan_rows.append(
                WirePlanRow(
                    row=dict(row),
                    blocked_reason="missing prolific_study_id or qualtrics_survey_id",
                    prolific_current_redirect_url="",
                    prolific_desired_redirect_url=str(
                        row.get("desired_prolific_redirect_url") or ""
                    ).strip(),
                    qualtrics_current_eos_redirect_url="",
                    qualtrics_desired_eos_redirect_url=str(
                        row.get("desired_qualtrics_eos_redirect_url") or ""
                    ).strip(),
                    qualtrics_current_header="",
                    qualtrics_new_header="",
                    qualtrics_first_embedded_flow_id="",
                    qualtrics_first_embedded_fields=[],
                    qualtrics_missing_embedded_fields=list(REQUIRED_PROLIFIC_EMBEDDED_FIELDS),
                    options_payload=None,
                )
            )
            continue

        study = _fetch_prolific_study(token=prolific_token, study_id=study_id)
        options_payload = _fetch_qualtrics_options(
            base_url=base_url,
            headers=headers,
            survey_id=survey_id,
        )
        if survey_id in embedded_check_by_survey:
            embedded_flow_id, embedded_fields, missing_embedded_fields = embedded_check_by_survey[
                survey_id
            ]
        else:
            survey_result = _fetch_qualtrics_definition(
                base_url=base_url,
                headers=headers,
                survey_id=survey_id,
            )
            embedded_flow_id, embedded_fields = _first_embedded_data_block(survey_result)
            missing_embedded_fields = _missing_required_prolific_embedded_fields(embedded_fields)
            embedded_check_by_survey[survey_id] = (
                embedded_flow_id,
                embedded_fields,
                missing_embedded_fields,
            )

        prolific_current = str(study.get("external_study_url") or "").strip()
        prolific_desired = str(row.get("desired_prolific_redirect_url") or "").strip()
        if not prolific_desired:
            prolific_desired = build_qualtrics_form_redirect_url(base_url, survey_id)

        eos_current = str(options_payload.get("EOSRedirectURL") or "").strip()
        eos_desired = str(row.get("desired_qualtrics_eos_redirect_url") or "").strip()
        if not eos_desired:
            completion_code = str(row.get("completion_code") or "").strip()
            if not completion_code:
                completion_code = _extract_completion_code(study)
            eos_desired = build_prolific_completion_url(completion_code)

        header_current = str(options_payload.get("Header") or "")
        header_new = header_current
        blocked_reason: str | None = None

        if contains_prolific_qualtrics_script(header_current):
            header_new = header_current
        elif auth_snippet:
            mode = "append" if header_current.strip() else "replace"
            header_new = merge_header(header_current, auth_snippet, mode=mode)
        else:
            blocked_reason = (
                "header missing Prolific snippet and no snippet source was provided "
                "(--auth-snippet / --auth-snippet-file / PROLIFIC_AUTH_SNIPPET / --auth-token)"
            )

        if missing_embedded_fields:
            if embedded_flow_id:
                embedded_reason = (
                    f"first EmbeddedData block (FlowID={embedded_flow_id}) missing required fields: "
                    f"{', '.join(missing_embedded_fields)}"
                )
            else:
                embedded_reason = (
                    "SurveyFlow has no EmbeddedData block; required first-block fields missing: "
                    f"{', '.join(missing_embedded_fields)}"
                )
            blocked_reason = (
                f"{blocked_reason}; {embedded_reason}"
                if blocked_reason
                else embedded_reason
            )

        if not prolific_desired:
            blocked_reason = (
                f"{blocked_reason}; missing desired Prolific redirect URL"
                if blocked_reason
                else "missing desired Prolific redirect URL"
            )

        if not eos_desired:
            blocked_reason = (
                f"{blocked_reason}; missing desired Qualtrics EOS redirect URL"
                if blocked_reason
                else "missing desired Qualtrics EOS redirect URL"
            )

        plan_rows.append(
            WirePlanRow(
                row=dict(row),
                blocked_reason=blocked_reason,
                prolific_current_redirect_url=prolific_current,
                prolific_desired_redirect_url=prolific_desired,
                qualtrics_current_eos_redirect_url=eos_current,
                qualtrics_desired_eos_redirect_url=eos_desired,
                qualtrics_current_header=header_current,
                qualtrics_new_header=header_new,
                qualtrics_first_embedded_flow_id=embedded_flow_id,
                qualtrics_first_embedded_fields=list(embedded_fields),
                qualtrics_missing_embedded_fields=list(missing_embedded_fields),
                options_payload=options_payload,
            )
        )

    return plan_rows


def _print_plan_summary(
    *,
    mode: str,
    plan_rows: Sequence[WirePlanRow],
    only_state: str,
) -> None:
    print(
        f"[qsync:prolific] {mode}: rows={len(plan_rows)} "
        f"(state filter={str(only_state or 'APPROVED').upper()})"
    )

    for plan in plan_rows:
        row = plan.row
        study_id = row.get("prolific_study_id") or "?"
        survey_id = row.get("qualtrics_survey_id") or "?"
        status = "BLOCKED" if plan.blocked_reason else "READY"

        prolific_change = (
            plan.prolific_current_redirect_url != plan.prolific_desired_redirect_url
        )
        eos_change = (
            plan.qualtrics_current_eos_redirect_url
            != plan.qualtrics_desired_eos_redirect_url
        )
        header_change = plan.qualtrics_current_header != plan.qualtrics_new_header
        embedded_ok = not plan.qualtrics_missing_embedded_fields
        embedded_status = "ok" if embedded_ok else "missing"

        print(
            f"  - {study_id} -> {survey_id} [{status}] "
            f"prolific_redirect={'change' if prolific_change else 'ok'} "
            f"eos_redirect={'change' if eos_change else 'ok'} "
            f"header={'change' if header_change else 'ok'} "
            f"embedded_first_block={embedded_status}"
        )

        if plan.qualtrics_missing_embedded_fields:
            flow_id = plan.qualtrics_first_embedded_flow_id or "(none)"
            print(
                "    embedded-missing: "
                + ", ".join(plan.qualtrics_missing_embedded_fields)
                + f" | first_flow_id={flow_id}"
            )
        if plan.blocked_reason:
            print(f"    reason: {plan.blocked_reason}")


def _write_journal(
    *,
    account: str | None,
    payload: dict[str, Any],
    op_id: str,
) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target_dir = _journal_dir(account=account)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{timestamp}__{op_id}.json"
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return target


def _resolve_journal_path(*, account: str | None, op_id_or_path: str) -> Path:
    candidate = Path(op_id_or_path)
    if candidate.exists():
        return candidate.resolve()

    journal_root = _journal_dir(account=account)
    matches = sorted(journal_root.glob(f"*__{op_id_or_path}.json"))
    if not matches:
        raise SystemExit(
            f"[qsync:prolific] ERROR: journal not found for op-id '{op_id_or_path}' in {journal_root}"
        )
    return matches[-1].resolve()


def handle_pull_studies(args: argparse.Namespace) -> None:
    account = _resolve_account_from_args(args)
    prolific_token = _resolve_prolific_token(args, account=account)
    state = str(getattr(args, "state", "") or "").strip() or None
    studies = _list_prolific_studies(token=prolific_token, state=state)

    fetched_at = _now_iso()
    rows: list[dict[str, str]] = []
    for study in studies:
        study_id = str(study.get("id") or "").strip()
        if not study_id:
            continue
        rows.append(
            {
                "prolific_study_id": study_id,
                "prolific_internal_name": str(
                    study.get("internal_name") or study.get("name") or ""
                ).strip(),
                "prolific_study_name": str(study.get("name") or "").strip(),
                "prolific_status": str(study.get("state") or study.get("status") or "").strip(),
                "completion_code": _extract_completion_code(study),
                "redirect_url_current": str(study.get("external_study_url") or "").strip(),
                "fetched_at": fetched_at,
            }
        )

    rows.sort(key=lambda row: (row.get("prolific_internal_name", "").casefold(), row.get("prolific_study_id", "")))

    output_path = _resolve_studies_csv_path(args, account=account)
    _write_csv_rows(output_path, rows=rows, fieldnames=STUDIES_FIELDNAMES)

    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "ok": True,
                    "account": account,
                    "state": state,
                    "output": str(output_path),
                    "count": len(rows),
                },
                ensure_ascii=False,
            )
        )
        return

    print(f"[qsync:prolific] Pulled {len(rows)} studies -> {output_path}")


def handle_propose_matches(args: argparse.Namespace) -> None:
    account = _resolve_account_from_args(args)

    if bool(getattr(args, "pull_studies", False)):
        handle_pull_studies(args)

    if bool(getattr(args, "qualtrics_inventory_refresh", False)):
        base_url, headers = _get_client_config_for_account(account)
        refresh_inventory(base_url, headers, progress=False, quiet=True)

    studies_path = _resolve_studies_csv_path(args, account=account)
    matches_path = _resolve_matches_csv_path(args, account=account)

    studies_rows = _read_csv_rows(studies_path)
    if not studies_rows:
        raise SystemExit(
            f"[qsync:prolific] ERROR: no study rows found at {studies_path}. "
            "Run `qsync prolific pull-studies` first."
        )

    surveys = _load_qualtrics_surveys(account=account)
    base_url, _headers = _get_client_config_for_account(account)

    existing_rows = _read_csv_rows(matches_path)
    prefix_tokens = max(int(getattr(args, "prefix_tokens", 2) or 2), 1)

    proposed_rows = build_match_rows(
        studies=studies_rows,
        qualtrics_surveys=surveys,
        qualtrics_base_url=base_url,
        prefix_tokens=prefix_tokens,
        existing_rows=existing_rows,
    )
    _write_csv_rows(matches_path, rows=proposed_rows, fieldnames=MATCHES_FIELDNAMES)

    summary = {
        "total": len(proposed_rows),
        "proposed": 0,
        "approved": 0,
        "review_required": 0,
        "skip": 0,
    }
    for row in proposed_rows:
        state = _normalize_state(row.get("state"), default="PROPOSED")
        if state == "PROPOSED":
            summary["proposed"] += 1
        elif state == "APPROVED":
            summary["approved"] += 1
        elif state == "SKIP":
            summary["skip"] += 1
        elif state == "REVIEW_REQUIRED":
            summary["review_required"] += 1

    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "ok": True,
                    "account": account,
                    "studies_csv": str(studies_path),
                    "matches_csv": str(matches_path),
                    "prefix_tokens": prefix_tokens,
                    "min_prefix_tokens": prefix_tokens,
                    "summary": summary,
                },
                ensure_ascii=False,
            )
        )
        return

    print(f"[qsync:prolific] Wrote matches -> {matches_path}")
    print(
        "[qsync:prolific] "
        f"rows={summary['total']} proposed={summary['proposed']} "
        f"approved={summary['approved']} review_required={summary['review_required']} skip={summary['skip']}"
    )


def handle_wire_preview(args: argparse.Namespace) -> None:
    account = _resolve_account_from_args(args)
    prolific_token = _resolve_prolific_token(args, account=account)
    matches_path = _resolve_matches_csv_path(args, account=account)
    rows = _read_csv_rows(matches_path)
    only_state = str(getattr(args, "only_state", "APPROVED") or "APPROVED")
    selected = iter_rows_for_state(rows, only_state=only_state)

    if not selected:
        if getattr(args, "json", False):
            print(
                json.dumps(
                    {
                        "ok": True,
                        "account": account,
                        "matches_csv": str(matches_path),
                        "only_state": only_state.upper(),
                        "rows": [],
                        "summary": {"rows": 0},
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(
                f"[qsync:prolific] Preview: no rows in state {only_state.upper()} at {matches_path}"
            )
        return

    base_url, headers = _get_client_config_for_account(account)
    auth_snippet = _resolve_auth_snippet(args, account=account)
    plan_rows = _build_wire_plan_rows(
        selected_rows=selected,
        base_url=base_url,
        headers=headers,
        prolific_token=prolific_token,
        auth_snippet=auth_snippet,
    )

    if getattr(args, "json", False):
        payload = {
            "ok": True,
            "account": account,
            "matches_csv": str(matches_path),
            "only_state": only_state.upper(),
            "rows": [
                {
                    "prolific_study_id": p.row.get("prolific_study_id"),
                    "qualtrics_survey_id": p.row.get("qualtrics_survey_id"),
                    "blocked_reason": p.blocked_reason,
                    "prolific_current_redirect_url": p.prolific_current_redirect_url,
                    "prolific_desired_redirect_url": p.prolific_desired_redirect_url,
                    "qualtrics_current_eos_redirect_url": p.qualtrics_current_eos_redirect_url,
                    "qualtrics_desired_eos_redirect_url": p.qualtrics_desired_eos_redirect_url,
                    "header_change": p.qualtrics_current_header != p.qualtrics_new_header,
                    "first_embedded_flow_id": p.qualtrics_first_embedded_flow_id,
                    "first_embedded_fields": p.qualtrics_first_embedded_fields,
                    "missing_embedded_fields": p.qualtrics_missing_embedded_fields,
                }
                for p in plan_rows
            ],
        }
        print(json.dumps(payload, ensure_ascii=False))
        return

    _print_plan_summary(mode="preview", plan_rows=plan_rows, only_state=only_state)


def handle_wire_apply(args: argparse.Namespace) -> None:
    account = _resolve_account_from_args(args)
    prolific_token = _resolve_prolific_token(args, account=account)
    matches_path = _resolve_matches_csv_path(args, account=account)
    only_state = str(getattr(args, "only_state", "APPROVED") or "APPROVED")
    rows = _read_csv_rows(matches_path)
    selected = iter_rows_for_state(rows, only_state=only_state)

    if not selected:
        raise SystemExit(
            f"[qsync:prolific] ERROR: no rows in state {only_state.upper()} at {matches_path}"
        )

    base_url, headers = _get_client_config_for_account(account)
    auth_snippet = _resolve_auth_snippet(args, account=account)
    plan_rows = _build_wire_plan_rows(
        selected_rows=selected,
        base_url=base_url,
        headers=headers,
        prolific_token=prolific_token,
        auth_snippet=auth_snippet,
    )

    _print_plan_summary(mode="apply", plan_rows=plan_rows, only_state=only_state)

    if not getattr(args, "yes", False):
        if not sys.stdin.isatty():
            raise SystemExit(
                "[qsync:prolific] ERROR: Confirmation required but stdin is not interactive. "
                "Re-run with --yes to proceed."
            )
        typed = input("Type 'apply' to continue: ").strip()
        if typed != "apply":
            raise SystemExit("[qsync:prolific] Aborted.")

    publish_arg = getattr(args, "publish", None)
    activate_arg = getattr(args, "activate", None)
    publish = True if publish_arg is None else bool(publish_arg)
    activate = True if activate_arg is None else bool(activate_arg)
    continue_on_error = bool(getattr(args, "continue_on_error", False))
    publish_description = (
        str(getattr(args, "publish_description", "") or "").strip()
        or "Prolific wiring update"
    )

    op_id = uuid.uuid4().hex[:10]
    journal_rows: list[dict[str, Any]] = []
    success_count = 0
    error_count = 0
    skipped_count = 0
    show_progress = (
        not bool(getattr(args, "json", False))
        and len(plan_rows) > 1
        and should_use_rich()
    )
    apply_progress_cm = (
        progress_context("Applying Prolific wiring links", total=len(plan_rows))
        if show_progress
        else nullcontext(None)
    )

    with apply_progress_cm as progress_state:
        for index, plan in enumerate(plan_rows, start=1):
            row = dict(plan.row)
            study_id = str(row.get("prolific_study_id") or "").strip()
            survey_id = str(row.get("qualtrics_survey_id") or "").strip()
            stop_after_iteration = False

            if progress_state is not None:
                progress, task_id = progress_state
                progress.update(
                    task_id,
                    description=(
                        f"Linking studies {index}/{len(plan_rows)} "
                        f"{study_id or '?'} -> {survey_id or '?'}"
                    ),
                )

            before = {
                "prolific_redirect_url": plan.prolific_current_redirect_url,
                "qualtrics_eos_redirect_url": plan.qualtrics_current_eos_redirect_url,
                "qualtrics_header": plan.qualtrics_current_header,
            }

            record: dict[str, Any] = {
                "prolific_study_id": study_id,
                "qualtrics_survey_id": survey_id,
                "state": row.get("state"),
                "before": before,
                "desired": {
                    "prolific_redirect_url": plan.prolific_desired_redirect_url,
                    "qualtrics_eos_redirect_url": plan.qualtrics_desired_eos_redirect_url,
                    "qualtrics_header": plan.qualtrics_new_header,
                },
                "steps": {},
            }

            if plan.blocked_reason:
                record["status"] = "skipped"
                record["reason"] = plan.blocked_reason
                journal_rows.append(record)
                skipped_count += 1
                if progress_state is not None:
                    progress, task_id = progress_state
                    progress.advance(task_id)
                continue

            try:
                prolific_changed = (
                    plan.prolific_current_redirect_url != plan.prolific_desired_redirect_url
                )
                if prolific_changed:
                    _write_prolific_study_redirect(
                        token=prolific_token,
                        study_id=study_id,
                        redirect_url=plan.prolific_desired_redirect_url,
                    )
                    record["steps"]["prolific_redirect"] = "updated"
                else:
                    record["steps"]["prolific_redirect"] = "no-op"

                options_payload = dict(plan.options_payload or {})
                options_changed = False
                if (
                    plan.qualtrics_current_eos_redirect_url
                    != plan.qualtrics_desired_eos_redirect_url
                ):
                    options_payload["EOSRedirectURL"] = plan.qualtrics_desired_eos_redirect_url
                    options_changed = True

                if plan.qualtrics_current_header != plan.qualtrics_new_header:
                    options_payload["Header"] = plan.qualtrics_new_header
                    options_changed = True

                if options_changed:
                    _write_qualtrics_options(
                        base_url=base_url,
                        headers=headers,
                        survey_id=survey_id,
                        options_payload=options_payload,
                    )
                    record["steps"]["qualtrics_options"] = "updated"
                else:
                    record["steps"]["qualtrics_options"] = "no-op"

                if publish:
                    publish_survey_definition(
                        survey_id,
                        description=publish_description,
                        base_url=base_url,
                        headers=headers,
                    )
                    record["steps"]["publish"] = "updated"

                if activate:
                    _activate_survey(base_url=base_url, headers=headers, survey_id=survey_id)
                    record["steps"]["activate"] = "updated"

                record["status"] = "success"
                success_count += 1
            except Exception as exc:  # noqa: BLE001
                record["status"] = "error"
                record["error"] = str(exc)
                error_count += 1
                journal_rows.append(record)
                if not continue_on_error:
                    stop_after_iteration = True
            else:
                journal_rows.append(record)

            if progress_state is not None:
                progress, task_id = progress_state
                progress.advance(task_id)
            if stop_after_iteration:
                break

    journal_payload = {
        "op_id": op_id,
        "created_at": _now_iso(),
        "account": account,
        "matches_csv": str(matches_path),
        "only_state": only_state.upper(),
        "publish": publish,
        "activate": activate,
        "summary": {
            "rows": len(journal_rows),
            "success": success_count,
            "errors": error_count,
            "skipped": skipped_count,
        },
        "rows": journal_rows,
    }
    journal_path = _write_journal(account=account, payload=journal_payload, op_id=op_id)

    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "ok": error_count == 0,
                    "op_id": op_id,
                    "journal": str(journal_path),
                    "summary": journal_payload["summary"],
                },
                ensure_ascii=False,
            )
        )
    else:
        print(
            f"[qsync:prolific] Apply finished: success={success_count} "
            f"errors={error_count} skipped={skipped_count}"
        )
        print(f"[qsync:prolific] Journal: {journal_path}")

    if error_count > 0:
        raise SystemExit(1)


def handle_wire_rollback(args: argparse.Namespace) -> None:
    account = _resolve_account_from_args(args)
    prolific_token = _resolve_prolific_token(args, account=account)
    op_id = str(getattr(args, "op_id", "") or "").strip()
    if not op_id:
        raise SystemExit("[qsync:prolific] ERROR: --op-id is required.")

    journal_path = _resolve_journal_path(account=account, op_id_or_path=op_id)
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    rows = payload.get("rows") or []
    if not isinstance(rows, list):
        raise SystemExit(f"[qsync:prolific] ERROR: invalid journal payload in {journal_path}")

    if not getattr(args, "yes", False):
        if not sys.stdin.isatty():
            raise SystemExit(
                "[qsync:prolific] ERROR: Confirmation required but stdin is not interactive. "
                "Re-run with --yes to proceed."
            )
        typed = input("Type 'rollback' to continue: ").strip()
        if typed != "rollback":
            raise SystemExit("[qsync:prolific] Aborted.")

    base_url, headers = _get_client_config_for_account(account)
    publish = bool(getattr(args, "publish", False))
    activate = bool(getattr(args, "activate", False))
    publish_description = (
        str(getattr(args, "publish_description", "") or "").strip()
        or "Prolific wiring rollback"
    )

    rollback_rows: list[dict[str, Any]] = []
    success_count = 0
    error_count = 0
    show_progress = (
        not bool(getattr(args, "json", False))
        and len(rows) > 1
        and should_use_rich()
    )
    rollback_progress_cm = (
        progress_context("Rolling back Prolific wiring links", total=len(rows))
        if show_progress
        else nullcontext(None)
    )

    with rollback_progress_cm as progress_state:
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "").strip().lower()
            if status not in {"success", "error", "skipped"}:
                continue

            study_id = str(row.get("prolific_study_id") or "").strip()
            survey_id = str(row.get("qualtrics_survey_id") or "").strip()
            before = row.get("before") or {}

            if progress_state is not None:
                progress, task_id = progress_state
                progress.update(
                    task_id,
                    description=(
                        f"Rollback links {index}/{len(rows)} "
                        f"{study_id or '?'} -> {survey_id or '?'}"
                    ),
                )

            record: dict[str, Any] = {
                "prolific_study_id": study_id,
                "qualtrics_survey_id": survey_id,
                "steps": {},
            }

            try:
                if study_id:
                    _write_prolific_study_redirect(
                        token=prolific_token,
                        study_id=study_id,
                        redirect_url=str(before.get("prolific_redirect_url") or "").strip(),
                    )
                    record["steps"]["prolific_redirect"] = "restored"

                if survey_id:
                    options_payload = _fetch_qualtrics_options(
                        base_url=base_url,
                        headers=headers,
                        survey_id=survey_id,
                    )
                    options_payload["EOSRedirectURL"] = str(
                        before.get("qualtrics_eos_redirect_url") or ""
                    ).strip()
                    options_payload["Header"] = str(before.get("qualtrics_header") or "")
                    _write_qualtrics_options(
                        base_url=base_url,
                        headers=headers,
                        survey_id=survey_id,
                        options_payload=options_payload,
                    )
                    record["steps"]["qualtrics_options"] = "restored"

                    if publish:
                        publish_survey_definition(
                            survey_id,
                            description=publish_description,
                            base_url=base_url,
                            headers=headers,
                        )
                        record["steps"]["publish"] = "updated"
                    if activate:
                        _activate_survey(base_url=base_url, headers=headers, survey_id=survey_id)
                        record["steps"]["activate"] = "updated"

                record["status"] = "success"
                success_count += 1
            except Exception as exc:  # noqa: BLE001
                record["status"] = "error"
                record["error"] = str(exc)
                error_count += 1

            rollback_rows.append(record)
            if progress_state is not None:
                progress, task_id = progress_state
                progress.advance(task_id)

    rollback_payload = {
        "source_journal": str(journal_path),
        "source_op_id": payload.get("op_id"),
        "created_at": _now_iso(),
        "account": account,
        "publish": publish,
        "activate": activate,
        "summary": {
            "rows": len(rollback_rows),
            "success": success_count,
            "errors": error_count,
        },
        "rows": rollback_rows,
    }
    rollback_op_id = f"rollback-{uuid.uuid4().hex[:10]}"
    rollback_journal_path = _write_journal(
        account=account,
        payload=rollback_payload,
        op_id=rollback_op_id,
    )

    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "ok": error_count == 0,
                    "journal": str(rollback_journal_path),
                    "summary": rollback_payload["summary"],
                },
                ensure_ascii=False,
            )
        )
    else:
        print(
            f"[qsync:prolific] Rollback finished: success={success_count} errors={error_count}"
        )
        print(f"[qsync:prolific] Journal: {rollback_journal_path}")

    if error_count > 0:
        raise SystemExit(1)


def _add_prolific_token_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--prolific-token",
        help="Prolific API token (preferred: set PROLIFIC_API_TOKEN in .env).",
    )


def _add_wire_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--matches",
        help="Path to matches CSV (default: surveys/.<account>/prolific/matches.csv)",
    )
    parser.add_argument(
        "--only-state",
        default="APPROVED",
        help="Process only rows in this state (default: APPROVED; use ALL for all rows).",
    )
    parser.add_argument(
        "--auth-snippet",
        help="Full Prolific authenticity snippet HTML (preferred for deterministic wiring).",
    )
    parser.add_argument(
        "--auth-snippet-file",
        help="Path to a file containing the Prolific authenticity snippet HTML.",
    )
    parser.add_argument(
        "--auth-token",
        help="Public Prolific snippet token used to generate the auth script tag (not the API token).",
    )
    _add_prolific_token_arg(parser)


def register_prolific_commands(
    subparsers: Any,
    *,
    command_name: str = "prolific",
    help_text: str = "Automate Prolific ↔ Qualtrics wiring workflows (group)",
) -> None:
    parser = subparsers.add_parser(
        command_name,
        help=help_text,
    )
    parser.add_argument(
        "--account",
        help="Use credentials/cache from `.env.<account>` under the workspace root.",
    )
    prolific_subs = parser.add_subparsers(
        dest="prolific_command",
        required=True,
        metavar="COMMAND",
    )

    pull_studies = prolific_subs.add_parser(
        "pull-studies",
        help="Pull Prolific studies into account-scoped CSV cache",
    )
    pull_studies.add_argument(
        "--state",
        help="Optional Prolific state filter for study pull (e.g. ACTIVE, DRAFT).",
    )
    pull_studies.add_argument(
        "--studies",
        help="Output CSV path (default: surveys/.<account>/prolific/studies.csv)",
    )
    _add_prolific_token_arg(pull_studies)
    pull_studies.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    pull_studies.set_defaults(func=handle_pull_studies)

    propose = prolific_subs.add_parser(
        "propose-matches",
        help="Generate/refresh match proposals from Prolific studies + Qualtrics inventory",
    )
    propose.add_argument(
        "--studies",
        help="Input studies CSV path (default: surveys/.<account>/prolific/studies.csv)",
    )
    propose.add_argument(
        "--matches",
        help="Output matches CSV path (default: surveys/.<account>/prolific/matches.csv)",
    )
    propose.add_argument(
        "--prefix-tokens",
        type=int,
        default=2,
        help=(
            "Minimum number of normalized leading tokens required before unique-prefix matching "
            "is attempted (default: 2). Matcher expands beyond this minimum until unique, when possible."
        ),
    )
    propose.add_argument(
        "--pull-studies",
        action="store_true",
        help="Refresh Prolific studies before proposing matches.",
    )
    propose.add_argument(
        "--qualtrics-inventory-refresh",
        action="store_true",
        help="Refresh Qualtrics inventory before proposing matches.",
    )
    _add_prolific_token_arg(propose)
    propose.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    propose.set_defaults(func=handle_propose_matches)

    wire = prolific_subs.add_parser(
        "wire",
        help="Preview/apply/rollback cross-platform wiring changes",
    )
    wire_subs = wire.add_subparsers(dest="prolific_wire_command", required=True)

    wire_preview = wire_subs.add_parser(
        "preview",
        help="Preview planned wiring changes for selected rows",
    )
    _add_wire_common_args(wire_preview)
    wire_preview.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    wire_preview.set_defaults(func=handle_wire_preview)

    wire_apply = wire_subs.add_parser(
        "apply",
        help="Apply wiring changes for selected rows (default: APPROVED only)",
    )
    _add_wire_common_args(wire_apply)
    wire_apply_publish_group = wire_apply.add_mutually_exclusive_group()
    wire_apply_publish_group.add_argument(
        "--publish",
        action="store_true",
        dest="publish",
        default=None,
        help="Publish Qualtrics survey definitions after wiring (default behavior).",
    )
    wire_apply_publish_group.add_argument(
        "--no-publish",
        action="store_false",
        dest="publish",
        help="Skip publish after wiring (not recommended).",
    )
    wire_apply_activate_group = wire_apply.add_mutually_exclusive_group()
    wire_apply_activate_group.add_argument(
        "--activate",
        action="store_true",
        dest="activate",
        default=None,
        help="Activate Qualtrics surveys after wiring (default behavior).",
    )
    wire_apply_activate_group.add_argument(
        "--no-activate",
        action="store_false",
        dest="activate",
        help="Skip activate after wiring (not recommended).",
    )
    wire_apply.add_argument(
        "--publish-description",
        default="Prolific wiring update",
        help="Publish description when publish step is enabled.",
    )
    wire_apply.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue applying remaining rows after a per-row error.",
    )
    wire_apply.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    wire_apply.set_defaults(func=handle_wire_apply)

    wire_rollback = wire_subs.add_parser(
        "rollback",
        help="Rollback a previous apply run by operation ID",
    )
    wire_rollback.add_argument(
        "--op-id",
        required=True,
        help="Operation ID from an apply journal (or a direct journal file path).",
    )
    _add_prolific_token_arg(wire_rollback)
    wire_rollback.add_argument(
        "--publish",
        action="store_true",
        help="Publish Qualtrics survey definitions after restoring options.",
    )
    wire_rollback.add_argument(
        "--activate",
        action="store_true",
        help="Activate Qualtrics surveys after rollback updates.",
    )
    wire_rollback.add_argument(
        "--publish-description",
        default="Prolific wiring rollback",
        help="Publish description when --publish is enabled.",
    )
    wire_rollback.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    wire_rollback.set_defaults(func=handle_wire_rollback)

    reorder_subparser_choices(
        prolific_subs,
        [
            "pull-studies",
            "propose-matches",
            "wire",
        ],
    )
    reorder_subparser_choices(
        wire_subs,
        [
            "preview",
            "apply",
            "rollback",
        ],
    )
