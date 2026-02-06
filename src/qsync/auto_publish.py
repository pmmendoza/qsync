"""
Auto-publish functionality after push operations.

Provides unified auto-publish logic across all dimensions with interactive
description prompts, default description templates, and validation.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from qsync.publish_description import make_publish_description
from qsync.qualtrics_client import (
    SURVEY_VERSION_DESCRIPTION_MAX_CHARS,
    publish_survey_definition,
)

DimensionType = Literal["items", "edf", "js", "translations", "eos", "flow"]


class PublishSkipped(Exception):
    """Raised when user explicitly skips publishing."""

    pass


def auto_publish_after_push(
    survey_id: str,
    dimension: DimensionType,
    *,
    skip_publish: bool = False,
    auto_yes: bool | None = None,
    changed_qids: Optional[list[str]] = None,
    count: Optional[int] = None,
    languages: Optional[list[str]] = None,
    custom_description: str | None = None,
    workbook_path: str | None = None,
    interactive: bool | None = None,
    context: dict[str, Any] | None = None,
) -> str:
    """
    Auto-publish survey after push with interactive description prompt.

    Args:
        survey_id: Survey ID to publish
        dimension: Dimension being pushed (items, edf, js, translations, eos, flow)
        skip_publish: If True, skip publishing
        auto_yes: If True, use default description without prompting
        changed_qids: List of changed QIDs (for items/js)
        count: Count of changed items
        languages: List of languages (for translations)

    Returns:
        Published version description

    Raises:
        PublishSkipped: If user explicitly skips publishing
        ValueError: If description validation fails
    """
    # Backwards-compatibility: some callers pass `interactive` rather than `auto_yes`.
    if auto_yes is None and interactive is not None:
        auto_yes = not interactive
    if auto_yes is None:
        auto_yes = False

    if skip_publish:
        raise PublishSkipped("Publishing skipped via --no-publish")

    # Generate default description
    default_desc = _generate_default_description(
        dimension=dimension,
        changed_qids=changed_qids,
        count=count,
        languages=languages,
    )

    # Determine description.
    if custom_description is not None and custom_description.strip():
        validate_publish_description(custom_description)
        description = custom_description.strip()
        print(f"[qsync:{dimension}] Publishing with custom description: {description}")
    elif auto_yes:
        description = default_desc
        print(f"[qsync:{dimension}] Auto-publishing with description: {description}")
    else:
        description = _prompt_publish_description(
            dimension=dimension,
            default_description=default_desc,
        )

    # Publish
    publish_survey_definition(
        survey_id=survey_id,
        description=description,
        published=True,
    )

    print(f"[qsync:{dimension}] Published: {description}")
    return description


def _generate_default_description(
    dimension: DimensionType,
    changed_qids: Optional[list[str]] = None,
    count: Optional[int] = None,
    languages: Optional[list[str]] = None,
) -> str:
    """
    Generate default publish description for a dimension.

    Args:
        dimension: Dimension type
        changed_qids: List of changed QIDs
        count: Count of changes
        languages: List of language codes (for translations)

    Returns:
        Default description string
    """
    if dimension == "items":
        return make_publish_description(
            operation="update items",
            changed_qids=changed_qids or [],
            count=count,
            max_chars=SURVEY_VERSION_DESCRIPTION_MAX_CHARS,
        )

    elif dimension == "js":
        return make_publish_description(
            operation="update JS",
            changed_qids=changed_qids or [],
            count=count,
            max_chars=SURVEY_VERSION_DESCRIPTION_MAX_CHARS,
        )

    elif dimension == "translations":
        if languages:
            lang_list = ",".join(languages[:5])  # Limit to first 5
            if len(languages) > 5:
                lang_list += f",+{len(languages) - 5}"
            return f"qsync: update {lang_list} translations"
        else:
            return "qsync: update translations"

    elif dimension == "eos":
        n = count if count is not None else 0
        return f"qsync: update {n} EOS message(s)"
    elif dimension == "flow":
        n = count if count is not None else 0
        return f"qsync: update survey flow ({n} change(s))"

    else:
        # Fallback
        return f"qsync: update {dimension}"


def _prompt_publish_description(
    dimension: DimensionType,
    default_description: str,
) -> str:
    """
    Prompt user for publish description with interactive editing.

    Args:
        dimension: Dimension type
        default_description: Default description to show

    Returns:
        Final description (user input or default)

    Raises:
        PublishSkipped: If user enters 'skip'
        ValueError: If description exceeds max length
    """
    # Show default and prompt
    print(f"\n[qsync:{dimension}] Publish description:")
    print(f"  Default: {default_description}")
    print(
        "  (Press Enter to use default, type 'skip' to skip publish, or enter custom description)"
    )

    response = input("Description: ").strip()

    # Handle user input
    if not response:
        # Empty = use default
        return default_description

    if response.lower() == "skip":
        # Explicit skip
        raise PublishSkipped("User requested to skip publish")

    # Custom description - validate length
    if len(response) > SURVEY_VERSION_DESCRIPTION_MAX_CHARS:
        raise ValueError(
            f"Description must be <= {SURVEY_VERSION_DESCRIPTION_MAX_CHARS} characters "
            f"(got {len(response)})"
        )

    return response


def validate_publish_description(description: str) -> None:
    """
    Validate publish description length.

    Args:
        description: Description to validate

    Raises:
        ValueError: If description is invalid
    """
    if not description or not description.strip():
        raise ValueError("Description cannot be empty")

    if len(description) > SURVEY_VERSION_DESCRIPTION_MAX_CHARS:
        raise ValueError(
            f"Description must be <= {SURVEY_VERSION_DESCRIPTION_MAX_CHARS} characters "
            f"(got {len(description)})"
        )
