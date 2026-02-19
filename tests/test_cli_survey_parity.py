from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest


def _ns(**kwargs: Any) -> argparse.Namespace:
    defaults = dict(
        a="SV_SOURCE",
        b="SV_TARGET",
        deep=False,
        profile="cross_account",
        split_profile=False,
        manifest=None,
        account=None,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_parity_check_profile_flags_require_deep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qsync import cli_survey

    monkeypatch.setattr(
        cli_survey,
        "_get_client_config_for_args",
        lambda _args: ("example.qualtrics.test", {"X-API-TOKEN": "token"}),
    )

    with pytest.raises(SystemExit) as exc:
        cli_survey.handle_parity_check(_ns(profile="split"))

    assert exc.value.code == 1


def test_parity_check_split_alias_routes_profile_and_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qsync import cli_survey
    import qsync.survey_deep_parity

    monkeypatch.setattr(
        cli_survey,
        "_get_client_config_for_args",
        lambda _args: ("example.qualtrics.test", {"X-API-TOKEN": "token"}),
    )
    monkeypatch.setattr(
        cli_survey,
        "fetch_survey_definition",
        lambda _base, _headers, survey_id, fmt="json": {
            "SurveyID": survey_id,
            "SurveyOptions": {"SurveyLanguage": "EN", "AvailableLanguages": {"EN": True}},
            "Questions": {},
            "SurveyFlow": {"Flow": []},
        },
    )

    captured: dict[str, Any] = {}

    def _fake_compare(*_args, **kwargs):
        captured.update(kwargs)
        return qsync.survey_deep_parity.DeepParityReport(
            ok=True,
            hash_a="a" * 64,
            hash_b="a" * 64,
            diff_count=0,
            diff_paths=[],
            section_counts={},
            flow_changes=[],
        )

    monkeypatch.setattr(
        qsync.survey_deep_parity,
        "compare_survey_definition_deep_parity",
        _fake_compare,
    )
    monkeypatch.setattr(
        cli_survey,
        "_emit_deep_parity_report",
        lambda **_kwargs: True,
    )

    manifest_path = Path("/tmp/test-split-manifest.json")
    cli_survey.handle_parity_check(
        _ns(
            deep=True,
            profile="cross_account",
            split_profile=True,
            manifest=str(manifest_path),
        )
    )

    assert captured["profile"] == "split"
    assert captured["manifest_path"] == str(manifest_path)
