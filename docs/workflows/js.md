# JS workflow (Question JavaScript)

_Migrated from `appendices/js_sync_workflow.md` (monorepo) so the standalone `qsync` repo can be self-contained._

This document explains how we keep the QuestionJS embedded in Qualtrics aligned with the ground-truth files under `survey_js/core/`. All commands assume a virtualenv is activated.

## 1. Mapping CSV recap

- File: `survey_js/survey_qid_js_map.csv`.
- Columns: `js_file`, then one column per survey using the pattern `SV_<ID>-<label>` (e.g. `SV_5AsKyAO5QqswBcq-NEWSFLOWS_pre_pilot_api`).
- Rows: every JS file in `survey_js/core/` plus “hint rows” for inline JS without a matching file. Hint rows have `js_file` in quotes (`"Qualtrics.SurveyEngi"`) and are ignored by preview/stage/push tooling.
- Regeneration: `qsync js pull` rebuilds the CSV from the cached survey JSONs (internally calls `src/qsync/js_mapping.py`).

## 2. Quick runbook (standalone)

In a standalone workspace/repo, use the CLI directly:

| Step | Command | Description |
| --- | --- | --- |
| Pull (cache) | `qsync survey pull --survey-id SV_xxx` | Refreshes the cached survey definition (recommended before diffing/pushing). |
| Pull (JS) | `qsync js pull --survey-id SV_xxx` | Rebuilds the mapping CSV and verifies the survey column exists. |
| Preview | `qsync js preview --survey-id SV_xxx` | Prints a summary table and (optionally) unified diffs. |
| Stage | `qsync js stage --survey-id SV_xxx` | Writes pending JS entries only (no cache mutation). |
| Push | `qsync js push --survey-id SV_xxx --force-live --yes` | Pushes QuestionJS and refreshes the cache after push. |

Monorepo note: in the original monorepo, these steps were wrapped by Make targets (`make pull.js`, `make preview DIMENSION=js`, …). Those targets are not part of the standalone package.

## 3. `qsync js` commands

Direct usage:

```bash
qsync js pull    --survey-id SV_5AsKyAO5QqswBcq
qsync js preview --survey-id SV_5AsKyAO5QqswBcq --detailed --show-equal
qsync js stage   --survey-id SV_5AsKyAO5QqswBcq --create-missing
qsync js push    --survey-id SV_5AsKyAO5QqswBcq --force-live --yes
```

- `js pull` rebuilds the mapping and verifies that the requested survey column exists.
- `js preview` classifies every `(js_file, QID)` pair as `match`, `comments-only`, `diff`, `missing`, `trash`, or `unused`. The summary table now includes a `Δ(+/-)` column counting the diff lines and reports how many active JS blocks are unmatched.
- `js stage` writes pending JS entries only (no cache mutation). Use `--allow-diff` to include substantive code diffs, and `--no-include-match` to skip identical blocks when you only care about missing QuestionJS entries.
- `js push` enforces the same safeguards as wording pushes: locked surveys are blocked, live responses require `--force-live`, preview responses trigger warnings/confirmations, and stale inventory triggers a quick live re-check.

## 4. Handling hint rows & unmapped QIDs

- When the JS mapping rebuild encounters inline JS that doesn't match any core file, it writes a row whose `js_file` contains the first 20 characters of the inline code in quotes. These rows let you audit orphaned QuestionJS blocks and decide whether to port them into `survey_js/core/`.
- Preview/stage/push commands ignore hint rows automatically, so they won’t block the pipeline. Use the CSV itself (or the `--show-equal` preview output) to locate and reconcile them manually.
- If the preview summary reports “block(s) currently use inline JS with no matching file,” add or rename the corresponding file under `survey_js/core/` and re-run `qsync js pull` so the mapping picks it up.

## 5. Trash blocks and unplaced questions

`qsync js preview` categorises each QID based on the survey structure:

- `active` – appears in a non-Trash block (included in push calculations).
- `trash` – appears in the Trash block (reported but skipped unless you pass `--include-trash`).
- `unplaced` – defined under `Questions` but not in any block (reported as `unused`).

The tool warns when a mapped QID ends up in Trash/unplaced regions so you can clean up the mapping or move the question into a live block.

## 6. Push safeguards (summary)

- Locked surveys: `ensure_unlocked` stops the run immediately.
- Live responses: `response_count > 0` requires `--force-live`. A warning summarises preview/live counts, and (unless `--yes`) you must confirm before the push proceeds.
- Preview-only responses: JS pushes emit a warning and prompt for confirmation but do **not** require an extra flag. If you also run wording pushes, follow the stricter rules described in `../reference/push-safeguards.md`.
- Stale inventory: `qsync` performs a lightweight `GET /surveys/{id}` refresh when the recorded counts look stale/missing before deciding whether to demand `--force-live`.

## 7. Survey management pointers

- **Survey Master (bulk metadata/options/status edits):** See `survey-master.md` and `../reference/survey-master-fields.md`.
- **Publish/version/rollback:** See `../reference/cli.md` for the full `qsync survey ...` command surface.

## 8. Roundtrip checklist

1. `qsync survey pull --survey-id SV_…`
2. `qsync js pull --survey-id SV_…`
3. `qsync js preview --survey-id SV_…`
4. Review the summary table (`QID`, `JS file`, change type, diff counts). Inspect individual diffs with `--detailed` if needed.
5. `qsync js stage --survey-id SV_…` (writes pending JS; no cache mutation)
6. `qsync js push --survey-id SV_… --force-live --yes`
7. `qsync js preview --survey-id SV_…` again to confirm everything now reads `match`.

Keep `survey_js/core/` committed so Git tracks JS changes alongside the updated survey JSON/mapping rows.
