"""Survey tagging and filtering for batch operations.

Provides tag-based filtering for survey master operations, allowing
selective application of changes based on survey properties from
the inventory CSV (component, stage, country, etc.).
"""

import csv
from pathlib import Path
from typing import Dict, List, Optional, Set

from .config import resolve_root


def _surveys_csv_path() -> Path:
    """Get path to the inventory CSV (canonical, with legacy fallback)."""
    root = resolve_root(required=False) or Path.cwd()
    path = root / "surveys" / "inventory.csv"
    if path.exists():
        return path
    return root / "surveys" / "qualtrics_surveys.csv"


def load_survey_tags() -> Dict[str, Dict[str, str]]:
    """Load survey tags from the inventory CSV.

    Returns dict mapping survey_id -> {component, stage, cntry, ...}

    Example:
        {
            'SV_001': {'component': 'pre', 'stage': 'pilot', 'cntry': 'US'},
            'SV_002': {'component': 'post', 'stage': 'prod', 'cntry': 'NL'},
            ...
        }
    """
    tags: Dict[str, Dict[str, str]] = {}

    csv_path = _surveys_csv_path()
    if not csv_path.exists():
        return tags

    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return tags

            for row in reader:
                survey_id = row.get("id", "").strip()
                if not survey_id:
                    continue

                # Extract tag columns
                tags[survey_id] = {
                    "component": row.get("component", "").strip(),
                    "stage": row.get("stage", "").strip(),
                    "cntry": row.get("cntry", "").strip(),
                }

    except (OSError, csv.Error):
        # If reading fails, return empty dict (non-fatal)
        pass

    return tags


def parse_tag_filters(tag_specs: Optional[List[str]]) -> Dict[str, Set[str]]:
    """Parse tag filter specifications into a dict.

    Args:
        tag_specs: List of "key=value" strings
                   Example: ["component=pre", "stage=prod"]

    Returns:
        Dict mapping tag_key -> set of allowed values
        Example: {"component": {"pre"}, "stage": {"prod"}}

    Raises:
        ValueError: If tag spec format is invalid
    """
    filters: Dict[str, Set[str]] = {}

    if not tag_specs:
        return filters

    for spec in tag_specs:
        if "=" not in spec:
            raise ValueError(f"Invalid tag spec: '{spec}' (expected 'key=value')")

        key, value = spec.split("=", 1)
        key = key.strip()
        value = value.strip()

        if not key or not value:
            raise ValueError(f"Invalid tag spec: '{spec}' (key and value required)")

        if key not in filters:
            filters[key] = set()

        filters[key].add(value)

    return filters


def filter_surveys_by_tags(
    survey_ids: List[str], tag_filters: Dict[str, Set[str]]
) -> List[str]:
    """Filter survey IDs based on tag criteria.

    Args:
        survey_ids: List of survey IDs to filter
        tag_filters: Dict mapping tag_key -> set of allowed values
                     If empty, returns all surveys (no filtering)

    Returns:
        List of survey IDs matching all tag criteria
    """
    if not tag_filters:
        return survey_ids

    # Load all survey tags
    all_tags = load_survey_tags()

    # Filter surveys that match ALL tag criteria
    filtered = []
    for survey_id in survey_ids:
        survey_tags = all_tags.get(survey_id, {})

        # Check if survey matches all filter criteria
        matches_all = True
        for tag_key, allowed_values in tag_filters.items():
            tag_value = survey_tags.get(tag_key, "")
            if tag_value not in allowed_values:
                matches_all = False
                break

        if matches_all:
            filtered.append(survey_id)

    return filtered


def get_available_tags() -> Dict[str, Set[str]]:
    """Get all unique tag values present in surveys.

    Returns:
        Dict mapping tag_key -> set of unique values
        Example: {
            "component": {"pre", "post", "payout"},
            "stage": {"pilot", "prod"},
            "cntry": {"US", "NL", "UK"}
        }
    """
    all_tags = load_survey_tags()

    available: Dict[str, Set[str]] = {
        "component": set(),
        "stage": set(),
        "cntry": set(),
    }

    for survey_tags in all_tags.values():
        for key in available.keys():
            value = survey_tags.get(key, "").strip()
            if value:
                available[key].add(value)

    return available


def format_tag_help() -> str:
    """Format help text showing available tags.

    Returns:
        String with available tag options
    """
    available = get_available_tags()

    lines = ["Available tags for filtering:\n"]

    for tag_key in ["component", "stage", "cntry"]:
        values = sorted(available.get(tag_key, set()))
        if values:
            values_str = ", ".join(values)
            lines.append(f"  --tag {tag_key}=<value>")
            lines.append(f"    Allowed values: {values_str}")
        else:
            lines.append(f"  --tag {tag_key}=<value>")
            lines.append("    (no values currently available)")

    return "\n".join(lines)
