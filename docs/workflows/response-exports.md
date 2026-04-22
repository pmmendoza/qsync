# Response Exports

Use `survey export-responses` for both raw Qualtrics response exports and qsync enriched analysis bundles.

## Raw Exports

```bash
qsync survey export-responses --survey-id SV_xxx --format csv
qsync survey export-responses --survey-id SV_xxx --format spss
qsync survey export-responses --survey-id SV_xxx --format json
qsync survey export-responses --survey-id SV_xxx --format ndjson
```

Supported raw formats are `csv`, `tsv`, `spss`, `json`, `ndjson`, and `xml`.

For non-JSON/NDJSON exports, qsync keeps its historical Qualtrics options:

- `useLabels=true`
- `seenUnansweredRecode=999`
- `timeZone=UTC`

Display-order columns can be requested for supported tabular/SPSS exports:

```bash
qsync survey export-responses --survey-id SV_xxx --format csv --include-display-order
qsync survey export-responses --survey-id SV_xxx --format spss --include-display-order
```

Qualtrics rejects `includeDisplayOrder` for `json` and `ndjson`; qsync blocks that combination before starting the export.

## Enriched Bundle

```bash
qsync survey export-responses \
  --survey-id SV_xxx \
  --analysis-bundle \
  --analysis-formats csv,sav,rds,parquet
```

Bundle shape:

```text
responses/<survey-name>__<survey-id>__responses_bundle/
  responses_enriched.csv
  responses_enriched.sav
  responses_enriched.rds
  responses_enriched.parquet
  codebook.csv
  manifest.json
  raw/
    responses.ndjson
    qualtrics-display-order.csv
    survey-definition.json
    export-manifest.json
```

Add `--keep-json` to also preserve `raw/responses.json`.

## What Is Preserved

- `responses.ndjson` is the canonical raw response source.
- `qualtrics-display-order.csv` is requested with `includeDisplayOrder=true` and supplies official `*_DO_*` display-order columns.
- `responses_enriched.*` is one row per response.
- Raw answer values become ordinary wide columns.
- NDJSON `labels` become `<variable>__label` columns.
- NDJSON `displayedValues` become `<variable>__displayed_values` columns.
- NDJSON `displayedFields` becomes `qsync_displayed_fields`, a pipe-delimited ordered character column.
- `codebook.csv` maps enriched variables back to QIDs/import IDs/export tags where possible.
- `manifest.json` records file hashes, export options, row/column counts, warnings, and format-specific compromises.

## Format Notes

- CSV is always available and uses sidecar metadata in `codebook.csv` and `manifest.json`.
- SAV requires the optional `responses-sav` dependencies and preserves variable labels/value labels where SPSS supports them.
- RDS requires `Rscript` on `PATH`; qsync writes a plain R data frame with label/value-label attributes.
- Parquet requires the optional `responses-parquet` dependencies and includes qsync schema metadata.

Install optional Python writer dependencies when needed:

```bash
uv sync --extra responses
```

## Safety

Response exports are read-only against Qualtrics. Normal command output prints paths, counts, and warning counts only; respondent values are not printed.
