from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest


def _ns(**kwargs: Any) -> argparse.Namespace:
    defaults = dict(
        name="Created Survey",
        from_qsf=None,
        template_survey_id=None,
        language=None,
        project_category=None,
        force_duplicate=False,
        json=False,
        account="damian",
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _source_qsf() -> dict[str, Any]:
    return {
        "SurveyEntry": {
            "SurveyID": "SV_OLD",
            "SurveyName": "Old Survey",
            "SurveyStatus": "Active",
            "SurveyLanguage": "FR",
        },
        "SurveyElements": [
            {
                "SurveyID": "SV_OLD",
                "Element": "SO",
                "Payload": {
                    "SurveyTitle": "Old Survey",
                    "AvailableLanguages": {"FR": []},
                },
            },
            {
                "SurveyID": "SV_OLD",
                "Element": "PROJ",
                "PrimaryAttribute": "CORE",
                "Payload": {"ProjectCategory": "CORE"},
            },
        ],
    }


def _patch_common(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from qsync import cli_survey
    import qsync.terminal_output as terminal_output

    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        cli_survey,
        "_get_client_config_for_args",
        lambda _args: ("example.qualtrics.test", {"X-API-TOKEN": "token"}),
    )
    monkeypatch.setattr(terminal_output, "log_confirmation", lambda *_a, **_k: None)

    def fake_upload(qsf, new_name, base_url, headers, **kwargs):
        captured["qsf"] = qsf
        captured["new_name"] = new_name
        captured["base_url"] = base_url
        captured["headers"] = headers
        captured["kwargs"] = kwargs
        return "SV_NEW"

    monkeypatch.setattr(cli_survey, "upload_qsf_to_account", fake_upload)
    return captured


def test_survey_create_help_includes_command(capsys: pytest.CaptureFixture[str]) -> None:
    from qsync.cli import main

    with pytest.raises(SystemExit):
        main(["survey", "--help"])

    assert "create" in capsys.readouterr().out


def test_create_minimal_template_uploads_inactive_survey(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from qsync import cli_survey

    captured = _patch_common(monkeypatch)
    cli_survey.handle_create(_ns(language="EN"))

    qsf = captured["qsf"]
    assert captured["new_name"] == "Created Survey"
    assert captured["kwargs"]["action"] == "qsync.survey.create"
    assert captured["kwargs"]["log_meta"]["source_kind"] == "minimal"
    assert qsf["SurveyEntry"]["SurveyName"] == "Created Survey"
    assert qsf["SurveyEntry"]["SurveyStatus"] == "Inactive"
    assert qsf["SurveyEntry"]["SurveyLanguage"] == "EN"
    assert "SurveyID" not in qsf["SurveyEntry"]
    assert "Successfully created Created Survey (SV_NEW)" in capsys.readouterr().out


def test_create_from_qsf_file_rewrites_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from qsync import cli_survey

    qsf_path = tmp_path / "source.qsf"
    qsf_path.write_text(json.dumps(_source_qsf()), encoding="utf-8")
    captured = _patch_common(monkeypatch)

    cli_survey.handle_create(
        _ns(from_qsf=str(qsf_path), language="NL", project_category="RESEARCH")
    )

    qsf = captured["qsf"]
    assert qsf["SurveyEntry"]["SurveyName"] == "Created Survey"
    assert qsf["SurveyEntry"]["SurveyLanguage"] == "NL"
    assert "SurveyID" not in qsf["SurveyEntry"]
    proj = next(e for e in qsf["SurveyElements"] if e.get("Element") == "PROJ")
    assert proj["PrimaryAttribute"] == "RESEARCH"
    assert proj["Payload"]["ProjectCategory"] == "RESEARCH"


def test_create_from_template_survey_fetches_qsf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qsync import cli_survey

    captured = _patch_common(monkeypatch)
    fetched: dict[str, Any] = {}

    def fake_fetch(base, headers, survey_id, fmt="json"):
        fetched.update(
            {"base": base, "headers": headers, "survey_id": survey_id, "fmt": fmt}
        )
        return _source_qsf()

    monkeypatch.setattr(cli_survey, "fetch_survey_definition", fake_fetch)

    cli_survey.handle_create(_ns(template_survey_id="SV_TEMPLATE"))

    assert fetched == {
        "base": "example.qualtrics.test",
        "headers": {"X-API-TOKEN": "token"},
        "survey_id": "SV_TEMPLATE",
        "fmt": "qsf",
    }
    assert captured["kwargs"]["log_meta"]["source_kind"] == "template-survey"
    assert captured["kwargs"]["log_meta"]["source_ref"] == "SV_TEMPLATE"


def test_create_json_output_is_compact_machine_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from qsync import cli_survey

    _patch_common(monkeypatch)
    cli_survey.handle_create(_ns(json=True))

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "ok": True,
        "survey_id": "SV_NEW",
        "name": "Created Survey",
        "account": "damian",
        "base_url": "example.qualtrics.test",
        "source_kind": "minimal",
        "source_ref": "bundled-minimal-qsf",
        "edit_url": "https://example.qualtrics.test/survey-builder/SV_NEW/edit",
    }
