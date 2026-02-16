from __future__ import annotations

from qsync.cli_prolific import (
    MatchSurvey,
    build_match_rows,
    build_match_formula,
    build_qualtrics_form_redirect_url,
    build_prolific_completion_url,
    exact_name_key,
    iter_rows_for_state,
)


def _row_by_study(rows: list[dict[str, str]], study_id: str) -> dict[str, str]:
    for row in rows:
        if row.get("prolific_study_id") == study_id:
            return row
    raise AssertionError(f"missing study row: {study_id}")


def test_build_match_rows_unique_prefix_only() -> None:
    studies = [
        {
            "prolific_study_id": "P1",
            "prolific_internal_name": "Main Newsflow Pilot",
            "prolific_study_name": "Main Newsflow Pilot",
            "completion_code": "AAA111",
        },
        {
            "prolific_study_id": "P2",
            "prolific_internal_name": "Main Newsflow Followup",
            "prolific_study_name": "Main Newsflow Followup",
            "completion_code": "BBB222",
        },
        {
            "prolific_study_id": "P3",
            "prolific_internal_name": "Sports Quick",
            "prolific_study_name": "Sports Quick",
            "completion_code": "CCC333",
        },
    ]
    surveys = [
        MatchSurvey("SV_MAIN_EN", "Main Newsflow English"),
        MatchSurvey("SV_MAIN_NL", "Main Newsflow Dutch"),
        MatchSurvey("SV_SPORTS", "Sports Quick Survey"),
    ]

    rows = build_match_rows(
        studies=studies,
        qualtrics_surveys=surveys,
        qualtrics_base_url="example.qualtrics.com",
        prefix_tokens=2,
    )

    p1 = _row_by_study(rows, "P1")
    p2 = _row_by_study(rows, "P2")
    p3 = _row_by_study(rows, "P3")

    assert p1["state"] == "REVIEW_REQUIRED"
    assert p2["state"] == "REVIEW_REQUIRED"
    assert p1["match_mode"] == "ambiguous"
    assert p2["match_mode"] == "ambiguous"

    assert p3["state"] == "PROPOSED"
    assert p3["qualtrics_survey_id"] == "SV_SPORTS"
    assert p3["match_mode"] in {"prefix_unique", "prefix_exact"}


def test_build_match_rows_expands_prefix_beyond_minimum_when_needed() -> None:
    studies = [
        {
            "prolific_study_id": "P10",
            "prolific_internal_name": "BSKY_main_payout_10_CZ_p",
            "prolific_study_name": "BSKY_main_payout_10_CZ_p",
            "completion_code": "AAA010",
        },
        {
            "prolific_study_id": "P20",
            "prolific_internal_name": "BSKY_main_payout_20_CZ_p",
            "prolific_study_name": "BSKY_main_payout_20_CZ_p",
            "completion_code": "AAA020",
        },
    ]
    surveys = [
        MatchSurvey("SV_P10", "BSKY_main_payout_10_CZ"),
        MatchSurvey("SV_P20", "BSKY_main_payout_20_CZ"),
    ]

    rows = build_match_rows(
        studies=studies,
        qualtrics_surveys=surveys,
        qualtrics_base_url="example.qualtrics.com",
        prefix_tokens=2,
    )

    p10 = _row_by_study(rows, "P10")
    p20 = _row_by_study(rows, "P20")

    assert p10["state"] == "PROPOSED"
    assert p10["qualtrics_survey_id"] == "SV_P10"
    assert p10["match_mode"] == "prefix_exact"
    assert p10["name_prefix_key"] == "bsky main payout 10"

    assert p20["state"] == "PROPOSED"
    assert p20["qualtrics_survey_id"] == "SV_P20"
    assert p20["match_mode"] == "prefix_exact"
    assert p20["name_prefix_key"] == "bsky main payout 20"


def test_build_match_rows_preserves_approved_manual_mapping() -> None:
    studies = [
        {
            "prolific_study_id": "P10",
            "prolific_internal_name": "Alpha Baseline",
            "prolific_study_name": "Alpha Baseline",
            "completion_code": "ZXCV1",
        }
    ]
    surveys = [MatchSurvey("SV_AUTO", "Alpha Baseline Survey")]
    existing = [
        {
            "state": "APPROVED",
            "prolific_study_id": "P10",
            "prolific_internal_name": "Alpha Baseline",
            "qualtrics_survey_id": "SV_MANUAL",
            "qualtrics_survey_name": "Manual Survey",
            "match_mode": "manual",
            "match_confidence": "high",
            "notes": "manual override",
            "desired_prolific_redirect_url": "https://custom.example/redirect",
            "desired_qualtrics_eos_redirect_url": "https://app.prolific.com/submissions/complete?cc=ZXCV1",
        }
    ]

    rows = build_match_rows(
        studies=studies,
        qualtrics_surveys=surveys,
        qualtrics_base_url="example.qualtrics.com",
        prefix_tokens=2,
        existing_rows=existing,
    )

    row = _row_by_study(rows, "P10")
    assert row["state"] == "APPROVED"
    assert row["qualtrics_survey_id"] == "SV_MANUAL"
    assert row["qualtrics_survey_name"] == "Manual Survey"
    assert row["match_mode"] == "manual"
    assert row["notes"] == "manual override"
    assert row["desired_prolific_redirect_url"] == "https://custom.example/redirect"


def test_build_match_rows_recomputes_review_required_rows() -> None:
    studies = [
        {
            "prolific_study_id": "P20",
            "prolific_internal_name": "Beta Main Payout 1 IE p",
            "prolific_study_name": "Beta Main Payout 1 IE p",
            "completion_code": "CODE20",
        }
    ]
    surveys = [MatchSurvey("SV_BETA", "Beta Main Payout 1 IE")]
    existing = [
        {
            "state": "REVIEW_REQUIRED",
            "prolific_study_id": "P20",
            "notes": "Ambiguous prefix 'beta' -> ...",
        }
    ]

    rows = build_match_rows(
        studies=studies,
        qualtrics_surveys=surveys,
        qualtrics_base_url="example.qualtrics.com",
        prefix_tokens=5,
        existing_rows=existing,
    )

    row = _row_by_study(rows, "P20")
    assert row["state"] == "PROPOSED"
    assert row["qualtrics_survey_id"] == "SV_BETA"
    assert "Ambiguous prefix" not in (row.get("notes") or "")


def test_iter_rows_for_state_filters_approved_only() -> None:
    rows = [
        {"state": "APPROVED", "prolific_study_id": "P1"},
        {"state": "PROPOSED", "prolific_study_id": "P2"},
        {"state": "REVIEW_REQUIRED", "prolific_study_id": "P3"},
    ]

    approved = iter_rows_for_state(rows, only_state="APPROVED")
    all_rows = iter_rows_for_state(rows, only_state="ALL")

    assert [r["prolific_study_id"] for r in approved] == ["P1"]
    assert len(all_rows) == 3


def test_redirect_url_builders_use_expected_templates() -> None:
    redirect = build_qualtrics_form_redirect_url("vuamsterdam.eu.qualtrics.com", "SV_123")
    eos = build_prolific_completion_url("ABC123")

    assert "PROLIFIC_PID={{%PROLIFIC_PID%}}" in redirect
    assert "STUDY_ID={{%STUDY_ID%}}" in redirect
    assert "SESSION_ID={{%SESSION_ID%}}" in redirect
    assert redirect.startswith("https://vuamsterdam.eu.qualtrics.com/jfe/form/SV_123?")
    assert eos == "https://app.prolific.com/submissions/complete?cc=ABC123"


def test_exact_name_key_drops_trailing_p_suffix() -> None:
    assert (
        exact_name_key("BSKY_main_post_CZ_p", drop_trailing_p=True)
        == "bsky main post cz"
    )
    assert (
        exact_name_key("BSKY_main_post_CZ_p", drop_trailing_p=False)
        == "bsky main post cz p"
    )


def test_build_match_rows_matches_exact_with_trailing_p_suffix() -> None:
    studies = [
        {
            "prolific_study_id": "PX1",
            "prolific_internal_name": "BSKY_main_post_CZ_p",
            "prolific_study_name": "BSKY_main_post_CZ_p",
            "completion_code": "XYZ001",
        }
    ]
    surveys = [MatchSurvey("SV_POST_CZ", "BSKY_main_post_CZ")]

    rows = build_match_rows(
        studies=studies,
        qualtrics_surveys=surveys,
        qualtrics_base_url="example.qualtrics.com",
        prefix_tokens=5,
    )
    row = _row_by_study(rows, "PX1")
    assert row["state"] == "PROPOSED"
    assert row["match_mode"] == "prefix_exact"
    assert row["qualtrics_survey_id"] == "SV_POST_CZ"
    assert row["match_formula"] == "bsky main post cz"


def test_build_match_rows_can_match_using_study_title() -> None:
    studies = [
        {
            "prolific_study_id": "PT1",
            "prolific_internal_name": "test",
            "prolific_study_name": "BSKY_main_payout_10_IE_p",
            "completion_code": "TT100",
        }
    ]
    surveys = [MatchSurvey("SV_P10IE", "BSKY_main_payout_10_IE")]

    rows = build_match_rows(
        studies=studies,
        qualtrics_surveys=surveys,
        qualtrics_base_url="example.qualtrics.com",
        prefix_tokens=5,
    )
    row = _row_by_study(rows, "PT1")
    assert row["state"] == "PROPOSED"
    assert row["match_mode"] == "prefix_exact"
    assert row["qualtrics_survey_id"] == "SV_P10IE"
    assert row["prolific_study_name"] == "BSKY_main_payout_10_IE_p"


def test_build_match_formula_overlap_only() -> None:
    formula = build_match_formula(
        "BSKY_main_payout_10_CZ_p",
        "BSKY_main_payout_10_CZ",
    )
    assert formula == "bsky main payout 10 cz"
