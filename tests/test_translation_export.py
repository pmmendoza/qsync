from __future__ import annotations

from pathlib import Path
import re

import pytest

docx = pytest.importorskip("docx")

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc``\x00"
    b"\x00\x00\x02\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
)

_TINY_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"></svg>'


def _doc_text(doc) -> str:
    parts: list[str] = []
    for p in doc.paragraphs:
        if p.text:
            parts.append(p.text)
    for tbl in doc.tables:
        for row in tbl.rows:
            for cell in row.cells:
                if cell.text:
                    parts.append(cell.text)
    return "\n".join(parts)


def _write_fake_mermaid_png(_code: str, out_path: Path) -> None:
    out_path.write_bytes(_TINY_PNG)


def _write_fake_mermaid_svg(_code: str, out_path: Path) -> None:
    out_path.write_bytes(_TINY_SVG)


def _sbs_matrix_payload() -> dict:
    return {
        "result": {
            "SurveyOptions": {"SurveyLanguage": "EN", "AvailableLanguages": ["EN", "FR"]},
            "SurveyFlow": {"Flow": [{"Type": "Standard", "ID": "BL_1"}]},
            "Blocks": {
                "BL_1": {
                    "Type": "Default",
                    "Description": "SBS Matrix",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID15"}],
                }
            },
            "Questions": {
                "QID15": {
                    "QuestionType": "SBS",
                    "Selector": "SBSMatrix",
                    "QuestionText": "How much do you remember?",
                    "DataExportTag": "newsmem",
                    "Choices": {
                        "1": {"Display": "Statement 1"},
                        "2": {"Display": "Statement 2"},
                    },
                    "ChoiceOrder": ["1", "2"],
                    "Answers": {
                        "1": {"Display": "Never"},
                        "2": {"Display": "Sometimes"},
                    },
                    "AnswerOrder": ["1", "2"],
                    "AdditionalQuestions": {
                        "1": {
                            "QuestionText": "Today",
                            # Column answers omitted to force fallback via top-level Answers.
                        },
                        "2": {
                            "QuestionText": "Yesterday",
                            "Answers": {
                                "1": {"Display": "Never"},
                                "2": {"Display": "Sometimes"},
                            },
                            "AnswerOrder": ["1", "2"],
                        },
                    },
                    "Language": {
                        "FR": {
                            "QuestionText": "De quelle façon vous en souvenez-vous ?",
                            "Choices": {
                                "1": {"Display": "Énoncé 1"},
                                "2": {"Display": "Énoncé 2"},
                            },
                            "Answers": {
                                "1": {"Display": "Jamais"},
                                "2": {"Display": "Parfois"},
                            },
                            "AdditionalQuestions": {
                                "1": {
                                    "QuestionText": "Aujourd'hui",
                                    # No column answers in FR either.
                                },
                                "2": {
                                    "QuestionText": "Hier",
                                    "Answers": {
                                        "1": {"Display": "Jamais"},
                                        "2": {"Display": "Parfois"},
                                    },
                                },
                            },
                        }
                    },
                }
            },
        }
    }


def test_translation_export_mvp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.translation_export import export_survey_payload_to_word

    monkeypatch.setattr(
        "qsync.translation_export._render_mermaid_to_png",
        _write_fake_mermaid_png,
    )
    monkeypatch.setattr(
        "qsync.translation_export._render_mermaid_to_svg",
        _write_fake_mermaid_svg,
    )

    mapping_csv = tmp_path / "survey_qid_js_map.csv"
    mapping_csv.write_text(
        "js_file,SV_TEST-demo\n" "test_guard.js,QID1\n",
        encoding="utf-8",
    )

    payload = {
        "result": {
            "SurveyFlow": {
                "Flow": [
                    {"Type": "Standard", "ID": "BL_1"},
                    {
                        "Type": "EndSurvey",
                        "FlowID": "FL_END",
                        "Options": {
                            "SurveyTermination": "DisplayMessage",
                            "EOSMessageLibrary": "UR_LIB",
                            "EOSMessage": "MS_MSG",
                        },
                    },
                ]
            },
            "Blocks": {
                "BL_1": {
                    "Type": "Default",
                    "Description": "Intro",
                    "BlockElements": [
                        {"Type": "Question", "QuestionID": "QID1"},
                        {"Type": "Question", "QuestionID": "QID2"},
                        {"Type": "Question", "QuestionID": "QID4"},
                    ],
                },
                "BL_TRASH": {
                    "Type": "Trash",
                    "Description": "Trash",
                    "BlockElements": [
                        {"Type": "Question", "QuestionID": "QID3"},
                    ],
                },
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": "<p>Hello <strong>world</strong></p>",
                    "DataExportTag": "hello_tag",
                    "QuestionJS": "Qualtrics.SurveyEngine.addOnReady(function(){});",
                },
                "QID2": {
                    "QuestionType": "MC",
                    "Selector": "SAVR",
                    "QuestionText": 'Pick <span data-topic-id="t1">topic</span>',
                    "DataExportTag": "topic_tag",
                    "DisplayLogic": {
                        "0": {
                            "0": {
                                "Description": '<span class="ConjDesc">If</span> X <span class="OpDesc">Is Selected</span>',
                                "Type": "Expression",
                            },
                            "Type": "If",
                        },
                        "Type": "BooleanExpression",
                    },
                    "Validation": {"Settings": {"ForceResponse": "ON", "Type": "None"}},
                    "Choices": {"1": {"Display": "One"}, "2": {"Display": "Two"}},
                    "ChoiceOrder": ["1", "2"],
                },
                "QID4": {
                    "QuestionType": "Timing",
                    "Selector": "PageTimer",
                    "QuestionText": "Should not be expanded",
                    "DataExportTag": "timing_tag",
                },
                "QID3": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": "This is trash",
                    "DataExportTag": "trash_tag",
                },
            },
        }
    }

    out_docx = tmp_path / "out.docx"
    out_mmd = tmp_path / "out.flow.mmd"
    out_png = tmp_path / "out.flow.png"
    out_svg = tmp_path / "out.flow.svg"
    export_survey_payload_to_word(
        "SV_TEST",
        payload,
        out_docx,
        mermaid_path=out_mmd,
        mermaid_image_path=out_png,
        mermaid_svg_path=out_svg,
        render_mermaid=True,
        mapping_path=mapping_csv,
    )

    assert out_docx.exists()
    assert out_mmd.exists()
    assert out_png.exists()
    assert out_svg.exists()
    mermaid = out_mmd.read_text(encoding="utf-8")
    assert "flowchart TB" in mermaid
    assert "QID1:hello_tag" in mermaid
    assert "QID2:topic_tag" in mermaid
    assert "QIDs:" not in mermaid
    assert "Join" not in mermaid

    d = docx.Document(str(out_docx))
    text = _doc_text(d)

    # Active/in-flow only: QID3 is not in SurveyFlow blocks (and trash).
    assert "[QID1][TE][JS] hello_tag" in text
    assert "[QID2][MC] topic_tag" in text
    assert "QID3" not in text

    # Display logic and branch visualization
    assert "DISPLAY IF:" in text
    assert "*" in text

    # Timing questions should be compact
    assert "[QID4][TIM] timing_tag" in text
    assert "Timing Block" in text

    # Data-* attributes are ignored for Word rendering; content should still be readable.
    assert "Pick topic" in text
    assert "data-topic-id" not in text

    # QID + DataExportTag surfaced
    assert "[QID1][TE][JS] hello_tag" in text
    assert "[QID2][MC] topic_tag" in text

    # QuestionJS mapping surfaced
    assert "survey_js/core/test_guard.js" in text

    # EndSurvey message references surfaced
    assert "EOSMessageLibrary=UR_LIB" in text
    assert "EOSMessage=MS_MSG" in text

    # Question table: at least one cell contains question text.
    assert any(
        "Hello world" in cell.text
        for tbl in d.tables
        for row in tbl.rows
        for cell in row.cells
    )

    # Rows are vertical: ensure QID1 table has metadata row + text row (no empty logic/statements/options rows).
    q1_tables = [
        tbl
        for tbl in d.tables
        if any("Hello world" in cell.text for row in tbl.rows for cell in row.cells)
        and len(tbl.columns) == 1
    ]
    assert q1_tables
    assert len(q1_tables[0].rows) == 2
    assert "[QID1][TE][JS] hello_tag" in q1_tables[0].rows[0].cells[0].text

    # QID2 has metadata + display logic + text + options (4 rows), but no statements row.
    q2_tables = [
        tbl
        for tbl in d.tables
        if any("Pick" in cell.text for row in tbl.rows for cell in row.cells)
        and len(tbl.columns) == 1
    ]
    assert q2_tables
    assert len(q2_tables[0].rows) == 4
    assert "[QID2][MC] topic_tag" in q2_tables[0].rows[0].cells[0].text


def test_translation_export_mermaid_env_disable_skips_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from qsync.translation_export import export_survey_payload_to_word

    monkeypatch.setenv("QSYNC_MERMAID_RENDER", "0")

    payload = {
        "result": {
            "SurveyFlow": {"Flow": [{"Type": "Standard", "ID": "BL_1"}]},
            "Blocks": {
                "BL_1": {
                    "Type": "Default",
                    "Description": "Main",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
                }
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": "Hello",
                    "DataExportTag": "hello_tag",
                }
            },
        }
    }

    out_docx = tmp_path / "out.docx"
    out_mmd = tmp_path / "out.flow.mmd"
    export_survey_payload_to_word(
        "SV_TEST",
        payload,
        out_docx,
        mermaid_path=out_mmd,
        render_mermaid=True,
    )

    assert out_docx.exists()
    assert not out_mmd.exists()


def test_translation_export_mermaid_artifacts_can_be_separate_from_docx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.translation_export import export_survey_payload_to_word

    monkeypatch.setattr(
        "qsync.translation_export._render_mermaid_to_png",
        _write_fake_mermaid_png,
    )
    monkeypatch.setattr(
        "qsync.translation_export._render_mermaid_to_svg",
        _write_fake_mermaid_svg,
    )

    payload = {
        "result": {
            "SurveyFlow": {"Flow": [{"Type": "Standard", "ID": "BL_1"}]},
            "Blocks": {
                "BL_1": {
                    "Type": "Default",
                    "Description": "Main",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
                }
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": "Hello",
                    "DataExportTag": "hello_tag",
                }
            },
        }
    }

    out_docx = tmp_path / "out.docx"
    out_mmd = tmp_path / "out.flow.mmd"
    out_png = tmp_path / "out.flow.png"
    out_svg = tmp_path / "out.flow.svg"
    export_survey_payload_to_word(
        "SV_TEST",
        payload,
        out_docx,
        mermaid_path=out_mmd,
        mermaid_image_path=out_png,
        mermaid_svg_path=out_svg,
        render_mermaid=True,
        include_mermaid=False,
    )

    assert out_docx.exists()
    assert out_mmd.exists()
    assert out_png.exists()
    assert out_svg.exists()

    d = docx.Document(str(out_docx))
    text = _doc_text(d)
    assert "FLOW DIAGRAM (Mermaid)" not in text


def test_translation_export_edf_branch_filtering(tmp_path: Path) -> None:
    from qsync.translation_export import export_survey_payload_to_word

    payload = {
        "result": {
            "SurveyFlow": {
                "Flow": [
                    {
                        "Type": "Branch",
                        "FlowID": "FL_B",
                        "BranchLogic": {
                            "0": {
                                "0": {
                                    "LogicType": "EmbeddedField",
                                    "LeftOperand": "S_VERSION",
                                    "Operator": "EqualTo",
                                    "RightOperand": "PROLIFIC",
                                    "Type": "Expression",
                                    "Description": "If S_VERSION Is Equal to PROLIFIC",
                                },
                                "Type": "If",
                            },
                            "Type": "BooleanExpression",
                        },
                        "Then": [{"Type": "Standard", "ID": "BL_THEN"}],
                        "Else": [{"Type": "Standard", "ID": "BL_ELSE"}],
                    }
                ]
            },
            "Blocks": {
                "BL_THEN": {
                    "Type": "Default",
                    "Description": "Then",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
                },
                "BL_ELSE": {
                    "Type": "Default",
                    "Description": "Else",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID2"}],
                },
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": "Then question",
                    "DataExportTag": "then_tag",
                },
                "QID2": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": "Else question",
                    "DataExportTag": "else_tag",
                },
            },
        }
    }

    out_docx = tmp_path / "out.docx"
    export_survey_payload_to_word(
        "SV_TEST",
        payload,
        out_docx,
        edf_overrides={"S_VERSION": "PROLIFIC"},
    )

    d = docx.Document(str(out_docx))
    text = _doc_text(d)
    assert "[QID1][TE] then_tag" in text
    assert "[QID2][TE] else_tag" not in text


def test_translation_export_mermaid_respects_edf_branch_filtering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.translation_export import export_survey_payload_to_word

    monkeypatch.setattr(
        "qsync.translation_export._render_mermaid_to_png",
        _write_fake_mermaid_png,
    )
    monkeypatch.setattr(
        "qsync.translation_export._render_mermaid_to_svg",
        _write_fake_mermaid_svg,
    )

    payload = {
        "result": {
            "SurveyFlow": {
                "Flow": [
                    {
                        "Type": "Branch",
                        "FlowID": "FL_B",
                        "BranchLogic": {
                            "0": {
                                "0": {
                                    "LogicType": "EmbeddedField",
                                    "LeftOperand": "S_VERSION",
                                    "Operator": "EqualTo",
                                    "RightOperand": "PROLIFIC",
                                    "Type": "Expression",
                                    "Description": "If S_VERSION Is Equal to PROLIFIC",
                                },
                                "Type": "If",
                            },
                            "Type": "BooleanExpression",
                        },
                        "Then": [{"Type": "Standard", "ID": "BL_THEN"}],
                        "Else": [{"Type": "Standard", "ID": "BL_ELSE"}],
                    }
                ]
            },
            "Blocks": {
                "BL_THEN": {
                    "Type": "Default",
                    "Description": "Then",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
                },
                "BL_ELSE": {
                    "Type": "Default",
                    "Description": "Else",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID2"}],
                },
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": "Then question",
                    "DataExportTag": "then_tag",
                },
                "QID2": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": "Else question",
                    "DataExportTag": "else_tag",
                },
            },
        }
    }

    out_docx = tmp_path / "out.docx"
    out_mmd = tmp_path / "out.flow.mmd"
    export_survey_payload_to_word(
        "SV_TEST",
        payload,
        out_docx,
        mermaid_path=out_mmd,
        mermaid_image_path=tmp_path / "out.flow.png",
        mermaid_svg_path=tmp_path / "out.flow.svg",
        render_mermaid=True,
        include_mermaid=False,
        edf_overrides={"S_VERSION": "PROLIFIC"},
    )

    mermaid = out_mmd.read_text(encoding="utf-8")
    assert "Scenario EDF" in mermaid
    assert "S_VERSION=PROLIFIC" in mermaid
    assert "~~~" in mermaid
    assert "Block Then" in mermaid
    assert "BL_THEN" in mermaid
    assert "Block Else" not in mermaid
    assert "BL_ELSE" not in mermaid
    assert "IF S_VERSION" not in mermaid


def test_translation_export_mermaid_shows_embedded_fields_and_terminal_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.translation_export import export_survey_payload_to_word

    monkeypatch.setattr(
        "qsync.translation_export._render_mermaid_to_png",
        _write_fake_mermaid_png,
    )
    monkeypatch.setattr(
        "qsync.translation_export._render_mermaid_to_svg",
        _write_fake_mermaid_svg,
    )

    payload = {
        "result": {
            "SurveyFlow": {
                "Flow": [
                    {
                        "Type": "EmbeddedData",
                        "EmbeddedData": [
                            {"Field": "country", "Value": "IE"},
                            {"Field": "Q_Language"},
                            {"Field": "DEBUG"},
                        ],
                    },
                    {
                        "Type": "Branch",
                        "BranchLogic": {
                            "0": {
                                "0": {
                                    "LogicType": "Question",
                                    "QuestionID": "QID1",
                                    "QuestionIDFromLocator": "QID1",
                                    "ChoiceLocator": "q://QID1/SelectableChoice/1",
                                    "Operator": "Selected",
                                    "Type": "Expression",
                                    "Description": "If QID1 option 1 is selected",
                                },
                                "Type": "If",
                            },
                            "Type": "BooleanExpression",
                        },
                        "Then": [
                            {
                                "Type": "EndSurvey",
                                "Options": {"SurveyTermination": "DisplayMessage"},
                            }
                        ],
                        "Else": [{"Type": "Standard", "ID": "BL_1"}],
                    },
                ]
            },
            "Blocks": {
                "BL_1": {
                    "Type": "Default",
                    "Description": "Main",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
                }
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "MC",
                    "Selector": "SAVR",
                    "QuestionText": "Continue?",
                    "DataExportTag": "continue_tag",
                    "Choices": {"1": {"Display": "Yes"}},
                }
            },
        }
    }

    out_docx = tmp_path / "out.docx"
    out_mmd = tmp_path / "out.flow.mmd"
    export_survey_payload_to_word(
        "SV_TEST",
        payload,
        out_docx,
        mermaid_path=out_mmd,
        mermaid_image_path=tmp_path / "out.flow.png",
        mermaid_svg_path=tmp_path / "out.flow.svg",
        render_mermaid=True,
        include_mermaid=False,
    )

    mermaid = out_mmd.read_text(encoding="utf-8")
    assert "Writes: country=IE," in mermaid
    assert "DEBUG" in mermaid
    assert "QID1:continue_tag" in mermaid
    assert "IF QID1:continue_tag = Yes" in mermaid
    assert "EndSurvey<br/>DisplayMessage" in mermaid
    assert "Join" not in mermaid


def test_translation_export_mermaid_full_order_randomizer_uses_cluster_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.translation_export import export_survey_payload_to_word

    monkeypatch.setattr(
        "qsync.translation_export._render_mermaid_to_png",
        _write_fake_mermaid_png,
    )
    monkeypatch.setattr(
        "qsync.translation_export._render_mermaid_to_svg",
        _write_fake_mermaid_svg,
    )

    payload = {
        "result": {
            "SurveyFlow": {
                "Flow": [
                    {
                        "Type": "BlockRandomizer",
                        "SubSet": 4,
                        "EvenPresentation": True,
                        "Flow": [
                            {"Type": "Standard", "ID": "BL_1"},
                            {"Type": "Standard", "ID": "BL_2"},
                            {"Type": "Standard", "ID": "BL_3"},
                            {"Type": "Standard", "ID": "BL_4"},
                        ],
                    },
                    {"Type": "Standard", "ID": "BL_5"},
                ]
            },
            "Blocks": {
                **{
                    f"BL_{idx}": {
                        "Type": "Default",
                        "Description": f"Block {idx}",
                        "BlockElements": [{"Type": "Question", "QuestionID": f"QID{idx}"}],
                    }
                    for idx in range(1, 6)
                }
            },
            "Questions": {
                **{
                    f"QID{idx}": {
                        "QuestionType": "TE",
                        "Selector": "SL",
                        "QuestionText": f"Question {idx}",
                        "DataExportTag": f"tag_{idx}",
                    }
                    for idx in range(1, 6)
                }
            },
        }
    }

    out_docx = tmp_path / "out.docx"
    out_mmd = tmp_path / "out.flow.mmd"
    export_survey_payload_to_word(
        "SV_TEST",
        payload,
        out_docx,
        mermaid_path=out_mmd,
        mermaid_image_path=tmp_path / "out.flow.png",
        mermaid_svg_path=tmp_path / "out.flow.svg",
        render_mermaid=True,
        include_mermaid=False,
    )

    mermaid = out_mmd.read_text(encoding="utf-8")
    assert "Random order" in mermaid
    assert "grid 2x2" in mermaid
    row_subgraphs = re.findall(r'subgraph n\d+_r\d+\[" "\]', mermaid)
    assert len(row_subgraphs) == 2
    assert "QID1:tag_1" in mermaid
    assert "QID4:tag_4" in mermaid


def test_translation_export_mermaid_shows_all_qids_without_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.translation_export import export_survey_payload_to_word

    monkeypatch.setattr(
        "qsync.translation_export._render_mermaid_to_png",
        _write_fake_mermaid_png,
    )
    monkeypatch.setattr(
        "qsync.translation_export._render_mermaid_to_svg",
        _write_fake_mermaid_svg,
    )

    payload = {
        "result": {
            "SurveyFlow": {"Flow": [{"Type": "Standard", "ID": "BL_1"}]},
            "Blocks": {
                "BL_1": {
                    "Type": "Default",
                    "Description": "Large",
                    "BlockElements": [
                        {"Type": "Question", "QuestionID": f"QID{idx}"}
                        for idx in range(1, 9)
                    ],
                }
            },
            "Questions": {
                f"QID{idx}": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": f"Question {idx}",
                    "DataExportTag": f"tag_{idx}",
                }
                for idx in range(1, 9)
            },
        }
    }

    out_docx = tmp_path / "out.docx"
    out_mmd = tmp_path / "out.flow.mmd"
    export_survey_payload_to_word(
        "SV_TEST",
        payload,
        out_docx,
        mermaid_path=out_mmd,
        mermaid_image_path=tmp_path / "out.flow.png",
        mermaid_svg_path=tmp_path / "out.flow.svg",
        render_mermaid=True,
        include_mermaid=False,
    )

    mermaid = out_mmd.read_text(encoding="utf-8")
    assert "QIDs:" not in mermaid
    assert "QID1:tag_1" in mermaid
    assert "QID8:tag_8" in mermaid


def test_translation_export_edf_prunes_unreachable_selected_question_branch(
    tmp_path: Path,
) -> None:
    from qsync.translation_export import export_survey_payload_to_word

    payload = {
        "result": {
            "SurveyFlow": {
                "Flow": [
                    {
                        "Type": "Branch",
                        "FlowID": "FL_DEBUG_BRANCH",
                        "BranchLogic": {
                            "0": {
                                "0": {
                                    "LogicType": "EmbeddedField",
                                    "LeftOperand": "DEBUG",
                                    "Operator": "EqualTo",
                                    "RightOperand": "T",
                                    "Type": "Expression",
                                    "Description": "If DEBUG Is Equal to T",
                                },
                                "Type": "If",
                            },
                            "Type": "BooleanExpression",
                        },
                        "Then": [{"Type": "Standard", "ID": "BL_DEBUG"}],
                        "Else": [],
                    },
                    {
                        "Type": "Branch",
                        "FlowID": "FL_SELECTED_BRANCH",
                        "BranchLogic": {
                            "0": {
                                "0": {
                                    "LogicType": "Question",
                                    "QuestionID": "QID_DEBUG",
                                    "Operator": "Selected",
                                    "RightOperand": "1",
                                    "Type": "Expression",
                                    "Description": "If debug question Is Selected",
                                },
                                "Type": "If",
                            },
                            "Type": "BooleanExpression",
                        },
                        "Then": [
                            {
                                "Type": "EmbeddedData",
                                "FlowID": "FL_ED",
                                "EmbeddedData": [
                                    {"Field": "SHOULD_NOT_APPEAR", "Value": "X"}
                                ],
                            }
                        ],
                        "Else": [{"Type": "Standard", "ID": "BL_MAIN"}],
                    },
                ]
            },
            "Blocks": {
                "BL_DEBUG": {
                    "Type": "Default",
                    "Description": "Debug",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID_DEBUG"}],
                },
                "BL_MAIN": {
                    "Type": "Default",
                    "Description": "Main",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID_MAIN"}],
                },
            },
            "Questions": {
                "QID_DEBUG": {
                    "QuestionType": "MC",
                    "Selector": "SAVR",
                    "QuestionText": "Debug question",
                    "DataExportTag": "debug_q",
                    "Choices": {"1": {"Display": "One"}},
                    "ChoiceOrder": ["1"],
                },
                "QID_MAIN": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": "Main question",
                    "DataExportTag": "main_tag",
                },
            },
        }
    }

    out_docx = tmp_path / "out.docx"
    export_survey_payload_to_word(
        "SV_TEST",
        payload,
        out_docx,
        edf_overrides={"DEBUG": "F"},
    )

    d = docx.Document(str(out_docx))
    text = _doc_text(d)
    assert "[QID_MAIN][TE] main_tag" in text
    assert "[QID_DEBUG][MC] debug_q" not in text
    assert "EMBEDDED DATA WRITES (FlowID=FL_ED)" not in text
    assert "SHOULD_NOT_APPEAR" not in text
    # Both branches are resolved/disabled under explicit EDF pruning, so branch annotations should be hidden.
    assert "BRANCH:" not in text
    assert "END BRANCH" not in text


def test_translation_export_edf_warns_on_unused_key(tmp_path: Path) -> None:
    from qsync.translation_export import export_survey_payload_to_word

    payload = {
        "result": {
            "SurveyFlow": {
                "Flow": [
                    {
                        "Type": "Branch",
                        "FlowID": "FL_B",
                        "BranchLogic": {
                            "0": {
                                "0": {
                                    "LogicType": "EmbeddedField",
                                    "LeftOperand": "SVERSION",
                                    "Operator": "EqualTo",
                                    "RightOperand": "PILOT_1",
                                    "Type": "Expression",
                                    "Description": "If SVERSION Is Equal to PILOT_1",
                                },
                                "Type": "If",
                            },
                            "Type": "BooleanExpression",
                        },
                        "Then": [{"Type": "Standard", "ID": "BL_THEN"}],
                        "Else": [{"Type": "Standard", "ID": "BL_ELSE"}],
                    }
                ]
            },
            "Blocks": {
                "BL_THEN": {
                    "Type": "Default",
                    "Description": "Then",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
                },
                "BL_ELSE": {
                    "Type": "Default",
                    "Description": "Else",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID2"}],
                },
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": "Then question",
                    "DataExportTag": "then_tag",
                },
                "QID2": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": "Else question",
                    "DataExportTag": "else_tag",
                },
            },
        }
    }

    out_docx = tmp_path / "out.docx"
    export_survey_payload_to_word(
        "SV_TEST",
        payload,
        out_docx,
        edf_overrides={"S_VERSION": "PROLIFIC"},
    )

    d = docx.Document(str(out_docx))
    text = _doc_text(d)
    assert (
        "WARNING: Some --edf keys are not used in any SurveyFlow BranchLogic." in text
    )
    assert "Unused: S_VERSION" in text


def test_translation_export_edf_warns_on_variant_key_spellings(tmp_path: Path) -> None:
    from qsync.translation_export import export_survey_payload_to_word

    payload = {
        "result": {
            "SurveyFlow": {
                "Flow": [
                    {
                        "Type": "Branch",
                        "FlowID": "FL_B1",
                        "BranchLogic": {
                            "0": {
                                "0": {
                                    "LogicType": "EmbeddedField",
                                    "LeftOperand": "SVERSION",
                                    "Operator": "EqualTo",
                                    "RightOperand": "PILOT_1",
                                    "Type": "Expression",
                                    "Description": "If SVERSION Is Equal to PILOT_1",
                                },
                                "Type": "If",
                            },
                            "Type": "BooleanExpression",
                        },
                        "Then": [],
                        "Else": [],
                    },
                    {
                        "Type": "Branch",
                        "FlowID": "FL_B2",
                        "BranchLogic": {
                            "0": {
                                "0": {
                                    "LogicType": "EmbeddedField",
                                    "LeftOperand": "S_VERSION",
                                    "Operator": "EqualTo",
                                    "RightOperand": "PROLIFIC",
                                    "Type": "Expression",
                                    "Description": "If S_VERSION Is Equal to PROLIFIC",
                                },
                                "Type": "If",
                            },
                            "Type": "BooleanExpression",
                        },
                        "Then": [],
                        "Else": [],
                    },
                ]
            },
            "Blocks": {},
            "Questions": {},
        }
    }

    out_docx = tmp_path / "out.docx"
    export_survey_payload_to_word(
        "SV_TEST",
        payload,
        out_docx,
        edf_overrides={"S_VERSION": "PROLIFIC"},
    )

    d = docx.Document(str(out_docx))
    text = _doc_text(d)
    assert "WARNING: Survey uses multiple EDF key spellings:" in text
    assert "SVERSION" in text
    assert "S_VERSION" in text


def test_translation_export_render_language_overlays_choices_and_matrix_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.translation_export import export_survey_payload_to_word

    import qsync.translation_export as texp

    monkeypatch.setattr(
        texp,
        "get_client_config",
        lambda env=None: ("example.qualtrics.com", {}),  # noqa: ARG005
    )

    payload = {
        "result": {
            "SurveyOptions": {
                "SurveyLanguage": "EN",
                "AvailableLanguages": ["EN", "FR"],
            },
            "SurveyFlow": {"Flow": [{"Type": "Standard", "ID": "BL_1"}]},
            "Blocks": {
                "BL_1": {
                    "Type": "Default",
                    "Description": "Intro",
                    "BlockElements": [
                        {"Type": "Question", "QuestionID": "QID1"},
                        {"Type": "Question", "QuestionID": "QID2"},
                    ],
                }
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "Matrix",
                    "Selector": "Likert",
                    "QuestionText": "Matrix base",
                    "DataExportTag": "matrix_tag",
                    "Choices": {"1": {"Display": "Row A"}},
                    "ChoiceOrder": ["1"],
                    "Answers": {"1": {"Display": "Col X"}},
                    "AnswerOrder": ["1"],
                    "Language": {
                        "FR": {
                            "QuestionText": "Matrice FR",
                            "Choices": {"1": {"Display": "Ligne A"}},
                            "Answers": {"1": {"Display": "Colonne X"}},
                        }
                    },
                },
                "QID2": {
                    "QuestionType": "MC",
                    "Selector": "SAVR",
                    "QuestionText": "Pick one",
                    "DataExportTag": "mc_tag",
                    "Choices": {"1": {"Display": "One"}},
                    "ChoiceOrder": ["1"],
                    "Language": {
                        "FR": {
                            "QuestionText": "Choisissez une option",
                            "Choices": {"1": {"Display": "Un"}},
                        }
                    },
                },
            },
        }
    }

    out_docx = tmp_path / "out.docx"
    export_survey_payload_to_word(
        "SV_TEST",
        payload,
        out_docx,
        render_language="FR",
        edf_overrides={"S_VERSION": "PROLIFIC"},
    )

    d = docx.Document(str(out_docx))
    text = _doc_text(d)

    # Export includes language rendering summary + a survey link with Q_Language.
    assert "LANGUAGE RENDERING SUMMARY" in text
    assert "Q_Language=FR" in text
    assert "S_VERSION=PROLIFIC" in text

    # Matrix mapping: rows -> Choice keys, cols -> Answer keys.
    assert "Matrice FR" in text
    assert "Ligne A" in text
    assert "Colonne X" in text

    # Non-matrix mapping: MC options -> Choice keys (not Answer keys).
    assert "Choisissez une option" in text
    assert "Un" in text
    assert "One" not in text


def test_collect_expected_translation_keys_supports_sbs_matrix_fallback_answers() -> None:
    from qsync.translation_export import _collect_expected_translation_keys

    payload = _sbs_matrix_payload()
    questions = payload["result"]["Questions"]
    keys = set(
        _collect_expected_translation_keys(questions, {"QID15"})
    )

    assert "QID15_QuestionText" in keys
    assert "QID15_Choice1" in keys
    assert "QID15_Choice2" in keys
    # SBS column headers and answers are now expanded even when column-level answers are absent.
    assert "QID15#1_QuestionText" in keys
    assert "QID15#1_Answer1" in keys
    assert "QID15#1_Answer2" in keys
    assert "QID15#2_QuestionText" in keys
    assert "QID15#2_Answer1" in keys
    assert "QID15#2_Answer2" in keys


def test_build_translation_map_from_cache_uses_sbs_matrix_fallback_answers() -> None:
    from qsync.translation_export import build_translation_map_from_cache

    payload = _sbs_matrix_payload()
    fr_map = build_translation_map_from_cache(
        payload,
        language="FR",
        base_language="EN",
    )

    # Column-level statements are present in both base and translated surfaces.
    assert fr_map["QID15#1_QuestionText"] == "Aujourd'hui"
    assert fr_map["QID15#2_QuestionText"] == "Hier"
    # Missing column 1 answer values must still be emitted using base question-level answers.
    assert fr_map["QID15#1_Answer1"] == "Jamais"
    assert fr_map["QID15#1_Answer2"] == "Parfois"
    # Column 2 keeps its own translated answers.
    assert fr_map["QID15#2_Answer1"] == "Jamais"
    assert fr_map["QID15#2_Answer2"] == "Parfois"


def test_build_translation_map_from_cache_base_uses_survey_options_title_fallback() -> None:
    from qsync.translation_export import build_translation_map_from_cache

    payload = {
        "result": {
            "SurveyLanguage": "EN",
            "SurveyTitle": None,
            "SurveyDescription": None,
            "SurveyOptions": {
                "SurveyTitle": "Fallback Title",
                "SurveyMetaDescription": "Fallback Description",
            },
            "Questions": {},
        }
    }

    base_map = build_translation_map_from_cache(
        payload,
        language="EN",
        base_language="EN",
    )

    assert base_map["SurveyTitle"] == "Fallback Title"
    assert base_map["SurveyDescription"] == "Fallback Description"


def test_word_export_renders_sbs_matrix_with_fallbacked_answers(tmp_path: Path) -> None:
    from qsync.translation_export import export_survey_payload_to_word

    payload = _sbs_matrix_payload()
    out_docx = tmp_path / "out.docx"
    export_survey_payload_to_word(
        "SV_TEST",
        payload,
        out_docx,
        render_language="FR",
    )

    d = docx.Document(str(out_docx))
    text = _doc_text(d)

    assert "QID15" in text
    assert "SBS Matrix" in text
    assert "Column 1" in text
    assert "Column 2" in text
    assert "Aujourd'hui" in text
    assert "Hier" in text
    # Fallbacked column answers should render even when not present in Column block.
    assert "Jamais" in text
    assert "Parfois" in text


def test_pdf_export_builds_sbs_matrix_sections(tmp_path: Path) -> None:
    from qsync.translation_export import (
        _build_pdf_html_template,
        _prepare_export_content,
    )

    payload = _sbs_matrix_payload()
    content = _prepare_export_content(
        "SV_TEST",
        payload,
        output_path=tmp_path / "out.pdf",
        render_language="FR",
    )
    html = _build_pdf_html_template(content)

    assert "QID15" in html
    assert "Aujourd'hui" in html
    assert "Hier" in html
    assert "Jamais" in html
    assert "Parfois" in html


def test_translation_export_logic_uses_language_blocks_when_single_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.translation_export import export_survey_payload_to_word

    payload = {
        "result": {
            "SurveyOptions": {
                "SurveyLanguage": "EN",
                "AvailableLanguages": ["EN", "NL"],
            },
            "SurveyFlow": {
                "Flow": [
                    {"Type": "Standard", "ID": "BL_1"},
                    {
                        "Type": "Branch",
                        "FlowID": "FL_B",
                        "BranchLogic": {
                            "0": {
                                "0": {
                                    "LogicType": "Question",
                                    "QuestionID": "QID50",
                                    "Operator": "Selected",
                                    "ChoiceLocator": "q://QID50/SelectableChoice/1",
                                    "Type": "Expression",
                                    "Description": "If ... Is Selected",
                                },
                                "Type": "If",
                            },
                            "Type": "BooleanExpression",
                        },
                        "Then": [{"Type": "Standard", "ID": "BL_2"}],
                        "Else": [],
                    },
                ]
            },
            "Blocks": {
                "BL_1": {
                    "Type": "Default",
                    "Description": "Intro",
                    "BlockElements": [
                        {"Type": "Question", "QuestionID": "QID50"},
                        {"Type": "Question", "QuestionID": "QID51"},
                    ],
                },
                "BL_2": {
                    "Type": "Default",
                    "Description": "Then",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID52"}],
                },
            },
            "Questions": {
                "QID50": {
                    "QuestionType": "MC",
                    "Selector": "SAVR",
                    "QuestionText": "Are you participating in this study as part of...",
                    "DataExportTag": "q50",
                    "Choices": {"1": {"Display": "internal pilot"}},
                    "ChoiceOrder": ["1"],
                    "Language": {
                        "NL": {
                            "QuestionText": "Neemt u deel aan dit onderzoek als onderdeel van...",
                            "Choices": {"1": {"Display": "interne pilot"}},
                        }
                    },
                },
                "QID51": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": "Shown conditionally",
                    "DataExportTag": "q51",
                    "DisplayLogic": {
                        "0": {
                            "0": {
                                "LogicType": "Question",
                                "QuestionID": "QID50",
                                "Operator": "Selected",
                                "ChoiceLocator": "q://QID50/SelectableChoice/1",
                                "Type": "Expression",
                                "Description": "If ... Is Selected",
                            },
                            "Type": "If",
                        },
                        "Type": "BooleanExpression",
                    },
                },
                "QID52": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": "Then question",
                    "DataExportTag": "q52",
                },
            },
        }
    }

    out_docx = tmp_path / "out.docx"
    export_survey_payload_to_word(
        "SV_TEST",
        payload,
        out_docx,
        render_language="NL",
    )

    d = docx.Document(str(out_docx))
    text = _doc_text(d)

    # Branch + DisplayLogic should use the target language for the referenced question and option.
    assert (
        'DISPLAY IF: QID50:"Neemt u deel aan dit onderzoek als onderdeel van..."'
        in text
    )
    assert '"interne pilot" is selected' in text
    assert (
        'BRANCH: IF QID50:"Neemt u deel aan dit onderzoek als onderdeel van..."' in text
    )

    # English labels should not be used inside the logic lines in single-language export.
    assert 'QID50:"Are you participating in this study as part of..."' not in text
    assert '"internal pilot" is selected' not in text


def test_translation_export_compare_to_base_bilingual_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qsync.translation_export import export_survey_payload_to_word

    payload = {
        "result": {
            "SurveyOptions": {
                "SurveyLanguage": "EN",
                "AvailableLanguages": ["EN", "FR"],
            },
            "SurveyFlow": {"Flow": [{"Type": "Standard", "ID": "BL_1"}]},
            "Blocks": {
                "BL_1": {
                    "Type": "Default",
                    "Description": "Intro",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
                }
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "MC",
                    "Selector": "SAVR",
                    "QuestionText": "Hello",
                    "DataExportTag": "tag",
                    "Choices": {"1": {"Display": "Yes"}},
                    "ChoiceOrder": ["1"],
                    "Language": {
                        "FR": {
                            "QuestionText": "Bonjour",
                            "Choices": {"1": {"Display": "Oui"}},
                        }
                    },
                }
            },
        }
    }

    out_docx = tmp_path / "out.docx"
    export_survey_payload_to_word(
        "SV_TEST",
        payload,
        out_docx,
        render_language="FR",
        compare_to_base=True,
    )

    d = docx.Document(str(out_docx))
    text = _doc_text(d)

    assert "Mode: EN-FR" in text
    assert "Hello" in text
    assert "Bonjour" in text
    assert "Yes" in text
    assert "Oui" in text

    # Side-by-side table: the question table is two columns where EN/FR land in different cells.
    q_tables = [
        tbl
        for tbl in d.tables
        if len(tbl.columns) == 2
        and any("[QID1][MC]" in cell.text for row in tbl.rows for cell in row.cells)
    ]
    assert q_tables
    q_tbl = q_tables[0]
    # Find a row that contains the question text and ensure it is split across columns.
    assert any(
        "Hello" in row.cells[0].text and "Bonjour" in row.cells[1].text
        for row in q_tbl.rows
    )
    # Find a row that contains the option and ensure it is split across columns.
    assert any(
        "Yes" in row.cells[0].text and "Oui" in row.cells[1].text for row in q_tbl.rows
    )


def test_translation_export_no_html_suppresses_html_source(tmp_path: Path) -> None:
    from zipfile import ZipFile

    from qsync.translation_export import export_survey_payload_to_word

    payload = {
        "result": {
            "SurveyFlow": {"Flow": [{"Type": "Standard", "ID": "BL_1"}]},
            "Blocks": {
                "BL_1": {
                    "Type": "Default",
                    "Description": "Intro",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
                }
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": '<p>Prompt</p><canvas id="c"></canvas>',
                    "DataExportTag": "tag",
                }
            },
        }
    }

    out_docx = tmp_path / "out.docx"
    export_survey_payload_to_word(
        "SV_TEST",
        payload,
        out_docx,
        include_html_source=False,
    )

    d = docx.Document(str(out_docx))
    text = _doc_text(d)
    assert "Interactive chart omitted in export" in text
    assert "HTML (source):" not in text

    # System placeholder should be rendered in monospace (Courier New) in the docx XML.
    with ZipFile(out_docx) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    idx = xml.find("Interactive chart omitted in export")
    assert idx != -1
    snippet = xml[max(0, idx - 600) : idx + 200]
    assert "Courier New" in snippet


def test_translation_export_ids_are_monospace(tmp_path: Path) -> None:
    """QIDs + Block IDs should render in monospace without changing other formatting."""

    from zipfile import ZipFile

    from qsync.translation_export import export_survey_payload_to_word

    payload = {
        "result": {
            "SurveyFlow": {"Flow": [{"Type": "Standard", "ID": "BL_1"}]},
            "Blocks": {
                "BL_1": {
                    "Type": "Default",
                    "Description": "Intro",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
                }
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": "Prompt",
                    "DataExportTag": "tag",
                }
            },
        }
    }

    out_docx = tmp_path / "out.docx"
    export_survey_payload_to_word("SV_TEST", payload, out_docx)

    with ZipFile(out_docx) as z:
        xml = z.read("word/document.xml").decode("utf-8")

    idx_qid = xml.find("QID1")
    assert idx_qid != -1
    snippet_qid = xml[max(0, idx_qid - 600) : idx_qid + 200]
    assert "Courier New" in snippet_qid

    idx_bl = xml.find("BL_1")
    assert idx_bl != -1
    snippet_bl = xml[max(0, idx_bl - 600) : idx_bl + 200]
    assert "Courier New" in snippet_bl


def test_translation_export_layout_heuristics_flag_controls_list_to_table(
    tmp_path: Path,
) -> None:
    from qsync.translation_export import export_survey_payload_to_word

    payload = {
        "result": {
            "SurveyFlow": {"Flow": [{"Type": "Standard", "ID": "BL_1"}]},
            "Blocks": {
                "BL_1": {
                    "Type": "Default",
                    "Description": "Intro",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
                }
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "DB",
                    "Selector": "TB",
                    "QuestionText": (
                        "<p><strong>Task overview</strong></p>"
                        "<ul>"
                        "<li><p>[<strong>task</strong>] Survey</p></li>"
                        "<li><p>[<strong>time</strong>] 20min</p></li>"
                        "<li><p>[<strong>pay</strong>] 1 GBP</p></li>"
                        "<li><p>[<strong>reward</strong>] automatic</p></li>"
                        "</ul>"
                    ),
                    "DataExportTag": "tag",
                }
            },
        }
    }

    out_docx = tmp_path / "out.docx"
    export_survey_payload_to_word(
        "SV_TEST",
        payload,
        out_docx,
        include_html_source=False,
    )
    d = docx.Document(str(out_docx))
    assert not any(
        len(tbl2.columns) == 2
        and any(
            cell.text.strip() == "[task]" for row in tbl2.rows for cell in row.cells
        )
        for tbl in d.tables
        for row in tbl.rows
        for cell in row.cells
        for tbl2 in getattr(cell, "tables", []) or []
    )

    out_docx2 = tmp_path / "out2.docx"
    export_survey_payload_to_word(
        "SV_TEST",
        payload,
        out_docx2,
        include_html_source=False,
        layout_heuristics=True,
    )
    d2 = docx.Document(str(out_docx2))
    assert any(
        len(tbl2.columns) == 2
        and any(
            cell.text.strip() == "[task]" for row in tbl2.rows for cell in row.cells
        )
        for tbl in d2.tables
        for row in tbl.rows
        for cell in row.cells
        for tbl2 in getattr(cell, "tables", []) or []
    )


def test_safe_html_headings_do_not_bleed_bold(tmp_path: Path) -> None:
    from qsync.translation_export import export_survey_payload_to_word

    payload = {
        "result": {
            "SurveyFlow": {"Flow": [{"Type": "Standard", "ID": "BL_1"}]},
            "Blocks": {
                "BL_1": {
                    "Type": "Default",
                    "Description": "Intro",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
                }
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "DB",
                    "Selector": "TB",
                    "QuestionText": "<h2>Title</h2><p>Body</p>",
                    "DataExportTag": "tag",
                }
            },
        }
    }

    out_docx = tmp_path / "out.docx"
    export_survey_payload_to_word(
        "SV_TEST", payload, out_docx, include_html_source=False
    )

    d = docx.Document(str(out_docx))
    cells = [cell for tbl in d.tables for row in tbl.rows for cell in row.cells]
    target = next((c for c in cells if "Title" in c.text and "Body" in c.text), None)
    assert target is not None

    title_bold = None
    body_bold = None
    for p in target.paragraphs:
        for r in p.runs:
            if r.text == "Title":
                title_bold = r.bold
            if r.text == "Body":
                body_bold = r.bold
    assert title_bold is True
    assert body_bold is not True


def test_safe_html_nested_list_renders_bullets(tmp_path: Path) -> None:
    from qsync.translation_export import export_survey_payload_to_word

    payload = {
        "result": {
            "SurveyFlow": {"Flow": [{"Type": "Standard", "ID": "BL_1"}]},
            "Blocks": {
                "BL_1": {
                    "Type": "Default",
                    "Description": "Intro",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
                }
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "DB",
                    "Selector": "TB",
                    # Qualtrics often wraps list items in <p> tags; ensure we don't
                    # split the bullet marker and the content onto separate lines.
                    "QuestionText": "<p>Intro</p><ul><li><p>First</p></li><li><p>Second</p></li></ul>",
                    "DataExportTag": "tag",
                }
            },
        }
    }

    out_docx = tmp_path / "out.docx"
    export_survey_payload_to_word(
        "SV_TEST", payload, out_docx, include_html_source=False
    )

    d = docx.Document(str(out_docx))
    # List items are rendered as real Word list paragraphs (so the bullet character
    # is not part of the paragraph text).
    cells = [cell for tbl in d.tables for row in tbl.rows for cell in row.cells]
    first = [p for c in cells for p in c.paragraphs if p.text.strip() == "First"]
    second = [p for c in cells for p in c.paragraphs if p.text.strip() == "Second"]
    assert first and any(p.style and p.style.name == "List Bullet" for p in first)
    assert second and any(p.style and p.style.name == "List Bullet" for p in second)


def test_translation_export_flowid_is_monospace_in_embedded_data_table(
    tmp_path: Path,
) -> None:
    from qsync.translation_export import export_survey_payload_to_word

    payload = {
        "result": {
            "SurveyFlow": {
                "Flow": [
                    {
                        "Type": "EmbeddedData",
                        "FlowID": "FL_ED1",
                        "EmbeddedData": [{"Field": "DEBUG", "Value": "T"}],
                    },
                    {"Type": "Standard", "ID": "BL_1"},
                ]
            },
            "Blocks": {
                "BL_1": {
                    "Type": "Default",
                    "Description": "Intro",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
                }
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": "Prompt",
                    "DataExportTag": "tag",
                }
            },
        }
    }

    out_docx = tmp_path / "out.docx"
    export_survey_payload_to_word("SV_TEST", payload, out_docx)

    d = docx.Document(str(out_docx))

    found = False
    for tbl in d.tables:
        for row in tbl.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    for r in p.runs:
                        if r.text == "FL_ED1":
                            found = True
                            assert r.font.name == "Courier New"
    assert found


def test_translation_export_edf_override_reflected_in_first_embedded_data_block(
    tmp_path: Path,
) -> None:
    from qsync.translation_export import export_survey_payload_to_word

    payload = {
        "result": {
            "SurveyFlow": {
                "Flow": [
                    {
                        "Type": "EmbeddedData",
                        "FlowID": "FL_ED1",
                        "EmbeddedData": [{"Field": "DEBUG", "Value": "T"}],
                    },
                    {"Type": "Standard", "ID": "BL_1"},
                    {
                        "Type": "EmbeddedData",
                        "FlowID": "FL_ED2",
                        "EmbeddedData": [{"Field": "DEBUG", "Value": "T2"}],
                    },
                ]
            },
            "Blocks": {
                "BL_1": {
                    "Type": "Default",
                    "Description": "Intro",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
                }
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": "Prompt",
                    "DataExportTag": "tag",
                }
            },
        }
    }

    out_docx = tmp_path / "out.docx"
    export_survey_payload_to_word(
        "SV_TEST", payload, out_docx, edf_overrides={"DEBUG": "F"}
    )

    d = docx.Document(str(out_docx))
    values: list[str] = []
    for tbl in d.tables:
        if len(tbl.columns) != 4:
            continue
        hdr = tbl.rows[0].cells
        if (
            hdr[0].text.strip() != "Field"
            or hdr[1].text.strip() != "Value"
            or hdr[2].text.strip() != "Type"
            or hdr[3].text.strip() != "FlowID"
        ):
            continue
        for row in tbl.rows[1:]:
            if row.cells[0].text.strip() == "DEBUG":
                values.append(row.cells[1].text.strip())

    assert "F" in values
    assert "T2" in values


# ==============================================================================
# PDF Export Tests
# ==============================================================================


def test_pdf_export_mvp(tmp_path: Path) -> None:
    """Test basic PDF export with questions and blocks."""
    from qsync.translation_export import export_survey_payload_to_pdf

    mapping_csv = tmp_path / "survey_qid_js_map.csv"
    mapping_csv.write_text(
        "js_file,SV_TEST-demo\n" "test_guard.js,QID1\n",
        encoding="utf-8",
    )

    payload = {
        "result": {
            "SurveyFlow": {
                "Flow": [
                    {"Type": "Standard", "ID": "BL_1"},
                    {
                        "Type": "EndSurvey",
                        "FlowID": "FL_END",
                        "Options": {
                            "SurveyTermination": "DisplayMessage",
                            "EOSMessageLibrary": "UR_LIB",
                            "EOSMessage": "MS_MSG",
                        },
                    },
                ]
            },
            "Blocks": {
                "BL_1": {
                    "Type": "Default",
                    "Description": "Intro",
                    "BlockElements": [
                        {"Type": "Question", "QuestionID": "QID1"},
                        {"Type": "Question", "QuestionID": "QID2"},
                    ],
                },
                "BL_TRASH": {
                    "Type": "Trash",
                    "Description": "Trash",
                    "BlockElements": [
                        {"Type": "Question", "QuestionID": "QID3"},
                    ],
                },
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": "<p>Hello <strong>world</strong></p>",
                    "DataExportTag": "hello_tag",
                    "QuestionJS": "Qualtrics.SurveyEngine.addOnReady(function(){});",
                },
                "QID2": {
                    "QuestionType": "MC",
                    "Selector": "SAVR",
                    "QuestionText": "Pick one",
                    "DataExportTag": "topic_tag",
                    "Choices": {"1": {"Display": "One"}, "2": {"Display": "Two"}},
                    "ChoiceOrder": ["1", "2"],
                },
                "QID3": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": "This is trash",
                    "DataExportTag": "trash_tag",
                },
            },
        }
    }

    out_pdf = tmp_path / "out.pdf"
    out_mmd = tmp_path / "out.flow.mmd"
    export_survey_payload_to_pdf(
        "SV_TEST",
        payload,
        out_pdf,
        mermaid_path=out_mmd,
        mapping_path=mapping_csv,
    )

    assert out_pdf.exists()
    assert out_pdf.suffix == ".pdf"
    assert out_pdf.stat().st_size > 1000  # PDF should be at least 1KB
    assert not out_mmd.exists()


def test_pdf_export_with_translation_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test PDF export with translation language overlay."""
    from qsync.translation_export import export_survey_payload_to_pdf

    # Mock the QSYNC_ROOT to use tmp_path
    monkeypatch.setenv("QSYNC_ROOT", str(tmp_path))

    payload = {
        "result": {
            "SurveyOptions": {
                "SurveyLanguage": "EN",
                "AvailableLanguages": ["EN", "FR"],
            },
            "SurveyFlow": {"Flow": [{"Type": "Standard", "ID": "BL_1"}]},
            "Blocks": {
                "BL_1": {
                    "Type": "Default",
                    "Description": "Main",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
                }
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "MC",
                    "Selector": "SAVR",
                    "QuestionText": "What is your favorite color?",
                    "DataExportTag": "color",
                    "Choices": {"1": {"Display": "Red"}, "2": {"Display": "Blue"}},
                    "ChoiceOrder": ["1", "2"],
                    "Language": {
                        "FR": {
                            "QuestionText": "Quelle est votre couleur préférée?",
                            "Choices": {
                                "1": {"Display": "Rouge"},
                                "2": {"Display": "Bleu"},
                            },
                        }
                    },
                }
            },
        }
    }

    out_pdf = tmp_path / "out_fr.pdf"
    export_survey_payload_to_pdf(
        "SV_TEST",
        payload,
        out_pdf,
        render_language="FR",
    )

    assert out_pdf.exists()
    assert out_pdf.suffix == ".pdf"
    assert out_pdf.stat().st_size > 1000


def test_pdf_export_compare_to_base_bilingual(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test PDF export with bilingual (base + target) mode."""
    from qsync.translation_export import export_survey_payload_to_pdf

    # Mock the QSYNC_ROOT to use tmp_path
    monkeypatch.setenv("QSYNC_ROOT", str(tmp_path))

    payload = {
        "result": {
            "SurveyOptions": {
                "SurveyLanguage": "EN",
                "AvailableLanguages": ["EN", "NL"],
            },
            "SurveyFlow": {"Flow": [{"Type": "Standard", "ID": "BL_1"}]},
            "Blocks": {
                "BL_1": {
                    "Type": "Default",
                    "Description": "Main",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
                }
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": "Enter your name",
                    "DataExportTag": "name",
                    "Language": {"NL": {"QuestionText": "Voer uw naam in"}},
                }
            },
        }
    }

    out_pdf = tmp_path / "out_bilingual.pdf"
    export_survey_payload_to_pdf(
        "SV_TEST",
        payload,
        out_pdf,
        render_language="NL",
        compare_to_base=True,
    )

    assert out_pdf.exists()
    assert out_pdf.suffix == ".pdf"
    # Bilingual PDF should be larger than single-language
    assert out_pdf.stat().st_size > 1000


def test_pdf_html_rendering_fidelity(tmp_path: Path) -> None:
    """Test PDF export with complex HTML (tables, lists, headings)."""
    from qsync.translation_export import export_survey_payload_to_pdf

    payload = {
        "result": {
            "SurveyFlow": {"Flow": [{"Type": "Standard", "ID": "BL_1"}]},
            "Blocks": {
                "BL_1": {
                    "Type": "Default",
                    "Description": "HTML Test",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
                }
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "DB",
                    "Selector": "TB",
                    "QuestionText": """
                        <h1>Main Heading</h1>
                        <p>This is a <strong>bold</strong> and <em>italic</em> text.</p>
                        <ul>
                            <li>Item 1</li>
                            <li>Item 2</li>
                            <li>Item 3</li>
                        </ul>
                        <table>
                            <tr><th>Header 1</th><th>Header 2</th></tr>
                            <tr><td>Cell 1</td><td>Cell 2</td></tr>
                        </table>
                    """,
                    "DataExportTag": "html_test",
                }
            },
        }
    }

    out_pdf = tmp_path / "out_html.pdf"
    export_survey_payload_to_pdf(
        "SV_TEST",
        payload,
        out_pdf,
    )

    assert out_pdf.exists()
    assert out_pdf.suffix == ".pdf"
    assert out_pdf.stat().st_size > 1000


def test_pdf_css_styling_applied(tmp_path: Path) -> None:
    """Test that PDF export includes CSS styling."""
    from qsync.translation_export import _build_pdf_css

    css = _build_pdf_css()

    # Verify key CSS elements are present
    assert "@page" in css
    assert "font-family" in css
    assert ".question" in css
    assert ".translation-summary" in css
    assert ".external-surface" in css
    assert "margin" in css
    assert "table" in css


def test_apply_active_qid_filters_supports_qid_tag_and_block_filters() -> None:
    from qsync.translation_export import _apply_active_qid_filters

    result = {
        "Questions": {
            "QID1": {"DataExportTag": "tag_one"},
            "QID2": {"DataExportTag": "tag_two"},
            "QID3": {"DataExportTag": "tag_three"},
        },
        "Blocks": {
            "BL_A": {
                "BlockElements": [{"Type": "Question", "QuestionID": "QID2"}],
            }
        },
    }

    active = {"QID1", "QID2"}
    assert _apply_active_qid_filters(
        result=result,
        active_qids=active,
        include_qids={"QID1"},
        include_tags=None,
        include_blocks=None,
    ) == {"QID1"}
    assert _apply_active_qid_filters(
        result=result,
        active_qids=active,
        include_qids=None,
        include_tags={"tag_two"},
        include_blocks=None,
    ) == {"QID2"}
    assert _apply_active_qid_filters(
        result=result,
        active_qids=active,
        include_qids=None,
        include_tags=None,
        include_blocks={"BL_A"},
    ) == {"QID2"}


def test_export_word_include_qid_filter_limits_rendered_questions(tmp_path: Path) -> None:
    from qsync.translation_export import export_survey_payload_to_word

    payload = {
        "result": {
            "SurveyFlow": {"Flow": [{"Type": "Standard", "ID": "BL_1"}]},
            "Blocks": {
                "BL_1": {
                    "Type": "Default",
                    "Description": "Main",
                    "BlockElements": [
                        {"Type": "Question", "QuestionID": "QID1"},
                        {"Type": "Question", "QuestionID": "QID2"},
                    ],
                }
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": "Question one",
                    "DataExportTag": "q1",
                },
                "QID2": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": "Question two",
                    "DataExportTag": "q2",
                },
            },
        }
    }

    out_docx = tmp_path / "filtered.docx"
    export_survey_payload_to_word(
        "SV_TEST",
        payload,
        out_docx,
        include_qids={"QID2"},
    )

    d = docx.Document(str(out_docx))
    text = _doc_text(d)
    assert "[QID2][TE] q2" in text
    assert "[QID1][TE] q1" not in text
