# Changelog

## 2026-02-05
- Fixed `qsync init` / `qsync items pull` to prefill non-base language columns (`Text_*`, `Label_*`) from the cached survey definition language blocks (without overwriting non-empty Excel cells).
- Added optional keychain (`keyring`) support for resolving the Qualtrics API token (still supports env + `.env`).

## 2026-01-27
- Added pipx install guidance (README + troubleshooting).
- Added pipx smoke test script and CI checks.
- Added CI coverage for pipx `pdf` extra on macOS and Ubuntu.
- Added Windows pipx smoke CI job (core CLI).
- Expanded `qsync --version` with install diagnostics.
