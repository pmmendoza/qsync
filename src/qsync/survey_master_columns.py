"""Survey Master column configuration (order + visibility).

Two editing surfaces are supported:
1) Manual YAML edits (workspace-level config file)
2) An optional Textual TUI (`qsync survey master columns`)

The config is intentionally simple:
- Order is the list order in the YAML.
- `enabled: false` hides a column from the CSV/XLSX surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml

from .config import resolve_root


MASTER_COLUMNS_CONFIG_VERSION = 1
MASTER_COLUMNS_CONFIG_FILENAME = "survey_master_columns.yaml"
MASTER_COLUMNS_ENV_KEY = "QSYNC_MASTER_COLUMNS_YAML"

# Columns required for Survey Master workflows to function.
PINNED_MASTER_COLUMNS: tuple[str, ...] = ("SurveyID",)


@dataclass(frozen=True)
class MasterColumn:
    name: str
    enabled: bool
    pinned: bool = False


def master_columns_config_path(*, root: Path | None = None) -> Path:
    override = (os.environ.get(MASTER_COLUMNS_ENV_KEY) or "").strip()
    if override:
        return Path(override).expanduser().resolve()

    root = root or resolve_root(required=False) or Path.cwd()
    return (root / MASTER_COLUMNS_CONFIG_FILENAME).resolve()


def load_master_columns_yaml(path: Path) -> object | None:
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


def _coerce_enabled(value: object, *, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw in ("true", "t", "yes", "y", "1", "on", "enabled"):
            return True
        if raw in ("false", "f", "no", "n", "0", "off", "disabled"):
            return False
    return default


def _iter_config_entries(data: object) -> Iterable[tuple[str, bool]]:
    if data is None:
        return []

    # Allow either a dict wrapper (`{columns: [...]}`) or a bare list for
    # hand-authored configs.
    entries = data
    if isinstance(data, dict):
        entries = data.get("columns", [])

    if not isinstance(entries, list):
        return []

    parsed: list[tuple[str, bool]] = []
    for raw in entries:
        if isinstance(raw, str):
            name = raw.strip()
            if name:
                parsed.append((name, True))
            continue
        if isinstance(raw, dict):
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            enabled = _coerce_enabled(raw.get("enabled"), default=True)
            parsed.append((name, enabled))
            continue

    return parsed


def resolve_master_columns(
    *,
    available_in_default_order: Sequence[str],
    config_data: object | None,
    pinned: Sequence[str] = PINNED_MASTER_COLUMNS,
    default_enabled_when_missing: bool = False,
    default_enabled_when_no_config: bool = True,
) -> tuple[list[MasterColumn], list[str]]:
    """Resolve the final ordered column list with enabled/disabled flags.

    Returns:
        (columns, warnings)

    Notes:
    - Columns not present in `available_in_default_order` are ignored (with warning).
    - Columns missing from the config are appended at the end (disabled by default).
    - Pinned columns are always enabled; if missing, they're inserted at the start.
    """

    available_set = {c for c in available_in_default_order if str(c).strip()}
    pinned_set = {c for c in pinned if c in available_set}

    warnings: list[str] = []

    if config_data is None:
        columns = [
            MasterColumn(
                name=name,
                enabled=True if name in pinned_set else bool(default_enabled_when_no_config),
                pinned=name in pinned_set,
            )
            for name in available_in_default_order
        ]
        # Ensure pinned even if a caller asked for default_enabled_when_no_config=False.
        columns = [
            MasterColumn(name=c.name, enabled=True, pinned=True)
            if c.name in pinned_set
            else c
            for c in columns
        ]
        return columns, warnings

    resolved: list[MasterColumn] = []
    seen: set[str] = set()

    for name, enabled in _iter_config_entries(config_data):
        if name not in available_set:
            warnings.append(f"Unknown Survey Master column in config (ignored): {name}")
            continue
        if name in seen:
            warnings.append(f"Duplicate Survey Master column in config (ignored): {name}")
            continue
        seen.add(name)
        is_pinned = name in pinned_set
        if is_pinned and not enabled:
            warnings.append(f"Pinned Survey Master column cannot be disabled; forcing enabled: {name}")
            enabled = True
        resolved.append(MasterColumn(name=name, enabled=bool(enabled), pinned=is_pinned))

    # Ensure pinned columns are present and enabled (insert at the top if omitted).
    for pinned_name in pinned_set:
        if pinned_name not in seen:
            warnings.append(f"Pinned Survey Master column missing from config; inserting: {pinned_name}")
            resolved.insert(
                0, MasterColumn(name=pinned_name, enabled=True, pinned=True)
            )
            seen.add(pinned_name)

    # Append any mapping columns not mentioned in the config (disabled by default).
    for name in available_in_default_order:
        if name in seen:
            continue
        is_pinned = name in pinned_set
        enabled = True if is_pinned else bool(default_enabled_when_missing)
        resolved.append(MasterColumn(name=name, enabled=enabled, pinned=is_pinned))
        seen.add(name)

    return resolved, warnings


def dump_master_columns_yaml(columns: Sequence[MasterColumn]) -> str:
    payload = {
        "version": MASTER_COLUMNS_CONFIG_VERSION,
        "columns": [{"name": c.name, "enabled": bool(c.enabled)} for c in columns],
    }
    text = yaml.safe_dump(
        payload,
        sort_keys=False,
        allow_unicode=False,
        width=120,
    )
    header = (
        "# qsync Survey Master columns\n"
        "# - Order is the list order below.\n"
        "# - enabled: false hides a column from qualtrics_master.csv/.xlsx.\n"
    )
    return header + text


def save_master_columns_yaml(path: Path, columns: Sequence[MasterColumn]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = dump_master_columns_yaml(columns)
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")

