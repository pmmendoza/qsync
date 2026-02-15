# qsync translation export (Word)

_Migrated from `appendices/qsync_translation_export.md` (monorepo) so the standalone `qsync` repo can be self-contained._

This document describes the current behavior of `qsync survey export-translation`, including document structure, formatting conventions, and the semantics/limitations of scenario exports (`--edf`).

## 1) What this export is for

The translation export produces a translator/reviewer-friendly `.docx` that mirrors **SurveyFlow order** (including conditional logic) and renders:

- Block structure (`BLOCK START: …`)
- Questions (text + statements + answer options) as compact tables
- Conditional logic (`BRANCH:` / `DISPLAY IF:`) in a visually distinct style
- Embedded Data writes as inline tables at the point they occur in SurveyFlow
- WebService nodes as a single readable “card”
- EndSurvey (DisplayMessage) content when the referenced library message is available locally

It is intentionally **not** a full Qualtrics runtime simulator; it aims to be readable, consistent, and conservative.

Note: SurveyFlow traversal is centralized in `qsync.flow_traversal` and shared across export formats (DOCX/PDF) and translation checks to keep ordering and pruning consistent.

## 2) Running the export

```bash
# Default export (writes to export/<SurveyName>__<SurveyID>__<BASE>.docx)
qsync survey export-translation --survey-id SV_xxx

# Render using cached translations (participant view)
qsync survey export-translation --survey-id SV_xxx --language FR

# Batch export multiple languages (one .docx per language)
qsync survey export-translation --survey-id SV_xxx --languages FR,NL,CS

# Bilingual review mode (EN + target rendered together)
qsync survey export-translation --survey-id SV_xxx --language FR --compare-to-base

# Enable layout heuristics (reviewer-friendly transforms; default is UI-faithful)
qsync survey export-translation --survey-id SV_xxx --language FR --compare-to-base --layout-heuristics

# Refresh cached survey definition from Qualtrics before export (network)
qsync survey export-translation --survey-id SV_xxx --language FR --refresh

# Scenario export: prune provably-irrelevant paths based on explicit Embedded Data Field values
qsync survey export-translation --survey-id SV_xxx --edf S_VERSION=PROLIFIC --edf DEBUG=F

# Scenario export using presets (from surveys/edf_presets.json)
qsync survey export-translation --survey-id SV_xxx --edf-preset <preset-name>

# List available presets for this survey
qsync survey export-translation --survey-id SV_xxx --list-edf-presets

# Print flow traversal traces (what was dropped and why)
qsync survey export-translation --survey-id SV_xxx --edf DEBUG=F --flow-trace

# Disable Mermaid rendering (keeps .flow.mmd, skips .flow.png rendering/embed)
QSYNC_MERMAID_RENDER=0 qsync survey export-translation --survey-id SV_xxx

# Omit sanitized HTML source blocks when a parsed rendering exists
qsync survey export-translation --survey-id SV_xxx --no-html
```

Note: the exported document includes a clickable **survey link**.
- If `--edf` is set, those key/value pairs are appended to the URL query string so reviewers can open the same scenario.
- If `--language/--languages` is set, the link includes `Q_Language=<LANG>` so reviewers open the same language.

Account scoping: when an account is active (via `--account <name>` or `qsync account use <name>`), artifacts are written under `export/.<name>/` by default. See `../reference/accounts.md`.

Artifacts (default):

- `export/<SurveyName>__<SurveyID>__<BASE>.docx`
- `export/<SurveyName>__<SurveyID>__<BASE>.flow.mmd`
- `export/<SurveyName>__<SurveyID>__<BASE>.flow.png` (only when Mermaid rendering is enabled; can require network access)

Artifacts (with `--language FR`):

- `export/<SurveyName>__<SurveyID>__FR.docx`
- `export/<SurveyName>__<SurveyID>__FR.flow.mmd`
- `export/<SurveyName>__<SurveyID>__FR.flow.png` (only when Mermaid rendering is enabled)

Artifacts (with `--language FR --compare-to-base`):

- `export/<SurveyName>__<SurveyID>__<BASE>-FR.docx`
- `export/<SurveyName>__<SurveyID>__<BASE>-FR.flow.mmd`
- `export/<SurveyName>__<SurveyID>__<BASE>-FR.flow.png` (only when Mermaid rendering is enabled)

## 3) Document structure

The `.docx` is structured as:

1. `SURVEY TRANSLATION EXPORT` header + timestamp (+ scenario EDF list, when present) + a clickable **survey link**
2. `LANGUAGE RENDERING SUMMARY` (only when `--language/--languages` is set; coverage + sample missing keys)
3. `COVERAGE SUMMARY` (total questions, active exported, excluded)
4. `QUESTION TYPE LEGEND` (only the abbreviations actually used in this export)
5. Mermaid flow diagram section (+ `.flow.mmd` and optional `.flow.png`)
6. `SURVEY CONTENT` (SurveyFlow traversal, in order)
7. `EXTERNAL TRANSLATION SURFACES` (QuestionJS mapping + EndSurvey message references)

## 4) Formatting conventions

### 4.1 Typography + spacing defaults

- Body text is explicitly set to **Arial, 10pt**.
- Default paragraph spacing is **after** paragraphs (no default “space before”).
- Tables are followed by an explicit spacer paragraph to avoid “tables touching”.

### 4.2 Block headers

Each block begins with a highlighted header:

`BLOCK START: {BlockName} ({BlockID})`

Conventions:

- Gray background
- Larger, bold font
- Extra spacing **both before and after**
- No forced page breaks

Blocks that are `Trash` are never rendered. In scenario exports, blocks that become empty after pruning are omitted.

### 4.3 Question blocks (tables)

Each question renders as a 1-column table with stacked rows (only the rows that exist are emitted):

1. **Metadata line** (always)
2. `DISPLAY IF:` line (if the question has DisplayLogic)
3. Question text (if present)
4. Statements / subitems (when present)
5. Answer options / labels (when present)
6. **JavaScript User-Visible Strings** (when question has JS and feature is enabled)

**Metadata line format**

`[{QID}][{QT}][JS] {ET} {VALIDATION}`

- `QID`: Qualtrics question id (monospace, 11pt)
- `QT`: abbreviated question type (legend is included near the top)
  - randomized questions append `+R` (e.g., `MC+R`)
- `[JS]`: present when QuestionJS exists or a JS file is mapped via `survey_js/survey_qid_js_map.csv`
- `ET`: `DataExportTag` (from this token onwards, font is explicitly Arial)
- `VALIDATION`:
  - `*` for Force Response (`ForceResponse=ON`)
  - `+` for Request Response (`ForceResponse=RequestResponse`)
  - empty otherwise

**JavaScript String Extraction** (enabled by default)

When a question contains JavaScript (inline or external), the exporter attempts to extract user-visible strings for translation review. The extraction process:

1. **Prioritizes COPY objects**: First extracts from internationalization patterns like `COPY = { EN: { key: 'value', ... }, ... }`
2. **Applies comprehensive filtering** to remove technical noise:
   - String concatenation artifacts (e.g., `'+ debugId +'`, `'+ String(i + 1).padStart(2,'`)
   - Debug/logging messages with technical prefixes (e.g., `fetch:`, `startSignup:`, `scheduleIdleCheck:`)
   - Variable assignments (e.g., `bs_ok=1`, `verifying=true`, `, choiceChecked=`)
   - CSS selectors (e.g., `li, .ChoiceStructure, .QuestionAnswers`)
   - Parenthetical status indicators (e.g., `(No response)`, `(Waiting...)`)
   - Technical fragments ending with quotes or incomplete code
3. **Extracts only user-facing messages**: Typically reduces output by ~29% compared to unfiltered extraction

To disable this feature globally:
```bash
qsync survey export-translation --survey-id SV_xxx --skip-js-strings
```

Extracted strings appear in a dedicated section below the question content, clearly labeled as "JavaScript User-Visible Strings" with a bulleted list.

**Compact formatting for technical questions**

Timing and Meta questions (e.g., Browser metadata collectors) are intentionally rendered as compact placeholders to avoid translator noise:
- **Timing questions**: `"Timing Block"` annotation
- **Meta questions**: `"Meta Block"` annotation

Both display only the metadata line without expanding full question details.

### 4.4 Logic blocks (Branch + DisplayLogic)

Branch and Display logic lines are formatted to clearly separate them from user-facing copy:

- Monospace (`Courier New`), 8pt
- Light gray background
- Token highlighting so the “question vs answer vs condition” is easy to scan

The export standardizes question references inside logic to include the QID:

`QIDxx:"Question wording …"`

Token colors:

- `QID…:"…"` question segment: black
- `A:"…"` answer segment: blue (bold)
- `EDF:…` embedded data segment: green (bold)
- operators and surrounding text: red (operators may be emphasized)

### 4.5 Embedded Data writes

SurveyFlow `EmbeddedData` nodes are rendered inline as an `EMBEDDED DATA WRITES` table:

- Columns: `Field | Value | Type | FlowID`
- Header row is bold
- Fixed column widths:
  - Field/Value are wider
  - Type/FlowID are narrower

`${e://Field/...}` tokens are highlighted in monospace green wherever they appear (including inside table cells).

### 4.6 WebService nodes

SurveyFlow `WebService` nodes are rendered as a single “card” table (one table per node), with:

- Header row: `WEB SERVICE: {Method} {URL} (FlowID=…)`
- Structured sections when present:
  - Method / URL / ContentType
  - Credential (param format/name/template/id)
  - RequestParams (key/value; EDF tokens highlighted)
  - Headers (key/value)
  - Body (key/value)
  - ResponseMap (`path → ${e://Field/...}`)
  - Flags (FireAndForget, StringifyValues, SchemaVersion)

### 4.7 EndSurvey (DisplayMessage) content

If a SurveyFlow `EndSurvey` node is `DisplayMessage` and references a library message, the export embeds the message content **only if it exists locally** under:

`contents/qualtrics_library_messages/<LibraryId>/<MessageId>/messages/*.html`

To fetch those message files:

```bash
qsync eos pull --survey-id SV_xxx
```

If the message content is not present on disk, the export includes an explicit note instead of silently omitting the content.

## 5) Scenario exports (`--edf`) semantics

`--edf KEY=VALUE` creates a **scenario-specific** export. The exporter attempts to prune provably-irrelevant branches and questions based on the supplied Embedded Data Field values.

Important principles:

- The pruning is **conservative**: when the exporter cannot decide a condition from the information it has, it keeps content.
- Scenario exports use the provided EDF values only; they do **not** simulate runtime side-effects (EmbeddedData writes, JS, responses, piped text resolution).

What is pruned:

1. **BranchLogic pruning (EmbeddedField expressions)**  
   When BranchLogic can be evaluated from the supplied EDF values, only the taken path is rendered.

2. **Flow-order heuristic for “Selected”**  
   Some BranchLogic expressions contain `Operator=Selected` (answer-based routing). In scenario exports, the exporter additionally treats “Question is Selected” as **false** when the referenced question has not been asked yet in the reachable flow order under the current EDF pruning. This can make some OR conditions decidable.

3. **Question-level DisplayLogic pruning (EDF-only decidable cases)**  
   If a question’s DisplayLogic becomes decidably false from the supplied EDF values, the question is omitted (unknown/undecidable DisplayLogic keeps the question).

4. **Empty blocks omitted**  
   If a block has no renderable questions after pruning and DisplayLogic evaluation, it is omitted entirely (no “BLOCK START” line).

How branch annotations behave in scenario exports:

- When a branch is decidable (via EDF evaluation or the flow-order heuristic), the export renders only the taken path and omits the `BRANCH:` / `THEN:` / `ELSE:` / `END BRANCH` annotation lines for that branch.
- Branch annotations remain for undecidable branches.

## 6) Current limitations (by design)

This export intentionally does not attempt to:

- Execute QuestionJS or infer effects from it
- Simulate responses / determine which answer options would be selected
- Execute SurveyFlow EmbeddedData writes to derive downstream EDF values
- Fully render arbitrary HTML/CSS/JS (complex interactive content is shown as best-effort rendered text and may include a sanitized `HTML (source):` block unless `--no-html` is set)

When in doubt, the exporter prefers to include content rather than risk hiding something relevant.

## 7) Translation rendering (selected language exports)

When `--language/--languages` is provided, the exporter overlays user-facing copy using the cached
survey definition translations (Language blocks + `SurveyOptions.MetaDataTranslations`).

Fallback behavior:

- Missing values fall back to the base survey-definition JSON (EN).
- Empty values fall back to EN unless the base language value is also empty for that key.

Logic rendering (BranchLogic / DisplayLogic):

- In single-language exports (`--language` without `--compare-to-base`), the exporter also attempts to render **question/option labels inside logic lines** using the target language strings (e.g., `DISPLAY IF: QID50:"…" "…" is selected`).
- This is best-effort and only works reliably when the logic object is in the structured `BooleanExpression` shape with `QuestionID` + `ChoiceLocator` (or similar). If Qualtrics only provides a free-form `Description` without IDs, the export falls back to that description (typically EN).

If a requested language is missing from the cached survey definition, use `--refresh` (or `qsync survey pull`) to update the cache.

### 7.1 EOS message language selection

For `EndSurvey` → `DisplayMessage`, the exporter embeds the EOS library message **only if it exists locally** (see section 4.7).

When `--language` is provided:
- in single-language mode, the exporter prefers the matching message variant (e.g., `fr`) when present, otherwise falls back to `en`.
- in bilingual mode (`--compare-to-base`), it shows the base (`en`) and target variant side-by-side when both exist.

### 7.2 Bilingual question layout (`--compare-to-base`)

When `--compare-to-base` is enabled, each question table switches to a **two-column side-by-side** layout:

- Left column: `EN`
- Right column: target language (e.g., `FR`, `NL`, `CS`)

The first row (QID metadata) and optional display-logic row are shared (spanning both columns). Question text, statements, and options/labels are then shown side-by-side for fast review.

### 7.3 Layout heuristics (`--layout-heuristics`)

By default, the exporter aims to be **UI-faithful** (e.g., `<ul>/<li>` remains lists).

If you enable `--layout-heuristics`, the exporter may apply reviewer-friendly layout transforms that can diverge from the Qualtrics UI (example: converting certain structured lists like `[task]/[time]/[pay]/[reward]` into a small 2-column table).
