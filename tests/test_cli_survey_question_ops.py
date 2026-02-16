from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class _Resp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


@dataclass
class _PushCtx:
    survey_name: str = "Test Survey"
    response_count: int = 0
    counts_unknown: bool = False

    def describe_counts(self) -> str:
        return "preview=0 response=0 source=test"


def _base_definition() -> dict[str, Any]:
    return {
        "Questions": {
            "QID15": {
                "QuestionID": "QID15",
                "QuestionType": "MC",
                "Selector": "SAVR",
                "SubSelector": "TX",
                "QuestionText": "Template question",
                "DataExportTag": "Q15",
            }
        },
        "Blocks": {
            "BL_MAIN": {
                "Type": "Standard",
                "BlockElements": [
                    {"Type": "Question", "QuestionID": "QID15"},
                ],
            }
        },
        "SurveyFlow": {
            "Flow": [
                {"Type": "Block", "ID": "BL_MAIN"},
            ]
        },
    }


def test_add_question_dry_run_does_not_write(monkeypatch) -> None:
    from qsync import cli_survey

    monkeypatch.setattr(
        cli_survey,
        "_prompt_for_survey_id_api_if_needed",
        lambda **_kwargs: "SV_TEST",
    )
    monkeypatch.setattr(
        cli_survey,
        "_get_client_config_for_args",
        lambda _args: ("example.qualtrics.com", {"X-API-TOKEN": "x"}),
    )
    monkeypatch.setattr(cli_survey, "ensure_unlocked", lambda _sid: None)
    monkeypatch.setattr(cli_survey, "load_push_context", lambda *_a, **_k: _PushCtx())
    monkeypatch.setattr(
        cli_survey,
        "fetch_survey_definition",
        lambda *_a, **_k: _base_definition(),
    )

    writes: list[str] = []

    def _fail_write(*_a, **_k):
        writes.append("write")
        raise AssertionError("send_api_request should not be called in dry-run")

    monkeypatch.setattr(cli_survey, "send_api_request", _fail_write)

    args = argparse.Namespace(
        survey_id="SV_TEST",
        from_question_id="QID15",
        question_json=None,
        question_text=["Item A", "Item B"],
        question_text_file=None,
        target_block_id=None,
        after_qid="QID15",
        before_qid=None,
        position="append",
        data_export_tag=None,
        allow_duplicate_tags=False,
        dry_run=True,
        force_live=False,
        yes=False,
        no_publish=True,
        publish_description=None,
        account=None,
    )

    cli_survey.handle_add_question(args)
    assert writes == []


def test_add_question_creates_and_reorders_blocks(monkeypatch) -> None:
    from qsync import cli_survey

    initial = _base_definition()
    after_create = _base_definition()
    after_create["Questions"]["QID16"] = {
        "QuestionID": "QID16",
        "QuestionType": "MC",
        "QuestionText": "Item A",
    }
    after_create["Questions"]["QID17"] = {
        "QuestionID": "QID17",
        "QuestionType": "MC",
        "QuestionText": "Item B",
    }
    after_create["Blocks"]["BL_AUTO"] = {
        "Type": "Standard",
        "BlockElements": [
            {"Type": "Question", "QuestionID": "QID16"},
            {"Type": "Question", "QuestionID": "QID17"},
        ],
    }
    after_create["SurveyFlow"]["Flow"].append({"Type": "Block", "ID": "BL_AUTO"})

    monkeypatch.setattr(
        cli_survey,
        "_prompt_for_survey_id_api_if_needed",
        lambda **_kwargs: "SV_TEST",
    )
    monkeypatch.setattr(
        cli_survey,
        "_get_client_config_for_args",
        lambda _args: ("example.qualtrics.com", {"X-API-TOKEN": "x"}),
    )
    monkeypatch.setattr(cli_survey, "ensure_unlocked", lambda _sid: None)
    monkeypatch.setattr(cli_survey, "load_push_context", lambda *_a, **_k: _PushCtx())

    fetch_calls = {"n": 0}

    def _fetch(*_a, **_k):
        fetch_calls["n"] += 1
        return initial if fetch_calls["n"] == 1 else after_create

    monkeypatch.setattr(cli_survey, "fetch_survey_definition", _fetch)

    created_ids = iter(["QID16", "QID17"])
    updated_blocks: dict[str, dict[str, Any]] = {}

    def _send_api_request(*, method: str, path: str, json=None, **_kwargs):
        if method == "POST" and path == "survey-definitions/SV_TEST/questions":
            return _Resp({"result": {"QuestionID": next(created_ids)}})
        if method == "PUT" and path.startswith(
            "survey-definitions/SV_TEST/blocks/"
        ):
            block_id = Path(path).name
            updated_blocks[block_id] = json
            return _Resp({"result": {"ok": True}})
        raise AssertionError(f"Unexpected API call: {method} {path}")

    monkeypatch.setattr(cli_survey, "send_api_request", _send_api_request)

    published: list[str] = []
    monkeypatch.setattr(
        cli_survey,
        "publish_survey_definition",
        lambda survey_id, **_kwargs: published.append(survey_id),
    )
    monkeypatch.setattr(
        cli_survey,
        "download_survey_definition",
        lambda survey_id, **_kwargs: Path(f"/tmp/{survey_id}.json"),
    )

    args = argparse.Namespace(
        survey_id="SV_TEST",
        from_question_id="QID15",
        question_json=None,
        question_text=["Item A", "Item B"],
        question_text_file=None,
        target_block_id=None,
        after_qid="QID15",
        before_qid=None,
        position="append",
        data_export_tag=None,
        allow_duplicate_tags=False,
        dry_run=False,
        force_live=False,
        yes=True,
        no_publish=False,
        publish_description="add qids",
        account=None,
    )

    cli_survey.handle_add_question(args)

    assert published == ["SV_TEST"]
    assert set(updated_blocks.keys()) == {"BL_MAIN", "BL_AUTO"}
    main_qids = [
        str(elem.get("QuestionID") or "")
        for elem in updated_blocks["BL_MAIN"].get("BlockElements", [])
    ]
    assert main_qids == ["QID15", "QID16", "QID17"]
    auto_qids = [
        str(elem.get("QuestionID") or "")
        for elem in updated_blocks["BL_AUTO"].get("BlockElements", [])
    ]
    assert auto_qids == []


def test_move_question_reorders_within_block(monkeypatch) -> None:
    from qsync import cli_survey

    definition = {
        "Questions": {
            "QID1": {"QuestionID": "QID1", "QuestionType": "MC"},
            "QID2": {"QuestionID": "QID2", "QuestionType": "MC"},
            "QID3": {"QuestionID": "QID3", "QuestionType": "MC"},
        },
        "Blocks": {
            "BL_MAIN": {
                "Type": "Standard",
                "BlockElements": [
                    {"Type": "Question", "QuestionID": "QID1"},
                    {"Type": "Question", "QuestionID": "QID2"},
                    {"Type": "Question", "QuestionID": "QID3"},
                ],
            }
        },
        "SurveyFlow": {"Flow": [{"Type": "Block", "ID": "BL_MAIN"}]},
    }

    monkeypatch.setattr(
        cli_survey,
        "_prompt_for_survey_id_api_if_needed",
        lambda **_kwargs: "SV_TEST",
    )
    monkeypatch.setattr(
        cli_survey,
        "_get_client_config_for_args",
        lambda _args: ("example.qualtrics.com", {"X-API-TOKEN": "x"}),
    )
    monkeypatch.setattr(cli_survey, "ensure_unlocked", lambda _sid: None)
    monkeypatch.setattr(cli_survey, "load_push_context", lambda *_a, **_k: _PushCtx())
    monkeypatch.setattr(
        cli_survey,
        "fetch_survey_definition",
        lambda *_a, **_k: definition,
    )

    block_payloads: list[dict[str, Any]] = []

    def _send_api_request(*, method: str, path: str, json=None, **_kwargs):
        if method == "PUT" and path == "survey-definitions/SV_TEST/blocks/BL_MAIN":
            block_payloads.append(json)
            return _Resp({"result": {"ok": True}})
        raise AssertionError(f"Unexpected API call: {method} {path}")

    monkeypatch.setattr(cli_survey, "send_api_request", _send_api_request)
    monkeypatch.setattr(
        cli_survey,
        "download_survey_definition",
        lambda survey_id, **_kwargs: Path(f"/tmp/{survey_id}.json"),
    )

    args = argparse.Namespace(
        survey_id="SV_TEST",
        question_id=["QID3"],
        target_block_id=None,
        after_qid=None,
        before_qid="QID1",
        position="append",
        dry_run=False,
        force_live=False,
        yes=True,
        no_publish=True,
        publish_description=None,
        account=None,
    )

    cli_survey.handle_move_question(args)

    assert len(block_payloads) == 1
    qids = [
        str(elem.get("QuestionID") or "")
        for elem in block_payloads[0].get("BlockElements", [])
    ]
    assert qids == ["QID3", "QID1", "QID2"]
