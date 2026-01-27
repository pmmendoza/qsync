"""Tests for embedded data extraction and JS detection."""

from __future__ import annotations

import unittest

from qsync import excel_io


def _sample_payload() -> dict:
    return {
        "result": {
            "SurveyFlow": {
                "Flow": [
                    {
                        "Type": "EmbeddedData",
                        "FlowID": "FL_1",
                        "EmbeddedData": [
                            {
                                "Field": "alpha",
                                "Type": "Custom",
                                "Value": "A",
                                "Description": "alpha",
                                "DataVisibility": [],
                                "AnalyzeText": False,
                            },
                            {
                                "Field": "beta",
                                "Type": "Recipient",
                                "Value": None,
                                "Description": "beta",
                                "DataVisibility": [],
                                "AnalyzeText": False,
                            },
                        ],
                    }
                ]
            },
            "Questions": {
                "QID1": {
                    "QuestionJS": (
                        "Qualtrics.SurveyEngine.setEmbeddedData('alpha', 'x');\n"
                        'Qualtrics.SurveyEngine.setEmbeddedData("gamma", 1);'
                    )
                },
                "QID2": {
                    "QuestionJS": (
                        "// comment\n"
                        "Qualtrics.SurveyEngine.setEmbeddedData('gamma', 2);"
                    )
                },
            },
            "Blocks": {},
        }
    }


class EmbeddedDataExtractionTests(unittest.TestCase):
    def test_build_embedded_data_rows(self) -> None:
        payload = _sample_payload()
        rows = excel_io.build_embedded_data_rows("SV_TEST", payload)
        by_field = {row.field: row for row in rows}

        alpha = by_field["alpha"]
        self.assertEqual(alpha.ed_type, "Custom")
        self.assertEqual(alpha.value, "A")
        self.assertEqual(alpha.written_by_qids, "QID1")

        beta = by_field["beta"]
        self.assertEqual(beta.ed_type, "Recipient")
        self.assertIsNone(beta.value)

        gamma = by_field["gamma"]
        self.assertEqual(gamma.ed_type, "JS-only")
        self.assertEqual(gamma.flow_order, 0)
        self.assertEqual(gamma.written_by_qids, "QID1,QID2")
