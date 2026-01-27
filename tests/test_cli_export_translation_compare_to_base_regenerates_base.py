from __future__ import annotations

import argparse
from pathlib import Path


def test_export_translation_compare_to_base_also_exports_base(monkeypatch, tmp_path: Path) -> None:
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
    monkeypatch.setattr("qsync.translation_export.export_survey_to_pdf", lambda *a, **k: tmp_path / "out.pdf")

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

    assert {"fmt": "docx", "survey_id": "SV_TEST", "render_language": "FR", "compare_to_base": True} in calls
    assert {"fmt": "docx", "survey_id": "SV_TEST", "render_language": None, "compare_to_base": False} in calls


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
    monkeypatch.setattr("qsync.translation_export.export_survey_to_pdf", lambda *a, **k: tmp_path / "out.pdf")

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
    assert ( "FR", True) in calls
    assert ( "NL", True) in calls

