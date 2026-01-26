# Push safeguards & overwrite policy

_Migrated from `appendices/` (monorepo) so the standalone `qsync` repo can be self-contained._

This document explains how `qsync` decides whether a wording or JS push is allowed. It is the source of truth for the `--force-live` and “preview responses” override flags surfaced in the CLI.

## 1. Inventory columns

`surveys/inventory.csv` is updated via `qsync survey inventory`. Relevant columns:

| Column | Meaning |
| --- | --- |
| `focal` | If `TRUE`, the survey is selected by default when commands omit `--survey-id` and operate on “focal surveys”. |
| `locked` | Hard block: pushes abort immediately when this is `TRUE`. Toggle only after coordinating with the research lead. |
| `preview_count` | Number of finished preview/test responses (determined via a JSON export where `distributionChannel == preview` or `ResponseType == "Survey Preview"`). |
| `response_count` | Number of finished real responses (`ResponseType != "Survey Preview"`). |
| `generated_at` | Timestamp (UTC) when the inventory row was last refreshed. Used to determine staleness. |

## 2. Freshness checks

When `qsync` loads a row it considers the counts trustworthy only if:

- The CSV exists and the row was found, **and**
- `generated_at` is less than ~30 minutes old **and** at least one of `preview_count` / `response_count` is non-null.

If not, `qsync` performs a lightweight `GET /API/v3/surveys/{id}` request and reads the `responseCounts` block (`generated` vs `auditable`). Those values override stale or missing counts for the current CLI invocation.

## 3. Decision matrix

| Scenario | Required flags | Behaviour |
| --- | --- | --- |
| `locked == TRUE` | *not overridable* | Abort with instructions to clear `locked` first. |
| `response_count > 0` | `--force-live` | Warn with current counts, require confirmation unless `--yes`. Applies to both items and JS pushes. |
| `response_count == 0` and `preview_count > 0` | Items: `--force-preview` (or `--force-live`)<br>JS: warning only | Wording pushes refuse to proceed without the flag; JS pushes warn and seek confirmation but don’t require an extra flag. |
| Counts missing or stale | None, but live check runs | CLI prints a note indicating a live refresh was performed. |

## 4. Prompts & logging

When a flag is required, `qsync` prints a warning summarising the counts:

```
[qsync:items] WARNING: pushing despite live responses -- 1 live / 3 preview (source: inventory, inventory @ 2025-11-19T15:11:29+00:00)
Proceed with push? [y/N]
```

- Passing `--yes` auto-confirms after printing the warning.
- All pushes log API calls to `logs/qualtrics_push.log`, so we retain an audit trail even when flags are used.

## 5. Recommended practice

1. Run `qsync survey inventory` before every push to ensure counts are fresh.
2. Use `qsync items preview` / `qsync js preview` to review diffs before invoking any overwrite flags.
3. Only use `--force-live` after the research lead approves overwriting a survey with real responses. Document the rationale in commit messages or `logs/qualtrics_push.log`.
4. If `response_count > 0` due to “test” production data, consider spinning up a fresh API-owned copy rather than overwriting the live survey.

Following these rules keeps accidental overwrites reversible and makes it clear when we intentionally override Qualtrics safeguards.
