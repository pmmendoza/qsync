from __future__ import annotations

import json
from pathlib import Path
import unittest

from qsync.dimensions.items_core import stage_rename_embedded_field
from qsync.qualtrics_client import load_cached_survey


def _payload(*, include_second_debug: bool = False) -> dict:
    second_field = "DEBUG" if include_second_debug else "ALT_FIELD"
    return {
        "result": {
            "SurveyFlow": {
                "Flow": [
                    {
                        "Type": "EmbeddedData",
                        "FlowID": "FL_1",
                        "EmbeddedData": [
                            {
                                "Field": "DEBUG",
                                "Type": "Custom",
                                "Value": "F",
                                "Description": "DEBUG",
                                "DataVisibility": [],
                                "AnalyzeText": False,
                            },
                            {
                                "Field": "COUNTRY",
                                "Type": "Custom",
                                "Value": "IE",
                                "Description": "COUNTRY",
                                "DataVisibility": [],
                                "AnalyzeText": False,
                            },
                        ],
                    },
                    {
                        "Type": "EmbeddedData",
                        "FlowID": "FL_2",
                        "EmbeddedData": [
                            {
                                "Field": second_field,
                                "Type": "Custom",
                                "Value": "X",
                                "Description": second_field,
                                "DataVisibility": [],
                                "AnalyzeText": False,
                            }
                        ],
                    },
                ]
            },
            "Questions": {},
            "Blocks": {},
        }
    }


def _write_cached_survey(root: Path, survey_id: str, payload: dict) -> None:
    surveys_dir = root / "surveys"
    backups_dir = surveys_dir / "backups"
    surveys_dir.mkdir(parents=True, exist_ok=True)
    backups_dir.mkdir(parents=True, exist_ok=True)
    cached_path = surveys_dir / f"TEST__{survey_id}.json"
    backup_path = backups_dir / f"TEST__{survey_id}.json"
    serialized = json.dumps(payload, indent=2)
    cached_path.write_text(serialized, encoding="utf-8")
    backup_path.write_text(serialized, encoding="utf-8")


def _list_fields(cache_payload: dict, flow_id: str) -> list[str]:
    for node in cache_payload.get("result", {}).get("SurveyFlow", {}).get("Flow", []):
        if node.get("FlowID") == flow_id:
            return [
                str(entry.get("Field") or "").strip()
                for entry in (node.get("EmbeddedData") or [])
            ]
    return []


class EmbeddedDataRenameTests(unittest.TestCase):
    def test_stage_rename_embedded_field_renames_unique_match(self) -> None:
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            survey_id = "SV_RENAME_1"
            _write_cached_survey(root, survey_id, _payload(include_second_debug=False))
            with patch.dict("os.environ", {"QSYNC_ROOT": str(root)}, clear=False):
                renamed = stage_rename_embedded_field(
                    survey_id,
                    old_field="DEBUG",
                    new_field="DEBUG_NEW",
                )
                cached = load_cached_survey(survey_id)

        self.assertEqual(
            renamed,
            [{"flow_id": "FL_1", "from_field": "DEBUG", "field": "DEBUG_NEW"}],
        )
        self.assertIn("DEBUG_NEW", _list_fields(cached.payload, "FL_1"))
        self.assertNotIn("DEBUG", _list_fields(cached.payload, "FL_1"))

    def test_stage_rename_embedded_field_dry_run_does_not_mutate(self) -> None:
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            survey_id = "SV_RENAME_DRY"
            _write_cached_survey(root, survey_id, _payload(include_second_debug=False))
            with patch.dict("os.environ", {"QSYNC_ROOT": str(root)}, clear=False):
                renamed = stage_rename_embedded_field(
                    survey_id,
                    old_field="DEBUG",
                    new_field="DEBUG_NEW",
                    dry_run=True,
                )
                cached = load_cached_survey(survey_id)

        self.assertEqual(
            renamed,
            [{"flow_id": "FL_1", "from_field": "DEBUG", "field": "DEBUG_NEW"}],
        )
        self.assertIn("DEBUG", _list_fields(cached.payload, "FL_1"))
        self.assertNotIn("DEBUG_NEW", _list_fields(cached.payload, "FL_1"))

    def test_stage_rename_embedded_field_requires_scope_when_ambiguous(self) -> None:
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            survey_id = "SV_RENAME_AMBIG"
            _write_cached_survey(root, survey_id, _payload(include_second_debug=True))
            with patch.dict("os.environ", {"QSYNC_ROOT": str(root)}, clear=False):
                with self.assertRaisesRegex(ValueError, "use --all or --flow-id"):
                    stage_rename_embedded_field(
                        survey_id,
                        old_field="DEBUG",
                        new_field="DEBUG_NEW",
                    )

    def test_stage_rename_embedded_field_all_occurrences(self) -> None:
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            survey_id = "SV_RENAME_ALL"
            _write_cached_survey(root, survey_id, _payload(include_second_debug=True))
            with patch.dict("os.environ", {"QSYNC_ROOT": str(root)}, clear=False):
                renamed = stage_rename_embedded_field(
                    survey_id,
                    old_field="DEBUG",
                    new_field="DEBUG_NEW",
                    all_occurrences=True,
                )
                cached = load_cached_survey(survey_id)

        self.assertEqual(len(renamed), 2)
        self.assertEqual({entry["flow_id"] for entry in renamed}, {"FL_1", "FL_2"})
        self.assertIn("DEBUG_NEW", _list_fields(cached.payload, "FL_1"))
        self.assertIn("DEBUG_NEW", _list_fields(cached.payload, "FL_2"))

    def test_stage_rename_embedded_field_flow_scope(self) -> None:
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            survey_id = "SV_RENAME_SCOPE"
            _write_cached_survey(root, survey_id, _payload(include_second_debug=True))
            with patch.dict("os.environ", {"QSYNC_ROOT": str(root)}, clear=False):
                renamed = stage_rename_embedded_field(
                    survey_id,
                    old_field="DEBUG",
                    new_field="DEBUG_NEW",
                    flow_id="FL_2",
                )
                cached = load_cached_survey(survey_id)

        self.assertEqual(
            renamed,
            [{"flow_id": "FL_2", "from_field": "DEBUG", "field": "DEBUG_NEW"}],
        )
        self.assertIn("DEBUG", _list_fields(cached.payload, "FL_1"))
        self.assertIn("DEBUG_NEW", _list_fields(cached.payload, "FL_2"))

    def test_stage_rename_embedded_field_rejects_existing_target(self) -> None:
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            survey_id = "SV_RENAME_CONFLICT"
            _write_cached_survey(root, survey_id, _payload(include_second_debug=False))
            with patch.dict("os.environ", {"QSYNC_ROOT": str(root)}, clear=False):
                with self.assertRaisesRegex(
                    ValueError, "already exists in FlowID=FL_1"
                ):
                    stage_rename_embedded_field(
                        survey_id,
                        old_field="DEBUG",
                        new_field="COUNTRY",
                    )


if __name__ == "__main__":
    unittest.main()
