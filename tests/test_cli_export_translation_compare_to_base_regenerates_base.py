from __future__ import annotations

import argparse
from pathlib import Path

import pytest


def test_export_translation_compare_to_base_also_exports_base(
    monkeypatch, tmp_path: Path
) -> None:
    from qsync.cli_survey import handle_export_translation

    calls: list[dict] = []

    def fake_word(
        survey_id: str,
        output_path=None,
        *,
        render_language=None,
        compare_to_base=False,
        **kwargs,
    ):
        calls.append(
            {
                "fmt": "docx",
                "survey_id": survey_id,
                "render_language": render_language,
                "compare_to_base": bool(compare_to_base),
            }
        )
        return tmp_path / f"out_{len(calls)}.docx"

    monkeypatch.setattr("qsync.translation_export.export_survey_to_word", fake_word)
    monkeypatch.setattr(
        "qsync.translation_export.export_survey_to_pdf",
        lambda *a, **k: tmp_path / "out.pdf",
    )

    args = argparse.Namespace(
        survey_id="SV_TEST",
        output=None,
        no_html=False,
        edf=[],
        smart_name=False,
        open=False,
        compare_to_base=True,
        refresh=False,
        layout_heuristics=False,
        format="docx",
        language="FR",
        languages=None,
        skip_js_strings=False,
    )

    handle_export_translation(args)

    assert {
        "fmt": "docx",
        "survey_id": "SV_TEST",
        "render_language": "FR",
        "compare_to_base": True,
    } in calls
    assert {
        "fmt": "docx",
        "survey_id": "SV_TEST",
        "render_language": None,
        "compare_to_base": False,
    } in calls


def test_export_translation_compare_to_base_exports_base_once_for_multiple_languages(
    monkeypatch, tmp_path: Path
) -> None:
    from qsync.cli_survey import handle_export_translation

    calls: list[tuple[str | None, bool]] = []

    def fake_word(
        survey_id: str,
        output_path=None,
        *,
        render_language=None,
        compare_to_base=False,
        **kwargs,
    ):
        calls.append((render_language, bool(compare_to_base)))
        return tmp_path / f"out_{len(calls)}.docx"

    monkeypatch.setattr("qsync.translation_export.export_survey_to_word", fake_word)
    monkeypatch.setattr(
        "qsync.translation_export.export_survey_to_pdf",
        lambda *a, **k: tmp_path / "out.pdf",
    )

    args = argparse.Namespace(
        survey_id="SV_TEST",
        output=None,
        no_html=False,
        edf=[],
        smart_name=False,
        open=False,
        compare_to_base=True,
        refresh=False,
        layout_heuristics=False,
        format="docx",
        language=None,
        languages="FR,NL",
        skip_js_strings=False,
    )

    handle_export_translation(args)

    assert calls.count((None, False)) == 1
    assert ("FR", True) in calls
    assert ("NL", True) in calls


def test_export_translation_defaults_skip_js_strings_when_missing(monkeypatch, tmp_path: Path) -> None:
    from qsync.cli_survey import handle_export_translation

    captured_kwargs: dict[str, bool] = {}

    def fake_word(
        survey_id: str,
        output_path=None,
        *,
        render_language=None,
        compare_to_base=False,
        include_js_strings: bool = True,
        **kwargs,
    ):
        captured_kwargs["include_js_strings"] = bool(include_js_strings)
        return tmp_path / "out.docx"

    monkeypatch.setattr("qsync.translation_export.export_survey_to_word", fake_word)
    monkeypatch.setattr(
        "qsync.translation_export.export_survey_to_pdf",
        lambda *a, **k: tmp_path / "out.pdf",
    )

    # Simulate callsites that build a partial namespace (e.g., survey menu path).
    args = argparse.Namespace(
        survey_id="SV_TEST",
        output=None,
        no_html=False,
        edf=[],
        smart_name=False,
        open=False,
        compare_to_base=False,
        refresh=False,
        layout_heuristics=False,
        format="docx",
        language=None,
        languages=None,
        edf_preset=None,
        list_edf_presets=False,
        flow_trace=False,
    )

    handle_export_translation(args)

    assert captured_kwargs["include_js_strings"] is True


def test_export_translation_forwards_include_filters(monkeypatch, tmp_path: Path) -> None:
    from qsync.cli_survey import handle_export_translation

    captured: dict[str, object] = {}

    def fake_word(
        survey_id: str,
        output_path=None,
        *,
        include_qids=None,
        include_tags=None,
        include_blocks=None,
        **kwargs,
    ):
        captured["survey_id"] = survey_id
        captured["include_qids"] = set(include_qids or set())
        captured["include_tags"] = set(include_tags or set())
        captured["include_blocks"] = set(include_blocks or set())
        return tmp_path / "out.docx"

    monkeypatch.setattr("qsync.translation_export.export_survey_to_word", fake_word)
    monkeypatch.setattr(
        "qsync.translation_export.export_survey_to_pdf",
        lambda *a, **k: tmp_path / "out.pdf",
    )

    args = argparse.Namespace(
        survey_id="SV_TEST",
        output=None,
        no_html=False,
        edf=[],
        edf_preset=None,
        list_edf_presets=False,
        smart_name=False,
        open=False,
        compare_to_base=False,
        compare_with=None,
        refresh=False,
        layout_heuristics=False,
        format="docx",
        language=None,
        languages=None,
        include_qid=["QID1,QID2", "QID2"],
        include_tag=["news_tag,topic_tag"],
        block=["BL_1", "BL_2,BL_1"],
        skip_js_strings=False,
        flow_trace=False,
    )

    handle_export_translation(args)

    assert captured["survey_id"] == "SV_TEST"
    assert captured["include_qids"] == {"QID1", "QID2"}
    assert captured["include_tags"] == {"news_tag", "topic_tag"}
    assert captured["include_blocks"] == {"BL_1", "BL_2"}


def test_export_translation_compare_with_uses_side_by_side_docx(
    monkeypatch, tmp_path: Path
) -> None:
    from qsync.cli_survey import handle_export_translation

    captured: dict[str, object] = {}

    def fake_compare(survey_a: str, survey_b: str, **kwargs):
        captured["survey_a"] = survey_a
        captured["survey_b"] = survey_b
        captured["kwargs"] = dict(kwargs)
        return tmp_path / "compare.docx"

    monkeypatch.setattr(
        "qsync.translation_export.export_surveys_side_by_side_docx", fake_compare
    )
    monkeypatch.setattr("qsync.interactive_menu.is_interactive", lambda: True)

    args = argparse.Namespace(
        survey_id="SV_A",
        output=None,
        no_html=False,
        edf=[],
        edf_preset=None,
        list_edf_presets=False,
        smart_name=False,
        open=False,
        compare_to_base=False,
        compare_with="SV_B",
        refresh=True,
        layout_heuristics=False,
        format="docx",
        language=None,
        languages=None,
        include_qid=None,
        include_tag=None,
        block=None,
        skip_js_strings=False,
        flow_trace=False,
    )

    handle_export_translation(args)

    assert captured["survey_a"] == "SV_A"
    assert captured["survey_b"] == "SV_B"
    assert bool((captured["kwargs"] or {}).get("refresh")) is True


def test_export_translation_compare_with_rejects_scope_filters(
    monkeypatch, tmp_path: Path
) -> None:
    from qsync.cli_survey import handle_export_translation

    monkeypatch.setattr(
        "qsync.translation_export.export_surveys_side_by_side_docx",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("compare exporter should not run for invalid flag combo")
        ),
    )

    args = argparse.Namespace(
        survey_id="SV_A",
        output=None,
        no_html=False,
        edf=[],
        edf_preset=None,
        list_edf_presets=False,
        smart_name=False,
        open=False,
        compare_to_base=False,
        compare_with="SV_B",
        refresh=False,
        layout_heuristics=False,
        format="docx",
        language=None,
        languages=None,
        include_qid=["QID1"],
        include_tag=None,
        block=None,
        skip_js_strings=False,
        flow_trace=False,
    )

    with pytest.raises(SystemExit):
        handle_export_translation(args)
