# Survey Master Mapping Schema

_Migrated from `appendices/survey_master_mapping_schema.md` (monorepo) so the standalone `qsync` repo can be self-contained._

This document describes the schema for the Survey Master mapping CSV. This mapping is the
source of truth for Survey Master columns, validation, and endpoint behavior.

Where the mapping lives:
- Workspace override: `surveys/qualtrics_api_key_mapping.csv`
- Fallback default: packaged `qsync/resources/qualtrics_api_key_mapping.csv`

If a workspace mapping is not present, `qsync` will use the packaged defaults. You can also
override explicitly via `--mapping-csv` or `QSYNC_MAPPING_CSV`.

## Overview

The mapping CSV controls:
- Which fields appear in `surveys/qualtrics_master.csv`.
- Which fields are writable vs read-only in Survey Master.
- Validation rules (data type, allowed values, format notes).
- Endpoint routing and write semantics for each field.

## Column Glossary

Required / commonly used columns:
- `id`: Stable row identifier for auditing and ordering.
- `domain`: Logical grouping / endpoint family (e.g., `survey_metadata`, `survey_options`, `survey_detail`).
- `object_path`: JSON path used to read/write the field (e.g., `result.SurveyName`).
- `field_name`: CSV column name used in Survey Master (e.g., `SurveyName`).
- `description`: Human-readable description.
- `survey_master`: Inclusion flag for Survey Master (`read`, `write`, `none` or blank).
- `order`: Optional integer ordering for master CSV column order.
- `readable` / `writable`: Legacy flags for other workflows (not the Survey Master gate).
- `data_type`: Validation type (`string`, `int`, `bool`, `datetime`, `url`, `object`).
- `allowed_values`: Semicolon-separated allowed values (e.g., `Active; Inactive`).
- `format_notes`: Format guidance (e.g., `ISO 8601 (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ)`), nullable notes.
- `write_semantics`: Write strategy (e.g., `patch-like`, `replace (full object)`).
- `read_endpoint` / `write_endpoint`: Qualtrics API endpoints used for reads/writes.
- `auth_notes`: Required permissions for API calls.
- `implementation_ref`: Source code reference or doc pointer.

## How Survey Master Uses the Mapping

- `survey_master=write`:
  - Column appears in `surveys/qualtrics_master.csv` and is editable.
  - Validation uses `data_type`, `allowed_values`, and `format_notes`.
- `survey_master=read`:
  - Column appears in the CSV as read-only (prefixed with `_` in outputs).
- `survey_master=none` or blank:
  - Column is excluded from the master CSV.

### Column Ordering

- If `order` is present, columns are sorted by that integer.
- Columns without `order` are appended deterministically.
- `order` values should be unique (recommended; not currently enforced by the CLI).

### Endpoint Routing

- `domain` determines the endpoint family:
  - `survey_metadata` -> `/survey-definitions/{surveyId}/metadata`
  - `survey_options`  -> `/survey-definitions/{surveyId}/options`
  - `survey_detail`   -> `/surveys/{surveyId}`
- `write_semantics` communicates how writes are applied (patch-like vs replace).

## Example Row

```csv
id,domain,object_path,field_name,description,survey_master,order,readable,writable,data_type,allowed_values,format_notes,write_semantics,read_endpoint,write_endpoint
9,survey_metadata,result.SurveyStartDate,SurveyStartDate,Start date (nullable),write,14,Y,Y,datetime,string,ISO 8601 (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ),patch-like,GET /survey-definitions/{surveyId}/metadata,PUT /survey-definitions/{surveyId}/metadata
```

## Duplicate Fields / Domain Precedence

Some Qualtrics fields exist on multiple endpoints (e.g., `SurveyName`, `SurveyStartDate`).
Survey Master should use a single authoritative endpoint. The mapping should mark
only one as `survey_master=write` and leave the duplicates as `none` to avoid ambiguity.

## Changing the Mapping (Safe Workflow)

1. Create a workspace override mapping (copy from the packaged default):
   - Put it at `surveys/qualtrics_api_key_mapping.csv`, or
   - Set `QSYNC_MAPPING_CSV=/path/to/your_mapping.csv`.
2. Edit the CSV.
3. Run a safe preview to catch obvious schema issues:
   ```bash
   qsync survey master preview
   ```
4. Re-pull master snapshots + regenerate the master CSV:
   ```bash
   qsync survey master pull
   ```
5. Update any user-facing docs if needed (field reference, workflows).

## Related Docs

- `survey-master-fields.md`
- `../workflows/survey-master.md`
- `cli.md`
