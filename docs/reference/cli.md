# qsync CLI Reference

This reference tracks canonical CLI grammar as of **2026-02-21**.

For exact runtime truth, use:

- `qsync --help`
- `qsync <group> --help`
- `qsync <group> <command> --help`

## Root Command

```text
Usage: qsync [-h] [--root ROOT] [--env-path ENV_PATH] [--account ACCOUNT]
             [--color {auto,always,never}] [--version] [--allow-locked]
             [--yes]
             COMMAND ...
```

Global flags:

- `--root`
- `--env-path`
- `--account`
- `--color`
- `--allow-locked`
- `--yes` / `-y` (global confirmation bypass)

## Top-Level Groups

- `onboard`
- `survey`
- `prolific`
- `sync`
- `items`
- `translations`
- `flow`
- `blocks`
- `js`
- `eos`
- `export`
- `compare`
- `logs`
- `settings`
- `tui`
- `help`
- `doctor`
- `self-update`
- `account`

## Canonical Syntax Notes

- Removed top-level legacy commands: `qsync init`, `qsync preview`, `qsync apply`, `qsync push`.
- `qsync compare` uses `--report-path` (not `--json-output`).
- Cross-survey commands use explicit IDs:
  - `--source-id`
  - `--target-id`
- Scope selectors use explicit flags:
  - `--all-focal`
  - `--all-surveys`

## Sync

```text
qsync sync [--survey-id ...] [--all-focal] [--dimensions ...] [--scope ...]
           [--tui[=on|auto] | --no-tui]
           [--refresh-inventory | --no-refresh-inventory]
           [--pending-action push|discard|abort]
           [--force-live] [--force-preview] [--skip-publish]
           [--refresh-workbooks] [--allow-drift] [--json] [--fix ...]
```

Notes:

- Valid dimensions: `items, edf, js, translations, eos, blocks, flow, master`
- `--yes` is global (put before or after subcommands)
- `--fix` supports `safe|all|all-safe|type:<ISSUE_TYPE>`
- Inventory preflight refresh:
  - default: auto-refreshes inventory when local data is missing/stale (>30 minutes)
  - `--refresh-inventory`: force refresh before sync
  - `--no-refresh-inventory`: skip preflight refresh
- Non-interactive sync that would mutate now requires explicit `--yes`.
- TUI mode:
  - `--tui` forces Textual sync wizard.
  - `--tui=auto` starts Textual only in interactive TTY (falls back to CLI for `--yes`, `--json`, or non-TTY).
  - `--no-tui` forces CLI sync flow.

## Items

Group:

```text
qsync items {pull,preview,stage,push,edit,inspect,repair-edf}
```

Core workflow:

```text
qsync items pull --survey-id SV_xxx [--prune-orphans] [--scope ...]
qsync items preview --survey-id SV_xxx [--detailed] [--embedded-data-only]
qsync items stage --survey-id SV_xxx [--allow-dangerous]
qsync items push --survey-id SV_xxx [--force-live] [--force-preview] [--no-publish]
```

## Translations

Group:

```text
qsync translations {languages,pull,preview,stage,push,pack,drift,doctor,check-language}
```

Core workflow:

```text
qsync translations pull --survey-id SV_xxx [--account <name>]
qsync translations preview --survey-id SV_xxx --languages FR,NL
qsync translations stage --survey-id SV_xxx --languages FR,NL
qsync translations push --survey-id SV_xxx --languages FR,NL [--use-pending]
```

Compatibility flags kept on `translations push`:

- `--mode {validate,apply}` (deprecated)
- `--validate`
- `--dry-run` (alias for validate behavior)

## Flow and Blocks

Flow:

```text
qsync flow {pull,preview,stage,push}
```

Blocks:

```text
qsync blocks {pull,preview,stage,push,move-qid,add-page-break,remove-page-break,remove-qid}
```

Both support staged workflow and push safeguards (`--force-live`, `--force-preview`, `--no-publish`, `--allow-drift`).

## Survey

Group:

```text
qsync survey COMMAND ...
```

Key command families:

- inventory/cache: `list`, `label`, `focal`, `inventory`, `pull`, `prepare`
- copy/derive: `copy`, `slice-language`, `copy-cross-account`, `slice-registry`, `parity-check`
- edits/utilities: `add-question`, `move-question`, `remove-question`, `add-page-break`, `remove-page-break`, `inspect-question`, `push-question`, `replace-question`
- lifecycle: `publish`, `activate`, `deactivate`, `versions`, `version-fetch`, `rollback`
- exports: `export-responses`, `export-translation`, `export-side-by-side`
- bulk: `master`
- interactive: `menu`

### Survey Master

```text
qsync survey master {columns,pull,preview,stage,push,rollback}
```

Canonical bulk workflow:

```text
qsync survey master pull
qsync survey master preview
qsync survey master stage
qsync survey master push
```

## Compare and Export

Compare:

```text
qsync compare --source-id SV_A --target-id SV_B [--report-path report.json]
```

Export alias surface:

```text
qsync export survey --survey-id SV_xxx [--format docx|pdf|both]
```

Response export:

```text
qsync survey export-responses --survey-id SV_xxx [--format csv|tsv|spss|json|ndjson|xml]
```

Notes:

- Default format is `csv`.
- Output is written under `responses/` (account-scoped when `--account` is used).
- `json` and `ndjson` use the API-only response export modes; qsync automatically
  omits the tabular export flags that Qualtrics rejects for those formats.

Side-by-side export:

```text
qsync survey export-side-by-side --source-id SV_A --target-id SV_B
```

## Logs

Group:

```text
qsync logs {recent,errors,session,since,survey,action,stats,slow,report,archives,rotate}
```

Examples:

```text
qsync logs recent --limit 20 --level INFO
qsync logs session <SESSION_ID>
qsync logs slow --limit 10
qsync logs report --weekly --report-path logs/error_report_weekly.json
```

Notes:

- Most query commands accept `--include-archives` and `--level`.
- `qsync logs report --json` prints machine-readable structured error analysis.

## Prolific and Settings

Prolific group:

```text
qsync prolific {pull-studies,propose-matches,wire}
```

Settings:

```text
qsync settings [--tui]
```

## Account and Doctor

Account group:

```text
qsync account {status,list,use,clear,adopt}
```

Doctor:

```text
qsync doctor [--json] [--quiet] [--check-api] [--account <name>]
```
