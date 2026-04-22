# Survey Create Workflow

This workflow covers `qsync survey create`, which creates a new inactive Qualtrics survey in the selected account.

## What this does

- Creates a new survey by uploading QSF to `POST /API/v3/surveys`.
- Leaves the new survey inactive and unpublished.
- Prints the new `SurveyID` and edit URL.
- Supports automation-friendly JSON output.

Creation sources:

- No source: use qsync's bundled minimal QSF seed.
- `--from-qsf`: import a local QSF file as the starting point.
- `--template-survey-id`: export an existing survey as QSF and import it under a new name.

## Prerequisites

- The selected account must have API credentials configured.
- The Qualtrics user must have permission to create surveys.
- For named accounts, pass `--account <name>` or select one with `qsync account use`.

## Runbook

Create a blank starter survey:

```bash
qsync --account damian survey create "New Survey Name" --language EN
```

Create from a local QSF:

```bash
qsync survey create "New Survey Name" --from-qsf path/to/template.qsf
```

Create from an existing survey template in the same account:

```bash
qsync survey create "New Survey Name" --template-survey-id SV_SOURCE
```

Automation output:

```bash
qsync survey create "New Survey Name" --json
```

The JSON object includes:

- `ok`
- `survey_id`
- `name`
- `account`
- `base_url`
- `source_kind`
- `source_ref`
- `edit_url`

## After create

Pull the new survey before editing qsync-managed surfaces:

```bash
qsync survey pull --survey-id SV_NEW
```

Then use the usual dimension workflows:

```bash
qsync items pull --survey-id SV_NEW
qsync js pull --survey-id SV_NEW
qsync flow pull --survey-id SV_NEW
```

Publish or activate only when you explicitly intend to:

```bash
qsync survey publish --survey-id SV_NEW --description "Initial qsync publish"
qsync survey activate --survey-id SV_NEW
```

Delete a temporary smoke survey after validation:

```bash
qsync --account damian --yes survey delete SV_NEW
```

## Notes

- The command rewrites `SurveyEntry.SurveyName`, `SurveyEntry.SurveyLanguage`, and `SurveyEntry.SurveyStatus`.
- The source survey or QSF is otherwise preserved so Qualtrics receives a real QSF-shaped import.
- `survey create` is same-account only. Use `survey copy-cross-account` when source and target accounts differ.

## See also

- CLI reference: `../reference/cli.md`
- QSF structure notes: `../reference/qsf-vs-json.md`
- Cross-account copy: `copy-cross-account.md`
