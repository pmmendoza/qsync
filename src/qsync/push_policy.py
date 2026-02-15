"""Push safeguard policies used by `qsync push` and related commands."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional

from .api_push import send_api_request
from .config import get_client_config, resolve_root

ROOT = resolve_root(required=False) or Path.cwd()
FRESHNESS_LIMIT = timedelta(minutes=30)
TRUE_VALUES = {"true", "1", "yes", "y", "t"}


class InventoryNotFound(RuntimeError):
    """Raised when the survey inventory CSV is missing or unreadable."""


@dataclass
class PushContext:
    """Context about a survey used to enforce push safeguards."""

    survey_id: str
    survey_name: str
    preview_count: int
    response_count: int
    counts_source: str
    generated_at: Optional[datetime]
    stale: bool
    counts_unknown: bool

    def describe_counts(self) -> str:
        """Return a human-readable summary of response counts and their provenance."""

        timestamp = self.generated_at.isoformat() if self.generated_at else "unknown"
        return (
            f"Responses: {self.response_count} live / {self.preview_count} preview"
            f" (source: {self.counts_source}, inventory @ {timestamp})"
        )


def _parse_timestamp(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_optional_int(value: str | None) -> Optional[int]:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return int(stripped)
    except ValueError:
        return None


def _load_inventory_row(survey_id: str) -> Dict[str, str]:
    from .survey_inventory import resolve_inventory_csv_path

    inventory_csv = resolve_inventory_csv_path(required=False)
    if not inventory_csv.exists():
        raise InventoryNotFound(
            "Missing surveys/inventory.csv (legacy: surveys/qualtrics_surveys.csv). "
            "Run 'qsync survey inventory' first."
        )
    with inventory_csv.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(
            line for line in fh if not line.lstrip().startswith("#")
        )
        for row in reader:
            if (row.get("id") or "").strip() == survey_id:
                return row
    raise InventoryNotFound(
        f"SurveyID {survey_id} not found in {inventory_csv}. Run the inventory script and retry."
    )


def _fetch_quick_counts(
    survey_id: str,
    *,
    base_url: str | None = None,
    headers: dict | None = None,
) -> tuple[int, int]:
    if base_url is None or headers is None:
        base_url, headers = get_client_config()
    response = send_api_request(
        action="qsync.push.policy.quick.counts",
        method="GET",
        base_url=base_url,
        headers=headers,
        path=f"surveys/{survey_id}",
        log_event=False,
        timeout=30,
    )
    result = response.json().get("result", {})
    counts = result.get("responseCounts") or {}
    preview = int(counts.get("generated") or 0)
    live = int(counts.get("auditable") or 0)
    return preview, live


def load_push_context(
    survey_id: str,
    *,
    base_url: str | None = None,
    headers: dict | None = None,
) -> PushContext:
    """Load response-count context for a survey (inventory-first, with optional live-check)."""

    inventory_missing = False
    try:
        row = _load_inventory_row(survey_id)
    except InventoryNotFound:
        row = {"id": survey_id, "name": survey_id}
        inventory_missing = True

    name = (row.get("name") or survey_id).strip()
    preview_count_opt = _parse_optional_int(row.get("preview_count"))
    response_count_opt = _parse_optional_int(row.get("response_count"))
    generated_at = _parse_timestamp(row.get("generated_at"))

    counts_unknown = preview_count_opt is None or response_count_opt is None
    preview = preview_count_opt or 0
    live = response_count_opt or 0
    counts_source = "missing-inventory" if inventory_missing else "inventory"

    now = datetime.now(timezone.utc)
    stale = generated_at is None or (now - generated_at) > FRESHNESS_LIMIT
    needs_refresh = counts_unknown or stale or (preview == 0 and live == 0)

    if needs_refresh:
        try:
            preview, live = _fetch_quick_counts(
                survey_id, base_url=base_url, headers=headers
            )
            counts_unknown = False
            counts_source = "live-check"
            stale = False
        except Exception:
            # Leave counts as-is; enforcement layer will decide how to proceed.
            pass

    return PushContext(
        survey_id=survey_id,
        survey_name=name,
        preview_count=preview,
        response_count=live,
        counts_source=counts_source,
        generated_at=generated_at,
        stale=stale,
        counts_unknown=counts_unknown,
    )
