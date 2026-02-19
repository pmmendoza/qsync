# Items workflow (Excel wording)

_Migrated from `appendices/qsync_workflow.md` (monorepo) so the standalone `qsync` repo can be self-contained._

This document explains how to edit Qualtrics wording via Excel. It assumes you run commands from your workspace root with a virtualenv activated.

Account scoping: if you run with `--account <name>` or set a workspace default via `qsync account use <name>`, `qsync` reads/writes the workflow surfaces under `.<name>/` subdirectories (see `../reference/accounts.md`). The paths below assume the default account.
## 0. File locations reference

The table below shows the file locations for each qsync dimension:

| Dimension | Editing Surface | Staged Files | Cache Files |
|-----------|----------------|--------------|-------------|
| **Items** | `excel/<slug>-<SurveyID>.xlsx` | `surveys/pending/items/<SurveyID>.json` | `surveys/<label>__SV_<ID>.json` |
| **JS** | `survey_js/core/*.js` | `surveys/pending/js/<SurveyID>.json` | `surveys/<label>__SV_<ID>.json` |
| **Translations** | `excel/<slug>-<SurveyID>.xlsx` (language columns) | `surveys/pending/translations/<SurveyID>.json` | `surveys/<label>__SV_<ID>.json` |
| **EOS** | `contents/qualtrics_library_messages/<LibraryID>/<MessageID>` | `surveys/pending/eos/<SurveyID>.json` | `surveys/<label>__SV_<ID>.json` (SurveyFlow refs) |

- **Editing Surface**: Where you make changes locally
- **Staged Files**: Where pending changes are recorded after `qsync <dimension> stage`
- **Cache Files**: Read-only local copies of Qualtrics survey definitions, refreshed by `qsync <dimension> pull`

**Important**: `qsync init` regenerates Excel files from cached survey JSON but does **not** modify staged files or cache files. It preserves existing Excel content where questions/options still exist in the survey.
## 1. Key files & concepts

- **Inventory (`surveys/inventory.csv`)** – built via `qsync survey inventory`. Each row stores `id`, `name`, `focal`, `locked`, `preview_count`, `response_count`, etc. (Legacy filename: `surveys/qualtrics_surveys.csv`.)
- **Cached survey JSON (`surveys/<label>__SV_… .json`)** – refreshed whenever you run `qsync items pull`. This is the single source of truth for previews/pushes.
- **Per-survey workbook (`excel/<slug>-<SurveyID>.xlsx`)** – generated/updated by `qsync items pull`. Filenames follow `<slug>-<SurveyID>.xlsx` where slug is derived from: (1) 'name' column in inventory CSV, (2) SurveyTitle from cached survey, or (3) Survey ID as fallback. **Note:** Old-format files (`<SurveyID>-<slug>.xlsx`) are automatically detected and used for backward compatibility. New files are created with the new format. You can override with `--xlsx` explicitly.
- **Externally managed items** – questions/options owned by scripts (recognition, salience, cued recall). They stay read-only in Excel and are tagged through `MetaComment` + `DataExportTag` (see `EXTERNALLY_MANAGED_TAGS` in `src/qsync/excel_io.py`). By default, `qsync items stage/push` (and `qsync sync`) skip option/subitem edits for these questions unless you explicitly opt in (see “SBSMatrix notes” below for SBS-specific sheets).

## 2. Quick runbook (standalone)

| Step | Command | What it does |
| --- | --- | --- |
| Inventory | `qsync survey inventory` | Refreshes `surveys/inventory.csv` (locks + response counts). |
| Pull | `qsync items pull --survey-id SV_xxx` | Refreshes cached JSON and writes/updates the workbook. |
| Preview | `qsync items preview --survey-id SV_xxx` | Shows diffs between workbook and cache. |
| Stage | `qsync items stage --survey-id SV_xxx --yes` | Writes pending changes under `surveys/pending/` (no cache mutation). |
| Push | `qsync items push --survey-id SV_xxx --force-live --yes` | Pushes staged changes to Qualtrics and refreshes cache after push. |

Notes:
- `qsync items push` enforces overwrite safeguards based on `surveys/inventory.csv` (see `../reference/push-safeguards.md`).
- Use `--force-preview` (items) when only preview/test responses exist; use `--force-live` when finished responses exist.
- `locked=TRUE` in the inventory blocks all pushes; clear it (with justification) before rerunning.
- `--yes` skips interactive confirmations.

## 3. Direct qsync commands

You can run the CLI directly (or `python -m qsync.cli …`) when you need finer control:

```bash
qsync items pull --survey-id SV_5AsKyAO5QqswBcq
qsync items preview --survey-id SV_5AsKyAO5QqswBcq --filter-column InPre --filter-value TRUE
qsync items stage --survey-id SV_5AsKyAO5QqswBcq --yes
qsync items push --survey-id SV_5AsKyAO5QqswBcq --force-live --yes
```

- `items pull` downloads the Qualtrics definition, writes/updates the Excel workbook, and keeps the cached JSON in sync.
- `items preview` compares Excel vs cached JSON. Non-HTML cells are compared via Markdown; HTML-only cells compare normalized HTML directly.
- `items stage` writes pending change records (no Qualtrics API calls; no cache mutation) and records the staged QIDs.
- `items push` reads staged QIDs from disk, enforces safeguards, uploads the questions via the Qualtrics API, and refreshes the cache after push.
- Use `--filter-column/--filter-value` when you only want to preview/apply/push a subset of the workbook (e.g. `InPre == TRUE`).
- **Legacy commands:** `qsync init`, `qsync preview`, `qsync apply`, `qsync push` still work as aliases but are deprecated.

### Translation validation export (Word) (optional)

When you want a translator/reviewer-friendly document that mirrors SurveyFlow order (including conditional logic), export a `.docx`:

```bash
# Default output goes to export/<SurveyName>__<SurveyID>__<BASE>.docx (+ Mermaid artifacts)
qsync survey export-translation --survey-id SV_xxx

# Render using cached translations (participant view)
qsync survey export-translation --survey-id SV_xxx --language FR

# Batch export multiple languages (one .docx per language)
qsync survey export-translation --survey-id SV_xxx --languages FR,NL,CS

# Bilingual review mode (EN + target rendered together)
qsync survey export-translation --survey-id SV_xxx --language FR --compare-to-base

# Enable layout heuristics (reviewer-friendly transforms; default is UI-faithful)
qsync survey export-translation --survey-id SV_xxx --language FR --compare-to-base --layout-heuristics

# Scenario export: prune provably-irrelevant branches using explicit EDF values
qsync survey export-translation --survey-id SV_xxx --edf S_VERSION=PROLIFIC --edf DEBUG=F

# Disable Mermaid rendering (keeps .mmd, skips rendering/embed)
QSYNC_MERMAID_RENDER=0 qsync survey export-translation --survey-id SV_xxx

# Refresh cached survey definition from Qualtrics before exporting (network)
qsync survey export-translation --survey-id SV_xxx --language FR --refresh
```

Artifacts are written under `export/` by default:
- `.docx` translation export
- `.flow.mmd` Mermaid source
- `.flow.png` rendered Mermaid image (when enabled)

For a detailed “how to read” guide (question metadata format, logic highlighting, scenario semantics, WebService/EOS rendering, and limitations), see `../features/translation-export.md`.

## 4. Excel schema refresher

Each workbook ships with an `Instructions` sheet regenerated at every `qsync items pull` (or legacy `qsync init`). Highlights:

- **Questions sheet** – 1 row per question; edit `text_{base}` (e.g. `text_en` for English‑base surveys), toggle `ishtml_{base}`, and (when needed) edit response settings via `ForceResponseMode`, `ValidationType`, `ValidationSettingsJSON`, `RandomizationType`, `RandomizationSettingsJSON`. `RequiredResponse` is derived/read-only. `QuestionConfigJSON` is a read-only canonical mirror.
- **Options sheet** – 1 row per choice/scale point; edit `Label_{base}_MD` (Markdown) or mark `Label_{base}_IsHTML`. `MetaComment` conveys ownership (e.g. "Externally managed by recognition script").
- **Subitems sheet** – 1 row per matrix row/sub-statement; same Markdown/HTML toggles as Options.
- **SBS_Columns sheet** – for SBSMatrix (side-by-side) items only: 1 row per SBS column (the side-by-side “panels”); edit `Label_{base}_MD` (Markdown) or mark `Label_{base}_IsHTML`.
- **SBS_ColumnAnswers sheet** – for SBSMatrix items only: 1 row per SBS column answer/scale label; edit `Label_{base}_MD` (Markdown) or mark `Label_{base}_IsHTML`.
- **Embedded_Data sheet** – 1 row per embedded field; edit `Value` for defaults. Fields without defaults show `---` and require `qsync items stage --allow-dangerous` (or legacy `qsync apply --allow-dangerous`) to stage. `WrittenByQIDs` lists JS writers (map via `survey_js/survey_qid_js_map.csv`).
- **Embedded field renames** – use CLI staging for field-name changes:
  `qsync survey rename-embedded-field --survey-id SV_xxx --from OLD_FIELD --to NEW_FIELD`
- **System sheet** – read-only (timing, display logic metadata). Provided for context.

Across workbook tables, non-editable/system columns are shaded light gray so it is visually clear which cells are intended editing surfaces.

`qsync items preview` (and legacy `qsync preview`) only report differences when Markdown (for non-HTML cells) or normalized HTML actually changes, so formatting tweaks that don’t alter rendered output remain silent.

### SBSMatrix notes (Qualtrics `QuestionType="SBS"`, `Selector="SBSMatrix"`)

Qualtrics SBSMatrix questions (the “side-by-side” layout, e.g. the news memory recognition battery) are represented in JSON differently from typical MC/Matrix questions:

- **Statements (rows)** live under `Questions[QID].Choices[*].Display`, but are edited in Excel via the **Subitems** sheet (not the Options sheet).
- **SBS columns (headers/panels)** live under `Questions[QID].AdditionalQuestions[*].QuestionText`, edited via **SBS_Columns**.
- **Per-column answer scales** live under `Questions[QID].AdditionalQuestions[*].Answers[*].Display`, edited via **SBS_ColumnAnswers**.

Important behaviors:

- **Options sheet is not used** for SBSMatrix questions. Older workbooks that incorrectly stored SBS statements in `Options` are automatically migrated to `Subitems` on workbook refresh.
- SBSMatrix statements are duplicated under each `AdditionalQuestions[*].Choices` block in Qualtrics. When `qsync` pushes Subitems edits for an SBSMatrix question, it mirrors the updated statements into every `AdditionalQuestions[*].Choices` block to keep the survey consistent.
- If the question’s `DataExportTag` is externally managed (e.g. `newsmem_recognition`), option/subitem/SBS edits are skipped by default. To override this for specific QIDs, use:
  - Env var `QSYNC_ITEMS_ALLOW_EXTERNALLY_MANAGED_QIDS=QID15` (or scoped tokens like `SV_xxx:QID15`), or
  - Use `all` (or `*`) to allow all externally managed QIDs for the current command context, or
  - CLI flag `--allow-externally-managed-qids QID15` on `qsync items preview|stage|push` and `qsync sync` (flag takes precedence over the env var).

## 5. Push safeguards (summary)

Before applying/pushing, `qsync` loads the target row from `surveys/inventory.csv` and enforces:

1. **Lock check** – if `locked == TRUE`, abort immediately.
2. **Live responses** – if `response_count > 0`, require `--force-live`. A warning summarises live/preview counts and prompts for confirmation (unless `--yes`).
3. **Preview-only responses** – if `response_count == 0` but `preview_count > 0`, wording pushes require `--force-preview` (or `--force-live`). JS pushes merely warn but still ask for confirmation unless `--yes`.
4. **Stale inventory** – if `generated_at` is older than ~30 min or counts are missing, `qsync` performs a lightweight live check via `GET /surveys/{id}` before trusting zeros.

See `../reference/push-safeguards.md` for the full decision matrix and CLI flags.

## 6. Survey management pointers

- **Survey Master (bulk metadata/options/status edits):** See `survey-master.md` and `../reference/survey-master-fields.md`.
- **Publish/version/rollback:** See `../reference/cli.md` for the full `qsync survey ...` command surface.

## 7. Tips & troubleshooting

- Always run `qsync survey inventory` (and `qsync items pull`) before editing. This avoids working on stale caches and ensures push safeguards see fresh counts.
- Use `qsync sync --survey-id SV_xxx` to get a holistic view across items/EDF/JS/translations/EOS in one go.
- If Qualtrics introduces a new QID (e.g. question created in the UI), re-run `qsync items pull` so the workbook includes the new row before editing.
- When collaborating, commit both the Excel workbook and the corresponding pending/cached artifacts. Reviewers can re-run `qsync items preview` (and/or `qsync sync`) to validate there are no hidden diffs before approving.
- If `qsync preview` reports duplicate placeholder embedded fields (e.g. “Create New Field or Choose From Dropdown...”), run `qsync survey cleanup-embedded-data --survey-id SV_xxx --apply --publish` to remove the duplicates from SurveyFlow.
