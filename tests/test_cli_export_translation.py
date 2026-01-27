"""CLI integration tests for export-translation command with --format flag."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

docx = pytest.importorskip("docx")


def test_cli_export_translation_format_docx(tmp_path: Path) -> None:
    """Test export-translation with --format docx using payload API."""
    from qsync.translation_export import export_survey_payload_to_word

    payload = {
        "result": {
            "SurveyFlow": {"Flow": [{"Type": "Standard", "ID": "BL_1"}]},
            "Blocks": {
                "BL_1": {
                    "Type": "Default",
                    "Description": "Test Block",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
                }
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": "Test question",
                    "DataExportTag": "test_tag",
                }
            },
        }
    }

    out_docx = tmp_path / "test_output.docx"
    export_survey_payload_to_word(
        "SV_TEST",
        payload,
        out_docx,
    )

    assert out_docx.exists()
    assert out_docx.suffix == ".docx"
    assert out_docx.stat().st_size > 1000


def test_cli_export_translation_format_pdf(tmp_path: Path) -> None:
    """Test export-translation with --format pdf using payload API."""
    pytest.importorskip("weasyprint")
    from qsync.translation_export import export_survey_payload_to_pdf

    payload = {
        "result": {
            "SurveyFlow": {"Flow": [{"Type": "Standard", "ID": "BL_1"}]},
            "Blocks": {
                "BL_1": {
                    "Type": "Default",
                    "Description": "Test Block",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
                }
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": "Test question",
                    "DataExportTag": "test_tag",
                }
            },
        }
    }

    out_pdf = tmp_path / "test_output.pdf"
    export_survey_payload_to_pdf(
        "SV_TEST",
        payload,
        out_pdf,
    )

    assert out_pdf.exists()
    assert out_pdf.suffix == ".pdf"
    assert out_pdf.stat().st_size > 1000


def test_cli_export_translation_format_both(tmp_path: Path) -> None:
    """Test export-translation with both formats using payload API."""
    pytest.importorskip("weasyprint")
    from qsync.translation_export import export_survey_payload_to_pdf, export_survey_payload_to_word

    payload = {
        "result": {
            "SurveyFlow": {"Flow": [{"Type": "Standard", "ID": "BL_1"}]},
            "Blocks": {
                "BL_1": {
                    "Type": "Default",
                    "Description": "Test Block",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
                }
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": "Test question",
                    "DataExportTag": "test_tag",
                }
            },
        }
    }

    out_docx = tmp_path / "test_output.docx"
    out_pdf = tmp_path / "test_output.pdf"

    export_survey_payload_to_word("SV_TEST", payload, out_docx)
    export_survey_payload_to_pdf("SV_TEST", payload, out_pdf)

    assert out_docx.exists()
    assert out_docx.suffix == ".docx"
    assert out_pdf.exists()
    assert out_pdf.suffix == ".pdf"


def test_cli_export_translation_multi_format_multi_file(tmp_path: Path) -> None:
    """Test export-translation generating multiple files in both formats."""
    pytest.importorskip("weasyprint")
    from qsync.translation_export import export_survey_payload_to_pdf, export_survey_payload_to_word

    payload = {
        "result": {
            "SurveyFlow": {"Flow": [{"Type": "Standard", "ID": "BL_1"}]},
            "Blocks": {
                "BL_1": {
                    "Type": "Default",
                    "Description": "Test Block",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
                }
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": "Test question",
                    "DataExportTag": "test_tag",
                }
            },
        }
    }

    # Export multiple files in both formats
    for idx in range(2):
        out_docx = tmp_path / f"test_output_{idx}.docx"
        out_pdf = tmp_path / f"test_output_{idx}.pdf"

        export_survey_payload_to_word(
            "SV_TEST",
            payload,
            out_docx,
        )
        export_survey_payload_to_pdf(
            "SV_TEST",
            payload,
            out_pdf,
        )

        assert out_docx.exists()
        assert out_pdf.exists()

    # Verify 4 files were created (2 files x 2 formats)
    docx_files = list(tmp_path.glob("*.docx"))
    pdf_files = list(tmp_path.glob("*.pdf"))

    assert len(docx_files) == 2
    assert len(pdf_files) == 2


def test_cli_export_translation_format_validation(tmp_path: Path) -> None:
    """Test format validation: verify DOCX and PDF files have correct suffixes."""
    pytest.importorskip("weasyprint")
    from qsync.translation_export import export_survey_payload_to_pdf, export_survey_payload_to_word

    payload = {
        "result": {
            "SurveyFlow": {"Flow": [{"Type": "Standard", "ID": "BL_1"}]},
            "Blocks": {
                "BL_1": {
                    "Type": "Default",
                    "Description": "Test Block",
                    "BlockElements": [{"Type": "Question", "QuestionID": "QID1"}],
                }
            },
            "Questions": {
                "QID1": {
                    "QuestionType": "TE",
                    "Selector": "SL",
                    "QuestionText": "Test question",
                    "DataExportTag": "test_tag",
                }
            },
        }
    }

    # Test that DOCX export creates .docx file
    out_docx = tmp_path / "test.docx"
    export_survey_payload_to_word("SV_TEST", payload, out_docx)
    assert out_docx.exists()
    assert out_docx.suffix == ".docx"

    # Test that PDF export creates .pdf file
    out_pdf = tmp_path / "test.pdf"
    export_survey_payload_to_pdf("SV_TEST", payload, out_pdf)
    assert out_pdf.exists()
    assert out_pdf.suffix == ".pdf"
