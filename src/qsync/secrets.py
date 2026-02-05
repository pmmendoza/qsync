"""Secret management helpers (env + optional system keychain).

qsync prefers explicit config via environment variables / .env, but can fall back
to the OS keychain via the optional `keyring` dependency.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, Literal, Mapping, Sequence

QUALTRICS_TOKEN_KEYS: Sequence[str] = ("X-API-TOKEN", "QUALTRICS_API_KEY")

QUALTRICS_KEYRING_SERVICE_ENV = "QSYNC_QUALTRICS_KEYRING_SERVICE"
QUALTRICS_KEYRING_USERNAME_ENV = "QSYNC_QUALTRICS_KEYRING_USERNAME"
DISABLE_KEYRING_ENV = "QSYNC_DISABLE_KEYRING"

DEFAULT_QUALTRICS_KEYRING_SERVICE = "qualtrics-token"
DEFAULT_QUALTRICS_KEYRING_USERNAME = "token"


def _is_truthy_env(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def keyring_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return True if keyring lookups should be attempted."""

    if _is_truthy_env(os.environ.get(DISABLE_KEYRING_ENV)):
        return False
    if env and _is_truthy_env(env.get(DISABLE_KEYRING_ENV)):
        return False
    return True


def _default_username_candidates() -> list[str]:
    candidates: list[str] = [DEFAULT_QUALTRICS_KEYRING_USERNAME]
    try:
        import getpass

        user = (getpass.getuser() or "").strip()
        if user and user not in candidates:
            candidates.append(user)
    except Exception:
        pass
    if "" not in candidates:
        candidates.append("")
    return candidates


def _iter_qualtrics_keyring_candidates(
    env: Mapping[str, str] | None = None,
) -> Iterable[tuple[str, str]]:
    """Yield possible (service, username) pairs for the Qualtrics API token.

    Defaults match the common pattern used by the keyring CLI:
    - service: "Qualtrics"
    - username: "token"

    If env overrides are provided, only that specific pair is used.
    """

    service_override = (os.environ.get(QUALTRICS_KEYRING_SERVICE_ENV) or "").strip()
    username_override = (os.environ.get(QUALTRICS_KEYRING_USERNAME_ENV) or "").strip()
    if (not service_override) and env:
        service_override = (env.get(QUALTRICS_KEYRING_SERVICE_ENV) or "").strip()
    if (not username_override) and env:
        username_override = (env.get(QUALTRICS_KEYRING_USERNAME_ENV) or "").strip()
    if service_override or username_override:
        service = service_override or DEFAULT_QUALTRICS_KEYRING_SERVICE
        usernames = (
            [username_override] if username_override else _default_username_candidates()
        )
        for username in usernames:
            yield (service, username)
        return

    services = [
        DEFAULT_QUALTRICS_KEYRING_SERVICE,
        # Backward-compatible defaults from earlier setups/docs.
        "Qualtrics",
        # Friendly fallback for users who store the combined label as the service.
        "Qualtrics - token",
    ]
    usernames = _default_username_candidates()
    for service in services:
        for username in usernames:
            yield (service, username)


def get_qualtrics_api_token_from_keyring(env: Mapping[str, str] | None = None) -> str | None:
    """Return a Qualtrics API token from system keychain (if available)."""

    if not keyring_enabled(env):
        return None

    try:
        import keyring  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        return None

    for service, username in _iter_qualtrics_keyring_candidates(env):
        try:
            value = keyring.get_password(service, username)
        except Exception:
            continue
        if value is None:
            continue
        token = str(value).strip()
        if token:
            return token

    return None


TokenSource = Literal["env", "dotenv", "keyring", "missing"]


@dataclass(frozen=True)
class ResolvedToken:
    value: str | None
    source: TokenSource
    key: str | None = None


def resolve_qualtrics_api_token(file_env: Mapping[str, str] | None = None) -> ResolvedToken:
    """Resolve Qualtrics API token with precedence: env > .env > keyring."""

    for key in QUALTRICS_TOKEN_KEYS:
        value = (os.environ.get(key) or "").strip()
        if value:
            return ResolvedToken(value=value, source="env", key=key)

    if file_env:
        for key in QUALTRICS_TOKEN_KEYS:
            value = (file_env.get(key) or "").strip()
            if value:
                return ResolvedToken(value=value, source="dotenv", key=key)

    value = get_qualtrics_api_token_from_keyring(file_env)
    if value:
        return ResolvedToken(value=value, source="keyring", key=None)

    return ResolvedToken(value=None, source="missing", key=None)
