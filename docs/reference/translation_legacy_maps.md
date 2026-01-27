# Legacy Translations Workflow (Historical Archive)

**Status:** Archived (retired workflow)  
**Purpose:** Document how translations used to be managed via Qualtrics translation maps and the translations endpoint.

---

## Summary of the legacy model

Translations were historically managed using **language map JSON files** stored locally and synced with the **Qualtrics translations endpoint**:

- Local editing surface:  
  `contents/qualtrics_survey_translations/<SurveyID>/<LANG>.json`
- Remote API:  
  `GET /surveys/{surveyId}/translations/{lang}`  
  `PUT /surveys/{surveyId}/translations/{lang}`

These JSON files served as both:
1) the **operator editing surface**, and  
2) the **local cache** for drift detection.

---

## How translation maps were structured

Each `<LANG>.json` contained key → string mappings, e.g.:

- `QID10_QuestionText`
- `QID10_Choice1`
- `QID10_Answer1`
- `QID10_Label1`
- `SurveyTitle`
- `SurveyDescription`

Values were plain strings (HTML or Markdown‑rendered text was allowed, but not explicitly tracked via `*_IsHTML` flags).

---

## Legacy workflow steps (now retired)

1) **Pull maps from Qualtrics**
```
qsync translations pull --survey-id SV_xxx --languages FR,NL
```

2) **Edit local JSON map files** under `contents/qualtrics_survey_translations/`

3) **Optional: sync maps into workbook** (legacy helper)
```
qsync translations workbook pull --survey-id SV_xxx --languages FR,NL
```

4) **Push maps to Qualtrics**
```
qsync translations push --survey-id SV_xxx --languages FR,NL
```

---

## Why this was retired

- **Multiple sources of truth:** translation maps vs workbook vs cached survey definition.
- **Drift ambiguity:** local JSON files doubled as “cache” and “editing surface.”
- **Inconsistent parity:** map keys did not map cleanly to workbook structure (`*_IsHTML`, `Survey_Metadata`, etc.).
- **Poor alignment with other dimensions:** items/js used workbook + cached survey definition, while translations used a separate map surface.

---

## Current canonical model (for reference)

Translations now follow the same flow as items/js:

`Workbook → Pending (cache-backed) → Push via survey-definitions → Cache refresh`

Translation content is sourced from:
- `Questions.<QID>.Language.<LANG>.*`
- `SurveyOptions.MetaDataTranslations`

---

## Migration note

As of **2026‑01‑26**, legacy translation map workflows are formally retired and retained here **for historical reference only**. The commands and on‑disk maps should be considered deprecated and non‑canonical.

**Workspace cleanup (optional):** If you still have `contents/qualtrics_survey_translations/`, it is safe to delete once you have confirmed your workbook + cached survey definition are current.
