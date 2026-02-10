from __future__ import annotations

import argparse
from types import SimpleNamespace

from openpyxl import Workbook


def _survey_payload(*, base: str, available: list[str]) -> dict:
    return {
        "result": {
            "SurveyID": "SV_TEST",
            "SurveyOptions": {
                "SurveyLanguage": base,
                "AvailableLanguages": available,
            },
            "Questions": {},
            "Blocks": {},
            "SurveyFlow": {"Flow": []},
        }
    }


def _write_workbook_with_question_columns(path, text_columns: list[str]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Questions"
    ws.append(["SurveyID", "QID", *text_columns])
    ws.append(["SV_TEST", "QID1", *(["example"] * len(text_columns))])
    wb.save(path)


def _slice_qsf_payload() -> dict:
    return {
        "SurveyEntry": {
            "SurveyID": "SV_SRC",
            "SurveyName": "Source Survey",
            "SurveyLanguage": "EN",
            "SurveyDescription": "Base description",
        },
        "SurveyElements": [
            {
                "Element": "SO",
                "PrimaryAttribute": "Survey Options",
                "Payload": {
                    "SurveyLanguage": "EN",
                    "AvailableLanguages": ["EN", "FR"],
                    "SurveyTitle": "Base title",
                    "MetaDataTranslations": {
                        "FR": {
                            "SurveyTitle": "Titre FR",
                            "SurveyMetaDescription": "Description FR",
                        }
                    },
                },
            },
            {
                "Element": "SQ",
                "PrimaryAttribute": "QID1",
                "Payload": {
                    "QuestionID": "QID1",
                    "QuestionText": "Hello",
                    "Choices": {"1": {"Display": "Yes"}},
                    "ChoiceOrder": ["1"],
                    "Answers": {"1": {"Display": "Col X"}},
                    "AnswerOrder": ["1"],
                    "Labels": {"1": {"Display": "Label A"}},
                    "Language": {
                        "FR": {
                            "QuestionText": "Bonjour",
                            "Choices": {"1": {"Display": "Oui"}},
                            "Answers": {"1": {"Display": "Colonne X"}},
                            "Labels": {"1": {"Display": "Libelle A"}},
                        }
                    },
                },
            },
        ],
    }


def test_translations_missing_workbook_is_non_blocking(monkeypatch, tmp_path):
    import qsync.dimensions.translations as translations_dimension

    missing = tmp_path / "missing.xlsx"
    monkeypatch.setattr(
        translations_dimension.WorkbookResolver,
        "default_path",
        lambda self, _survey_id: missing,
    )

    result = translations_dimension.detect_unstaged_changes("SV_TEST")
    assert result.status_kind == "none"
    assert result.error_detail is None
    assert result.warning_detail is not None
    assert "qsync items pull --survey-id SV_TEST" in result.warning_detail
    assert result.safe_to_autofix is True


def test_resolve_stage_languages_prefers_enabled_non_base(monkeypatch, tmp_path):
    from qsync.dimensions.translations_core import _resolve_stage_languages

    workbook_path = tmp_path / "stale.xlsx"
    _write_workbook_with_question_columns(
        workbook_path,
        ["Text_en_MD", "Text_en_IsHTML", "Text_de_MD", "Text_de_IsHTML"],
    )
    payload = _survey_payload(base="EN", available=["EN", "FR"])

    langs = _resolve_stage_languages(
        "SV_TEST",
        payload,
        workbook_path,
        explicit_languages=None,
        allow_empty=False,
    )
    assert langs == ["FR"]


def test_translations_detect_warns_for_workbook_only_languages(monkeypatch, tmp_path):
    import qsync.dimensions.translations as translations_dimension

    workbook_path = tmp_path / "workbook.xlsx"
    _write_workbook_with_question_columns(
        workbook_path,
        ["Text_en_MD", "Text_en_IsHTML"],
    )

    monkeypatch.setattr(
        translations_dimension.WorkbookResolver,
        "default_path",
        lambda self, _survey_id: workbook_path,
    )
    monkeypatch.setattr(
        translations_dimension,
        "load_cached_survey",
        lambda _survey_id: SimpleNamespace(
            payload=_survey_payload(base="FR", available=["FR"])
        ),
    )

    result = translations_dimension.detect_unstaged_changes("SV_TEST")
    assert result.status_kind == "none"
    assert result.has_changes is False
    assert result.warning_detail is not None
    assert "not enabled online: EN" in result.warning_detail


def test_init_survey_to_excel_preserves_explicit_empty_languages(monkeypatch, tmp_path):
    import qsync.dimensions.items_core as items_core
    import qsync.translations as translations

    captured: dict[str, object] = {}
    payload = _survey_payload(base="FR", available=["FR"])

    monkeypatch.setattr(
        items_core,
        "check_drift_fn",
        lambda *_args, **_kwargs: SimpleNamespace(has_drift=False),
    )
    monkeypatch.setattr(
        items_core,
        "refresh_survey_cache",
        lambda _survey_id: (SimpleNamespace(payload=payload), False),
    )
    monkeypatch.setattr(
        translations,
        "list_enabled_languages",
        lambda _survey_id: (_ for _ in ()).throw(
            AssertionError("Auto-detect should not run for explicit empty language list")
        ),
    )

    def _capture_init(_survey_id, _payload, _xlsx_path, *, languages=None):
        captured["languages"] = languages

    monkeypatch.setattr(items_core.excel_io, "init_workbook_from_survey", _capture_init)
    items_core.init_survey_to_excel("SV_TEST", tmp_path / "out.xlsx", languages=[])
    assert captured["languages"] == []


def test_sync_translation_autofix_passes_explicit_empty_languages(monkeypatch, tmp_path):
    import qsync.sync_core as sync_core
    import qsync.sync_orchestrator as orchestrator
    import qsync.workbook_resolver as workbook_resolver
    import qsync.qualtrics_client as qualtrics_client

    captured: dict[str, object] = {}
    payload = _survey_payload(base="FR", available=["FR"])

    monkeypatch.setattr(
        workbook_resolver.WorkbookResolver,
        "resolve",
        lambda self, _survey_id: tmp_path / "autofix.xlsx",
    )
    monkeypatch.setattr(
        qualtrics_client,
        "load_cached_survey",
        lambda _survey_id: SimpleNamespace(payload=payload),
    )

    def _capture_init(_survey_id, _xlsx_path, *, languages=None):
        captured["languages"] = languages

    monkeypatch.setattr(sync_core, "init_survey_to_excel", _capture_init)
    orchestrator._run_autofix("translations", "SV_TEST")
    assert captured["languages"] == []


def test_slice_language_next_steps_include_items_pull(monkeypatch, tmp_path):
    import qsync.cli as cli
    import qsync.cli_survey as cli_survey
    import qsync.dimensions.translations_core as translations_core
    import qsync.terminal_output as terminal_output

    info_messages: list[str] = []
    qsf = _slice_qsf_payload()

    monkeypatch.setattr(cli_survey, "_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(cli_survey, "get_client_config", lambda: ("example.qualtrics.com", {}))
    monkeypatch.setattr(
        cli,
        "_prompt_for_survey_id_if_needed",
        lambda _sid, allow_all_surveys=True: "SV_SRC",
    )
    monkeypatch.setattr(
        translations_core,
        "list_enabled_languages",
        lambda _survey_id: ["EN", "FR"],
    )
    monkeypatch.setattr(
        cli_survey,
        "fetch_survey_definition",
        lambda _base, _headers, _source_id, fmt="qsf": qsf,
    )
    monkeypatch.setattr(cli_survey, "ensure_unique_survey_name", lambda *_a, **_k: None)
    monkeypatch.setattr(
        cli_survey,
        "upload_qsf_to_account",
        lambda *_a, **_k: "SV_NEW",
    )
    monkeypatch.setattr(terminal_output, "header", lambda *_a, **_k: None)
    monkeypatch.setattr(terminal_output, "success", lambda *_a, **_k: None)
    monkeypatch.setattr(terminal_output, "warn", lambda *_a, **_k: None)
    monkeypatch.setattr(terminal_output, "dim", lambda *_a, **_k: None)
    monkeypatch.setattr(terminal_output, "prompt_yes_no", lambda *_a, **_k: True)
    monkeypatch.setattr(terminal_output, "log_confirmation", lambda *_a, **_k: None)
    monkeypatch.setattr(
        terminal_output,
        "error",
        lambda _prefix, message: (_ for _ in ()).throw(AssertionError(message)),
    )

    def _capture_info(_prefix, message):
        info_messages.append(message)

    monkeypatch.setattr(terminal_output, "info", _capture_info)

    args = argparse.Namespace(
        source_survey_id="SV_SRC",
        language="FR",
        languages=None,
        name=None,
        keep_languages="target-only",
        allow_incomplete=False,
        allow_fallback=False,
        no_flow_text=False,
        dry_run=False,
        verify_parity=False,
        yes=True,
        force_duplicate=True,
    )

    cli_survey.handle_slice_language(args)
    assert any("qsync items pull --survey-id SV_NEW" in msg for msg in info_messages)
    assert any(
        "qsync translations pull --survey-id SV_NEW" in msg for msg in info_messages
    )
