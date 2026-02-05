import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class KeyringQualtricsTokenFallbackTests(unittest.TestCase):
    def test_build_headers_falls_back_to_keyring(self) -> None:
        from qsync.config import build_headers

        fake_keyring = types.ModuleType("keyring")
        fake_keyring.get_password = lambda service, username: "keyring-token"

        with (
            patch.dict(sys.modules, {"keyring": fake_keyring}),
            patch.dict(
                os.environ,
                {
                    "QSYNC_DISABLE_KEYRING": "",
                    "QSYNC_QUALTRICS_KEYRING_SERVICE": "",
                    "QSYNC_QUALTRICS_KEYRING_USERNAME": "",
                    "X-API-TOKEN": "",
                    "QUALTRICS_API_KEY": "",
                },
                clear=False,
            ),
        ):
            headers = build_headers({})

        self.assertEqual(headers.get("X-API-TOKEN"), "keyring-token")

    def test_load_env_populates_token_from_keyring(self) -> None:
        from qsync.config import load_env

        fake_keyring = types.ModuleType("keyring")
        fake_keyring.get_password = lambda service, username: "keyring-token"

        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / ".env"
            env_path.write_text(
                "QUALTRICS_BASE_URL=example.qualtrics.com\n",
                encoding="utf-8",
            )

            with (
                patch.dict(sys.modules, {"keyring": fake_keyring}),
                patch.dict(
                    os.environ,
                    {
                        "QSYNC_DISABLE_KEYRING": "",
                        "QSYNC_QUALTRICS_KEYRING_SERVICE": "",
                        "QSYNC_QUALTRICS_KEYRING_USERNAME": "",
                        "X-API-TOKEN": "",
                        "QUALTRICS_API_KEY": "",
                    },
                    clear=False,
                ),
            ):
                env = load_env(env_path)

        self.assertEqual(env.get("QUALTRICS_BASE_URL"), "example.qualtrics.com")
        self.assertEqual(env.get("X-API-TOKEN"), "keyring-token")

    def test_keyring_tries_current_user_when_token_username_missing(self) -> None:
        from qsync.secrets import get_qualtrics_api_token_from_keyring

        fake_keyring = types.ModuleType("keyring")

        def _get_password(service: str, username: str) -> str | None:
            if service == "qualtrics-token" and username == "pm":
                return "keyring-token"
            return None

        fake_keyring.get_password = _get_password

        with (
            patch.dict(sys.modules, {"keyring": fake_keyring}),
            patch("getpass.getuser", lambda: "pm"),
            patch.dict(
                os.environ,
                {
                    "QSYNC_DISABLE_KEYRING": "",
                    "QSYNC_QUALTRICS_KEYRING_SERVICE": "",
                    "QSYNC_QUALTRICS_KEYRING_USERNAME": "",
                },
                clear=False,
            ),
        ):
            token = get_qualtrics_api_token_from_keyring()

        self.assertEqual(token, "keyring-token")


if __name__ == "__main__":
    unittest.main()
