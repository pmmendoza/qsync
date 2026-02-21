# Workspace Path Ownership

This document defines filesystem ownership boundaries and account scoping behavior.

## Account-Scoped Surfaces

These resolve through scoped path helpers and are account data:

- `surveys` (cache files, inventory, pending state, flow/blocks/master artifacts)
- `excel` (workbooks)
- `survey_js` / `js` (JS core files + `survey_qid_js_map.csv`)
- `contents` (EOS/library message + translation/support artifacts)
- `export`
- `responses`
- `tmp`

Layout variants:
- account-root layout: `accounts/<account>/<surface>/...`
- legacy compatibility layout: `<root>/<surface>/...` (default account) and `<root>/<surface>/.<account>/...` (named accounts)

## Shared Workspace Paths

These are intentionally not account-scoped:

- `<root>/.env` and `<root>/.env.<account>`
- `<root>/.qsync/*` (preferences, migrations, locks)
- `<root>/logs/*`

## Compatibility Paths

These are backward-compatible read/write fallbacks and should not be primary targets for new writes:

- `<root>/surveys/qualtrics_api_key_mapping.csv`
- `<root>/appendices/qualtrics_api_key_mapping.csv`
- `<root>/surveys/edf_presets.json`

## Flow vs Blocks Ownership (Implemented)

Blocks surfaces are colocated with flow surfaces but remain a distinct dimension.

- Flow editing surface:
  - `.../flow/<survey-slug>-<survey-id>/flow.yaml`
- Blocks editing surface:
  - `.../flow/<survey-slug>-<survey-id>/blocks.yaml`
  - `.../flow/<survey-slug>-<survey-id>/blocks_baseline.json`
- Pending state:
  - `.../pending/flow/<survey-id>.json`
  - `.../pending/blocks/<survey-id>.json`

Ownership split:
- `flow` owns routing/traversal structure.
- `blocks` owns in-block `BlockElements` order (question/page-break structure).
