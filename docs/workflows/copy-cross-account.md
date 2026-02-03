# Cross-Account Survey Copy

This workflow covers `qsync survey copy-cross-account`, which copies a survey from one Qualtrics account ("source") into another ("target").

## What this does (and does not do)

- Copies a survey definition by exporting from the source account and importing into the target account.
- Can optionally copy translations (languages + strings) after the import.
- Can optionally publish and/or activate the target survey after copy.

Important limitations:
- If you overwrite an existing target survey, Qualtrics version history is lost for that target survey, and the replacement survey will have a NEW SurveyID.
- It does not preserve "survey history" across accounts; treat this as a controlled import.

## Prerequisites

- API access for both accounts (valid API tokens).
- Base URLs are host-only (no `https://`), e.g. `iad1.qualtrics.com`.
- The target account user must have permission to create surveys (and publish/activate if you use those flags).

By default, the source account is your configured account (from `.env` / `--env-path` / environment variables). Use `--source-api-key` and `--source-base-url` to override the source explicitly.

## Recommended runbook

1) Run in preview mode first (do not use `--yes`):

```bash
qsync survey copy-cross-account SV_SOURCE "New Survey Name" \
  --target-base-url iad1.qualtrics.com \
  --target-api-key "$TARGET_API_TOKEN"
```

2) If you do not want translations copied:

```bash
qsync survey copy-cross-account SV_SOURCE "New Survey Name" \
  --target-base-url iad1.qualtrics.com \
  --target-api-key "$TARGET_API_TOKEN" \
  --no-translations
```

3) If you want qsync to publish and activate after copy:

```bash
qsync survey copy-cross-account SV_SOURCE "New Survey Name" \
  --target-base-url iad1.qualtrics.com \
  --target-api-key "$TARGET_API_TOKEN" \
  --publish \
  --publish-description "qsync copy from SV_SOURCE" \
  --activate
```

4) Overwrite mode (dangerous):

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
