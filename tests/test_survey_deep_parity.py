from __future__ import annotations

from qsync.survey_deep_parity import compare_survey_definition_deep_parity


def _base_def() -> dict:
    return {
        # Volatile identity/account metadata
        "SurveyID": "SV_SOURCE",
        "SurveyName": "Source Survey",
        "SurveyStatus": "Active",
        "BrandID": "BRAND_A",
        "BrandBaseURL": "https://example.qualtrics.com",
        "CreatorID": "UR_1",
        "OwnerID": "UR_2",
        "DivisionID": "DV_1",
        "ProjectInfo": {"ProjectID": "PRJ_1"},
        "LastModified": "2026-02-14T00:00:00Z",
        "LastAccessed": "2026-02-14T00:00:00Z",
        "LastActivated": "2026-02-14T00:00:00Z",
        # Response set volatility
        "ResponseSets": {"RS_123": "Default Response Set"},
        "SurveyOptions": {
            "ActiveResponseSet": "RS_123",
            "Skin": "skin_1",
            "SkinLibrary": "lib_1",
        },
        "Questions": {
            "QID1": {
                "QuestionID": "QID1",
                "DataExportTag": "tag_1",
                "QuestionText": "Hello",
                "QuestionText_Unsafe": "Hello",
            }
        },
        "Blocks": {
            "BL_1": {
                "Type": "Default",
                "ID": "BL_1",
                "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
            }
        },
        "SurveyFlow": {
            "Flow": [{"Type": "Block", "ID": "BL_1"}],
        },
    }


def test_deep_parity_ignores_cross_account_volatile_fields() -> None:
    a = _base_def()
    b = _base_def()

    # By-definition drift across accounts
    b["SurveyID"] = "SV_TARGET"
    b["SurveyName"] = "Target Survey"
    b["SurveyStatus"] = "Inactive"
    b["BrandID"] = "BRAND_B"
    b["LastModified"] = "2026-02-15T00:00:00Z"

    # ResponseSet IDs regenerate; names should still compare equal.
    b["ResponseSets"] = {"RS_999": "Default Response Set"}
    b["SurveyOptions"]["ActiveResponseSet"] = "RS_999"

    report = compare_survey_definition_deep_parity(a, b)
    assert report.ok


def test_deep_parity_fails_on_theme_drift() -> None:
    a = _base_def()
    b = _base_def()
    b["SurveyOptions"]["Skin"] = "skin_2"

    report = compare_survey_definition_deep_parity(a, b)
    assert not report.ok
    assert report.section_counts.get("SurveyOptions", 0) > 0


def test_deep_parity_fails_when_unsafe_differs_from_safe() -> None:
    a = _base_def()
    b = _base_def()
    b["Questions"]["QID1"]["QuestionText_Unsafe"] = "Hello (unsafe)"

    report = compare_survey_definition_deep_parity(a, b)
    assert not report.ok
    assert report.section_counts.get("Questions", 0) > 0


def test_deep_parity_flow_changes_are_reported() -> None:
    a = _base_def()
    b = _base_def()
    b["SurveyFlow"]["Flow"].append({"Type": "Block", "ID": "BL_2"})

    report = compare_survey_definition_deep_parity(a, b)
    assert not report.ok
    assert report.flow_changes, "Expected semantic flow changes on mismatch"


def _split_manifest() -> dict:
    return {
        "manifest_version": 2,
        "source_survey_id": "SV_SOURCE",
        "target_survey_id": "SV_SPLIT",
        "target_language": "DE",
        "keep_languages_policy": "target-only",
    }


def _canonical_for_split() -> dict:
    return {
        "SurveyID": "SV_SOURCE",
        "SurveyOptions": {
            "SurveyLanguage": "EN",
            "AvailableLanguages": {"EN": [], "DE": []},
        },
        "Questions": {
            "QID1": {
                "QuestionID": "QID1",
                "QuestionText": "Hello",
                "Choices": {"1": {"Display": "Yes"}},
                "Language": {
                    "DE": {
                        "QuestionText": "Hallo",
                        "Choices": {"1": {"Display": "Ja"}},
                    }
                },
            }
        },
        "SurveyFlow": {
            "Flow": [
                {
                    "Type": "WebService",
                    "FlowID": "FL_WS",
                    "URL": "https://api.example.com/collect",
                    "Method": "POST",
                }
            ]
        },
    }


def _split_for_de() -> dict:
    return {
        "SurveyID": "SV_SPLIT",
        "SurveyOptions": {
            "SurveyLanguage": "DE",
            "AvailableLanguages": {"DE": []},
        },
        "Questions": {
            "QID1": {
                "QuestionID": "QID1",
                "QuestionText": "Hallo",
                "Choices": {"1": {"Display": "Ja"}},
                "Language": {},
            }
        },
        "SurveyFlow": {
            "Flow": [
                {
                    "Type": "WebService",
                    "FlowID": "FL_WS",
                    "URL": "https://api.example.com/collect",
                    "Method": "POST",
                }
            ]
        },
    }


def test_split_profile_requires_manifest() -> None:
    report = compare_survey_definition_deep_parity(
        _canonical_for_split(),
        _split_for_de(),
        profile="split",
    )
    assert not report.ok
    assert any("manifest" in item for item in report.hard_fail_paths)


def test_split_profile_allows_translation_drift_with_manifest() -> None:
    report = compare_survey_definition_deep_parity(
        _canonical_for_split(),
        _split_for_de(),
        profile="split",
        manifest=_split_manifest(),
    )
    assert report.ok
    assert report.gate_results.get("translation") is True
    assert report.gate_results.get("operational_policy") is True
    assert report.gate_results.get("structural") is True


def test_split_profile_hard_fails_unknown_path() -> None:
    canonical = _canonical_for_split()
    split = _split_for_de()
    split["SurveyOptions"]["Skin"] = "skin_2"
    report = compare_survey_definition_deep_parity(
        canonical,
        split,
        profile="split",
        manifest=_split_manifest(),
    )
    assert not report.ok
    assert any("SurveyOptions.Skin" in item for item in report.hard_fail_paths)


def test_split_profile_detects_translation_gate_mismatch() -> None:
    canonical = _canonical_for_split()
    split = _split_for_de()
    split["Questions"]["QID1"]["QuestionText"] = "Guten Tag"
    report = compare_survey_definition_deep_parity(
        canonical,
        split,
        profile="split",
        manifest=_split_manifest(),
    )
    assert not report.ok
    assert report.gate_results.get("translation") is False
    assert any("translation gate failed" in item for item in report.hard_fail_paths)


def test_split_profile_webservice_aliases_are_normalized() -> None:
    canonical = _canonical_for_split()
    split = _split_for_de()
    split["SurveyFlow"]["Flow"][0].pop("URL", None)
    split["SurveyFlow"]["Flow"][0].pop("Method", None)
    split["SurveyFlow"]["Flow"][0]["RequestURL"] = "https://api.example.com/collect"
    split["SurveyFlow"]["Flow"][0]["RequestType"] = "POST"

    report = compare_survey_definition_deep_parity(
        canonical,
        split,
        profile="split",
        manifest=_split_manifest(),
    )
    assert report.ok


def test_split_profile_available_languages_false_markers_are_filtered() -> None:
    canonical = _canonical_for_split()
    split = _split_for_de()
    split["SurveyOptions"]["AvailableLanguages"] = {
        "DE": [],
        "EN": False,
        "FR": "0",
    }
    report = compare_survey_definition_deep_parity(
        canonical,
        split,
        profile="split",
        manifest=_split_manifest(),
    )
    assert report.ok


def test_split_profile_scopes_translation_gate_to_active_qids() -> None:
    canonical = {
        "SurveyID": "SV_SOURCE",
        "SurveyOptions": {
            "SurveyLanguage": "EN",
            "AvailableLanguages": {"EN": [], "DE": []},
        },
        "Questions": {
            "QID1": {
                "QuestionID": "QID1",
                "QuestionText": "Hello",
                "Language": {"DE": {"QuestionText": "Hallo"}},
            },
            "QID_UNUSED": {
                "QuestionID": "QID_UNUSED",
                "QuestionText": "Unused base",
                "Language": {"DE": {"QuestionText": "Ungenutzt"}},
            },
        },
        "Blocks": {
            "BL_1": {
                "Type": "Standard",
                "ID": "BL_1",
                "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
            },
            "BL_TRASH": {
                "Type": "Trash",
                "ID": "BL_TRASH",
                "BlockElements": [{"Type": "Question", "QuestionID": "QID_UNUSED"}],
            },
        },
        "SurveyFlow": {"Flow": [{"Type": "Block", "ID": "BL_1"}]},
    }
    split = {
        "SurveyID": "SV_SPLIT",
        "SurveyOptions": {
            "SurveyLanguage": "DE",
            "AvailableLanguages": {"DE": []},
        },
        "Questions": {
            "QID1": {"QuestionID": "QID1", "QuestionText": "Hallo"},
            "QID_UNUSED": {"QuestionID": "QID_UNUSED", "QuestionText": "DRIFT"},
        },
        "Blocks": canonical["Blocks"],
        "SurveyFlow": canonical["SurveyFlow"],
    }

    report = compare_survey_definition_deep_parity(
        canonical,
        split,
        profile="split",
        manifest=_split_manifest(),
    )
    assert report.ok


def test_split_profile_language_policy_gate_fails_survey_language_mismatch() -> None:
    canonical = _canonical_for_split()
    split = _split_for_de()
    split["SurveyOptions"]["SurveyLanguage"] = "FR"

    report = compare_survey_definition_deep_parity(
        canonical,
        split,
        profile="split",
        manifest=_split_manifest(),
    )

    assert not report.ok
    assert report.gate_results.get("language_policy") is False
    assert any("language policy gate failed" in item for item in report.hard_fail_paths)
