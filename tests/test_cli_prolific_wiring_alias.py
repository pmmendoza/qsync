from __future__ import annotations

import os
from pathlib import Path


def test_survey_prolific_wiring_alias_routes_pull_studies(
    monkeypatch, tmp_path: Path
) -> None:
    from qsync.cli import main

    seen: dict[str, str | None] = {}

    def _fake_handle_pull_studies(args) -> None:
        seen["survey_command"] = getattr(args, "survey_command", None)
        seen["prolific_command"] = getattr(args, "prolific_command", None)
        seen["account"] = getattr(args, "account", None)

    monkeypatch.setattr("qsync.cli_prolific.handle_pull_studies", _fake_handle_pull_studies)

    main(
        [
            "--root",
            str(tmp_path),
            "survey",
            "prolific-wiring",
            "pull-studies",
        ]
    )

    assert seen["survey_command"] == "prolific-wiring"
    assert seen["prolific_command"] == "pull-studies"
    assert seen["account"] is None


def test_top_level_prolific_supports_account_flag(
    monkeypatch, tmp_path: Path
) -> None:
    from qsync.cli import main

    seen: dict[str, str | None] = {}

    def _fake_handle_pull_studies(args) -> None:
        seen["prolific_command"] = getattr(args, "prolific_command", None)
        seen["account"] = os.environ.get("QSYNC_ACCOUNT")

    monkeypatch.setattr("qsync.cli_prolific.handle_pull_studies", _fake_handle_pull_studies)

    main(
        [
            "--root",
            str(tmp_path),
            "prolific",
            "--account",
            "damian",
            "pull-studies",
        ]
    )

    assert seen["prolific_command"] == "pull-studies"
    assert seen["account"] == "damian"
