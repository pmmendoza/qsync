# Survey Master Preview JSON Output

_Migrated from `appendices/survey_master_json_output.md` (monorepo) so the standalone `qsync` repo can be self-contained._

This document describes the JSON schema emitted by:

```bash
qsync survey master preview --format json
```

This output is intended for automation and reporting. It is produced by `preview_master(...)` in `src/qsync/survey_master.py` and printed directly by the CLI.

## Top-level schema

The command prints a single JSON object:

- `validation_errors`: `string[]`
  - Empty array when validation passes.
  - Non-empty when validation fails (and `summary` is `null`).
- `survey_diffs`: `SurveyDiff[]`
  - Empty array when validation fails.
- `summary`: `PreviewSummary | null`

### `PreviewSummary`

- `total_surveys`: `number` (count of surveys processed excluding errors)
- `surveys_with_changes`: `number`
- `total_changes`: `number` (sum of all field changes across all surveys)
- `requires_publish`: `boolean` (any metadata/options change present)
- `has_dangerous`: `boolean` (any dangerous field change present)

## `SurveyDiff`

Each entry in `survey_diffs` is:

- `survey_id`: `string`
- `survey_name`: `string`
- `changes`: `FieldChange[]`
- `publish_required`: `boolean`
- `has_dangerous_changes`: `boolean`
- `error`: `string | null`

If a survey cannot be processed, `error` is a string and `changes` is typically empty.

## `FieldChange`

Each entry in `changes` is:

- `field_name`: `string`
- `old_value`: `string`
- `new_value`: `string`
- `endpoint`: `string`
  - One of `metadata`, `options`, `status` (or `unknown` if derivation fails).
- `is_dangerous`: `boolean`

Notes:
- Values are normalized to strings for comparison/output.
- Empty string typically represents “null/empty” in the CSV.

## Behavior on validation failure

If the master CSV fails schema/value validation, the output is:

- `validation_errors`: non-empty
- `survey_diffs`: `[]`
- `summary`: `null`

The CLI exits with status code 1 in this case.
