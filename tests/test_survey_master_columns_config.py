import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class SurveyMasterColumnsConfigTests(unittest.TestCase):
    def test_yaml_overrides_order_and_visibility(self) -> None:
        from qsync.survey_master import _get_column_order

        mapping = {
            "SurveyID": {"field_name": "SurveyID", "order": "1"},
            "SurveyName": {"field_name": "SurveyName", "order": "2"},
            "Footer": {"field_name": "Footer", "order": "3"},
        }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "survey_master_columns.yaml").write_text(
                "\n".join(
                    [
                        "version: 1",
                        "columns:",
                        "  - name: SurveyName",
                        "    enabled: true",
                        "  - name: Footer",
                        "    enabled: false",
                        "  - name: SurveyID",
                        "    enabled: true",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"QSYNC_ROOT": str(root), "QSYNC_JSON_MODE": "1"}):
                with patch("qsync.survey_master._parse_mapping_csv", return_value=mapping):
                    cols = _get_column_order()

        self.assertEqual(cols, ["SurveyName", "SurveyID"])

    def test_survey_id_is_pinned_and_inserted_if_missing(self) -> None:
        from qsync.survey_master import _get_column_order

        mapping = {
            "SurveyID": {"field_name": "SurveyID", "order": "1"},
            "SurveyName": {"field_name": "SurveyName", "order": "2"},
        }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "survey_master_columns.yaml").write_text(
                "\n".join(
                    [
                        "version: 1",
                        "columns:",
                        "  - name: SurveyName",
                        "    enabled: true",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"QSYNC_ROOT": str(root), "QSYNC_JSON_MODE": "1"}):
                with patch("qsync.survey_master._parse_mapping_csv", return_value=mapping):
                    cols = _get_column_order()

        self.assertEqual(cols, ["SurveyID", "SurveyName"])

    def test_unknown_columns_in_yaml_are_ignored(self) -> None:
        from qsync.survey_master import _get_column_order

        mapping = {
            "SurveyID": {"field_name": "SurveyID", "order": "1"},
            "SurveyName": {"field_name": "SurveyName", "order": "2"},
        }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "survey_master_columns.yaml").write_text(
                "\n".join(
                    [
                        "version: 1",
                        "columns:",
                        "  - name: NotARealField",
                        "    enabled: true",
                        "  - name: SurveyName",
                        "    enabled: true",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"QSYNC_ROOT": str(root), "QSYNC_JSON_MODE": "1"}):
                with patch("qsync.survey_master._parse_mapping_csv", return_value=mapping):
                    cols = _get_column_order()

        self.assertEqual(cols, ["SurveyID", "SurveyName"])

