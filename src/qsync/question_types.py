"""Shared question-type classification helpers."""

from __future__ import annotations

from typing import Any, Mapping

# System/technical question types that should not be treated as editable item
# content in workbook surfaces.
SYSTEM_QUESTION_TYPES = frozenset({"timing", "meta", "metainfo", "captcha"})


def normalize_question_type(value: object) -> str:
    """Return a normalized, case-insensitive question type token."""

    return str(value or "").strip().lower()


def is_system_question_type(question_type: object) -> bool:
    """Return True when the provided question type is system/technical."""

    return normalize_question_type(question_type) in SYSTEM_QUESTION_TYPES


def is_system_question(question: Mapping[str, Any] | None) -> bool:
    """Return True when a question payload is system/technical."""

    if not isinstance(question, Mapping):
        return False
    return is_system_question_type(question.get("QuestionType"))

