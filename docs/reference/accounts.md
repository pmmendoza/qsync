# Accounts and Multi-Account Workspaces

`qsync` supports multiple Qualtrics accounts in one workspace via `.env.<account>` credentials plus account-scoped artifact directories.

## Account Selection

Credential resolution order:

1. `--account <name>`
2. `QSYNC_ACCOUNT=<name>`
3. Workspace preference `.qsync/preferences.json` (`qsync account use <name>`)
4. Default account (`.env`)

Notes:
- `--env-path` only affects the default account.
- Named accounts always load `<root>/.env.<name>`.
- `qsync account use` stores workspace preference only; it does not export shell env vars.

## Layout Modes

Primary layout (account-root):
- Default account: `accounts/default/...`
- Named account: `accounts/<name>/...`

Legacy compatibility layout (still supported):
- Default account: unscoped roots (`surveys/`, `excel/`, `survey_js/`, ...)
- Named account: `.<name>` subfolders (`surveys/.<name>/`, `excel/.<name>/`, ...)

`qsync` resolves paths from workspace layout mode; command behavior is the same.

## Account-Scoped Surfaces

These surfaces are account-scoped:

- `surveys` (inventory, cached definitions, pending state, flow/blocks/master artifacts)
- `excel` (workbooks)
- `survey_js`
- `contents`
- `export`
- `responses`
- `tmp`

Shared (not account-scoped):

- `.env` / `.env.<account>`
- `.qsync/*`
- `logs/*`
- compatibility mapping files (for example `surveys/qualtrics_api_key_mapping.csv`)

## `qsync account` Commands

- `qsync account status`: show active account resolution and scoped directories.
- `qsync account list`: list discoverable `.env.<account>` files.
- `qsync account use <name>`: persist active account in workspace preferences.
- `qsync account clear`: clear persisted active account preference.
- `qsync account adopt <name>`: migrate allowlisted unscoped artifacts into that account's scoped surfaces.
  - Use `--dry-run` first.
  - `--merge` skips conflicts, `--overwrite` replaces existing files.
  - `--use` switches active account after adoption.

## Cross-Account Copy and `default`

For commands with separate source/target accounts (for example `qsync survey copy-cross-account`), use `default` to force primary `.env` credentials even if a named account is active:

- `--source-account default`
- `--target-account default`
