"""
Central configuration module for Qualtrics scripts.
Handles .env loading and API header generation.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Tuple

from .errors import QsyncConfigError, QsyncValidationError

_ROOT_ENV_KEYS = ("QSYNC_ROOT", "QSYNC_DATA_DIR")
_ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

try:
    from newsflows_workspace.paths import iter_parents as _iter_parents
    from newsflows_workspace.paths import parse_dotenv as _parse_dotenv
except ModuleNotFoundError:  # pragma: no cover - used outside the workspace install

    def _iter_parents(start: Path):
        current = start.resolve()
        while True:
            yield current
            if current.parent == current:
                break
            current = current.parent

    def _parse_dotenv(text: str) -> Dict[str, str]:
        data: Dict[str, str] = {}
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if "#" in value:
                value = value.split("#", 1)[0].strip()
            value = value.strip().strip("\"'").strip()
            if key:
                data[key] = value
        return data


def discover_root(start: Path | None = None) -> Path | None:
    """Discover the qsync workspace root by searching parent directories."""

    start = start or Path.cwd()
    for candidate in _iter_parents(start):
        if (candidate / "surveys" / "inventory.csv").exists():
            return candidate
        if (candidate / "surveys" / "qualtrics_surveys.csv").exists():
            return candidate
        # fallback heuristics
        if (candidate / "survey_js").is_dir() and (candidate / "surveys").is_dir():
            return candidate
    return None


def resolve_root(
    root: str | Path | None = None,
    *,
    env_path: str | Path | None = None,
    required: bool = False,
) -> Path | None:
    """Resolve the workspace root from args/env/discovery."""

    if root:
        return Path(root).expanduser().resolve()

    for key in _ROOT_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            return Path(value).expanduser().resolve()

    env_candidate = env_path or os.environ.get("QSYNC_ENV_PATH")
    if env_candidate:
        try:
            env_file = Path(env_candidate).expanduser().resolve()
            if env_file.exists():
                file_env = load_env_file(env_file)
                for key in _ROOT_ENV_KEYS:
                    raw = (file_env.get(key) or "").strip()
                    if not raw:
                        continue
                    path = Path(raw).expanduser()
                    if not path.is_absolute():
                        path = (env_file.parent / path).resolve()
                    else:
                        path = path.resolve()
                    return path

                if env_file.name == ".env":
                    return env_file.parent.resolve()
        except Exception:
            pass

    discovered = discover_root()
    if discovered:
        return discovered

    if required:
        raise RuntimeError(
            "Could not resolve qsync workspace root. Provide `--root`, set `QSYNC_ROOT`, "
            "or run from within a workspace containing `surveys/inventory.csv` "
            "(legacy: `surveys/qualtrics_surveys.csv`)."
        )
    return None


def resolve_env_path(
    env_path: str | Path | None = None,
    *,
    root: Path | None = None,
) -> Path | None:
    """Resolve the `.env` path from args/env/defaults."""

    if env_path:
        return Path(env_path).expanduser().resolve()

    value = os.environ.get("QSYNC_ENV_PATH")
    if value:
        return Path(value).expanduser().resolve()

    if root:
        return (root / ".env").resolve()

    return None


def load_env_file(path: Path | None) -> Dict[str, str]:
    """Load key/value pairs from a dotenv-style file (best-effort)."""

    if not path or not path.exists():
        return {}
    return _parse_dotenv(path.read_text(encoding="utf-8"))


ROOT = resolve_root(required=False) or Path.cwd()
ENV_PATH = resolve_env_path(root=ROOT)


def load_env(path: Path | None = None) -> Dict[str, str]:
    """Load credentials/settings from environment and optional .env file.

    Precedence:
    - Values from the process environment override .env values.
    - Missing .env is not an error by itself; callers should validate required keys.
    """
    env_path = path if path is not None else ENV_PATH
    file_env = load_env_file(env_path)

    keys = set(file_env.keys())
    keys.update(
        {
            "QUALTRICS_BASE_URL",
            "QUALTRICS_API_KEY",
            "X-API-TOKEN",
        }
    )
    merged = dict(file_env)
    for key in keys:
        if key in os.environ and os.environ[key]:
            merged[key] = os.environ[key].strip()

    # Optional keychain support via `keyring` (only if no token was provided).
    if not (merged.get("X-API-TOKEN") or merged.get("QUALTRICS_API_KEY")):
        try:
            from .secrets import get_qualtrics_api_token_from_keyring

            token = get_qualtrics_api_token_from_keyring(merged)
            if token:
                merged["X-API-TOKEN"] = token
        except Exception:
            # Best-effort only; treat keyring as an optional enhancement.
            pass
    return merged


def validate_account_name(account: str) -> str:
    """Validate and normalize an account selector used for `.env.<account>` lookup."""

    name = (account or "").strip()
    if not name:
        raise QsyncValidationError(
            error_id="QSYNC-VALIDATION-ACCOUNT-001",
            problem="Account name is required.",
            why="`--account` selects credentials from a workspace-local dotenv file named `.env.<account>`.",
            impact="qsync cannot determine which credentials to use.",
            action="Provide an account name like `damian` (allowed: letters, numbers, `_`, `-`).",
            exit_code=2,
        )
    if not _ACCOUNT_NAME_RE.fullmatch(name):
        raise QsyncValidationError(
            error_id="QSYNC-VALIDATION-ACCOUNT-002",
            problem=f"Invalid account name: {name!r}.",
            why="Account names are mapped directly to `.env.<account>` filenames under the workspace root.",
            impact="qsync rejected the value to prevent path traversal or ambiguous file resolution.",
            action="Use a simple name like `damian` or `partner_2` (allowed: letters, numbers, `_`, `-`).",
            context={"account": name},
            exit_code=2,
        )
    return name


def resolve_account_env_path(
    account: str,
    *,
    root: Path | None = None,
) -> Path:
    """Return the filesystem path to `.env.<account>` under the workspace root."""

    name = validate_account_name(account)
    root_path = root or resolve_root(required=False) or Path.cwd()
    return (root_path / f".env.{name}").resolve()


def resolve_scoped_dir(
    dirname: str | Path,
    *,
    root: Path | None = None,
    account: str | None = None,
) -> Path:
    """Resolve an account-scoped workspace artifact directory.

    Default account:
      <root>/<dirname>/

    Alternate account (via `--account NAME`):
      <root>/<dirname>/.NAME/
    """

    root_path = root or resolve_root(required=False) or Path.cwd()
    base = (root_path / dirname).resolve()
    if account:
        scoped = validate_account_name(account)
        return (base / f".{scoped}").resolve()
    return base


def load_account_env(
    account: str,
    *,
    root: Path | None = None,
) -> Dict[str, str]:
    """Load credentials/settings from `.env.<account>` under the workspace root.

    This is intentionally strict: it does not merge credentials from the process
    environment, so selecting an account is deterministic and doesn't get
    accidentally overridden by exported `QUALTRICS_BASE_URL` / `X-API-TOKEN`.
    """

    env_path = resolve_account_env_path(account, root=root)
    if not env_path.exists():
        raise QsyncConfigError(
            error_id="QSYNC-CONFIG-ACCOUNTENV-001",
            problem=f"Account env file not found: {env_path.name}",
            why="`--account` requires a workspace-local dotenv file named `.env.<account>`.",
            impact="The command cannot run against the selected account.",
            action=(
                f"Create `{env_path}` with at least:\n"
                "  QUALTRICS_BASE_URL=iad1.qualtrics.com\n"
                "  X-API-TOKEN=<token>\n"
                "(or use QUALTRICS_API_KEY instead of X-API-TOKEN)."
            ),
            context={"env_path": str(env_path), "account": validate_account_name(account)},
            exit_code=1,
        )

    file_env = load_env_file(env_path)
    # Accept canonical keys and common TARGET_* variants so existing dotenv
    # setups can be reused as `.env.<account>` with minimal churn.
    base_url = (
        (file_env.get("QUALTRICS_BASE_URL") or "").strip()
        or (file_env.get("TARGET_QUALTRICS_BASE_URL") or "").strip()
    )
    api_token = (
        (file_env.get("X-API-TOKEN") or "").strip()
        or (file_env.get("QUALTRICS_API_KEY") or "").strip()
        or (file_env.get("TARGET_X-API-TOKEN") or "").strip()
        or (file_env.get("TARGET_QUALTRICS_API_KEY") or "").strip()
    )

    missing: list[str] = []
    if not base_url:
        missing.append("QUALTRICS_BASE_URL")
    if not api_token:
        missing.append(
            "X-API-TOKEN (or QUALTRICS_API_KEY; also accepts TARGET_X-API-TOKEN / TARGET_QUALTRICS_API_KEY)"
        )

    if missing:
        raise QsyncConfigError(
            error_id="QSYNC-CONFIG-ACCOUNTENV-002",
            problem=f"Missing required key(s) in {env_path.name}.",
            why="Account selection needs both a Qualtrics datacenter host and an API token.",
            impact="Any command that talks to the Qualtrics API will fail for this account.",
            action=(
                f"Edit `{env_path}` and add:\n"
                "  QUALTRICS_BASE_URL=iad1.qualtrics.com\n"
                "  X-API-TOKEN=<token>"
            ),
            context={"env_path": str(env_path), "missing": ", ".join(missing), "account": validate_account_name(account)},
            exit_code=1,
        )

    if base_url.startswith("http://") or base_url.startswith("https://"):
        raise QsyncConfigError(
            error_id="QSYNC-CONFIG-ACCOUNTENV-003",
            problem="QUALTRICS_BASE_URL must be host-only (no scheme).",
            why="qsync constructs API URLs as `https://<QUALTRICS_BASE_URL>/API/v3/...`.",
            impact="A value like `https://iad1.qualtrics.com` would produce an invalid URL.",
            action=f"Edit `{env_path}` and set `QUALTRICS_BASE_URL` to host-only (e.g. `iad1.qualtrics.com`).",
            context={"env_path": str(env_path), "account": validate_account_name(account)},
            exit_code=1,
        )

    env = dict(file_env)
    env["QUALTRICS_BASE_URL"] = base_url
    if not (env.get("X-API-TOKEN") or env.get("QUALTRICS_API_KEY")):
        env["X-API-TOKEN"] = api_token

    # Return an env dict suitable for get_client_config().
    return env


def build_headers(env: Dict[str, str]) -> Dict[str, str]:
    """Build request headers using API token credentials."""
    headers = {"Accept": "application/json"}

    api_token = env.get("X-API-TOKEN") or env.get("QUALTRICS_API_KEY")

    if not api_token:
        # Optional keychain support via `keyring`.
        try:
            from .secrets import get_qualtrics_api_token_from_keyring

            api_token = get_qualtrics_api_token_from_keyring(env)
        except Exception:
            api_token = None

    if api_token:
        headers["X-API-TOKEN"] = str(api_token).strip()
        return headers

    raise QsyncConfigError(
        error_id="QSYNC-CONFIG-TOKEN-001",
        problem="No Qualtrics API token found.",
        why="qsync requires an API token for Qualtrics API calls (OAuth is not supported in this repo configuration).",
        impact="Any command that talks to the Qualtrics API will fail.",
        action=(
            "Set `X-API-TOKEN` (preferred) or `QUALTRICS_API_KEY` in your shell or `.env` "
            "(or store it in your system keychain via the optional `keyring` integration), "
            "then re-run `qsync doctor`."
        ),
        docs_url="README.md#workspace-configuration",
    )


def get_client_config(env: Dict[str, str] | None = None) -> Tuple[str, Dict[str, str]]:
    """Return (base_url, headers) for Qualtrics API calls."""
    env = env or load_env()
    base_url = env.get("QUALTRICS_BASE_URL")
    if not base_url:
        raise QsyncConfigError(
            error_id="QSYNC-CONFIG-BASEURL-001",
            problem="QUALTRICS_BASE_URL is missing.",
            why="qsync needs the Qualtrics datacenter host to build API URLs.",
            impact="Any command that talks to the Qualtrics API will fail.",
            action="Set `QUALTRICS_BASE_URL` (host only, e.g. `iad1.qualtrics.com`) in your shell or `.env` (override via `QSYNC_ENV_PATH` / `--env-path`).",
            docs_url="README.md#workspace-configuration",
        )
    headers = build_headers(env)
    return base_url, headers
