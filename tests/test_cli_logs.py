from __future__ import annotations

import io
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

from tests.workspace_helpers import ensure_qsync_workspace


def _touch_env(root: Path) -> None:
    (root / ".env").write_text("", encoding="utf-8")


def test_cli_logs_rotate_and_archives_commands() -> None:
    from qsync.cli import main

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ensure_qsync_workspace(root)
        _touch_env(root)
        log_file = root / "logs" / "qualtrics_push.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text('{"action":"qsync.test","status":200}\n', encoding="utf-8")

        rotate_out = io.StringIO()
        with redirect_stdout(rotate_out):
            main(["--root", str(root), "logs", "rotate", "--force"])
        assert "Rotated log" in rotate_out.getvalue()

        archives_out = io.StringIO()
        with redirect_stdout(archives_out):
            main(["--root", str(root), "logs", "archives"])
        assert "qualtrics_push.log." in archives_out.getvalue()


def test_cli_logs_recent_include_archives_reads_rotated_entries() -> None:
    from qsync.cli import main

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ensure_qsync_workspace(root)
        _touch_env(root)
        log_file = root / "logs" / "qualtrics_push.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text('{"action":"qsync.test","status":200}\n', encoding="utf-8")

        with redirect_stdout(io.StringIO()):
            main(["--root", str(root), "logs", "rotate", "--force"])

        out = io.StringIO()
        with redirect_stdout(out):
            main(["--root", str(root), "logs", "recent", "--include-archives"])
        text = out.getvalue()
        assert "qsync.test" in text


def test_cli_logs_session_filters_by_session_id() -> None:
    from qsync.cli import main

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ensure_qsync_workspace(root)
        _touch_env(root)
        log_file = root / "logs" / "qualtrics_push.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text(
            '\n'.join(
                [
                    '{"action":"qsync.a","session_id":"S1","status":200}',
                    '{"action":"qsync.b","session_id":"S2","status":200}',
                    '{"action":"qsync.c","session_id":"S1","status":500}',
                ]
            )
            + '\n',
            encoding="utf-8",
        )

        out = io.StringIO()
        with redirect_stdout(out):
            main(["--root", str(root), "logs", "session", "S1"])
        text = out.getvalue()
        assert "qsync.a" in text
        assert "qsync.c" in text
        assert "qsync.b" not in text


def test_cli_logs_report_json_output() -> None:
    from qsync.cli import main

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ensure_qsync_workspace(root)
        _touch_env(root)
        log_file = root / "logs" / "qualtrics_push.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text(
            '\n'.join(
                [
                    '{"timestamp":"2026-02-20T10:00:00+00:00","action":"qsync.test","status":200}',
                    '{"timestamp":"2026-02-20T10:01:00+00:00","action":"qsync.test","status":500,"error":{"type":"HTTPError","message":"boom"}}',
                ]
            )
            + '\n',
            encoding="utf-8",
        )

        out = io.StringIO()
        with redirect_stdout(out):
            main(["--root", str(root), "logs", "report", "--json"])
        text = out.getvalue()
        assert '"totals"' in text
        assert '"patterns"' in text
        assert '"systemic_issues"' in text


def test_cli_logs_help_lists_new_analysis_commands() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "qsync.cli", "logs", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    help_text = result.stdout
    assert "session" in help_text
    assert "slow" in help_text
    assert "report" in help_text
