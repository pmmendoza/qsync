# Accounts and Multi-Account Workspaces

`qsync` can run against multiple Qualtrics accounts from a single workspace by selecting different dotenv files and scoping workspace writes under account-specific directories.

## Account Selectors

- Default account (unnamed): credentials come from `<root>/.env` (or `--env-path` / `QSYNC_ENV_PATH`).
- Named account `<name>`: credentials come from `<root>/.env.<name>`.

Account names are validated and mapped directly to filenames; use simple names like `damian` or `partner_2` (allowed: letters, numbers, `_`, `-`).

## Selection Precedence

When you run a command that needs credentials, `qsync` selects an account in this order:

1. `--account <name>` (per-command)
2. `QSYNC_ACCOUNT=<name>` (exported environment)
3. Workspace preference: `.qsync/preferences.json` key `active_account` (set via `qsync account use <name>`)
4. Default account (`.env`)

Notes:
- `--env-path` only affects the default account. Named accounts always load `.env.<name>` from the workspace root.
- `qsync account use` does not export any environment variables in your shell; it only writes a workspace-local preference file.

## Where Files Go (Account-Scoped Surfaces)

When a named account is active, `qsync` scopes workspace artifacts under `.<name>/` inside each relevant directory:

- `surveys/.<name>/` (inventory, cached survey JSON, pending state, flow surfaces, survey master artifacts)
- `excel/.<name>/` (workbooks and `excel/archive/`)
- `survey_js/.<name>/` (account-derived mapping CSVs)
- `contents/.<name>/` (EOS/library message cache, translation cache)
- `export/.<name>/` (DOCX/PDF exports)
- `responses/.<name>/` (response exports)
- `tmp/.<name>/` (scratch artifacts)

For the default account, artifacts live directly under the unscoped directories (`surveys/`, `excel/`, `survey_js/`, ...).

Switching accounts does not move any existing files automatically; it changes which surface `qsync` reads/writes.

If you already have unscoped artifacts (default account layout) and want to migrate them into a named account surface, use `qsync account adopt <name>` (see below).

## `qsync account` Commands

- `qsync account status`: Show the resolved active account, where it came from, and the resolved scoped directories.
- `qsync account list`: Discover `.env.<account>` files in the workspace root (best-effort validation).
- `qsync account use <name>`: Persist `active_account=<name>` in `.qsync/preferences.json` for this workspace.
  - On first switch to a named account, qsync also bootstraps `.env.default` from `.env` when `.env.default` is missing.
  - This makes one-off commands like `qsync --account default …` work without manual setup.
- `qsync account clear`: Remove `active_account` from `.qsync/preferences.json` (restores default behavior).
- `qsync account adopt <name>`: Move allowlisted unscoped artifacts into `.<name>/` directories.
  - Use `--dry-run` first.
  - Defaults to refusing conflicts; use `--merge` (skip existing) or `--overwrite` (dangerous).
  - `--use` sets the workspace active account after adoption.
  - Shared file `surveys/qualtrics_api_key_mapping.csv` is intentionally not moved (it remains unscoped).

In `qsync survey menu`, the `Account & Diagnostics → Check API (/whoami)` action now prints the active account label as part of the WHOAMI output, so you can confirm exactly which account the command is hitting.

## Cross-Account Copy: Forcing the Primary `.env`

Some commands accept multiple account selectors. For `qsync survey copy-cross-account`, you can explicitly refer to the primary `.env` even when a named account is active:

- `--source-account default`
- `--target-account default`

This is useful when you’ve set a workspace active account but need to copy to/from the primary `.env` credentials for one side of the operation.
