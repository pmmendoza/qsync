from __future__ import annotations


from ..translations_utils import normalize_language_code, normalize_language_list


def _survey_result(payload: dict) -> dict:
    if isinstance(payload.get("result"), dict):
        return payload["result"]
    return payload


def _survey_options(payload: dict) -> dict:
    return _survey_result(payload).get("SurveyOptions") or {}


def get_base_language(payload: dict) -> str:
    options = _survey_options(payload)
    return normalize_language_code(options.get("SurveyLanguage") or "")


def list_enabled_languages(payload: dict) -> list[str]:
    options = _survey_options(payload)
    available = options.get("AvailableLanguages")
    if isinstance(available, dict):
        return normalize_language_list(list(available.keys()))
    if isinstance(available, list):
        return normalize_language_list([str(lang) for lang in available])
    return []


def _ensure_language_block(question: dict, language: str) -> dict:
    lang = normalize_language_code(language)
    if not lang:
        return {}
    if "Language" not in question or not isinstance(question.get("Language"), dict):
        question["Language"] = {}
    language_block = question["Language"].get(lang)
    if not isinstance(language_block, dict):
        language_block = {}
        question["Language"][lang] = language_block
    return language_block


def _ensure_section(question: dict, language: str, section: str) -> dict:
    language_block = _ensure_language_block(question, language)
    if section not in language_block or not isinstance(
        language_block.get(section), dict
    ):
        language_block[section] = {}
    return language_block[section]


def read_question_text(question: dict, language: str) -> str | None:
    lang = normalize_language_code(language)
    language_block = question.get("Language")
    if not isinstance(language_block, dict):
        return None
    block = language_block.get(lang)
    if not isinstance(block, dict):
        return None
    value = block.get("QuestionText")
    return str(value) if value is not None else None


def write_question_text(question: dict, language: str, value: str) -> None:
    block = _ensure_language_block(question, language)
    block["QuestionText"] = value


def read_choice_display(question: dict, language: str, choice_id: str) -> str | None:
    lang = normalize_language_code(language)
    language_block = question.get("Language")
    if not isinstance(language_block, dict):
        return None
    lang_block = language_block.get(lang)
    if not isinstance(lang_block, dict):
        return None
    choices = (
        lang_block.get("Choices", {})
        if isinstance(lang_block.get("Choices"), dict)
        else {}
    )
    value = choices.get(str(choice_id), {}).get("Display")
    return str(value) if value is not None else None


def write_choice_display(
    question: dict, language: str, choice_id: str, value: str
) -> None:
    choices = _ensure_section(question, language, "Choices")
    entry = choices.get(str(choice_id)) or {}
    entry["Display"] = value
    choices[str(choice_id)] = entry


def read_answer_display(question: dict, language: str, answer_id: str) -> str | None:
    lang = normalize_language_code(language)
    language_block = question.get("Language")
    if not isinstance(language_block, dict):
        return None
    lang_block = language_block.get(lang)
    if not isinstance(lang_block, dict):
        return None
    answers = (
        lang_block.get("Answers", {})
        if isinstance(lang_block.get("Answers"), dict)
        else {}
    )
    value = answers.get(str(answer_id), {}).get("Display")
    return str(value) if value is not None else None


def write_answer_display(
    question: dict, language: str, answer_id: str, value: str
) -> None:
    answers = _ensure_section(question, language, "Answers")
    entry = answers.get(str(answer_id)) or {}
    entry["Display"] = value
    answers[str(answer_id)] = entry


def read_label_display(question: dict, language: str, label_id: str) -> str | None:
    lang = normalize_language_code(language)
    language_block = question.get("Language")
    if not isinstance(language_block, dict):
        return None
    lang_block = language_block.get(lang)
    if not isinstance(lang_block, dict):
        return None
    labels = (
        lang_block.get("Labels", {})
        if isinstance(lang_block.get("Labels"), dict)
        else {}
    )
    value = labels.get(str(label_id), {}).get("Display")
    return str(value) if value is not None else None


def write_label_display(
    question: dict, language: str, label_id: str, value: str
) -> None:
    labels = _ensure_section(question, language, "Labels")
    entry = labels.get(str(label_id)) or {}
    entry["Display"] = value
    labels[str(label_id)] = entry
