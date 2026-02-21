#!/usr/bin/env bash
set -euo pipefail

# Run from repo root:
#   ./scripts/test_pipx_install_local.sh
#
# Last run (macOS 2026-01-29):
#   ./scripts/test_pipx_install_local.sh

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)

if ! command -v pipx >/dev/null 2>&1; then
  echo "pipx not found. Install with: python -m pip install pipx" >&2
  exit 1
fi

PIPX_TEST_ROOT=$(mktemp -d)
WORKSPACE_DIR=$(mktemp -d)
cleanup() {
  rm -rf "$PIPX_TEST_ROOT" "$WORKSPACE_DIR"
}
trap cleanup EXIT

export PIPX_HOME="$PIPX_TEST_ROOT/pipx"
export PIPX_BIN_DIR="$PIPX_TEST_ROOT/bin"
export PATH="$PIPX_BIN_DIR:$PATH"

mkdir -p "$PIPX_HOME" "$PIPX_BIN_DIR"

pipx install "$REPO_ROOT"

qsync --help >/dev/null

(
  cd "$WORKSPACE_DIR"
  qsync onboard --non-interactive --skip-gitignore >/dev/null
)

DOCTOR_JSON=$(qsync --root "$WORKSPACE_DIR" doctor --json || true)
python - "$WORKSPACE_DIR" "$DOCTOR_JSON" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
payload = json.loads(sys.argv[2])

layout = payload.get("workspace_layout")
if layout == "account_root_v1":
    required = [
        root / "accounts" / "default" / "surveys",
        root / "accounts" / "default" / "surveys" / "pending",
        root / "accounts" / "default" / "excel",
        root / "accounts" / "default" / "survey_js" / "core",
        root / "accounts" / "default" / "contents" / "qualtrics_library_messages",
        root / "accounts" / "default" / "contents" / "qualtrics_survey_translations",
        root / "accounts" / "default" / "export",
        root / "accounts" / "default" / "responses",
        root / "accounts" / "default" / "tmp",
        root / "logs",
    ]
elif layout == "legacy":
    required = [
        root / "surveys",
        root / "surveys" / "pending",
        root / "excel",
        root / "survey_js" / "core",
        root / "contents" / "qualtrics_library_messages",
        root / "contents" / "qualtrics_survey_translations",
        root / "export",
        root / "responses",
        root / "tmp",
        root / "logs",
    ]
else:
    raise SystemExit(f"Unexpected workspace layout: {layout!r}")

missing = [str(path) for path in required if not path.exists()]
if missing:
    raise SystemExit("Missing workspace dirs:\n" + "\n".join(missing))
PY

if [[ -n "${QSYNC_PIPX_GIT_REF:-}" ]]; then
  QSYNC_PIPX_GIT_URL=${QSYNC_PIPX_GIT_URL:-"https://github.com/pmmendoza/qsync.git"}
  echo "Running pipx extras checks using ref: ${QSYNC_PIPX_GIT_REF}"

  pipx uninstall qsync >/dev/null 2>&1 || true
  pipx install --include-deps "qsync[completion] @ git+${QSYNC_PIPX_GIT_URL}@${QSYNC_PIPX_GIT_REF}"
  command -v activate-global-python-argcomplete >/dev/null 2>&1
  pipx runpip qsync show argcomplete >/dev/null

  pipx uninstall qsync >/dev/null 2>&1 || true
  if pipx install "qsync[langcheck] @ git+${QSYNC_PIPX_GIT_URL}@${QSYNC_PIPX_GIT_REF}"; then
    pipx runpip qsync show fasttext-wheel >/dev/null
  else
    echo "Skipping langcheck extra (install failed)" >&2
  fi
fi

echo "pipx local install smoke test: OK"
