# qsync Logging Conventions

_Migrated from `appendices/logging_conventions.md` (monorepo) so the standalone `qsync` repo can be self-contained._

## Action naming

All logged actions must follow:

```
{namespace}.{entity}.{operation}[.{detail}]
```

Rules:
- **Dots only** as separators (no underscores or hyphens).
- **Lowercase** names.
- Keep operations short and descriptive (`fetch`, `list`, `push`, `publish`, `export`, `apply`).
- Use optional detail segments for sub-operations (`export.responses.start`).

Examples:

- `qsync.survey.list`
- `qsync.survey.fetch.definition`
- `qsync.survey.export.responses.start`
- `qsync.survey.export.responses.poll`
- `qsync.survey.export.responses.download`
- `qsync.survey.push.question`
- `qsync.survey.rollback.question.put`
- `qsync.master.fetch.status`
- `qsync.master.write.metadata`
- `qsync.inventory.fetch.surveys`
- `qsync.response.stats.export.start`

## Error fields (JSONL)

When an error is logged, include:

- `retry_count` (integer)
- `recoverable` (boolean)
- `suggestion` (short actionable text)
- `docs_url` (path to relevant docs)

## Adding a new action

1. Pick a dot-only name following the pattern above.
2. Use it consistently in `send_api_request(action=...)`.
3. If the action writes data, ensure logs are enabled and include any relevant `meta`.
