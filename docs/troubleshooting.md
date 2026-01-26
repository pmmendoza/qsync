# qsync Troubleshooting

This document is intentionally short and oriented toward “first run” failures.

## 1) Verify workspace + credentials

Run:

```bash
qsync doctor
```

Key checks:
- `root` points to the directory that contains `surveys/`, `excel/`, `survey_js/`, etc.
- `QUALTRICS_BASE_URL` is host-only (e.g. `iad1.qualtrics.com`, not `https://...`).
- API token is present via `X-API-TOKEN` (preferred) or `QUALTRICS_API_KEY`.

## 2) “Arrow keys don’t work” / interactive menus missing

`qsync` uses `questionary` for interactive arrow-key menus. Menus are only available when:
- `questionary` is installed
- both `stdin` and `stdout` are TTY

Debug:

```bash
qsync doctor --json
```

If you’re running inside a non-interactive environment (CI, redirected output), use explicit flags like `--survey-id ...`.

## 3) “Workspace root not found”

If you’re running `qsync` from a directory that is *not* your workspace, pass `--root`:

```bash
qsync --root /path/to/workspace doctor
```

Or set `QSYNC_ROOT` in your environment.

## 4) Color output / diffs look “all gray”

Color output depends on terminal capabilities and the selected mode.

Try forcing color:

```bash
qsync --color always sync
```

To disable color:

```bash
qsync --color never sync
```

If your environment sets `NO_COLOR`, `qsync` will default to `--color never`.

## 5) Docs link in error messages is wrong

You can override the docs URL shown in errors via:

```bash
export QSYNC_DOCS_URL="docs/troubleshooting.md"
```
