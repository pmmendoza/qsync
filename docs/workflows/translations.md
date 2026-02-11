# qsync translations workflow

_Migrated from `appendices/qsync_translations_workflow.md` (monorepo) so the standalone `qsync` repo can be self-contained._

This document describes the canonical workflow for Qualtrics survey translations managed by `qsync`.

For **split/sliced survey families** (one SurveyID per language/country), see:
`translation-consistency.md` (explains how translations become base-language “items” after slicing).

## Migration notes

- **2026-01-23:** Stage 1 introduces the workbook → cached survey definition → question push flow for
  `qsync translations preview/apply/push`. `qsync translations ...` is now the canonical namespace.
  The workbook `Subitems.Field` column disambiguates `Answer` vs `Label` rows (slider/scale endpoints).
- **2026-01-23:** Stage 2 switches translation export/pack to read from the cached survey definition
  (Language blocks + `MetaDataTranslations`) and adds a cache freshness preflight. `--refresh` now
  refreshes the cached survey definition, and `qsync export survey` is available as an alias.
- **2026-01-24:** Decommissioned translation map editing surfaces and the legacy
  `qsync survey translations ...` entry point. Use `qsync translations ...` and the workbook-based flow.
- **2026-01-26:** `qsync translations stage` writes pending changes only (no cache mutation) and
  `qsync translations push` can keep staged changes when Excel differs (use `--use-pending`).
- **2026-01-26:** Translation map files and related CLI surfaces were removed; see
  `docs/translation_legacy_maps.md` for historical details.

## Key constraints (tenant-verified)

- **Translations live in the survey definition.**  
  `qsync translations` reads/writes `Questions.*.Language.<LANG>` and `SurveyOptions.MetaDataTranslations`.
- **Base language edits are handled via the items workflow.**  
  The translations workflow targets non-base languages; use `qsync items ...` for base-language copy.
- **Not all survey copy is covered.**  
  JS-injected strings and End-of-Survey (EOS) library messages are managed by separate
  qsync workflows (`qsync js`, `qsync eos`).

## Files on disk

- Workbook (canonical editing surface):  
  `excel/<SurveyName>-<SurveyID>.xlsx` (translation columns + `Survey_Metadata`)
- Cached survey definition:  
  `surveys/*__<SurveyID>.json`
- Pending translations (staged list):  
  `surveys/pending/translations/<SurveyID>.json`
- Legacy (archived reference):  
  See `docs/translation_legacy_maps.md` (translation map files removed from workflow).

## Core commands

Enable languages:

```
qsync translations languages ensure --survey-id SV_xxx --languages FR,NL
```

Create/update workbook translation columns:

```
qsync items pull --survey-id SV_xxx --languages FR,NL
```

Refresh cached survey definition (alias for `qsync survey pull`):

```
qsync translations pull --survey-id SV_xxx
```

Preview diffs:

```
qsync translations preview --survey-id SV_xxx --languages FR,NL
```

Stage changes (creates pending list; no cache mutation):

```
qsync translations stage --survey-id SV_xxx --languages FR,NL
```

Validate with doctor (no API writes):

```
qsync translations doctor --survey-id SV_xxx --languages FR,NL
```

Push to Qualtrics:

```
qsync translations push --survey-id SV_xxx --languages FR,NL --yes
```

Translation pack export (docx + cached translations):

```
qsync translations pack --survey-id SV_xxx --languages FR,NL
```

Drift check (cached survey vs live API):

```
qsync translations drift --survey-id SV_xxx --languages FR,NL
```

## Workflow notes

- The workbook is the canonical editing surface for non-base translations.
- `Survey_Metadata` covers `SurveyOptions.MetaDataTranslations` keys (e.g., `SurveyTitle`, `SurveyDescription`).
- Use `--allow-drift` sparingly when you intentionally want to preview/push against a drifted cache.
- If Excel differs from cache and a pending record exists, push prompts to restage (use `--use-pending` to skip).

## Doctor checks

The translation doctor validates:

- placeholder preservation (`${e://Field/...}` tokens)
- HTML hazards (e.g., `<script>` or event handlers)
- coverage completeness (warns if values are empty)

Workbook validation with an explicit path:

```
qsync translations doctor --survey-id SV_xxx --languages FR,NL --workbook excel/...xlsx
```

Large-delta warnings (length/line-count changes) are reported as warnings in doctor output.

## Language detection check

Use `check-language` to flag likely mistranslations with a binary language hypothesis test:

```
qsync translations check-language --survey-id SV_xxx --languages FR,NL,CS
```

Defaults and behavior:
- Uses the cached survey definition (no API writes).
- Prompts for survey selection if `--survey-id` is omitted (interactive).
- Strips HTML tags and unescapes `\uXXXX`/JS escapes before detection and display.
- Treats detection as a binary hypothesis test ("is this in the target language?"), with
  configurable confidence and margin thresholds.
- Skips empty strings, placeholders, and (optionally) meta/system items.
- Flags strings identical to base language as **Untranslated** (excluding numeric/low-signal strings).

Useful flags:

```
qsync translations check-language --survey-id SV_xxx --languages FR,NL \
  --min-confidence 0.85 --min-margin 0.15 --skip-meta --skip-js

# EDF-scoped checks: only questions reachable under the scenario
qsync translations check-language --survey-id SV_xxx --edf DEBUG=F
```

Notes:
- Short strings are hard to detect reliably; use `--disallow-single-word` only when needed.
- The output includes an "uncertain" count for close calls (not shown in the issues table).
- Results are grouped and ordered by SurveyFlow.

Troubleshooting:
- If short Likert/choice labels show up frequently, lower `--min-confidence` or allow single words.
- For EDF-scoped runs, make sure the EDF keys match SurveyFlow BranchLogic spelling exactly.
- If a string is legitimate English across languages (e.g., brand names), consider adding it to the neutral brand list.

## Smoke-testing guidance

Avoid testing on active production surveys. Use surveys with "smoke" in the name:

- `NEWSFLOWS_pre_main_debug_smoketest_20260110__SV_5zrBxvTWvWBMIzs`
- `ZZZ_qsync_smoketest_20251213_160439_edited_api__SV_5BeVXRVDCgJCsPI`

Default to `qsync translations push --validate` during testing; only use `push --yes` on smoke surveys.
