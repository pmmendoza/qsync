# Qualtrics Survey Inventory CSV (`surveys/inventory.csv`)

_Migrated from `appendices/qualtrics_surveys_schema.md` (monorepo) so the standalone `qsync` repo can be self-contained._

This document covers the schema and intended usage of `surveys/inventory.csv`.

Account scoping: inventory is account-scoped. In account-root layout it lives under `accounts/<account>/surveys/inventory.csv` (legacy compatibility: `surveys/.<account>/inventory.csv`). See `accounts.md`.

This file is the local “survey inventory” cache used by `qsync` for:
- selecting focal surveys,
- enforcing push safeguards (live response checks, locked surveys),
- and tag-based filtering for Survey Master operations (`--tag`).

Regenerate/update via:

```bash
qsync survey inventory
```

Backward compatibility: older workspaces may still use `surveys/qualtrics_surveys.csv`. `qsync` will read it, but new runs write `surveys/inventory.csv`.

## Columns

All columns are stored as CSV strings. Treat this file as a cache; most fields are populated by the Qualtrics API.

### Identity

- `id`: Qualtrics SurveyID (e.g., `SV_...`). Primary key.
- `name`: Survey name (human readable).
- `ownerId`: Qualtrics OwnerID (e.g., `UR_...`).

### Selection / batching

- `focal`: `TRUE`/`FALSE`. Determines “focal surveys” for batch workflows (e.g., Survey Master pull).
- `locked`: `TRUE`/`FALSE`. Safety override to block API writes. When `TRUE`, pushes should refuse to proceed.

### Status / safeguards

- `isActive`: `TRUE`/`FALSE`. Whether the survey is active for respondents (activation is distinct from publishing).
- `preview_count`: Preview response count (may be empty if unknown/unavailable).
- `response_count`: Finished response count (may be empty if unknown/unavailable).

### Timing / audit

- `creationDate`: Survey creation timestamp (ISO 8601, UTC `Z`).
- `lastModified`: Last modified timestamp (ISO 8601, UTC `Z`).
- `generated_at`: When this inventory row was last refreshed (ISO 8601, UTC `Z`).

### API capability

- `editableViaApi`: `TRUE`/`FALSE`. Whether the current credentials appear to have API edit permissions for the survey.

### Tags (for Survey Master `--tag`)

Survey Master tag filtering reads these columns (if present):

- `component`: e.g., `pre`, `post`, `payout` (project-defined).
- `stage`: e.g., `pilot`, `main` (project-defined).
- `cntry`: country code (project-defined), e.g., `US`, `IE`.

**How `--tag` works**

Survey Master supports repeatable filters:

```bash
qsync survey master preview --tag component=pre --tag stage=pilot
qsync survey master stage --tag cntry=US
qsync survey master push --tag cntry=US
```

Implementation notes:
- Tag keys are currently: `component`, `stage`, `cntry` (see `src/qsync/survey_tags.py`).
- Multiple `--tag key=value` filters are combined as an AND across keys (must match all provided criteria).

## Operational notes

- Prefer editing tags (`component`, `stage`, `cntry`) manually (when needed) rather than changing safeguards fields.
- Avoid manual edits to `preview_count`, `response_count`, `generated_at`, etc.; refresh via `qsync survey inventory`.
- When a survey is blocked for API editing, set `locked=TRUE` (with an internal note elsewhere) rather than relying on memory.
