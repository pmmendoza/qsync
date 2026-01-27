#!/usr/bin/env bash
set -euo pipefail

# Run from repo root:
#   ./dev/scripts/test_pipx_install_local.sh

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "${SCRIPT_DIR}/../.." && pwd)

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

for dir in surveys excel survey_js contents logs export responses; do
  if [[ ! -d "$WORKSPACE_DIR/$dir" ]]; then
    echo "Missing workspace dir: $dir" >&2
    exit 1
  fi
done

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
