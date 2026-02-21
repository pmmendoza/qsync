# Script Run Contracts

This page documents the operational scripts that currently remain in `scripts/`.

## `scripts/split_recog_blocks.py`

Purpose:
- Split recognition statement+confidence pairs into separate blocks and wrap them in a randomizer flow structure.

Safety:
- Use `--dry-run` first. In dry-run mode it still reads survey definition from Qualtrics (`GET`) but does not perform writes.
- The script expects an unsplit baseline structure with `BL_bEBNoi3ynL4qR1A` and profile-specific QID sets.
  If the survey is already refactored/split, it will exit with a missing-QID error.

Inputs:
- `--survey-id` (required)
- `--account` (optional qsync account context via `.env.<account>`)
- `--profile` (`pre` or `post`)
- `--dry-run` (recommended first)

Dependencies:
- Python 3
- `qsync` importable from repo (`src/`)
- Valid account credentials resolvable by `qsync.config.load_account_env`

Credential requirements:
- Qualtrics API token with survey read permission for dry-run
- Qualtrics API token with survey edit permission for non-dry-run execution

Example:
```bash
python scripts/split_recog_blocks.py \
  --survey-id SV_XXXXXXXXXXXXXXX \
  --profile pre \
  --dry-run
```

If you omit `--account`, the script uses the active/default `.env` context.

## `scripts/test_pipx_install_local.sh`

Purpose:
- Validate local pipx install path for `qsync` and basic workspace bootstrap behavior.

Safety:
- Uses temporary directories and cleans them up automatically.
- No Qualtrics writes.

Inputs:
- Optional env:
  - `QSYNC_PIPX_GIT_REF` (enables additional extras-install checks)
  - `QSYNC_PIPX_GIT_URL` (defaults to `https://github.com/pmmendoza/qsync.git`)

Dependencies:
- `pipx` available on PATH
- Python runtime for inline doctor JSON verification

Validation checks:
- `qsync` installs via pipx from local repo
- `qsync onboard --non-interactive` succeeds
- Workspace layout is `account_root_v1`
- Required directories exist under `accounts/default/`

Example:
```bash
bash scripts/test_pipx_install_local.sh
```
