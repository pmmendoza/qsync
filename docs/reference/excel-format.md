# qsync Excel formatting principles (items workbooks)

This document summarises the workbook structure and formatting rules applied by `qsync` for per-survey Excel workbooks.

Workbooks are written to account-scoped paths based on workspace layout:
- account-root layout: `accounts/<account>/excel/<slug>-<SurveyID>.xlsx`
- legacy compatibility layout: `excel/<slug>-<SurveyID>.xlsx` (default) or `excel/.<account>/...` (named account)

Implementation reference: `src/qsync/excel_io.py`.

---

## 1. Sheet overview

The per-survey workbook contains these relevant sheets for wording sync:

- `Questions` (1 row per QID; question text + flags)
- `Options` (1 row per option/scale label)
- `Subitems` (1 row per matrix row/statement)
- `SBS_Columns` (SBSMatrix only: 1 row per side-by-side column header)
- `SBS_ColumnAnswers` (SBSMatrix only: 1 row per side-by-side column answer label)
- `Survey_Metadata` (survey-level metadata text fields)
- `Embedded_Data` (SurveyFlow embedded defaults)
- `System` (read-only context: timing/meta)
- `Instructions` (auto-generated guidance; regenerated on workbook refresh)

Some workbooks also include additional helper sheets (for example translation-key mapping); treat them as `qsync`-owned.

---

## 2. Column roles

### 2.1 System-owned columns

System-owned columns are populated and maintained by `qsync` and/or Qualtrics (IDs, tags, ordering/context). Examples include:

- `SurveyID`
- `QID`
- `BlockName`
- `QuestionType`
- `DataExportTag` / `ExportTag`
- `ChoiceId` / `AnswerId` / `ColumnId`
- `FlowID` / `FlowOrder`
- `Code`
- Preview columns like `OptionsPreview` and `SubitemsPreview`
- Derived columns like `RequiredResponse`

Formatting:

- System headers (and their body cells) are bolded.
- System/read-only body cells are shaded light grey to signal “not this sheet’s editing surface”.

Behaviour:

- These columns are refreshed from cache/API on each workbook refresh (`qsync items pull`).

### 2.2 User-editable columns

Columns where you are expected to edit content:

- `Questions`: `text_{base}` (plus `ishtml_{base}` when you intentionally need raw HTML), `ForceResponseMode`, `ValidationType`, `ValidationSettingsJSON`, `RandomizationType`, `RandomizationSettingsJSON`
- `Options`, `Subitems`, `SBS_Columns`, `SBS_ColumnAnswers`: `Label_{base}_MD` (plus `Label_{base}_IsHTML` when you intentionally need raw HTML)
- `Survey_Metadata`: `*_MD` columns (plus `*_IsHTML` flags)
- `Embedded_Data`: `Value`

Read-only mirror columns:

- `Questions.QuestionConfigJSON` is a canonical JSON mirror generated from the editable response-setting columns.

Translation columns:

- When present, `text_<lang>` / `Label_<lang>_MD` columns (and their `ishtml_<lang>` / `*_IsHTML` flags) are also user-editable.

Preservation rule on refresh:

- `qsync` does not overwrite non-empty `*_MD` cells (and `Embedded_Data.Value`). New rows/cells are filled when missing.
- `*_IsHTML` flags are treated as workflow flags and may be refreshed from cached survey JSON on workbook refresh.

### 2.3 Boolean flag columns

Common flags include:

- `Text_*_IsHTML`, `Label_*_IsHTML`
- `RequiredResponse` (derived/read-only in `Questions`)

Formatting:

- Data validation is applied as a drop-down list: `TRUE` / `FALSE` (blank allowed).

### 2.4 Notes

Some sheets include `MetaComment` for notes/ownership:

- `qsync` writes `MetaComment` for options and SBS rows that are externally managed.
- Treat `MetaComment` as `qsync`-owned unless you know what you are doing.

---

## 3. HTML vs Markdown highlighting

When a `*_IsHTML` flag is set to `TRUE`, `qsync` applies conditional formatting to highlight the corresponding `*_MD` cell (pale yellow/orange). This is a visual warning that the cell content is treated as raw HTML.

## 3.1 Required-response highlighting

`Questions.RequiredResponse` is a system-derived field (`TRUE` when `ForceResponseMode` is `ON` or `RequestResponse`).

When `RequiredResponse=TRUE`, `qsync` highlights `text_*` cells (light red tint) so required questions are obvious during workbook review.

---

## 4. Dirty indicators (changes since last sync)

`qsync items preview` (and related workflows) maintain a `Dirty` column to help locate rows that differ between Excel and the cached survey JSON.

- A `Dirty` column exists on:
  - `Questions`
  - `Options`
  - `Subitems`
  - `SBS_Columns`
  - `SBS_ColumnAnswers`
  - `Embedded_Data`

Behaviour:

- Each preview run clears prior `Dirty` values and re-marks them based on current diffs.
- When a row is dirty, its `Dirty` cell is set to `Y`.
- Conditional formatting highlights the edited cell:
  - `Questions`: `text_{base}`, `ForceResponseMode`, `ValidationType`, `ValidationSettingsJSON`, `RandomizationType`, `RandomizationSettingsJSON`
  - `Options`/`Subitems`/`SBS_*`: `Label_{base}_MD`
  - `Embedded_Data`: `Value`

---

## 5. Externally managed wording

Some questions have options/subitems owned by scripts (for example: recognition, salience, cued recall). These are identified by `DataExportTag` (see `EXTERNALLY_MANAGED_TAGS` in `src/qsync/excel_io.py`).

Behaviour:

- Question text (`Questions.Text_*`) remains editable.
- By default, `qsync items stage/push` (and `qsync sync`) skip option/subitem/SBS edits for externally managed questions.
- You can override this for specific QIDs by:
  - Setting `QSYNC_ITEMS_ALLOW_EXTERNALLY_MANAGED_QIDS` in your env/.env (supports tokens like `QID15` and `SV_xxx:QID15`), or
  - Setting `QSYNC_ITEMS_ALLOW_EXTERNALLY_MANAGED_QIDS=all` (or `*`) to allow all externally managed QIDs for the current command context, or
  - Passing `--allow-externally-managed-qids ...` to `qsync items preview|stage|push` or `qsync sync` (CLI flag takes precedence).

SBSMatrix note:

- Qualtrics side-by-side matrices are encoded as `QuestionType="SBS"` and `Selector="SBSMatrix"`.
- For SBSMatrix questions, the Options sheet is not used; the relevant editable surfaces are:
  - `Subitems` (statements/rows)
  - `SBS_Columns` (column headers)
  - `SBS_ColumnAnswers` (per-column answer labels)

---

## 6. Tables and widths

To support filtering and sorting, the main editing sheets are stored as Excel Tables (banded rows + filter dropdowns). Table names:

- `QuestionsTable`
- `OptionsTable`
- `SubitemsTable`
- `SBSColumnsTable`
- `SBSColumnAnswersTable`
- `EmbeddedDataTable`

Column widths are tuned by header name, and long text columns are wrapped.
