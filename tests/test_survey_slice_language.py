from __future__ import annotations

import json
from pathlib import Path

from qsync.survey_slice_language import (
    compute_slice_coverage,
    resolve_keep_languages,
    slice_qsf_to_language,
    write_coverage_report,
    write_slice_manifest,
)


def _qsf_payload(*, available_languages, meta_translations=None, question_payload=None):
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
                    "AvailableLanguages": available_languages,
                    "SurveyTitle": "Base title",
                    "MetaDataTranslations": meta_translations or {},
                },
            },
            {
                "Element": "SQ",
                "PrimaryAttribute": "QID1",
                "Payload": question_payload
                or {
                    "QuestionID": "QID1",
                    "QuestionText": "Hello",
                    "Choices": {"1": {"Display": "Yes"}},
                    "ChoiceOrder": ["1"],
                    "Answers": {"1": {"Display": "Col X"}},
                    "AnswerOrder": ["1"],
                    "Labels": {"1": {"Display": "Label A"}},
                    "SubQuestions": {"1": {"Description": "Row A"}},
                    "ChoiceGroups": {"1": {"Description": "Group 1"}},
                    "Language": {
                        "DE": {
                            "QuestionText": "Hallo",
                            "Choices": {"1": {"Display": "Ja"}},
                            "Answers": {"1": {"Display": "Spalte X"}},
                            "Labels": {"1": {"Display": "Etikett A"}},
                            "SubQuestions": {"1": {"Description": "Reihe A"}},
                            "ChoiceGroups": {"1": {"Description": "Gruppe 1"}},
                        },
                        "FR": {
                            "QuestionText": "Bonjour",
                            "Choices": {"1": {"Display": "Oui"}},
                        },
                    },
                },
            },
        ],
    }


def test_compute_slice_coverage_reports_missing_required_keys() -> None:
    qsf = _qsf_payload(
        available_languages=["EN", "DE"],
        question_payload={
            "QuestionID": "QID1",
            "QuestionText": "Hello",
            "Choices": {"1": {"Display": "Yes"}},
            "Language": {"DE": {"Choices": {"1": {"Display": "Ja"}}}},  # Missing text
        },
    )

    report = compute_slice_coverage(qsf, target_language="DE")
    assert report.base_language == "EN"
    assert report.target_language == "DE"
    assert report.missing_required_total >= 1
    assert "QID1_QuestionText" in report.missing_required


def test_compute_slice_coverage_passes_when_complete() -> None:
    qsf = _qsf_payload(
        available_languages=["EN", "DE"],
        meta_translations={
            "DE": {"SurveyTitle": "Titel", "SurveyMetaDescription": "Beschreibung"}
        },
    )

    report = compute_slice_coverage(qsf, target_language="DE")
    assert report.missing_required_total == 0
    assert report.required_total > 0


def test_slice_qsf_target_only_rebases_and_strips_language_blocks() -> None:
    qsf = _qsf_payload(
        available_languages=["EN", "DE", "FR"],
        meta_translations={
            "DE": {"SurveyTitle": "Titel", "SurveyMetaDescription": "Beschreibung"},
            "FR": {"SurveyTitle": "Titre"},
        },
    )

    result = slice_qsf_to_language(qsf, target_language="DE", kept_languages=["DE"])
    assert result.base_language_before == "EN"
    assert result.base_language_after == "DE"
    assert result.kept_languages == ["DE"]

    so = qsf["SurveyElements"][0]["Payload"]
    assert so["SurveyLanguage"] == "DE"
    assert so["AvailableLanguages"] == ["DE"]
    assert so.get("MetaDataTranslations") == {}
    assert so.get("SurveyTitle") == "Titel"
    assert so.get("SurveyMetaDescription") == "Beschreibung"
    assert qsf["SurveyEntry"]["SurveyLanguage"] == "DE"
    assert qsf["SurveyEntry"]["SurveyDescription"] == "Base description"

    q1 = qsf["SurveyElements"][1]["Payload"]
    assert q1["QuestionText"] == "Hallo"
    assert q1["Choices"]["1"]["Display"] == "Ja"
    assert q1["Answers"]["1"]["Display"] == "Spalte X"
    assert q1["Labels"]["1"]["Display"] == "Etikett A"
    assert q1["SubQuestions"]["1"]["Description"] == "Reihe A"
    assert q1["ChoiceGroups"]["1"]["Description"] == "Gruppe 1"
    assert set((q1.get("Language") or {}).keys()) == {"DE"}


def test_slice_qsf_keep_all_materializes_old_base_non_destructively() -> None:
    qsf = _qsf_payload(
        available_languages={"EN": True, "DE": True, "FR": True},
        meta_translations={
            "DE": {"SurveyTitle": "Titel", "SurveyMetaDescription": "Beschreibung"},
            "FR": {"SurveyTitle": "Titre"},
        },
        question_payload={
            "QuestionID": "QID1",
            "QuestionText": "Hello",
            "Choices": {"1": {"Display": "Yes"}},
            "SubQuestions": {"1": {"Description": "Row A"}},
            "ChoiceGroups": {"1": {"Description": "Group 1"}},
            "Language": {
                "EN": {"QuestionText": "Existing EN"},  # should not be overwritten
                "DE": {
                    "QuestionText": "Hallo",
                    "Choices": {"1": {"Display": "Ja"}},
                    "SubQuestions": {"1": {"Description": "Reihe A"}},
                    "ChoiceGroups": {"1": {"Description": "Gruppe 1"}},
                },
                "FR": {"QuestionText": "Bonjour"},
            },
        },
    )

    result = slice_qsf_to_language(
        qsf,
        target_language="DE",
        kept_languages=["DE", "EN", "FR"],
    )
    assert result.warnings

    so = qsf["SurveyElements"][0]["Payload"]
    assert set(so["AvailableLanguages"].keys()) == {"DE", "EN", "FR"}
    assert so["SurveyLanguage"] == "DE"

    meta = so.get("MetaDataTranslations") or {}
    assert "DE" not in meta  # promoted to base
    assert "FR" in meta
    assert "EN" in meta  # materialized from old base

    q1 = qsf["SurveyElements"][1]["Payload"]
    assert q1["QuestionText"] == "Hallo"
    assert q1["Choices"]["1"]["Display"] == "Ja"
    assert q1["SubQuestions"]["1"]["Description"] == "Reihe A"
    assert q1["ChoiceGroups"]["1"]["Description"] == "Gruppe 1"
    assert "DE" not in (q1.get("Language") or {})
    assert q1["Language"]["EN"]["QuestionText"] == "Existing EN"


def test_slice_qsf_target_equals_base_target_only_prunes_languages() -> None:
    qsf = _qsf_payload(
        available_languages=["EN", "DE"],
        meta_translations={"DE": {"SurveyTitle": "Titel"}},
    )
    result = slice_qsf_to_language(qsf, target_language="EN", kept_languages=["EN"])
    assert result.base_language_before == "EN"
    assert result.base_language_after == "EN"
    assert qsf["SurveyElements"][0]["Payload"]["AvailableLanguages"] == ["EN"]
    q1 = qsf["SurveyElements"][1]["Payload"]
    assert q1["QuestionText"] == "Hello"
    assert "Language" not in q1


def test_slice_qsf_target_equals_base_keep_de_keeps_language_blocks() -> None:
    qsf = _qsf_payload(available_languages=["EN", "DE"])
    result = slice_qsf_to_language(
        qsf,
        target_language="EN",
        kept_languages=["EN", "DE"],
    )
    assert result.warnings == []
    so = qsf["SurveyElements"][0]["Payload"]
    assert so["SurveyLanguage"] == "EN"
    assert so["AvailableLanguages"] == ["EN", "DE"]
    q1 = qsf["SurveyElements"][1]["Payload"]
    assert q1["QuestionText"] == "Hello"
    assert "DE" in (q1.get("Language") or {})


def test_resolve_keep_languages_modes() -> None:
    enabled = ["EN", "DE", "FR"]
    assert resolve_keep_languages(
        enabled, target_language="DE", base_language="EN", keep_languages_raw="target-only"
    ) == ["DE"]
    assert resolve_keep_languages(
        enabled, target_language="DE", base_language="EN", keep_languages_raw="all"
    )[0] == "DE"
    keep = resolve_keep_languages(
        enabled, target_language="DE", base_language="EN", keep_languages_raw="FR"
    )
    assert set(keep) == {"DE", "EN", "FR"}


def test_write_reports_and_manifest(tmp_path: Path) -> None:
    qsf = _qsf_payload(
        available_languages=["EN", "DE"],
        meta_translations={"DE": {"SurveyTitle": "Titel"}},
    )
    report = compute_slice_coverage(qsf, target_language="DE")

    coverage_path = write_coverage_report(
        tmp_path,
        source_survey_id="SV_SRC",
        target_language="DE",
        report=report,
    )
    assert coverage_path.exists()

    manifest_path = write_slice_manifest(
        tmp_path,
        source_survey_id="SV_SRC",
        source_survey_name="Source Survey",
        source_base_language="EN",
        target_language="DE",
        new_survey_id="SV_NEW",
        new_survey_name="New Survey",
        keep_languages_mode="target-only",
        kept_languages=["DE"],
        allow_incomplete=False,
        coverage_report_path=coverage_path,
        report=report,
        qsf_sha256="deadbeef",
        qsync_version="0.0.0",
    )
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["source_survey_id"] == "SV_SRC"
    assert data["source_survey_name"] == "Source Survey"
    assert data["source_base_language"] == "EN"
    assert data["new_survey_id"] == "SV_NEW"
    assert data["target_language"] == "DE"
    assert data["keep_languages_mode"] == "target-only"
    assert data["kept_languages"] == ["DE"]
    assert data["allow_incomplete"] is False
    assert data["coverage_report_path"] == str(coverage_path)
    assert data["qsf_sha256"] == "deadbeef"
    assert data["qsync_version"] == "0.0.0"
    assert "coverage" in data


def test_compute_slice_coverage_ignores_trash_block_qids() -> None:
    qsf = {
        "SurveyEntry": {
            "SurveyID": "SV_SRC",
            "SurveyName": "Source Survey",
            "SurveyLanguage": "EN",
        },
        "SurveyElements": [
            {
                "Element": "SO",
                "PrimaryAttribute": "Survey Options",
                "Payload": {
                    "SurveyLanguage": "EN",
                    "AvailableLanguages": ["EN", "DE"],
                    "SurveyTitle": "Base title",
                    "MetaDataTranslations": {
                        "DE": {
                            "SurveyTitle": "Titel",
                            "SurveyMetaDescription": "Beschreibung",
                        }
                    },
                },
            },
            {
                "Element": "BL",
                "PrimaryAttribute": "Survey Blocks",
                "Payload": {
                    "0": {
                        "ID": "BL_MAIN",
                        "Type": "Default",
                        "BlockElements": [
                            {"Type": "Question", "QuestionID": "QID1"}
                        ],
                    },
                    "1": {
                        "ID": "BL_TRASH",
                        "Type": "Trash",
                        "BlockElements": [
                            {"Type": "Question", "QuestionID": "QID2"}
                        ],
                    },
                },
            },
            {
                "Element": "FL",
                "PrimaryAttribute": "Survey Flow",
                "Payload": {
                    "Flow": [
                        {"Type": "Block", "ID": "BL_MAIN"},
                    ]
                },
            },
            {
                "Element": "SQ",
                "PrimaryAttribute": "QID1",
                "Payload": {
                    "QuestionID": "QID1",
                    "QuestionText": "Hello",
                    "Language": {"DE": {"QuestionText": "Hallo"}},
                },
            },
            {
                "Element": "SQ",
                "PrimaryAttribute": "QID2",
                "Payload": {
                    "QuestionID": "QID2",
                    "QuestionText": "Unused",
                },
            },
        ],
    }

    report = compute_slice_coverage(qsf, target_language="DE")
    assert "QID2_QuestionText" not in report.missing_required
    assert report.inactive_qids_total == 1
