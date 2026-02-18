from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from tests.workspace_helpers import ensure_qsync_workspace


def test_extract_eos_message_refs() -> None:
    from qsync.eos_messages import extract_eos_message_refs

    payload = {
        "result": {
            "SurveyFlow": {
                "Flow": [
                    {
                        "Type": "EndSurvey",
                        "FlowID": "FL_1",
                        "Options": {
                            "SurveyTermination": "DisplayMessage",
                            "EOSMessageLibrary": "UR_LIB",
                            "EOSMessage": "MS_MSG",
                        },
                    }
                ]
            }
        }
    }
    refs = extract_eos_message_refs("SV_TEST", payload)
    assert [(r.library_id, r.message_id, r.flow_id) for r in refs] == [
        ("UR_LIB", "MS_MSG", "FL_1")
    ]


def test_find_message_contexts_scans_surveys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.eos_messages import find_message_contexts

    ensure_qsync_workspace(tmp_path)
    monkeypatch.setenv("QSYNC_ROOT", str(tmp_path))

    payload_a = {
        "result": {
            "SurveyFlow": {
                "Flow": [
                    {
                        "Type": "EndSurvey",
                        "FlowID": "FL_A",
                        "Options": {
                            "SurveyTermination": "DisplayMessage",
                            "EOSMessageLibrary": "UR_LIB",
                            "EOSMessage": "MS_MSG",
                        },
                    }
                ]
            }
        }
    }
    payload_b = {
        "result": {
            "SurveyFlow": {
                "Flow": [
                    {
                        "Type": "EndSurvey",
                        "FlowID": "FL_B",
                        "Options": {
                            "SurveyTermination": "DisplayMessage",
                            "EOSMessageLibrary": "UR_LIB",
                            "EOSMessage": "MS_MSG",
                        },
                    }
                ]
            }
        }
    }
    (tmp_path / "surveys" / "A__SV_A.json").write_text(
        json.dumps(payload_a), encoding="utf-8"
    )
    (tmp_path / "surveys" / "B__SV_B.json").write_text(
        json.dumps(payload_b), encoding="utf-8"
    )

    ctx = find_message_contexts(refs={("UR_LIB", "MS_MSG")}, include_backups=False)
    assert ("UR_LIB", "MS_MSG") in ctx
    survey_ids = sorted({c["survey_id"] for c in ctx[("UR_LIB", "MS_MSG")]})
    assert survey_ids == ["SV_A", "SV_B"]


def test_eos_codec_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from qsync.eos_messages import (
        read_library_message_from_disk,
        write_library_message_to_disk,
    )

    ensure_qsync_workspace(tmp_path)
    monkeypatch.setenv("QSYNC_ROOT", str(tmp_path))

    api_payload = {
        "result": {
            "category": "cat",
            "description": "desc",
            "messages": {"en": "<p>Hello</p>", "fr": "<p>Bonjour</p>"},
        }
    }
    folder = write_library_message_to_disk(
        library_id="UR_LIB",
        message_id="MS_MSG",
        api_payload=api_payload,
        contexts=[{"survey_id": "SV_TEST", "flow_id": "FL_1"}],
    )
    assert folder.exists()
    assert (folder / "meta.json").exists()
    assert (folder / "messages" / "_keys.json").exists()

    loaded = read_library_message_from_disk("UR_LIB", "MS_MSG")
    assert loaded is not None
    assert loaded.get("category") == "cat"
    assert loaded.get("description") == "desc"
    assert loaded.get("messages", {}).get("en") == "<p>Hello</p>"
    assert loaded.get("messages", {}).get("fr") == "<p>Bonjour</p>"


def test_eos_apply_stages_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.eos_messages import apply_eos_messages, write_library_message_to_disk
    from qsync.pending_stage import load_pending, EosPendingPayload

    ensure_qsync_workspace(tmp_path)
    monkeypatch.setenv("QSYNC_ROOT", str(tmp_path))

    survey_id = "SV_TEST"
    survey_payload = {
        "result": {
            "SurveyFlow": {
                "Flow": [
                    {
                        "Type": "EndSurvey",
                        "FlowID": "FL_1",
                        "Options": {
                            "SurveyTermination": "DisplayMessage",
                            "EOSMessageLibrary": "UR_LIB",
                            "EOSMessage": "MS_MSG",
                        },
                    }
                ]
            },
            "Questions": {},
            "Blocks": {},
        }
    }
    (tmp_path / "surveys" / f"Test__{survey_id}.json").write_text(
        json.dumps(survey_payload, indent=2), encoding="utf-8"
    )

    write_library_message_to_disk(
        library_id="UR_LIB",
        message_id="MS_MSG",
        api_payload={"result": {"messages": {"en": "hi"}}},
        contexts=[{"survey_id": survey_id, "flow_id": "FL_1"}],
    )

    # Create a local change after the pull snapshot.
    keys_path = (
        tmp_path
        / "contents"
        / "qualtrics_library_messages"
        / "UR_LIB"
        / "MS_MSG"
        / "messages"
        / "_keys.json"
    )
    keys = json.loads(keys_path.read_text(encoding="utf-8"))["entries"]
    assert keys
    first_file = keys[0]["file"]
    msg_path = (
        tmp_path
        / "contents"
        / "qualtrics_library_messages"
        / "UR_LIB"
        / "MS_MSG"
        / "messages"
        / first_file
    )
    msg_path.write_text(
        msg_path.read_text(encoding="utf-8") + "\n<!-- test -->\n", encoding="utf-8"
    )

    record = apply_eos_messages(
        survey_id=survey_id,
        allow_shared=True,
        allow_destructive=False,
        include_backups_scan=False,
    )
    assert record.survey_id == survey_id
    assert isinstance(record.payload, EosPendingPayload)
    assert len(record.payload.operations) == 1

    loaded = load_pending(survey_id, "eos")
    assert loaded is not None
    assert loaded.survey_id == survey_id
    assert isinstance(loaded.payload, EosPendingPayload)
    assert len(loaded.payload.operations) == 1
    op = loaded.payload.operations[0]
    assert op.library_id == "UR_LIB"
    assert op.message_id == "MS_MSG"


def test_eos_apply_is_noop_when_no_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.eos_messages import apply_eos_messages, write_library_message_to_disk
    from qsync.pending_stage import load_pending

    ensure_qsync_workspace(tmp_path)
    monkeypatch.setenv("QSYNC_ROOT", str(tmp_path))

    survey_id = "SV_TEST"
    survey_payload = {
        "result": {
            "SurveyFlow": {
                "Flow": [
                    {
                        "Type": "EndSurvey",
                        "FlowID": "FL_1",
                        "Options": {
                            "SurveyTermination": "DisplayMessage",
                            "EOSMessageLibrary": "UR_LIB",
                            "EOSMessage": "MS_MSG",
                        },
                    }
                ]
            },
            "Questions": {},
            "Blocks": {},
        }
    }
    (tmp_path / "surveys" / f"Test__{survey_id}.json").write_text(
        json.dumps(survey_payload, indent=2), encoding="utf-8"
    )

    write_library_message_to_disk(
        library_id="UR_LIB",
        message_id="MS_MSG",
        api_payload={"result": {"messages": {"en": "hi"}}},
        contexts=[{"survey_id": survey_id, "flow_id": "FL_1"}],
    )

    record = apply_eos_messages(
        survey_id=survey_id,
        allow_shared=True,
        allow_destructive=False,
        include_backups_scan=False,
    )
    assert record is None
    assert load_pending(survey_id, "eos") is None


def test_eos_apply_blocks_shared_message_with_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.eos_messages import ERROR_ID_EOS_SHARED_MESSAGE, apply_eos_messages

    ensure_qsync_workspace(tmp_path)
    monkeypatch.setenv("QSYNC_ROOT", str(tmp_path))

    payload = {
        "result": {
            "SurveyFlow": {
                "Flow": [
                    {
                        "Type": "EndSurvey",
                        "FlowID": "FL_1",
                        "Options": {
                            "SurveyTermination": "DisplayMessage",
                            "EOSMessageLibrary": "UR_LIB",
                            "EOSMessage": "MS_MSG",
                        },
                    }
                ]
            }
        }
    }
    (tmp_path / "surveys" / "A__SV_A.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    (tmp_path / "surveys" / "B__SV_B.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )

    with pytest.raises(RuntimeError) as excinfo:
        apply_eos_messages(
            survey_id="SV_A",
            allow_shared=False,
            allow_destructive=False,
            include_backups_scan=False,
        )

    msg = str(excinfo.value)
    assert ERROR_ID_EOS_SHARED_MESSAGE in msg
    assert "UR_LIB/MS_MSG" in msg
    assert "SV_B" in msg
    assert "qsync eos references --library-id UR_LIB --message-id MS_MSG" in msg

    log_path = tmp_path / "logs" / "qualtrics_push.log"
    assert log_path.exists()
    last = json.loads(log_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert last["error"]["error_id"] == ERROR_ID_EOS_SHARED_MESSAGE


def test_cross_account_eos_repair_dry_run_plans_imports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qsync.dimensions import eos_core

    calls: list[tuple[str, str, str]] = []

    class _Resp:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    target_payload = {
        "result": {
            "SurveyFlow": {
                "Flow": [
                    {
                        "Type": "EndSurvey",
                        "FlowID": "FL_1",
                        "Options": {
                            "SurveyTermination": "DisplayMessage",
                            "EOSMessageLibrary": "UR_SRC",
                            "EOSMessage": "MS_OLD",
                        },
                    }
                ]
            }
        }
    }
    source_payload = {
        "result": {
            "SurveyFlow": {
                "Flow": [
                    {
                        "Type": "EndSurvey",
                        "FlowID": "FL_1",
                        "Options": {
                            "SurveyTermination": "DisplayMessage",
                            "EOSMessageLibrary": "UR_SRC",
                            "EOSMessage": "MS_SRC",
                        },
                    }
                ]
            }
        }
    }

    def fake_send_api_request(**kwargs):
        key = (kwargs.get("method"), kwargs.get("base_url"), kwargs.get("path"))
        calls.append((str(key[0]), str(key[1]), str(key[2])))
        if key == ("GET", "target.qualtrics.test", "survey-definitions/SV_TGT"):
            return _Resp(target_payload)
        if key == ("GET", "source.qualtrics.test", "survey-definitions/SV_SRC"):
            return _Resp(source_payload)
        if key == ("GET", "target.qualtrics.test", "libraries/UR_SRC/messages/MS_OLD"):
            response = Mock(status_code=404)
            raise requests.HTTPError("not found", response=response)
        raise AssertionError(f"Unexpected API call: {kwargs}")

    monkeypatch.setattr(eos_core, "send_api_request", fake_send_api_request)

    result = eos_core.repair_eos_messages_from_source_account(
        target_survey_id="SV_TGT",
        source_survey_id="SV_SRC",
        target_base_url="target.qualtrics.test",
        target_headers={"X-API-TOKEN": "target-token"},
        source_base_url="source.qualtrics.test",
        source_headers={"X-API-TOKEN": "source-token"},
        include_backups_scan=False,
        dry_run=True,
        publish=False,
    )

    assert result.dry_run is True
    assert result.target_refs_total == 1
    assert result.source_refs_total == 1
    assert result.missing_refs == 1
    assert result.planned_rewire_count == 1
    assert len(result.planned_imports) == 1
    planned = result.planned_imports[0]
    assert planned.target_library_id == "UR_SRC"
    assert planned.target_message_id == "MS_OLD"
    assert planned.source_library_id == "UR_SRC"
    assert planned.source_message_id == "MS_SRC"
    assert planned.target_create_library_id == "UR_SRC"
    assert result.created_pairs == {}
    assert result.replacements == {}
    assert ("POST", "target.qualtrics.test", "libraries/UR_SRC/messages") not in calls


def test_cross_account_eos_repair_rewrites_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qsync.dimensions import eos_core

    ensure_qsync_workspace(tmp_path)
    monkeypatch.setenv("QSYNC_ROOT", str(tmp_path))

    pushed_flow: dict = {}

    class _Resp:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    target_payload = {
        "result": {
            "SurveyFlow": {
                "Flow": [
                    {
                        "Type": "EndSurvey",
                        "FlowID": "FL_1",
                        "Options": {
                            "SurveyTermination": "DisplayMessage",
                            "EOSMessageLibrary": "UR_SRC",
                            "EOSMessage": "MS_OLD",
                        },
                    }
                ]
            }
        }
    }
    source_payload = {
        "result": {
            "SurveyFlow": {
                "Flow": [
                    {
                        "Type": "EndSurvey",
                        "FlowID": "FL_1",
                        "Options": {
                            "SurveyTermination": "DisplayMessage",
                            "EOSMessageLibrary": "UR_SRC",
                            "EOSMessage": "MS_SRC",
                        },
                    }
                ]
            }
        }
    }

    def fake_send_api_request(**kwargs):
        method = kwargs.get("method")
        base_url = kwargs.get("base_url")
        path = kwargs.get("path")
        if (method, base_url, path) == (
            "GET",
            "target.qualtrics.test",
            "survey-definitions/SV_TGT",
        ):
            return _Resp(target_payload)
        if (method, base_url, path) == (
            "GET",
            "source.qualtrics.test",
            "survey-definitions/SV_SRC",
        ):
            return _Resp(source_payload)
        if (method, base_url, path) == (
            "GET",
            "target.qualtrics.test",
            "libraries/UR_SRC/messages/MS_OLD",
        ):
            response = Mock(status_code=404)
            raise requests.HTTPError("not found", response=response)
        if (method, base_url, path) == (
            "GET",
            "source.qualtrics.test",
            "libraries/UR_SRC/messages/MS_SRC",
        ):
            return _Resp(
                {
                    "result": {
                        "category": "endOfSurvey",
                        "description": "source",
                        "messages": {"en": "<p>source</p>"},
                    }
                }
            )
        if (method, base_url, path) == ("GET", "target.qualtrics.test", "whoami"):
            return _Resp({"result": {"userId": "UR_TARGET"}})
        if (method, base_url, path) == (
            "POST",
            "target.qualtrics.test",
            "libraries/UR_SRC/messages",
        ):
            return _Resp({"result": {"messageId": "MS_NEW"}})
        if (method, base_url, path) == (
            "PUT",
            "target.qualtrics.test",
            "libraries/UR_SRC/messages/MS_NEW",
        ):
            return _Resp({"result": {}})
        if (method, base_url, path) == (
            "PUT",
            "target.qualtrics.test",
            "survey-definitions/SV_TGT/flow",
        ):
            pushed_flow.update(kwargs.get("json") or {})
            return _Resp({"result": {}})
        raise AssertionError(f"Unexpected API call: {kwargs}")

    monkeypatch.setattr(eos_core, "send_api_request", fake_send_api_request)
    monkeypatch.setattr(
        eos_core, "ensure_backup", lambda survey_id: tmp_path / "surveys" / "backups"
    )
    monkeypatch.setattr(
        eos_core,
        "refresh_survey_cache",
        lambda survey_id: (
            Mock(survey_id=survey_id, payload={"result": {}}, path=tmp_path / "surveys"),
            False,
        ),
    )
    monkeypatch.setattr(
        eos_core,
        "pull_eos_messages",
        lambda **kwargs: [
            tmp_path
            / "contents"
            / "qualtrics_library_messages"
            / "UR_SRC"
            / "MS_NEW"
        ],
    )
    monkeypatch.setattr(
        eos_core,
        "publish_survey_definition",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("publish should not be called")
        ),
    )

    result = eos_core.repair_eos_messages_from_source_account(
        target_survey_id="SV_TGT",
        source_survey_id="SV_SRC",
        target_base_url="target.qualtrics.test",
        target_headers={"X-API-TOKEN": "target-token"},
        source_base_url="source.qualtrics.test",
        source_headers={"X-API-TOKEN": "source-token"},
        include_backups_scan=False,
        dry_run=False,
        publish=False,
    )

    assert result.dry_run is False
    assert result.missing_refs == 1
    assert result.created_pairs == {("UR_SRC", "MS_SRC"): ("UR_SRC", "MS_NEW")}
    assert result.replacements == {("UR_SRC", "MS_OLD"): ("UR_SRC", "MS_NEW")}
    assert result.updated_flow_ids == ["FL_1"]
    assert len(result.pulled_paths) == 1
    assert pushed_flow["Flow"][0]["Options"]["EOSMessageLibrary"] == "UR_SRC"
    assert pushed_flow["Flow"][0]["Options"]["EOSMessage"] == "MS_NEW"
