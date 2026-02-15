# qsync CLI Reference

This file is a snapshot of `qsync --help` output (captured from the CLI itself).
For the most up-to-date view, run `qsync --help` and `qsync <command> --help`.

## `qsync (root)`

```text
usage: qsync [-h] [--root ROOT] [--env-path ENV_PATH]
             [--color {auto,always,never}] [--allow-locked]
             {doctor,compare,init,preview,apply,push,items,sync,export,survey,logs,js,eos,flow,translations}
             ...

Qualtrics sync and survey management for Qualtrics surveys

positional arguments:
  {doctor,compare,init,preview,apply,push,items,sync,export,survey,logs,js,eos,flow,translations}
    doctor              Print resolved workspace/config paths for debugging
    compare             Compare two surveys (items + JS + metadata) using
                        cached or refreshed definitions.
    init                Initialise or refresh the Excel workbook for a survey
                        from Qualtrics
    preview             Show what would change in Qualtrics based on the
                        workbook
    apply               Apply the changes from the workbook to Qualtrics
    push                Push staged wording changes from the cached JSON to
                        Qualtrics
    items               Manage survey items (questions, options, subitems) via
                        Excel workbook
    sync                Orchestrate multi-dimension sync for one or more
                        surveys
    export              Export survey content for review
    survey              Manage Qualtrics surveys (inventory,
                        copy/rename/delete, publish/version/rollback, master)
    logs                View and analyze operation logs
    js                  Manage Qualtrics QuestionJS via the mapping CSV
    eos                 Manage Qualtrics EndSurvey (EOS) library messages
    flow                Manage survey flow (branching logic, block ordering, routing)
    translations        Manage survey translations

options:
  -h, --help            show this help message and exit
  --root ROOT           Workspace root directory (contains surveys/, excel/,
                        survey_js/, etc.).
  --env-path ENV_PATH   Path to a .env file with credentials (overrides
                        QSYNC_ENV_PATH and <root>/.env).
  --color {auto,always,never}
                        Color output: auto (default), always, or never.
                        NO_COLOR forces never.
  --allow-locked        Bypass surveys/inventory.csv lock checks (dangerous).
```

## `qsync doctor`

```text
usage: qsync doctor [-h] [--json] [--quiet] [--check-api] [--account ACCOUNT]

options:
  -h, --help   show this help message and exit
  --json       Emit machine-readable JSON to stdout (no other output)
  --quiet      Suppress non-error output
  --check-api  Call GET /whoami to validate credentials and detect datacenter
               mismatch (requires network).
  --account ACCOUNT
               Use credentials from `.env.<account>` under the workspace root
               (affects credential checks and --check-api).
```

## `qsync compare`

```text
usage: qsync compare [-h] --source-id SOURCE_ID --target-id TARGET_ID
                     [--no-refresh] [--include-tag INCLUDE_TAGS]
                     [--exclude-tag EXCLUDE_TAGS] [--json-output JSON_OUTPUT]
                     [--fail-on {any,question,metadata}] [--with-diffs]

options:
  -h, --help            show this help message and exit
  --source-id SOURCE_ID
                        Source SurveyID
  --target-id TARGET_ID
                        Target SurveyID
  --no-refresh          Use cached surveys without refreshing from Qualtrics
  --include-tag INCLUDE_TAGS
                        Only compare questions with these DataExportTag values
                        (can repeat)
  --exclude-tag EXCLUDE_TAGS
                        Skip questions with these DataExportTag values (can
                        repeat)
  --json-output JSON_OUTPUT
                        Optional path to write JSON report
  --fail-on {any,question,metadata}
                        Exit non-zero when mismatches exist (default: any)
  --with-diffs          Include per-field before/after and unified diffs in
                        the JSON output
```

## `qsync init`

```text
usage: qsync init [-h] [--survey-id SURVEY_ID] [--filter-column FILTER_COLUMN]
                  [--filter-value FILTER_VALUE] [--include-qid INCLUDE_QIDS]
                  [--include-tag INCLUDE_TAGS] [--xlsx XLSX]
                  [--language LANGUAGE] [--languages LANGUAGES]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Target Qualtrics Survey ID (omit to select
                        interactively)
  --filter-column FILTER_COLUMN
                        Optional filter column on Questions sheet (e.g. InPre,
                        InPost)
  --filter-value FILTER_VALUE
                        Value to match in the filter column (default: TRUE)
  --include-qid INCLUDE_QIDS
                        Limit to specific Qualtrics QIDs (can be repeated).
  --include-tag INCLUDE_TAGS
                        Limit to specific DataExportTag values (can be
                        repeated).
  --xlsx XLSX           Path to the Excel workbook (default:
                        excel/<SurveyTitle>-<SurveyID>.xlsx)
  --language LANGUAGE   Add translation columns for a language (repeatable).
                        If omitted, auto-detects all enabled languages from
                        Qualtrics.
  --languages LANGUAGES
                        Comma-separated language codes to add as translation
                        columns. If omitted, auto-detects all enabled
                        languages from Qualtrics.
```

## `qsync preview`

```text
usage: qsync preview [-h] [--survey-id SURVEY_ID] [--xlsx XLSX]
                     [--filter-column FILTER_COLUMN]
                     [--filter-value FILTER_VALUE]
                     [--include-qid INCLUDE_QIDS] [--include-tag INCLUDE_TAGS]
                     [--detailed] [--embedded-data-only] [--allow-drift]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Target Qualtrics Survey ID (omit to select
                        interactively)
  --xlsx XLSX           Path to the Excel workbook for this survey (default:
                        derived)
  --filter-column FILTER_COLUMN
                        Optional filter column on Questions sheet (e.g. InPre,
                        InPost)
  --filter-value FILTER_VALUE
                        Value to match in the filter column (default: TRUE)
  --include-qid INCLUDE_QIDS
                        Limit to specific Qualtrics QIDs (can be repeated).
  --include-tag INCLUDE_TAGS
                        Limit to specific DataExportTag values (can be
                        repeated).
  --detailed            Print full old/new HTML for each detected change
  --embedded-data-only  Only show Embedded_Data changes
  --allow-drift         Allow preview against a drifted cache without
                        prompting
```

## `qsync apply`

```text
usage: qsync apply [-h] [--survey-id SURVEY_ID] [--xlsx XLSX]
                   [--filter-column FILTER_COLUMN]
                   [--filter-value FILTER_VALUE] [--include-qid INCLUDE_QIDS]
                   [--include-tag INCLUDE_TAGS] [--yes] [--force-live]
                   [--force-preview-items] [--embedded-data-only]
                   [--allow-dangerous] [--allow-drift]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Target Qualtrics Survey ID (omit to select
                        interactively)
  --xlsx XLSX           Path to the Excel workbook for this survey (default:
                        derived)
  --filter-column FILTER_COLUMN
                        Optional filter column on Questions sheet (e.g. InPre,
                        InPost)
  --filter-value FILTER_VALUE
                        Value to match in the filter column (default: TRUE)
  --include-qid INCLUDE_QIDS
                        Limit to specific Qualtrics QIDs (can be repeated).
  --include-tag INCLUDE_TAGS
                        Limit to specific DataExportTag values (can be
                        repeated).
  --yes                 Proceed without an interactive confirmation prompt
  --force-live          Allow pushes even if finished responses exist in
                        Qualtrics
  --force-preview-items
                        Allow item pushes when only preview/test responses
                        exist
  --embedded-data-only  Only apply Embedded_Data changes
  --allow-dangerous     Allow dangerous embedded data edits (fields without
                        defaults).
  --allow-drift         Proceed even if cached survey differs from the live
                        API
```

## `qsync push`

```text
usage: qsync push [-h] --survey-id SURVEY_ID [--yes] [--force-live]
                  [--force-preview-items] [--allow-drift] [--no-publish]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Target Qualtrics Survey ID (e.g. SV_5AsKyAO5QqswBcq)
  --yes                 Skip the interactive confirmation prompt
  --force-live          Allow pushes even if finished responses exist in
                        Qualtrics
  --force-preview-items
                        Allow item pushes when only preview/test responses
                        exist
  --allow-drift         Proceed even if cached survey differs from the live
                        API
  --no-publish          Skip publishing the survey after pushing question
                        updates
```

## `qsync items`

```text
usage: qsync items [-h] {pull,preview,stage,push} ...

positional arguments:
  {pull,preview,stage,push}
    pull                Pull survey to Excel workbook
    preview             Show workbook diffs
    stage               Stage changes to local cache
    push                Push staged changes to Qualtrics

options:
  -h, --help            show this help message and exit
```

## `qsync items pull`

```text
usage: qsync items pull [-h] [--survey-id SURVEY_ID]
                        [--filter-column FILTER_COLUMN]
                        [--filter-value FILTER_VALUE]
                        [--include-qid INCLUDE_QIDS]
                        [--include-tag INCLUDE_TAGS] [--xlsx XLSX]
                        [--language LANGUAGE] [--languages LANGUAGES]
                        [--scope SCOPE]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Target Qualtrics Survey ID (omit to select
                        interactively)
  --filter-column FILTER_COLUMN
                        Optional filter column on Questions sheet (e.g. InPre,
                        InPost)
  --filter-value FILTER_VALUE
                        Value to match in the filter column (default: TRUE)
  --include-qid INCLUDE_QIDS
                        Limit to specific Qualtrics QIDs (can be repeated).
  --include-tag INCLUDE_TAGS
                        Limit to specific DataExportTag values (can be
                        repeated).
  --xlsx XLSX           Path to Excel workbook
  --language LANGUAGE   Add translation columns
  --languages LANGUAGES
                        Comma-separated language codes
  --scope SCOPE         Scope filter expression (qid:<QID>,
                        tag:<DataExportTag>; supports AND/OR/()). See
                        docs/reference/scope-semantics.md.
```

## `qsync items preview`

```text
usage: qsync items preview [-h] [--survey-id SURVEY_ID] [--xlsx XLSX]
                           [--filter-column FILTER_COLUMN]
                           [--filter-value FILTER_VALUE]
                           [--include-qid INCLUDE_QIDS]
                           [--include-tag INCLUDE_TAGS] [--detailed]
                           [--embedded-data-only] [--scope SCOPE]
                           [--allow-drift]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Target Qualtrics Survey ID (omit to select
                        interactively)
  --xlsx XLSX           Path to the Excel workbook for this survey (default:
                        derived)
  --filter-column FILTER_COLUMN
                        Optional filter column on Questions sheet (e.g. InPre,
                        InPost)
  --filter-value FILTER_VALUE
                        Value to match in the filter column (default: TRUE)
  --include-qid INCLUDE_QIDS
                        Limit to specific Qualtrics QIDs (can be repeated).
  --include-tag INCLUDE_TAGS
                        Limit to specific DataExportTag values (can be
                        repeated).
  --detailed            Full diffs
  --embedded-data-only
  --scope SCOPE         Scope filter expression (qid:<QID>,
                        tag:<DataExportTag>; supports AND/OR/()). See
                        docs/reference/scope-semantics.md.
  --allow-drift         Allow preview against a drifted cache without
                        prompting
```

## `qsync items stage`

```text
usage: qsync items stage [-h] [--survey-id SURVEY_ID] [--xlsx XLSX]
                         [--filter-column FILTER_COLUMN]
                         [--filter-value FILTER_VALUE]
                         [--include-qid INCLUDE_QIDS]
                         [--include-tag INCLUDE_TAGS] [--yes]
                         [--embedded-data-only] [--allow-dangerous]
                         [--scope SCOPE] [--allow-drift]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Target Qualtrics Survey ID (omit to select
                        interactively)
  --xlsx XLSX           Path to the Excel workbook for this survey (default:
                        derived)
  --filter-column FILTER_COLUMN
                        Optional filter column on Questions sheet (e.g. InPre,
                        InPost)
  --filter-value FILTER_VALUE
                        Value to match in the filter column (default: TRUE)
  --include-qid INCLUDE_QIDS
                        Limit to specific Qualtrics QIDs (can be repeated).
  --include-tag INCLUDE_TAGS
                        Limit to specific DataExportTag values (can be
                        repeated).
  --yes
  --embedded-data-only
  --allow-dangerous
  --scope SCOPE         Scope filter expression (qid:<QID>,
                        tag:<DataExportTag>; supports AND/OR/()). See
                        docs/reference/scope-semantics.md.
  --allow-drift         Proceed even if cached survey differs from the live
                        API
```

## `qsync items push`

```text
usage: qsync items push [-h] [--survey-id SURVEY_ID] [--yes] [--force-live]
                        [--force-preview] [--no-publish] [--dry-run]
                        [--scope SCOPE] [--allow-drift]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Target survey ID (omit to select interactively)
  --yes
  --force-live
  --force-preview
  --no-publish
  --dry-run
  --scope SCOPE         Scope filter expression (qid:<QID>,
                        tag:<DataExportTag>; supports AND/OR/()). See
                        docs/reference/scope-semantics.md.
  --allow-drift         Proceed even if cached survey differs from the live
                        API
```

## `qsync sync`

```text
usage: qsync sync [-h] [--survey-id SURVEY_ID] [--all]
                  [--dimensions DIMENSIONS] [--scope SCOPE] [--per-dimension]
                  [--yes] [--pending-action {push,discard,abort}] [--force-live]
                  [--force-preview] [--skip-publish] [--refresh-workbooks]
                  [--skip-refresh] [--allow-drift] [--json]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Target survey ID (omit to scan all focal surveys)
  --all                 Process all focal surveys without prompting (for
                        automation)
  --dimensions DIMENSIONS
                        Comma-separated dimensions to sync (default: auto-
                        detect)
  --scope SCOPE         Scope filter expression passed to per-dimension
                        workflows where supported (items/js/translations). See
                        docs/reference/scope-semantics.md.
  --per-dimension       Preview and approve each dimension separately
                        (default: batch per-survey)
  --yes                 Skip all confirmation prompts (non-interactive)
  --pending-action {push,discard,abort}
                        If staged pending changes exist when running with
                        --yes: push/discard/abort (default: abort)
  --force-live          Force push despite live responses
  --force-preview       Suppress preview-only response warnings
  --skip-publish        Skip auto-publish step (no version snapshot)
  --refresh-workbooks   Refresh Excel workbooks after successful sync (runs
                        qsync items pull)
  --skip-refresh        (Legacy/deprecated) Refresh is disabled by default;
                        use --refresh-workbooks to enable
  --allow-drift         Proceed even if cached survey differs from the live
                        API
  --json                Emit machine-readable JSON when blocked by pending
                        changes
```

### CI Output (JSON)

When running `qsync sync --yes --pending-action abort --json`, blocked runs emit
JSON to stdout and exit non-zero. The payload includes:

- `error`
- `survey_id`
- `pending_dims`
- `pending_summary`
- `next_commands` (includes `interactive_review`, `push_all`, `discard_all`, `pending_inspect`, `push_by_dimension`)
- `next_commands` preserve `--scope ...` when the original run included `--scope`

## `qsync survey`

```text
usage: qsync survey [-h]
                    {label,focal,list,copy,parity-check,copy-cross-account,rename,delete,inventory,prepare,add-embedded-field,remove-embedded-field,rename-embedded-field,pull,cleanup-embedded-data,prolific-auth,publish,activate,deactivate,versions,version-fetch,rollback,inspect-question,push-question,export-responses,export-translation,master,menu}
                    ...

positional arguments:
  {label,focal,list,copy,parity-check,copy-cross-account,rename,delete,inventory,prepare,add-embedded-field,remove-embedded-field,rename-embedded-field,pull,cleanup-embedded-data,prolific-auth,publish,activate,deactivate,versions,version-fetch,rollback,inspect-question,push-question,export-responses,export-translation,master,menu}
    label               Print '<SurveyID> - <Name>' using
                        surveys/inventory.csv (legacy:
                        surveys/qualtrics_surveys.csv)
    focal               List SurveyIDs marked focal in surveys/inventory.csv
                        (legacy: surveys/qualtrics_surveys.csv)
    list                List all surveys
    copy                Copy a survey
    parity-check        Compare two surveys for parity (flow/QID/tag-lite; optional deep)
    copy-cross-account  Copy a survey from one Qualtrics account to another
    rename              Rename a survey
    delete              Delete survey(s)
    inventory           Refresh the Qualtrics survey inventory cache
    prepare             Hydrate local editing surfaces for one or more surveys
                        (pull-only)
    add-embedded-field  Stage a new embedded data field in SurveyFlow
                        (requires qsync push)
    remove-embedded-field
                        Stage removal of an embedded data field in SurveyFlow
                        (requires qsync push)
    rename-embedded-field
                        Stage renaming an embedded data field in SurveyFlow
                        (requires qsync push)
    pull                Download a survey definition JSON to local cache
    cleanup-embedded-data
                        Remove duplicate embedded data placeholder rows in
                        SurveyFlow
    prolific-auth
                        Set (or append) a Prolific authenticity-check HTML
                        snippet in SurveyOptions.Header
    publish             Publish staged survey-definition changes (create a
                        published version)
    activate            Activate a survey (set isActive=true)
    deactivate          Deactivate a survey (set isActive=false)
    versions            List survey-definition versions for a survey
    version-fetch       Fetch a specific survey-definition version (by
                        VersionID)
    rollback            Restore question(s) from a historical version and
                        publish the restore
    inspect-question    Print a cached question payload from surveys/ (no API
                        calls)
    push-question       Push a single question from cached survey JSON to
                        Qualtrics
    export-responses    Export survey responses to CSV
    export-translation  Export survey content to a Word document for
                        translation validation
    master              Manage survey master (focal-only bulk editing)
    menu                Interactive survey admin menu

options:
  -h, --help            show this help message and exit
```

## `qsync survey menu`

```text
usage: qsync survey menu [-h]

options:
  -h, --help  show this help message and exit
```

## `qsync survey inventory`

```text
usage: qsync survey inventory [-h] [--focal | --full] [--survey-id SURVEY_IDS]
                              [--dry-run]

options:
  -h, --help            show this help message and exit
  --focal               Fetch response counts for focal surveys
  --full                Fetch response counts for all surveys
  --survey-id SURVEY_IDS
                        Limit refresh to specific survey ID(s) (repeatable,
                        comma-separated)
  --dry-run             Print stats without writing to disk
```

## `qsync survey list`

```text
usage: qsync survey list [-h] [--account ACCOUNT] [name_pattern]

positional arguments:
  name_pattern  Optional regex to match survey names (case-insensitive)

options:
  -h, --help  show this help message and exit
  --account ACCOUNT
              Use credentials from `.env.<account>` under the workspace root
              (API-only; skips inventory-based ordering).
```

## `qsync survey pull`

```text
usage: qsync survey pull [-h] [--survey-id SURVEY_ID] [--dest DEST] [--account ACCOUNT]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Qualtrics survey ID to download (omit to select
                        interactively)
  --dest DEST           Destination directory (default: surveys/, or surveys/.<account>/ for
                        --account mode)
                        All --account writes land in surveys/.<account>/ unless
                        --dest is explicitly set.
  --account ACCOUNT     Use credentials from `.env.<account>` under the workspace root.
```

## `qsync survey prolific-auth`

```text
usage: qsync survey prolific-auth [-h] [--survey-id SURVEY_ID]
                                  [--snippet SNIPPET] [--file FILE]
                                  [--mode {append,replace}] [--yes] [--dry-run]
                                  [--print-current] [--no-validate]
                                  [--no-publish] [--no-activate]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Qualtrics survey ID to update (omit to select
                        interactively or enter manually)
  --snippet SNIPPET     HTML snippet to set (useful for scripting; otherwise
                        you'll be prompted to paste)
  --file FILE           Read the snippet from a file path (UTF-8)
  --mode {append,replace}
                        How to apply the snippet when Header already exists
                        (default: prompt; non-interactive requires this or
                        --yes)
  --yes                 Skip prompts and use the recommended mode (replace if
                        Prolific is already present; otherwise append)
  --dry-run             Preview the change without calling the API
  --print-current       Print the current SurveyOptions.Header and exit
  --no-validate         Skip Prolific-specific snippet validation checks
  --no-publish          Skip auto-publish after writing the header (by
                        default, qsync publishes so changes are immediately
                        live)
  --no-activate         Skip auto-activate after updating the header (by
                        default, qsync sets isActive=true)
```

## `qsync survey copy`

```text
usage: qsync survey copy [-h] [--from-qsf FROM_QSF]
                         [--project-category PROJECT_CATEGORY]
                         [--language LANGUAGE] [--force-duplicate]
                         [--generate-qsf]
                         [source_survey_id] [name]

positional arguments:
  source_survey_id      Existing Qualtrics survey ID to copy
  name                  Name for the new survey

options:
  -h, --help            show this help message and exit
  --from-qsf FROM_QSF   Import from local QSF file instead of Qualtrics
  --project-category PROJECT_CATEGORY
                        Optional project category
  --language LANGUAGE   Base language for the new survey
  --force-duplicate     Allow duplicate names
  --generate-qsf        Generate QSF locally only
```

## `qsync survey parity-check`

```text
usage: qsync survey parity-check [-h] --a A --b B [--deep]

options:
  -h, --help  show this help message and exit
  --a A       Survey ID A
  --b B       Survey ID B
  --deep      Run deep parity against survey-definitions JSON (strict; ignores only cross-account volatile fields).
```

## `qsync survey copy-cross-account`

```text
usage: qsync survey copy-cross-account [-h] [--target-api-key TARGET_API_KEY]
                                       [--target-base-url TARGET_BASE_URL]
                                       [--target-account TARGET_ACCOUNT]
                                       [--source-api-key SOURCE_API_KEY]
                                       [--source-base-url SOURCE_BASE_URL]
                                       [--source-account SOURCE_ACCOUNT]
                                       [--activate] [--publish]
                                       [--publish-description PUBLISH_DESCRIPTION]
                                       [--force-overwrite] [--yes]
                                       [--no-translations]
                                       [--verify] [--verify-deep]
                                       source_survey_id new_name

positional arguments:
  source_survey_id      Survey ID to copy from source account
  new_name              Name for the survey in target account

options:
  -h, --help            show this help message and exit
  --target-api-key TARGET_API_KEY
                        API key for target Qualtrics account (or set
                        TARGET_X-API-TOKEN in env/.env)
  --target-base-url TARGET_BASE_URL
                        Base URL for target Qualtrics account (e.g.,
                        iad1.qualtrics.com) (or set TARGET_QUALTRICS_BASE_URL
                        in env/.env)
  --target-account TARGET_ACCOUNT
                        Load target credentials from `.env.<account>` under the
                        workspace root (overrides TARGET_* defaults; explicit
                        --target-* flags still win).
  --source-api-key SOURCE_API_KEY
                        API key for source account (optional; defaults to
                        .env)
  --source-base-url SOURCE_BASE_URL
                        Base URL for source account (optional; defaults to
                        .env)
  --source-account SOURCE_ACCOUNT
                        Load source credentials from `.env.<account>` under the
                        workspace root (explicit --source-* flags still win).
  --activate            Activate the survey after copying
  --publish             Publish the survey after copying (creates version)
  --publish-description PUBLISH_DESCRIPTION
                        Description for published version (max 140 chars,
                        implies --publish)
  --force-overwrite     If name exists in target, delete and replace. WARNING:
                        this permanently deletes the target survey (including
                        its version/publish history) and the replacement will
                        have a NEW SurveyID.
  --yes                 Skip confirmation prompt
  --no-translations     Do not copy survey translations (languages + strings)
                        (default: copy translations).
  --verify              After copy, verify parity (QIDs/flow/tags) and
                        translations (best-effort); exits non-zero on mismatch.
  --verify-deep         After copy, verify deep parity against survey-definitions JSON (strict; ignores only cross-account volatile fields); exits non-zero on mismatch.
```

## `qsync survey rename`

```text
usage: qsync survey rename [-h] [survey_id] [new_name]

positional arguments:
  survey_id   Qualtrics survey ID to rename
  new_name    New name for the survey

options:
  -h, --help  show this help message and exit
```

## `qsync survey delete`

```text
usage: qsync survey delete [-h] [--account ACCOUNT] survey_ids [survey_ids ...]

positional arguments:
  survey_ids  One or more Survey IDs to delete

options:
  -h, --help  show this help message and exit
  --account ACCOUNT
              Use credentials from `.env.<account>` under the workspace root.
```

## `qsync survey publish`

```text
usage: qsync survey publish [-h] [--survey-id SURVEY_ID]
                            [--description DESCRIPTION] [--dry-run]
                            [--retry-attempts RETRY_ATTEMPTS]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Qualtrics survey ID to publish (omit to select
                        interactively)
  --description DESCRIPTION
                        Version description recorded in Qualtrics (max 140
                        chars)
  --dry-run             Show the request without calling the API
  --retry-attempts RETRY_ATTEMPTS
                        Retry the publish operation this many times (in
                        addition to built-in HTTP retries).
```

## `qsync survey versions`

```text
usage: qsync survey versions [-h] [--survey-id SURVEY_ID] [--limit LIMIT]
                             [--json]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Qualtrics survey ID (omit to select interactively)
  --limit LIMIT         Limit the number of versions shown (newest-first).
                        Default: show all returned.
  --json                Output as JSON for automation
```

## `qsync survey version-fetch`

```text
usage: qsync survey version-fetch [-h] [--survey-id SURVEY_ID] --version-id
                                  VERSION_ID [--format {json,qsf}]
                                  [--output OUTPUT] [--json]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Qualtrics survey ID (omit to select interactively)
  --version-id VERSION_ID
                        Qualtrics VersionID (from `qsync survey versions`)
  --format {json,qsf}   Response format (qsf returns a QSF-like JSON payload).
  --output OUTPUT       Write the fetched payload to this file path (JSON).
  --json                Print the full fetched payload as JSON
```

## `qsync survey rollback`

```text
usage: qsync survey rollback [-h] [--survey-id SURVEY_ID] --version-id
                             VERSION_ID --question-id QUESTION_ID [--dry-run]
                             [--no-publish] [--description DESCRIPTION]
                             [--force-live] [--yes]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Survey ID (omit to select interactively)
  --version-id VERSION_ID
                        Qualtrics VersionID (from `qsync survey versions`)
  --question-id QUESTION_ID
                        Comma-separated list of QIDs to restore (e.g.,
                        QID1,QID7).
  --dry-run             Validate inputs and show the plan without writing to
                        Qualtrics.
  --no-publish          Restore questions but do not publish the survey
                        afterwards.
  --description DESCRIPTION
                        Publish description override (max 140 chars).
  --force-live          Allow rollback even if finished responses exist.
  --yes                 Skip confirmation prompts.
```

## `qsync survey master`

```text
usage: qsync survey master [-h] {pull,preview,apply,push} ...

positional arguments:
  {pull,preview,apply,push}
    pull                Pull focal survey snapshots and generate master CSV
    preview             Preview changes that would be applied by master apply
    apply               Apply changes from master CSV to Qualtrics
    push                Publish surveys after applying changes

options:
  -h, --help            show this help message and exit
```

## `qsync survey master pull`

```text
usage: qsync survey master pull [-h] [--mapping-csv MAPPING_CSV]
                                [--survey-id SURVEY_IDS]

options:
  -h, --help            show this help message and exit
  --mapping-csv MAPPING_CSV
                        Path to a Qualtrics API field mapping CSV for Survey
                        Master (overrides packaged defaults)
  --survey-id SURVEY_IDS
                        Limit to specific survey ID(s) (repeatable); default:
                        all focal surveys
```

## `qsync survey master preview`

```text
usage: qsync survey master preview [-h] [--mapping-csv MAPPING_CSV] [--detail]
                                   [--survey-id SURVEY_ID]
                                   [--format {text,json}] [--tag TAGS]

options:
  -h, --help            show this help message and exit
  --mapping-csv MAPPING_CSV
                        Path to a Qualtrics API field mapping CSV for Survey
                        Master (overrides packaged defaults)
  --detail              Show detailed per-field changes
  --survey-id SURVEY_ID
                        Preview only this specific survey (by SurveyID)
  --format {text,json}  Output format (default: text)
  --tag TAGS            Filter surveys by tag (e.g., --tag component=pre --tag
                        stage=prod)
```

## `qsync survey master apply`

```text
usage: qsync survey master apply [-h] [--mapping-csv MAPPING_CSV]
                                 [--allow-dangerous] [--force]
                                 [--survey-id SURVEY_ID] [--skip-drift]
                                 [--dry-run] [--tag TAGS]

options:
  -h, --help            show this help message and exit
  --mapping-csv MAPPING_CSV
                        Path to a Qualtrics API field mapping CSV for Survey
                        Master (overrides packaged defaults)
  --allow-dangerous     Allow changes to dangerous fields (isActive, redirect
                        URLs, etc.)
  --force               Override drift detection (proceed even if values
                        changed since last pull)
  --survey-id SURVEY_ID
                        Apply only to this specific survey (by SurveyID);
                        useful for testing
  --skip-drift          Skip drift detection (faster but riskier; assumes no
                        concurrent changes)
  --dry-run             Preview what would be applied without actually writing
                        changes
  --tag TAGS            Filter surveys by tag (e.g., --tag component=pre --tag
                        stage=prod)
```

## `qsync survey label`

```text
usage: qsync survey label [-h] [--survey-id SURVEY_ID]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Survey ID (omit to select interactively)
```

## `qsync survey focal`

```text
usage: qsync survey focal [-h] [--newline]

options:
  -h, --help  show this help message and exit
  --newline   Print one survey ID per line (default: space-delimited)
```

## `qsync survey inspect-question`

```text
usage: qsync survey inspect-question [-h] [--survey-id SURVEY_ID]
                                     --question-id QUESTION_ID
                                     [--survey-file SURVEY_FILE]
                                     [--field FIELD] [--raw]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Qualtrics survey ID (omit to select interactively)
  --question-id QUESTION_ID
  --survey-file SURVEY_FILE
                        Path to survey JSON (default: auto-detect from
                        surveys/)
  --field FIELD         Print only a single field from the question payload
                        (e.g., QuestionJS, QuestionText)
  --raw                 When used with --field and the field is a string,
                        print without JSON quoting
```

## `qsync survey push-question`

```text
usage: qsync survey push-question [-h] [--survey-id SURVEY_ID] --question-id
                                  QUESTION_ID [--survey-file SURVEY_FILE]
                                  [--dry-run] [--force-live] [--yes]
                                  [--show-diff] [--no-publish]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Target Qualtrics survey ID (omit to select
                        interactively)
  --question-id QUESTION_ID
                        Question ID to push (e.g., QID15)
  --survey-file SURVEY_FILE
                        Path to survey JSON (default: auto-detect from
                        surveys/)
  --dry-run             Show diff without pushing
  --force-live          Allow push even if survey has live responses
  --yes, -y             Skip confirmation prompt
  --show-diff           Always show diff (even with --yes)
  --no-publish          Skip publishing the survey after pushing the question
                        definition
```

## `qsync survey export-responses`

```text
usage: qsync survey export-responses [-h] [--survey-id SURVEY_ID]
                                     [--output OUTPUT]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Qualtrics survey ID to export responses from (omit to
                        select interactively)
  --output OUTPUT       Output directory (default: responses/)
```

## `qsync js`

```text
usage: qsync js [-h] {pull,preview,stage,apply,push} ...

positional arguments:
  {pull,preview,stage,apply,push}
    pull                Rebuild survey_qid_js_map.csv and ensure mappings
                        exist
    preview             Preview differences between local JS and cached
                        QuestionJS
    stage               Sync cached QuestionJS with local survey_js/core files
    apply               [DEPRECATED: use 'stage'] Sync cached QuestionJS with
                        local survey_js/core files
    push                Push cached QuestionJS for mapped QIDs to Qualtrics

options:
  -h, --help            show this help message and exit
```

## `qsync js pull`

```text
usage: qsync js pull [-h] [--survey-id SURVEY_ID] [--mapping MAPPING]
                     [--include-qid INCLUDE_QIDS] [--include-tag INCLUDE_TAGS]
                     [--include-js INCLUDE_JS] [--dry-run]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Target Qualtrics Survey ID (omit to select
                        interactively)
  --mapping MAPPING     Path to survey_qid_js_map.csv (default:
                        survey_js/survey_qid_js_map.csv)
  --include-qid INCLUDE_QIDS
                        Limit to specific Qualtrics QIDs (can be repeated).
  --include-tag INCLUDE_TAGS
                        Limit to specific DataExportTag values (can be
                        repeated).
  --include-js INCLUDE_JS
                        Limit JS operations to specific core filenames.
  --dry-run             Show a summary without writing the CSV
```

## `qsync js preview`

```text
usage: qsync js preview [-h] [--survey-id SURVEY_ID] [--mapping MAPPING]
                        [--include-qid INCLUDE_QIDS]
                        [--include-tag INCLUDE_TAGS] [--include-js INCLUDE_JS]
                        [--show-equal] [--detailed] [--scope SCOPE]
                        [--allow-drift]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Target Qualtrics Survey ID (omit to select
                        interactively)
  --mapping MAPPING     Path to survey_qid_js_map.csv (default:
                        survey_js/survey_qid_js_map.csv)
  --include-qid INCLUDE_QIDS
                        Limit to specific Qualtrics QIDs (can be repeated).
  --include-tag INCLUDE_TAGS
                        Limit to specific DataExportTag values (can be
                        repeated).
  --include-js INCLUDE_JS
                        Limit JS operations to specific core filenames.
  --show-equal          Include matches with no differences
  --detailed            Print unified diffs for each pair
  --scope SCOPE         Scope filter expression (qid:<QID>,
                        tag:<DataExportTag>, js:<file>; supports AND/OR/()).
                        See docs/reference/scope-semantics.md.
  --allow-drift         Allow preview against a drifted cache without
                        prompting
```

## `qsync js stage`

```text
usage: qsync js stage [-h] [--survey-id SURVEY_ID] [--mapping MAPPING]
                      [--include-qid INCLUDE_QIDS]
                      [--include-tag INCLUDE_TAGS] [--include-js INCLUDE_JS]
                      [--dry-run] [--create-missing] [--allow-diff]
                      [--no-include-match] [--allow-drift] [--scope SCOPE]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Target Qualtrics Survey ID (omit to select
                        interactively)
  --mapping MAPPING     Path to survey_qid_js_map.csv (default:
                        survey_js/survey_qid_js_map.csv)
  --include-qid INCLUDE_QIDS
                        Limit to specific Qualtrics QIDs (can be repeated).
  --include-tag INCLUDE_TAGS
                        Limit to specific DataExportTag values (can be
                        repeated).
  --include-js INCLUDE_JS
                        Limit JS operations to specific core filenames.
  --dry-run             Compute staged entries without writing pending changes
  --create-missing      Create QuestionJS blocks when they are missing
  --allow-diff          Include substantive code diffs when staging
  --no-include-match    Skip staging when cached JS already matches
  --allow-drift         Allow staging against a drifted cache without
                        prompting
  --scope SCOPE         Scope filter expression (qid:<QID>,
                        tag:<DataExportTag>, js:<file>; supports AND/OR/()).
                        See docs/reference/scope-semantics.md.
```

## `qsync js push`

```text
usage: qsync js push [-h] [--survey-id SURVEY_ID] [--mapping MAPPING]
                     [--include-qid INCLUDE_QIDS] [--include-tag INCLUDE_TAGS]
                     [--include-js INCLUDE_JS] [--include-trash] [--dry-run]
                     [--push-all] [--force-live] [--force-preview] [--yes]
                     [--no-publish] [--scope SCOPE] [--allow-drift]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Target Qualtrics Survey ID (omit to select
                        interactively)
  --mapping MAPPING     Path to survey_qid_js_map.csv (default:
                        survey_js/survey_qid_js_map.csv)
  --include-qid INCLUDE_QIDS
                        Limit to specific Qualtrics QIDs (can be repeated).
  --include-tag INCLUDE_TAGS
                        Limit to specific DataExportTag values (can be
                        repeated).
  --include-js INCLUDE_JS
                        Limit JS operations to specific core filenames.
  --include-trash       Also push QIDs that live in Trash blocks
  --dry-run             Show which QIDs would be pushed without calling the
                        API
  --push-all            Ignore staged JS and push all mapped QIDs (still
                        filtered by include flags).
  --force-live          Allow JS pushes even if finished responses exist
  --force-preview       Force push to preview database even with responses
  --yes                 Skip confirmation prompts for JS pushes
  --no-publish          Skip publishing the survey after pushing QuestionJS
                        updates
  --scope SCOPE         Scope filter expression (qid:<QID>,
                        tag:<DataExportTag>, js:<file>; supports AND/OR/()).
                        See docs/reference/scope-semantics.md.
  --allow-drift         Proceed even if cached survey differs from the live
                        API
```

## `qsync eos`

```text
usage: qsync eos [-h]
                {pull,preview,repair,stage,apply,push,references,clone-shared}
                ...

positional arguments:
  {pull,preview,repair,stage,apply,push,references,clone-shared}
    pull                Pull EOS library messages referenced by a survey into
                        contents/
    preview             Preview differences between local EOS message files
                        and live API content
    repair              Re-fetch EOS library messages for a survey and update
                        local files
    stage               Stage EOS message pushes under surveys/pending/eos/
                        (no API writes)
    apply               [DEPRECATED: use 'stage'] Stage EOS message pushes
                        under surveys/pending/eos/ (no API writes)
    push                Push staged EOS message updates to Qualtrics (requires
                        --yes)
    references          List cached surveys that reference a given EOS library
                        message (local scan)
    clone-shared        Clone shared EOS library messages and rewrite SurveyFlow
                        to reference the clones (API writes)

options:
  -h, --help            show this help message and exit
```

## `qsync eos pull`

```text
usage: qsync eos pull [-h] [--survey-id SURVEY_ID]
                      [--allow-shared-message-edit] [--include-backups-scan]
                      [--yes]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Target survey ID (omit to select interactively)
  --allow-shared-message-edit
                        Allow edits even if a library message is detected as
                        shared (local scan only).
  --include-backups-scan
                        Also scan surveys/backups when detecting shared
                        message usage (local-only).
  --yes                 Skip interactive confirmations (required for push).
```

## `qsync eos preview`

```text
usage: qsync eos preview [-h] [--survey-id SURVEY_ID]
                         [--allow-shared-message-edit]
                         [--include-backups-scan] [--yes] [--detailed]
                         [--allow-drift] [--scope SCOPE]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Target survey ID (omit to select interactively)
  --allow-shared-message-edit
                        Allow edits even if a library message is detected as
                        shared (local scan only).
  --include-backups-scan
                        Also scan surveys/backups when detecting shared
                        message usage (local-only).
  --yes                 Skip interactive confirmations (required for push).
  --detailed            Include unified diffs for changed keys
  --allow-drift         Allow preview against a drifted cache without
                        prompting
  --scope SCOPE         Scope filter expression (accepted but currently
                        ignored for EOS). See
                        docs/reference/scope-semantics.md.
```

## `qsync eos stage`

```text
usage: qsync eos stage [-h] [--survey-id SURVEY_ID]
                       [--allow-shared-message-edit] [--include-backups-scan]
                       [--yes] [--allow-destructive] [--scope SCOPE]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Target survey ID (omit to select interactively)
  --allow-shared-message-edit
                        Allow edits even if a library message is detected as
                        shared (local scan only).
  --include-backups-scan
                        Also scan surveys/backups when detecting shared
                        message usage (local-only).
  --yes                 Skip interactive confirmations (required for push).
  --allow-destructive   Allow destructive key deletions (missing message keys)
                        for push.
  --scope SCOPE         Scope filter expression (accepted but currently
                        ignored for EOS). See
                        docs/reference/scope-semantics.md.
```

## `qsync eos push`

```text
usage: qsync eos push [-h] [--survey-id SURVEY_ID]
                      [--allow-shared-message-edit] [--include-backups-scan]
                      [--yes] [--dry-run] [--force-live] [--force-preview]
                      [--allow-drift] [--no-publish] [--scope SCOPE]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Target survey ID (omit to select interactively)
  --allow-shared-message-edit
                        Allow edits even if a library message is detected as
                        shared (local scan only).
  --include-backups-scan
                        Also scan surveys/backups when detecting shared
                        message usage (local-only).
  --yes                 Skip interactive confirmations (required for push).
  --dry-run             Show which messages would be pushed without calling
                        the API
  --force-live          Allow pushes even if finished responses exist
  --force-preview       Force push to preview database even with responses
  --allow-drift         Proceed even if cached EOS messages differ from the
                        live API
  --no-publish          Skip publishing the survey after pushing EOS updates
  --scope SCOPE         Scope filter expression (accepted but currently
                        ignored for EOS). See
                        docs/reference/scope-semantics.md.
```

## `qsync eos clone-shared`

```text
usage: qsync eos clone-shared [-h] [--survey-id SURVEY_ID]
                              [--allow-shared-message-edit]
                              [--include-backups-scan] [--yes]
                              [--allow-non-smoke] [--dry-run] [--allow-drift]
                              [--no-publish]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Target survey ID (omit to select interactively)
  --allow-shared-message-edit
                        Allow edits even if a library message is detected as
                        shared (local scan only).
  --include-backups-scan
                        Also scan surveys/backups when detecting shared
                        message usage (local-only).
  --yes                 Skip interactive confirmations (required for push).
  --allow-non-smoke     Allow running on surveys whose names do not include
                        'smoke' (dangerous).
  --dry-run             Show what would be cloned/rewired without API writes.
  --allow-drift         Proceed even if cached survey differs from the live API.
  --no-publish          Skip publishing the survey after rewriting SurveyFlow.
```

## `qsync eos repair`

```text
usage: qsync eos repair [-h] [--survey-id SURVEY_ID]
                        [--allow-shared-message-edit] [--include-backups-scan]
                        [--yes]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Target survey ID (omit to select interactively)
  --allow-shared-message-edit
                        Allow edits even if a library message is detected as
                        shared (local scan only).
  --include-backups-scan
                        Also scan surveys/backups when detecting shared
                        message usage (local-only).
  --yes                 Skip interactive confirmations (required for push).
```

## `qsync flow`

```text
usage: qsync flow [-h] {pull,preview,stage,push} ...

positional arguments:
  {pull,preview,stage,push}
    pull                Pull survey flow from Qualtrics and save as YAML
    preview             Preview differences between local flow YAML and cached
                        baseline
    stage               Stage flow changes into pending cache (no API writes)
    push                Push staged flow changes to Qualtrics

options:
  -h, --help            show this help message and exit
```

## `qsync flow pull`

```text
usage: qsync flow pull [-h] [--survey-id SURVEY_ID] [--yes] [--force]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Target survey ID (omit to select interactively)
  --yes                 Skip interactive confirmations.
  --force               Overwrite existing YAML even if it has local changes
```

## `qsync flow preview`

```text
usage: qsync flow preview [-h] [--survey-id SURVEY_ID] [--yes] [--verbose]
                          [--visual] [--allow-drift]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Target survey ID (omit to select interactively)
  --yes                 Skip interactive confirmations.
  --verbose             Include detailed diff output with old/new values
  --visual              Generate Mermaid diagrams for visual diff
                        (placeholder)
  --allow-drift         Allow preview against a drifted baseline without
                        prompting
```

## `qsync flow stage`

```text
usage: qsync flow stage [-h] [--survey-id SURVEY_ID] [--yes] [--allow-drift]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Target survey ID (omit to select interactively)
  --yes                 Skip interactive confirmations.
  --allow-drift         Allow staging even if remote has drifted
```

## `qsync flow push`

```text
usage: qsync flow push [-h] [--survey-id SURVEY_ID] [--yes] [--force-live]
                       [--force-preview] [--allow-drift] [--no-publish]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Target survey ID (omit to select interactively)
  --yes                 Skip interactive confirmations.
  --force-live          Allow pushes even if finished responses exist
  --force-preview       Force push to preview database even with responses
  --allow-drift         Proceed even if flow baseline differs from the live
                        API
  --no-publish          Skip publishing the survey after pushing flow updates
```

## `qsync translations`

```text
usage: qsync translations [-h]
                          {languages,preview,apply,doctor,drift,pack,push}
                          ...

positional arguments:
  {languages,preview,apply,stage,doctor,drift,pack,push}
    languages           List or enable survey languages
    preview             Preview workbook vs cached survey definition
                        translations
    apply               (deprecated) Stage workbook translations (use
                        `qsync translations stage`)
    stage               Stage workbook translations into pending changes
    doctor              Run translation validation checks
    drift               Report drift between repo and Qualtrics translations
    pack                Create a translator pack zip (docx + translations)
    push                Push staged translations via survey definition

options:
  -h, --help            show this help message and exit
```

## `qsync translations push`

```text
usage: qsync translations push [-h] [--survey-id SURVEY_ID]
                               [--language LANGUAGE] [--languages LANGUAGES]
                               [--mode {validate,apply}] [--validate]
                               [--dry-run] [--yes] [--detailed] [--force-live]
                               [--force-preview] [--allow-drift]
                               [--no-publish] [--use-pending] [--scope SCOPE]

options:
  -h, --help            show this help message and exit
  --survey-id SURVEY_ID
                        Target survey ID (omit to select interactively)
  --language LANGUAGE   Language code to push (repeatable)
  --languages LANGUAGES
                        Comma-separated language codes to push
  --mode {validate,apply}
                        (deprecated) validate: run checks only; apply: push to
                        Qualtrics
  --validate            Run checks only (no API writes)
  --dry-run             Alias for --mode validate
  --yes                 Skip confirmation prompt when pushing
  --detailed            Include unified diffs for changed translation keys
  --force-live          Allow pushes even if finished responses exist
  --force-preview       Allow pushes that affect preview/test responses
                        without extra confirmation
  --allow-drift         Proceed even if the cached survey definition differs
                        from live
  --no-publish          Skip publishing the survey after push
  --use-pending         If staged changes exist and Excel differs, push staged
                        changes instead of re-staging from Excel
  --scope SCOPE         Scope filter expression (qid:<QID>,
                        tag:<DataExportTag>; supports AND/OR/()). See
                        docs/reference/scope-semantics.md.
```
