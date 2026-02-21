from __future__ import annotations

from pathlib import Path

import pytest

from tests.workspace_helpers import ensure_qsync_workspace


def test_global_yes_parses_before_and_after_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    from qsync.cli import main

    main(
        [
            "--yes",
            "self-update",
            "--dry-run",
            "--pip",
            "--repo",
            "owner/repo",
            "--ref",
            "main",
        ]
    )
    first_out = capsys.readouterr().out
    assert "Dry run" in first_out

    main(
        [
            "self-update",
            "--dry-run",
            "--pip",
            "--repo",
            "owner/repo",
            "--ref",
            "main",
            "--yes",
        ]
    )
    second_out = capsys.readouterr().out
    assert "Dry run" in second_out


@pytest.mark.parametrize(
    "argv, expected",
    [
        (
            [
                "compare",
                "--source-id",
                "SV_A",
                "--target-id",
                "SV_B",
                "--json-output",
                "report.json",
            ],
            "--report-path",
        ),
        (["sync", "--all"], "--all-focal"),
        (["survey", "prepare", "--all"], "--all-surveys"),
        (
            [
                "survey",
                "parity-check",
                "--source-id",
                "SV_A",
                "--b",
                "SV_B",
            ],
            "--source-id/--target-id",
        ),
        (
            ["survey", "export-side-by-side", "--a", "SV_A", "--b", "SV_B"],
            "--source-id/--target-id",
        ),
    ],
)
def test_removed_aliases_fail_with_actionable_guidance(
    argv: list[str], expected: str
) -> None:
    from qsync.cli import main

    with pytest.raises(SystemExit) as exc:
        main(argv)

    assert expected in str(exc.value)


def test_parity_check_accepts_canonical_source_target_flags(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from qsync.cli import main

    ensure_qsync_workspace(tmp_path)
    seen: dict[str, str] = {}

    def _fake_handle(args) -> None:
        seen["source_id"] = str(getattr(args, "source_id", ""))
        seen["target_id"] = str(getattr(args, "target_id", ""))

    monkeypatch.setattr("qsync.cli_survey.handle_parity_check", _fake_handle)

    main(
        [
            "--root",
            str(tmp_path),
            "survey",
            "parity-check",
            "--source-id",
            "SV_A",
            "--target-id",
            "SV_B",
        ]
    )

    assert seen == {"source_id": "SV_A", "target_id": "SV_B"}


def test_export_side_by_side_accepts_canonical_source_target_flags(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from qsync.cli import main

    ensure_qsync_workspace(tmp_path)
    seen: dict[str, str] = {}

    def _fake_handle(args) -> None:
        seen["source_id"] = str(getattr(args, "source_id", ""))
        seen["target_id"] = str(getattr(args, "target_id", ""))

    monkeypatch.setattr("qsync.cli_survey.handle_export_side_by_side", _fake_handle)

    main(
        [
            "--root",
            str(tmp_path),
            "survey",
            "export-side-by-side",
            "--source-id",
            "SV_A",
            "--target-id",
            "SV_B",
        ]
    )

    assert seen == {"source_id": "SV_A", "target_id": "SV_B"}
