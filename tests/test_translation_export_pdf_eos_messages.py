from __future__ import annotations

from pathlib import Path


def test_pdf_html_end_survey_embeds_eos_message(monkeypatch, tmp_path: Path) -> None:
    from qsync.translation_export import ExportContent, _render_end_survey_html

    def fake_read(_library_id: str, _message_id: str) -> dict:
        return {
            "description": "My EOS message",
            "messages": {
                "en": "<p>EN content</p>",
                "fr": "<p>FR content</p>",
            },
        }

    monkeypatch.setattr(
        "qsync.translation_export._read_eos_message_from_disk", fake_read
    )

    content = ExportContent(
        survey_id="SV_TEST",
        survey_name="Test Survey",
        survey_title=None,
        survey_description=None,
        version_number=None,
        version_id=None,
        version_description=None,
        survey_payload={"result": {}},
        survey_link="",
        active_qids=set(),
        translation_ctx=None,
        render_plan=None,
        qid_to_js={},
        mermaid_code=None,
        mermaid_path=None,
        mermaid_image_path=None,
        edf_overrides=None,
        include_html_source=False,
        layout_heuristics=False,
        compare_to_base=True,
        render_language="FR",
        base_language="EN",
        output_path=tmp_path / "out.pdf",
        include_js_strings=True,
        flow_trace=None,
    )

    node = {
        "Type": "EndSurvey",
        "FlowID": "FL_END",
        "Options": {
            "SurveyTermination": "DisplayMessage",
            "EOSMessageLibrary": "UR_LIB",
            "EOSMessage": "MS_MSG",
        },
    }

    html = _render_end_survey_html(node, content, depth=0)
    assert "END SURVEY" in html
    assert "My EOS message" in html
    assert "EN content" in html
    assert "FR content" in html
