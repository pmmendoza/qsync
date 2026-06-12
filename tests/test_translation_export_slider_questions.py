from __future__ import annotations

from pathlib import Path

import pytest


def _build_slider_and_cs_payload() -> dict:
    return {
        "result": {
            "SurveyFlow": {"Flow": [{"Type": "Standard", "ID": "BL_1"}]},
            "Blocks": {
                "BL_1": {
                    "Type": "Default",
                    "Description": "Block",
                    "BlockElements": [
                        {"Type": "Question", "QuestionID": "QID_SLD"},
                        {"Type": "Question", "QuestionID": "QID_CS"},
                    ],
                }
            },
            "Questions": {
                "QID_SLD": {
                    "QuestionType": "Slider",
                    "Selector": "HSLIDER",
                    "QuestionText": "Slider question",
                    "DataExportTag": "sld_tag",
                    "Choices": {
                        "1": {"Display": "&nbsp;"},
                        "2": {"Display": "Statement A"},
                    },
                    "ChoiceOrder": ["1", "2"],
                    "Labels": {
                        "1": {"Display": "Left"},
                        "2": {"Display": "Right"},
                    },
                    "Configuration": {
                        "CSSliderMin": -5,
                        "CSSliderMax": 5,
                        "GridLines": 10,
                        "SnapToGrid": True,
                        "NumDecimals": 0,
                        "ShowValue": False,
                    },
                },
                "QID_CS": {
                    "QuestionType": "CS",
                    "Selector": "HSLIDER",
                    "QuestionText": "Constant sum slider question",
                    "DataExportTag": "cs_tag",
                    "Choices": {
                        "1": {"Display": "Topic A"},
                        "2": {"Display": "Topic B"},
                    },
                    "ChoiceOrder": ["1", "2"],
                    "Labels": {
                        "1": {"Display": "None"},
                        "2": {"Display": "All"},
                    },
                    "Configuration": {
                        "CSSliderMin": 0,
                        "CSSliderMax": 100,
                        "GridLines": 5,
                        "NumDecimals": 0,
                        "ShowValue": True,
                    },
                },
            },
        }
    }


def test_docx_slider_and_cs_render_statements_labels_and_scale(tmp_path: Path) -> None:
    docx = pytest.importorskip("docx")

    from qsync.translation_export import export_survey_payload_to_word

    payload = _build_slider_and_cs_payload()
    out_docx = tmp_path / "out.docx"
    export_survey_payload_to_word("SV_TEST", payload, out_docx)

    d = docx.Document(str(out_docx))
    text = "\n".join(
        cell.text
        for tbl in d.tables
        for row in tbl.rows
        for cell in row.cells
        if cell.text
    )

    # Slider: statements + labels + scale
    assert "Statement A" in text
    assert "Labels" in text
    assert "Left" in text
    assert "Right" in text
    assert "Scale" in text
    assert "Range: -5–5" in text

    # CS(+R): statements + labels + scale
    assert "Topic A" in text
    assert "Topic B" in text
    assert "Range: 0–100" in text


def test_pdf_html_includes_slider_statements_labels_and_scale(tmp_path: Path) -> None:
    from qsync.translation_export import ExportContent, _render_question_html_full

    payload = _build_slider_and_cs_payload()
    questions = payload["result"]["Questions"]

    content = ExportContent(
        survey_id="SV_TEST",
        survey_name="Test Survey",
        survey_title=None,
        survey_description=None,
        version_number=None,
        version_id=None,
        version_description=None,
        survey_payload=payload,
        survey_link="",
        active_qids={"QID_SLD", "QID_CS"},
        translation_ctx=None,
        render_plan=None,
        qid_to_js={},
        mermaid_code=None,
        mermaid_path=None,
        mermaid_image_path=None,
        mermaid_svg_path=None,
        include_mermaid=False,
        edf_overrides=None,
        include_html_source=False,
        layout_heuristics=False,
        compare_to_base=False,
        render_language=None,
        base_language="EN",
        output_path=tmp_path / "out.pdf",
        include_js_strings=True,
        flow_trace=None,
    )

    sld_html = _render_question_html_full("QID_SLD", questions, content, depth=0)
    assert "Statements" in sld_html
    assert "Statement A" in sld_html
    assert "Labels" in sld_html
    assert "Left" in sld_html
    assert "Right" in sld_html
    assert "Scale" in sld_html
    assert "Range: -5–5" in sld_html

    cs_html = _render_question_html_full("QID_CS", questions, content, depth=0)
    assert "Statements" in cs_html
    assert "Topic A" in cs_html
    assert "Topic B" in cs_html
    assert "Labels" in cs_html
    assert "Range: 0–100" in cs_html
