# qsync Troubleshooting

This document is intentionally short and oriented toward “first run” failures.

## 1) Verify workspace + credentials

Run:

```bash
qsync doctor
```

Key checks:
- `root` points to the workspace root (account-root layout: `accounts/default/...`; legacy layout: `surveys/`, `excel/`, `survey_js/`, etc.).
- `QUALTRICS_BASE_URL` is host-only (e.g. `iad1.qualtrics.com`, not `https://...`).
- API token is present via `X-API-TOKEN` (preferred) or `QUALTRICS_API_KEY`.

If `qsync doctor` reports missing account-scoped files, you likely have an active account selection that does not match where your artifacts currently live. Run:

```bash
qsync account status
```

Then either clear it (`qsync account clear`) or run the missing command in the selected account context (for example `qsync survey inventory`).

## 2) pipx installs (CLI)

If you installed `qsync` with pipx:

- Ensure pipx bin is on PATH: run `pipx ensurepath` and restart your shell.
- Confirm which binary you are running: `which qsync`.
- List installed apps: `pipx list`.
- Inspect dependencies: `pipx runpip qsync list`.
- `qsync --version` prints install diagnostics (pipx vs venv, paths, git SHA when available).
- Do not mix `pip` and `pipx` installs at the same time. Uninstall one.
- Completion fallback: if `activate-global-python-argcomplete` is missing, run:
  `pipx inject --include-apps qsync argcomplete`.
- PDF extra needs system libs (WeasyPrint). Example:
  - macOS: `brew install cairo pango gdk-pixbuf libffi`
  - Ubuntu: `sudo apt-get install libcairo2 libpango-1.0-0 libgdk-pixbuf2.0-0 libffi8`
  - Package names may vary by distro.
- langcheck extra (`fasttext-wheel`) may fall back to a source build on newer Python
  versions. On Apple Silicon, prefer Python 3.11 for pipx:
  - `pipx install --python /opt/homebrew/opt/python@3.11/bin/python3.11 --include-deps "qsync[langcheck] @ git+..."`
  - or `export PIPX_DEFAULT_PYTHON=/opt/homebrew/opt/python@3.11/bin/python3.11`
- If you see `fatal error: 'istream' file not found` during a build, your Xcode Command
  Line Tools are broken. Reinstall CLT or restore the libc++ headers (see below).
- Windows: pipx installs are supported for the core CLI; PDF export is not supported on Windows yet.

## 3) “Arrow keys don’t work” / interactive menus missing

`qsync` uses `questionary` for interactive arrow-key menus. Menus are only available when:
- `questionary` is installed
- both `stdin` and `stdout` are TTY

Debug:

```bash
qsync doctor --json
```

If you’re running inside a non-interactive environment (CI, redirected output), use explicit flags like `--survey-id ...`.

## 4) “Workspace root not found”

If you’re running `qsync` from a directory that is *not* your workspace, pass `--root`:

```bash
qsync --root /path/to/workspace doctor
```

Or set `QSYNC_ROOT` in your environment.

## 5) Color output / diffs look “all gray”

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

## 6) Docs link in error messages is wrong

You can override the docs URL shown in errors via:

```bash
export QSYNC_DOCS_URL="docs/troubleshooting.md"
```
