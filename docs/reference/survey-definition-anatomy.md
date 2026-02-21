# Qualtrics Survey Definition Anatomy (qsync)

This page documents the structure of the JSON returned by:

- `GET /survey-definitions/{surveyId}`

qsync stores this payload in `surveys/...__SV_*.json` under `result` and maps different parts to different dimensions.

## Top-level shape

```json
{
  "result": {
    "SurveyID": "SV_...",
    "SurveyName": "...",
    "Questions": { "QID1": { ... } },
    "Blocks": { "BL_xxx": { ... } },
    "SurveyFlow": { "Type": "Root", "Flow": [ ... ] },
    "SurveyOptions": { ... },
    "ResponseSets": { ... },
    "Scoring": { ... },
    "ProjectInfo": { ... },
    "...": "..."
  }
}
```

## Structural segments and ownership

| Segment | What it contains | Ownership guidance in qsync |
|---|---|---|
| `SurveyID`, `SurveyName`, status/timestamps, owner/brand metadata | Identity and metadata envelope | Survey Master (metadata/status), plus system-managed fields |
| `Questions` | Question payloads, labels, validation, randomization, language blocks, `QuestionJS` | Items, Translations, JS dimensions |
| `Blocks` | Block definitions and `BlockElements` (question/page-break order inside each block) | Blocks dimension (`qsync blocks` workflow surface) |
| `SurveyFlow` | Flow graph: block references, branches, randomizers, embedded-data nodes, web services, EOS options in flow nodes | Flow dimension |
| `SurveyOptions` | Global survey options incl. `MetaDataTranslations`, header/footer, EOS redirect, etc. | Survey Master (options), plus translations metadata flow |
| `ResponseSets`, `Scoring`, `ProjectInfo`, `Notes` | Additional survey definition metadata | Mostly system/policy surfaces; touched by specific workflows only |

## Critical distinction: `Blocks` vs `SurveyFlow`

- **Question order within a block** lives in `Blocks[<BL_ID>].BlockElements`.
- **Block routing/order in the survey graph** lives in `SurveyFlow.Flow` (and nested flow nodes).

So if you need to move `QID64` to a different position *inside a block*, that is a **Blocks** mutation, not a SurveyFlow mutation.

## Translation placement

- Question-level translations: `Questions[QID].Language.<LANG>`
- Survey-level translation metadata: `SurveyOptions.MetaDataTranslations`

This split matters for ownership and staging:

- Non-base question copy belongs to the translations workflow.
- Survey-level metadata translations are in `SurveyOptions` and should be treated as options/metadata ownership.

## Current qsync dimension mapping (practical)

- Items: question base content + options/subitems + embedded defaults workbook surface
- Translations: non-base language columns + `Survey_Metadata` values
- JS: `QuestionJS`
- Blocks: block-internal `BlockElements` order (`blocks.yaml`)
- Flow: `SurveyFlow`
- Survey Master: metadata/options/status endpoints
- EOS: library messages referenced from flow/options

## Design rule for new features

When adding sync/edit capability, assign ownership by JSON segment first:

1. Identify the exact source segment (`Questions`, `Blocks`, `SurveyFlow`, `SurveyOptions`, ...).
2. Expose one primary editing surface for that segment.
3. Keep pull/read surfaces non-destructive and push explicit (`pull -> preview -> stage -> push`).
4. Avoid overlapping write ownership across dimensions unless explicitly documented.
