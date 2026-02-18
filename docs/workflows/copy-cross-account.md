# Cross-Account Survey Copy

This workflow covers `qsync survey copy-cross-account`, which copies a survey from one Qualtrics account ("source") into another ("target").

## What this does (and does not do)

- Copies a survey definition by exporting from the source account and importing into the target account.
- Can optionally copy translations (languages + strings) after the import.
- Can optionally publish and/or activate the target survey after copy.

Important limitations:
- If you overwrite an existing target survey, Qualtrics version history is lost for that target survey, and the replacement survey will have a NEW SurveyID.
- It does not preserve "survey history" across accounts; treat this as a controlled import.
- EOS library message IDs can be account-specific. If EndSurvey `DisplayMessage` refs are broken in target, run:

```bash
qsync eos repair --survey-id SV_TARGET \
  --account <target-account> \
  --source-account <source-account-or-default> \
  --source-survey-id SV_SOURCE \
  --yes
```

## Prerequisites

- API access for both accounts (valid API tokens).
- Base URLs are host-only (no `https://`), e.g. `iad1.qualtrics.com`.
- The target account user must have permission to create surveys (and publish/activate if you use those flags).

By default, the source account is whatever `qsync` would normally use for this run (in order: `--account`, `QSYNC_ACCOUNT`, workspace `active_account` set via `qsync account use`, else `.env`). Use `--source-api-key` and `--source-base-url` to override the source explicitly.

Target account configuration:
- Recommended (multi-account): create a workspace-local dotenv file named `.env.<account>` (e.g. `.env.partner`) containing:
  - `QUALTRICS_BASE_URL`
  - `X-API-TOKEN` (or `QUALTRICS_API_KEY`)
  Then use `--target-account <account>`.
  - Note: `.env.<account>` also accepts `TARGET_QUALTRICS_BASE_URL` and `TARGET_X-API-TOKEN` for backward compatibility, but the canonical keys above are recommended.
- Recommended: set these in `.env` (or environment variables) and omit `--target-*` flags:
  - `TARGET_QUALTRICS_BASE_URL`
  - `TARGET_X-API-TOKEN` (or `TARGET_QUALTRICS_API_KEY`)
- Alternatively: pass `--target-base-url` and `--target-api-key` on the command line.

If you have a workspace active account but want to explicitly use the primary `.env` for one side, you can use the literal value `default`:

- `--source-account default`
- `--target-account default`

## Recommended runbook

1) Run in preview mode first (do not use `--yes`):

```bash
qsync survey copy-cross-account SV_SOURCE "New Survey Name" \
  --target-base-url iad1.qualtrics.com \
  --target-api-key "$TARGET_API_TOKEN"
```

If you have a `.env.partner` (or similar) in your workspace root, you can select it directly:

```bash
qsync survey copy-cross-account SV_SOURCE "New Survey Name" \
  --target-account partner
```

If you have `TARGET_QUALTRICS_BASE_URL` and `TARGET_X-API-TOKEN` configured in `.env`, you can omit the `--target-*` flags:

```bash
qsync survey copy-cross-account SV_SOURCE "New Survey Name"
```

2) If you do not want translations copied:

```bash
qsync survey copy-cross-account SV_SOURCE "New Survey Name" \
  --target-base-url iad1.qualtrics.com \
  --target-api-key "$TARGET_API_TOKEN" \
  --no-translations
```

3) If you want to verify parity (recommended for smoke runs / automation):

```bash
qsync survey copy-cross-account SV_SOURCE "New Survey Name" \
  --target-base-url iad1.qualtrics.com \
  --target-api-key "$TARGET_API_TOKEN" \
  --verify
```

3b) If you want to verify *deep parity* (strict, compares `survey-definitions` JSON after normalization):

```bash
qsync survey copy-cross-account SV_SOURCE "New Survey Name" \
  --target-base-url iad1.qualtrics.com \
  --target-api-key "$TARGET_API_TOKEN" \
  --verify-deep
```

4) If you want qsync to publish and activate after copy:

```bash
qsync survey copy-cross-account SV_SOURCE "New Survey Name" \
  --target-base-url iad1.qualtrics.com \
  --target-api-key "$TARGET_API_TOKEN" \
  --publish \
  --publish-description "qsync copy from SV_SOURCE" \
  --activate
```

5) Overwrite mode (dangerous):

```bash
qsync survey copy-cross-account SV_SOURCE "Existing Target Survey Name" \
  --target-base-url iad1.qualtrics.com \
  --target-api-key "$TARGET_API_TOKEN" \
  --force-overwrite
```

Use overwrite only when you explicitly accept that the existing target survey (and its version/publish history) will be deleted and replaced.

## See also

- Full flag reference: `../reference/cli.md` (search for `copy-cross-account`)
- QSF structure notes: `../reference/qsf-vs-json.md`
