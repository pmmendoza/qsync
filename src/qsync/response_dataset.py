"""Build enriched, analysis-friendly response export bundles."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


RESPONSE_BUNDLE_SCHEMA_VERSION = "qsync.response_bundle.v1"
RAW_RESPONSE_MANIFEST_SCHEMA_VERSION = "qsync.response_export.raw_manifest.v1"
LIST_DELIMITER = "|"
SUPPORTED_ANALYSIS_FORMATS = ("csv", "sav", "rds", "parquet")


class ResponseDatasetError(RuntimeError):
    """Raised when qsync cannot build a requested response dataset."""


@dataclass(frozen=True)
class EnrichedResponseBundleResult:
    """Files and dimensions produced by one enriched response bundle build."""

    output_dir: Path
    row_count: int
    column_count: int
    output_files: tuple[Path, ...]
    manifest_path: Path
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class CsvColumnMeta:
    """Qualtrics CSV header metadata for one column."""

    header: str
    label: str
    import_id: str
    choice_id: str
    raw_import_metadata: dict[str, Any]


@dataclass(frozen=True)
class DisplayOrderCsv:
    """Parsed include-display-order CSV export data."""

    columns: tuple[CsvColumnMeta, ...]
    import_to_header: dict[str, str]
    rows_by_response_id: dict[str, dict[str, str]]
    display_order_headers: tuple[str, ...]


def utc_now_iso() -> str:
    """Return a stable UTC timestamp string for manifests and metadata."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_record(path: Path, *, relative_to: Path | None = None) -> dict[str, Any]:
    display_path = path
    if relative_to is not None:
        try:
            display_path = path.relative_to(relative_to)
        except ValueError:
            display_path = path
    return {
        "path": str(display_path),
        "bytes": path.stat().st_size,
        "sha256": file_sha256(path),
    }


def normalize_analysis_formats(raw: str | Sequence[str] | None) -> tuple[str, ...]:
    if raw is None:
        parts = ["csv"]
    elif isinstance(raw, str):
        parts = [p.strip().lower() for p in raw.replace(";", ",").split(",")]
    else:
        parts = []
        for item in raw:
            parts.extend(str(item).replace(";", ",").split(","))
        parts = [p.strip().lower() for p in parts]

    formats: list[str] = []
    for part in parts:
        if not part:
            continue
        if part not in SUPPORTED_ANALYSIS_FORMATS:
            allowed = ", ".join(SUPPORTED_ANALYSIS_FORMATS)
            raise ResponseDatasetError(
                f"Unsupported analysis format '{part}'. Choose from: {allowed}."
            )
        if part not in formats:
            formats.append(part)
    if not formats:
        formats.append("csv")
    return tuple(formats)


def validate_analysis_format_dependencies(formats: Sequence[str]) -> None:
    """Fail before remote exports when requested writers are unavailable."""

    normalized = normalize_analysis_formats(formats)
    if "sav" in normalized and (
        importlib.util.find_spec("pandas") is None
        or importlib.util.find_spec("pyreadstat") is None
    ):
        raise ResponseDatasetError(
            "Writing SAV requires optional dependencies. Install qsync with "
            "`qsync[responses-sav]` or install `pandas` and `pyreadstat`."
        )
    if "parquet" in normalized and (
        importlib.util.find_spec("pandas") is None
        or importlib.util.find_spec("pyarrow") is None
    ):
        raise ResponseDatasetError(
            "Writing Parquet requires optional dependencies. Install qsync with "
            "`qsync[responses-parquet]` or install `pandas` and `pyarrow`."
        )
    if "rds" in normalized and shutil.which("Rscript") is None:
        raise ResponseDatasetError(
            "Writing RDS requires `Rscript` on PATH so qsync can preserve "
            "R-native labels and value-label attributes."
        )


def load_ndjson_responses(path: Path) -> list[dict[str, Any]]:
    responses: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                response = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ResponseDatasetError(
                    f"Invalid NDJSON at {path}:{line_number}: {exc}"
                ) from exc
            if not isinstance(response, dict):
                raise ResponseDatasetError(
                    f"Invalid NDJSON at {path}:{line_number}: expected object row."
                )
            responses.append(response)
    return responses


def parse_display_order_csv(path: Path) -> DisplayOrderCsv:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        try:
            headers = next(reader)
            labels = next(reader)
            imports = next(reader)
        except StopIteration as exc:
            raise ResponseDatasetError(
                f"Display-order CSV must contain three Qualtrics header rows: {path}"
            ) from exc
        if not (len(headers) == len(labels) == len(imports)):
            raise ResponseDatasetError(
                f"Display-order CSV header row lengths differ: {path}"
            )

        columns: list[CsvColumnMeta] = []
        import_to_header: dict[str, str] = {}
        display_order_headers: list[str] = []
        response_id_index: int | None = None
        for index, (header, label, import_text) in enumerate(
            zip(headers, labels, imports)
        ):
            try:
                import_meta = json.loads(import_text) if import_text else {}
            except json.JSONDecodeError:
                import_meta = {}
            import_id = str(import_meta.get("ImportId") or "")
            choice_id = str(import_meta.get("choiceId") or "")
            meta = CsvColumnMeta(
                header=header,
                label=label,
                import_id=import_id,
                choice_id=choice_id,
                raw_import_metadata=import_meta,
            )
            columns.append(meta)
            if import_id and not choice_id and import_id not in import_to_header:
                import_to_header[import_id] = header
            if _is_display_order_column(meta):
                display_order_headers.append(header)
            if header == "ResponseId" or import_id == "_recordId":
                response_id_index = index

        if response_id_index is None:
            raise ResponseDatasetError(
                f"Display-order CSV has no ResponseId/_recordId column: {path}"
            )

        rows_by_response_id: dict[str, dict[str, str]] = {}
        for row in reader:
            if not row:
                continue
            padded = row + [""] * max(0, len(headers) - len(row))
            response_id = padded[response_id_index].strip()
            if not response_id:
                continue
            rows_by_response_id[response_id] = {
                header: padded[index] if index < len(padded) else ""
                for index, header in enumerate(headers)
            }

    return DisplayOrderCsv(
        columns=tuple(columns),
        import_to_header=import_to_header,
        rows_by_response_id=rows_by_response_id,
        display_order_headers=tuple(display_order_headers),
    )


def _is_display_order_column(meta: CsvColumnMeta) -> bool:
    return meta.import_id.endswith("_DO") or "_DO_" in meta.header


def _ordered_keys_from_responses(
    responses: Iterable[dict[str, Any]], container_key: str
) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for response in responses:
        container = response.get(container_key) or {}
        if not isinstance(container, Mapping):
            continue
        for key in container:
            if key not in seen:
                seen.add(str(key))
                keys.append(str(key))
    return keys


def _cell_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _join_ordered_values(values: Any) -> str:
    if values is None:
        return ""
    if not isinstance(values, list):
        values = [values]
    parts = ["" if item is None else str(item) for item in values]
    if any(LIST_DELIMITER in part or "\n" in part or "\r" in part for part in parts):
        return json.dumps(parts, ensure_ascii=False)
    return LIST_DELIMITER.join(parts)


def _dedupe_column_names(
    names: Iterable[str],
) -> tuple[list[str], dict[str, str], list[str]]:
    out: list[str] = []
    seen: dict[str, int] = {}
    mapping: dict[str, str] = {}
    warnings: list[str] = []
    for name in names:
        base = name or "unnamed"
        count = seen.get(base, 0)
        if count == 0:
            final = base
        else:
            final = f"{base}__{count + 1}"
            warnings.append(f"Column name collision resolved: {base} -> {final}")
        seen[base] = count + 1
        out.append(final)
        mapping[name] = final
    return out, mapping, warnings


def _survey_payload(survey_definition: Mapping[str, Any]) -> Mapping[str, Any]:
    result = survey_definition.get("result")
    return result if isinstance(result, Mapping) else survey_definition


def _questions_by_id(survey_definition: Mapping[str, Any]) -> Mapping[str, Any]:
    questions = _survey_payload(survey_definition).get("Questions") or {}
    return questions if isinstance(questions, Mapping) else {}


def _qid_from_import_id(import_id: str) -> str:
    match = re.match(r"^(QID\d+)", import_id or "")
    return match.group(1) if match else ""


def _strip_html(value: Any) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _question_text(question: Mapping[str, Any] | None) -> str:
    if not question:
        return ""
    return _strip_html(
        question.get("QuestionText") or question.get("Description") or ""
    )


def _choice_or_answer_text(
    question: Mapping[str, Any] | None,
    *,
    choice_id: str = "",
    answer_id: str = "",
) -> tuple[str, str]:
    if not question:
        return "", ""
    choice_text = ""
    answer_text = ""
    choices = question.get("Choices") or {}
    answers = question.get("Answers") or {}
    if choice_id and isinstance(choices, Mapping):
        choice = choices.get(str(choice_id)) or {}
        if isinstance(choice, Mapping):
            choice_text = _strip_html(
                choice.get("Display") or choice.get("Text") or choice.get("Label") or ""
            )
    if answer_id and isinstance(answers, Mapping):
        answer = answers.get(str(answer_id)) or {}
        if isinstance(answer, Mapping):
            answer_text = _strip_html(
                answer.get("Display") or answer.get("Text") or answer.get("Label") or ""
            )
    return choice_text, answer_text


def _value_labels_json(question: Mapping[str, Any] | None) -> str:
    if not question:
        return ""
    labels: dict[str, str] = {}
    for container_name in ("Choices", "Answers"):
        container = question.get(container_name) or {}
        if not isinstance(container, Mapping):
            continue
        for item_id, item in container.items():
            if not isinstance(item, Mapping):
                continue
            key = item.get("Recode") or item.get("VariableName") or item_id
            label = _strip_html(
                item.get("Display") or item.get("Text") or item.get("Label") or ""
            )
            if label:
                labels[str(key)] = label
    return json.dumps(labels, ensure_ascii=False, sort_keys=True) if labels else ""


def _codebook_row(
    *,
    variable: str,
    source: str,
    import_id: str = "",
    qid: str = "",
    questions: Mapping[str, Any],
    csv_meta: CsvColumnMeta | None = None,
    notes: str = "",
) -> dict[str, str]:
    qid = qid or _qid_from_import_id(import_id)
    question = questions.get(qid) if qid else None
    if not isinstance(question, Mapping):
        question = None
    choice_id = csv_meta.choice_id if csv_meta is not None else ""
    choice_text, answer_text = _choice_or_answer_text(
        question,
        choice_id=choice_id,
        answer_id="",
    )
    return {
        "variable": variable,
        "source": source,
        "qid": qid,
        "export_tag": str(question.get("DataExportTag") or "") if question else "",
        "import_id": import_id,
        "question_type": str(question.get("QuestionType") or "") if question else "",
        "selector": str(question.get("Selector") or "") if question else "",
        "subselector": str(question.get("SubSelector") or "") if question else "",
        "question_text": _question_text(question),
        "choice_id": choice_id,
        "choice_text": choice_text,
        "answer_id": "",
        "answer_text": answer_text,
        "value_labels_json": _value_labels_json(question),
        "notes": notes,
    }


def _write_csv(
    path: Path,
    rows: Sequence[Mapping[str, Any]],
    columns: Sequence[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {column: _cell_value(row.get(column, "")) for column in columns}
            )


def _write_codebook(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    columns = [
        "variable",
        "source",
        "qid",
        "export_tag",
        "import_id",
        "question_type",
        "selector",
        "subselector",
        "question_text",
        "choice_id",
        "choice_text",
        "answer_id",
        "answer_text",
        "value_labels_json",
        "notes",
    ]
    _write_csv(path, rows, columns)


def build_enriched_response_bundle(
    *,
    output_dir: Path,
    ndjson_path: Path,
    display_order_csv_path: Path,
    survey_definition_path: Path,
    survey_id: str,
    survey_name: str,
    account: str | None,
    formats: Sequence[str],
    keep_json_path: Path | None = None,
    command_args: Mapping[str, Any] | None = None,
    raw_exports: Sequence[Mapping[str, Any]] | None = None,
    created_at_utc: str | None = None,
) -> EnrichedResponseBundleResult:
    """Build a compact enriched response bundle from raw Qualtrics exports."""

    normalized_formats = normalize_analysis_formats(formats)
    created_at_utc = created_at_utc or utc_now_iso()
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    responses = load_ndjson_responses(ndjson_path)
    display_csv = parse_display_order_csv(display_order_csv_path)
    survey_definition = json.loads(survey_definition_path.read_text(encoding="utf-8"))
    questions = _questions_by_id(survey_definition)

    warnings: list[str] = []
    value_keys = _ordered_keys_from_responses(responses, "values")
    label_keys = _ordered_keys_from_responses(responses, "labels")
    displayed_value_keys = _ordered_keys_from_responses(responses, "displayedValues")

    csv_meta_by_import = {
        column.import_id: column
        for column in display_csv.columns
        if column.import_id and not column.choice_id
    }
    csv_meta_by_header = {column.header: column for column in display_csv.columns}

    value_column_names: list[str] = []
    key_to_column: dict[str, str] = {}
    for key in value_keys:
        column_name = display_csv.import_to_header.get(key, key)
        value_column_names.append(column_name)
        key_to_column[key] = column_name

    if "_recordId" not in value_keys and "ResponseId" not in value_column_names:
        value_column_names.insert(0, "ResponseId")

    label_column_names = [
        f"{key_to_column.get(key) or display_csv.import_to_header.get(key, key)}__label"
        for key in label_keys
    ]
    displayed_value_column_names = [
        (
            f"{key_to_column.get(key) or display_csv.import_to_header.get(key, key)}"
            "__displayed_values"
        )
        for key in displayed_value_keys
    ]
    raw_columns = (
        ["qsync_survey_id", "qsync_exported_at_utc"]
        + value_column_names
        + label_column_names
        + displayed_value_column_names
        + ["qsync_displayed_fields"]
        + list(display_csv.display_order_headers)
    )
    columns, column_name_map, dedupe_warnings = _dedupe_column_names(raw_columns)
    warnings.extend(dedupe_warnings)

    rows: list[dict[str, Any]] = []
    missing_display_rows = 0
    for response in responses:
        values = (
            response.get("values")
            if isinstance(response.get("values"), Mapping)
            else {}
        )
        labels = (
            response.get("labels")
            if isinstance(response.get("labels"), Mapping)
            else {}
        )
        displayed_values = (
            response.get("displayedValues")
            if isinstance(response.get("displayedValues"), Mapping)
            else {}
        )
        response_id = str(values.get("_recordId") or response.get("responseId") or "")
        row: dict[str, Any] = {
            column_name_map["qsync_survey_id"]: survey_id,
            column_name_map["qsync_exported_at_utc"]: created_at_utc,
        }

        if "_recordId" not in value_keys and "ResponseId" in column_name_map:
            row[column_name_map["ResponseId"]] = response_id
        for key in value_keys:
            raw_column = key_to_column[key]
            row[column_name_map[raw_column]] = _cell_value(values.get(key, ""))
        for key, raw_column in zip(label_keys, label_column_names):
            row[column_name_map[raw_column]] = _cell_value(labels.get(key, ""))
        for key, raw_column in zip(displayed_value_keys, displayed_value_column_names):
            row[column_name_map[raw_column]] = _join_ordered_values(
                displayed_values.get(key)
            )
        row[column_name_map["qsync_displayed_fields"]] = _join_ordered_values(
            response.get("displayedFields") or []
        )

        display_row = display_csv.rows_by_response_id.get(response_id)
        if display_row is None:
            missing_display_rows += 1
            display_row = {}
        for header in display_csv.display_order_headers:
            row[column_name_map[header]] = display_row.get(header, "")
        rows.append(row)

    if missing_display_rows:
        warnings.append(
            f"{missing_display_rows} response(s) had no matching "
            "include-display-order CSV row."
        )

    codebook_rows: list[dict[str, str]] = []
    codebook_rows.append(
        _codebook_row(
            variable=column_name_map["qsync_survey_id"],
            source="qsync_metadata",
            questions=questions,
            notes="qsync-added survey identifier.",
        )
    )
    codebook_rows.append(
        _codebook_row(
            variable=column_name_map["qsync_exported_at_utc"],
            source="qsync_metadata",
            questions=questions,
            notes="qsync bundle creation timestamp in UTC.",
        )
    )

    for key in value_keys:
        raw_column = key_to_column[key]
        meta = csv_meta_by_import.get(key)
        codebook_rows.append(
            _codebook_row(
                variable=column_name_map[raw_column],
                source="value",
                import_id=key,
                questions=questions,
                csv_meta=meta,
                notes=(
                    f"Qualtrics CSV label: {meta.label}"
                    if meta and meta.label
                    else ""
                ),
            )
        )
    if "_recordId" not in value_keys and "ResponseId" in column_name_map:
        codebook_rows.append(
            _codebook_row(
                variable=column_name_map["ResponseId"],
                source="response_id",
                import_id="_recordId",
                questions=questions,
                notes="Top-level NDJSON responseId.",
            )
        )

    for key, raw_column in zip(label_keys, label_column_names):
        codebook_rows.append(
            _codebook_row(
                variable=column_name_map[raw_column],
                source="label",
                import_id=key,
                questions=questions,
                notes="Label value from NDJSON labels container.",
            )
        )
    for key, raw_column in zip(displayed_value_keys, displayed_value_column_names):
        codebook_rows.append(
            _codebook_row(
                variable=column_name_map[raw_column],
                source="displayed_values",
                import_id=key,
                questions=questions,
                notes="Pipe-delimited ordered values from NDJSON displayedValues.",
            )
        )
    codebook_rows.append(
        _codebook_row(
            variable=column_name_map["qsync_displayed_fields"],
            source="displayed_fields_order",
            questions=questions,
            notes="Pipe-delimited full displayedFields sequence from NDJSON.",
        )
    )
    for header in display_csv.display_order_headers:
        meta = csv_meta_by_header.get(header)
        codebook_rows.append(
            _codebook_row(
                variable=column_name_map[header],
                source="qualtrics_display_order",
                import_id=meta.import_id if meta else "",
                questions=questions,
                csv_meta=meta,
                notes="Official Qualtrics includeDisplayOrder column.",
            )
        )

    output_files: list[Path] = []
    csv_path = output_dir / "responses_enriched.csv"
    if "csv" in normalized_formats:
        _write_csv(csv_path, rows, columns)
        output_files.append(csv_path)

    codebook_path = output_dir / "codebook.csv"
    _write_codebook(codebook_path, codebook_rows)
    output_files.append(codebook_path)

    format_details: dict[str, Any] = {}
    if "sav" in normalized_formats:
        sav_path = output_dir / "responses_enriched.sav"
        format_details["sav"] = _write_sav(sav_path, rows, columns, codebook_rows)
        output_files.append(sav_path)
    if "rds" in normalized_formats:
        rds_path = output_dir / "responses_enriched.rds"
        format_details["rds"] = _write_rds(
            rds_path,
            rows,
            columns,
            codebook_rows,
            codebook_path=codebook_path,
            csv_path=csv_path if csv_path.exists() else None,
        )
        output_files.append(rds_path)
    if "parquet" in normalized_formats:
        parquet_path = output_dir / "responses_enriched.parquet"
        format_details["parquet"] = _write_parquet(
            parquet_path,
            rows,
            columns,
            codebook_path=codebook_path,
        )
        output_files.append(parquet_path)

    duplicate_tags = _duplicate_export_tags(questions)
    if duplicate_tags:
        warnings.append(
            "Duplicate survey-definition DataExportTag values observed: "
            + ", ".join(duplicate_tags)
        )

    raw_files = [
        ndjson_path,
        display_order_csv_path,
        survey_definition_path,
    ]
    if keep_json_path is not None:
        raw_files.append(keep_json_path)

    raw_manifest_path = raw_dir / "export-manifest.json"
    raw_manifest = {
        "schema": RAW_RESPONSE_MANIFEST_SCHEMA_VERSION,
        "created_at_utc": created_at_utc,
        "account": account,
        "survey_id": survey_id,
        "survey_name": survey_name,
        "qualtrics_exports": list(raw_exports or []),
        "files": [file_record(path, relative_to=output_dir) for path in raw_files],
    }
    raw_manifest_path.write_text(
        json.dumps(raw_manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    output_files.append(raw_manifest_path)

    manifest_path = output_dir / "manifest.json"
    manifest = {
        "schema": RESPONSE_BUNDLE_SCHEMA_VERSION,
        "created_at_utc": created_at_utc,
        "account": account,
        "survey_id": survey_id,
        "survey_name": survey_name,
        "command_args": dict(command_args or {}),
        "analysis_formats": list(normalized_formats),
        "row_count": len(rows),
        "column_count": len(columns),
        "list_delimiter": LIST_DELIMITER,
        "list_fallback": "JSON string when values contain delimiter or newlines.",
        "raw_files": [file_record(path, relative_to=output_dir) for path in raw_files],
        "output_files": [
            file_record(path, relative_to=output_dir) for path in output_files
        ],
        "format_details": format_details,
        "warnings": warnings,
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    output_files.append(manifest_path)

    return EnrichedResponseBundleResult(
        output_dir=output_dir,
        row_count=len(rows),
        column_count=len(columns),
        output_files=tuple(output_files),
        manifest_path=manifest_path,
        warnings=tuple(warnings),
    )


def _duplicate_export_tags(questions: Mapping[str, Any]) -> list[str]:
    seen: dict[str, int] = {}
    for question in questions.values():
        if not isinstance(question, Mapping):
            continue
        tag = str(question.get("DataExportTag") or "").strip()
        if not tag:
            continue
        seen[tag] = seen.get(tag, 0) + 1
    return sorted(tag for tag, count in seen.items() if count > 1)


def _write_sav(
    path: Path,
    rows: Sequence[Mapping[str, Any]],
    columns: Sequence[str],
    codebook_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    try:
        import pandas as pd  # type: ignore
        import pyreadstat  # type: ignore
    except ImportError as exc:
        raise ResponseDatasetError(
            "Writing SAV requires optional dependencies. Install qsync with "
            "`qsync[responses-sav]` or install `pandas` and `pyreadstat`."
        ) from exc

    name_map = _spss_name_map(columns)
    df = pd.DataFrame(rows, columns=list(columns)).rename(columns=name_map)
    codebook_by_variable = {row["variable"]: row for row in codebook_rows}
    column_labels = {
        name_map[column]: _spss_label_for_column(column, codebook_by_variable)
        for column in columns
    }
    value_labels = _spss_value_labels(name_map, codebook_by_variable)
    pyreadstat.write_sav(
        df,
        str(path),
        column_labels=column_labels,
        variable_value_labels=value_labels,
    )
    changed = {
        source: target for source, target in name_map.items() if source != target
    }
    return {
        "writer": "pyreadstat",
        "variable_name_map": changed,
    }


def _spss_name_map(columns: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    used: set[str] = set()
    for column in columns:
        base = re.sub(r"[^A-Za-z0-9_]", "_", column)
        base = re.sub(r"_+", "_", base).strip("_") or "v"
        if not re.match(r"^[A-Za-z]", base):
            base = f"v_{base}"
        if len(base) > 64:
            suffix = hashlib.sha1(column.encode("utf-8")).hexdigest()[:8]
            base = f"{base[:55]}_{suffix}"
        candidate = base
        counter = 2
        while candidate.lower() in used:
            suffix = f"_{counter}"
            candidate = f"{base[: 64 - len(suffix)]}{suffix}"
            counter += 1
        used.add(candidate.lower())
        out[column] = candidate
    return out


def _spss_label_for_column(
    column: str, codebook_by_variable: Mapping[str, Mapping[str, Any]]
) -> str:
    row = codebook_by_variable.get(column) or {}
    label = (
        row.get("question_text")
        or row.get("notes")
        or row.get("import_id")
        or column
    )
    return str(label)[:255]


def _coerce_label_value(value: str) -> Any:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if number.is_integer():
        return int(number)
    return number


def _spss_value_labels(
    name_map: Mapping[str, str],
    codebook_by_variable: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[Any, str]]:
    labels: dict[str, dict[Any, str]] = {}
    for source_name, target_name in name_map.items():
        row = codebook_by_variable.get(source_name) or {}
        raw = row.get("value_labels_json") or ""
        if not raw:
            continue
        try:
            parsed = json.loads(str(raw))
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, Mapping) or not parsed:
            continue
        labels[target_name] = {
            _coerce_label_value(str(value)): str(label)[:120]
            for value, label in parsed.items()
        }
    return labels


def _write_rds(
    path: Path,
    rows: Sequence[Mapping[str, Any]],
    columns: Sequence[str],
    codebook_rows: Sequence[Mapping[str, Any]],
    *,
    codebook_path: Path,
    csv_path: Path | None,
) -> dict[str, Any]:
    rscript = shutil.which("Rscript")
    if not rscript:
        raise ResponseDatasetError(
            "Writing RDS requires `Rscript` on PATH so qsync can preserve "
            "R-native labels and value-label attributes."
        )

    cleanup_csv = False
    if csv_path is None:
        temp_csv = path.with_suffix(".rds-input.csv")
        _write_csv(temp_csv, rows, columns)
        csv_path = temp_csv
        cleanup_csv = True

    script_path = path.with_suffix(".write_rds.R")
    script_path.write_text(
        _render_rds_script(
            csv_path=csv_path,
            output_path=path,
            codebook_path=codebook_path,
            codebook_rows=codebook_rows,
        ),
        encoding="utf-8",
    )
    try:
        subprocess.run(
            [rscript, str(script_path)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise ResponseDatasetError(
            "Rscript failed while writing RDS: " + (exc.stderr or exc.stdout or "")
        ) from exc
    finally:
        script_path.unlink(missing_ok=True)
        if cleanup_csv:
            csv_path.unlink(missing_ok=True)

    return {"writer": "Rscript", "codebook_attribute": "qsync_codebook_path"}


def _r_quote(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _render_rds_script(
    *,
    csv_path: Path,
    output_path: Path,
    codebook_path: Path,
    codebook_rows: Sequence[Mapping[str, Any]],
) -> str:
    lines = [
        (
            f"df <- read.csv({_r_quote(csv_path)}, check.names=FALSE, "
            "stringsAsFactors=FALSE, na.strings=character(0))"
        ),
        "df <- type.convert(df, as.is=TRUE, na.strings=character(0))",
    ]
    for row in codebook_rows:
        variable = str(row.get("variable") or "")
        label = str(
            row.get("question_text")
            or row.get("notes")
            or row.get("import_id")
            or variable
        )
        if variable and label:
            lines.append(
                f"attr(df[[{_r_quote(variable)}]], 'label') <- {_r_quote(label)}"
            )
        raw_labels = row.get("value_labels_json") or ""
        if not raw_labels:
            continue
        try:
            parsed = json.loads(str(raw_labels))
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, Mapping) or not parsed:
            continue
        entries = []
        for value, value_label in parsed.items():
            coerced = _coerce_label_value(str(value))
            if isinstance(coerced, (int, float)):
                entries.append(f"{_r_quote(value_label)}={coerced}")
        if entries:
            lines.append(
                f"attr(df[[{_r_quote(variable)}]], 'labels') <- c({', '.join(entries)})"
            )
    lines.extend(
        [
            f"attr(df, 'qsync_codebook_path') <- {_r_quote(codebook_path.name)}",
            f"saveRDS(df, {_r_quote(output_path)}, version=2)",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_parquet(
    path: Path,
    rows: Sequence[Mapping[str, Any]],
    columns: Sequence[str],
    *,
    codebook_path: Path,
) -> dict[str, Any]:
    try:
        import pandas as pd  # type: ignore
        import pyarrow as pa  # type: ignore
        import pyarrow.parquet as pq  # type: ignore
    except ImportError as exc:
        raise ResponseDatasetError(
            "Writing Parquet requires optional dependencies. Install qsync with "
            "`qsync[responses-parquet]` or install `pandas` and `pyarrow`."
        ) from exc

    df = pd.DataFrame(rows, columns=list(columns)).replace({"": None})
    table = pa.Table.from_pandas(df, preserve_index=False)
    metadata = dict(table.schema.metadata or {})
    metadata.update(
        {
            b"qsync_schema": RESPONSE_BUNDLE_SCHEMA_VERSION.encode("utf-8"),
            b"qsync_codebook_file": codebook_path.name.encode("utf-8"),
            b"qsync_list_delimiter": LIST_DELIMITER.encode("utf-8"),
        }
    )
    table = table.replace_schema_metadata(metadata)
    pq.write_table(table, path)
    return {
        "writer": "pyarrow",
        "schema_metadata": sorted(k.decode("utf-8") for k in metadata),
    }
