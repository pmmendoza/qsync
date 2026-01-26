"""
Convert between Qualtrics API JSON Definition format and QSF portable format.

QSF Format: Used for export/import of complete surveys
  - Top-level: {"SurveyEntry": {...}, "SurveyElements": [...]}
  - Portable, human-readable, used for backups and transfers

JSON Definition Format: Used by the API
  - Top-level: {"SurveyID": "...", "SurveyName": "...", "Blocks": {...}, "Questions": {...}, ...}
  - Flat structure, suitable for programmatic manipulation
"""

from __future__ import annotations

from datetime import datetime


def json_definition_to_qsf(definition: dict) -> dict:
    """
    Convert a JSON Definition (from GET /survey-definitions/{id}) to QSF format.

    Args:
        definition: JSON definition dictionary with flat structure

    Returns:
        QSF format dictionary with SurveyEntry and SurveyElements
    """

    # Extract metadata for SurveyEntry
    survey_entry = {
        "SurveyID": definition.get("SurveyID"),
        "SurveyName": definition.get("SurveyName"),
        "SurveyDescription": definition.get("SurveyDescription"),
        "SurveyOwnerID": definition.get("OwnerID"),
        "SurveyBrandID": definition.get("BrandID"),
        "DivisionID": definition.get("DivisionID"),
        "SurveyLanguage": "EN",  # Default language
        "SurveyActiveResponseSet": _get_active_response_set(
            definition.get("ResponseSets", {})
        ),
        "SurveyStatus": definition.get("SurveyStatus", "Active"),
        "SurveyStartDate": "0000-00-00 00:00:00",
        "SurveyExpirationDate": "0000-00-00 00:00:00",
        "SurveyCreationDate": _normalize_timestamp(
            definition.get("SurveyCreationDate", _current_timestamp())
        ),
        "CreatorID": definition.get("CreatorID"),
        "LastModified": _normalize_timestamp(
            definition.get("LastModified", _current_timestamp())
        ),
        "LastAccessed": _normalize_timestamp(
            definition.get("LastAccessed", "0000-00-00 00:00:00")
        ),
        "LastActivated": _normalize_timestamp(
            definition.get("LastActivated", "0000-00-00 00:00:00")
        ),
        "Deleted": None,
    }

    # Build SurveyElements array
    survey_elements = []
    survey_id = definition.get("SurveyID")

    # Element 1: Blocks (BL)
    if definition.get("Blocks"):
        blocks_list = _blocks_dict_to_list(definition["Blocks"])
        survey_elements.append(
            {
                "SurveyID": survey_id,
                "Element": "BL",
                "PrimaryAttribute": "Survey Blocks",
                "SecondaryAttribute": None,
                "TertiaryAttribute": None,
                "Payload": blocks_list,
            }
        )

    # Element 2: Survey Flow (FL)
    if definition.get("SurveyFlow"):
        survey_elements.append(
            {
                "SurveyID": survey_id,
                "Element": "FL",
                "PrimaryAttribute": "Survey Flow",
                "SecondaryAttribute": None,
                "TertiaryAttribute": None,
                "Payload": {
                    "Type": definition["SurveyFlow"].get("Type", "Default"),
                    "FlowID": definition["SurveyFlow"].get("FlowID", ""),
                    "Flow": definition["SurveyFlow"].get("Flow", []),
                    "Properties": definition["SurveyFlow"].get("Properties", {}),
                },
            }
        )

    # Element 3: Preview/Looks (PL)
    survey_elements.append(
        {
            "SurveyID": survey_id,
            "Element": "PL",
            "PrimaryAttribute": "Preview Link",
            "SecondaryAttribute": None,
            "TertiaryAttribute": None,
            "Payload": {
                "PreviewType": "Unauthenticated",
                "PreviewID": "",
            },
        }
    )

    # Element 4: Project Category (PROJ)
    if definition.get("ProjectInfo"):
        survey_elements.append(
            {
                "SurveyID": survey_id,
                "Element": "PROJ",
                "PrimaryAttribute": definition["ProjectInfo"].get(
                    "ProjectCategory", "CORE"
                ),
                "SecondaryAttribute": None,
                "TertiaryAttribute": None,
                "Payload": {
                    "ProjectCategory": definition["ProjectInfo"].get(
                        "ProjectCategory", "CORE"
                    ),
                    "SchemaVersion": definition["ProjectInfo"].get(
                        "SchemaVersion", "3.2"
                    ),
                },
            }
        )

    # Element 5: Question Count (QC)
    question_count = str(len(definition.get("Questions", {})))
    survey_elements.append(
        {
            "SurveyID": survey_id,
            "Element": "QC",
            "PrimaryAttribute": "Survey Question Count",
            "SecondaryAttribute": question_count,
            "TertiaryAttribute": None,
            "Payload": None,
        }
    )

    # Element 6: Response Sets (RS)
    if definition.get("ResponseSets"):
        active_rs_id = _get_active_response_set(definition["ResponseSets"])
        survey_elements.append(
            {
                "SurveyID": survey_id,
                "Element": "RS",
                "PrimaryAttribute": active_rs_id,
                "SecondaryAttribute": "Default Response Set",
                "TertiaryAttribute": None,
                "Payload": definition["ResponseSets"],
            }
        )

    # Element 7: Scoring (SCO)
    if definition.get("Scoring"):
        survey_elements.append(
            {
                "SurveyID": survey_id,
                "Element": "SCO",
                "PrimaryAttribute": "Scoring",
                "SecondaryAttribute": None,
                "TertiaryAttribute": None,
                "Payload": definition["Scoring"],
            }
        )

    # Element 8: Survey Options (SO)
    if definition.get("SurveyOptions"):
        survey_elements.append(
            {
                "SurveyID": survey_id,
                "Element": "SO",
                "PrimaryAttribute": "Survey Options",
                "SecondaryAttribute": None,
                "TertiaryAttribute": None,
                "Payload": definition["SurveyOptions"],
            }
        )

    # Element 9: Questions (SQ) - one element per question
    for qid, question in definition.get("Questions", {}).items():
        q_text = question.get("QuestionText", "")[:50]  # Truncate for display
        survey_elements.append(
            {
                "SurveyID": survey_id,
                "Element": "SQ",
                "PrimaryAttribute": qid,
                "SecondaryAttribute": q_text,
                "TertiaryAttribute": None,
                "Payload": question,
            }
        )

    # Element 10: Statistics (STAT)
    survey_elements.append(
        {
            "SurveyID": survey_id,
            "Element": "STAT",
            "PrimaryAttribute": "Survey Statistics",
            "SecondaryAttribute": None,
            "TertiaryAttribute": None,
            "Payload": {
                "MobileCompatible": True,
                "ID": survey_id,
            },
        }
    )

    return {
        "SurveyEntry": survey_entry,
        "SurveyElements": survey_elements,
    }


def qsf_to_json_definition(qsf: dict) -> dict:
    """
    Convert a QSF format dictionary to JSON Definition format.

    Args:
        qsf: QSF dictionary with SurveyEntry and SurveyElements

    Returns:
        JSON definition dictionary with flat structure
    """

    survey_entry = qsf.get("SurveyEntry", {})
    survey_elements = qsf.get("SurveyElements", [])

    # Start with metadata from SurveyEntry
    definition = {
        "SurveyID": survey_entry.get("SurveyID"),
        "SurveyName": survey_entry.get("SurveyName"),
        "SurveyDescription": survey_entry.get("SurveyDescription"),
        "SurveyStatus": survey_entry.get("SurveyStatus", "Active"),
        "OwnerID": survey_entry.get("SurveyOwnerID"),
        "BrandID": survey_entry.get("SurveyBrandID"),
        "DivisionID": survey_entry.get("DivisionID"),
        "CreatorID": survey_entry.get("CreatorID"),
        "LastModified": survey_entry.get("LastModified"),
        "LastAccessed": survey_entry.get("LastAccessed"),
        "LastActivated": survey_entry.get("LastActivated"),
        "QuestionCount": "0",
    }

    # Extract content from SurveyElements
    questions = {}
    for elem in survey_elements:
        elem_type = elem.get("Element")
        payload = elem.get("Payload")

        if elem_type == "BL" and payload:
            definition["Blocks"] = _blocks_list_to_dict(payload)
        elif elem_type == "FL" and payload:
            definition["SurveyFlow"] = payload
        elif elem_type == "PROJ" and payload:
            definition["ProjectInfo"] = payload
        elif elem_type == "RS" and payload:
            definition["ResponseSets"] = payload
        elif elem_type == "SCO" and payload:
            definition["Scoring"] = payload
        elif elem_type == "SO" and payload:
            definition["SurveyOptions"] = payload
        elif elem_type == "SQ" and payload:
            qid = elem.get("PrimaryAttribute")
            questions[qid] = payload
        elif elem_type == "QC":
            definition["QuestionCount"] = elem.get("SecondaryAttribute", "0")

    if questions:
        definition["Questions"] = questions

    return definition


def _get_active_response_set(response_sets: dict) -> str:
    """Get the ID of the active/default response set."""
    if not response_sets:
        return ""
    # Return first key (usually the default)
    return next(iter(response_sets.keys()), "")


def _normalize_timestamp(ts: str | None) -> str:
    """
    Normalize timestamps to Qualtrics format (YYYY-MM-DD HH:MM:SS).
    Handles ISO 8601 format conversion if needed.
    """
    if not ts:
        return "0000-00-00 00:00:00"

    # Already in correct format
    if len(ts) == 19 and ts[10] == " ":  # "YYYY-MM-DD HH:MM:SS"
        return ts

    # ISO 8601 format (e.g., "2025-11-19T17:24:12Z")
    if "T" in ts:
        # Remove Z or timezone info, replace T with space
        ts = ts.replace("T", " ").replace("Z", "").split("+")[0]
        # Take only first 19 chars for consistency
        return ts[:19]

    return ts


def _blocks_dict_to_list(blocks_dict: dict) -> list:
    """Convert blocks dictionary to QSF format list."""
    # QSF stores blocks as a list in Payload
    # JSON stores as a dict with numeric or string keys
    if isinstance(blocks_dict, list):
        return blocks_dict

    result = []
    for key in sorted(
        blocks_dict.keys(), key=lambda x: int(x) if str(x).isdigit() else x
    ):
        block = blocks_dict[key]
        if isinstance(block, dict):
            result.append(block)
    return result


def _blocks_list_to_dict(blocks_list: list) -> dict:
    """Convert blocks list (QSF) to dictionary (JSON)."""
    if isinstance(blocks_list, dict):
        return blocks_list

    result = {}
    for i, block in enumerate(blocks_list):
        result[str(i)] = block
    return result


def _current_timestamp() -> str:
    """Return current timestamp in Qualtrics format."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
