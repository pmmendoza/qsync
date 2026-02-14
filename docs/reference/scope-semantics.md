# Scope semantics (`--scope`)

_Migrated from `appendices/qsync_scope_semantics.md` (monorepo) so the standalone `qsync` repo can be self-contained._

`qsync` supports a shared `--scope` expression language across dimensions:

- `qid:QID123` — match a specific Qualtrics QID
- `tag:<DataExportTag>` — match a Qualtrics `DataExportTag` (e.g. `sd_age`, `sd_gender`)
- `js:<file>` — match a JS file (used by JS tooling)

Boolean operators are supported: `AND`, `OR`, parentheses.

Important: `tag:` matches **DataExportTag**, not Excel helper columns like `InPre`/`InPost`.

---

## Items (`qsync items ...`)

Where `--scope` applies:
- `qsync items preview --scope ...`
- `qsync items stage --scope ...`
- `qsync items push --scope ...`

Matching rules:
- `qid:` matches the QID (e.g. `QID10`).
- `tag:` matches the **Questions sheet** `DataExportTag` value for that QID.

Notes / limitations:
- `--scope` currently filters **question-level diffs**; embedded-data diffs are still detected via `Embedded_Data` and may be staged/pushed unless you use `--embedded-data-only` (stage) or avoid embedded edits.

---

## JS (`qsync js ...`)

Where `--scope` applies:
- `qsync js preview --scope ...`
- `qsync js stage --scope ...`
- `qsync js push --scope ...`

Matching rules:
- `qid:` matches the QID being compared/pushed.
- `tag:` matches the question’s `DataExportTag` from the cached survey definition.
- `js:` matches the local JS file name:
  - the file stem (e.g. `js:newschoice_pre-1-newschoice_generator`), or
  - the full mapping entry (e.g. `js:newschoice_pre-1-newschoice_generator.js`).

Notes / limitations:
- JS scope is applied to the mapping (JS file → QIDs) before diff/push; it does not invent new mappings.

---

## Translations (`qsync translations ...`)

Where `--scope` applies:
- `qsync translations preview --scope ...`
- `qsync translations stage --scope ...`
- `qsync translations push --scope ...`

Matching rules:
- `qid:` matches the QID.
- `tag:` matches the **Questions sheet** `DataExportTag` value for that QID.

Notes / limitations:
- `--scope` is designed for question-level translation keys (QuestionText/Choices/Answers/Subitems). Survey-level metadata translations may still be included depending on the workbook content.

---

## EOS (`qsync eos ...`)

Current state:
- `qsync eos ...` accepts `--scope` for CLI parity, but it is currently ignored (preview/stage/push operate on all referenced EOS messages).

If you need smoke-safe EOS end-to-end testing:
- Prefer `qsync eos clone-shared --survey-id ... --yes` first, so the smoke survey references survey-specific EOS library messages.

---

## Sync orchestrator (`qsync sync`)

- `qsync sync --scope ...` passes scope to per-dimension preview/stage/push where supported.
- When `qsync sync --yes --pending-action abort --json` is blocked by pending staged changes, the emitted `next_commands` preserve the original `--scope` so you can re-run the same slice later.
- In interactive **QID-mode**, “Search by ExportTag (autocomplete)” filters by workbook `DataExportTag` and only offers QIDs with detected edits.
