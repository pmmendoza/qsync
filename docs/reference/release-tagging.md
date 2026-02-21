# Release And Tagging Process

This document defines the canonical release flow for `qsync` so installs by GitHub ref remain reproducible (`@vX.Y.Z`) and rollback paths are clear.

## Versioning Policy

- Tags use semantic versioning: `vMAJOR.MINOR.PATCH`.
- Bump rules:
  - `MAJOR`: breaking CLI/behavior changes.
  - `MINOR`: backward-compatible features.
  - `PATCH`: backward-compatible fixes/docs/tests.
- Tags are immutable once published.

## Pre-Release Gate

Before tagging:

1. Run local tests:
   - `PYTHONPATH=src pytest -q`
2. Run smoke tests for mutating workflows using smoke survey naming policy:
   - `[smoke_test]_[YYMMDD_HHMM]_[feature]`
3. Delete all smoke-test surveys created during validation.
4. Ensure docs match parser/runtime truth (`README.md`, `docs/reference/cli.md`, touched workflow docs).
5. Ensure working tree is clean except intended release changes.

## Tagging Steps

1. Update `CHANGELOG.md` with the release notes and date.
2. Commit release prep on `main`.
3. Create annotated tag:
   - `git tag -a vX.Y.Z -m "qsync vX.Y.Z"`
4. Push branch + tag:
   - `git push origin main`
   - `git push origin vX.Y.Z`
5. Create GitHub release from the pushed tag, including:
   - summary of shipped tasks,
   - migration/syntax changes,
   - smoke-test evidence links.

## Install Verification

Validate install from the released tag:

```bash
pipx install --force "qsync @ git+https://github.com/pmmendoza/qsync.git@vX.Y.Z"
qsync --version
```

If extras are required:

```bash
pipx install --force "qsync[pdf,langcheck] @ git+https://github.com/pmmendoza/qsync.git@vX.Y.Z"
```

## Rollback Procedure

If a release is bad:

1. Reinstall previous known-good tag:
   - `pipx install --force "qsync @ git+https://github.com/pmmendoza/qsync.git@vPREV"`
2. Open a hotfix PR from `main`.
3. Release a new patch tag (do not retag existing versions).

## Operator Notes

- Prefer tagged refs (`@vX.Y.Z`) over branches (`@main`) for production environments.
- Keep `qsync self-update` guidance aligned with this policy so users can pin or roll forward intentionally.
