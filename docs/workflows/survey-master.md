# Survey Master Workflow

Survey Master is the bulk-edit surface for survey metadata/options/status fields across multiple surveys.

Canonical workflow:

`pull -> edit -> preview -> stage -> push`

## Account Scoping

Survey Master artifacts are account-scoped:

- account-root layout: `accounts/<account>/surveys/...`
- legacy compatibility layout: `surveys/` (default) or `surveys/.<account>/...`

See `../reference/accounts.md` and `../reference/workspace-path-ownership.md`.

## Editing Surfaces

After `qsync survey master pull`, qsync writes:

- `qualtrics_master.csv`
- `qualtrics_master.xlsx`
- snapshot files used for preview/stage/rollback

These files live in the account-scoped `surveys` surface.

## Runbook

1. Pull

```bash
qsync survey master pull
```

Common pull options:

- `--survey-id <SV_...>` (repeatable) to limit scope
- `--force-overwrite` to regenerate CSV without merge-preserving existing edits
- `--mapping-csv <path>` to use a workspace mapping override

2. Edit

- Preferred: edit `qualtrics_master.xlsx`
- Alternative: edit `qualtrics_master.csv`

3. Preview

```bash
qsync survey master preview
```

Useful preview options:

- `--detail` for per-field diffs
- `--survey-id <SV_...>` for single-survey focus
- `--tag key=value` (repeatable) for tag-scoped preview
- `--format json` for automation
- `--all-surveys` to include non-focal rows

4. Stage

```bash
qsync survey master stage
```

Useful stage options:

- `--survey-id <SV_...>`
- `--tag key=value` (repeatable)
- `--all-surveys`

5. Push

```bash
qsync survey master push
```

Useful push options:

- `--description "qsync master push ..."` custom publish description
- `--no-publish` write-only (skip publish)
- `--force-live` allow push with live responses
- `--force-preview` suppress preview-response warnings
- `--allow-dangerous` allow dangerous field writes
- `--allow-locked` bypass inventory lock checks
- `--survey-id <SV_...>` or `--all-surveys`

## Rollback

List snapshots:

```bash
qsync survey master rollback --list
```

Preview rollback:

```bash
qsync survey master rollback --survey-id SV_xxx --version 1 --dry-run
```

Apply rollback:

```bash
qsync survey master rollback --survey-id SV_xxx --version 1
```

Common rollback safety flags:

- `--force` (override drift guard)
- `--allow-dangerous`
- `--no-publish`

## Notes

- Use `preview` before every `stage`/`push`.
- `push` is the remote-write step; `stage` is local-only.
- For field-level details and dangerous-field definitions, see `../reference/survey-master-fields.md`.
