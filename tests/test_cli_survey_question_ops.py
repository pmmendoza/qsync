from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest


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


def test_add_question_insert_index_with_between_page_breaks(monkeypatch) -> None:
    from qsync import cli_survey

    initial = {
        "Questions": {
            "QID1": {
                "QuestionID": "QID1",
                "QuestionType": "MC",
                "QuestionText": "Anchor A",
            },
            "QID2": {
                "QuestionID": "QID2",
                "QuestionType": "MC",
                "QuestionText": "Anchor B",
            },
        },
        "Blocks": {
            "BL_MAIN": {
                "Type": "Standard",
                "BlockElements": [
                    {"Type": "Question", "QuestionID": "QID1"},
                    {"Type": "Question", "QuestionID": "QID2"},
                ],
            }
        },
        "SurveyFlow": {"Flow": [{"Type": "Block", "ID": "BL_MAIN"}]},
    }
    after_create = {
        "Questions": {
            **initial["Questions"],
            "QID101": {"QuestionID": "QID101", "QuestionType": "MC"},
            "QID102": {"QuestionID": "QID102", "QuestionType": "MC"},
        },
        "Blocks": {
            "BL_MAIN": {
                "Type": "Standard",
                "BlockElements": [
                    {"Type": "Question", "QuestionID": "QID1"},
                    {"Type": "Question", "QuestionID": "QID2"},
                ],
            },
            "BL_AUTO": {
                "Type": "Standard",
                "BlockElements": [
                    {"Type": "Question", "QuestionID": "QID101"},
                    {"Type": "Question", "QuestionID": "QID102"},
                ],
            },
        },
        "SurveyFlow": {
            "Flow": [{"Type": "Block", "ID": "BL_MAIN"}, {"Type": "Block", "ID": "BL_AUTO"}]
        },
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

    fetch_calls = {"n": 0}

    def _fetch(*_a, **_k):
        fetch_calls["n"] += 1
        return initial if fetch_calls["n"] == 1 else after_create

    monkeypatch.setattr(cli_survey, "fetch_survey_definition", _fetch)

    created_ids = iter(["QID101", "QID102"])
    updated_blocks: dict[str, dict[str, Any]] = {}

    def _send_api_request(*, method: str, path: str, json=None, **_kwargs):
        if method == "GET" and path == "surveys/SV_TEST/languages":
            return _Resp({"result": {"AvailableLanguages": {"EN": True}}})
        if method == "POST" and path == "survey-definitions/SV_TEST/questions":
            return _Resp({"result": {"QuestionID": next(created_ids)}})
        if method == "PUT" and path.startswith("survey-definitions/SV_TEST/blocks/"):
            block_id = Path(path).name
            updated_blocks[block_id] = json
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
        from_question_id="QID1",
        question_json=None,
        question_text=["Item A", "Item B"],
        question_text_file=None,
        source_account=None,
        source_survey_id=None,
        source_question_id=None,
        from_scratch_mcq=False,
        from_scratch_type=None,
        choice_text=None,
        choice_text_file=None,
        statement_text=None,
        statement_text_file=None,
        mc_multi_response=False,
        target_block_id="BL_MAIN",
        after_qid=None,
        before_qid=None,
        position="append",
        insert_index=1,
        page_break_mode="between",
        data_export_tag=None,
        allow_duplicate_tags=False,
        dry_run=False,
        force_live=False,
        yes=True,
        no_publish=True,
        publish_description=None,
        account=None,
        interactive_mode=False,
    )

    cli_survey.handle_add_question(args)

    block = updated_blocks["BL_MAIN"]
    rendered = []
    for elem in block.get("BlockElements", []):
        etype = str(elem.get("Type") or "")
        if etype == "Question":
            rendered.append(str(elem.get("QuestionID") or ""))
        else:
            rendered.append(etype)
    assert rendered == ["QID1", "QID101", "Page Break", "QID102", "QID2"]


def test_add_page_break_inserts_at_explicit_index(monkeypatch) -> None:
    from qsync import cli_survey

    definition = {
        "Questions": {
            "QID1": {"QuestionID": "QID1", "QuestionType": "MC"},
            "QID2": {"QuestionID": "QID2", "QuestionType": "MC"},
        },
        "Blocks": {
            "BL_MAIN": {
                "Type": "Standard",
                "BlockElements": [
                    {"Type": "Question", "QuestionID": "QID1"},
                    {"Type": "Question", "QuestionID": "QID2"},
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
    monkeypatch.setattr(
        cli_survey,
        "_preflight_question_writes",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        cli_survey,
        "fetch_survey_definition",
        lambda *_a, **_k: definition,
    )
    monkeypatch.setattr(
        cli_survey,
        "_refresh_cache_after_question_write",
        lambda **_kwargs: None,
    )

    updated_block: dict[str, Any] = {}

    def _send_api_request(*, method: str, path: str, json=None, **_kwargs):
        if method == "PUT" and path == "survey-definitions/SV_TEST/blocks/BL_MAIN":
            updated_block.clear()
            updated_block.update(json or {})
            return _Resp({"result": {"ok": True}})
        raise AssertionError(f"Unexpected API call: {method} {path}")

    monkeypatch.setattr(cli_survey, "send_api_request", _send_api_request)

    args = argparse.Namespace(
        survey_id="SV_TEST",
        target_block_id="BL_MAIN",
        after_qid=None,
        before_qid=None,
        position="append",
        insert_index=1,
        dry_run=False,
        force_live=False,
        yes=True,
        no_publish=True,
        publish_description=None,
        account=None,
        interactive_mode=False,
    )

    cli_survey.handle_add_page_break(args)

    rendered = []
    for elem in updated_block.get("BlockElements", []):
        etype = str(elem.get("Type") or "")
        if etype == "Question":
            rendered.append(str(elem.get("QuestionID") or ""))
        else:
            rendered.append(etype)
    assert rendered == ["QID1", "Page Break", "QID2"]


def test_remove_page_break_removes_selected_indices(monkeypatch) -> None:
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
                    {"Type": "Page Break"},
                    {"Type": "Question", "QuestionID": "QID2"},
                    {"Type": "Page Break"},
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
    monkeypatch.setattr(
        cli_survey,
        "_preflight_question_writes",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        cli_survey,
        "fetch_survey_definition",
        lambda *_a, **_k: definition,
    )
    monkeypatch.setattr(
        cli_survey,
        "_refresh_cache_after_question_write",
        lambda **_kwargs: None,
    )

    updated_block: dict[str, Any] = {}

    def _send_api_request(*, method: str, path: str, json=None, **_kwargs):
        if method == "PUT" and path == "survey-definitions/SV_TEST/blocks/BL_MAIN":
            updated_block.clear()
            updated_block.update(json or {})
            return _Resp({"result": {"ok": True}})
        raise AssertionError(f"Unexpected API call: {method} {path}")

    monkeypatch.setattr(cli_survey, "send_api_request", _send_api_request)

    args = argparse.Namespace(
        survey_id="SV_TEST",
        target_block_id="BL_MAIN",
        element_index=["1", "3"],
        dry_run=False,
        force_live=False,
        yes=True,
        no_publish=True,
        publish_description=None,
        account=None,
        interactive_mode=False,
    )

    cli_survey.handle_remove_page_break(args)

    rendered = []
    for elem in updated_block.get("BlockElements", []):
        etype = str(elem.get("Type") or "")
        if etype == "Question":
            rendered.append(str(elem.get("QuestionID") or ""))
        else:
            rendered.append(etype)
    assert rendered == ["QID1", "QID2", "QID3"]


def test_remove_page_break_rejects_non_page_break_index(monkeypatch) -> None:
    from qsync import cli_survey

    definition = {
        "Questions": {
            "QID1": {"QuestionID": "QID1", "QuestionType": "MC"},
            "QID2": {"QuestionID": "QID2", "QuestionType": "MC"},
        },
        "Blocks": {
            "BL_MAIN": {
                "Type": "Standard",
                "BlockElements": [
                    {"Type": "Question", "QuestionID": "QID1"},
                    {"Type": "Page Break"},
                    {"Type": "Question", "QuestionID": "QID2"},
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
    monkeypatch.setattr(
        cli_survey,
        "_preflight_question_writes",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        cli_survey,
        "fetch_survey_definition",
        lambda *_a, **_k: definition,
    )

    args = argparse.Namespace(
        survey_id="SV_TEST",
        target_block_id="BL_MAIN",
        element_index=["0"],
        dry_run=False,
        force_live=False,
        yes=True,
        no_publish=True,
        publish_description=None,
        account=None,
        interactive_mode=False,
    )

    with pytest.raises(SystemExit) as excinfo:
        cli_survey.handle_remove_page_break(args)

    assert "expected 'Page Break'" in str(excinfo.value)


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


def test_move_question_insert_index_adjusts_for_removed_items(monkeypatch) -> None:
    from qsync import cli_survey

    definition = {
        "Questions": {
            "QID1": {"QuestionID": "QID1", "QuestionType": "MC"},
            "QID2": {"QuestionID": "QID2", "QuestionType": "MC"},
            "QID3": {"QuestionID": "QID3", "QuestionType": "MC"},
            "QID4": {"QuestionID": "QID4", "QuestionType": "MC"},
        },
        "Blocks": {
            "BL_MAIN": {
                "Type": "Standard",
                "BlockElements": [
                    {"Type": "Question", "QuestionID": "QID1"},
                    {"Type": "Question", "QuestionID": "QID2"},
                    {"Type": "Question", "QuestionID": "QID3"},
                    {"Type": "Question", "QuestionID": "QID4"},
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

    captured_block: dict[str, Any] = {}

    def _send_api_request(*, method: str, path: str, json=None, **_kwargs):
        if method == "PUT" and path == "survey-definitions/SV_TEST/blocks/BL_MAIN":
            captured_block.clear()
            captured_block.update(json or {})
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
        question_id=["QID2"],
        target_block_id="BL_MAIN",
        after_qid=None,
        before_qid=None,
        position="append",
        insert_index=3,
        dry_run=False,
        force_live=False,
        yes=True,
        no_publish=True,
        publish_description=None,
        account=None,
    )

    cli_survey.handle_move_question(args)

    qids = [
        str(elem.get("QuestionID") or "")
        for elem in captured_block.get("BlockElements", [])
        if str(elem.get("Type") or "") == "Question"
    ]
    assert qids == ["QID1", "QID3", "QID2", "QID4"]


def test_move_question_before_anchor_same_block_keeps_correct_order(monkeypatch) -> None:
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

    captured_block: dict[str, Any] = {}

    def _send_api_request(*, method: str, path: str, json=None, **_kwargs):
        if method == "PUT" and path == "survey-definitions/SV_TEST/blocks/BL_MAIN":
            captured_block.clear()
            captured_block.update(json or {})
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
        question_id=["QID2"],
        target_block_id=None,
        after_qid=None,
        before_qid="QID3",
        position="append",
        dry_run=False,
        force_live=False,
        yes=True,
        no_publish=True,
        publish_description=None,
        account=None,
    )

    cli_survey.handle_move_question(args)

    qids = [
        str(elem.get("QuestionID") or "")
        for elem in captured_block.get("BlockElements", [])
    ]
    assert qids == ["QID1", "QID2", "QID3"]


def test_remove_question_dry_run_does_not_write(monkeypatch) -> None:
    from qsync import cli_survey

    definition = {
        "Questions": {
            "QID1": {"QuestionID": "QID1", "QuestionType": "MC"},
            "QID2": {"QuestionID": "QID2", "QuestionType": "MC"},
        },
        "Blocks": {
            "BL_MAIN": {
                "Type": "Standard",
                "BlockElements": [
                    {"Type": "Question", "QuestionID": "QID1"},
                    {"Type": "Question", "QuestionID": "QID2"},
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
    monkeypatch.setattr(
        cli_survey,
        "_preflight_question_writes",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        cli_survey,
        "fetch_survey_definition",
        lambda *_a, **_k: definition,
    )

    writes: list[str] = []
    monkeypatch.setattr(
        cli_survey,
        "send_api_request",
        lambda *_a, **_k: writes.append("write") or _Resp({"result": {}}),
    )

    args = argparse.Namespace(
        survey_id="SV_TEST",
        question_id=["QID2"],
        dry_run=True,
        force_live=False,
        yes=False,
        no_publish=True,
        publish_description=None,
        account=None,
        interactive_mode=False,
    )

    cli_survey.handle_remove_question(args)
    assert writes == []


def test_remove_question_updates_blocks_and_moves_qids_to_trash(monkeypatch) -> None:
    from qsync import cli_survey

    definition = {
        "Questions": {
            "QID1": {"QuestionID": "QID1", "QuestionType": "MC"},
            "QID2": {"QuestionID": "QID2", "QuestionType": "MC"},
            "QID3": {"QuestionID": "QID3", "QuestionType": "MC"},
        },
        "Blocks": {
            "BL_A": {
                "Type": "Standard",
                "BlockElements": [
                    {"Type": "Question", "QuestionID": "QID1"},
                    {"Type": "Question", "QuestionID": "QID2"},
                ],
            },
            "BL_B": {
                "Type": "Standard",
                "BlockElements": [
                    {"Type": "Question", "QuestionID": "QID3"},
                ],
            },
            "BL_TRASH": {
                "Type": "Trash",
                "BlockElements": [
                    {"Type": "Question", "QuestionID": "QID999"},
                ],
            },
        },
        "SurveyFlow": {
            "Flow": [
                {"Type": "Block", "ID": "BL_A"},
                {"Type": "Block", "ID": "BL_B"},
            ]
        },
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
    monkeypatch.setattr(
        cli_survey,
        "_preflight_question_writes",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        cli_survey,
        "fetch_survey_definition",
        lambda *_a, **_k: definition,
    )
    monkeypatch.setattr(
        cli_survey,
        "_refresh_cache_after_question_write",
        lambda **_kwargs: None,
    )

    block_updates: dict[str, dict[str, Any]] = {}
    delete_calls: list[str] = []

    def _send_api_request(*, method: str, path: str, json=None, **_kwargs):
        if method == "PUT" and path.startswith("survey-definitions/SV_TEST/blocks/"):
            block_id = Path(path).name
            block_updates[block_id] = json or {}
            return _Resp({"result": {"ok": True}})
        if method == "DELETE" and path.startswith("survey-definitions/SV_TEST/questions/"):
            delete_calls.append(path)
            return _Resp({"result": {"ok": True}})
        raise AssertionError(f"Unexpected API call: {method} {path}")

    monkeypatch.setattr(cli_survey, "send_api_request", _send_api_request)

    args = argparse.Namespace(
        survey_id="SV_TEST",
        question_id=["QID2", "QID3"],
        dry_run=False,
        force_live=False,
        yes=True,
        no_publish=True,
        publish_description=None,
        account=None,
        interactive_mode=False,
    )

    cli_survey.handle_remove_question(args)

    assert set(block_updates.keys()) == {"BL_A", "BL_B", "BL_TRASH"}
    block_a_qids = [
        str(elem.get("QuestionID") or "")
        for elem in block_updates["BL_A"].get("BlockElements", [])
        if str(elem.get("Type") or "") == "Question"
    ]
    block_b_qids = [
        str(elem.get("QuestionID") or "")
        for elem in block_updates["BL_B"].get("BlockElements", [])
        if str(elem.get("Type") or "") == "Question"
    ]
    trash_qids = [
        str(elem.get("QuestionID") or "")
        for elem in block_updates["BL_TRASH"].get("BlockElements", [])
        if str(elem.get("Type") or "") == "Question"
    ]
    assert block_a_qids == ["QID1"]
    assert block_b_qids == []
    assert trash_qids == ["QID999", "QID2", "QID3"]
    assert delete_calls == []


def test_add_question_cross_account_preserves_source_order_and_filters_languages(
    monkeypatch,
) -> None:
    from qsync import cli_survey

    target_initial = {
        "Questions": {
            "QID1": {
                "QuestionID": "QID1",
                "QuestionType": "MC",
                "QuestionText": "Anchor",
            }
        },
        "Blocks": {
            "BL_MAIN": {
                "Type": "Standard",
                "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
            }
        },
        "SurveyFlow": {"Flow": [{"Type": "Block", "ID": "BL_MAIN"}]},
    }
    target_after_create = {
        "Questions": {
            **target_initial["Questions"],
            "QID101": {"QuestionID": "QID101", "QuestionType": "MC"},
            "QID102": {"QuestionID": "QID102", "QuestionType": "MC"},
        },
        "Blocks": {
            "BL_MAIN": {
                "Type": "Standard",
                "BlockElements": [
                    {"Type": "Question", "QuestionID": "QID1"},
                ],
            },
            "BL_AUTO": {
                "Type": "Standard",
                "BlockElements": [
                    {"Type": "Question", "QuestionID": "QID101"},
                    {"Type": "Question", "QuestionID": "QID102"},
                ],
            },
        },
        "SurveyFlow": {
            "Flow": [{"Type": "Block", "ID": "BL_MAIN"}, {"Type": "Block", "ID": "BL_AUTO"}]
        },
    }
    source_definition = {
        "Questions": {
            "QID10": {
                "QuestionID": "QID10",
                "QuestionType": "MC",
                "Selector": "SAVR",
                "QuestionText": "Source A",
                "Language": {"FR": {"QuestionText": "Source A FR"}, "DE": {"QuestionText": "Source A DE"}},
            },
            "QID11": {
                "QuestionID": "QID11",
                "QuestionType": "MC",
                "Selector": "SAVR",
                "QuestionText": "Source B",
                "Language": {"FR": {"QuestionText": "Source B FR"}, "DE": {"QuestionText": "Source B DE"}},
            },
        },
        "Blocks": {},
        "SurveyFlow": {"Flow": []},
    }

    monkeypatch.setattr(
        cli_survey,
        "_prompt_for_survey_id_api_if_needed",
        lambda **_kwargs: "SV_TARGET",
    )
    monkeypatch.setattr(
        cli_survey,
        "_get_client_config_for_args",
        lambda _args: ("target.qualtrics.com", {"X-API-TOKEN": "t"}),
    )
    monkeypatch.setattr(cli_survey, "ensure_unlocked", lambda _sid: None)
    monkeypatch.setattr(cli_survey, "load_push_context", lambda *_a, **_k: _PushCtx())
    monkeypatch.setattr(
        cli_survey,
        "load_account_env",
        lambda account, root=None: {"QUALTRICS_BASE_URL": f"{account}.qualtrics.com", "X-API-TOKEN": "s"},
    )
    monkeypatch.setattr(
        cli_survey,
        "get_client_config",
        lambda env=None: (
            str((env or {}).get("QUALTRICS_BASE_URL") or "target.qualtrics.com"),
            {"X-API-TOKEN": str((env or {}).get("X-API-TOKEN") or "t")},
        ),
    )

    fetch_calls = {"target": 0}

    def _fetch(base_url, _headers, survey_id):
        if survey_id == "SV_SOURCE":
            return source_definition
        if survey_id != "SV_TARGET":
            raise AssertionError(f"Unexpected survey fetch: {survey_id}")
        fetch_calls["target"] += 1
        return target_initial if fetch_calls["target"] == 1 else target_after_create

    monkeypatch.setattr(cli_survey, "fetch_survey_definition", _fetch)

    created_ids = iter(["QID101", "QID102"])
    created_payloads: list[dict[str, Any]] = []
    updated_block: dict[str, Any] = {}

    def _send_api_request(*, method: str, path: str, json=None, **_kwargs):
        if method == "GET" and path == "surveys/SV_TARGET/languages":
            return _Resp({"result": {"AvailableLanguages": {"EN": True, "FR": True}}})
        if method == "POST" and path == "survey-definitions/SV_TARGET/questions":
            created_payloads.append(json or {})
            return _Resp({"result": {"QuestionID": next(created_ids)}})
        if method == "PUT" and path == "survey-definitions/SV_TARGET/blocks/BL_MAIN":
            updated_block.clear()
            updated_block.update(json or {})
            return _Resp({"result": {"ok": True}})
        if method == "PUT" and path == "survey-definitions/SV_TARGET/blocks/BL_AUTO":
            return _Resp({"result": {"ok": True}})
        raise AssertionError(f"Unexpected API call: {method} {path}")

    monkeypatch.setattr(cli_survey, "send_api_request", _send_api_request)
    monkeypatch.setattr(
        cli_survey,
        "download_survey_definition",
        lambda survey_id, **_kwargs: Path(f"/tmp/{survey_id}.json"),
    )

    args = argparse.Namespace(
        survey_id="SV_TARGET",
        from_question_id=None,
        question_json=None,
        source_account="linda",
        source_survey_id="SV_SOURCE",
        source_question_id=["QID11", "QID10"],
        from_scratch_mcq=False,
        choice_text=None,
        choice_text_file=None,
        mc_multi_response=False,
        question_text=None,
        question_text_file=None,
        target_block_id=None,
        after_qid="QID1",
        before_qid=None,
        position="append",
        data_export_tag=None,
        allow_duplicate_tags=False,
        dry_run=False,
        force_live=False,
        yes=True,
        no_publish=True,
        publish_description=None,
        account=None,
        interactive_mode=False,
    )

    cli_survey.handle_add_question(args)

    assert [p.get("QuestionText") for p in created_payloads] == ["Source B", "Source A"]
    assert created_payloads[0].get("Language") == {"FR": {"QuestionText": "Source B FR"}}
    assert created_payloads[1].get("Language") == {"FR": {"QuestionText": "Source A FR"}}
    qids = [
        str(elem.get("QuestionID") or "")
        for elem in updated_block.get("BlockElements", [])
    ]
    assert qids == ["QID1", "QID101", "QID102"]


def test_add_question_cross_account_normalizes_malformed_language_entries(
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from qsync import cli_survey

    target_initial = {
        "Questions": {
            "QID1": {
                "QuestionID": "QID1",
                "QuestionType": "MC",
                "QuestionText": "Anchor",
            }
        },
        "Blocks": {
            "BL_MAIN": {
                "Type": "Standard",
                "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
            }
        },
        "SurveyFlow": {"Flow": [{"Type": "Block", "ID": "BL_MAIN"}]},
    }
    target_after_create = {
        "Questions": {
            **target_initial["Questions"],
            "QID101": {"QuestionID": "QID101", "QuestionType": "MC"},
        },
        "Blocks": {
            "BL_MAIN": {
                "Type": "Standard",
                "BlockElements": [
                    {"Type": "Question", "QuestionID": "QID1"},
                ],
            },
            "BL_AUTO": {
                "Type": "Standard",
                "BlockElements": [{"Type": "Question", "QuestionID": "QID101"}],
            },
        },
        "SurveyFlow": {
            "Flow": [{"Type": "Block", "ID": "BL_MAIN"}, {"Type": "Block", "ID": "BL_AUTO"}]
        },
    }
    source_definition = {
        "Questions": {
            "QID10": {
                "QuestionID": "QID10",
                "QuestionType": "MC",
                "Selector": "SAVR",
                "QuestionText": "Source malformed language payload",
                "Language": {
                    "FR": "Texte FR legacy",
                    "DE": {"QuestionText": "Text DE should be dropped"},
                    "IT": ["bad-shape"],
                    "": {"QuestionText": "missing-key"},
                },
            }
        },
        "Blocks": {},
        "SurveyFlow": {"Flow": []},
    }

    monkeypatch.setattr(
        cli_survey,
        "_prompt_for_survey_id_api_if_needed",
        lambda **_kwargs: "SV_TARGET",
    )
    monkeypatch.setattr(
        cli_survey,
        "_get_client_config_for_args",
        lambda _args: ("target.qualtrics.com", {"X-API-TOKEN": "t"}),
    )
    monkeypatch.setattr(cli_survey, "ensure_unlocked", lambda _sid: None)
    monkeypatch.setattr(cli_survey, "load_push_context", lambda *_a, **_k: _PushCtx())
    monkeypatch.setattr(
        cli_survey,
        "load_account_env",
        lambda account, root=None: {"QUALTRICS_BASE_URL": f"{account}.qualtrics.com", "X-API-TOKEN": "s"},
    )
    monkeypatch.setattr(
        cli_survey,
        "get_client_config",
        lambda env=None: (
            str((env or {}).get("QUALTRICS_BASE_URL") or "target.qualtrics.com"),
            {"X-API-TOKEN": str((env or {}).get("X-API-TOKEN") or "t")},
        ),
    )

    fetch_calls = {"target": 0}

    def _fetch(base_url, _headers, survey_id):
        if survey_id == "SV_SOURCE":
            return source_definition
        if survey_id != "SV_TARGET":
            raise AssertionError(f"Unexpected survey fetch: {survey_id}")
        fetch_calls["target"] += 1
        return target_initial if fetch_calls["target"] == 1 else target_after_create

    monkeypatch.setattr(cli_survey, "fetch_survey_definition", _fetch)

    created_payloads: list[dict[str, Any]] = []

    def _send_api_request(*, method: str, path: str, json=None, **_kwargs):
        if method == "GET" and path == "surveys/SV_TARGET/languages":
            return _Resp({"result": {"AvailableLanguages": {"EN": True, "FR": True}}})
        if method == "POST" and path == "survey-definitions/SV_TARGET/questions":
            created_payloads.append(json or {})
            return _Resp({"result": {"QuestionID": "QID101"}})
        if method == "PUT" and path in {
            "survey-definitions/SV_TARGET/blocks/BL_MAIN",
            "survey-definitions/SV_TARGET/blocks/BL_AUTO",
        }:
            return _Resp({"result": {"ok": True}})
        raise AssertionError(f"Unexpected API call: {method} {path}")

    monkeypatch.setattr(cli_survey, "send_api_request", _send_api_request)
    monkeypatch.setattr(
        cli_survey,
        "download_survey_definition",
        lambda survey_id, **_kwargs: Path(f"/tmp/{survey_id}.json"),
    )

    args = argparse.Namespace(
        survey_id="SV_TARGET",
        from_question_id=None,
        question_json=None,
        source_account="linda",
        source_survey_id="SV_SOURCE",
        source_question_id=["QID10"],
        from_scratch_mcq=False,
        choice_text=None,
        choice_text_file=None,
        mc_multi_response=False,
        question_text=None,
        question_text_file=None,
        target_block_id=None,
        after_qid="QID1",
        before_qid=None,
        position="append",
        data_export_tag=None,
        allow_duplicate_tags=False,
        dry_run=False,
        force_live=False,
        yes=True,
        no_publish=True,
        publish_description=None,
        account=None,
        interactive_mode=False,
    )

    cli_survey.handle_add_question(args)

    assert created_payloads
    assert created_payloads[0].get("Language") == {
        "FR": {"QuestionText": "Texte FR legacy"}
    }
    out = capsys.readouterr().out
    assert "malformed translation language block entries" in out


def test_resolve_source_client_default_uses_workspace_dotenv(
    monkeypatch, tmp_path: Path
) -> None:
    from qsync import cli_survey

    monkeypatch.setattr(cli_survey, "_workspace_root", lambda: tmp_path.resolve())
    monkeypatch.setattr(
        cli_survey,
        "load_env",
        lambda path=None: {
            "QUALTRICS_BASE_URL": "default.example.qualtrics.com",
            "X-API-TOKEN": "default-token",
        },
    )
    monkeypatch.setattr(
        cli_survey,
        "get_client_config",
        lambda env=None: (
            str((env or {}).get("QUALTRICS_BASE_URL") or ""),
            {"X-API-TOKEN": str((env or {}).get("X-API-TOKEN") or "")},
        ),
    )
    monkeypatch.setattr(
        cli_survey,
        "load_account_env",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError(
                "load_account_env should not be called for source_account=default"
            )
        ),
    )

    args = argparse.Namespace(source_account="default")
    base_url, headers = cli_survey._resolve_source_client_for_add_question(
        args=args,
        target_base_url="target.example.qualtrics.com",
        target_headers={"X-API-TOKEN": "target-token"},
    )

    assert base_url == "default.example.qualtrics.com"
    assert headers["X-API-TOKEN"] == "default-token"


def test_add_question_from_scratch_mcq_dry_run(monkeypatch) -> None:
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
    monkeypatch.setattr(
        cli_survey,
        "send_api_request",
        lambda *_a, **_k: writes.append("write") or _Resp({"result": {}}),
    )

    args = argparse.Namespace(
        survey_id="SV_TEST",
        from_question_id=None,
        question_json=None,
        source_account=None,
        source_survey_id=None,
        source_question_id=None,
        from_scratch_mcq=True,
        choice_text=["Yes", "No"],
        choice_text_file=None,
        mc_multi_response=False,
        question_text=["Do you agree?"],
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
        interactive_mode=False,
    )

    cli_survey.handle_add_question(args)
    assert writes == []


def test_add_question_from_scratch_te_dry_run(monkeypatch) -> None:
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
    monkeypatch.setattr(
        cli_survey,
        "send_api_request",
        lambda *_a, **_k: writes.append("write") or _Resp({"result": {}}),
    )

    args = argparse.Namespace(
        survey_id="SV_TEST",
        from_question_id=None,
        question_json=None,
        source_account=None,
        source_survey_id=None,
        source_question_id=None,
        from_scratch_mcq=False,
        from_scratch_type="te",
        choice_text=None,
        choice_text_file=None,
        statement_text=None,
        statement_text_file=None,
        mc_multi_response=False,
        question_text=["Please describe your experience."],
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
        interactive_mode=False,
    )

    cli_survey.handle_add_question(args)
    assert writes == []


def test_add_question_from_scratch_matrix_dry_run(monkeypatch) -> None:
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
    monkeypatch.setattr(
        cli_survey,
        "send_api_request",
        lambda *_a, **_k: writes.append("write") or _Resp({"result": {}}),
    )

    args = argparse.Namespace(
        survey_id="SV_TEST",
        from_question_id=None,
        question_json=None,
        source_account=None,
        source_survey_id=None,
        source_question_id=None,
        from_scratch_mcq=False,
        from_scratch_type="matrix",
        choice_text=["Strongly disagree", "Disagree", "Agree", "Strongly agree"],
        choice_text_file=None,
        statement_text=["Statement A", "Statement B"],
        statement_text_file=None,
        mc_multi_response=False,
        question_text=["How much do you agree with these statements?"],
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
        interactive_mode=False,
    )

    cli_survey.handle_add_question(args)
    assert writes == []


def test_add_question_interactive_force_live_override(monkeypatch) -> None:
    from qsync import cli_survey

    initial = _base_definition()
    after_create = _base_definition()
    after_create["Questions"]["QID16"] = {
        "QuestionID": "QID16",
        "QuestionType": "MC",
        "QuestionText": "Template question",
    }
    after_create["Blocks"]["BL_AUTO"] = {
        "Type": "Standard",
        "BlockElements": [{"Type": "Question", "QuestionID": "QID16"}],
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
    monkeypatch.setattr(
        cli_survey,
        "load_push_context",
        lambda *_a, **_k: _PushCtx(response_count=4),
    )

    fetch_calls = {"n": 0}

    def _fetch(*_a, **_k):
        fetch_calls["n"] += 1
        return initial if fetch_calls["n"] == 1 else after_create

    monkeypatch.setattr(cli_survey, "fetch_survey_definition", _fetch)
    monkeypatch.setattr(
        "qsync.interactive_menu.select_from_list",
        lambda message, choices, instruction=None, default=None: (
            "Continue with override"
            if "finished response" in message
            else choices[0]
        ),
    )

    created = {"count": 0}

    def _send_api_request(*, method: str, path: str, json=None, **_kwargs):
        if method == "GET" and path == "surveys/SV_TEST/languages":
            return _Resp({"result": {"AvailableLanguages": {"EN": True}}})
        if method == "POST" and path == "survey-definitions/SV_TEST/questions":
            created["count"] += 1
            return _Resp({"result": {"QuestionID": "QID16"}})
        if method == "PUT" and path.startswith("survey-definitions/SV_TEST/blocks/"):
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
        from_question_id="QID15",
        question_json=None,
        source_account=None,
        source_survey_id=None,
        source_question_id=None,
        from_scratch_mcq=False,
        choice_text=None,
        choice_text_file=None,
        mc_multi_response=False,
        question_text=None,
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
        no_publish=True,
        publish_description=None,
        account=None,
        interactive_mode=True,
    )

    cli_survey.handle_add_question(args)
    assert created["count"] == 1
