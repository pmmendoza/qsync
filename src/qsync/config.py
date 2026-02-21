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
_ACCOUNT_ENV_KEY = "QSYNC_ACCOUNT"
_WORKSPACE_LAYOUT_ENV_KEY = "QSYNC_WORKSPACE_LAYOUT"
_WORKSPACE_LAYOUT_PREF_KEY = "workspace_layout"
WORKSPACE_LAYOUT_LEGACY = "legacy"
WORKSPACE_LAYOUT_ACCOUNT_ROOT_V1 = "account_root_v1"
_SURVEY_CACHE_SUBDIR_ENV_KEY = "QSYNC_SURVEY_CACHE_SUBDIR"
_SURVEY_CACHE_SUBDIR_PREF_KEY = "survey_cache_subdir"
_DEFAULT_SURVEY_CACHE_SUBDIR_LEGACY = "caches"
_DEFAULT_SURVEY_CACHE_SUBDIR_ACCOUNT_ROOT = "cache"
_SURVEY_CACHE_SUBDIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
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


def validate_workspace_layout(layout: str | None) -> str:
    """Validate and normalize workspace layout mode."""

    raw = (layout or "").strip().lower().replace("-", "_")
    aliases = {
        "legacy": WORKSPACE_LAYOUT_LEGACY,
        "accounts": WORKSPACE_LAYOUT_ACCOUNT_ROOT_V1,
        "account_root": WORKSPACE_LAYOUT_ACCOUNT_ROOT_V1,
        "account_root_v1": WORKSPACE_LAYOUT_ACCOUNT_ROOT_V1,
    }
    normalized = aliases.get(raw)
    if normalized:
        return normalized
    raise QsyncValidationError(
        error_id="QSYNC-VALIDATION-LAYOUT-001",
        problem=f"Invalid workspace layout: {layout!r}.",
        why="qsync supports only known workspace layout modes.",
        impact="Path resolution cannot continue safely with an unknown mode.",
        action=(
            "Use one of: `legacy`, `accounts`, or `account_root_v1` "
            f"(via {_WORKSPACE_LAYOUT_ENV_KEY} or .qsync/preferences.json)."
        ),
        context={"layout": layout},
        exit_code=2,
    )


def resolve_workspace_layout(*, root: Path | None = None) -> str:
    """Resolve current workspace layout mode.

    Precedence:
    1) `QSYNC_WORKSPACE_LAYOUT` env override
    2) workspace preference `.qsync/preferences.json` key `workspace_layout`
    3) default: `legacy`
    """

    env_raw = (os.environ.get(_WORKSPACE_LAYOUT_ENV_KEY) or "").strip()
    if env_raw:
        return validate_workspace_layout(env_raw)

    root_path = root or resolve_root(required=False) or Path.cwd()
    try:
        from .workspace_prefs import load_prefs

        prefs, _err = load_prefs(root_path)
        pref_raw = prefs.get(_WORKSPACE_LAYOUT_PREF_KEY)
        if isinstance(pref_raw, str) and pref_raw.strip():
            return validate_workspace_layout(pref_raw)
    except Exception:
        # Preferences are best-effort; fall back to default.
        pass

    return WORKSPACE_LAYOUT_LEGACY


def discover_root(start: Path | None = None) -> Path | None:
    """Discover the qsync workspace root by searching parent directories."""

    start = start or Path.cwd()
    for candidate in _iter_parents(start):
        # Account-first workspace layout.
        if (candidate / "accounts" / "default" / "inventory.csv").exists():
            return candidate
        if (candidate / "accounts" / "default").is_dir() and (
            candidate / ".qsync"
        ).is_dir():
            return candidate

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
            "(legacy mode) or `accounts/default/inventory.csv` (account-root layout)."
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
    """Load credentials/settings for the current run.

    Default account behavior: load from `.env` (or `--env-path` / `QSYNC_ENV_PATH`)
    and overlay values from the process environment.

    If `QSYNC_ACCOUNT` is set (for example via `--account`, an exported env var, or
    a workspace default set by `qsync account use`), qsync instead loads
    `.env.<account>` from the workspace root and does not overlay process env vars.

    Precedence:
    - Values from the process environment override .env values.
    - Missing .env is not an error by itself; callers should validate required keys.
    """
    # Account selection (QSYNC_ACCOUNT) overrides the default `.env` behavior,
    # but callers can bypass it by passing an explicit `path` (used by onboarding).
    if path is None:
        raw_account = (os.environ.get(_ACCOUNT_ENV_KEY) or "").strip()
        if raw_account:
            account = validate_account_name(raw_account)
            return load_account_env(account)

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


def get_active_account() -> str | None:
    """Return the active account selection, if any (from QSYNC_ACCOUNT).

    Note: the CLI may set `QSYNC_ACCOUNT` from a workspace preference
    (`.qsync/preferences.json` key `active_account`) so downstream code can treat
    it uniformly.
    """

    raw = (os.environ.get(_ACCOUNT_ENV_KEY) or "").strip()
    if not raw:
        return None
    return validate_account_name(raw)


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

    Alternate account (via `QSYNC_ACCOUNT` / `--account NAME` / workspace active account):
      <root>/<dirname>/.NAME/
    """

    root_path = root or resolve_root(required=False) or Path.cwd()
    selected = account if account is not None else get_active_account()
    if selected and str(selected).strip().lower() == "default":
        selected = None

    layout = resolve_workspace_layout(root=root_path)
    dirname_path = Path(dirname)
    parts = list(dirname_path.parts)
    if not parts:
        raise ValueError("resolve_scoped_dir() requires a non-empty dirname")

    if layout == WORKSPACE_LAYOUT_LEGACY:
        base = (root_path / dirname_path).resolve()
        if selected:
            scoped = validate_account_name(str(selected))
            return (base / f".{scoped}").resolve()
        return base

    # account_root_v1 layout
    account_name = validate_account_name(str(selected)) if selected else "default"
    account_root = (root_path / "accounts" / account_name).resolve()

    head = parts[0]
    tail = parts[1:]
    mapped_head = "js" if head == "survey_js" else head

    if mapped_head == "surveys":
        base = account_root
    elif mapped_head == "export":
        base = (account_root / "derived" / "export").resolve()
    elif mapped_head == "responses":
        base = (account_root / "derived" / "responses").resolve()
    elif mapped_head == "tmp":
        base = (account_root / "state" / "tmp").resolve()
    else:
        base = (account_root / mapped_head).resolve()

    if tail:
        return (base.joinpath(*tail)).resolve()
    return base


def _normalize_survey_cache_subdir(value: str | None) -> str | None:
    """Return a safe single-segment cache subdir name, or None if invalid/empty."""

    raw = (value or "").strip()
    if not raw:
        return None
    name = raw.strip("/\\").strip()
    if not name:
        return None
    if "/" in name or "\\" in name:
        return None
    if not _SURVEY_CACHE_SUBDIR_RE.fullmatch(name):
        return None
    return name


def validate_survey_cache_subdir(value: str) -> str:
    """Validate and normalize a survey-cache subdirectory name."""

    name = _normalize_survey_cache_subdir(value)
    if name:
        return name
    raw = str(value or "")
    raise QsyncValidationError(
        error_id="QSYNC-VALIDATION-SURVEYCACHE-001",
        problem=f"Invalid survey cache subdirectory: {raw!r}.",
        why=(
            "qsync uses a single folder name under `surveys/` for optional "
            "survey-definition caches."
        ),
        impact="The setting was rejected to prevent unsafe or ambiguous path resolution.",
        action=(
            "Use a simple folder name like `caches` or `defs` "
            "(letters/numbers plus `.`, `_`, `-`; no slashes)."
        ),
        context={"value": raw},
        exit_code=2,
    )


def resolve_survey_cache_subdir(
    *,
    root: Path | None = None,
) -> str:
    """Resolve the workspace survey cache subfolder name.

    Precedence:
    1) `QSYNC_SURVEY_CACHE_SUBDIR`
    2) workspace preference `.qsync/preferences.json` key `survey_cache_subdir`
    3) default: `caches`
    """

    env_value = _normalize_survey_cache_subdir(
        os.environ.get(_SURVEY_CACHE_SUBDIR_ENV_KEY)
    )
    if env_value:
        return env_value

    root_path = root or resolve_root(required=False) or Path.cwd()
    try:
        from .workspace_prefs import load_prefs

        prefs, _err = load_prefs(root_path)
        pref_raw = prefs.get(_SURVEY_CACHE_SUBDIR_PREF_KEY)
        if isinstance(pref_raw, str):
            pref_value = _normalize_survey_cache_subdir(pref_raw)
            if pref_value:
                return pref_value
    except Exception:
        # Preferences are best-effort; fall back to default.
        pass

    layout = resolve_workspace_layout(root=root_path)
    if layout == WORKSPACE_LAYOUT_ACCOUNT_ROOT_V1:
        return _DEFAULT_SURVEY_CACHE_SUBDIR_ACCOUNT_ROOT
    return _DEFAULT_SURVEY_CACHE_SUBDIR_LEGACY


def resolve_survey_cache_dir(
    *,
    root: Path | None = None,
    account: str | None = None,
) -> Path:
    """Resolve where survey definition JSON cache files should live.

    Behavior:
    - Legacy layout: base path is account-scoped `surveys/`.
    - Account-root layout: base path is `<account>/state/`.
    - If `<base>/<cache_subdir>/` exists, return that directory.
    - Legacy-only fallback: if `<base>/caches/` exists, return it.
    - Otherwise, return the base fallback directory.
    """

    base_dir = resolve_survey_cache_base_dir(root=root, account=account)
    root_path = root or resolve_root(required=False) or Path.cwd()
    layout = resolve_workspace_layout(root=root_path)
    cache_subdir = resolve_survey_cache_subdir(root=root_path)
    candidate = (base_dir / cache_subdir).resolve()
    if candidate.exists() and candidate.is_dir():
        return candidate

    # Backward-compatible fallback only for legacy layout workspaces.
    # Account-root mode is intentionally strict so migration mistakes don't
    # silently route cache reads/writes into stale folders.
    if layout == WORKSPACE_LAYOUT_LEGACY:
        legacy_candidate = (base_dir / _DEFAULT_SURVEY_CACHE_SUBDIR_LEGACY).resolve()
        if legacy_candidate.exists() and legacy_candidate.is_dir():
            return legacy_candidate

    return base_dir.resolve()


def resolve_survey_cache_base_dir(
    *,
    root: Path | None = None,
    account: str | None = None,
) -> Path:
    """Resolve the layout-aware base directory for survey-definition caches.

    - Legacy layout: `<surveys-scoped>/`
    - Account-root layout: `<surveys-scoped>/state/`
    """

    root_path = root or resolve_root(required=False) or Path.cwd()
    surveys_dir = resolve_scoped_dir("surveys", root=root_path, account=account)
    layout = resolve_workspace_layout(root=root_path)
    base_dir = surveys_dir if layout == WORKSPACE_LAYOUT_LEGACY else surveys_dir / "state"
    return base_dir.resolve()


def load_account_env(
    account: str,
    *,
    root: Path | None = None,
) -> Dict[str, str]:
    """Load credentials/settings for an explicit account selector.

    This is intentionally strict: it does not merge credentials from the process
    environment, so selecting an account is deterministic and doesn't get
    accidentally overridden by exported `QUALTRICS_BASE_URL` / `X-API-TOKEN`.

    Special case:
    - `account="default"` maps to the primary `.env` credentials.
    """

    root_path = root or resolve_root(required=False) or Path.cwd()
    selected = validate_account_name(account)
    if selected.lower() == "default":
        env_path = resolve_env_path(root=root_path) or (root_path / ".env").resolve()
    else:
        env_path = resolve_account_env_path(selected, root=root_path)
    if not env_path.exists():
        if selected.lower() == "default":
            raise QsyncConfigError(
                error_id="QSYNC-CONFIG-ACCOUNTENV-004",
                problem=f"Default env file not found: {env_path.name}",
                why="`--account default` maps to the workspace primary `.env` credentials.",
                impact="The command cannot run against the selected account.",
                action=(
                    f"Create `{env_path}` with at least:\n"
                    "  QUALTRICS_BASE_URL=iad1.qualtrics.com\n"
                    "  X-API-TOKEN=<token>\n"
                    "(or use QUALTRICS_API_KEY instead of X-API-TOKEN)."
                ),
                context={"env_path": str(env_path), "account": selected},
                exit_code=1,
            )
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
            context={"env_path": str(env_path), "account": selected},
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
            context={
                "env_path": str(env_path),
                "missing": ", ".join(missing),
                "account": selected,
            },
            exit_code=1,
        )

    if base_url.startswith("http://") or base_url.startswith("https://"):
        raise QsyncConfigError(
            error_id="QSYNC-CONFIG-ACCOUNTENV-003",
            problem="QUALTRICS_BASE_URL must be host-only (no scheme).",
            why="qsync constructs API URLs as `https://<QUALTRICS_BASE_URL>/API/v3/...`.",
            impact="A value like `https://iad1.qualtrics.com` would produce an invalid URL.",
            action=f"Edit `{env_path}` and set `QUALTRICS_BASE_URL` to host-only (e.g. `iad1.qualtrics.com`).",
            context={"env_path": str(env_path), "account": selected},
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
