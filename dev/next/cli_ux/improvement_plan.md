j# CLI UX Enhancement Plan: Learning from slackdump

**Purpose:** Independent improvement elements for qsync's CLI user experience, inspired by slackdump's TUI approach.

**Context:** This plan contains discrete, independently implementable UX improvements. Unlike linear implementation plans, each element can be tackled separately and provides standalone value.

## Implementation status (updated 2026-02-15)

Implemented on `origin/main` (commit `72ccfeb`):
- Rich panels/columns helpers (`terminal_output.py`) with JSON/TTY gating.
- Drift display upgraded to 2-column summary + diff preview (`drift_check.py`).
- Sync menu: Rich table for change detection, conflict UX improvements, and on-demand “View details” (`sync_orchestrator.py`).
- Survey picker: structured menu + “View details (top 30)” + disabled locked/no-API-edit; inactive shown as informational only (`cli_survey.py`).
- `qsync help <topic>` content expanded + workspace default account precedence wired (`cli.py`).
- Textual TUI: live right-pane details on highlight (`tui/app.py`).
- Account commands/prefs modules are tracked (prevents runtime import failures after reinstall): `cli_account.py`, `workspace_prefs.py`.

Implemented on `origin/agentx/ux-hardening` (rollback-friendly branch):
- Menus: empty-string choices treated as separators; instruction footer fits terminal width; fallback wraps instructions (`interactive_menu.py`).
- Output: `NO_COLOR` disables ANSI globally (`terminal_colors.py`).
- Items diffs: optional “before/after panels” view without removing unified diffs (`dimensions/items.py`).
- Survey selection: shared picker helper + consistent “View details / Manual entry / Back” affordances wired into `survey_inventory.py` and record-based selection in `cli_survey.py` (`survey_selection.py`).

---

## Executive Summary

### Current State
Qsync has a solid interactive menu system built on questionary with:
- Arrow-key navigation with fallback to numbered selection
- Good TTY detection and environment variable control (`QSYNC_USE_QUESTIONARY`)
- Comprehensive error handling and terminal state preservation
- Rich library in dependencies (underutilized)
- Clean separation of interactive vs non-interactive modes

### What Slackdump Does Better
From analysis of slackdump (Go-based, using Charmbracelet ecosystem):
1. **Dynamic menu validation** - Items enable/disable based on runtime state
2. **Better progress feedback** - Spinners, progress bars, async operation visibility
3. **Preview panels** - Show context/details alongside selections
4. **Built-in help system** - Keyboard shortcuts overlay
5. **Enhanced styling** - Richer visual hierarchy with borders, padding, colors
6. **Nested menu models** - Each item can have sub-model with validation
7. **Form validation** - Real-time validation for text inputs

### Python Equivalents
- **Textual** - Full TUI framework (Bubble Tea equivalent) - ship as optional extra `qsync[tui]` for real multi-pane UIs
- **Rich** (already in use!) - Can do much more: progress, spinners, panels, tables, layouts
- Enhanced **questionary** usage - Better styling and validation

---

## Proposed UX Contract (Cross-Cutting)

These are repo-level UX rules to keep the CLI consistent across commands and to
avoid “pretty but fragile” output.

**Approval gate (cross-cutting):**
- [x] **Approve UX contract + menu model refactor** (enables Elements 2/3/4/5/9 to be implemented cleanly)

### Output Modes (must be explicit)
- **Interactive TTY:** rich UX allowed (spinners/progress, panels, tables).
- **Non-TTY / CI:** deterministic, line-oriented output only; no cursor control; avoid spinners.
- **JSON mode (per-command `--json`):** JSON to stdout only; no additional human output (timers/spinners/help must be suppressed).

### Survey selection contract (cross-cutting)
When a command supports omitting `--survey-id`, survey selection should be consistent and command-appropriate:
- **Non-interactive / non-TTY:** `--survey-id` is required (fail fast with a clear message).
- **Interactive TTY:** picker must always offer:
  - scoped lists (focal vs all) when inventory exists
  - on-demand details (“View details”)
  - regex-powered search/filter
  - manual entry escape hatch (direct SurveyID or search)
  - consistent “↩ Back” and “✗ Cancel” semantics
- **Source-of-truth depends on command type:**
  - commands that do not ingest local editing surfaces (remote-only) should default to using the API list (equivalent to `qsync survey list`) for freshness.
  - commands that ingest local editing surfaces should default to inventory (local) first, because it aligns with local artifacts (workbooks, caches, pending).

### Navigation Model
- All nested flows should provide **← Back** where meaningful (not just “Cancel”).
- “Cancel” should always mean **abort without changes** and return to the caller consistently.

### Menu Model (why this matters for qsync)
Today, some menus are built from raw strings plus “fake entries” (empty strings, section headers, separators). This leads to selectable-but-ignored entries and inconsistent behavior between questionary and fallback.

**Contract:** menus should be built from a small structured model (option value, enabled/disabled + reason, separators, and special actions like Back/Cancel).

**Value potentially lost by adopting this contract:** it requires refactoring existing menu call sites (more engineering time up front).
**Value gained:** consistent UX and fewer edge cases, especially in fallback/CI modes.

**Reality check (current code):** fallback currently filters separators only by `startswith("─")`. This should be aligned with `_is_separator()` so separators are never selectable in either questionary or fallback mode.

**Priority note:** treat the menu model + fallback consistency as the first concrete milestone. Most other UX elements (preview/help/cancel semantics) become cheaper and less fragile once menus are structured.

---

## Slackdump Feature Parity (Reality Check)

This section clarifies what we *can* and *cannot* replicate with the current
qsync stack (questionary + Rich) versus a real TUI framework (Textual).

**Approval gate (optional dependency):**
- [x] **Approve a Textual TUI pilot shipped as an optional extra (`qsync[tui]`)** (two-pane layout, live preview, key overlays)

### Feasible with current stack (questionary + Rich)
- **← Back** / nested flows: yes (implemented as explicit menu actions + loop control).
- **Static two-column context** (after selection / during summaries): yes via `rich.columns.Columns` (Element 7).
- **Progress/spinners/timing**: yes (Elements 1 and 10).
- **Disabled options with reasons**: yes (Element 2, via menu model + disabled rendering).
- **Real-time input validation**: yes (Element 6).

### Not truly feasible with current stack (without deeper UI work)
- **Live-updating right-hand context panel while moving the cursor** (screenshot-style):
  - questionary does not provide a clean supported hook for “highlight changed” to update a second pane.
  - You can approximate by printing a table before the prompt, but it won’t update live.
- **`?` help overlay / keybind overlays** inside menus: similar limitation.

### Options if we want full parity
- **Textual** (recommended for parity): build a small multi-screen UI for config/onboarding and selection flows (installed via `qsync[tui]`).
- **prompt_toolkit directly**: more control than questionary, but higher complexity and more “terminal footguns”.

### Enablement + Packaging (must stay safe)
- TUI ships behind an optional dependency extra: `pip install qsync[tui]` (base install stays unchanged).
- TUI must never start in non-interactive contexts (non-TTY, `--json`, `--yes`).
- `textual` imports must be lazy/guarded so the base install has no runtime dependency on it.

---

## Independent Enhancement Elements

Each element below is independently implementable and provides standalone value.

---

## Element 1: Enhanced Progress & Status Indicators

**Task ID:** `QSYNC-UX-010`

**Motivation:** Operations like `qsync survey inventory`, `qsync sync`, and file processing currently show minimal progress feedback. Slackdump uses spinners and progress bars extensively for async operations.

**Current State:**
- Line-by-line text output during operations
- `rich.progress.track` used sparingly (survey_inventory.py:594)
- No visual indication of operation status during API calls

**Proposed Enhancement:**
Use rich's `Progress`, `Spinner`, and `Status` components for better feedback:

```python
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.spinner import Spinner
from rich.status import Status

# Example: Inventory refresh with spinner
console = Console()
with console.status("[cyan]Fetching survey inventory...", spinner="dots"):
    refresh_inventory(...)

# Example: Multi-survey sync with progress bar
with Progress(
    SpinnerColumn(),
    TextColumn("[progress.description]{task.description}"),
    BarColumn(),
    TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
) as progress:
    task = progress.add_task("Syncing surveys", total=len(surveys))
    for survey in surveys:
        sync_survey(survey)
        progress.advance(task)
```

**Files to Modify:**
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/terminal_output.py` (centralize Rich gating/wrappers to keep `--color never`/non-TTY/`--json` consistent)
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/survey_inventory.py` (inventory refresh)
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/sync_orchestrator.py` (sync operations)
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/onboarding.py` (setup wizard)

**Acceptance Criteria:**
- [x] API operations show spinner with operation description
- [x] Multi-item operations show progress bar with percentage
- [x] Spinners/progress respect `--color never` and non-TTY environments
- [x] Timing information displayed after completion ("Completed in 2.3s")

**Definition of Done:**
- [x] Spinners appear during all API calls > 1 second
- [x] Progress bars show for batch operations (inventory, multi-survey sync)
- [x] (Optional) `QSYNC_USE_RICH=0` environment variable disables rich components (use only as a kill switch; default behavior should already be safe via TTY + `--color never`/`NO_COLOR` gating)
- [x] Non-TTY mode falls back to simple text output

---

## Element 2: Dynamic Menu Item Validation

**Task ID:** `QSYNC-UX-011`

**Motivation:** Slackdump's menu items have `Validate() func() error` called every render to enable/disable options based on state. Qsync menus currently show all options always, even when invalid.

**Approval gate:**
- [x] **Approve revised Element 2 approach (menu model + disabled reasons)**  
  *(This changes the original plan from “use Separators to disable” to a structured menu model so questionary + fallback behave consistently.)*

**Potential value lost if we change the approach:** slightly more code and refactors up front (we must touch `interactive_menu.py` and menu call sites).  
**Value gained:** fewer “selectable-but-ignored” menu entries, consistent behavior in fallback mode, and a cleaner foundation for Element 3/4/9.

**Current State:**
- All menu items always enabled
- Post-selection validation with error messages
- Example: sync_orchestrator.py:752-828 shows dimension choices even when no changes exist

**Proposed Enhancement (revised):**
Introduce a small structured “menu model” so each option can be:
- enabled/disabled (with a reason)
- a separator / section header
- a navigation action (← Back / ✗ Cancel)

Then render that model to:
- **questionary** (enabled/disabled styling)
- **fallback** (numbered list that clearly marks disabled options and prevents selecting them)

This avoids today’s fragile pattern of “fake items” like `""` or `"Fix errors:"` that are selectable but then ignored.

**Implementation Approach (staged):**
1. **Menu model in `interactive_menu.py`**
   - Add a minimal `MenuOption` type (label, value, enabled, disabled_reason, kind).
   - Update `select_from_list()` to accept menu options as well as plain strings (for incremental migration).
   - Update `_fallback_select()` to use `_is_separator()` and to display disabled options + reasons without allowing selection.
2. **Refactor key menus to use the model**
   - `sync_orchestrator.prompt_dimension_selection()` (remove empty strings + section headers; make “Fix errors” actions non-selectable until chosen explicitly).
   - `_prompt_qid_mode_dimension_selection()` (show disabled dims with “none” reason, keep stable ordering).
   - `survey_inventory.prompt_for_survey_id()` (standardize ← Back and ✗ Cancel).
3. **Consistency checks**
   - Ensure stable order: `items`, `js`, `translations`, `eos`.
   - Ensure “Back/Cancel” semantics are consistent across nested menus.

**Files to Modify:**
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/sync_orchestrator.py` (dimension selection - lines 752-828)
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/survey_inventory.py` (survey selection - lines 827-858)
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/interactive_menu.py` (add validation support)

**Acceptance Criteria:**
- [x] Disabled options are visible but not selectable (with a short reason)
- [x] Status icons indicate option state (✓ ✗ ⚡; `─` = skipped)
- [x] Validation runs immediately before display (not post-selection)
- [ ] No selectable “fake entries” (empty strings/section headers) remain in menus
- [x] Fallback mode (numbered) also indicates unavailable/disabled options consistently

**Definition of Done:**
- [x] Dimension menus show all dimensions in stable order, disabling ones with no changes (instead of hiding them)
- [x] Survey selection greys out locked/no-API-edit surveys with explanatory text; inactive remains selectable
- [ ] All validation errors shown inline, not after selection

---

## Element 3: Preview Panels & Context Display

**Task ID:** `QSYNC-UX-012`

**Motivation:** Slackdump shows preview panels and context information alongside selections. Qsync menus are minimal with no context preview.

**Approval gate:**
- [x] **Approve revised Element 3 approach (on-demand details first; optional 2-pane TUI later)**  
  *(This changes the original “always print a table before menus” into “show details when requested”, plus an explicit optional path to a true side panel.)*

**Potential value lost if we change the approach:** less “always-visible” context before every prompt; no live-updating preview panel while moving the cursor (unless we later adopt a real TUI framework).  
**Value gained:** much less scroll/noise in long flows (especially `qsync sync`), and a feasible path to Slackdump-like previews without forcing an early Textual migration.

**Current State:**
- Bare menu lists (e.g., "SV_abc123 - Survey A (focal)")
- No preview of what selection will do
- Context shown only after selection

**Proposed Enhancement (revised):**
### Stage 1 — On-demand context (works with current stack)
Use Rich `Panel`/`Table`/`Columns` to show context, but only when it adds value:
- A “**View details**” / “**Show summary**” menu action that prints a table/panel, then returns to the same menu (with **← Back** where relevant).
- Post-selection confirmation panels (“Here’s what will happen next”) before destructive actions.
- Two-column *static* context where it fits (e.g., local vs remote counts, before vs after), using `rich.columns.Columns` and auto-stacking on narrow terminals.

### Stage 2 — True side panel preview (Slackdump-like; optional)
The screenshot-style “menu on the left + live-updating context on the right” requires a real TUI layout loop (Textual via `qsync[tui]` or direct prompt_toolkit), because questionary doesn’t expose “highlight changed” events in a clean, supported way.

If we want true Slackdump parity:
- adopt **Textual** (`qsync[tui]`) for a few high-value flows (e.g., onboarding/config, sync selection), or
- replace questionary menus with direct **prompt_toolkit** layout widgets.

**Files to Modify:**
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/survey_inventory.py` (survey selection)
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/sync_orchestrator.py` (conflict resolution, dimension selection)
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/drift_check.py` (drift preview)

**Acceptance Criteria:**
- [x] At least one high-cost menu (sync dimension selection) provides an on-demand “View details” action
- [x] Conflict resolution can show a readable summary panel (counts + identifiers, not giant payloads)
- [x] Comparison outputs can render in two columns when terminal width allows (auto-stack otherwise)
- [x] All panels/tables respect `--color never` and non-TTY environments

**Definition of Done:**
- Stage 1 done:
  - All multi-item selections offer on-demand preview tables/panels
  - Confirmation prompts show summary panel of pending action
  - Drift previews use rich diff/syntax highlighting where already available
  - Tables auto-adjust to terminal width
- Stage 2 done (optional):
  - At least one flow ships a true two-pane interactive UI (left menu + right context)

---

## Element 4: Built-in Help System

**Task ID:** `QSYNC-UX-013`

**Motivation:** Slackdump has integrated help system using bubbles/help and bubbles/key. Qsync menus have minimal guidance beyond instruction text.

**Approval gate:**
- [x] **Approve revised Element 4 approach (help footer + `qsync help` topics; overlays optional)**  
  *(This changes the original “press `?` for overlay” into a feasible staged approach for questionary.)*

**Potential value lost if we change the approach:** we may not ship a true in-menu `?` overlay immediately; users lose the “instant overlay” discoverability Slackdump has.  
**Value gained:** help becomes real and consistent across questionary + fallback + CI, without a risky prompt_toolkit deep hack.

**Current State:**
- Basic instruction text: "Use ↑↓ arrows and Enter to select"
- No keyboard shortcut reference
- No contextual help in menus

**Proposed Enhancement (revised):**
### Stage 1 — Help footer everywhere (works now)
- Standardize instruction footer text across all menus (questionary + fallback):
  - Navigation: ↑↓, Enter
  - Cancel: Ctrl+C
  - Tips: “Use `--survey-id` to skip selection”, “Use `--yes` for non-interactive”, etc.
- Ensure the footer is short and CI-safe (no ANSI when `--color never` or non-TTY).

### Stage 2 — `qsync help <topic>` (discoverable long-form help)
- Add lightweight help topics for common workflows (sync, staging/pending, drift, onboarding).
- Menus can include a “Help…” choice that prints the relevant topic and then returns (← Back).

### Stage 3 — True `?` overlay (optional; requires TUI upgrade)
If we want Slackdump-style overlays (`?` opens help while staying in the menu), treat it as a **Textual (`qsync[tui]`) / prompt_toolkit** feature.

**Help Content Locations:**
- Menu navigation shortcuts (↑↓ Enter Ctrl+C)
- Context-specific tips (e.g., "Tip: Use --survey-id to skip selection")
- Common workflows (e.g., "Workflow: init → preview → apply → push")

**Files to Modify:**
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/interactive_menu.py` (add help display)
- All menu call sites (add context parameter)

**Acceptance Criteria:**
- [x] All selection menus show navigation shortcuts in footer (questionary + fallback)
- [x] Long-form help is available via `qsync help <topic>` (or equivalent)
- [x] Help text includes workflow guidance
- [ ] Help respects terminal width

**Definition of Done:**
- Stage 1 done:
  - Instruction text enhanced with shortcuts and tips
  - Numbered fallback mode shows help text too
  - Non-TTY mode omits help (doesn't break scripts)
- Stage 2 done:
  - Help topics exist and are linked from relevant menus/commands
- Stage 3 done (optional):
  - `?` overlay works reliably in interactive menus without breaking terminals

---

## Element 5: Enhanced Visual Hierarchy

**Task ID:** `QSYNC-UX-014`

**Motivation:** Slackdump uses Lip Gloss for sophisticated styling. Qsync styling is basic (questionary custom style + terminal_colors).

**Approval gate:**
- [x] **Approve revised Element 5 approach (style guide + selective Rich panels)**  
  *(This changes the original “use borders/panels everywhere” into “use panels sparingly and standardize the basics first”.)*

**Potential value lost if we change the approach:** less “wow factor” from heavy panel/border usage everywhere.  
**Value gained:** better scanability in long command output (especially `qsync sync`), and fewer messy CI logs / wrapped borders.

**Current State:**
- interactive_menu.py:29-47 defines basic questionary style
- terminal_output.py provides colored(), header(), success(), warn(), error()
- No borders, padding, or layout control

**Proposed Enhancement (revised):**
### Stage 1 — Define a small style guide
Add a documented “terminal style guide” that answers:
- What is a header? What is a section? What is a warning?
- When do we use a panel vs plain text?
- How does output degrade in `--color never`, non-TTY, and JSON mode?

### Stage 2 — Apply selectively in high-value places
Use Rich panels/tables/columns for *summaries and decisions*, not for every line:
- Command “summary header” at the start of a complex workflow (TTY only).
- Final result summary panels (TTY only).
- Tables for lists; columns for comparisons (auto-stack on narrow terminals).

Example (TTY-only):

```python
from rich.console import Console, Group
from rich.panel import Panel
from rich.columns import Columns
from rich import box

# Example: Command output with visual hierarchy
console = Console()

# Header with border
console.print(Panel(
    "[bold cyan]qsync sync[/] - Survey Synchronization",
    box=box.DOUBLE,
    border_style="cyan"
))

# Sections with padding
console.print()
console.print("[bold]Configuration:[/]")
console.print("  Survey: SV_abc123 (focal)")
console.print("  Dimension: items")
console.print("  Scope: staged")
console.print()

# Success message with emoji
console.print(Panel(
    "✓ Sync completed successfully",
    style="green",
    box=box.ROUNDED
))
```

**Design Patterns:**
1. **Section headers** - Bold cyan with separator line
2. **Info blocks** - Panels for important information
3. **Success/error** - Colored panels with icons
4. **Lists** - Indented with bullet points
5. **Multi-column** - Side-by-side display for comparisons

**Files to Modify:**
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/terminal_output.py` (add rich helpers)
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/cli.py` (command output)
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/sync_orchestrator.py` (sync output)

**Acceptance Criteria:**
- [ ] Command outputs follow a documented style guide (consistent headers/sections/summaries)
- [ ] Success/error messages use colored panels
- [ ] Multi-item lists use indentation and bullets
- [ ] Side-by-side comparisons use columns
- [ ] All styling respects `--color never`

**Definition of Done:**
- Style guide is documented (in `terminal_output.py` or `docs/`)
- Info/success/error/warn have panel variants for TTY mode (plain text fallback otherwise)
- Multi-column layout used for before/after comparisons (auto-stack < 80 cols)
- Panels are used selectively (summaries/decisions), not for routine log lines

---

## Element 6: Real-time Form Validation

**Task ID:** `QSYNC-UX-015`

**Motivation:** Slackdump uses Huh for interactive forms with real-time validation. Qsync text inputs validate only after submission.

**Approval gate:**
- [x] **Approve Element 6 constraints (validators must be local-only)**  
  *(Validators must not make network calls or write to disk; they must be fast and deterministic.)*

**Potential value lost by enforcing local-only validators:** we can’t do “validate token by calling /whoami while typing”.  
**Value gained:** responsive UX and fewer surprising failures/latency in prompts.

**Current State:**
- interactive_menu.py:239-275 `text_input()` - basic questionary.text()
- Validation happens post-input with error message
- onboarding.py credentials input has no validation

**Proposed Enhancement:**
Add questionary validators for real-time feedback:

```python
from questionary import ValidationError, Validator

class SurveyIdValidator(Validator):
    def validate(self, document):
        text = document.text
        if not text.startswith("SV_"):
            raise ValidationError(
                message="Survey ID must start with 'SV_'",
                cursor_position=len(text)
            )
        if len(text) < 10:
            raise ValidationError(
                message="Survey ID too short",
                cursor_position=len(text)
            )

# Usage
survey_id = text_input(
    "Enter survey ID:",
    validator=SurveyIdValidator,
    validate_while_typing=True
)
```

**Validation Types to Add:**
1. Survey ID format (SV_*)
2. API token format (non-empty, proper length)
3. Datacenter host format (hostname only, no URL scheme)
4. File path existence
5. QID format (QID*)

**Files to Modify:**
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/interactive_menu.py` (add validators)
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/onboarding.py` (use validators for credentials)
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/cli.py` (use validators for prompts)

**Acceptance Criteria:**
- [x] Survey ID input validates format in real-time
- [x] API token input validates length/format
- [x] Datacenter host input validates as a hostname (no scheme)
- [x] Error messages show inline during typing
- [x] Fallback mode validates after input

**Definition of Done:**
- Common input types have validators defined
- Validators work with `validate_while_typing=True`
- Error messages are helpful and actionable
- Numbered fallback mode validates after submission

---

## Element 7: Multi-Column Layouts

**Task ID:** `QSYNC-UX-016`

**Motivation:** Better terminal space usage for side-by-side comparisons (e.g., before/after, local/remote).

**Approval gate:**
- [x] **Approve Element 7 constraints (columns only when readable; always degrade safely)**  
  *(This is a small re-scope: use columns for comparisons, but never at the expense of readability or CI stability.)*

**Potential value lost by adding constraints:** fewer “always-two-column” displays (sometimes it will stack vertically).  
**Value gained:** readable output on narrow terminals and stable non-TTY output.

**Current State:**
- Sequential vertical output
- Diffs shown line-by-line
- No side-by-side comparison

**Proposed Enhancement:**
Use rich `Columns` and `Table` for side-by-side display:

```python
from rich.columns import Columns
from rich.panel import Panel

# Before/after comparison
before_panel = Panel(
    "items: 10\njs: 5\ntranslations: 3",
    title="[yellow]Local (Excel)[/]",
    border_style="yellow"
)

after_panel = Panel(
    "items: 12\njs: 5\ntranslations: 4",
    title="[green]Remote (Qualtrics)[/]",
    border_style="green"
)

console.print(Columns([before_panel, after_panel]))
```

**Use Cases:**
1. Drift comparison (local vs remote)
2. Conflict resolution (dimension A vs dimension B)
3. Before/after preview (current state vs pending changes)
4. Multi-survey status (side-by-side survey info)

**Files to Modify:**
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/drift_check.py` (drift display)
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/sync_orchestrator.py` (conflict display)

**Acceptance Criteria:**
- [x] Drift check shows side-by-side local/remote
- [x] Conflict resolution shows side-by-side dimension states
- [x] Preview shows before/after in columns (items workbook diffs: optional panels; unified diffs remain default)
- [x] Layouts adapt to narrow terminals (Rich auto-stacks when width is insufficient)

**Definition of Done:**
- Side-by-side display for all comparison operations
- Auto-stacking on narrow terminals
- Color-coded panels (yellow=local, green=remote, red=conflict)
- Non-TTY mode shows sequential output

---

## Element 8: Real TUI Mode (Textual, Optional Extra)

**Task ID:** `QSYNC-UX-017` *(Extension — but approved for planning; implementation gated below)*

**Motivation:** Slackdump’s biggest UX win is a **real TUI**: multi-pane layouts, live previews, and keybinding overlays. qsync’s current stack (questionary + Rich) can’t deliver the screenshot-style experience without brittle prompt_toolkit hacks.

**Approval gates (implementation):**
- [x] **Approve adopting Textual as the real TUI foundation (installed via `qsync[tui]`)**
- [x] **Approve adding a `qsync tui` entrypoint** (recommended: separate app; lowest risk to existing CLI)
- [ ] **Approve adding `--tui/--no-tui/--tui=auto` to `qsync sync`** (optional: conservative auto-selection in interactive TTY only)
- [ ] **Approve migrating “live preview panel” and “`?` overlay help” work into Textual screens** (Element 3 Stage 2 + Element 4 Stage 3)

**Potential value lost by moving UI work into Textual:** adds a dependency (optional extra) and a second UI layer to maintain.  
**Value gained:** unlocks true Slackdump-style UX (live two-pane, overlays, better input widgets) while keeping the existing CLI stable and script-friendly.

### Packaging & Install
- Install TUI mode via optional extra: `pip install qsync[tui]`
- Base install remains unchanged.
- If the user runs the TUI without the extra installed, show a one-line install hint and exit non-zero (no stack traces).

### Proposed Architecture (keep maintenance low)
- Keep **business logic** in existing modules (`sync_orchestrator`, `survey_inventory`, `config`).
- Add a thin **TUI layer** that:
  - orchestrates the flow + navigation stack (**← Back**)
  - renders state in panes (left menu / right context)
  - provides a real help overlay (`?`) and inline validation
- Avoid duplicating network calls: the TUI should request data from existing functions (or small shared “query” helpers), not re-implement API access.

### Scope (pilot; minimal but real)
1. **Sync Wizard Screen (pilot)**
   - survey selection + dimension selection + confirmation
   - right pane shows live context (selection summary, warnings, what will happen)
   - back stack between steps; cancel returns cleanly
2. **Config / Login Screen (follow-up)**
   - edit config with masked secrets + validation
   - show source of each value (env vs `.env`)
   - back up `.env` before write; confirm before saving

**Files created (agentx/ux):**
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/tui/__init__.py` (TUI package)
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/tui/app.py` (Textual app)

**Files to Modify (when approved):**
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/cli.py` (register `tui` command and/or `--tui` flag)
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/pyproject.toml` (add optional dependency extra `tui = ["textual>=..."]`)

**Acceptance Criteria:**
- [x] `pip install qsync[tui]` enables the TUI without affecting base installs
- [x] `qsync tui` launches a Textual app in interactive TTY mode
- [ ] The TUI implements:
  - [ ] **← Back** where meaningful (not just Cancel)
  - [x] live-updating right pane context while navigating
  - [ ] `?` help overlay (or a Help screen) with shortcuts and workflow hints
- [ ] Non-TTY / `--json` / `--yes` never starts the TUI (always CLI behavior)
- [x] Without the `tui` extra installed, `qsync tui` prints an install hint and exits non-zero

**Definition of Done (pilot):**
- One end-to-end “happy path” is usable (sync selection → confirm), even if execution delegates back to existing commands for now.
- Manual smoke test passes in macOS Terminal/iTerm2 and in a narrow terminal.
- Documentation includes `qsync tui --help` and a short “Install TUI” section.

---

## Element 9: Improved Keyboard Shortcuts

**Task ID:** `QSYNC-UX-018`

**Motivation:** Better keyboard navigation and shortcuts like slackdump.

**Approval gate:**
- [x] **Approve revised Element 9 approach (cancel semantics first; extra keybinds optional)**  
  *(This changes the original “add lots of shortcuts” into a staged approach aligned with questionary constraints.)*

**Potential value lost if we change the approach:** we may not ship Escape/Home/End/PageUp/PageDown reliably in questionary across terminals.  
**Value gained:** cancellation becomes consistent and safe (no stack traces), which is the highest-impact UX improvement here.

**Current State:**
- Basic: ↑↓ arrows, Enter, Ctrl+C
- No additional shortcuts
- No customization

**Proposed Enhancement (revised):**
### Stage 1 — Cancellation + consistency
- Ctrl+C always yields a clean “Cancelled” path (no stack traces; consistent return behavior).
- Fallback selection (`_fallback_select`) matches questionary semantics for cancel/back.

### Stage 2 — Extra keybinds (only if reliably supported)
- Escape as cancel (if supported consistently).
- Home/End/PageUp/PageDown if supported.
- Document type-ahead filtering for autocomplete menus (already present).

**Files to Modify:**
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/interactive_menu.py` (improve interrupt handling)

**Acceptance Criteria:**
- [x] Ctrl+C shows "Cancelled" instead of stack trace
- [ ] Escape key works as alternative cancel (only if stable across terminals)
- [ ] Keyboard shortcuts documented in help
- [ ] Type-ahead filtering promoted in autocomplete menus

**Definition of Done:**
- Stage 1 done:
  - Clean exit messages for all interrupts
  - No Python stack traces in normal operation
  - Help text mentions cancel/navigation
- Stage 2 done (optional):
  - Extra keybinds implemented and documented (only where stable)

---

## Element 10: Operation Timing Display

**Task ID:** `QSYNC-UX-019`

**Motivation:** Show operation duration for transparency (mentioned in roadmap line 66: "Enhanced progress indicators with timing").

**Current State:**
- No timing information
- Operations complete silently or with simple "Done" message

**Proposed Enhancement:**
Add timing to all operations:

```python
import time
from rich.console import Console

console = Console()

start = time.time()
with console.status("[cyan]Syncing survey...", spinner="dots"):
    result = sync_survey(...)
elapsed = time.time() - start

console.print(f"[green]✓[/] Sync completed in {elapsed:.1f}s")
```

**Files to Modify:**
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/sync_orchestrator.py` (sync operations)
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/survey_inventory.py` (inventory refresh)
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/cli.py` (command-level timing)

**Acceptance Criteria:**
- [x] All operations > 1s show timing
- [x] Format: "Completed in X.Xs" or "Completed in Xm Ys"
- [x] Timing respects `--color never`
- [x] Machine-readable output (JSON) omits timing messages

**Definition of Done:**
- [x] All major operations show completion time
- [x] Sub-second operations shown as "< 1s"
- [x] Long operations shown as "2m 34s" format
- [x] Timing included in success/completion messages

---

## Element 11: Survey Selection Harmonization (Regex + Command-Aware Sources)

**Task ID:** `QSYNC-UX-020`

**Motivation:** Survey picking is currently inconsistent across commands. Depending on the entrypoint, users see focal-only lists, all surveys, API lists, substring filters, or manual SurveyID prompts. This creates friction and makes it harder to form a mental model.

### Implementation decisions (confirm before coding)
- [ ] **Default survey list source is command-aware** (Recommended)
Reason: remote-only commands should be fresh and not depend on local inventory; local-surface commands should be inventory-aligned.
- [ ] Default survey list source is always inventory (offline-first)
Reason: simpler, but stale and may mislead for remote-only tasks.
- [ ] Default survey list source is always API (`qsync survey list`)
Reason: fresh, but slower and may be noisy for local-surface workflows.

- [ ] **Manual entry uses regex-first search with direct SurveyID fallback** (Recommended)
Behavior: user types either `SV_...` or `/regex/` or `regex:` or a plain string; show matches; allow choose or refine.
- [ ] Manual entry is direct SurveyID only
Reason: simplest, but does not help discovery.
- [ ] Manual entry is substring filter only
Reason: easy, but less precise than regex and harder for large lists.

- [ ] **Scope selection is explicit: “Focal surveys” vs “All surveys”** (Recommended)
- [ ] Scope selection is implicit (focal-first list with “show all” action)

- [ ] Multi-select is supported for survey selection where a command can operate on many surveys (Recommended)
Example: “prepare”, “batch sync focal”, “inventory maintenance”.
- [ ] Multi-select remains limited to specific existing flows only

### Stages

#### Stage 1 (MVP): One canonical picker API, used from both inventory and API lists
- [ ] Create a single `pick_survey_id(...)` facade that can:
  - accept record lists from inventory
  - accept record lists from API (`qsync survey list` equivalent)
  - apply regex search/filter
  - render consistent menu actions: details, manual entry, back/cancel
- [ ] Add “manual entry” semantics:
  - direct SurveyID input accepted (`SV_...`)
  - regex search accepted (`/pattern/` or `regex:pattern`)
  - invalid regex produces a friendly error and returns to the same picker
- [ ] DoD:
  - Survey selection UI looks and behaves the same from `qsync sync`, `qsync survey menu`, and inventory-based commands (only the underlying data source differs).
  - Non-interactive runs never prompt; they require `--survey-id`.

#### Stage 2 (Improvements): Consistency across command families + caching + performance
- [ ] Decide per command whether default source should be inventory or API.
- [ ] Add lightweight API list caching for one session (avoid repeated `/surveys` calls).
- [ ] Add “top N most recent” quick choice when list is large.
- [ ] DoD:
  - No command uses ad-hoc survey selection logic; all go through the shared picker.
  - Large lists remain usable (filter required, or top-N option).

#### Stage 3 (Extensions): Multi-select survey picking + batch flows
- [ ] Provide a multi-select survey picker where applicable.
- [ ] Ensure disabled reasons (locked/no-API-edit) render consistently for both single- and multi-select; inactive is never a disabled reason.
- [ ] DoD:
  - Batch commands can use a consistent multi-select UI in interactive mode.

### Command taxonomy (how to choose list source)
Remote-only commands (default to API list):
- Examples: survey lifecycle/versioning, response export triggers, remote metadata operations.
Local-surface commands (default to inventory/local-first):
- Examples: sync, items/js/translations workflows, anything that reads/writes workbooks, pending, caches, local files.

### Acceptance Criteria
- [ ] Regex filter supported in the picker and is discoverable from the prompt text.
- [ ] The same “View details / Manual entry / Back” actions exist everywhere survey selection is used.
- [ ] Survey list source is appropriate for the command category (remote-only vs local-surface).
- [ ] Non-interactive behavior is deterministic (`--survey-id` required).

---

## Element 12: Survey Menu UX Structure Audit + Non-Destructive Improvements

**Task ID:** `QSYNC-UX-021`

**Motivation:** `qsync survey menu` is useful and already includes account switching. We should keep that, but improve consistency and reduce duplication with the general survey picker.

### Requirements
- Keep account switching in `qsync survey menu` intact (explicit non-goal: do not remove it).
- Use the shared survey picker UI inside the survey menu where possible (normalize API list into record shape).
- Visually disable actions that are not allowed in the current account context (instead of allowing selection then rejecting).

### Work items
- [ ] Replace the survey menu’s internal “pick survey” logic with the shared picker (API list normalized to records).
- [ ] Add a small “Account context” header panel (TTY only) showing:
  - selected account label
  - resolved base URL
  - token present (boolean only)
- [ ] For default-account-only actions, render menu entries disabled with a reason when a partner account is selected.

### Acceptance Criteria
- [ ] Survey menu uses the same survey selection behavior as elsewhere (filter, details, regex/manual entry).
- [ ] No surprise post-selection rejections for account-scoped restrictions; invalid actions are disabled in the menu.

---

## Element 13: Interactive Settings and Configuration Surfaces (Ideas + Optional Implementation)

**Task ID:** `QSYNC-UX-022`

**Motivation:** Users want a single “control panel” for qsync configuration and workspace health (accounts, preferences, inventory, focal set, diagnostics). This should complement `qsync survey menu`, not replace it.

### Implementation decisions (confirm before coding)
- [ ] Add `qsync settings` (CLI menus) that works without TUI extras (Recommended)
- [ ] Add `qsync settings` as Textual-only (requires `qsync[tui]`)
- [ ] Add both: CLI menus now, TUI later as an extension

### Candidate interactive settings (ideas)
- Active account:
  - set workspace default account (`qsync account use`)
  - show account status (resolved base URL, token present, env file path)
  - clear workspace account preference
- Workspace preferences:
  - show `.qsync/preferences.json`
  - edit preferences with safe prompts (no secrets)
- Inventory:
  - refresh inventory
  - show last refresh metadata
  - manage focal surveys (multi-select)
- Diagnostics:
  - run `qsync doctor` checks
  - show “fix it” commands in a panel
- Output/UX toggles (safe, non-secret):
  - set default color mode (auto/never) for the workspace
  - set default rich mode (auto/off) for the workspace

### Constraints
- Must not log secrets.
- Must degrade cleanly in non-TTY and JSON mode (should refuse to run interactively there).

### Stage 1 (MVP): Add a simple CLI-driven settings menu
- [ ] Provide a single menu entrypoint that links to the above categories.
- [ ] Use the shared menu model everywhere (disabled items, separators, back/cancel).
- [ ] DoD:
  - A user can discover and set the workspace default account without editing env vars manually.
  - A user can refresh inventory and manage focal surveys from one place.

---

## Element 14: Move `qsync survey menu` into the Textual TUI (Parity + Preserve Account Switching)

**Task ID:** `QSYNC-UX-023`

**Motivation:** The existing `qsync survey menu` is the natural “control panel” for survey operations (and already supports account switching). A real two-pane Textual UI is a better home for this than nested questionary prompts, especially as we add structural editors and richer previews.

### Requirements (locked in by user)
- [x] `qsync survey menu` remains the primary entry point for survey-level operations.
- [x] The TUI is an optional entry from the survey menu (not just `qsync sync`).
- [x] Account switching stays in the CLI survey menu for now; do not re-add it to the TUI yet (defer to future package-wide account UX).
- [x] TUI is reachable from the survey menu but does not deprecate the conventional (current) `qsync survey menu` path.
- [x] The TUI must not leak secrets (never print tokens; only safe booleans/status).
- [x] Non-TTY and JSON mode must not start the TUI (fail fast with a clear message).

### Implementation decisions (confirm before coding)
- [ ] Make `qsync survey menu` launch the TUI when installed, else fall back to CLI menu
Pros: simple mental model, “always the nice menu”.
Cons: behavior change may surprise users relying on the current CLI menu.
- [x] **Add `qsync survey menu --tui` and keep default as current CLI menu** (Recommended, per user)
Pros: opt-in, low-risk rollout, easy rollback.
Cons: users must discover the flag.
- [ ] Add a new command `qsync tui --survey-menu` only (leave `qsync survey menu` unchanged)
Pros: zero behavior change.
Cons: splits discovery; users keep using the old menu by habit.

### Stage 1 (MVP): TUI survey menu skeleton + account switching + basic operations
- [x] Add a TUI survey menu screen reachable via `qsync survey menu --tui`.
- [x] Add a TUI survey picker sufficient to select a survey for a workflow (inventory-backed or API-backed per action).
- [ ] Mirror additional top-level categories from the CLI survey menu.
- [ ] Add a TUI “account context” pane (display-only).
- [x] Add a display-only account context panel (base URL + token-present; no secrets).
- [ ] Integrate the shared picker semantics (filter/details/regex/manual) into the TUI picker (full parity).
- [ ] DoD:
  - [x] A user can navigate categories and run at least 3 existing survey-menu actions from the TUI (pull, inventory refresh, items structural edits).
  - (Deferred) Account switching works in the TUI and affects API-backed list operations immediately.

### Stage 2 (Improvements): Bring over the rest of survey-menu actions + disabled reasons
- [ ] Parity for all current survey-menu submenus.
- [ ] Disabled actions render with reasons (for default-account-only or safety constraints).
- [ ] DoD:
  - No post-selection “surprise” denial when an action is invalid in current account context; it is disabled upfront.

### Stage 3 (Extensions): Optional `?` overlay help per screen + richer previews
- [ ] Help overlays per screen (keybinds + workflow hints).
- [ ] Rich diff previews in-pane where safe.

---

## Element 16: Global `qsync tui` as an Optional Interactive Dashboard (Discovery-first)

**Task ID:** `QSYNC-UX-025`

**Motivation:** Provide a single, optional interactive dashboard for qsync that improves discoverability and reduces “remember the command” friction, while keeping the CLI as the stable/scriptable interface.

### Requirements (locked in by user)
- [x] TUI can cover all major operations but does not have to be used.
- [x] CLI commands remain the source of truth and must not be deprecated.
- [x] No secrets in output (never print tokens).
- [x] Works as “launcher” first: screens may call existing CLI workflows under `App.suspend()` to avoid logic duplication.

### Proposed global menu tree (preliminary; log for future selection)

Home
- Continue last session (pending exists)
- Jump to… (type-to-filter)
- Recent surveys

Workbench (edit + sync pipeline)
- Prepare workspace (pull + hydrate)
- Detect changes (choose surfaces)
- Preview diffs
- Stage changes
- Push (with safeguards)
- Publish / post-push
- Pending queue
- Conflicts / drift resolution

Survey Library (browse + select + inspect)
- Browse surveys (API/inventory)
- Survey summary
- Inspect (flow/questions/versions)
- Inventory (refresh, manage focal)

Content Editors (interactive “surgical” changes)
- Items structural editor (SBS-first) (stage → preview → push)
- Embedded Data editor (SurveyFlow) (stage to pending where supported)
- (Future) Flow/routing editors

Remote Admin (account-side operations)
- Pull survey definition (cache JSON)
- Activate/deactivate
- Copy/derive
- Versions (list/fetch/rollback)

Exports
- Export responses
- Export translation review docs
- Export QSF

Accounts & Workspace
- Active account (workspace default)
- Diagnostics
- Preferences (safe toggles)
- Doctor/checks

Help
- Keybindings
- Pipeline mental model
- Troubleshooting
- About (version/paths)

### Stage 1 (MVP): Implement only “Content Editors” first (P0)
- [ ] Add a main-menu entry: “Content editors”.
- [ ] Include at least:
  - [ ] Items structural editor (SBS-first) (reuse existing structural session screens).
  - [ ] Embedded Data editor (add/remove/rename staged embedded fields).
- [ ] DoD:
  - A user can reach SBS structural editing from `qsync tui` without using CLI menus.
  - The workflow remains stage → preview → push and keeps workbook patch offer.

---

## Element 15: Items Structural Edits as a First-Class Survey Menu Workflow (SBS-first)

**Task ID:** `QSYNC-UX-024`

**Motivation:** Users need a supported workflow for structural edits that Excel cannot represent well (add/remove/edit choice options, answers/subitems, and SBS-specific structures). This should integrate cleanly into the standard qsync pipeline: stage → preview → push (or revert/abort).

### Requirements (log these before implementation)

User-specified requirements:
- [x] Reachable via `qsync survey menu` (not via `qsync sync`) and therefore also reachable via the TUI survey menu.
- [x] Follows the same “QID dimension classification” model as the workbook (Options/Subitems/QuestionText, plus SBS-specific surfaces).
- [x] Fully covers SBS QIDs (SBSMatrix: statements + columns + per-column answers).
- [x] After an edit is locked in, it is staged (aligned with normal qsync pending pipeline).
- [x] After staging: the flow supports additional edits before finalizing (CLI prompt loop; TUI via explicit “Add another edit” action).
- [x] Diff preview of staged vs cache is available before pushing.
- [x] End-of-session choices exist: push now / revert (clear staged) / abort (leave staged).
- [x] After push: offer to patch affected workbook cells immediately (dry-run first, then confirm).
- [x] Before editing a QID: detect workbook unstaged edits (scoped QID diff).
- [x] If workbook QID diff exists: warn user and offer “sync cycle first” vs “overwrite workbook diff”.

Additional safety requirements (recommended):
- [x] Refuse structural edits on externally managed QIDs (respect existing owner checks).
- [x] Enforce push safeguards at push time and keep delete confirmation strict (no delete ops by default; confirmations remain strict).
- [ ] Provide a dry-run mode for structural edits that stages nothing (prints intended ops and exits).
- [x] Journal/resume support for structural pushes (persist `push_journal` in pending and pass through to push).
- [x] Persistent “structural edit session summary” panel exists in the TUI right pane (Git-style staging panel).
- [ ] Provide “export ops to JSON” and “apply ops from JSON” for non-interactive repeatability and review.
- [ ] Preview defaults to affected surfaces only (per-QID), with an option to expand to a broader diff.
- [x] Workbook patching is dry-run first: show exactly which cells would be overwritten; user confirms before write.
- [x] Workbook drift resolution default is “sync-first” with “overwrite workbook” requiring an explicit confirmation.

### QID dimension classification (align with workbook)
Define one canonical mapping used by both UI and structural ops:
- `question_text` (QuestionText)
- `choices` (Choices/Options)
- `answers` (Subitems/Answers)
- `choice_groups` (if supported by workbook/model)
- `embedded_data` (out of scope for structural editor unless explicitly added later)
- `sbs_columns` (SBS columns)
- `sbs_column_answers` (SBS column answers)

Note: current structural ops cover `question_text`, `choices`, and `answers` (with `subitems` mapped to `answers`). SBS support is a planned extension and may require additional parsing + API update rules.

SBS implementation structure (already present in the workbook pipeline; structural editor should reuse it):
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/excel_io.py` defines SBS sheets (`SBS_Columns`, `SBS_ColumnAnswers`), row types, loaders, and HTML conversion helpers.
- `/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/dimensions/items_core.py` already:
  - previews SBS column and column-answer diffs as `kind="sbs_column"` / `kind="sbs_column_answer"`
  - applies those changes into `AdditionalQuestions[*]` payload
  - treats Matrix and SBSMatrix “subitems” as edits to `Choices` (statements/rows), not `Answers`

Implication for this editor:
- “Subitems” must be mapped by question type: Matrix/SBSMatrix subitems are `Choices` (statements), not `Answers`.
- SBSMatrix columns and per-column answers must be first-class structural targets (not shoehorned into regular choices/answers).

### Appendix: Surface mapping table (single source of truth)
This table defines how the editor should label surfaces (workbook terminology) and which JSON container is mutated.

- Non-matrix questions:
  - Question text: `QuestionText`
  - Options: `Choices[*].Display`
  - Subitems: `Answers[*].Display` (when `Answers` exists)
- Matrix questions (`QuestionType="Matrix"`):
  - Options (columns): `Answers[*].Display`
  - Subitems (rows/statements): `Choices[*].Display`
- SBSMatrix questions (`QuestionType="SBS"`, `Selector="SBSMatrix"`):
  - Subitems (rows/statements): `Choices[*].Display` (and any duplicated statements under `AdditionalQuestions[*].Choices` must be kept consistent)
  - SBS columns: `AdditionalQuestions[ColumnId].QuestionText`
  - SBS column answers: `AdditionalQuestions[ColumnId].Answers[AnswerId].Display`

### Implementation decisions (confirm before coding)
- [ ] **Stage structural ops into the existing pending record for `items`** (Recommended)
Rationale: keeps pipeline consistent and reuses push machinery.
- [ ] Store structural ops in a separate pending record (new dimension)
Rationale: clearer isolation, but increases surface area and requires new push coordination.

- [ ] Workbook patching after push is opt-in per session (Recommended)
- [ ] Workbook patching after push is always-on

- [ ] SBS support implemented by translating SBS edits into standard choice/answer mutations
Risk: may be incorrect for SBS matrix models.
- [ ] **SBS support implemented as first-class structural targets** `sbs_columns` and `sbs_column_answers` (Recommended)
Rationale: makes intent explicit and reduces accidental corruption.

### Stage 1 (MVP): CLI survey-menu workflow for non-SBS QIDs (question_text/choices/answers)
- [x] Add survey-menu entry: “Items: structural edits (stage → preview → push)”.
- [ ] Survey selection uses shared picker semantics.
- [ ] QID selection offers:
  - browse active-in-flow
  - search by tag/text
  - filter by ExportTag
  - show all questions (includes not-in-flow)
- [ ] Before editing a QID: run “QID workbook drift check” and present the two choices (sync first vs overwrite).
- [ ] After each locked edit: stage op and show a one-line summary (op, qid, id).
- [ ] End-of-session gate: preview staged vs cache, then push/revert/abort.
- [ ] After push: offer workbook patch for affected cells.
- [ ] Session summary visible throughout the flow (CLI: short summary after each op; TUI: persistent right pane panel).
- [ ] DoD:
  - A user can add/edit/remove a choice option and an answer/subitem via survey menu.
  - The result is staged and can be pushed or reverted without leaving the menu.
  - Non-interactive edit remains available via `qsync items edit --action ... --qid ...` (no regression).

### Stage 2 (SBS-first): Full SBS QID coverage
- [x] Structural editor detects SBSMatrix reliably (`QuestionType=SBS`, `Selector=SBSMatrix`).
- [x] SBS targets are available (columns + per-column answers + statements mapping).
- [x] SBS staging ops are implemented and integrated into the same stage → preview → push gate.
- [ ] DoD:
  - SBS QIDs are editable through the structural editor with the same staging/push workflow.
  - Preview shows what will change for SBS columns and column answers before pushing.

### Stage 3 (Improvements): Batch edits + multi-select + richer preview
- [ ] Multi-select QIDs for repeated edits (optional; can still be one-by-one).
- [ ] “Edit another?” loop with a queue (add multiple ops before preview).
- [ ] Rich side-by-side “before/after” preview for the targeted QID only (avoid giant survey diffs).

---

## Implementation Priority & Sequencing

**Current implementation status (agentx/ux):**
- Element 2 (menu model + fallback consistency): implemented
- Element 3 (on-demand “View details”): implemented for sync dimension selection
- Element 4 (help footer + `qsync help <topic>`): implemented
- Element 6 (validators wired into onboarding + survey prompts): implemented
- Element 8 (`qsync[tui]` extra + `qsync tui` pilot): implemented (pilot)
- Element 9 (Stage 1 cancel semantics): implemented

### High Value / Low Effort (Start Here)
1. **Element 2: Dynamic Menu Validation** - Menu model + fallback consistency (foundation for Elements 3/4/9)
2. **Element 10: Operation Timing** - Simple, high visibility
3. **Element 9: Keyboard Shortcuts** - Cancellation semantics + consistency (Ctrl+C + fallback behavior)
4. **Element 1: Progress Indicators** - Add via centralized `terminal_output` helpers (TTY-only; safe fallbacks)

### Medium Value / Medium Effort
5. **Element 5: Enhanced Visual Hierarchy** - Style guide + selective Rich adoption (keep it centralized)
6. **Element 3: Preview Panels** - Better context for decisions (on-demand actions)

### Lower Priority
7. **Element 4: Help System** - Good for discoverability (much easier once menus are structured)
8. **Element 6: Real-time Validation** - Nice-to-have, questionary validators
9. **Element 7: Multi-Column Layouts** - Specific use cases

### Extensions (Require Approval)
10. **Element 8: Real TUI Mode (Textual, optional extra)** - Only after Element 2 is shipped and at least one “on-demand details” flow exists; keep behind `qsync[tui]` and explicit opt-in

---

## Cross-Cutting Concerns

### Backward Compatibility
All enhancements must:
- Respect `--color never` flag (terminal_colors.py)
- Respect `QSYNC_USE_QUESTIONARY=0` (interactive_menu.py)
- Work in non-TTY environments (CI/CD)
- Support `NO_COLOR` environment variable
- Not break existing `--yes` / `--non-interactive` modes

### Testing Strategy
For each element:
- Manual testing in interactive mode
- Test with `QSYNC_USE_QUESTIONARY=0` (fallback mode)
- Test with `--color never` (no ANSI)
- Test in non-TTY (pipe to file, CI simulation)
- Test with narrow terminal (< 80 cols)

### Documentation Requirements
Each element needs:
- Update to relevant command help text
- Screenshot/demo for visual features
- Environment variable documentation
- Troubleshooting section (if needed)

---

## Verification Checklist

After implementing any element:
- [ ] Works in interactive TTY mode
- [ ] Works in fallback mode (QSYNC_USE_QUESTIONARY=0)
- [ ] Works with `--color never`
- [ ] Works in non-TTY (redirected output)
- [ ] Works with existing automation flags (`--yes`, `--non-interactive`)
- [ ] Respects `NO_COLOR` environment variable
- [ ] Terminal state properly saved/restored
- [ ] No Python stack traces in normal operation
- [ ] Help text updated
- [ ] Manual testing completed

---

## Dependencies & Constraints

### Current Dependencies (Available)
- questionary >= 2.0.0 (already in use)
- rich >= 13.0.0 (already in use, underutilized)

### Optional Dependencies (Shipped As Extras)
- textual (full TUI framework - for Element 8)
  - Install: `pip install qsync[tui]`
  - Decision: adopt Textual for real TUI work, but keep it optional so the base install remains stable and lightweight

### Environment Variables (Existing)
- `QSYNC_USE_QUESTIONARY` - Controls questionary usage
- `NO_COLOR` - Disables all colors
- `QSYNC_LOG_DISABLED` - Disables logging

### Environment Variables (Proposed New)
- `QSYNC_USE_RICH` *(optional kill switch only if needed)* - Control rich usage (default: auto)
- Values: `0/false/no` (disable), `1/true/yes` (enable), `auto` (TTY-based). Prefer implicit gating via TTY + `--color never` + `NO_COLOR` so we don't need another knob in normal operation.
- `QSYNC_USE_TUI` - Control Textual TUI usage (default: auto)
- Values: `0/false/no` (disable), `1/true/yes` (enable), `auto` (only when interactive TTY and `qsync[tui]` is installed)

---

## References

### Slackdump Architecture
- Charmbracelet stack: Bubble Tea + Bubbles + Lip Gloss + Huh
- Full TUI with Model-Update-View pattern
- Dynamic validation: `Validate()` called per render
- Nested models: Each menu item can have sub-model
- See: [dev/next/cli_ux/slackdump_inspo_1.md](/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/dev/next/cli_ux/slackdump_inspo_1.md)

### Python Equivalents
- **Textual**: Full TUI framework (Bubble Tea equivalent)
  - Comparison with Bubble Tea: https://textual.textualize.io/
- **Rich**: Terminal UI library (already in use)
  - Progress: https://rich.readthedocs.io/en/stable/progress.html
  - Panels: https://rich.readthedocs.io/en/stable/panel.html
  - Tables: https://rich.readthedocs.io/en/stable/tables.html

### Qsync Files
- Interactive menu: [src/qsync/interactive_menu.py](/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/interactive_menu.py)
- Terminal output: [src/qsync/terminal_output.py](/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/terminal_output.py)
- Sync orchestrator: [src/qsync/sync_orchestrator.py](/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/sync_orchestrator.py)
- Onboarding: [src/qsync/onboarding.py](/Users/pm/Work/VUPD/projects/NEWSFLOWS/qsync/src/qsync/onboarding.py)

---

## Next Steps

1. **Review this plan** - Confirm priorities and approach
2. **Choose starting element** - Recommend starting with Element 2 (menu model + fallback consistency)
   - If the goal is visible quick wins after that: Element 10 (timing) then Element 9 (cancel semantics), then Element 1 (progress)
   - If the goal is Slackdump-like parity: treat Element 8 (Textual pilot) as a later, explicit scope expansion behind `qsync[tui]`
3. **Implement incrementally** - One element at a time, test thoroughly
4. **Gather feedback** - User testing after each element
5. **Iterate** - Adjust based on real-world usage

---

## Notes

- **Plan format:** This plan follows dev/AGENTS.md conventions for independent elements rather than linear MVP→Improvements→Extensions
- **Location:** This plan intentionally lives under `dev/` (git-ignored). Move it to tracked docs only if we decide it should ship with the repo.
- **Task IDs:** QSYNC-UX-010 through QSYNC-UX-019 (sequential from existing QSYNC-UX-003)
- **Approval required:** Element 8 (Real TUI Mode) is marked as extension requiring explicit approval for implementation steps
