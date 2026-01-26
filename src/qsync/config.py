"""
Central configuration module for Qualtrics scripts.
Handles .env loading and API header generation.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Tuple

from .errors import QsyncConfigError

_ROOT_ENV_KEYS = ("QSYNC_ROOT", "QSYNC_DATA_DIR")

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
    return merged


def build_headers(env: Dict[str, str]) -> Dict[str, str]:
    """Build request headers using API token credentials."""
    headers = {"Accept": "application/json"}

    api_token = env.get("X-API-TOKEN") or env.get("QUALTRICS_API_KEY")

    if api_token:
        headers["X-API-TOKEN"] = api_token
        return headers

    raise QsyncConfigError(
        error_id="QSYNC-CONFIG-TOKEN-001",
        problem="No Qualtrics API token found.",
        why="qsync requires an API token for Qualtrics API calls (OAuth is not supported in this repo configuration).",
        impact="Any command that talks to the Qualtrics API will fail.",
        action="Set `X-API-TOKEN` (preferred) or `QUALTRICS_API_KEY` in your shell or `.env`, then re-run `qsync doctor`.",
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
