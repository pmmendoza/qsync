"""
Survey inventory management for qsync.

Fetches survey metadata from Qualtrics API and maintains a local CSV cache
for use by push safeguards and workflow tools.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import requests

from .config import resolve_root
from .api_push import send_api_request

ROOT = resolve_root(required=False) or Path.cwd()
SURVEYS_DIR = ROOT / "surveys"
INVENTORY_CSV = SURVEYS_DIR / "inventory.csv"
LEGACY_SURVEY_CACHE = SURVEYS_DIR / "qualtrics_surveys.csv"
# Backward-compat alias: many modules still refer to SURVEY_CACHE.
# Canonical path for writes is now surveys/inventory.csv.
SURVEY_CACHE = INVENTORY_CSV
FOCAL_SNAPSHOT = SURVEYS_DIR / ".focal_snapshot.json"
EXCEL_DIR = ROOT / "excel"
EXCEL_ARCHIVE = EXCEL_DIR / "archive"
SURVEY_ARCHIVE_DIR = SURVEYS_DIR / "archive"

TRUE_TOKENS = {"true", "1", "yes", "y", "t"}

INVENTORY_FIELDNAMES = [
    "id",
    "name",
    "focal",
    "locked",
    "isActive",
    "component",
    "stage",
    "cntry",
    "preview_count",
    "response_count",
    "lastModified",
    "creationDate",
    "editableViaApi",
    "generated_at",
    "ownerId",
]


def resolve_inventory_csv_path(*, required: bool = False) -> Path:
    """Return the inventory CSV path, preferring the canonical filename.

    Canonical: surveys/inventory.csv
    Legacy (read-only compatibility): surveys/qualtrics_surveys.csv
    """

    if INVENTORY_CSV.exists():
        return INVENTORY_CSV
    if LEGACY_SURVEY_CACHE.exists():
        return LEGACY_SURVEY_CACHE
    if required:
        raise FileNotFoundError(
            "Missing survey inventory file. Expected surveys/inventory.csv "
            "(or legacy surveys/qualtrics_surveys.csv). Run `qsync survey inventory` first."
        )
    return INVENTORY_CSV


def _as_bool(value: Any) -> bool:
    """Convert various truthy representations to bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if value is None:
        return False
    return str(value).strip().lower() in TRUE_TOKENS


def _parse_int(value: Any) -> int:
    """Parse various representations to int."""
    if isinstance(value, (int, float)):
        return int(value)
    if value is None:
        return 0
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _extract_embedded_field_value(
    payload: Dict[str, Any], field_name: str
) -> str | None:
    """Return the first embedded data value for the requested field."""
    if not payload:
        return None
    result = payload.get("result", {}) if isinstance(payload, dict) else {}
    flow = None
    if isinstance(result, dict) and "SurveyFlow" in result:
        flow = result.get("SurveyFlow")
    elif isinstance(result, dict) and "Flow" in result:
        flow = result
    elif isinstance(payload, dict) and "Flow" in payload:
        flow = payload
    if not isinstance(flow, dict):
        return None
    flow_list = flow.get("Flow", [])

    def walk(flow_list: List[Any]) -> str | None:
        for node in flow_list:
            if not isinstance(node, dict):
                continue
            if node.get("Type") == "EmbeddedData":
                for entry in node.get("EmbeddedData", []) or []:
                    if not isinstance(entry, dict):
                        continue
                    if str(entry.get("Field") or "").strip() == field_name:
                        value = entry.get("Value")
                        if value is None:
                            return None
                        return str(value).strip()
            subflow = node.get("Flow")
            if isinstance(subflow, list):
                found = walk(subflow)
                if found is not None:
                    return found
        return None

    return walk(flow_list if isinstance(flow_list, list) else [])


def _read_csv_rows() -> Iterable[dict]:
    """Read existing CSV inventory, filtering out comment lines."""
    path = resolve_inventory_csv_path(required=False)
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        filtered = (line for line in fh if not line.lstrip().startswith("#"))
        reader = csv.DictReader(filtered)
        return list(reader)


def fetch_surveys(base_url: str, headers: Dict[str, str]) -> List[dict]:
    """Retrieve all surveys, following pagination until exhausted."""
    url = f"https://{base_url}/API/v3/surveys"
    surveys: List[dict] = []
    first_page = True

    while url:
        params = {"pageSize": 100} if first_page else None
        response = send_api_request(
            action="qsync.inventory.fetch.surveys",
            method="GET",
            base_url=base_url,
            headers=headers,
            path=url,
            log_event=False,
            params=params,
            timeout=30,
        )
        payload = response.json()
        result = payload.get("result", {})
        surveys.extend(result.get("elements", []))
        url = result.get("nextPage")
        first_page = False

    return surveys


def fetch_survey_payload(
    base_url: str, headers: Dict[str, str], survey_id: str
) -> Dict[str, Any]:
    """Fetch detailed survey info for a single survey."""
    response = send_api_request(
        action="qsync.inventory.fetch.survey.payload",
        method="GET",
        base_url=base_url,
        headers=headers,
        path=f"surveys/{survey_id}",
        log_event=False,
        timeout=60,
    )
    return response.json().get("result", {})


def fetch_survey_flow_payload(
    base_url: str, headers: Dict[str, str], survey_id: str
) -> Dict[str, Any]:
    """Fetch SurveyFlow payload for a single survey."""
    response = send_api_request(
        action="qsync.inventory.fetch.survey.flow",
        method="GET",
        base_url=base_url,
        headers=headers,
        path=f"survey-definitions/{survey_id}/flow",
        log_event=False,
        timeout=30,
    )
    return response.json()


def fetch_survey_detail(
    base_url: str,
    headers: Dict[str, str],
    survey_id: str,
    *,
    payload: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Extract detail fields from survey payload."""
    result = (
        payload
        if payload is not None
        else fetch_survey_payload(base_url, headers, survey_id)
    )
    detail = {
        "ownerId": result.get("ownerId"),
        "organizationId": result.get("organizationId"),
        "isActive": result.get("isActive"),
        "creationDate": result.get("creationDate"),
        "lastModifiedDate": result.get("lastModifiedDate"),
    }
    permission_summary = result.get("permissionSummary") or result.get("permissions")
    if permission_summary is not None:
        detail["permissionSummary"] = permission_summary
    return detail


def build_summary_from_payload(
    payload: Dict[str, Any], survey_id: str
) -> Dict[str, Any]:
    """Build a summary record from a full payload (for targeted refresh)."""
    return {
        "id": payload.get("id") or survey_id,
        "name": payload.get("name"),
        "ownerId": payload.get("ownerId"),
        "creationDate": payload.get("creationDate"),
        "lastModified": payload.get("lastModified") or payload.get("lastModifiedDate"),
        "isActive": payload.get("isActive"),
    }


def fetch_current_user(base_url: str, headers: Dict[str, str]) -> Dict[str, Any]:
    """Fetch current authenticated user info."""
    response = send_api_request(
        action="qsync.inventory.whoami",
        method="GET",
        base_url=base_url,
        headers=headers,
        path="whoami",
        log_event=False,
        timeout=30,
    )
    return response.json().get("result", {})


def load_existing_metadata() -> Dict[str, dict]:
    """Load preserved metadata (focal, locked, component, etc.) from existing CSV."""
    rows = _read_csv_rows()
    meta: Dict[str, dict] = {}
    for entry in rows:
        sid = (entry.get("id") or "").strip()
        if not sid:
            continue
        target = meta.setdefault(sid, {})
        locked_val = entry.get("locked")
        if locked_val is not None:
            target["locked"] = _as_bool(locked_val)
        focal_val = entry.get("focal")
        if focal_val is not None:
            target["focal"] = _as_bool(focal_val)
        for key in ("component", "stage", "cntry"):
            value = entry.get(key)
            if value not in (None, ""):
                target[key] = value
    return meta


def load_cached_inventory_records() -> Dict[str, dict]:
    """Load full inventory records from existing CSV."""
    records: Dict[str, dict] = {}
    for entry in _read_csv_rows():
        sid = (entry.get("id") or "").strip()
        if not sid:
            continue
        records[sid] = {
            "id": sid,
            "name": entry.get("name"),
            "focal": _as_bool(entry.get("focal")),
            "locked": _as_bool(entry.get("locked")),
            "isActive": _as_bool(entry.get("isActive")),
            "preview_count": _parse_int(entry.get("preview_count")),
            "response_count": _parse_int(entry.get("response_count")),
            "lastModified": entry.get("lastModified"),
            "component": entry.get("component"),
            "stage": entry.get("stage"),
            "cntry": entry.get("cntry"),
            "creationDate": entry.get("creationDate"),
            "editableViaApi": _as_bool(entry.get("editableViaApi")),
            "generated_at": entry.get("generated_at"),
            "ownerId": entry.get("ownerId"),
        }
    return records


def load_inventory_record(survey_id: str) -> dict | None:
    """Load a single inventory record by survey ID.

    Args:
        survey_id: Survey ID to load

    Returns:
        Inventory record dict or None if not found
    """
    records = load_cached_inventory_records()
    return records.get(survey_id)


def get_focal_survey_ids() -> List[str]:
    """Get list of survey IDs marked as focal in inventory.

    Returns:
        List of focal survey IDs
    """
    focal_ids: List[str] = []
    for entry in _read_csv_rows():
        sid = (entry.get("id") or "").strip()
        if not sid:
            continue
        if _as_bool(entry.get("focal")):
            focal_ids.append(sid)
    return focal_ids


def _diff_payload(record: dict | None) -> Dict[str, Any]:
    """Extract diffable fields from a record."""
    if not record:
        return {}
    payload: Dict[str, Any] = {}
    for field in INVENTORY_FIELDNAMES:
        if field == "generated_at":
            continue
        payload[field] = record.get(field)
    return payload


def determine_changed_records(
    inventory: Iterable[dict],
    previous_records: Dict[str, dict],
) -> List[dict]:
    """Compare inventory against previous records to find changed rows."""
    changed: List[dict] = []
    for record in inventory:
        sid = record.get("id")
        if not sid:
            continue
        prev_payload = _diff_payload(previous_records.get(sid))
        curr_payload = _diff_payload(record)
        if not prev_payload or prev_payload != curr_payload:
            changed.append(record)
    return changed


def build_inventory_record(
    summary: dict,
    detail: dict,
    *,
    current_user_id: str | None,
    locked: bool = False,
) -> dict:
    """Build an inventory record from summary and detail data."""
    name = summary.get("name")
    owner_summary = summary.get("ownerId")
    editable = bool(current_user_id and owner_summary == current_user_id)

    record = {
        "id": summary.get("id"),
        "name": name,
        "ownerId": owner_summary,
        "creationDate": summary.get("creationDate") or detail.get("creationDate"),
        "lastModified": summary.get("lastModified") or detail.get("lastModifiedDate"),
        "isActive": _as_bool(summary.get("isActive")),
        "editableViaApi": editable,
        "locked": bool(locked),
        "component": summary.get("component") or "pre",
        "stage": summary.get("stage") or "main",
        "focal": bool(summary.get("focal", False)),
        "cntry": summary.get("cntry") or "US",
        "preview_count": None,
        "response_count": None,
    }

    if detail.get("permissionSummary") is not None:
        record["permissionSummary"] = detail["permissionSummary"]
    return record


def compose_inventory_record(
    summary: dict,
    detail: dict,
    *,
    current_user_id: str | None,
    existing_locks: Dict[str, dict],
    payload: Dict[str, Any] | None = None,
    flow_payload: Dict[str, Any] | None = None,
    include_counts: bool = True,
) -> dict:
    """Compose a full inventory record, preserving existing metadata."""
    survey_id = summary.get("id")
    record = build_inventory_record(
        summary,
        detail,
        current_user_id=current_user_id,
        locked=existing_locks.get(survey_id, {}).get("locked", False),
    )
    saved = existing_locks.get(survey_id, {})
    for key in ("component", "stage", "focal", "cntry"):
        if key in saved:
            record[key] = saved[key]

    if flow_payload is not None:
        embedded_cntry = _extract_embedded_field_value(flow_payload, "country")
        if embedded_cntry:
            record["cntry"] = embedded_cntry
        else:
            # Migration fallback: older surveys may still use surveylang as the ISO-2 routing field.
            legacy_cntry = _extract_embedded_field_value(flow_payload, "surveylang")
            if legacy_cntry:
                record["cntry"] = legacy_cntry
                print(
                    f"[inventory] NOTE: Survey {survey_id} uses legacy surveylang for cntry; please migrate to country."
                )
            else:
                print(
                    f"[inventory] WARNING: Survey {survey_id} missing country/surveylang embedded field; leaving cntry unchanged. "
                    "Next: add an EmbeddedData field named 'country' (preferred) and re-run inventory."
                )

    if payload is not None and include_counts:
        counts = payload.get("responseCounts") or {}
        record["preview_count"] = _parse_int(counts.get("generated"))
        record["response_count"] = _parse_int(counts.get("auditable"))
    return record


def persist_surveys(surveys: Iterable[dict], *, current_user_id: str | None) -> Path:
    """Persist survey metadata to the CSV cache file."""
    SURVEYS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    surveys_list = list(surveys)

    def normalize_bool(value: bool | str | None) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in TRUE_TOKENS
        return bool(value)

    def bool_str(value: bool | str | None) -> str:
        return "TRUE" if normalize_bool(value) else "FALSE"

    def optional_int(value: Any) -> str:
        if value is None:
            return ""
        return str(_parse_int(value))

    fieldnames = list(INVENTORY_FIELDNAMES)

    stage_order = {"main": 0}
    component_order = {"pre": 0}
    cntry_order = {"IE": 0, "NL": 1, "CZ": 2, "FR": 3, "UK": 4, "US": 5}

    def sort_key(record: dict) -> tuple:
        focal = bool(record.get("focal"))
        stage = str(record.get("stage") or "").strip()
        component = str(record.get("component") or "").strip()
        cntry = str(record.get("cntry") or "").strip()
        return (
            0 if focal else 1,
            stage_order.get(stage, 99),
            component_order.get(component, 99),
            cntry_order.get(cntry, 99),
        )

    sorted_by_modified = sorted(
        surveys_list,
        key=lambda r: (r.get("lastModified") or ""),
        reverse=True,
    )
    sorted_surveys = sorted(sorted_by_modified, key=sort_key)

    # Always write the canonical filename.
    with INVENTORY_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for record in sorted_surveys:
            row = {
                "id": record.get("id"),
                "name": record.get("name"),
                "focal": bool_str(record.get("focal")),
                "locked": bool_str(record.get("locked")),
                "isActive": bool_str(record.get("isActive")),
                "preview_count": optional_int(record.get("preview_count")),
                "response_count": optional_int(record.get("response_count")),
                "lastModified": record.get("lastModified"),
                "component": record.get("component") or "pre",
                "stage": record.get("stage") or "main",
                "cntry": record.get("cntry") or "US",
                "creationDate": record.get("creationDate"),
                "editableViaApi": bool_str(record.get("editableViaApi")),
                "generated_at": timestamp,
                "ownerId": record.get("ownerId"),
            }
            writer.writerow(row)

    return INVENTORY_CSV


def load_focal_snapshot() -> Dict[str, bool]:
    """Load focal survey snapshot from disk."""
    if not FOCAL_SNAPSHOT.exists():
        return {}
    try:
        data = json.loads(FOCAL_SNAPSHOT.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {str(k): bool(v) for k, v in data.items()}


def save_focal_snapshot(snapshot: Dict[str, bool]) -> None:
    """Save focal survey snapshot to disk."""
    FOCAL_SNAPSHOT.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")


def archive_survey_assets(survey_id: str, *, timestamp: str) -> None:
    """Archive Excel and survey JSON files for a survey losing focal status."""
    files_moved = 0
    if EXCEL_DIR.exists():
        EXCEL_ARCHIVE.mkdir(parents=True, exist_ok=True)
        for path in EXCEL_DIR.glob(f"{survey_id}-*.xlsx"):
            if path.name.startswith("~$"):
                continue
            dest = EXCEL_ARCHIVE / f"{timestamp}__{path.name}"
            path.rename(dest)
            files_moved += 1
    if SURVEYS_DIR.exists():
        SURVEY_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        for path in SURVEYS_DIR.glob(f"*{survey_id}.json"):
            if path in {INVENTORY_CSV, LEGACY_SURVEY_CACHE} or path.is_dir():
                continue
            dest = SURVEY_ARCHIVE_DIR / f"{timestamp}__{path.name}"
            path.rename(dest)
            files_moved += 1
    if files_moved:
        print(
            f"[inventory] Archived {files_moved} file(s) for {survey_id} → archive/{timestamp}__…"
        )


def refresh_inventory(
    base_url: str,
    headers: Dict[str, str],
    *,
    survey_filter: List[str] | None = None,
    dry_run: bool = False,
    counts_scope: str | None = None,
) -> Tuple[List[dict], List[dict]]:
    """
    Refresh the survey inventory from Qualtrics API.

    Args:
        base_url: Qualtrics API base URL
        headers: API authentication headers
        survey_filter: Optional list of survey IDs to refresh (None = all)
        dry_run: If True, don't write to disk
        counts_scope: Optional scope for response counts ("focal" or "full").

    Returns:
        Tuple of (all inventory records, changed records)
    """
    current_user = fetch_current_user(base_url, headers)
    current_user_id = current_user.get("userId")

    existing_locks = load_existing_metadata()
    previous_focal = load_focal_snapshot()
    previous_records = load_cached_inventory_records()

    inventory_map: Dict[str, dict]

    if survey_filter:
        # Targeted refresh: start from existing records, update only specified surveys
        inventory_map = {sid: dict(record) for sid, record in previous_records.items()}
        for survey_id in survey_filter:
            try:
                payload = fetch_survey_payload(base_url, headers, survey_id)
            except requests.HTTPError as exc:
                print(
                    f"[inventory] WARNING: Failed to fetch {survey_id}: {exc}. "
                    "Next: verify QUALTRICS_BASE_URL/token and retry (run `qsync doctor --check-api`)."
                )
                continue
            flow_payload = None
            try:
                flow_payload = fetch_survey_flow_payload(base_url, headers, survey_id)
            except requests.HTTPError as exc:
                print(
                    f"[inventory] WARNING: Failed to fetch SurveyFlow for {survey_id}: {exc}. "
                    "Next: verify permissions for the survey and retry."
                )
            if not payload:
                print(
                    f"[inventory] WARNING: Survey {survey_id} not found; skipping targeted refresh. "
                    "Next: verify the survey ID and account permissions."
                )
                continue
            summary = build_summary_from_payload(payload, survey_id)
            detail = fetch_survey_detail(base_url, headers, survey_id, payload=payload)
            record = compose_inventory_record(
                summary,
                detail,
                current_user_id=current_user_id,
                existing_locks=existing_locks,
                payload=payload,
                flow_payload=flow_payload,
            )
            inventory_map[survey_id] = record
        inventory = list(inventory_map.values())
    else:
        # Full refresh: fetch all surveys
        summaries = fetch_surveys(base_url, headers)
        inventory = []
        payload_cache: Dict[str, dict] = {}
        for summary in summaries:
            survey_id = summary.get("id")
            if not survey_id:
                continue
            payload = None
            flow_payload = None
            try:
                flow_payload = fetch_survey_flow_payload(base_url, headers, survey_id)
            except requests.HTTPError as exc:
                print(
                    f"[inventory] WARNING: Failed to fetch SurveyFlow for {survey_id}: {exc}. "
                    "Next: verify permissions for the survey and retry."
                )
            record = compose_inventory_record(
                summary,
                {},
                current_user_id=current_user_id,
                existing_locks=existing_locks,
                payload=payload,
                flow_payload=flow_payload,
                include_counts=False,
            )
            inventory.append(record)
        if counts_scope in {"focal", "full"}:
            inventory_map = {
                record.get("id"): record for record in inventory if record.get("id")
            }
            if counts_scope == "full":
                target_ids = list(inventory_map.keys())
            else:
                target_ids = [
                    record_id
                    for record_id, record in inventory_map.items()
                    if record.get("focal")
                ]
            for survey_id in target_ids:
                payload = payload_cache.get(survey_id)
                if payload is None:
                    try:
                        payload = fetch_survey_payload(base_url, headers, survey_id)
                    except requests.HTTPError as exc:
                        print(
                            f"[inventory] WARNING: Failed to fetch {survey_id}: {exc}. "
                            "Next: verify QUALTRICS_BASE_URL/token and retry (run `qsync doctor --check-api`)."
                        )
                        continue
                    payload_cache[survey_id] = payload
                counts = payload.get("responseCounts") or {}
                record = inventory_map.get(survey_id)
                if record is None:
                    continue
                record["preview_count"] = _parse_int(counts.get("generated"))
                record["response_count"] = _parse_int(counts.get("auditable"))

    changed_records = determine_changed_records(inventory, previous_records)

    if not dry_run:
        # Archive assets for surveys losing focal status
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        for record in inventory:
            sid = record.get("id")
            if not sid:
                continue
            prev = previous_focal.get(sid, False)
            now = bool(record.get("focal"))
            if prev and not now:
                archive_survey_assets(sid, timestamp=timestamp)

        # Persist to CSV and update focal snapshot
        persist_surveys(inventory, current_user_id=current_user_id)
        snapshot_payload = {
            record.get("id"): bool(record.get("focal"))
            for record in inventory
            if record.get("id")
        }
        save_focal_snapshot(snapshot_payload)

    return inventory, changed_records


def _load_focal_survey_records() -> List[Dict[str, str]]:
    """Load focal survey records with ID, name, and lastModified.

    Returns records sorted by lastModified (newest first).
    """
    records = []
    for entry in _read_csv_rows():
        sid = (entry.get("id") or "").strip()
        if not sid:
            continue
        if _as_bool(entry.get("focal")):
            records.append(
                {
                    "id": sid,
                    "name": entry.get("name", "Untitled"),
                    "lastModified": entry.get("lastModified", ""),
                }
            )

    # Sort by lastModified descending (newest first)
    records.sort(key=lambda r: r.get("lastModified", ""), reverse=True)
    return records


def _load_all_survey_records() -> List[Dict[str, str]]:
    """Load all survey records with ID, name, lastModified, and focal status.

    Returns records sorted by lastModified (newest first).
    """
    records = []
    for entry in _read_csv_rows():
        sid = (entry.get("id") or "").strip()
        if not sid:
            continue
        records.append(
            {
                "id": sid,
                "name": entry.get("name", "Untitled"),
                "lastModified": entry.get("lastModified", ""),
                "focal": _as_bool(entry.get("focal")),
            }
        )

    # Sort by lastModified descending (newest first)
    records.sort(key=lambda r: r.get("lastModified", ""), reverse=True)
    return records


def _refresh_inventory_for_prompt() -> bool:
    """Best-effort inventory refresh used by interactive survey selection."""

    from .terminal_output import info
    from .config import get_client_config

    info("[qsync]", "Pulling survey inventory from Qualtrics...")
    try:
        base, headers = get_client_config()
        refresh_inventory(
            base,
            headers,
            survey_filter=None,
            dry_run=False,
            counts_scope=None,
        )
        return True
    except Exception as exc:
        print(f"[qsync] Failed to pull inventory: {exc}")
        return False


def _select_from_records(
    records: List[Dict[str, str]],
    *,
    message: str,
    include_focal_tag: bool,
    include_back: bool = False,
) -> str | None:
    if not records:
        return None

    choices: list[str] = []
    for record in records:
        focal_tag = " (focal)" if include_focal_tag and record.get("focal") else ""
        choices.append(f"{record['id']} - {record.get('name', 'Untitled')}{focal_tag}")

    choices.append("─" * 60)
    if include_back:
        choices.append("Back")
    choices.append("Cancel")

    from .interactive_menu import select_from_list

    selection = select_from_list(
        message=message,
        choices=choices,
        instruction="Use ↑↓ arrows and Enter to select",
    )
    if not selection or selection in {"Cancel"}:
        return None
    if selection == "Back":
        return "BACK"
    return selection.split(" - ", 1)[0].strip()


def prompt_for_survey_id(
    *,
    allow_all_surveys: bool = False,
    interactive: bool = True,
) -> str | None:
    """Prompt user to select a survey from focal surveys.

    Args:
        allow_all_surveys: If True (for pull commands), include option
                          to show all surveys via inventory pull
        interactive: If False, return None (CI/CD mode)

    Returns:
        Selected survey ID, or None if cancelled/non-interactive
    """
    if not interactive:
        return None

    has_inventory = INVENTORY_CSV.exists() or LEGACY_SURVEY_CACHE.exists()
    if not has_inventory:
        from .interactive_menu import select_from_list

        selection = select_from_list(
            message="Inventory file missing. What do you want to do?",
            choices=[
                "✓ Run `qsync survey inventory` now",
                "✗ Cancel",
            ],
        )
        if selection is None or "Cancel" in selection:
            print(
                "[qsync] Inventory file missing. Next: run `qsync survey inventory` "
                "or pass --survey-id."
            )
            return None
        if not _refresh_inventory_for_prompt():
            print(
                "[qsync] Could not refresh inventory. Next: verify credentials (run `qsync doctor --check-api`) "
                "and retry, or pass --survey-id."
            )
            return None

    # Load focal + all surveys from the local inventory (canonical or legacy).
    focal_records = _load_focal_survey_records()
    all_records = _load_all_survey_records()

    if not all_records:
        print("[qsync] No surveys found in inventory.")
        return None

    if focal_records:
        # Focal-first menu with optional expansion to all surveys (match qsync sync style).
        from .interactive_menu import select_from_list

        focal_choices: list[str] = []
        for record in focal_records:
            focal_choices.append(f"{record['id']} - {record.get('name', 'Untitled')}")
        focal_choices.append("─" * 60)
        if allow_all_surveys:
            focal_choices.append("✓ Show all surveys")
        focal_choices.append("✗ Cancel")

        while True:
            selection = select_from_list(
                message="Select a survey:",
                choices=focal_choices,
            )
            if not selection or "Cancel" in selection:
                return None
            if "Show all surveys" in selection:
                picked = _select_from_records(
                    all_records,
                    message="Select a survey (all):",
                    include_focal_tag=True,
                    include_back=True,
                )
                if picked == "BACK":
                    continue
                return picked
            return selection.split(" - ", 1)[0].strip()

    # No focal surveys defined.
    if not allow_all_surveys:
        print("[qsync] No focal surveys found in inventory.csv.")
        return None

    picked = _select_from_records(
        all_records,
        message="Select a survey:",
        include_focal_tag=True,
        include_back=False,
    )
    if picked in {None, "BACK"}:
        return None
    return picked
