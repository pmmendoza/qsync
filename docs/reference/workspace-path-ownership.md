# Workspace Path Ownership

This document defines which filesystem surfaces are account-scoped versus shared.

## Account-scoped surfaces

These resolve via `resolve_scoped_dir(...)` and should be treated as account data.

- `surveys` (cache files, inventory, pending state, flow surfaces)
- `excel` (workbooks)
- `survey_js` / `js` (JS core files + `survey_qid_js_map.csv`)
- `contents` (translation and content exports)
- `export`
- `responses`
- `tmp`

In `account_root_v1`, these live under `accounts/<account>/...`.
In `legacy`, they live under `<root>/<surface>/` (or `<root>/<surface>/.<account>/`).

## Shared workspace paths

These are intentionally not account-scoped:

- `<root>/.env` and `<root>/.env.<account>`
- `<root>/.qsync/*` (preferences, migrations, locks)
- `<root>/logs/*`

## Compatibility paths

These are supported for backward compatibility, but should not be the primary target for new writes:

- `<root>/surveys/qualtrics_api_key_mapping.csv`
- `<root>/appendices/qualtrics_api_key_mapping.csv`
- `<root>/surveys/edf_presets.json`

Read order for compatibility files is implemented with fallback candidates.

## Blocks + Flow (planned ownership)

Blocks editing surfaces are colocated with flow surfaces but remain a separate dimension.

- Flow surface:
  - `.../flow/<survey-slug>-<survey-id>/flow.yaml`
- Blocks surface:
  - `.../flow/<survey-slug>-<survey-id>/blocks.yaml`
  - `.../flow/<survey-slug>-<survey-id>/blocks_baseline.json`
- Pending state:
  - `.../pending/flow/<survey-id>.json`
  - `.../pending/blocks/<survey-id>.json`

This keeps routing ownership (`flow`) separate from in-block element ordering (`blocks`).
