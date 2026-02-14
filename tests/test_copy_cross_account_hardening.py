from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest


def _ns(**kwargs: Any) -> argparse.Namespace:
    defaults = dict(
        source_survey_id="SV_SOURCE",
        new_name="New Survey",
        target_api_key=None,
        target_base_url=None,
        source_api_key=None,
        source_base_url=None,
        activate=False,
        publish=False,
        publish_description=None,
        force_overwrite=False,
        yes=True,
        no_translations=True,
        verify=False,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _resp(payload: dict) -> Mock:
    m = Mock()
    m.ok = True
    m.json.return_value = payload
    return m


def test_list_surveys_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    from qsync import cli_survey

    calls: list[dict[str, Any]] = []

    def fake_send_api_request(**kwargs):
        calls.append(dict(kwargs))
        # First page includes nextPage; second page ends.
        if len(calls) == 1:
            return _resp(
                {
                    "result": {
                        "elements": [{"id": "SV_1", "name": "A"}],
                        "nextPage": "https://example.qualtrics.com/API/v3/surveys?page=2",
                    }
                }
            )
        return _resp({"result": {"elements": [{"id": "SV_2", "name": "B"}]}})

    monkeypatch.setattr(cli_survey, "send_api_request", fake_send_api_request)

    surveys = cli_survey.list_surveys("example.qualtrics.com", {"X-API-TOKEN": "x"})
    assert [s.get("id") for s in surveys] == ["SV_1", "SV_2"]
    assert len(calls) == 2
    assert calls[0]["params"] == {"pageSize": 100}
    assert calls[1]["params"] is None


def test_copy_cross_account_requires_target_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qsync import cli_survey
    import qsync.config

    monkeypatch.delenv("TARGET_QUALTRICS_BASE_URL", raising=False)
    monkeypatch.delenv("TARGET_X-API-TOKEN", raising=False)
    monkeypatch.delenv("TARGET_QUALTRICS_API_KEY", raising=False)

    monkeypatch.setattr(cli_survey, "resolve_root", lambda required=False: Path("/tmp"))
    monkeypatch.setattr(qsync.config, "resolve_env_path", lambda root=None: None)
    monkeypatch.setattr(qsync.config, "load_env_file", lambda path=None: {})

    with pytest.raises(SystemExit) as exc:
        cli_survey.handle_copy_cross_account(_ns())
    assert exc.value.code == 1


def test_copy_cross_account_uses_target_env_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qsync import cli_survey
    import qsync.config

    # Make TARGET_* available via dotenv (not flags).
    monkeypatch.setattr(cli_survey, "resolve_root", lambda required=False: Path("/tmp"))
    monkeypatch.setattr(
        qsync.config, "resolve_env_path", lambda root=None: Path("/tmp/.env")
    )
    monkeypatch.setattr(
        qsync.config,
        "load_env_file",
        lambda path=None: {
            "TARGET_QUALTRICS_BASE_URL": "target.qualtrics.test",
            "TARGET_X-API-TOKEN": "target-token",
        },
    )

    # Source env is the default account.
    monkeypatch.setattr(
        qsync.config,
        "load_env",
        lambda *args, **kwargs: {
            "QUALTRICS_BASE_URL": "source.qualtrics.test",
            "X-API-TOKEN": "source-token",
        },
    )

    used_bases: list[str] = []

    def fake_get_client_config(env=None):
        base = (env or {}).get("QUALTRICS_BASE_URL") or "missing"
        token = (env or {}).get("X-API-TOKEN") or (env or {}).get("QUALTRICS_API_KEY")
        used_bases.append(base)
        return base, {"Accept": "application/json", "X-API-TOKEN": token}

    monkeypatch.setattr(cli_survey, "get_client_config", fake_get_client_config)
    monkeypatch.setattr(
        cli_survey,
        "fetch_survey_definition",
        lambda base, headers, survey_id, fmt="qsf": {
            "SurveyEntry": {"SurveyName": "SourceSurvey"},
            "SurveyElements": [],
        },
    )
    monkeypatch.setattr(
        cli_survey,
        "resolve_target_name_with_conflict",
        lambda *_args, **_kwargs: ("New Survey", None),
    )
    monkeypatch.setattr(cli_survey, "prepare_qsf_for_import", lambda *a, **k: None)

    captured_upload: dict[str, Any] = {}

    def fake_upload(qsf, new_name, base_url, headers, **kwargs):
        captured_upload["base_url"] = base_url
        return "SV_NEW"

    monkeypatch.setattr(cli_survey, "upload_qsf_to_account", fake_upload)

    def fake_send_api_request(**kwargs):
        # Only used for whoami in this test.
        if kwargs.get("path") == "whoami":
            return _resp({"result": {"userId": "UR_TEST", "brandId": "test"}})
        raise AssertionError(f"Unexpected API call: {kwargs}")

    monkeypatch.setattr(cli_survey, "send_api_request", fake_send_api_request)

    cli_survey.handle_copy_cross_account(_ns())

    # get_client_config was used for source and for target.
    assert "source.qualtrics.test" in used_bases
    assert "target.qualtrics.test" in used_bases
    assert captured_upload["base_url"] == "target.qualtrics.test"


def test_copy_cross_account_force_overwrite_delete_is_lock_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qsync import cli_survey
    import qsync.config

    monkeypatch.setattr(cli_survey, "resolve_root", lambda required=False: Path("/tmp"))
    monkeypatch.setattr(qsync.config, "resolve_env_path", lambda root=None: None)
    monkeypatch.setattr(qsync.config, "load_env_file", lambda path=None: {})
    monkeypatch.setattr(
        qsync.config,
        "load_env",
        lambda *args, **kwargs: {
            "QUALTRICS_BASE_URL": "source.qualtrics.test",
            "X-API-TOKEN": "source-token",
        },
    )

    def fake_get_client_config(env=None):
        base = (env or {}).get("QUALTRICS_BASE_URL") or "missing"
        token = (env or {}).get("X-API-TOKEN") or (env or {}).get("QUALTRICS_API_KEY")
        return base, {"Accept": "application/json", "X-API-TOKEN": token}

    monkeypatch.setattr(cli_survey, "get_client_config", fake_get_client_config)
    monkeypatch.setattr(
        cli_survey,
        "fetch_survey_definition",
        lambda base, headers, survey_id, fmt="qsf": {
            "SurveyEntry": {"SurveyName": "SourceSurvey"},
            "SurveyElements": [],
        },
    )
    monkeypatch.setattr(
        cli_survey,
        "resolve_target_name_with_conflict",
        lambda *_args, **_kwargs: ("New Survey", "SV_EXISTING"),
    )
    monkeypatch.setattr(cli_survey, "prepare_qsf_for_import", lambda *a, **k: None)
    monkeypatch.setattr(cli_survey, "upload_qsf_to_account", lambda *a, **k: "SV_NEW")
    monkeypatch.setattr(cli_survey, "publish_survey_definition", lambda *a, **k: {})

    calls: list[dict[str, Any]] = []

    def fake_send_api_request(**kwargs):
        calls.append(dict(kwargs))
        if kwargs.get("path") == "whoami":
            return _resp({"result": {"userId": "UR_TEST", "brandId": "test"}})
        return _resp({"result": {}})

    monkeypatch.setattr(cli_survey, "send_api_request", fake_send_api_request)

    cli_survey.handle_copy_cross_account(
        _ns(
            target_base_url="target.qualtrics.test",
            target_api_key="target-token",
            force_overwrite=True,
        )
    )

    delete_calls = [
        c for c in calls if c.get("method") == "DELETE" and str(c.get("path", "")).startswith("surveys/")
    ]
    assert delete_calls, "Expected a DELETE call for overwrite"
    assert delete_calls[0].get("survey_id") == "SV_EXISTING"


def test_copy_cross_account_verify_fails_on_parity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qsync import cli_survey
    import qsync.config

    monkeypatch.setattr(cli_survey, "resolve_root", lambda required=False: Path("/tmp"))
    monkeypatch.setattr(qsync.config, "resolve_env_path", lambda root=None: None)
    monkeypatch.setattr(qsync.config, "load_env_file", lambda path=None: {})
    monkeypatch.setattr(
        qsync.config,
        "load_env",
        lambda *args, **kwargs: {
            "QUALTRICS_BASE_URL": "source.qualtrics.test",
            "X-API-TOKEN": "source-token",
        },
    )

    def fake_get_client_config(env=None):
        base = (env or {}).get("QUALTRICS_BASE_URL") or "missing"
        token = (env or {}).get("X-API-TOKEN") or (env or {}).get("QUALTRICS_API_KEY")
        return base, {"Accept": "application/json", "X-API-TOKEN": token}

    monkeypatch.setattr(cli_survey, "get_client_config", fake_get_client_config)
    monkeypatch.setattr(
        cli_survey,
        "fetch_survey_definition",
        lambda base, headers, survey_id, fmt="qsf": {
            "SurveyEntry": {"SurveyName": "Survey"},
            "SurveyElements": [],
        },
    )
    monkeypatch.setattr(
        cli_survey,
        "resolve_target_name_with_conflict",
        lambda *_args, **_kwargs: ("New Survey", None),
    )
    monkeypatch.setattr(cli_survey, "prepare_qsf_for_import", lambda *a, **k: None)
    monkeypatch.setattr(cli_survey, "upload_qsf_to_account", lambda *a, **k: "SV_NEW")

    def fake_send_api_request(**kwargs):
        if kwargs.get("path") == "whoami":
            return _resp({"result": {"userId": "UR_TEST", "brandId": "test"}})
        return _resp({"result": {}})

    monkeypatch.setattr(cli_survey, "send_api_request", fake_send_api_request)

    def fake_emit_parity_report(*args, **kwargs):
        # `_emit_parity_report` is keyword-only. Catch accidental positional args.
        assert not args
        assert "result" in kwargs
        return False

    monkeypatch.setattr(cli_survey, "_emit_parity_report", fake_emit_parity_report)

    with pytest.raises(SystemExit):
        cli_survey.handle_copy_cross_account(
            _ns(
                target_base_url="target.qualtrics.test",
                target_api_key="target-token",
                verify=True,
                no_translations=True,
            )
        )


def test_copy_cross_account_languages_dict_payload_is_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test: AvailableLanguages may be a dict, not a list."""

    from qsync import cli_survey
    import qsync.config
    import qsync.translation_export

    monkeypatch.setattr(cli_survey, "resolve_root", lambda required=False: Path("/tmp"))
    monkeypatch.setattr(qsync.config, "resolve_env_path", lambda root=None: None)
    monkeypatch.setattr(qsync.config, "load_env_file", lambda path=None: {})
    monkeypatch.setattr(
        qsync.config,
        "load_env",
        lambda *args, **kwargs: {
            "QUALTRICS_BASE_URL": "source.qualtrics.test",
            "X-API-TOKEN": "source-token",
        },
    )

    def fake_get_client_config(env=None):
        base = (env or {}).get("QUALTRICS_BASE_URL") or "missing"
        token = (env or {}).get("X-API-TOKEN") or (env or {}).get("QUALTRICS_API_KEY")
        return base, {"Accept": "application/json", "X-API-TOKEN": token}

    monkeypatch.setattr(cli_survey, "get_client_config", fake_get_client_config)
    monkeypatch.setattr(
        cli_survey,
        "fetch_survey_definition",
        lambda base, headers, survey_id, fmt="qsf": {
            "SurveyEntry": {"SurveyName": "Survey"},
            "SurveyElements": [],
        },
    )
    monkeypatch.setattr(
        cli_survey,
        "resolve_target_name_with_conflict",
        lambda *_args, **_kwargs: ("New Survey", None),
    )
    monkeypatch.setattr(cli_survey, "prepare_qsf_for_import", lambda *a, **k: None)
    monkeypatch.setattr(cli_survey, "upload_qsf_to_account", lambda *a, **k: "SV_NEW")

    # Avoid relying on real translation map logic for this regression.
    monkeypatch.setattr(
        qsync.translation_export,
        "build_translation_map_from_cache",
        lambda *a, **k: {},
    )

    calls: list[dict[str, Any]] = []

    def fake_send_api_request(**kwargs):
        calls.append(dict(kwargs))
        path = kwargs.get("path")
        if path == "whoami":
            return _resp({"result": {"userId": "UR_TEST", "brandId": "test"}})
        if str(path).endswith("/languages") and kwargs.get("method") == "GET":
            # Dict-shaped payload is common: {"EN": true, "FR": true, ...}
            return _resp({"result": {"AvailableLanguages": {"EN": True}}})
        if str(path).endswith("/options"):
            return _resp({"result": {"SurveyLanguage": "EN"}})
        if str(path).startswith("survey-definitions/") and kwargs.get("method") == "GET":
            return _resp({"result": {"SurveyOptions": {}, "Questions": {}}})
        return _resp({"result": {}})

    monkeypatch.setattr(cli_survey, "send_api_request", fake_send_api_request)

    cli_survey.handle_copy_cross_account(
        _ns(
            target_base_url="target.qualtrics.test",
            target_api_key="target-token",
            no_translations=False,
        )
    )

    # Ensure we attempted to enable languages in the target (i.e., we did NOT
    # misread dict-shaped AvailableLanguages as "no languages").
    put_calls = [
        c
        for c in calls
        if c.get("action") == "qsync.translations.languages.ensure"
        and c.get("method") == "PUT"
    ]
    assert put_calls


def test_copy_cross_account_translation_failure_exits_after_upload(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If translation copy fails, the survey should already exist in target and the command must exit non-zero."""

    from qsync import cli_survey
    import qsync.config

    monkeypatch.setattr(cli_survey, "resolve_root", lambda required=False: Path("/tmp"))
    monkeypatch.setattr(qsync.config, "resolve_env_path", lambda root=None: None)
    monkeypatch.setattr(qsync.config, "load_env_file", lambda path=None: {})
    monkeypatch.setattr(
        qsync.config,
        "load_env",
        lambda *args, **kwargs: {
            "QUALTRICS_BASE_URL": "source.qualtrics.test",
            "X-API-TOKEN": "source-token",
        },
    )

    def fake_get_client_config(env=None):
        base = (env or {}).get("QUALTRICS_BASE_URL") or "missing"
        token = (env or {}).get("X-API-TOKEN") or (env or {}).get("QUALTRICS_API_KEY")
        return base, {"Accept": "application/json", "X-API-TOKEN": token}

    monkeypatch.setattr(cli_survey, "get_client_config", fake_get_client_config)
    monkeypatch.setattr(
        cli_survey,
        "fetch_survey_definition",
        lambda base, headers, survey_id, fmt="qsf": {
            "SurveyEntry": {"SurveyName": "Survey"},
            "SurveyElements": [],
        },
    )
    monkeypatch.setattr(
        cli_survey,
        "resolve_target_name_with_conflict",
        lambda *_args, **_kwargs: ("New Survey", None),
    )
    monkeypatch.setattr(cli_survey, "prepare_qsf_for_import", lambda *a, **k: None)

    uploaded: dict[str, Any] = {"ok": False}

    def fake_upload(qsf, new_name, base_url, headers, **kwargs):
        uploaded["ok"] = True
        return "SV_NEW"

    monkeypatch.setattr(cli_survey, "upload_qsf_to_account", fake_upload)

    def fake_send_api_request(**kwargs):
        path = str(kwargs.get("path") or "")
        if path == "whoami":
            return _resp({"result": {"userId": "UR_TEST", "brandId": "test"}})
        if path == "surveys/SV_SOURCE/languages":
            return _resp({"result": {"AvailableLanguages": ["EN"]}})
        if path == "survey-definitions/SV_SOURCE/options":
            return _resp({"result": {"SurveyLanguage": "EN"}})
        if path == "survey-definitions/SV_NEW/options":
            return _resp({"result": {"SurveyLanguage": "FR"}})
        return _resp({"result": {}})

    monkeypatch.setattr(cli_survey, "send_api_request", fake_send_api_request)

    with pytest.raises(SystemExit) as exc:
        cli_survey.handle_copy_cross_account(
            _ns(
                source_survey_id="SV_SOURCE",
                target_base_url="target.qualtrics.test",
                target_api_key="target-token",
                no_translations=False,
            )
        )
    assert exc.value.code == 1
    assert uploaded["ok"] is True

    out = capsys.readouterr().out
    assert "Survey uploaded: SV_NEW" in out
