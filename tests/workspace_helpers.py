"""Shared test helpers for creating minimal workspace directory structures."""

from __future__ import annotations

import json
from pathlib import Path


def ensure_qsync_workspace(root: Path) -> None:
    """Create the minimal on-disk structure expected by qsync commands."""
    (root / "surveys").mkdir(parents=True, exist_ok=True)
    (root / "excel").mkdir(parents=True, exist_ok=True)
    (root / "survey_js").mkdir(parents=True, exist_ok=True)


def ensure_psync_workspace(root: Path) -> None:
    """Create the minimal on-disk structure expected by psync commands."""
    (root / "prolific").mkdir(parents=True, exist_ok=True)


def write_inventory_csv(root: Path, csv_text: str) -> Path:
    """Write `surveys/inventory.csv` and return the resulting path."""
    ensure_qsync_workspace(root)
    path = root / "surveys" / "inventory.csv"
    path.write_text(csv_text, encoding="utf-8")
    return path


def ensure_master_workspace(root: Path) -> None:
    """Create workspace with master snapshots and mapping CSV directory."""
    ensure_qsync_workspace(root)
    (root / "surveys" / "qualtrics_master_snapshots").mkdir(parents=True, exist_ok=True)
    (root / "surveys").mkdir(parents=True, exist_ok=True)


def write_mapping_csv(root: Path, csv_text: str) -> Path:
    """Write surveys/qualtrics_api_key_mapping.csv and return the path."""
    (root / "surveys").mkdir(parents=True, exist_ok=True)
    path = root / "surveys" / "qualtrics_api_key_mapping.csv"
    path.write_text(csv_text, encoding="utf-8")
    return path


def write_focal_snapshot(root: Path, focal_dict: dict) -> Path:
    """Write surveys/.focal_snapshot.json and return the path."""
    ensure_qsync_workspace(root)
    path = root / "surveys" / ".focal_snapshot.json"
    path.write_text(json.dumps(focal_dict, indent=2), encoding="utf-8")
    return path


def write_master_snapshot(root: Path, survey_id: str, snapshot_data: dict) -> Path:
    """Write a master snapshot JSON for a survey and return the path."""
    ensure_master_workspace(root)
    path = root / "surveys" / "qualtrics_master_snapshots" / f"{survey_id}.json"
    path.write_text(json.dumps(snapshot_data, indent=2), encoding="utf-8")
    return path
