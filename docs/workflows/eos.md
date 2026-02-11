# EOS workflow (End-of-Survey library messages)

Qualtrics "EndSurvey" nodes can reference **library messages** (HTML content stored in a Qualtrics Library).

These messages are a separate editing surface from:

- Items/Translations (question wording and translations inside the survey definition)
- JS (QuestionJS)

If you want multilingual parity, EOS messages must be managed explicitly via `qsync eos`.

## What `qsync eos` manages

`qsync eos` pulls and pushes **library message HTML** referenced by a survey's SurveyFlow. Messages typically have one HTML payload per language.

### Files on disk

Pulled messages are stored under:

- `contents/qualtrics_library_messages/<LibraryId>/<MessageId>/`

Common files:

- `meta.json`: library/message identifiers + description
- `contexts.json`: where the message is referenced (survey id + flow id)
- `messages/_keys.json`: maps language keys (e.g., `en`, `fr`) to local HTML filenames
- `messages/k_*.html`: the actual HTML content per language
- `backups/<timestamp>.json`: raw snapshots for rollback/debugging

Pending/staged EOS pushes are recorded under:

- `surveys/pending/eos/<SurveyID>.json`

## Quick runbook

1. Pull EOS messages referenced by a survey:

```bash
qsync eos pull --survey-id SV_xxx
```

2. Edit the HTML files:

- Find the message under `contents/qualtrics_library_messages/<LibraryId>/<MessageId>/messages/`
- Use `messages/_keys.json` to locate the right file for a language (e.g., `en` -> `k_ABC.html`)
- Edit the corresponding `k_*.html`

3. Preview, stage, push:

```bash
qsync eos preview --survey-id SV_xxx --detailed
qsync eos stage   --survey-id SV_xxx --yes
qsync eos push    --survey-id SV_xxx --yes
```

Notes:
- `push` enforces the same safeguards as wording/JS pushes (locks, live responses, etc.).
- Use `--force-live` if finished responses exist; use `--force-preview` for preview-mode overrides.
- Use `--no-publish` if you do not want to auto-publish after EOS changes.

## Shared library messages (important)

Library messages can be referenced by multiple surveys. Editing a shared message can unintentionally change multiple surveys at once.

Recommended workflow:

1. Detect shared usage (local scan happens during pull/preview/stage; you can also use `references`):

```bash
qsync eos references --library-id UR_xxx --message-id MS_xxx
```

2. If a message is shared and you want survey-specific behavior, clone it:

```bash
qsync eos clone-shared --survey-id SV_xxx --yes
```

This clones shared messages and rewrites SurveyFlow to reference the clones (API writes). By default, it refuses to run on non-"smoke" surveys unless you pass `--allow-non-smoke`.

Escape hatches:
- `--allow-shared-message-edit` allows editing even if the message is shared (not recommended).

## Repair and drift

If local message files are out of sync with live content (or you want a clean re-pull):

```bash
qsync eos repair --survey-id SV_xxx
```

If your cached survey definition is drifted, preview/push may prompt or refuse unless you pass `--allow-drift`.

## Translation hygiene for EOS

EOS HTML frequently contains:

- embedded data placeholders (e.g., `${e://Field/...}`)
- external links
- Prolific/redirect logic (sometimes)

Treat EOS HTML as production code:

- preserve placeholders and URL parameters exactly
- avoid inline scripts unless you fully control the environment
- prefer small, auditable changes with `--detailed` preview diffs
