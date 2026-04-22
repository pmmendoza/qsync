# Changelog

## 2026-04-22
- Added `qsync survey create` for inactive survey creation from qsync's bundled QSF seed, a local QSF file, or an existing same-account survey template.
- Added JSON output and duplicate-name handling for `survey create`.
- Added `qsync self-update --uv-tool` for `uv tool install --force ...` based reinstalls.

## 2026-02-21
- Breaking syntax cleanup:
  - Added global `--yes/-y` handling across CLI commands (accepted anywhere in argv).
  - Removed deprecated aliases:
    - `qsync compare --json-output` -> use `--report-path`.
    - `qsync sync --all` -> use `--all-focal`.
    - `qsync survey prepare/master --all` -> use `--all-surveys`.
    - `qsync survey parity-check --a/--b` -> use `--source-id/--target-id`.
    - `qsync survey export-side-by-side --a/--b` -> use `--source-id/--target-id`.
  - Added explicit actionable errors for removed aliases.
  - Hardened non-interactive confirmation paths to fail fast with `--yes` guidance.

## 2026-02-05
- Fixed `qsync init` / `qsync items pull` to prefill non-base language columns (`Text_*`, `Label_*`) from the cached survey definition language blocks (without overwriting non-empty Excel cells).
- Added optional keychain (`keyring`) support for resolving the Qualtrics API token (still supports env + `.env`).

## 2026-01-27
- Added pipx install guidance (README + troubleshooting).
- Added pipx smoke test script and CI checks.
- Added CI coverage for pipx `pdf` extra on macOS and Ubuntu.
- Added Windows pipx smoke CI job (core CLI).
- Expanded `qsync --version` with install diagnostics.
