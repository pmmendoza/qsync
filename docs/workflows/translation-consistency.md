# Translation Consistency: Single Survey vs Split Surveys

This guide explains practical workflows for keeping **copy** (wording), **logic**, and **translations** consistent across languages in Qualtrics when using `qsync`.

Account scoping: if you run with `--account <name>` or set a workspace default via `qsync account use <name>`, `qsync` reads/writes most workflow surfaces under `.<name>/` subdirectories (see `../reference/accounts.md`). The paths below assume the default account.

It covers two common operating modes:

1. **Single survey, multiple languages** (one Qualtrics SurveyID, translations enabled inside the same survey).
2. **Split surveys (one per language/country)** after slicing the master survey into multiple SurveyIDs (common for Prolific).

This document is intentionally workflow-focused; for dimension-specific details, see:

- Items (base-language wording): `items.md`
- Translations (non-base languages in the same survey): `translations.md`
- JavaScript (QuestionJS): `js.md`
- EOS (library messages): `eos.md`
- Survey Master (Header/Footer, options, status): `survey-master.md`

## Core principle: separate "surfaces" must be managed separately

In practice, participant-visible text lives in multiple places:

| Surface | What it covers | Editing surface | Primary commands |
|---|---|---|---|
| Items | Base-language wording (QuestionText, choices, labels, subitems) | `excel/<slug>-<SurveyID>.xlsx` | `qsync items pull/preview/stage/push` |
| Translations | Non-base languages in the same survey (Language blocks + metadata translations) | same workbook (language columns) | `qsync translations pull/preview/stage/push` |
| JS | QuestionJS blocks (often includes participant-visible strings) | `survey_js/core/*.js` | `qsync js pull/preview/stage/push` |
| EOS | End-of-survey library messages | `contents/qualtrics_library_messages/...` | `qsync eos pull/preview/stage/push` |
| Header/Footer (Look & Feel) | SurveyOptions Header/Footer HTML (good for Prolific authenticity checks) | `surveys/qualtrics_master.csv` or a snippet file | `qsync survey prolific-auth ...` or `qsync survey master ...` |
| Flow text | Some SurveyFlow nodes may contain participant-visible text | `surveys/flow/<SurveyID>/flow.yaml` (when used) | `qsync flow pull/preview/stage/push` |

To keep translations consistent, you need a **policy** for which surfaces are canonical and a **runbook** that updates the relevant surfaces together.

## Mode A: Single survey with translations enabled (one SurveyID)

### When to use this mode

Use this when:

- You want one Qualtrics SurveyID with a language selector.
- You want translations to be managed as Qualtrics "translations" (Language blocks + metadata translations).
- You do not need separate SurveyIDs for recruiting/operational reasons.

### Recommended policy (canonical surfaces)

- **Base language (e.g., EN):** edit via **Items** workflow (workbook base-language columns).
- **Non-base languages (e.g., FR/NL/CS):** edit via **Translations** workflow (workbook translation columns).
- **Header/Footer:** edit via Survey Master (bulk) or `qsync survey prolific-auth` (Prolific snippet).
- **JS and EOS:** treat as separate translation surfaces; use `qsync js` and `qsync eos`.

Avoid editing text directly in the Qualtrics UI unless you immediately re-pull (`qsync items pull` / `qsync translations pull`) so your local workspace stays canonical.

### Base-language change runbook (EN example)

1. Refresh local state:
   - `qsync survey inventory`
   - `qsync items pull --survey-id SV_xxx --languages FR,NL,CS` (hydrates workbook + language columns)
2. Edit base-language copy in the workbook (`Text_en_MD`, `Label_en_MD`, etc.).
3. Preview and stage base-language changes:
   - `qsync items preview --survey-id SV_xxx`
   - `qsync items stage --survey-id SV_xxx --yes`
4. Update translation columns for impacted keys (FR/NL/CS) in the same workbook.
5. Run translation checks:
   - `qsync translations doctor --survey-id SV_xxx --languages FR,NL,CS`
   - (Optional) `qsync translations check-language --survey-id SV_xxx --languages FR,NL,CS`
6. Stage and push translations:
   - `qsync translations stage --survey-id SV_xxx --languages FR,NL,CS`
   - `qsync translations push --survey-id SV_xxx --languages FR,NL,CS --yes`
7. Push items (base-language) and any other dimensions:
   - If you staged multiple dimensions, run `qsync sync --survey-id SV_xxx` to orchestrate safe order.

Notes:
- `qsync translations` is for **non-base** languages only. If you "change English", that's items.
- Use `qsync survey export-translation --survey-id SV_xxx --language FR --compare-to-base` for reviewer-friendly bilingual QA (logic-aware ordering).
- If you use `--edf` scenarios for exports/checks, keep EDF keys aligned with your SurveyFlow BranchLogic.

### What checks are available in this mode

- `qsync translations doctor`: placeholder preservation, HTML hazards, empty coverage warnings.
- `qsync translations check-language`: flags likely wrong-language text / untranslated strings (uses cached definition).
- `qsync translations drift`: checks cached vs live translation drift.
- `qsync survey export-translation`: generates DOCX/PDF review documents (optionally bilingual).
- `qsync sync`: orchestrates items/js/translations/eos (and reports pending changes and fixable issues).

## Mode B: Split surveys after slicing (multiple SurveyIDs)

This is the "Prolific" shape: you build one master survey with translations, then create one derived survey per language/country.

### When to use this mode

Use this when:

- You need separate Qualtrics SurveyIDs (e.g., separate Prolific studies).
- You want each derived survey to be **monolingual** in-field (base language equals the target language).
- You want strong guarantees that the derived surveys remain structurally comparable.

### Key distinction: after slicing, "translations" often become "items"

After `qsync survey slice-language ...` produces a target-language survey:

- The derived survey's **base language** is the target language (e.g., FR).
- The participant-visible text in that survey is now **base-language wording**, so updates are done via:
  - `qsync items ...` (not `qsync translations ...`) for that derived survey.

This is the most common source of confusion when maintaining split survey families.

### Creating the split surveys (recommended runbook)

Assume you have a master multilingual survey `SV_MASTER` (base EN) with FR/NL/CS enabled and translated.

1. Validate translation completeness on the master survey:
   - `qsync translations doctor --survey-id SV_MASTER --languages FR,NL,CS`
2. Slice one language (strict by default; aborts if required keys are missing):
   - `qsync survey slice-language SV_MASTER --language FR --name "<MasterName> (FR)" --keep-languages target-only --verify-parity --yes`
   - Batch mode: `qsync survey slice-language SV_MASTER --languages FR,NL,CS --name "<MasterName>" --keep-languages target-only --verify-parity --yes`
3. Publish + activate the new survey (slice-language creates it inactive and un-published):
   - `qsync survey publish <SV_FR> --description "slice-language SV_MASTER -> FR"`
   - `qsync survey activate <SV_FR>`
4. Hydrate local editing surfaces for the derived survey:
   - `qsync items pull --survey-id <SV_FR>`

Operational traceability:
- Slice coverage reports and manifests are written under `surveys/slices/`.
- Use `qsync survey slice-registry --source SV_MASTER` to list derived surveys from local manifests.

Day-to-day dashboard:
- `qsync sync` scans focal surveys and reports pending changes and fixable issues across items/js/translations/eos.
- If a derived survey is missing a workbook, `qsync sync` will warn and suggest `qsync items pull --survey-id <SV_...>` to hydrate the workbook.

### Authenticity checks (Look & Feel header HTML)

There are two good options in `qsync`, depending on whether you want a specialized workflow or bulk editing.

Option 1: Prolific authenticity snippet helper (recommended for correctness)

- `qsync survey prolific-auth --survey-id SV_xxx --file path/to/snippet.html --yes`
- This updates `SurveyOptions.Header`. By default it also publishes and activates (use `--no-publish` / `--no-activate` to avoid that).

Option 2: Survey Master (recommended for bulk operations across many surveys)

- `qsync survey master pull`
- Edit `Header` in `surveys/qualtrics_master.csv` (and/or other operational fields you need).
- `qsync survey master preview --detail`
- `qsync survey master apply`
- `qsync survey master push`

### Keeping split surveys consistent over time

There are two governance models. Pick one explicitly; mixing them creates "phantom drift" and expensive debugging.

Model B1 (Master-first, derived surveys are mostly read-only)

- Treat the multilingual master survey as the canonical content source.
- Make edits to master (items/translations/js/eos).
- Re-slice when you need to deploy changes to per-language surveys.

This is simplest when:
- You can tolerate new SurveyIDs per "wave", or
- You are early in development and not yet fielding.

Model B2 (Derived surveys become canonical after slicing)

- Treat each derived survey as canonical for its language.
- Apply changes to each derived survey directly using `qsync items/js/eos/flow/survey master`.
- Use the master survey mainly as a template and translation workspace, not as an always-current source of truth.

This is more practical when:
- SurveyIDs must remain stable for operational reasons, and
- You expect small edits during fielding (though this is generally discouraged).

### Recommended update strategy in split mode

Classify changes before acting:

- Structural changes (new questions, flow edits, DataExportTag changes):
  - Prefer: update the master, then re-slice new derived surveys, then update recruiting links.
  - Use `qsync survey parity-check --a SV_MASTER --b SV_FR` to verify structure parity (or catch unintended drift).
- Text-only changes (wording tweaks):
  - If derived surveys must keep the same SurveyIDs: update each derived survey via `qsync items`.
  - Use `qsync survey export-side-by-side --a SV_MASTER --b SV_FR --label-a Master --label-b FR` to generate a single DOCX for review.
- Operational changes (Header snippet, redirect URL, activation status):
  - Use `qsync survey prolific-auth` (snippet) or Survey Master (bulk options/status).

### Practical patch runbook (text-only) when SurveyIDs must stay stable

If you cannot change SurveyIDs (or you prefer not to), treat each derived survey as the deployment surface and apply the text edits there.

Example: you updated FR wording in the master survey (as FR translations) and now need the FR derived survey to show the updated FR text (as base wording).

1. Hydrate the derived survey workbook:
   - `qsync items pull --survey-id <SV_FR>`
2. Copy the updated text into the derived workbook's **base-language** columns.
   - In the master workbook, the FR strings live in translation columns (e.g., `Text_FR_MD`, `Label_FR_MD`).
   - In the FR derived workbook, the FR strings live in base columns (e.g., `Text_fr_MD`, `Label_fr_MD`), because the base language is FR.
3. Preview and push the derived survey wording:
   - `qsync items preview --survey-id <SV_FR>`
   - `qsync items stage --survey-id <SV_FR> --yes`
   - `qsync items push --survey-id <SV_FR> --yes` (may require `--force-live` depending on responses)
4. Verify:
   - `qsync survey export-side-by-side --a SV_MASTER --b <SV_FR> --label-a Master --label-b FR`

If the change also touches other surfaces, repeat for each:

- JS: `qsync js ...` (language-specific strings in JS often need explicit i18n discipline)
- EOS: `qsync eos ...` (per-language HTML)
- Header/Footer: `qsync survey prolific-auth` or `qsync survey master`

### Current limitation (important)

Today, `qsync` does **not** provide a single command that takes "master translations" and updates the **base-language wording** of existing derived surveys in-place.

In other words:
- The master survey can hold FR text as **translations**.
- The FR derived survey shows FR text as **base**.
- There is not yet a built-in "propagate master FR translation changes into FR slice base fields" operation.

If you need that workflow, it is a good candidate for a dedicated command (e.g., "rebase updates into existing slice surveys") with strong safeguards and parity checks.

## Quick decision guide

If you want:

- One SurveyID, language selector, and translation management inside Qualtrics: use **Mode A**.
- Separate SurveyIDs per language/country (Prolific) and strict parity guarantees: use **Mode B** + `slice-language`.

If you are already in split mode and you need frequent text updates while keeping SurveyIDs stable, consider documenting an explicit policy for:

- which fields are allowed to differ per survey (Header snippet, redirect URLs, quotas), and
- what constitutes "parity" for your analysis (QIDs + DataExportTags + flow shape).
