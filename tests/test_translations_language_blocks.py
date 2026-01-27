from __future__ import annotations

from qsync.dimensions.translations_language_blocks import (
    get_base_language,
    list_enabled_languages,
    read_answer_display,
    read_choice_display,
    read_label_display,
    read_question_text,
    write_answer_display,
    write_choice_display,
    write_label_display,
    write_question_text,
)


def test_language_options_helpers() -> None:
    payload = {
        "result": {
            "SurveyOptions": {
                "SurveyLanguage": "en",
                "AvailableLanguages": {"EN": True, "FR": True},
            }
        }
    }

    assert get_base_language(payload) == "EN"
    assert list_enabled_languages(payload) == ["EN", "FR"]

    payload["result"]["SurveyOptions"]["AvailableLanguages"] = ["en", "fr"]
    assert list_enabled_languages(payload) == ["EN", "FR"]


def test_language_block_read_write_helpers() -> None:
    question: dict = {}

    write_question_text(question, "fr", "Question FR")
    write_choice_display(question, "fr", "1", "Choix 1")
    write_answer_display(question, "fr", "1", "Réponse 1")
    write_label_display(question, "fr", "1", "Label 1")

    assert read_question_text(question, "FR") == "Question FR"
    assert read_choice_display(question, "FR", "1") == "Choix 1"
    assert read_answer_display(question, "FR", "1") == "Réponse 1"
    assert read_label_display(question, "FR", "1") == "Label 1"

    assert question["Language"]["FR"]["QuestionText"] == "Question FR"
    assert question["Language"]["FR"]["Choices"]["1"]["Display"] == "Choix 1"
    assert question["Language"]["FR"]["Answers"]["1"]["Display"] == "Réponse 1"
    assert question["Language"]["FR"]["Labels"]["1"]["Display"] == "Label 1"


def test_read_helpers_ignore_non_dict_language_blocks() -> None:
    question = {"Language": ["EN", "FR"]}

    assert read_question_text(question, "FR") is None
    assert read_choice_display(question, "FR", "1") is None
    assert read_answer_display(question, "FR", "1") is None
    assert read_label_display(question, "FR", "1") is None
