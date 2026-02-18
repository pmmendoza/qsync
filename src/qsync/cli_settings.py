"""Interactive settings command center for qsync."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

from .cli_account import (
    handle_account_cache_dir,
    handle_account_clear,
    handle_account_list,
    handle_account_status,
    handle_account_use,
)
from .cli_survey import handle_inventory, handle_menu, handle_prepare
from .config import resolve_root
from .interactive_menu import is_interactive, select_from_list, text_input
from .terminal_output import format_elapsed


def _run_cli_subcommand(argv: list[str]) -> int:
    root = resolve_root(required=False) or Path.cwd()
    cmd = [sys.executable, "-m", "qsync.cli", "--root", str(root), *argv]
    result = subprocess.run(cmd, check=False)
    return int(result.returncode)


def _run_timed(label: str, func) -> None:
    started = time.perf_counter()
    try:
        func()
    except SystemExit as exc:
        code = getattr(exc, "code", None)
        if code not in (None, 0):
            print(str(exc))
    except Exception as exc:  # noqa: BLE001
        print(f"[qsync:settings] ERROR in {label}: {exc}")
    finally:
        elapsed = format_elapsed(time.perf_counter() - started)
        print(f"[qsync:settings] {label} completed in {elapsed}.")


def handle_settings(args: argparse.Namespace) -> None:
    """Interactive workspace settings hub."""

    if not is_interactive():
        raise SystemExit("[qsync:settings] ERROR: Interactive TTY required.")

    if bool(getattr(args, "tui", False)):
        if (os.environ.get("QSYNC_JSON_MODE") or "").strip():
            raise SystemExit("[qsync:settings] ERROR: JSON mode is not compatible with TUI settings.")
        try:
            from .tui.app import QsyncTuiApp
        except Exception:
            raise SystemExit("[qsync:settings] ERROR: TUI dependencies are not installed. Install: pip install 'qsync[tui]'")
        QsyncTuiApp(start_screen="settings").run()
        return

    while True:
        choice = select_from_list(
            "qsync settings",
            [
                "Account status",
                "List accounts",
                "Set active account",
                "Clear active account",
                "Refresh inventory (no counts)",
                "Refresh inventory (focal counts)",
                "Prepare surfaces",
                "Configure survey cache folder",
                "Doctor check API (/whoami)",
                "Open survey menu",
                "Help topics",
                "Exit",
            ],
            instruction="Centralized account/workspace controls.",
        )
        if not choice or choice == "Exit":
            return

        if choice == "Account status":
            _run_timed(
                "account status",
                lambda: handle_account_status(argparse.Namespace(json=False)),
            )
            continue

        if choice == "List accounts":
            _run_timed(
                "account list",
                lambda: handle_account_list(argparse.Namespace(json=False)),
            )
            continue

        if choice == "Set active account":
            raw = (text_input("Account name (`default` clears selection):") or "").strip()
            if not raw:
                print("[qsync:settings] Cancelled.")
                continue
            if raw.lower() == "default":
                _run_timed(
                    "account clear",
                    lambda: handle_account_clear(argparse.Namespace(json=False)),
                )
            else:
                _run_timed(
                    "account use",
                    lambda: handle_account_use(argparse.Namespace(account=raw, json=False)),
                )
            continue

        if choice == "Clear active account":
            _run_timed(
                "account clear",
                lambda: handle_account_clear(argparse.Namespace(json=False)),
            )
            continue

        if choice == "Refresh inventory (no counts)":
            _run_timed(
                "survey inventory",
                lambda: handle_inventory(
                    argparse.Namespace(
                        counts_scope=None,
                        survey_ids=None,
                        dry_run=False,
                        quiet=False,
                        progress=True,
                        progress_only=False,
                    )
                ),
            )
            continue

        if choice == "Refresh inventory (focal counts)":
            _run_timed(
                "survey inventory --focal",
                lambda: handle_inventory(
                    argparse.Namespace(
                        counts_scope="focal",
                        survey_ids=None,
                        dry_run=False,
                        quiet=False,
                        progress=True,
                        progress_only=False,
                    )
                ),
            )
            continue

        if choice == "Prepare surfaces":
            _run_timed(
                "survey prepare",
                lambda: handle_prepare(
                    argparse.Namespace(
                        survey_id=None,
                        focal=False,
                        all_surveys=False,
                        yes=False,
                        surfaces=None,
                        language=None,
                        languages=None,
                        overwrite_js=False,
                        shared_js=False,
                    )
                ),
            )
            continue

        if choice == "Configure survey cache folder":
            raw = (
                text_input(
                    "Cache subfolder name (`default` clears preference; blank cancels):"
                )
                or ""
            ).strip()
            if not raw:
                print("[qsync:settings] Cancelled.")
                continue
            if raw.lower() == "default":
                _run_timed(
                    "account cache-dir --clear",
                    lambda: handle_account_cache_dir(
                        argparse.Namespace(value=None, clear=True, json=False)
                    ),
                )
            else:
                _run_timed(
                    "account cache-dir",
                    lambda: handle_account_cache_dir(
                        argparse.Namespace(value=raw, clear=False, json=False)
                    ),
                )
            continue

        if choice == "Doctor check API (/whoami)":
            _run_timed("doctor --check-api", lambda: _run_cli_subcommand(["doctor", "--check-api"]))
            continue

        if choice == "Open survey menu":
            _run_timed(
                "survey menu",
                lambda: handle_menu(
                    argparse.Namespace(
                        tui=False,
                        structural_edit=False,
                        add_question_interactive=False,
                        move_question_interactive=False,
                        survey_id=None,
                        account=None,
                        quick_action="",
                    )
                ),
            )
            continue

        _run_timed("help topics", lambda: _run_cli_subcommand(["help", "topics"]))
