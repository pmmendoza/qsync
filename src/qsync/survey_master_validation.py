"""Field validation framework for survey master CSV.

Provides extensible validation for field values based on mapping CSV schema:
- data_type: string, int, bool, datetime, url, object
- allowed_values: semicolon-separated allowed values (e.g., "Active; Inactive")
- format_notes: ISO 8601, nullable, etc.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Tuple


def get_field_validators() -> (
    Dict[str, Callable[[str, dict], Tuple[bool, Optional[str]]]]
):
    """Return mapping of field_name -> validation function.

    Each validator function takes (value, field_info) and returns (is_valid, error_message).
    """
    return {
        "bool": _validate_boolean,
        "string": _validate_string,
        "int": _validate_integer,
        "datetime": _validate_datetime,
        "url": _validate_url,
        "object": _validate_object,
    }


def _validate_boolean(value: str, field_info: dict) -> Tuple[bool, Optional[str]]:
    """Validate boolean value (true/false)."""
    if not value:
        return (True, None)  # Empty is OK

    if value.lower() in ("true", "false", "yes", "no", "1", "0", "on", "off", "t", "f"):
        return (True, None)

    return (False, f"Expected 'true' or 'false', got '{value}'")


def _validate_string(value: str, field_info: dict) -> Tuple[bool, Optional[str]]:
    """Validate string value."""
    if not value:
        return (True, None)  # Empty is OK

    # Check allowed values if specified
    allowed_values = field_info.get("allowed_values", "").strip()
    if allowed_values:
        allowed_list = [v.strip() for v in allowed_values.split(";")]
        # Special-case boolean-ish enums, since spreadsheets often coerce to TRUE/FALSE.
        allowed_lower = {v.lower() for v in allowed_list if v}
        if allowed_lower == {"true", "false"}:
            if value.strip().lower() not in allowed_lower:
                return (False, "Must be one of: true, false")
        else:
            if value not in allowed_list:
                return (False, f"Must be one of: {', '.join(allowed_list)}")

    # Check max length if specified (can be inferred from format_notes)
    format_notes = field_info.get("format_notes", "").lower()
    if "url" in format_notes:
        # URLs should be reasonably long but not unlimited
        if not value.startswith(("http://", "https://")):
            return (False, "URL must start with http:// or https://")

    return (True, None)


def _validate_integer(value: str, field_info: dict) -> Tuple[bool, Optional[str]]:
    """Validate integer value."""
    if not value:
        return (True, None)  # Empty is OK

    try:
        int_val = int(value)

        # Check if must be positive (common for counts, page numbers, etc.)
        if int_val < 0:
            field_name = field_info.get("field_name", "field")
            if any(x in field_name.lower() for x in ("count", "page", "number", "id")):
                return (False, "Must be non-negative")

        return (True, None)
    except ValueError:
        return (False, f"Expected integer, got '{value}'")


def _validate_datetime(value: str, field_info: dict) -> Tuple[bool, Optional[str]]:
    """Validate datetime value (ISO 8601 format)."""
    if not value:
        return (True, None)  # Empty is OK (nullable)

    # Check ISO 8601 format (basic check)
    if "T" not in value and not any(x in value for x in ("-", "/")):
        return (
            False,
            "Date must be ISO 8601 format (e.g., 2025-12-20 or 2025-12-20T14:00:00Z)",
        )

    return (True, None)


def _validate_url(value: str, field_info: dict) -> Tuple[bool, Optional[str]]:
    """Validate URL value."""
    if not value:
        return (True, None)  # Empty is OK (nullable)

    if not value.startswith(("http://", "https://")):
        return (False, "URL must start with http:// or https://")

    # Basic URL check: should have some structure
    if len(value) < 10:
        return (False, "URL is too short")

    return (True, None)


def _validate_object(value: str, field_info: dict) -> Tuple[bool, Optional[str]]:
    """Validate object value (JSON or complex object).

    Objects must be valid JSON and should decode to an object/map.
    """
    if not value:
        return (True, None)  # Empty is OK (nullable)

    try:
        decoded = json.loads(value)
    except Exception:
        return (False, "Expected valid JSON object text")

    if not isinstance(decoded, dict):
        return (False, "Expected a JSON object (e.g., {\"key\": \"value\"})")

    return (True, None)


def validate_field_value(
    field_name: str, value: str, field_info: dict
) -> Tuple[bool, Optional[str]]:
    """Validate a single field value against schema rules.

    Args:
        field_name: Name of the field being validated
        value: Value to validate (from CSV)
        field_info: Field metadata from mapping CSV

    Returns:
        (is_valid, error_message) tuple
    """
    # Skip validation for read-only fields (prefixed with _)
    if field_name.startswith("_"):
        return (True, None)

    # Skip empty values (nullable)
    if not value or value.strip() == "":
        return (True, None)

    # Get data type from mapping
    data_type = field_info.get("data_type", "string").strip().lower()

    # Select appropriate validator
    validators = get_field_validators()
    validator = validators.get(data_type, validators["string"])

    return validator(value, field_info)


def validate_all_changes(
    csv_rows: List[dict],
    mapping: Dict[str, dict],
) -> List[Dict[str, Any]]:
    """Validate all changed values in CSV.

    Performs both field-level validation and cross-field constraint checking.

    Args:
        csv_rows: List of CSV row dicts from load_master_csv()
        mapping: Field mapping from _parse_mapping_csv()

    Returns:
        List of validation errors:
        [
            {
                'survey_id': 'SV_001',
                'field_name': 'SurveyName',
                'value': 'too long value',
                'error': 'Must be at most 200 chars',
                'type': 'field' (default)
            },
            ...
        ]
    """
    errors: List[Dict[str, Any]] = []

    for row in csv_rows:
        survey_id = row.get("SurveyID", "unknown")

        # Validate individual fields
        for field_name, field_value in row.items():
            # Skip survey ID and read-only fields
            if field_name in ("SurveyID") or field_name.startswith("_"):
                continue

            # Skip if field not in mapping (will be caught by CSV validation)
            if field_name not in mapping:
                continue

            field_info = mapping[field_name]

            # Validate the value
            is_valid, error_msg = validate_field_value(
                field_name, field_value, field_info
            )

            if not is_valid:
                errors.append(
                    {
                        "survey_id": survey_id,
                        "field_name": field_name,
                        "value": field_value,
                        "error": error_msg,
                        "type": "field",
                    }
                )

        # Validate cross-field constraints
        cross_field_errors = validate_cross_field_constraints(row, mapping)
        errors.extend(cross_field_errors)

    return errors


def validate_cross_field_constraints(
    row: dict, mapping: Dict[str, dict]
) -> List[Dict[str, Any]]:
    """Validate cross-field constraints and relationships.

    Checks relationships between multiple fields, such as:
    - SurveyStartDate < SurveyExpirationDate
    - Other temporal and logical constraints

    Args:
        row: A single CSV row dict
        mapping: Field mapping from _parse_mapping_csv()

    Returns:
        List of validation errors for this row
    """
    from datetime import datetime

    errors: List[Dict[str, Any]] = []
    survey_id = row.get("SurveyID", "unknown")

    # Helper to parse ISO datetime
    def parse_date(date_str: str) -> Optional[datetime]:
        """Parse ISO 8601 datetime string."""
        if not date_str or date_str.strip() == "":
            return None

        try:
            # Try with time component
            if "T" in date_str:
                return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            # Try date only
            else:
                return datetime.fromisoformat(date_str)
        except (ValueError, AttributeError):
            return None

    # Constraint 1: SurveyStartDate < SurveyExpirationDate
    start_date_str = row.get("SurveyStartDate", "").strip()
    expiration_date_str = row.get("SurveyExpirationDate", "").strip()

    if start_date_str and expiration_date_str:
        start_date = parse_date(start_date_str)
        expiration_date = parse_date(expiration_date_str)

        if start_date and expiration_date:
            if start_date >= expiration_date:
                errors.append(
                    {
                        "survey_id": survey_id,
                        "field_name": "SurveyStartDate",
                        "value": start_date_str,
                        "error": f"Start date must be before expiration date ({expiration_date_str})",
                        "type": "cross_field",
                    }
                )

    # Constraint 2: PartialDataCloseAfter behavior validation
    partial_data = row.get("PartialData", "").strip()
    partial_close_after = row.get("PartialDataCloseAfter", "").strip()

    if partial_close_after:
        # PartialDataCloseAfter should only be set if PartialData is enabled.
        # Qualtrics uses non-boolean strings (e.g., "+4 hour") for PartialData.
        partial_data_enabled = bool(partial_data)
        if partial_data and partial_data.lower() in ("false", "no", "disabled", "off"):
            partial_data_enabled = False

        if not partial_data_enabled:
            errors.append(
                {
                    "survey_id": survey_id,
                    "field_name": "PartialDataCloseAfter",
                    "value": partial_close_after,
                    "error": "PartialDataCloseAfter should only be set if PartialData is enabled",
                    "type": "cross_field",
                }
            )
        elif partial_close_after not in ("SurveyStart", "SurveyEnd", "LastActivity"):
            errors.append(
                {
                    "survey_id": survey_id,
                    "field_name": "PartialDataCloseAfter",
                    "value": partial_close_after,
                    "error": "PartialDataCloseAfter must be SurveyStart, SurveyEnd, or LastActivity",
                    "type": "cross_field",
                }
            )

    # Constraint 3: Survey protection method consistency
    survey_protection = row.get("SurveyProtection", "").strip()
    password_protection = row.get("PasswordProtection", "").strip()

    if survey_protection == "PasswordProtected" and password_protection.lower() in (
        "false",
        "no",
    ):
        errors.append(
            {
                "survey_id": survey_id,
                "field_name": "PasswordProtection",
                "value": password_protection,
                "error": "PasswordProtection must be 'Yes' when SurveyProtection is 'PasswordProtected'",
                "type": "cross_field",
            }
        )

    return errors


def format_validation_errors(errors: List[Dict[str, Any]]) -> str:
    """Format validation errors for display."""
    if not errors:
        return ""

    # Separate field-level and cross-field errors
    field_errors = [e for e in errors if e.get("type") != "cross_field"]
    cross_field_errors = [e for e in errors if e.get("type") == "cross_field"]

    lines = ["❌ Validation Errors:"]
    current_survey = None

    # Show field-level errors
    if field_errors:
        for error in field_errors:
            survey_id = error.get("survey_id")
            if survey_id != current_survey:
                lines.append(f"\n  {survey_id}:")
                current_survey = survey_id

            field_name = error.get("field_name")
            error_msg = error.get("error")
            lines.append(f"    • {field_name}: {error_msg}")

    # Show cross-field errors
    if cross_field_errors:
        if field_errors:
            lines.append("\n  Cross-Field Constraints:")
        for error in cross_field_errors:
            survey_id = error.get("survey_id")
            if survey_id != current_survey:
                if not field_errors and survey_id != current_survey:
                    lines.append(f"\n  {survey_id}:")
                current_survey = survey_id

            field_name = error.get("field_name")
            error_msg = error.get("error")
            lines.append(f"    • {field_name}: {error_msg}")

    return "\n".join(lines)
