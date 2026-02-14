# Survey Master Workflow Guide

_Migrated from `appendices/survey_master_workflow.md` (monorepo) so the standalone `qsync` repo can be self-contained._

> **Status:** Grammar-aligned implementation guide
> **Updated:** 2026-02-11
> **Related:** [Survey Master Field Reference](../reference/survey-master-fields.md)

## Overview

The **Survey Master** system allows you to bulk-edit survey metadata, options, and status across multiple focal surveys using a CSV-based workflow. It combines safety guardrails with flexibility for power users.

### The Standard Workflow

```
PULL → EDIT CSV → PREVIEW → STAGE → PUSH
```

1. **PULL**: Download survey definitions into CSV + snapshots (non-destructive merge preserves edits)
2. **EDIT**: Modify field values in the CSV file
3. **PREVIEW**: See what would change before staging
4. **STAGE**: Validate and write changes to pending (no API writes)
5. **PUSH**: Apply staged changes to Qualtrics API + publish (with safeguards)

---

## Stage 1: Pull Survey Data

### Command
```bash
qsync survey master pull
```

### What It Does
- Fetches all focal surveys from Qualtrics
- Downloads 4 data sources per survey:
  - **Status**: `GET /surveys/{surveyId}` (survey activation, response counts, etc.)
  - **Metadata**: `GET /survey-definitions/{surveyId}/metadata` (name, description, branding)
  - **Options**: `GET /survey-definitions/{surveyId}/options` (button labels, styling, etc.)
  - **Versions**: `GET /survey-definitions/{surveyId}/versions` (latest published version info)
- Saves **snapshots** to `surveys/qualtrics_master_snapshots/{survey_id}.json`
- Generates **master CSV** at `surveys/qualtrics_master.csv` with:
  - 78 columns (schema-driven from `surveys/qualtrics_api_key_mapping.csv` or packaged defaults)
  - One row per focal survey
  - Both read-only and editable fields

### Pull-Specific Flags

#### `--survey-id` (optional, repeatable)
```bash
qsync survey master pull --survey-id SV_abc123 --survey-id SV_def456
```
- Pull only specific surveys instead of all focal surveys
- Useful for testing or when only certain surveys have changed
- Can be repeated multiple times

#### `--force-overwrite` (optional)
```bash
qsync survey master pull --force-overwrite
```
- **Default behavior**: Pull preserves user edits by merging overrides from existing CSV
- With `--force-overwrite`: Discard existing CSV and generate fresh from snapshots
- Backup of existing CSV is written to `surveys/qualtrics_master.csv.bak` before merge

**Non-Destructive Merge Logic:**
1. Computes overrides: existing CSV - existing snapshots = user edits
2. Generates fresh CSV from newly fetched snapshots
3. Reapplies user edits onto fresh CSV
4. Writes backup of old CSV before merging
5. Result: Baseline refreshed, user edits preserved

### Output
```
[qsync:master-pull] Pulling 5 focal surveys...
[qsync:master-pull]   Fetching SV_abc123...
[qsync:master-pull]     ✓ Snapshot saved
...
[qsync:master-pull] Generating master CSV from 5 snapshots...
[qsync:master-pull] ✓ Master CSV written to surveys/qualtrics_master.csv
[qsync:master-pull] Complete: 5 snapshots, 1 CSV (5 rows)
```

---

## Stage 2: Edit the CSV

### Where to Edit
Open `surveys/qualtrics_master.csv` in a spreadsheet editor:
- **LibreOffice Calc** (recommended for large CSV files)
- **Excel** (works well)
- **Google Sheets** (acceptable, but watch for format conversions)

### What You Can Edit
Only **writable** fields can be edited. Read-only fields (marked in column header or by convention with `_` prefix) are locked.

**Datetime fields:** Use ISO 8601 (e.g., `2026-01-10` or `2026-01-10T14:00:00Z`). In spreadsheets, format the column as plain text to avoid auto-conversion.

#### Writable Field Categories

**Metadata fields** (definition updates):
- `SurveyName`: Survey display name
- `SurveyDescription`: Long-form description
- `SurveyStatus`: Active/Inactive
- `LanguageSettings`: Default language
- `ProjectCategory`: Survey type/category

**Options fields** (styling, behavior):
- `BackButton`: Show "Back" button
- `NextButton`: Show "Next" button
- `RequiredMessage`: Message for required fields
- `CustomStyles.customCSS`: Inline CSS for custom styling
- `RequiredOnlyOnAnswer`: Require only on answered

**Status fields** (activation, counts):
- `isActive`: Whether survey accepts responses
- `_responsesSummary`: Read-only response counts
- `_latestVersion`: Read-only version info

### Dangerous Fields (Requires `--allow-dangerous`)

Six fields require explicit approval to change:
- **`isActive`** — Activates/deactivates survey response collection
- **`SurveyStatus`** — Active/Inactive status (similar to above)
- **`EOSRedirectURL`** — End-of-survey redirect (security-sensitive)
- **`BallotBoxStuffingPreventionURL`** — Prevents duplicate responses
- **`RefererURL`** — Referrer URL for access control
- **`PasswordProtection`** — Requires password to access

**Why dangerous?** These fields directly impact:
- Whether respondents can access the survey
- Where respondents are sent after completion
- Security and access control

**Example:** Setting `isActive=true` on 50 surveys at once could expose surveys to unintended audiences.

### Example Edits

Before:
```csv
SurveyID,SurveyName,SurveyDescription,BackButton,isActive
SV_abc123,Old Name,Old description,true,false
SV_def456,Another Survey,,false,true
```

After:
```csv
SurveyID,SurveyName,SurveyDescription,BackButton,isActive
SV_abc123,New Name Updated,Revised description,false,false
SV_def456,Another Survey Updated,New description,false,true
```

---

## Stage 3: Preview Changes

### Command
```bash
qsync survey master preview
```

### What It Does
- Loads the edited CSV
- Compares each field to the saved snapshot (baseline)
- Counts total changes
- Flags dangerous fields and publishing requirements
- **Does NOT contact Qualtrics** (offline comparison)

### Preview-Specific Flags

#### `--detail`
```bash
qsync survey master preview --detail
```
Shows detailed per-field changes:
```
📋 Detailed Changes:

  📝 SV_abc123 - Old Name Updated:
     ⚠️  [metadata] SurveyName
       'Old Name' → 'New Name Updated'
     ⚠️  [options] BackButton
       'true' → 'false'
```

#### `--survey-id`
```bash
qsync survey master preview --survey-id SV_abc123
```
Preview only one survey (useful for testing):
```

#### `--format`
```bash
qsync survey master preview --format json
```
Emit machine-readable JSON output (useful for scripting or reporting). Default is `text`.
See `../reference/survey-master-json-output.md` for the output schema.

#### `--tag`
```bash
qsync survey master preview --tag component=pre --tag stage=prod
```
Filter surveys by tags stored in `surveys/inventory.csv` (available keys: `component`, `stage`, `cntry`).
See `../reference/inventory-schema.md` for the inventory/tag schema.
[qsync:master-preview] Validating 1 surveys...
[qsync:master-preview] Computing diffs for 1 surveys...
[qsync:master-preview]   SV_abc123: 2 change(s)

📊 Preview Summary:
  Surveys with changes: 1/1
  Total fields to change: 2
  ⚠️  Publishing required: YES (definition changes detected)
  ⚠️  Dangerous changes: NO
```

### Typical Output
```
[qsync:master-preview] Validating 5 surveys...
[qsync:master-preview] Computing diffs for 5 surveys...
[qsync:master-preview]   SV_abc123: 2 change(s)
[qsync:master-preview]   SV_def456: 1 change(s)

📊 Preview Summary:
  Surveys with changes: 2/5
  Total fields to change: 3
  ⚠️  Publishing required: YES (definition changes detected)
  ⚠️  Dangerous changes: NO

💡 Next: Run 'qsync survey master stage' then 'qsync survey master push'
```

---

## Stage 4: Stage Changes (Validate + Write Pending)

### Command
```bash
qsync survey master stage
```

### What It Does
1. Validates CSV schema
2. Computes diffs for each survey (CSV vs snapshots)
3. Creates pending records at `surveys/pending/master/{survey_id}.json`
4. Computes snapshot hash for drift detection
5. **NO API writes** - staging is purely local

### Why Stage?
- **Safety**: Validate before writing to API
- **Review**: Inspect pending records before push
- **Workflow alignment**: Matches items/translations/js dimensions
- **Drift detection**: Snapshot hash enables detection of baseline changes

### Stage-Specific Flags

#### `--survey-id` (optional)
```bash
qsync survey master stage --survey-id SV_abc123
```
Stage only this survey.

#### `--tag` (optional, repeatable)
```bash
qsync survey master stage --tag component=pre --tag stage=prod
```
Filter surveys by tag before staging.

### Output
```
[qsync:master-stage] Validating 5 surveys...
[qsync:master-stage] Computing diffs for 5 surveys...
[qsync:master-stage]   SV_abc123: 3 change(s)
[qsync:master-stage] Saving pending records for 2 survey(s)...
[qsync:master-stage]   Staged: surveys/pending/master/SV_abc123.json
[qsync:master-stage] ✅ Staged 2 survey(s) with 5 change(s)

💡 Next: Run 'qsync survey master push'
```

---

## Stage 5: Push Changes (API Write + Publish)

### Command
```bash
qsync survey master push
```

### What It Does (In Order)
1. Loads pending records from `surveys/pending/master/`
2. For each survey with staged changes:
   - Validates snapshot hash (drift detection) → **REFUSE** if changed
   - Checks for dangerous field changes → **REFUSE** unless `--allow-dangerous`
   - Enforces safeguards (locked surveys, response counts) → **BLOCK** unless override flags
   - Ensures backup via `ensure_backup()`
   - Captures pre-apply rollback snapshot
   - Groups changes by endpoint (metadata, options, status)
   - Writes to API in order: metadata → options → status
   - Publishes (unless `--no-publish`)
   - Clears pending on success
3. Returns summary of pushed/published/failed surveys

### Publishing vs Activation (Reminder)
- **Publishing** makes definition changes live (metadata/options). It does **not** toggle whether the survey is active.
- **Activation** is controlled by `isActive` (status endpoint). Changing `isActive` is a separate, explicit action and is treated as a dangerous field.

### Push-Specific Flags

#### `--no-publish` (API write only, skip publish)
```bash
qsync survey master push --no-publish
```
Writes changes to API but skips the publish step.

**Use cases:**
- Testing changes before making them live
- Writing changes to inactive surveys
- When you want to review changes in Qualtrics UI before publishing

#### `--allow-dangerous` (Required for dangerous fields)
```bash
qsync survey master push --allow-dangerous
```
Allows changes to the 6 dangerous fields:
- `isActive` (activation status)
- `EOSRedirectURL` (end-of-survey redirect)
- `BallotBoxStuffingPreventionURL`
- `RefererURL` (referrer URL)
- `PasswordProtection` (password toggle)
- `SurveyStatus` (Active/Inactive)

**Warning:** Use with caution! Setting `isActive=true` on multiple surveys can have major operational impact.

#### `--force-live` (Override live response check)
```bash
qsync survey master push --force-live
```
Allows push even when surveys have live responses (auditable responses > 0).

**Behavior:**
- Prompts for confirmation (unless `--yes`)
- Blocked by default as protection against accidentally modifying live data collection

**When to use:**
- Making non-breaking metadata changes (e.g., updating description)
- Emergency fixes to live surveys
- You've verified the changes won't affect data collection

#### `--force-preview` (Skip preview response warnings)
```bash
qsync survey master push --force-preview
```
Skips warnings about preview/test responses (preview-only responses > 0).

#### `--yes` / `-y` (Skip confirmation prompts)
```bash
qsync survey master push --yes
```
Skips all interactive confirmation prompts. Use for automation/CI.

#### `--allow-locked` (Override survey lock)
```bash
qsync survey master push --allow-locked
```
Allows push to surveys marked as locked in inventory CSV.

**Warning:** Locks exist for a reason - only override if you're certain.

#### `--survey-id` (Push specific survey)
```bash
qsync survey master push --survey-id SV_abc123
```
Pushes only this survey (must have staged changes).

**Recommended workflow:**
1. Edit CSV and stage: `qsync survey master stage`
2. **Test on one survey:** `qsync survey master push --survey-id SV_abc123`
3. Verify changes in Qualtrics UI
4. **Push all:** `qsync survey master push`

### Safeguards (Auto-Enforced)

**Survey Locks:**
- Surveys with `locked=TRUE` in inventory are blocked
- Override: `--allow-locked` (use with caution)

**Response Counts:**
- Live responses (> 0): **BLOCK** unless `--force-live` + confirmation
- Preview responses (> 0): **WARN** unless `--force-preview`
- Unknown counts: **BLOCK** (run `qsync survey inventory` first)

**Drift Detection:**
- Snapshot hash mismatch: **BLOCK** (baseline changed since staging)
- Resolution: Re-run `qsync survey master pull` and `qsync survey master stage`

### Output Example
```
[qsync:master-push] Pushing 2 survey(s) with staged changes...
[qsync:master-push]   SV_abc123: Rollback snapshot saved
[qsync:master-push]   SV_abc123: Writing metadata...
[qsync:master-push]   SV_abc123: ✓ API write successful (3 change(s))
[qsync:master-push]   SV_abc123: Publishing...
[qsync:master-push]   SV_abc123: ✓ Published
[qsync:master-push]   SV_abc123: ✓ Complete
[qsync:master-push] Summary: 2/2 pushed, 2 published, 0 failed

Push Summary:
  Total surveys: 2
  Surveys pushed (API write): 2
  Surveys published: 2
  Surveys failed: 0

✓ Push complete: 2 survey(s) pushed, 2 published

✓ Dry run complete: 1 survey/surveys would be updated
Run without --dry-run to apply changes
```

### Legacy: Direct Apply Path (Optional)

`qsync survey master apply` is kept for compatibility, but it bypasses pending-first workflow.
Prefer `preview -> stage -> push` for regular operations.

#### `--tag` (Filter surveys by tag)
```bash
qsync survey master apply --tag component=pre --tag stage=prod
```
Filter surveys by tags stored in `surveys/inventory.csv` (available keys: `component`, `stage`, `cntry`).

#### Flag Combinations

**Safe testing workflow:**
```bash
# Step 1: Preview all changes
qsync survey master preview --detail

# Step 2: Dry-run on one survey
qsync survey master apply --survey-id SV_abc123 --dry-run

# Step 3: Actually apply to one
qsync survey master apply --survey-id SV_abc123

# Step 4: Once verified, apply to all
qsync survey master apply
```

**Dangerous field with testing:**
```bash
# Preview dangerous changes
qsync survey master preview

# Dry-run first (even though we'll use --allow-dangerous)
qsync survey master apply --allow-dangerous --dry-run

# Actually apply
qsync survey master apply --allow-dangerous
```

**Power user (confident in environment):**
```bash
qsync survey master apply --allow-dangerous --skip-drift
```

### Typical Output

**Success case:**
```
[qsync:master-apply] Processing 2 surveys...
[qsync:master-apply] Checking SV_abc123...
[qsync:master-apply]   ✓ Rollback snapshot saved: 20260205T103015Z-pre-apply.json
[qsync:master-apply]   Writing metadata...
[qsync:master-apply]     ✓ Metadata written

[qsync:master-apply] Checking SV_def456...
[qsync:master-apply]   No changes

📝 Apply Summary:
  Total surveys: 2
  Surveys applied: 1
  Surveys failed: 0

📋 Details:
  ✓ SV_abc123: 1 changes applied
  ✗ SV_def456: No changes

✓ Apply complete: 1 survey/surveys updated
```

**Drift detected (requires --force or --skip-drift):**
```
[qsync:master-apply] Checking SV_abc123...
[qsync:master-apply]   Drift detected (SurveyName); use --force to override

📝 Apply Summary:
  Total surveys: 1
  Surveys applied: 0
  Surveys failed: 1

📋 Details:
  ✗ SV_abc123: Drift detected in fields: SurveyName

ⓘ No surveys were updated
```

**Dangerous field without --allow-dangerous:**
```
[qsync:master-apply] Checking SV_abc123...
[qsync:master-apply]   SV_abc123: Dangerous changes detected; use --allow-dangerous to proceed

📋 Details:
  ✗ SV_abc123: Dangerous changes require --allow-dangerous flag
```

---

## Understanding Drift Detection

### What is Drift?

**Drift** occurs when the live Qualtrics values differ from your snapshot baseline. This can happen if:
- Another user manually edited the survey in the Qualtrics UI
- Another team member ran `qsync survey master apply` on the same surveys
- Qualtrics updated a field automatically (e.g., response counts)

### How It Works

For each field you want to change:
1. Snapshot baseline = value from last `pull`
2. CSV value = your edited value
3. Live value = current value in Qualtrics
4. **Drift detected if:** baseline ≠ live

If drift is detected, apply refuses to proceed (unless `--force`).

### API Cost

Drift detection requires **3 additional API calls per survey** (one per endpoint):
- GET `/surveys/{surveyId}` for status
- GET `/survey-definitions/{surveyId}/metadata` for metadata
- GET `/survey-definitions/{surveyId}/options` for options

**Example:** Applying to 10 surveys = 30 extra API calls.

### When to Skip Drift

- **Environment:** You're the only person editing these surveys
- **Timing:** Changes are happening fast and you want speed
- **Risk tolerance:** You've verified no concurrent edits

Use `--skip-drift` to save API calls and time.

---

## Schema Version Mismatches

If `qualtrics_api_key_mapping.csv` has been updated since your last pull, you may see:

```
⚠️  Schema version mismatch: snapshot=20251219-abc123de, current=20251220-xyz789ab.
Consider running 'qsync survey master pull' to refresh snapshots.
```

**What to do:**
1. Run `qsync survey master pull` to refresh snapshots
2. Re-edit your CSV if needed
3. Re-run preview, then stage and push

The mismatch doesn't block legacy `apply` (with `--force`), but it's safer to re-pull.

---

## Publishing Behavior

### When Does Publishing Happen?

Publishing is a separate explicit step:

```bash
qsync survey master push
```

Use `master push` after `master stage` when you are ready to write and publish definition changes.

Status-only changes (for example `isActive`) still do not require publishing.

### What Gets Published?

A new **survey version** is created with:
- Description: `"qsync master push"` (or `--description` override)
- Published flag: `true` (in active status)

This version becomes the new "published" version for the survey.

### Rollback

Use rollback snapshots captured during `master push` (and legacy `master apply`):

```bash
qsync survey master rollback --list [--survey-id SV_xxx]
qsync survey master rollback --survey-id SV_xxx --version 1 --dry-run
qsync survey master rollback --survey-id SV_xxx --version 1
```

Rollback enforces drift and dangerous-field safeguards unless overridden with `--force` / `--allow-dangerous`.

---

## Audit Logging

### What Gets Logged

Every successful apply writes a **JSONL** entry to `logs/qualtrics_write.log` with:
- Timestamp (ISO 8601)
- Action: `qsync.master.apply`
- Survey ID
- List of applied changes (field → new value)

### Example Entry

```json
{
  "timestamp": "2025-12-19T14:30:45.123456Z",
  "action": "qsync.master.apply",
  "survey_id": "SV_abc123",
  "changes": [
    {"field": "SurveyName", "new_value": "Updated Survey Name"},
    {"field": "BackButton", "new_value": "false"}
  ]
}
```

### Dry-Run Doesn't Log

When using `--dry-run`, **no audit log entry is written**. This allows safe testing without leaving traces.

### Log Location

Default: `logs/qualtrics_write.log` (in workspace root)

Override with environment variables:
```bash
export QSYNC_LOG_DIR=/custom/log/path
qsync survey master apply

# OR

export QSYNC_LOG_DIR=/another/path
qsync survey master apply
```

---

## Troubleshooting

### "No snapshot found for {survey_id}"

**Cause:** You haven't run `qsync survey master pull` yet, or the snapshot was deleted.

**Fix:** Run `qsync survey master pull`

### "Drift detected in fields: SurveyName"

**Cause:** The snapshot baseline differs from the current live value.

**Possible reasons:**
- You edited the survey manually in Qualtrics UI after running pull
- Another user applied changes to this survey
- Qualtrics auto-updated the field

**Fix:**
1. Review the drift warning
2. Either:
   - Update your CSV to match the live value and re-pull
   - Use `--force` to overwrite the live value
   - Use `--skip-drift` if you know there are no concurrent edits

### "Dangerous changes require --allow-dangerous flag"

**Cause:** Your CSV edits included one of the 6 dangerous fields.

**Fields that trigger this:**
- `isActive`
- `SurveyStatus`
- `EOSRedirectURL`
- `BallotBoxStuffingPreventionURL`
- `RefererURL`
- `PasswordProtection`

**Fix:** Add `--allow-dangerous` flag if you're sure about the changes:
```bash
qsync survey master apply --allow-dangerous
```

### "One or more endpoint writes failed"

**Cause:** An API call to write metadata, options, or status returned an error.

**Likely reasons:**
- Network issue
- Survey is locked by another operation
- Invalid field value (doesn't match Qualtrics schema)

**Fix:**
1. Check internet connection
2. Verify the survey isn't locked in Qualtrics UI
3. Review the CSV value (e.g., `isActive` must be `true` or `false`, not `1` or `0`)
4. Try again with `--survey-id` to test on one survey first

### "Schema validation error: Unknown column"

**Cause:** Your CSV has a column that's not in the mapping.

**Fix:**
1. Check column spelling
2. Ensure it matches `qualtrics_api_key_mapping.csv` exactly
3. Delete the invalid column from your CSV

---

## Cross-Dimension Conflicts

When using Survey Master alongside other qsync dimensions (items, translations, flow, etc.), certain fields may overlap causing potential conflicts. Understanding and resolving these conflicts ensures safe multi-dimension workflows.

### Conflict Detection

The `qsync sync` orchestrator automatically detects potential conflicts between master and other dimensions:

```bash
qsync sync --survey-id SV_...
```

**Common conflict scenarios:**

1. **Master + Translations** (both staged):
   - **Fields**: `SurveyName`, `SurveyDescription`
   - **Warning**: "Both master and translations have staged changes. Consider pushing translations first to avoid metadata conflicts."
   - **Resolution**: Push translations dimension first, then master

2. **Master + Items** (overlapping survey metadata):
   - **Fields**: `BrandID`, survey options that affect display
   - **Warning**: "Master has staged changes while other dimensions have unstaged changes."
   - **Resolution**: Review changes carefully, ensure no field overwriting

### Conflict Resolution Policy

**Field precedence (when conflicts occur):**

| Field Type | Master | Translations | Items | Winner |
|------------|--------|--------------|-------|---------|
| `SurveyName`, `SurveyDescription` | ✓ | ✓ | - | Translations (if both staged, push translations first) |
| Survey options (branding, styling) | ✓ | - | Can affect | Master |
| Question text, choices | - | ✓ | ✓ | Items/Translations (master doesn't touch questions) |

**Safe merge order** (enforced by `qsync sync`):
1. Items (questions, choices, embedded data)
2. EDF (embedded data schema)
3. JS (question logic)
4. Translations (multi-language text)
5. EOS (end-of-survey messages)
6. Flow (survey flow structure)
7. **Master** (survey-level metadata and options) ← Applied last to avoid overwrites

### Best Practices

**When editing metadata:**
- ✅ Use **translations dimension** for `SurveyName` and `SurveyDescription` in multi-language surveys
- ✅ Use **master dimension** for survey-level options, branding, dates, and status
- ✅ Check `qsync sync --survey-id SV_...` before pushing to detect conflicts
- ⚠️ Avoid editing same fields in both dimensions simultaneously

**When using `qsync sync`:**
- The orchestrator applies master changes last to prevent overwriting dimension-specific metadata
- Conflict warnings are displayed before push
- You can abort and re-stage in the correct order

**Example workflow with conflicts:**
```bash
# 1. Check for conflicts
qsync sync --survey-id SV_abc123

# Output shows:
#   ⚠ Both master and translations have staged changes

# 2. Resolve by pushing translations first
qsync translations push --survey-id SV_abc123

# 3. Then push master
qsync survey master push --survey-id SV_abc123

# OR use qsync sync to handle safe order automatically
qsync sync --survey-id SV_abc123
```

### Non-Interactive Safety Gates

When using `qsync sync` with `--yes` (non-interactive mode), additional safety gates apply to master pushes:

- **Locked surveys**: Block push (override with `--allow-locked`)
- **Active responses**: Block push (override with `--force-live`)
- **Dangerous fields**: Block push (override with `--allow-dangerous`)
- **Drift detection**: Blocks if snapshot hash mismatches (override with `--allow-drift`)

These gates prevent silent master pushes that could affect production surveys without review.

---

## Performance Optimization

### API Call Counts

| Operation | API Calls | Notes |
|-----------|-----------|-------|
| `pull` | ~20 | 4 calls per survey (status, metadata, options, versions) |
| `preview` | 0 | Offline comparison to snapshot |
| `apply` (basic) | ~6 | 3 calls per endpoint (metadata, options, status) |
| `apply` (with drift) | ~9 | 3 drift calls + 6 apply calls |
| `apply` + publish | ~9 + publish | Plus publishing overhead |

### Reducing API Calls

**Use `--skip-drift` for speed:**
```bash
# 6 calls (no drift detection)
qsync survey master apply --skip-drift

# vs. 9+ calls (with drift detection)
qsync survey master apply
```

**Preview before apply:**
```bash
# This is free (no API calls):
qsync survey master preview

# Only apply if preview looks correct
qsync survey master apply
```

**Test on one survey:**
```bash
qsync survey master apply --survey-id SV_abc123
```

---

## Related Documentation

- **Survey Master Field Reference:** `../reference/survey-master-fields.md`
- **Survey Master Mapping Schema:** `../reference/survey-master-mapping-schema.md`
- **Mapping CSV (workspace override):** `surveys/qualtrics_api_key_mapping.csv`
- **CLI Reference:** `../reference/cli.md`

---

## Quick Reference

### Commands at a Glance

```bash
# 1. Pull data
qsync survey master pull

# 2. Edit surveys/qualtrics_master.csv in spreadsheet editor

# 3. Preview changes
qsync survey master preview --detail

# 4. Dry-run on one survey (optional)
qsync survey master apply --survey-id SV_abc123 --dry-run

# 5. Apply to all
qsync survey master apply

# 6. With dangerous fields
qsync survey master apply --allow-dangerous

# 7. Override drift detection
qsync survey master apply --force
```

### Flag Cheat Sheet

| Flag | Use Case | Impact |
|------|----------|--------|
| `--allow-dangerous` | Editing isActive, URLs, passwords | Required for 6 fields |
| `--force` | You know drift is safe | Skip drift check |
| `--skip-drift` | Speed matters, no concurrency | Save ~3 API calls/survey |
| `--dry-run` | Test before applying | No changes written |
| `--survey-id` | Test or edit one survey | Limit scope |
| `--detail` (preview) | See exact changes | Detailed output |

---

## Questions?

For issues or questions:
1. Check this guide's troubleshooting section
2. Review the `qsync survey master preview` output for clues
3. Run with higher verbosity (logs are printed to console)
