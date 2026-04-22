from __future__ import annotations

import csv
import json
import shutil
import subprocess
from pathlib import Path

import pytest


def _write_stage0_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    raw_dir = tmp_path / "bundle" / "raw"
    raw_dir.mkdir(parents=True)

    ndjson_path = raw_dir / "responses.ndjson"
    responses = [
        {
            "responseId": "R_1",
            "values": {
                "startDate": "2026-04-01T00:00:00Z",
                "_recordId": "R_1",
                "QID1": 1,
            },
            "labels": {"QID1": "Yes"},
            "displayedFields": ["QID1", "QID2"],
            "displayedValues": {"QID1": [1, 2]},
        },
        {
            "responseId": "R_2",
            "values": {
                "startDate": "2026-04-01T00:01:00Z",
                "_recordId": "R_2",
                "QID1": 2,
            },
            "labels": {"QID1": "No"},
            "displayedFields": ["QID2", "QID1"],
            "displayedValues": {"QID1": [2, 1]},
        },
    ]
    ndjson_path.write_text(
        "\n".join(json.dumps(row) for row in responses) + "\n",
        encoding="utf-8",
    )

    display_csv_path = raw_dir / "qualtrics-display-order.csv"
    with display_csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "StartDate",
                "ResponseId",
                "question_1",
                "question_1_DO_1",
                "question_1_DO_2",
            ]
        )
        writer.writerow(
            [
                "Start Date",
                "Response ID",
                "Question 1",
                "Question 1 Display Order 1",
                "Question 1 Display Order 2",
            ]
        )
        writer.writerow(
            [
                '{"ImportId":"startDate","timeZone":"UTC"}',
                '{"ImportId":"_recordId"}',
                '{"ImportId":"QID1"}',
                '{"ImportId":"QID1_DO","choiceId":"1"}',
                '{"ImportId":"QID1_DO","choiceId":"2"}',
            ]
        )
        writer.writerow(["2026-04-01 00:00:00", "R_1", "Yes", "1", "2"])
        writer.writerow(["2026-04-01 00:01:00", "R_2", "No", "2", "1"])

    survey_definition_path = raw_dir / "survey-definition.json"
    survey_definition_path.write_text(
        json.dumps(
            {
                "Questions": {
                    "QID1": {
                        "DataExportTag": "question_1",
                        "QuestionType": "MC",
                        "Selector": "SAVR",
                        "SubSelector": "TX",
                        "QuestionText": "Question 1?",
                        "Choices": {
                            "1": {"Display": "Yes", "Recode": "1"},
                            "2": {"Display": "No", "Recode": "2"},
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return ndjson_path, display_csv_path, survey_definition_path


def test_build_enriched_response_bundle_csv(tmp_path: Path) -> None:
    from qsync.response_dataset import build_enriched_response_bundle

    output_dir = tmp_path / "bundle"
    ndjson_path, display_csv_path, survey_definition_path = _write_stage0_fixture(
        tmp_path
    )

    result = build_enriched_response_bundle(
        output_dir=output_dir,
        ndjson_path=ndjson_path,
        display_order_csv_path=display_csv_path,
        survey_definition_path=survey_definition_path,
        survey_id="SV_TEST",
        survey_name="Fixture Survey",
        account="damian",
        formats=("csv",),
        command_args={"analysis_bundle": True},
        raw_exports=[],
        created_at_utc="2026-04-22T00:00:00+00:00",
    )

    assert result.row_count == 2
    assert (output_dir / "responses_enriched.csv").exists()
    assert (output_dir / "codebook.csv").exists()
    assert (output_dir / "manifest.json").exists()
    assert (output_dir / "raw" / "export-manifest.json").exists()

    with (output_dir / "responses_enriched.csv").open(
        encoding="utf-8", newline=""
    ) as f:
        rows = list(csv.DictReader(f))

    assert rows[0]["qsync_survey_id"] == "SV_TEST"
    assert rows[0]["ResponseId"] == "R_1"
    assert rows[0]["question_1"] == "1"
    assert rows[0]["question_1__label"] == "Yes"
    assert rows[0]["question_1__displayed_values"] == "1|2"
    assert rows[0]["qsync_displayed_fields"] == "QID1|QID2"
    assert rows[0]["question_1_DO_1"] == "1"
    assert rows[1]["question_1_DO_1"] == "2"

    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["row_count"] == 2
    assert manifest["analysis_formats"] == ["csv"]
    assert any(
        item["path"] == "raw/qualtrics-display-order.csv"
        for item in manifest["raw_files"]
    )


def test_parse_display_order_csv_requires_response_id(tmp_path: Path) -> None:
    from qsync.response_dataset import ResponseDatasetError, parse_display_order_csv

    path = tmp_path / "bad.csv"
    path.write_text("A\nA label\n{\"ImportId\":\"QID1\"}\n1\n", encoding="utf-8")

    try:
        parse_display_order_csv(path)
    except ResponseDatasetError as exc:
        assert "ResponseId" in str(exc)
    else:
        raise AssertionError("Expected ResponseDatasetError")


def test_validate_analysis_format_dependencies_fails_before_rds_without_rscript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qsync import response_dataset

    monkeypatch.setattr(response_dataset.shutil, "which", lambda _name: None)

    with pytest.raises(response_dataset.ResponseDatasetError, match="Rscript"):
        response_dataset.validate_analysis_format_dependencies(("rds",))


def test_validate_analysis_format_dependencies_fails_before_sav_without_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qsync import response_dataset

    def _missing_pyreadstat(name: str):
        if name == "pyreadstat":
            return None
        return object()

    monkeypatch.setattr(
        response_dataset.importlib.util,
        "find_spec",
        _missing_pyreadstat,
    )

    with pytest.raises(response_dataset.ResponseDatasetError, match="SAV"):
        response_dataset.validate_analysis_format_dependencies(("sav",))


def test_build_enriched_response_bundle_sav_round_trip(tmp_path: Path) -> None:
    pyreadstat = pytest.importorskip("pyreadstat")
    pytest.importorskip("pandas")

    from qsync.response_dataset import build_enriched_response_bundle

    output_dir = tmp_path / "bundle"
    ndjson_path, display_csv_path, survey_definition_path = _write_stage0_fixture(
        tmp_path
    )

    build_enriched_response_bundle(
        output_dir=output_dir,
        ndjson_path=ndjson_path,
        display_order_csv_path=display_csv_path,
        survey_definition_path=survey_definition_path,
        survey_id="SV_TEST",
        survey_name="Fixture Survey",
        account=None,
        formats=("sav",),
        created_at_utc="2026-04-22T00:00:00+00:00",
    )

    df, meta = pyreadstat.read_sav(str(output_dir / "responses_enriched.sav"))
    assert len(df) == 2
    assert "question_1" in df.columns
    assert dict(zip(df.columns, meta.column_labels))["question_1"] == "Question 1?"
    assert meta.variable_value_labels["question_1"][1.0] == "Yes"


def test_build_enriched_response_bundle_parquet_round_trip(tmp_path: Path) -> None:
    pytest.importorskip("pandas")
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")

    from qsync.response_dataset import build_enriched_response_bundle

    output_dir = tmp_path / "bundle"
    ndjson_path, display_csv_path, survey_definition_path = _write_stage0_fixture(
        tmp_path
    )

    build_enriched_response_bundle(
        output_dir=output_dir,
        ndjson_path=ndjson_path,
        display_order_csv_path=display_csv_path,
        survey_definition_path=survey_definition_path,
        survey_id="SV_TEST",
        survey_name="Fixture Survey",
        account=None,
        formats=("parquet",),
        created_at_utc="2026-04-22T00:00:00+00:00",
    )

    table = pq.read_table(output_dir / "responses_enriched.parquet")
    assert table.num_rows == 2
    assert "question_1" in table.column_names
    assert table.schema.metadata[b"qsync_schema"] == b"qsync.response_bundle.v1"
    assert pa.table({"x": [1]}).num_rows == 1


def test_build_enriched_response_bundle_rds_round_trip(tmp_path: Path) -> None:
    if not shutil.which("Rscript"):
        pytest.skip("Rscript is not available")

    from qsync.response_dataset import build_enriched_response_bundle

    output_dir = tmp_path / "bundle"
    ndjson_path, display_csv_path, survey_definition_path = _write_stage0_fixture(
        tmp_path
    )

    build_enriched_response_bundle(
        output_dir=output_dir,
        ndjson_path=ndjson_path,
        display_order_csv_path=display_csv_path,
        survey_definition_path=survey_definition_path,
        survey_id="SV_TEST",
        survey_name="Fixture Survey",
        account=None,
        formats=("rds",),
        created_at_utc="2026-04-22T00:00:00+00:00",
    )

    check = subprocess.run(
        [
            "Rscript",
            "-e",
            (
                "df <- readRDS(commandArgs(TRUE)[1]); "
                "cat(nrow(df), attr(df[['question_1']], 'label'), "
                "names(attr(df[['question_1']], 'labels'))[1], sep='|')"
            ),
            str(output_dir / "responses_enriched.rds"),
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert check.stdout == "2|Question 1?|Yes"
