from __future__ import annotations

import argparse
from pathlib import Path


def test_handle_publish_supports_multiple_surveys(monkeypatch) -> None:
    from qsync import cli_survey

    published: list[str] = []

    monkeypatch.setattr(
        cli_survey,
        "_prompt_for_survey_ids_api_if_needed",
        lambda **_kwargs: ["SV_ONE", "SV_TWO"],
    )
    monkeypatch.setattr(
        cli_survey,
        "_get_client_config_for_args",
        lambda _args: ("example.qualtrics.com", {"X-API-TOKEN": "x"}),
    )
    monkeypatch.setattr(
        cli_survey,
        "publish_survey_definition",
        lambda survey_id, **_kwargs: published.append(survey_id)
        or {"result": {"metadata": {"versionID": f"VER_{survey_id}"}}},
    )

    args = argparse.Namespace(
        survey_id=None,
        description="Batch publish",
        dry_run=False,
        retry_attempts=1,
        account=None,
    )

    cli_survey.handle_publish(args)
    assert published == ["SV_ONE", "SV_TWO"]


def test_handle_export_translation_supports_multiple_surveys(
    monkeypatch, tmp_path: Path
) -> None:
    from qsync import cli_survey

    exported: list[str] = []

    monkeypatch.setattr(
        cli_survey,
        "_prompt_for_survey_ids_api_if_needed",
        lambda **_kwargs: ["SV_ONE", "SV_TWO"],
    )
    monkeypatch.setattr(
        "qsync.interactive_menu.is_interactive",
        lambda: True,
    )
    monkeypatch.setattr(
        "qsync.translation_export.export_survey_to_word",
        lambda survey_id, **_kwargs: exported.append(survey_id)
        or (tmp_path / f"{survey_id}.docx"),
    )
    monkeypatch.setattr(
        "qsync.translation_export.export_survey_to_pdf",
        lambda *_args, **_kwargs: tmp_path / "unused.pdf",
    )

    args = argparse.Namespace(
        survey_id=None,
        output=None,
        no_html=False,
        edf=[],
        edf_preset=None,
        list_edf_presets=False,
        smart_name=False,
        open=False,
        compare_to_base=False,
        refresh=False,
        layout_heuristics=False,
        format="docx",
        language=None,
        languages=None,
        skip_js_strings=False,
        flow_trace=False,
    )

    cli_survey.handle_export_translation(args)
    assert exported == ["SV_ONE", "SV_TWO"]


def test_handle_export_translation_defaults_render_mermaid_disabled(
    monkeypatch, tmp_path: Path
) -> None:
    from qsync import cli_survey

    captured: dict[str, bool] = {}

    monkeypatch.setattr(
        cli_survey,
        "_prompt_for_survey_ids_api_if_needed",
        lambda **_kwargs: ["SV_ONE"],
    )
    monkeypatch.setattr(
        "qsync.interactive_menu.is_interactive",
        lambda: True,
    )

    def fake_word(survey_id, **kwargs):
        captured["render_mermaid"] = bool(kwargs.get("render_mermaid", False))
        return tmp_path / f"{survey_id}.docx"

    monkeypatch.setattr("qsync.translation_export.export_survey_to_word", fake_word)
    monkeypatch.setattr(
        "qsync.translation_export.export_survey_to_pdf",
        lambda *_args, **_kwargs: tmp_path / "unused.pdf",
    )

    args = argparse.Namespace(
        survey_id="SV_ONE",
        output=None,
        no_html=False,
        edf=[],
        edf_preset=None,
        list_edf_presets=False,
        smart_name=False,
        open=False,
        compare_to_base=False,
        refresh=False,
        layout_heuristics=False,
        format="docx",
        language=None,
        languages=None,
        skip_js_strings=False,
        flow_trace=False,
    )

    cli_survey.handle_export_translation(args)
    assert captured["render_mermaid"] is False


def test_handle_export_translation_forwards_render_mermaid(
    monkeypatch, tmp_path: Path
) -> None:
    from qsync import cli_survey

    captured: dict[str, bool] = {}

    monkeypatch.setattr(
        cli_survey,
        "_prompt_for_survey_ids_api_if_needed",
        lambda **_kwargs: ["SV_ONE"],
    )
    monkeypatch.setattr(
        "qsync.interactive_menu.is_interactive",
        lambda: True,
    )

    def fake_word(survey_id, **kwargs):
        captured["render_mermaid"] = bool(kwargs.get("render_mermaid", False))
        return tmp_path / f"{survey_id}.docx"

    monkeypatch.setattr("qsync.translation_export.export_survey_to_word", fake_word)
    monkeypatch.setattr(
        "qsync.translation_export.export_survey_to_pdf",
        lambda *_args, **_kwargs: tmp_path / "unused.pdf",
    )

    args = argparse.Namespace(
        survey_id="SV_ONE",
        output=None,
        no_html=False,
        edf=[],
        edf_preset=None,
        list_edf_presets=False,
        smart_name=False,
        open=False,
        compare_to_base=False,
        refresh=False,
        layout_heuristics=False,
        render_mermaid=True,
        format="docx",
        language=None,
        languages=None,
        skip_js_strings=False,
        flow_trace=False,
    )

    cli_survey.handle_export_translation(args)
    assert captured["render_mermaid"] is True
