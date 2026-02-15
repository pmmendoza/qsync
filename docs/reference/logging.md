# qsync Logging Guide

_Migrated from `appendices/logging_guide.md` (monorepo) so the standalone `qsync` repo can be self-contained._

## Overview

`qsync` maintains detailed operation logs to provide an audit trail of API interactions and local operations. This guide explains the logging infrastructure, available commands, and how to troubleshoot issues.

## Log File Locations

### Primary Log
- **File**: `logs/qualtrics_push.log`
- **Format**: JSONL (JSON Lines) - one JSON object per line
- **Contains**: All qsync operations (API calls, local operations, errors)
- **Location**: Relative to the workspace root (`QSYNC_ROOT` / `--root`)

## Environment Variables

### Control Logging Behavior

| Variable | Values | Effect |
|----------|--------|--------|
| `QSYNC_LOG_DISABLED` | `1`, `true`, `yes` | Disable all logging |
| `QSYNC_LOG_DIR` | `/path/to/dir` | Override log directory (default: `logs/`) |
| `NO_COLOR` | `1`, `true`, `yes` | Disable terminal colors in output |

### Examples

```bash
# Disable logging for a single command
QSYNC_LOG_DISABLED=1 qsync survey delete SV_xxx

# Use custom log directory
export QSYNC_LOG_DIR=~/my-logs
qsync survey copy --source SV_xxx --name "My Copy"
```

## Log Analysis Commands

Qsync provides built-in commands to view and analyze logs without external tools like `jq`.

### View Recent Operations

```bash
# Show last 10 operations (default)
qsync logs recent

# Show last 5 operations
qsync logs recent --limit 5

# Show last 20 operations
qsync logs recent --limit 20
```

**Example Output:**
```
[344] 2026-01-10 12:29:06 UTC
    Action:     qsync.master.apply
    Survey:     SV_5BeVXRVDCgJCsPI
    Method:     PUT
    Status:     ✓ 200
    User:       pm
```

### View Recent Errors

```bash
# Show last 10 errors (default)
qsync logs errors

# Show last 5 errors
qsync logs errors --limit 5
```

**Example Output:**
```
[102] 2026-01-10 11:45:32 UTC
    Action:     qsync.survey.delete
    Survey:     SV_abcd1234
    Method:     DELETE
    Status:     ✗ 404
    Error:      HTTPError: 404 Not Found
    Detail:     Survey does not exist
```

### View Summary Statistics

```bash
qsync logs stats
```

**Example Output:**
```
Operation Statistics:

  Total operations:    344
  Successful:          243 ✓
  Failed:              101 ✗
  Error rate:          29.3%

Top Actions:

  qsync.survey.push.question                 154
  scripts.copy_survey.push_definition        60
  qsync.survey.delete                        42
  qsync.master.apply                         25
  ...

Status Codes:

  200           243
  404            45
  500            12
```

### Filter by Survey ID

```bash
# Show all operations for a specific survey
qsync logs survey SV_5AsKyAO5QqswBcq

# Limit results
qsync logs survey SV_5AsKyAO5QqswBcq --limit 10
```

### Filter by Action Type

```bash
# Show all survey operations
qsync logs action qsync.survey

# Show all master workflow operations
qsync logs action qsync.master

# Show specific action
qsync logs action qsync.survey.delete --limit 5
```

### Filter by Timestamp

```bash
# Show operations since a specific time
qsync logs since "2026-01-10T12:00:00"

# ISO 8601 format supported
qsync logs since "2026-01-10T12:00:00+00:00"
```

## Log Entry Structure

Each log entry is a JSON object with the following fields:

```jsonl
{
  "timestamp": "2026-01-10T15:29:06.123456+00:00",
  "hostname": "VU-MWP-CLJ7P2GMWR",
  "user": "pm",
  "git_commit": "abc123def456",
  "script": "qsync",
  "action": "qsync.survey.copy",
  "method": "POST",
  "path": "https://vuamsterdam.eu.qualtrics.com/API/v3/surveys",
  "survey_id": "SV_5AsKyAO5QqswBcq",
  "status": 200,
  "meta": {
    "source_id": "SV_001",
    "new_name": "My Survey Copy"
  }
}
```

### Field Descriptions

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | string | ISO 8601 timestamp with timezone (UTC) |
| `hostname` | string | System hostname where command ran |
| `user` | string | OS username |
| `git_commit` | string\|null | Current git HEAD commit hash (if in git repo) |
| `script` | string | Script or command name (`qsync`, script name, etc.) |
| `action` | string | Hierarchical action identifier (e.g., `qsync.survey.copy`) |
| `method` | string | HTTP method (`GET`, `POST`, `PUT`, `DELETE`) or `LOCAL` |
| `path` | string | Full API URL or local path |
| `survey_id` | string\|null | Target survey ID (if applicable) |
| `status` | int\|null | HTTP status code (200, 404, etc.) or null for errors |
| `error` | object\|null | Error details (type, message, detail, reason, retry_count, recoverable, suggestion, docs_url) |
| `meta` | object | Operation-specific metadata |

### Error Entry Example

```jsonl
{
  "timestamp": "2026-01-10T15:45:12.789012+00:00",
  "action": "qsync.survey.delete",
  "method": "DELETE",
  "path": "https://vuamsterdam.eu.qualtrics.com/API/v3/surveys/SV_xxx",
  "survey_id": "SV_xxx",
  "status": 404,
  "error": {
    "type": "HTTPError",
    "message": "404 Not Found",
    "detail": "The requested survey does not exist",
    "reason": "Not Found",
    "retry_count": 0,
    "recoverable": false,
    "suggestion": "Verify the survey ID or endpoint; refresh inventory if needed.",
    "docs_url": "docs/troubleshooting.md"
  }
}
```

## Manual Log Analysis with `jq`

While built-in commands are recommended, you can also use `jq` for advanced queries:

### Count Operations by Action

```bash
jq -r '.action' logs/qualtrics_push.log | sort | uniq -c | sort -rn
```

### Show All Errors

```bash
jq 'select(.status >= 400 or .error != null)' logs/qualtrics_push.log
```

### Filter by Date Range

```bash
jq 'select(.timestamp >= "2026-01-10" and .timestamp < "2026-01-11")' logs/qualtrics_push.log
```

### Extract Specific Fields

```bash
jq '{timestamp, action, survey_id, status}' logs/qualtrics_push.log
```

### Pretty-Print Recent Entry

```bash
tail -1 logs/qualtrics_push.log | jq
```

## Operation Confirmations

All write operations print a confirmation message showing that the operation was logged:

```
[copy] Successfully copied SV_001 to My Survey Copy (SV_002)
[copy] Logged to logs/qualtrics_push.log
[copy] View: qsync logs recent --limit 1
```

This confirms:
- ✅ Operation completed successfully
- ✅ Operation was logged to audit trail
- ✅ Quick command to view the log entry

**No confirmation is shown when:**
- Logging is disabled (`QSYNC_LOG_DISABLED=1`)
- Dry-run mode is active (`--dry-run`)

## Troubleshooting

### Log File Not Found

**Symptom**: `qsync logs recent` shows "No operations found in log"

**Cause**: Log file doesn't exist yet (no operations have been performed)

**Solution**: Perform an operation (e.g., `qsync doctor`) to create the log file

### Permissions Error

**Symptom**: `[push-log] Failed to record event: Permission denied`

**Cause**: No write permission to `logs/` directory

**Solution**: Check directory permissions or set `QSYNC_LOG_DIR` to a writable location

### Corrupted Log Entries

**Symptom**: Some log entries don't appear in commands

**Cause**: Malformed JSON in log file (e.g., incomplete lines, syntax errors)

**Solution**: Log analysis commands skip malformed lines gracefully. To identify:

```bash
# Find lines that aren't valid JSON
python3 -c "
import json
with open('logs/qualtrics_push.log') as f:
    for i, line in enumerate(f, 1):
        try:
            json.loads(line.strip())
        except json.JSONDecodeError:
            print(f'Line {i}: {line.strip()[:100]}')
"
```

### Missing Historical Data

**Symptom**: Old operations from `copy_history.log` don't appear

**Cause**: Legacy log hasn't been migrated to JSONL format

**Solution**: Run migration script:

```bash
python scripts/migrate_copy_history_to_jsonl.py

# Or dry-run to preview
python scripts/migrate_copy_history_to_jsonl.py --dry-run
```

## Best Practices

### Regular Review

```bash
# Weekly error check
qsync logs errors --limit 50

# Monthly statistics
qsync logs stats
```

### Pre-Operation Verification

```bash
# Verify operation was logged
qsync survey publish --survey-id SV_xxx --description "test"
qsync logs recent --limit 1
```

### Audit Trail for Compliance

```bash
# Export operations for specific survey
qsync logs survey SV_xxx > audit_SV_xxx.log

# Export all operations for date range
jq 'select(.timestamp >= "2026-01" and .timestamp < "2026-02")' \
  logs/qualtrics_push.log > audit_january_2026.log
```

### Backup Important Logs

```bash
# Backup before major operations
cp logs/qualtrics_push.log logs/backup_$(date +%Y%m%d).log

# Or use git
git add logs/qualtrics_push.log
git commit -m "Log snapshot before major changes"
```

## See Also

- [CLI Reference](cli.md) - Full command documentation
- [Workflows](../index.md) - Common operation workflows
- [Survey Master Guide](../workflows/survey-master.md) - Master CSV workflow logging
