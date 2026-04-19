"""
Survey management CLI commands for qsync.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import csv
import json
import os
import re
import sys
import time
import zipfile
from difflib import unified_diff
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

from .config import (
    get_active_account,
    get_client_config,
    load_account_env,
    load_env,
    resolve_root,
    resolve_scoped_dir,
    resolve_survey_cache_base_dir,
    resolve_survey_cache_dir,
    resolve_survey_cache_subdir,
    validate_survey_cache_subdir,
)
from .api_push import send_api_request
from .survey_registry import ensure_unique_survey_name
from .survey_inventory import refresh_inventory, SURVEY_CACHE
from .qualtrics_client import (
    download_survey_definition,
    find_cached_survey_file,
    publish_survey_definition,
    list_survey_versions,
    fetch_survey_version,
    SURVEY_VERSION_DESCRIPTION_MAX_CHARS,
)
from .dimensions import blocks as blocks_dimension
from .publish_description import make_publish_description
from .push_policy import load_push_context
from .response_exports import (
    DEFAULT_RESPONSE_EXPORT_FORMAT,
    SUPPORTED_RESPONSE_EXPORT_FORMATS,
    build_response_export_payload,
    normalize_response_export_format,
)
from .survey_lock import SurveyLockedError, ensure_unlocked
from .scope_filter import ScopeFilter
from .workspace_paths import edf_presets_candidates, resolve_edf_presets_path


def _workspace_root() -> Path:
    return resolve_root(required=False) or Path.cwd()


def _inventory_csv_path(root: Path) -> Path:
    surveys_dir = resolve_scoped_dir("surveys", root=root)
    path = (surveys_dir / "inventory.csv").resolve()
    if path.exists():
        return path
    legacy = (surveys_dir / "qualtrics_surveys.csv").resolve()
    return legacy if legacy.exists() else path


def _iter_inventory_rows(csv_path: Path) -> Iterable[dict]:
    if not csv_path.exists():
        return []
    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(
            line for line in fh if not line.lstrip().startswith("#")
        )
        return list(reader)


def _resolve_pull_dest(
    root: Path, account: str | None, explicit_dest: str | None
) -> Path:
    if explicit_dest:
        path = Path(explicit_dest)
        if path.is_absolute():
            return path
        return (root / path).resolve()
    return resolve_survey_cache_dir(root=root, account=account)


def _resolve_responses_output_dir(
    root: Path, account: str | None, explicit_output: str | None
) -> Path:
    if explicit_output:
        path = Path(explicit_output)
        if path.is_absolute():
            return path
        return (root / path).resolve()
    return resolve_scoped_dir("responses", root=root, account=account)


def _pick_survey_id_from_records(
    message: str,
    records: list[dict[str, Any]],
) -> str | None:
    """Prompt interactively for a survey from pre-fetched records."""

    from .survey_selection import pick_survey_id_from_records

    return pick_survey_id_from_records(message=message, records=records)


def _pick_survey_ids_from_records(
    message: str,
    records: list[dict[str, Any]],
) -> list[str] | None:
    """Prompt interactively for one or more surveys from pre-fetched records."""

    from .survey_selection import pick_survey_ids_from_records

    return pick_survey_ids_from_records(
        message=message,
        records=records,
        include_back=False,
        allow_multiple=True,
    )


def _normalize_survey_ids(value: object) -> list[str]:
    ids: list[str] = []

    if value is None:
        return ids
    if isinstance(value, list):
        values = value
    else:
        values = [value]

    for item in values:
        if item is None:
            continue
        if isinstance(item, str):
            parts = [part.strip() for part in item.split(",") if part.strip()]
        else:
            item_str = str(item).strip()
            parts = [part.strip() for part in item_str.split(",") if part.strip()]
        ids.extend(parts)
    return ids


def _prompt_for_survey_ids_api_if_needed(
    *,
    survey_ids: object,
    args: argparse.Namespace,
    message: str,
    allow_multiple: bool = False,
) -> list[str]:
    """Prompt for SurveyIDs using live API list when omitted (interactive only)."""

    existing_ids = _normalize_survey_ids(survey_ids)
    if existing_ids:
        return existing_ids

    from .interactive_menu import is_interactive

    if not is_interactive():
        raise SystemExit(
            "[qsync] ERROR: --survey-id is required in non-interactive mode."
        )

    base, headers = _get_client_config_for_args(args)
    from .survey_selection import pick_survey_ids_from_api

    picked = pick_survey_ids_from_api(
        message=message,
        base_url=base,
        headers=headers,
        include_back=False,
        allow_multiple=allow_multiple,
    )
    if not picked:
        raise SystemExit("[qsync] Cancelled.")
    return picked


def _prompt_for_survey_id_api_if_needed(
    *,
    survey_id: str | None,
    args: argparse.Namespace,
    message: str,
) -> str:
    """Prompt for SurveyID using live API list when omitted (interactive only)."""

    picked = _prompt_for_survey_ids_api_if_needed(
        survey_ids=survey_id,
        args=args,
        message=message,
        allow_multiple=False,
    )
    return picked[0]


def _default_xlsx_path_for_survey(survey_id: str) -> Path:
    from .workbook_resolver import WorkbookResolver

    resolver = WorkbookResolver(root=_workspace_root())
    return resolver.default_path(survey_id)


def _collect_languages_from_args(args: argparse.Namespace) -> list[str] | None:
    languages: list[str] = []
    raw_list = getattr(args, "language", None)
    if raw_list:
        if isinstance(raw_list, str):
            languages.append(raw_list)
        else:
            languages.extend(raw_list)
    raw_csv = getattr(args, "languages", None)
    if raw_csv:
        for item in str(raw_csv).split(","):
            item = item.strip()
            if item:
                languages.append(item)
    return languages or None


PLACEHOLDER_EMBEDDED_FIELD = "Create New Field or Choose From Dropdown..."


def _fetch_survey_flow(survey_id: str) -> dict:
    base, headers = get_client_config()
    resp = send_api_request(
        action="qsync.survey.flow.fetch",
        method="GET",
        base_url=base,
        headers=headers,
        path=f"survey-definitions/{survey_id}",
        survey_id=survey_id,
        timeout=60,
    )
    payload = resp.json()
    return payload.get("result", {}).get("SurveyFlow", {})


def _dedupe_embedded_data(
    flow: dict, *, placeholder_only: bool
) -> tuple[int, list[dict]]:
    removed = 0
    details: list[dict] = []

    def walk(flow_list: list) -> None:
        nonlocal removed
        for node in flow_list or []:
            if not isinstance(node, dict):
                continue
            if node.get("Type") == "EmbeddedData":
                entries = node.get("EmbeddedData", []) or []
                if entries:
                    seen: set[str] = set()
                    cleaned = []
                    for entry in entries:
                        field = str(entry.get("Field") or "").strip()
                        if not field:
                            cleaned.append(entry)
                            continue
                        if placeholder_only and field != PLACEHOLDER_EMBEDDED_FIELD:
                            cleaned.append(entry)
                            continue
                        if field in seen:
                            removed += 1
                            details.append(
                                {
                                    "flow_id": node.get("FlowID"),
                                    "field": field,
                                }
                            )
                            continue
                        seen.add(field)
                        cleaned.append(entry)
                    if len(cleaned) != len(entries):
                        node["EmbeddedData"] = cleaned
            subflow = node.get("Flow")
            if isinstance(subflow, list):
                walk(subflow)

    walk(flow.get("Flow", []) if isinstance(flow, dict) else [])
    return removed, details


def handle_cleanup_embedded_data(args: argparse.Namespace) -> None:
    survey_id = _prompt_for_survey_id_api_if_needed(
        survey_id=getattr(args, "survey_id", None),
        args=args,
        message="Select a survey to clean up embedded data:",
    )
    placeholder_only = not bool(args.all_duplicates)
    apply_changes = bool(args.apply)
    dry_run = bool(args.dry_run) or not apply_changes

    flow = _fetch_survey_flow(survey_id)
    removed, details = _dedupe_embedded_data(flow, placeholder_only=placeholder_only)

    if removed == 0:
        print("[qsync:survey:cleanup-embedded-data] No duplicate embedded data rows found.")
        return

    scope = "placeholder duplicates only" if placeholder_only else "all duplicates"
    print(
        f"[qsync:survey:cleanup-embedded-data] Found {removed} duplicate embedded data row(s) "
        f"({scope})."
    )
    for item in details[:10]:
        flow_id = item.get("flow_id") or "?"
        field = item.get("field") or "?"
        print(f"  - FlowID {flow_id}: {field}")
    if len(details) > 10:
        print(f"  - ... {len(details) - 10} more")

    if dry_run:
        print("[qsync:survey:cleanup-embedded-data] Dry run only; no changes applied.")
        return

    if not args.yes:
        if not sys.stdin.isatty():
            raise SystemExit(
                "[qsync:survey:cleanup-embedded-data] Non-interactive session requires --yes"
            )
        try:
            from qsync.interactive_menu import confirm

            if not confirm(
                f"Apply embedded data cleanup to {survey_id}?", default=True
            ):
                print("[qsync:survey:cleanup-embedded-data] Aborted.")
                return
        except Exception:
            resp = (
                input(f"Apply embedded data cleanup to {survey_id}? [Y/n] ")
                .strip()
                .lower()
            )
            if resp and resp not in {"y", "yes"}:
                print("[qsync:survey:cleanup-embedded-data] Aborted.")
                return

    base, headers = get_client_config()
    send_api_request(
        action="qsync.survey.flow.cleanup",
        method="PUT",
        base_url=base,
        headers=headers,
        path=f"survey-definitions/{survey_id}/flow",
        survey_id=survey_id,
        json=flow,
        timeout=60,
    )

    if args.publish:
        description = (args.description or "").strip() or "qsync cleanup: embedded data"
        publish_survey_definition(
            survey_id,
            description=description,
            published=True,
            context={"origin": "qsync.cleanup.embedded_data"},
        )
        print("[qsync:survey:cleanup-embedded-data] Applied and published cleanup.")
    else:
        print("[qsync:survey:cleanup-embedded-data] Applied cleanup (not published).")


def handle_label(args: argparse.Namespace) -> None:
    """Print a human-readable label for a survey ID using the inventory CSV."""
    survey_id = args.survey_id
    root = _workspace_root()
    csv_path = _inventory_csv_path(root)
    if not csv_path.exists():
        print(survey_id)
        return
    for row in _iter_inventory_rows(csv_path):
        if (row.get("id") or "").strip() == survey_id:
            name = (row.get("name") or survey_id).strip()
            print(f"{survey_id} - {name}")
            return
    print(survey_id)


def handle_focal(args: argparse.Namespace) -> None:
    """Print SurveyIDs marked focal in the inventory CSV."""
    root = _workspace_root()
    csv_path = _inventory_csv_path(root)
    if not csv_path.exists():
        raise SystemExit(
            f"Inventory CSV not found at {csv_path}. Run 'qsync survey inventory' first."
        )
    focal_ids: list[str] = []
    for row in _iter_inventory_rows(csv_path):
        survey_id = (row.get("id") or "").strip()
        if not survey_id:
            continue
        focal = (row.get("focal") or "").strip().lower()
        if focal in {"true", "1", "yes", "y"}:
            focal_ids.append(survey_id)
    if args.newline:
        for sid in focal_ids:
            print(sid)
        return
    print(" ".join(focal_ids))


def _resolve_account_from_args(args: argparse.Namespace) -> str | None:
    raw = getattr(args, "account", None)
    if isinstance(raw, str):
        name = raw.strip()
        if name:
            if name.lower() == "default":
                return None
            return name
    try:
        from .config import get_active_account

        active = get_active_account()
        if isinstance(active, str) and active.strip().lower() == "default":
            return None
        return active
    except Exception:
        return None


def _get_client_config_for_args(args: argparse.Namespace) -> tuple[str, dict]:
    account = _resolve_account_from_args(args)
    if account:
        env = load_account_env(account, root=_workspace_root())
        return get_client_config(env)
    return get_client_config()


def _emit_active_account_banner(
    *,
    args: argparse.Namespace | None,
    action: str,
    base_url: str | None = None,
    prefix: str = "[account-preflight]",
) -> None:
    """Print a concise active-account preflight line for write-heavy actions."""

    account = "default"
    if args is not None:
        try:
            account = _resolve_account_from_args(args) or "default"
        except Exception:
            account = "default"
    resolved_base = (base_url or "").strip()
    if not resolved_base and args is not None:
        try:
            resolved_base = _get_client_config_for_args(args)[0]
        except Exception:
            resolved_base = ""
    print(
        f"{prefix} action={action} account={account} "
        f"base_url={resolved_base or '(unknown)'}"
    )


_SURVEY_WRITE_PREFLIGHT_ACTIONS: dict[str, str] = {
    # Remote/account-scoped writes
    "copy": "copy",
    "copy-cross-account": "copy-cross-account",
    "slice-language": "slice-language",
    "rename": "rename",
    "delete": "delete",
    "cleanup-embedded-data": "cleanup-embedded-data",
    "prolific-auth": "prolific-auth",
    "publish": "publish",
    "activate": "activate",
    "deactivate": "deactivate",
    "rollback": "rollback",
    # Local workspace writes
    "inventory": "inventory-refresh",
    "prepare": "prepare-surfaces",
}

_SURVEY_MASTER_WRITE_PREFLIGHT_ACTIONS: dict[str, str] = {
    "pull": "survey-master-pull",
    "stage": "survey-master-stage",
    "push": "survey-master-push",
    "rollback": "survey-master-rollback",
}


def maybe_emit_survey_write_preflight(args: argparse.Namespace) -> None:
    """Emit account/base preflight line for direct write-heavy survey commands."""

    sub = str(getattr(args, "survey_command", "") or "").strip()
    if not sub:
        return

    action = ""
    if sub == "master":
        master_sub = str(getattr(args, "master_command", "") or "").strip()
        action = _SURVEY_MASTER_WRITE_PREFLIGHT_ACTIONS.get(master_sub, "")
    else:
        action = _SURVEY_WRITE_PREFLIGHT_ACTIONS.get(sub, "")

    if not action:
        return

    _emit_active_account_banner(args=args, action=action)


def _discover_account_env_files(*, root: Path) -> list[str]:
    """Return account names for `.env.<account>` files under root (best-effort)."""

    accounts: list[str] = []
    for path in sorted(root.glob(".env.*")):
        # ".env.<account>" only; ignore templates/examples and other dotfiles.
        if not path.is_file():
            continue
        if path.name in {".env.example", ".env.template"}:
            continue
        account = path.name.split(".env.", 1)[-1].strip()
        if not account or account == path.name:
            continue
        try:
            # Validate + ensure required keys are present (base url + token).
            load_account_env(account, root=root)
        except Exception:
            continue
        accounts.append(account)
    return accounts


def _typed_confirmation(
    *,
    prompt: str,
    expected: str,
    input_fn=input,
) -> bool:
    """Require the user to type an exact confirmation string (interactive guardrail)."""

    try:
        typed = str(input_fn(prompt) or "").strip()
    except (EOFError, KeyboardInterrupt):
        return False
    return typed == expected


def _confirm_interactive_gate(*, prompt: str, default: bool = False) -> bool:
    """Prompt for an interactive yes/no decision with a safe default."""

    try:
        from .interactive_menu import confirm

        return bool(confirm(prompt, default=default))
    except Exception:
        suffix = "[Y/n]" if default else "[y/N]"
        raw = input(f"{prompt} {suffix}: ").strip().lower()
        if not raw:
            return default
        return raw in {"y", "yes"}


def _optional_int(value: Any) -> int | None:
    """Best-effort integer parser used for response counts."""

    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _status_response_counts(status_payload: Mapping[str, Any]) -> tuple[int | None, int | None]:
    """Extract (live, preview) response counts from a survey status payload."""

    counts = status_payload.get("responseCounts")
    if not isinstance(counts, Mapping):
        return (None, None)
    live = _optional_int(counts.get("auditable"))
    preview = _optional_int(counts.get("generated"))
    return (live, preview)


def handle_menu(args: argparse.Namespace) -> None:
    """Interactive wizard for common `qsync survey ...` operations."""

    from .interactive_menu import (
        MenuItem,
        confirm,
        is_interactive,
        multi_select_from_list,
        select_from_list,
    )

    if not is_interactive():
        raise SystemExit("[survey-menu] ERROR: Interactive TTY required.")

    # Optional: launch Textual TUI survey menu (opt-in; keep conventional menu intact).
    if bool(getattr(args, "tui", False)):
        if (os.environ.get("QSYNC_JSON_MODE") or "").strip():
            raise SystemExit("[survey-menu] ERROR: JSON mode is not compatible with the TUI.")
        try:
            from .tui.app import QsyncTuiApp  # lazy import (Textual is optional)
        except Exception:
            print("[survey-menu] ERROR: TUI dependencies are not installed.")
            print("  Install: pip install 'qsync[tui]'")
            raise SystemExit(1)
        QsyncTuiApp(start_screen="survey_menu").run()
        return

    root = _workspace_root()
    selected_account: str | None = None  # None = inherited ambient account
    requested_account = str(getattr(args, "account", "") or "").strip()
    if requested_account:
        if requested_account.lower() == "default":
            # Explicit default account selection must bypass inherited ambient
            # account context (for example a workspace-active named account).
            selected_account = "default"
        else:
            try:
                # Validate account + ensure credentials resolve before opening the menu.
                load_account_env(requested_account, root=root)
            except Exception as exc:
                raise SystemExit(
                    f"[survey-menu] ERROR: invalid --account '{requested_account}': {exc}"
                )
            selected_account = requested_account

    # Cache survey lists per account scope + base_url for responsiveness within a
    # menu session. Scope is part of the cache key so accounts sharing the same
    # base URL do not reuse each other's survey listings.
    survey_cache: dict[str, list[dict[str, Any]]] = {}

    def _resolve_menu_account() -> str | None:
        if selected_account is not None:
            return selected_account
        try:
            return get_active_account()
        except Exception:
            return None

    def _load_primary_env_for_menu() -> dict[str, str]:
        from .config import load_env, resolve_env_path

        env_path = resolve_env_path(root=root)
        return load_env(env_path)

    def _get_client_for_scope(account_scope: str | None) -> tuple[str, dict]:
        scope = str(account_scope or "").strip()
        if scope.lower() == "default":
            return get_client_config(_load_primary_env_for_menu())
        if scope:
            env = load_account_env(scope, root=root)
            return get_client_config(env)
        return get_client_config()

    def _resolve_menu_client() -> tuple[str, dict]:
        return _get_client_for_scope(_resolve_menu_account())

    def _menu_account_base() -> str | None:
        try:
            base, _headers = _get_client_for_scope(_resolve_menu_account())
            return base
        except Exception:
            return None

    def _account_label() -> str:
        return _resolve_menu_account() or "default"

    def _resolve_base_url_for_display() -> str | None:
        return _menu_account_base()

    def _get_client() -> tuple[str, dict]:
        return _resolve_menu_client()

    def _get_surveys() -> list[dict[str, Any]]:
        return _get_surveys_for_account(account=_resolve_menu_account())

    def _pick_survey_id(*, message: str) -> str | None:
        try:
            surveys = _get_surveys()
        except Exception as exc:
            print(f"[survey-menu] ERROR: unable to list surveys: {exc}")
            return None
        return _pick_survey_id_from_records(message=message, records=surveys)

    def _pick_survey_ids(*, message: str) -> list[str] | None:
        try:
            surveys = _get_surveys()
        except Exception as exc:
            print(f"[survey-menu] ERROR: unable to list surveys: {exc}")
            return None
        return _pick_survey_ids_from_records(message=message, records=surveys)

    def _run_action(func, ns: argparse.Namespace) -> None:
        write_action = str(getattr(ns, "_preflight_action", "") or "").strip()
        if not write_action:
            implicit_write_actions = {
                "handle_activate": "activate",
                "handle_deactivate": "deactivate",
                "handle_publish": "publish",
                "handle_rollback": "rollback",
                "handle_copy": "copy",
                "handle_copy_cross_account": "copy-cross-account",
                "handle_delete": "delete",
                "handle_rename": "rename",
                "handle_cleanup_embedded_data": "cleanup-embedded-data",
                "handle_prolific_auth": "prolific-auth",
                "handle_prolific_wiring": "prolific-wiring",
                "handle_add_question": "add-question",
                "handle_move_question": "move-question",
                "handle_remove_question": "remove-question",
                "handle_add_page_break": "add-page-break",
                "handle_remove_page_break": "remove-page-break",
                "handle_push_question": "push-question",
                "handle_master_pull": "survey-master-pull",
                "handle_master_stage": "survey-master-stage",
                "handle_master_push": "survey-master-push",
                "handle_master_rollback": "survey-master-rollback",
            }
            write_action = implicit_write_actions.get(getattr(func, "__name__", ""), "")
        if write_action:
            _emit_active_account_banner(
                args=argparse.Namespace(account=_resolve_menu_account()),
                action=write_action,
                base_url=_resolve_base_url_for_display(),
                prefix="[survey-menu]",
            )
        try:
            func(ns)
        except SystemExit as exc:
            # Keep the wizard alive on subcommand exits.
            code = getattr(exc, "code", None)
            if code not in (None, 0):
                print(str(exc))
        except Exception as exc:  # noqa: BLE001
            print(f"[survey-menu] ERROR: {exc}")

    def _require_default_account(*, action: str) -> bool:
        scope = str(_resolve_menu_account() or "").strip().lower()
        if not scope or scope == "default":
            return True
        print(
            f"[survey-menu] '{action}' is workspace-mutating and is only supported on the default account in this menu."
        )
        print("  Next: Switch account → default and retry.")
        return False

    def _menu_switch_account() -> None:
        nonlocal selected_account
        accounts = _discover_account_env_files(root=root)
        choices = ["default", *accounts, "↩ Back"]
        selection = select_from_list("Select account:", choices)
        if not selection or selection.endswith("Back"):
            return
        if selection == "default":
            selected_account = "default"
        else:
            selected_account = selection
        survey_cache.clear()

    def _menu_show_account_info() -> None:
        base = _resolve_base_url_for_display() or "(not configured)"
        print(f"[survey-menu] account={_account_label()} base_url={base}")
        try:
            resolved_base, headers = _get_client()
            token_present = bool(headers.get("X-API-TOKEN"))
            print(
                f"[survey-menu] resolved_base_url={resolved_base} token_present={token_present}"
            )
        except Exception as exc:
            print(f"[survey-menu] NOTE: could not resolve API client ({exc})")

    def _menu_check_api() -> None:
        try:
            base, headers = _get_client()
        except Exception as exc:
            print(f"[survey-menu] ERROR: could not resolve API client: {exc}")
            return
        resp = send_api_request(
            action="qsync.survey.menu.whoami",
            method="GET",
            base_url=base,
            headers=headers,
            path="whoami",
            log_event=False,
            timeout=15,
        )
        result = resp.json().get("result") or {}
        datacenter = (result.get("datacenter") or "").strip()
        user_id = (result.get("userId") or "").strip()
        print(
            f"[survey-menu] whoami account={_account_label()} datacenter={datacenter or '(unknown)'} userId={user_id or '(unknown)'}"
        )

    def _menu_delete() -> None:
        mode = select_from_list(
            "Delete: how do you want to select surveys?",
            [
                "Pick from survey list (repeat)",
                "Enter SurveyIDs manually",
                "↩ Back",
            ],
        )
        if not mode or mode.endswith("Back"):
            return

        survey_ids: list[str] = []
        if mode.startswith("Enter"):
            raw = input("Enter one or more SurveyIDs (space/comma separated): ").strip()
            for token in raw.replace(",", " ").split():
                token = token.strip()
                if token:
                    survey_ids.append(token)
        else:
            while True:
                picked = _pick_survey_id(message="Pick a survey to delete:")
                if not picked:
                    break
                if picked not in survey_ids:
                    survey_ids.append(picked)
                again = select_from_list("Add another?", ["Yes", "No (continue)"])
                if not again or again.startswith("No"):
                    break

        survey_ids = list(
            dict.fromkeys([sid.strip() for sid in survey_ids if sid.strip()])
        )
        if not survey_ids:
            return

        print()
        print("[survey-menu] WARNING: Delete is permanent in Qualtrics.")
        print("[survey-menu] Default behavior is dry-run.")
        print("[survey-menu] Account:", _account_label())
        print("[survey-menu] Surveys:", ", ".join(survey_ids))
        print("[survey-menu] You will be guided through per-survey confirmation gates.")
        print()

        _run_action(
            handle_delete,
            argparse.Namespace(
                survey_ids=survey_ids,
                account=selected_account,
                yes=False,
                force_live=False,
            ),
        )

    def _menu_rename() -> None:
        survey_id = _pick_survey_id(message="Pick a survey to rename:")
        if not survey_id:
            return
        new_name = input("Enter new survey name: ").strip()
        if not new_name:
            return
        _run_action(
            handle_rename,
            argparse.Namespace(
                survey_id=survey_id,
                new_name=new_name,
                account=selected_account,
            ),
        )

    def _menu_activate(*, active: bool) -> None:
        survey_ids = _pick_survey_ids(
            message="Pick survey(s) to activate:"
            if active
            else "Pick survey(s) to deactivate:"
        )
        if not survey_ids:
            return
        handler = handle_activate if active else handle_deactivate
        _run_action(
            handler,
            argparse.Namespace(
                survey_id=survey_ids,
                survey_ids_file=None,
                dry_run=False,
                force_live=False,
                yes=False,
                publish=False,
                publish_description="",
                show_versions=False,
                versions_limit=5,
                show_owner=False,
                account=selected_account,
            ),
        )

    def _menu_publish() -> None:
        survey_ids = _pick_survey_ids(message="Pick survey(s) to publish:")
        if not survey_ids:
            return
        desc = input("Version description (max 140 chars): ").strip()
        if not desc:
            return
        _run_action(
            handle_publish,
            argparse.Namespace(
                survey_id=survey_ids,
                description=desc,
                dry_run=False,
                retry_attempts=1,
                account=selected_account,
            ),
        )

    def _menu_versions() -> None:
        survey_id = _pick_survey_id(message="Pick a survey to list versions:")
        if not survey_id:
            return
        _run_action(
            handle_versions,
            argparse.Namespace(
                survey_id=survey_id,
                limit=None,
                json=False,
                account=selected_account,
            ),
        )

    def _menu_version_fetch() -> None:
        survey_id = _pick_survey_id(message="Pick a survey to fetch a version:")
        if not survey_id:
            return
        version_id = input("Enter VersionID: ").strip()
        if not version_id:
            return
        fmt = select_from_list("Format:", ["json", "qsf", "↩ Back"])
        if not fmt or fmt.endswith("Back"):
            return
        out_path = input("Output path (optional): ").strip() or None
        _run_action(
            handle_version_fetch,
            argparse.Namespace(
                survey_id=survey_id,
                version_id=version_id,
                format=fmt,
                output=out_path,
                json=False,
                account=selected_account,
            ),
        )

    def _menu_rollback() -> None:
        survey_id = _pick_survey_id(message="Pick a survey to rollback questions:")
        if not survey_id:
            return
        version_id = input("Enter VersionID to rollback from: ").strip()
        if not version_id:
            return
        qids = input(
            "Enter QIDs to rollback (comma-separated, e.g. QID1,QID2): "
        ).strip()
        if not qids:
            return
        dry_run = (
            select_from_list("Dry run?", ["No", "Yes", "↩ Back"]) == "Yes"
        )
        _run_action(
            handle_rollback,
            argparse.Namespace(
                survey_id=survey_id,
                version_id=version_id,
                question_id=qids,
                description="",
                dry_run=bool(dry_run),
                no_publish=False,
                force_live=False,
                yes=False,
                account=selected_account,
            ),
        )

    def _menu_copy() -> None:
        survey_id = _pick_survey_id(message="Pick a source survey to copy:")
        if not survey_id:
            return
        new_name = input("Enter name for new survey: ").strip()
        if not new_name:
            return
        _run_action(
            handle_copy,
            argparse.Namespace(
                source_survey_id=survey_id,
                name=new_name,
                from_qsf=None,
                project_category=None,
                language=None,
                force_duplicate=False,
                generate_qsf=False,
                account=selected_account,
            ),
        )

    def _menu_slice_language() -> None:
        survey_id = _pick_survey_id(message="Pick a multilingual source survey:")
        if not survey_id:
            return
        langs = input("Target language(s) (e.g. DE or DE,FR-CA): ").strip()
        if not langs:
            return
        _run_action(
            handle_slice_language,
            argparse.Namespace(
                source_survey_id=survey_id,
                language=None,
                languages=langs,
                name=None,
                keep_languages="target-only",
                allow_incomplete=False,
                allow_fallback=False,
                no_flow_text=False,
                dry_run=False,
                yes=False,
                verify_parity=False,
                force_duplicate=False,
                account=selected_account,
            ),
        )

    def _menu_slice_registry() -> None:
        raw_source = input("Filter by source survey ID (optional): ").strip() or None
        raw_limit = input("Limit (optional): ").strip()
        limit = int(raw_limit) if raw_limit.isdigit() else None
        open_links = select_from_list("Open edit links?", ["No", "Yes"]) == "Yes"
        _run_action(
            handle_slice_registry,
            argparse.Namespace(source=raw_source, limit=limit, open=bool(open_links)),
        )

    def _menu_parity_check() -> None:
        survey_a = _pick_survey_id(message="Pick survey A:")
        if not survey_a:
            return
        survey_b = _pick_survey_id(message="Pick survey B:")
        if not survey_b:
            return
        deep = select_from_list("Deep parity?", ["No", "Yes"]) == "Yes"
        _run_action(
            handle_parity_check,
            argparse.Namespace(
                a=survey_a, b=survey_b, deep=bool(deep), account=selected_account
            ),
        )

    def _menu_copy_cross_account() -> None:
        accounts = _discover_account_env_files(root=root)

        def _pick_account(*, label: str) -> str | None:
            choices = ["default (.env)", *accounts, "↩ Back"]
            selection = select_from_list(label, choices)
            if not selection or selection.endswith("Back"):
                return None
            if selection.startswith("default"):
                return "default"
            return selection

        source_acct = _pick_account(label="Select source account:")
        if not source_acct:
            return
        target_acct = _pick_account(label="Select target account:")
        if not target_acct:
            return

        nonlocal selected_account
        prior = selected_account
        try:
            selected_account = source_acct
            survey_id = _pick_survey_id(message="Pick a source survey to copy:")
        finally:
            selected_account = prior
            survey_cache.clear()
        if not survey_id:
            return

        new_name = input(
            "Enter name for the new survey in target account: "
        ).strip()
        if not new_name:
            return

        _run_action(
            handle_copy_cross_account,
            argparse.Namespace(
                source_survey_id=survey_id,
                new_name=new_name,
                target_api_key="",
                target_base_url="",
                # Explicitly allow copying into the primary account when the user
                # picked "default (.env)". `handle_copy_cross_account` treats
                # the literal value "default" as an alias for the primary creds.
                target_account=target_acct,
                source_api_key="",
                source_base_url="",
                source_account=source_acct,
                activate=False,
                publish=False,
                publish_description="",
                force_overwrite=False,
                yes=False,
                no_translations=False,
                verify=False,
                verify_deep=False,
            ),
        )

    def _menu_export_responses() -> None:
        survey_id = _pick_survey_id(message="Pick a survey to export responses:")
        if not survey_id:
            return
        default_out = (
            f"responses/.{selected_account}/" if selected_account else "responses/"
        )
        format_hint = "/".join(SUPPORTED_RESPONSE_EXPORT_FORMATS)
        response_format = (
            input(
                "Export format "
                f"({format_hint}; default: {DEFAULT_RESPONSE_EXPORT_FORMAT}): "
            ).strip()
            or DEFAULT_RESPONSE_EXPORT_FORMAT
        )
        out = (
            input(f"Output directory (optional; default: {default_out}): ").strip()
            or None
        )
        _run_action(
            handle_export_responses,
            argparse.Namespace(
                survey_id=survey_id,
                output=out,
                account=selected_account,
                export_format=response_format,
            ),
        )

    def _menu_export_translation() -> None:
        if not _require_default_account(action="export-translation"):
            return
        survey_ids = _pick_survey_ids(
            message="Pick survey(s) to export translation:"
        )
        if not survey_ids:
            return
        _run_action(
            handle_export_translation,
            argparse.Namespace(survey_id=survey_ids, skip_js_strings=False),
        )

    def _menu_export_side_by_side() -> None:
        if not _require_default_account(action="export-side-by-side"):
            return
        survey_a = _pick_survey_id(message="Pick survey A:")
        if not survey_a:
            return
        survey_b = _pick_survey_id(message="Pick survey B:")
        if not survey_b:
            return

        output = input("Output path (optional; file or directory): ").strip() or None
        smart_name = select_from_list("Append timestamp to filename?", ["No", "Yes"]) == "Yes"
        refresh = select_from_list("Refresh cached definitions first?", ["No", "Yes"]) == "Yes"
        skip_parity = select_from_list("Run parity check first?", ["Yes", "No"]) == "No"
        do_open = select_from_list("Open document after export?", ["No", "Yes"]) == "Yes"

        _run_action(
            handle_export_side_by_side,
            argparse.Namespace(
                a=survey_a,
                b=survey_b,
                output=Path(output) if output else None,
                label_a=None,
                label_b=None,
                skip_parity=bool(skip_parity),
                refresh=bool(refresh),
                smart_name=bool(smart_name),
                no_html=False,
                layout_heuristics=False,
                skip_js_strings=False,
                open=bool(do_open),
            ),
        )

    def _menu_inspect_question() -> None:
        survey_id = _pick_survey_id(message="Pick a survey to inspect a question from:")
        if not survey_id:
            return
        definition = _fetch_definition_for_menu(survey_id)
        if definition is None:
            return
        question_id = _pick_question_id_from_definition(
            definition,
            message="Pick a question to inspect:",
        )
        if not question_id:
            return
        field_choice = select_from_list(
            "Inspect output:",
            [
                "Full question payload",
                "QuestionText",
                "QuestionJS",
                "DataExportTag",
                "Custom field",
                "↩ Back",
            ],
        )
        if not field_choice or field_choice.endswith("Back"):
            return
        field_name = None
        raw = False
        if field_choice == "Custom field":
            entered = input("Enter field name (exact key): ").strip()
            if not entered:
                return
            field_name = entered
            raw = (
                select_from_list("Raw output if field is a string?", ["No", "Yes"])
                == "Yes"
            )
        elif field_choice != "Full question payload":
            field_name = field_choice
            raw = (
                select_from_list("Raw output if field is a string?", ["No", "Yes"])
                == "Yes"
            )
        _run_action(
            handle_inspect_question,
            argparse.Namespace(
                survey_id=survey_id,
                question_id=question_id,
                survey_file=None,
                field=field_name,
                raw=bool(raw),
            ),
        )

    def _menu_push_question() -> None:
        survey_id = _pick_survey_id(message="Pick a survey to push one question to:")
        if not survey_id:
            return
        definition = _fetch_definition_for_menu(survey_id)
        if definition is None:
            return
        question_id = _pick_question_id_from_definition(
            definition,
            message="Pick a question to push:",
        )
        if not question_id:
            return
        dry_run = select_from_list("Dry run only?", ["Yes", "No"]) == "Yes"
        force_live = (
            select_from_list("Allow push with live responses?", ["No", "Yes"]) == "Yes"
        )
        show_diff = select_from_list("Show diff output?", ["Yes", "No"]) == "Yes"
        no_publish = (
            select_from_list("Publish after push?", ["Yes", "No"]) == "No"
        )
        _run_action(
            handle_push_question,
            argparse.Namespace(
                survey_id=survey_id,
                question_id=question_id,
                survey_file=None,
                dry_run=bool(dry_run),
                force_live=bool(force_live),
                yes=False,
                show_diff=bool(show_diff),
                no_publish=bool(no_publish),
                _preflight_action="push-question",
            ),
        )

    def _menu_replace_question(*, preselected_survey_id: str | None = None) -> None:
        target_survey_id = (preselected_survey_id or "").strip() or _pick_survey_id(
            message="Pick target survey to replace a question in:"
        )
        if not target_survey_id:
            return
        target_definition = _fetch_definition_for_menu(target_survey_id)
        if not target_definition:
            return
        target_question_id = _pick_question_id_from_definition(
            target_definition,
            message="Pick target question to replace:",
        )
        if not target_question_id:
            return

        source_scope = select_from_list(
            "Replacement source account:",
            [
                f"Current account ({_account_label()})",
                "Another linked account",
                "↩ Back",
            ],
        )
        if not source_scope or source_scope.endswith("Back"):
            return

        source_account_scope = _resolve_menu_account()
        source_account_arg: str | None = None
        if source_scope.startswith("Another"):
            discovered = _discover_account_env_files(root=root)
            picked_account = select_from_list(
                "Pick source account:",
                ["default", *discovered, "↩ Back"],
            )
            if not picked_account or picked_account.endswith("Back"):
                return
            if picked_account == "default":
                source_account_scope = "default"
                source_account_arg = "default"
            else:
                source_account_scope = picked_account
                source_account_arg = picked_account

        if (
            source_scope.startswith("Current account")
            and source_account_scope == _resolve_menu_account()
        ):
            source_survey_pick = select_from_list(
                "Replacement source survey:",
                [
                    f"Use same survey ({target_survey_id})",
                    "Pick another survey",
                    "↩ Back",
                ],
            )
            if not source_survey_pick or source_survey_pick.endswith("Back"):
                return
            if source_survey_pick.startswith("Use same survey"):
                source_survey_id = target_survey_id
            else:
                source_surveys = _get_surveys_for_account(account=source_account_scope)
                if not source_surveys:
                    print("[survey-menu] No surveys found in source account.")
                    return
                source_survey_id = _pick_survey_id_from_records(
                    message="Pick source survey:",
                    records=source_surveys,
                )
                if not source_survey_id:
                    return
        else:
            source_surveys = _get_surveys_for_account(account=source_account_scope)
            if not source_surveys:
                print("[survey-menu] No surveys found in source account.")
                return
            source_survey_id = _pick_survey_id_from_records(
                message="Pick source survey:",
                records=source_surveys,
            )
            if not source_survey_id:
                return

        source_definition = (
            target_definition
            if source_survey_id == target_survey_id
            and source_account_scope == _resolve_menu_account()
            else _fetch_definition_for_menu(source_survey_id, account=source_account_scope)
        )
        if not source_definition:
            return
        source_question_id = _pick_question_id_from_definition(
            source_definition,
            message="Pick source question to copy from:",
        )
        if not source_question_id:
            return

        replace_data_export_tag = (
            select_from_list(
                "Replace target DataExportTag with source tag?",
                [
                    "No (keep target DataExportTag)",
                    "Yes (copy source DataExportTag)",
                ],
            )
            == "Yes (copy source DataExportTag)"
        )
        dry_run = select_from_list("Dry run?", ["No", "Yes"]) == "Yes"
        force_live = False
        show_diff = True
        publish = False
        publish_description = ""
        if not dry_run:
            force_live = (
                select_from_list(
                    "Allow writes if finished responses exist?",
                    ["No", "Yes"],
                )
                == "Yes"
            )
            show_diff = (
                select_from_list("Show diff output?", ["Yes", "No"]) == "Yes"
            )
            publish = (
                select_from_list("Publish after replace?", ["Yes", "No"]) == "Yes"
            )
            if publish:
                publish_description = (
                    input("Publish description (optional): ").strip() or ""
                )

        _run_action(
            handle_replace_question,
            argparse.Namespace(
                survey_id=target_survey_id,
                question_id=target_question_id,
                source_account=source_account_arg,
                source_survey_id=source_survey_id,
                source_question_id=source_question_id,
                dry_run=bool(dry_run),
                force_live=bool(force_live),
                yes=False,
                show_diff=bool(show_diff),
                replace_data_export_tag=bool(replace_data_export_tag),
                no_publish=not bool(publish),
                publish_description=publish_description,
                account=selected_account,
                interactive_mode=True,
            ),
        )

    def _menu_stage_by_qid() -> None:
        if not _require_default_account(action="stage-by-qid"):
            return
        survey_id = _pick_survey_id(message="Pick a survey to stage by QID scope:")
        if not survey_id:
            return
        definition = _fetch_definition_for_menu(survey_id)
        if definition is None:
            return
        qids = _pick_question_ids_from_definition(
            definition,
            message="Pick one or more QIDs to stage:",
        )
        if not qids:
            return
        from .interactive_menu import multi_select_from_list

        dims = multi_select_from_list(
            message="Dimensions to stage for selected QIDs:",
            choices=["items", "js", "translations"],
            instruction="Space: toggle, Enter: confirm",
        )
        if not dims:
            return
        dims_unique = [d for d in ["items", "js", "translations"] if d in set(dims)]
        if not dims_unique:
            return
        scope_expr = " OR ".join([f"qid:{qid}" for qid in qids])
        print(
            f"[survey-menu] Stage by QID scope: survey={survey_id} dims={','.join(dims_unique)} scope={scope_expr}"
        )
        _emit_active_account_banner(
            args=argparse.Namespace(account=_resolve_menu_account()),
            action="stage-by-qid",
            base_url=_resolve_base_url_for_display(),
            prefix="[survey-menu]",
        )
        from .cli import _main_impl

        cmd = [
            "--root",
            str(root),
            "sync",
            "--survey-id",
            survey_id,
            "--dimensions",
            ",".join(dims_unique),
            "--scope",
            scope_expr,
            "--pending-action",
            "stage",
            "--yes",
        ]
        account_scope = _resolve_menu_account()
        if account_scope:
            cmd = ["--account", account_scope, *cmd]
        try:
            _main_impl(cmd)
        except SystemExit as exc:
            code = getattr(exc, "code", 1)
            if code not in (None, 0):
                print(
                    f"[survey-menu] Stage-by-QID flow failed (exit={code}). Review sync output above."
                )

    def _menu_master() -> None:
        if not _require_default_account(action="survey master"):
            return
        choice = select_from_list(
            "Survey master",
            [
                "Pull focal snapshots + master CSV",
                "Preview staged changes",
                "Stage master CSV changes",
                "Stage by QID (scope sync dimensions)",
                "Push staged master changes",
                "Rollback master snapshot",
                "↩ Back",
            ],
            instruction="Bulk operations are workspace-wide; prefer pull → preview → stage → push.",
        )
        if not choice or choice.endswith("Back"):
            return
        if choice.startswith("Pull"):
            force_overwrite = (
                select_from_list("Force overwrite existing master CSV?", ["No", "Yes"])
                == "Yes"
            )
            _run_action(
                handle_master_pull,
                argparse.Namespace(
                    verbose=False,
                    mapping_csv=None,
                    survey_ids=None,
                    force_overwrite=bool(force_overwrite),
                    _preflight_action="survey-master-pull",
                ),
            )
            return
        if choice.startswith("Preview"):
            detail = select_from_list("Show detailed per-field changes?", ["No", "Yes"]) == "Yes"
            _run_action(
                handle_master_preview,
                argparse.Namespace(
                    verbose=False,
                    mapping_csv=None,
                    detail=bool(detail),
                    survey_id=None,
                    format="text",
                    tags=None,
                    all_surveys=False,
                ),
            )
            return
        if choice.startswith("Stage by QID"):
            _menu_stage_by_qid()
            return
        if choice.startswith("Stage"):
            _run_action(
                handle_master_stage,
                argparse.Namespace(
                    verbose=False,
                    survey_id=None,
                    tags=None,
                    all_surveys=False,
                    _preflight_action="survey-master-stage",
                ),
            )
            return
        if choice.startswith("Push"):
            publish = select_from_list("Publish after push?", ["Yes", "No"]) == "Yes"
            _run_action(
                handle_master_push,
                argparse.Namespace(
                    verbose=False,
                    mapping_csv=None,
                    description=None,
                    survey_id=None,
                    all_surveys=False,
                    no_publish=not bool(publish),
                    force_live=False,
                    force_preview=False,
                    yes=False,
                    allow_dangerous=False,
                    allow_locked=False,
                    _preflight_action="survey-master-push",
                ),
            )
            return
        _run_action(
            handle_master_rollback,
            argparse.Namespace(
                survey_id=None,
                list=True,
                version=1,
                dry_run=True,
                force=False,
                allow_dangerous=False,
                no_publish=False,
                description=None,
                yes=False,
                _preflight_action="survey-master-rollback",
            ),
        )

    def _menu_inventory() -> None:
        if not _require_default_account(action="survey inventory"):
            return
        sel = select_from_list(
            "Inventory refresh:",
            [
                "Refresh (no counts)",
                "Refresh (focal counts)",
                "Refresh (full counts)",
                "Refresh (targeted SurveyIDs)",
                "↩ Back",
            ],
        )
        if not sel or sel.endswith("Back"):
            return
        counts_scope = None
        survey_ids = None
        if "focal" in sel.lower():
            counts_scope = "focal"
        elif "full" in sel.lower():
            counts_scope = "full"
        elif "targeted" in sel.lower():
            raw = input("Enter one or more SurveyIDs (comma-separated): ").strip()
            if not raw:
                return
            survey_ids = [raw]
        _run_action(
            handle_inventory,
            argparse.Namespace(
                counts_scope=counts_scope,
                survey_ids=survey_ids,
                dry_run=False,
                quiet=False,
                progress=False,
                progress_only=False,
            ),
        )

    def _menu_refresh_question_bank_index() -> None:
        target_scope = _resolve_menu_account()
        target_label = _account_label()

        scope_choice = select_from_list(
            "Refresh question-bank index for which account?",
            [
                f"Current menu account ({target_label})",
                "Another linked account",
                "↩ Back",
            ],
            instruction="Indexing scans pulled survey JSON files in that account-scoped cache.",
        )
        if not scope_choice or scope_choice.endswith("Back"):
            return

        if scope_choice.startswith("Another"):
            discovered = _discover_account_env_files(root=root)
            choices = ["default", *discovered, "↩ Back"]
            picked_account = select_from_list("Pick account to index:", choices)
            if not picked_account or picked_account.endswith("Back"):
                return
            if picked_account == "default":
                target_scope = "default"
                target_label = "default"
            else:
                target_scope = picked_account
                target_label = picked_account

        try:
            surveys = _get_surveys_for_account(account=target_scope)
        except Exception as exc:
            print(f"[survey-menu] ERROR: unable to list surveys: {exc}")
            return

        indexed_payload = _build_question_bank_index(
            account=target_scope,
            surveys=surveys,
        )
        indexed_count = int(indexed_payload.get("survey_count") or 0)
        index_path = _question_bank_index_path(account=target_scope)
        print(
            f"[survey-menu] Indexed question bank updated "
            f"(account={target_label}, indexed_surveys={indexed_count}, listed_surveys={len(surveys)})."
        )
        print(f"[survey-menu] Index file: {index_path}")
        print("[survey-menu] Note: only pulled/cached survey JSON files are indexed.")

    def _menu_pull() -> None:
        survey_id = _pick_survey_id(message="Pick a survey to pull (cache JSON):")
        if not survey_id:
            return
        default_dest_path = resolve_survey_cache_dir(root=root, account=selected_account)
        try:
            default_dest = f"{default_dest_path.relative_to(root).as_posix()}/"
        except ValueError:
            default_dest = f"{default_dest_path.as_posix()}/"
        dest = (
            input(f"Destination directory (optional; default: {default_dest}): ").strip()
            or None
        )
        _run_action(
            handle_pull,
            argparse.Namespace(survey_id=survey_id, dest=dest, account=selected_account),
        )

    def _menu_configure_cache_folder() -> None:
        from .workspace_prefs import (
            get_workspace_survey_cache_subdir,
            set_workspace_survey_cache_subdir,
        )

        while True:
            account_scope = _resolve_menu_account()
            surveys_dir = resolve_scoped_dir("surveys", root=root, account=account_scope)
            cache_base_dir = resolve_survey_cache_base_dir(
                root=root, account=account_scope
            )
            pref = get_workspace_survey_cache_subdir(root)
            resolved_subdir = resolve_survey_cache_subdir(root=root)
            preferred_dir = (cache_base_dir / resolved_subdir).resolve()
            effective_dir = resolve_survey_cache_dir(root=root, account=account_scope)
            source = "subdir" if effective_dir == preferred_dir else "surveys root fallback"

            print("[survey-menu] Survey cache folder setting")
            print(f"  account: {account_scope or 'default'}")
            print(f"  pref: {pref or '(default)'}")
            print(f"  resolved_subdir: {resolved_subdir}")
            print(f"  preferred_cache_dir: {preferred_dir}")
            print(f"  preferred_cache_dir_exists: {preferred_dir.exists() and preferred_dir.is_dir()}")
            print(f"  effective_cache_dir: {effective_dir} ({source})")

            choice = select_from_list(
                "Survey cache folder settings",
                [
                    "Set cache subfolder name",
                    "Clear preference (default: caches)",
                    "Create preferred cache folder now",
                    "↩ Back",
                ],
            )
            if not choice or choice.endswith("Back"):
                return
            if choice.startswith("Set cache"):
                raw = input("Cache subfolder name (e.g. caches or defs): ").strip()
                if not raw:
                    print("[survey-menu] No change.")
                    continue
                try:
                    name = validate_survey_cache_subdir(raw)
                except Exception as exc:
                    print(f"[survey-menu] ERROR: {exc}")
                    continue
                set_workspace_survey_cache_subdir(root, name)
                print(
                    f"[survey-menu] Set workspace survey cache subfolder to `{name}`."
                )
            elif choice.startswith("Clear preference"):
                set_workspace_survey_cache_subdir(root, None)
                print("[survey-menu] Cleared workspace cache subfolder preference.")
            else:
                preferred_dir.mkdir(parents=True, exist_ok=True)
                print(f"[survey-menu] Created: {preferred_dir}")

    def _menu_configure_externally_managed_overrides() -> None:
        from .dimensions.items_structural import external_owner_for
        from .qualtrics_client import load_cached_survey
        from .workspace_prefs import (
            get_workspace_items_allow_externally_managed_qids,
            set_workspace_items_allow_externally_managed_qids,
        )

        def _qid_sort_key(qid: str) -> tuple[int, int | str]:
            m = re.match(r"^QID(\d+)$", str(qid or "").strip(), re.IGNORECASE)
            if m:
                return (0, int(m.group(1)))
            return (1, str(qid or "").strip())

        def _split_override_tokens(raw: str | None) -> list[str]:
            return [tok for tok in re.split(r"[,\s]+", str(raw or "").strip()) if tok]

        def _parse_override_token(token: str) -> tuple[str | None, str]:
            tok = str(token or "").strip()
            if not tok:
                return (None, "")
            if tok.upper().startswith("SV_") and (":" in tok or "/" in tok):
                if ":" in tok:
                    sv, qid = tok.split(":", 1)
                else:
                    sv, qid = tok.split("/", 1)
                sv = str(sv or "").strip()
                qid = str(qid or "").strip()
                if sv and qid:
                    return (sv, qid)
            return (None, tok)

        def _effective_allowed_qids_for_survey(
            *,
            survey_id: str,
            tokens: list[str],
        ) -> tuple[set[str], set[str], set[str], bool]:
            allowed: set[str] = set()
            from_scoped: set[str] = set()
            from_global: set[str] = set()
            allow_all = False
            for token in tokens:
                scoped_survey_id, qid = _parse_override_token(token)
                if not qid:
                    continue
                if qid.lower() in {"all", "*"}:
                    if scoped_survey_id is None or scoped_survey_id == survey_id:
                        allow_all = True
                    continue
                if scoped_survey_id is None:
                    allowed.add(qid)
                    from_global.add(qid)
                    continue
                if scoped_survey_id == survey_id:
                    allowed.add(qid)
                    from_scoped.add(qid)
            return allowed, from_scoped, from_global, allow_all

        def _load_protected_qids_for_survey(
            survey_id: str,
        ) -> list[tuple[str, str, str]]:
            account_scope = _resolve_menu_account()
            surveys_dir = resolve_survey_cache_dir(root=root, account=account_scope)
            env = (
                load_account_env(account_scope, root=root)
                if account_scope
                else None
            )
            survey = load_cached_survey(
                survey_id,
                surveys_dir=surveys_dir,
                env=env,
            )
            protected: list[tuple[str, str, str]] = []
            for raw_qid, q_json in (survey.questions or {}).items():
                qid = str(raw_qid or "").strip()
                if not qid:
                    continue
                tag = str((q_json or {}).get("DataExportTag") or "").strip()
                owner = external_owner_for(qid=qid, data_export_tag=tag)
                if owner:
                    protected.append((qid, tag, owner))
            protected.sort(key=lambda row: _qid_sort_key(row[0]))
            return protected

        def _print_protected_qids_view(
            *,
            survey_id: str,
            protected_rows: list[tuple[str, str, str]],
            allowed_qids: set[str],
            allow_all: bool = False,
        ) -> None:
            print(f"[survey-menu] Survey {survey_id}: externally managed protection")
            if not protected_rows:
                print("  (no externally managed QIDs found in this survey)")
                return
            allowed_count = len(
                [
                    qid
                    for qid, _tag, _owner in protected_rows
                    if allow_all or qid in allowed_qids
                ]
            )
            print(
                f"  protected_qids: {len(protected_rows)} | allowed_overrides: "
                f"{allowed_count}"
            )
            for qid, tag, owner in protected_rows:
                status = "ALLOWED" if (allow_all or qid in allowed_qids) else "PROTECTED"
                print(
                    f"  - {qid} [{status}]"
                    f" tag={tag or '-'} owner={owner}"
                )

        while True:
            pref = get_workspace_items_allow_externally_managed_qids(root)
            env_value = (
                os.environ.get("QSYNC_ITEMS_ALLOW_EXTERNALLY_MANAGED_QIDS") or ""
            ).strip()
            effective = env_value or (pref or "")

            print("[survey-menu] Externally managed items overrides")
            print(
                "  setting key: QSYNC_ITEMS_ALLOW_EXTERNALLY_MANAGED_QIDS "
                "(same token syntax as CLI/env)"
            )
            print(f"  workspace_pref: {pref or '(unset)'}")
            print(f"  shell_env: {env_value or '(unset)'}")
            print(f"  effective_now: {effective or '(none)'}")
            if env_value:
                print("  note: shell env/CLI flag takes precedence over workspace preference.")

            choice = select_from_list(
                "Externally managed overrides",
                [
                    "Show protected QIDs for one survey",
                    "Toggle allowed protected QIDs for one survey (multi-select)",
                    "Set override tokens (raw)",
                    "Clear workspace preference",
                    "↩ Back",
                ],
                instruction=(
                    "Examples: QID15 QID20 SV_xxx:QID30 all. "
                    "Used by items preview/stage/push/sync when env/CLI does not override."
                ),
            )
            if not choice or choice.endswith("Back"):
                return
            if choice.startswith("Show protected"):
                survey_id = _pick_survey_id(
                    message="Pick a survey to inspect externally managed QIDs:"
                )
                if not survey_id:
                    continue
                try:
                    protected_rows = _load_protected_qids_for_survey(survey_id)
                except Exception as exc:
                    print(
                        "[survey-menu] ERROR: could not load survey cache for "
                        f"{survey_id}: {exc}"
                    )
                    print(
                        "  Next: run `qsync items pull --survey-id <ID>` and retry."
                    )
                    continue
                tokens = _split_override_tokens(pref)
                allowed_qids, _from_scoped, _from_global, allow_all = (
                    _effective_allowed_qids_for_survey(
                        survey_id=survey_id,
                        tokens=tokens,
                    )
                )
                _print_protected_qids_view(
                    survey_id=survey_id,
                    protected_rows=protected_rows,
                    allowed_qids=allowed_qids,
                    allow_all=allow_all,
                )
                continue
            if choice.startswith("Toggle allowed protected"):
                survey_id = _pick_survey_id(
                    message="Pick a survey to toggle protected QID overrides:"
                )
                if not survey_id:
                    continue
                try:
                    protected_rows = _load_protected_qids_for_survey(survey_id)
                except Exception as exc:
                    print(
                        "[survey-menu] ERROR: could not load survey cache for "
                        f"{survey_id}: {exc}"
                    )
                    print(
                        "  Next: run `qsync items pull --survey-id <ID>` and retry."
                    )
                    continue
                if not protected_rows:
                    print(
                        "[survey-menu] No externally managed QIDs found for this survey."
                    )
                    continue

                tokens = _split_override_tokens(pref)
                allowed_qids, _from_scoped, from_global, allow_all = (
                    _effective_allowed_qids_for_survey(
                        survey_id=survey_id,
                        tokens=tokens,
                    )
                )

                labels: list[str] = []
                label_to_qid: dict[str, str] = {}
                default_labels: list[str] = []
                for qid, tag, owner in protected_rows:
                    source = []
                    if qid in allowed_qids or allow_all:
                        source.append("allowed")
                    if qid in from_global:
                        source.append("global")
                    if allow_all:
                        source.append("all")
                    source_label = f" ({', '.join(source)})" if source else ""
                    label = (
                        f"{qid} | tag={tag or '-'} | owner={owner}{source_label}"
                    )
                    labels.append(label)
                    label_to_qid[label] = qid
                    if qid in allowed_qids or allow_all:
                        default_labels.append(label)

                selected_labels = multi_select_from_list(
                    "Choose protected QIDs to ALLOW for editing",
                    labels,
                    instruction="Space: toggle, Enter: confirm",
                    default=default_labels,
                )
                if selected_labels is None:
                    print("[survey-menu] Cancelled.")
                    continue

                selected_qids = {
                    label_to_qid[label]
                    for label in selected_labels
                    if label in label_to_qid
                }
                protected_qids = {qid for qid, _tag, _owner in protected_rows}
                deselected_qids = protected_qids - selected_qids
                global_conflicts = sorted(qid for qid in deselected_qids if qid in from_global)
                global_all_conflict = False
                if deselected_qids:
                    for token in tokens:
                        scoped_survey_id, qid = _parse_override_token(token)
                        if scoped_survey_id is None and qid.lower() in {"all", "*"}:
                            global_all_conflict = True
                            break
                if global_conflicts:
                    if not confirm(
                        "Some deselected QIDs are enabled via global tokens "
                        f"({', '.join(global_conflicts)}). Remove those global tokens?",
                        default=False,
                    ):
                        print("[survey-menu] No changes saved.")
                        continue
                if global_all_conflict:
                    if not confirm(
                        "A global `all` override is active. To keep these QIDs protected, "
                        "remove the global `all` token now?",
                        default=False,
                    ):
                        print("[survey-menu] No changes saved.")
                        continue

                parsed_tokens: list[tuple[str, str | None, str]] = []
                for token in tokens:
                    scoped_survey_id, qid = _parse_override_token(token)
                    parsed_tokens.append((token, scoped_survey_id, qid))

                next_tokens: list[str] = []
                for raw_token, scoped_survey_id, qid in parsed_tokens:
                    if not qid:
                        continue
                    if scoped_survey_id == survey_id and (
                        qid in protected_qids or qid.lower() in {"all", "*"}
                    ):
                        continue
                    if scoped_survey_id is None and qid in global_conflicts:
                        continue
                    if scoped_survey_id is None and global_all_conflict and qid.lower() in {
                        "all",
                        "*",
                    }:
                        continue
                    next_tokens.append(raw_token)

                for qid in sorted(selected_qids, key=_qid_sort_key):
                    scoped = f"{survey_id}:{qid}"
                    if scoped not in next_tokens:
                        next_tokens.append(scoped)

                set_workspace_items_allow_externally_managed_qids(
                    root,
                    " ".join(next_tokens) if next_tokens else None,
                )
                print(
                    "[survey-menu] Updated protected-QID overrides for "
                    f"{survey_id}: allowed={len(selected_qids)}, "
                    f"protected={len(deselected_qids)}."
                )
                _print_protected_qids_view(
                    survey_id=survey_id,
                    protected_rows=protected_rows,
                    allowed_qids=selected_qids,
                    allow_all=False,
                )
                continue
            if choice.startswith("Set override"):
                raw = input(
                    "Override tokens (comma/space separated; blank = no change): "
                ).strip()
                if not raw:
                    print("[survey-menu] No change.")
                    continue
                set_workspace_items_allow_externally_managed_qids(root, raw)
                print("[survey-menu] Updated workspace externally managed override tokens.")
                continue
            set_workspace_items_allow_externally_managed_qids(root, None)
            print("[survey-menu] Cleared workspace externally managed override tokens.")

    def _menu_prepare() -> None:
        _run_action(
            handle_prepare,
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
                account=_resolve_menu_account(),
            ),
        )

    def _menu_embedded_field(action: str) -> None:
        if not _require_default_account(action=action):
            return
        survey_id = _pick_survey_id(message="Pick a survey (must be cached locally):")
        if not survey_id:
            return
        flow_id = input("FlowID (optional; blank = auto): ").strip() or None
        dry_run = select_from_list("Dry run?", ["No", "Yes"]) == "Yes"
        if action == "add-embedded-field":
            field = input("Embedded field name: ").strip()
            if not field:
                return
            value = input("Value (optional; blank = empty): ")
            value = value if value.strip() else None
            _run_action(
                handle_add_embedded_field,
                argparse.Namespace(
                    survey_id=survey_id,
                    field=field,
                    value=value,
                    flow_id=flow_id,
                    dry_run=bool(dry_run),
                ),
            )
        elif action == "remove-embedded-field":
            field = input("Embedded field name to remove: ").strip()
            if not field:
                return
            _run_action(
                handle_remove_embedded_field,
                argparse.Namespace(
                    survey_id=survey_id,
                    field=field,
                    flow_id=flow_id,
                    dry_run=bool(dry_run),
                ),
            )
        elif action == "rename-embedded-field":
            from_field = input("Rename from (old field name): ").strip()
            if not from_field:
                return
            to_field = input("Rename to (new field name): ").strip()
            if not to_field:
                return
            all_occurrences = (
                select_from_list("Rename in all occurrences?", ["No", "Yes"]) == "Yes"
            )
            _run_action(
                handle_rename_embedded_field,
                argparse.Namespace(
                    survey_id=survey_id,
                    from_field=from_field,
                    to_field=to_field,
                    flow_id=flow_id,
                    all_occurrences=bool(all_occurrences),
                    dry_run=bool(dry_run),
                ),
            )

    def _menu_cleanup_embedded_data() -> None:
        if not _require_default_account(action="cleanup-embedded-data"):
            return
        survey_id = _pick_survey_id(message="Pick a survey to cleanup embedded data:")
        if not survey_id:
            return
        sel = select_from_list(
            "Cleanup scope:",
            [
                "Placeholder duplicates only (recommended)",
                "All duplicates (dangerous)",
                "↩ Back",
            ],
        )
        if not sel or sel.endswith("Back"):
            return
        all_duplicates = sel.startswith("All duplicates")

        apply_changes = (
            select_from_list("Apply cleanup in Qualtrics?", ["No (dry run)", "Yes"])
            == "Yes"
        )
        publish = False
        description = ""
        if apply_changes:
            publish = select_from_list("Publish after cleanup?", ["No", "Yes"]) == "Yes"
            if publish:
                description = (
                    input(
                        f"Publish description (max {SURVEY_VERSION_DESCRIPTION_MAX_CHARS} chars) "
                        "(blank = default): "
                    ).strip()
                    or "qsync cleanup: embedded data placeholders"
                )
        _run_action(
            handle_cleanup_embedded_data,
            argparse.Namespace(
                survey_id=survey_id,
                all_duplicates=bool(all_duplicates),
                apply=bool(apply_changes),
                dry_run=False,
                yes=False,
                publish=bool(publish),
                description=description,
            ),
        )

    def _menu_prolific_auth() -> None:
        if not _require_default_account(action="prolific-auth"):
            return
        survey_id = _pick_survey_id(message="Pick a survey to set Prolific snippet:")
        if not survey_id:
            return
        _run_action(
            handle_prolific_auth,
            argparse.Namespace(
                survey_id=survey_id,
                snippet=None,
                file=None,
                mode=None,
                yes=False,
                dry_run=False,
                print_current=False,
                no_validate=False,
                no_publish=False,
                no_activate=False,
            ),
        )

    def _menu_prolific_wiring() -> None:
        from .cli_prolific import (
            handle_propose_matches,
            handle_pull_studies,
            handle_wire_apply,
            handle_wire_preview,
            handle_wire_rollback,
        )

        account_scope = _resolve_menu_account()
        scoped_surveys_dir = resolve_scoped_dir("surveys", root=root, account=account_scope)
        studies_path = (scoped_surveys_dir / "prolific" / "studies.csv").resolve()
        matches_path = (scoped_surveys_dir / "prolific" / "matches.csv").resolve()

        def _prompt_prefix_tokens(default: int = 5) -> int:
            raw = input(
                f"Minimum prefix token count for unique matching [{default}]: "
            ).strip()
            if not raw:
                return default
            try:
                value = int(raw)
            except Exception:
                print(f"[survey-menu] Invalid prefix token count '{raw}', using {default}.")
                return default
            return max(value, 1)

        def _prompt_auth_file() -> str | None:
            raw = input(
                "Auth snippet file path (blank = configured default from env/token): "
            ).strip()
            return raw or None

        while True:
            choice = select_from_list(
                "Prolific Wiring",
                [
                    "Pull Prolific studies cache",
                    "Propose matches (refresh Qualtrics inventory)",
                    "Show review guidance",
                    "Preview wiring plan (APPROVED rows)",
                    "Apply wiring (APPROVED rows)",
                    "Rollback a previous apply",
                    "↩ Back",
                ],
            )
            if not choice or choice.endswith("Back"):
                return

            if choice.startswith("Pull Prolific"):
                _run_action(
                    handle_pull_studies,
                    argparse.Namespace(
                        state=None,
                        studies=None,
                        prolific_token=None,
                        json=False,
                        account=account_scope,
                    ),
                )
                continue

            if choice.startswith("Propose matches"):
                prefix_tokens = _prompt_prefix_tokens()
                _run_action(
                    handle_propose_matches,
                    argparse.Namespace(
                        studies=None,
                        matches=None,
                        prefix_tokens=prefix_tokens,
                        pull_studies=True,
                        qualtrics_inventory_refresh=True,
                        prolific_token=None,
                        json=False,
                        account=account_scope,
                    ),
                )
                print(f"[survey-menu] Review matches in: {matches_path}")
                print(
                    "[survey-menu] Mark rows as APPROVED / SKIP / REVIEW_REQUIRED, then run preview/apply."
                )
                print(
                    "[survey-menu] Quick skim column: match_formula (between Prolific internal and Qualtrics survey names)."
                )
                continue

            if choice.startswith("Show review guidance"):
                print(f"[survey-menu] Studies cache: {studies_path}")
                print(f"[survey-menu] Match file:   {matches_path}")
                print("[survey-menu] Review flow:")
                print("  1) Confirm proposed pairing using match_formula + names.")
                print("  2) Set state=APPROVED for rows to wire, state=SKIP for rows to ignore.")
                print("  3) Keep uncertain rows as REVIEW_REQUIRED.")
                print("  4) Run preview, then apply on APPROVED rows only.")
                print("  5) Apply step publishes and activates Qualtrics surveys by default.")
                continue

            if choice.startswith("Preview wiring"):
                auth_file = _prompt_auth_file()
                _run_action(
                    handle_wire_preview,
                    argparse.Namespace(
                        matches=None,
                        only_state="APPROVED",
                        auth_snippet=None,
                        auth_snippet_file=auth_file,
                        auth_token=None,
                        prolific_token=None,
                        json=False,
                        account=account_scope,
                    ),
                )
                continue

            if choice.startswith("Apply wiring"):
                auth_file = _prompt_auth_file()
                print(
                    "[survey-menu] Finalization defaults: publish=Yes, activate=Yes "
                    "(required so Prolific authenticity checks are live)."
                )
                continue_on_error = (
                    select_from_list("Continue when one row fails?", ["Yes", "No"]) == "Yes"
                )
                _run_action(
                    handle_wire_apply,
                    argparse.Namespace(
                        matches=None,
                        only_state="APPROVED",
                        auth_snippet=None,
                        auth_snippet_file=auth_file,
                        auth_token=None,
                        prolific_token=None,
                        yes=False,
                        publish=True,
                        activate=True,
                        publish_description="Prolific wiring update",
                        continue_on_error=continue_on_error,
                        json=False,
                        account=account_scope,
                    ),
                )
                continue

            if choice.startswith("Rollback"):
                op_id = input("Operation ID (or full journal path): ").strip()
                if not op_id:
                    continue
                publish = (
                    select_from_list("Publish surveys after rollback writes?", ["No", "Yes"])
                    == "Yes"
                )
                activate = (
                    select_from_list("Activate surveys after rollback writes?", ["No", "Yes"])
                    == "Yes"
                )
                _run_action(
                    handle_wire_rollback,
                    argparse.Namespace(
                        op_id=op_id,
                        prolific_token=None,
                        yes=False,
                        publish=publish,
                        activate=activate,
                        publish_description="Prolific wiring rollback",
                        json=False,
                        account=account_scope,
                    ),
                )

    def _menu_items_structural_edits(*, preselected_survey_id: str | None = None) -> None:
        from .pending_stage import (
            ItemsPendingPayload,
            PendingStagedChanges,
            load_pending,
            save_pending,
        )
        from .qualtrics_client import load_cached_survey, refresh_survey_cache
        from .workbook_resolver import WorkbookResolver
        from .dimensions.items_structural import (
            interactive_choice_wizard,
            summarize_structural_ops,
            push_structural_ops,
            ItemsStructuralError,
            _wipe_workbook_qid_cells,  # type: ignore
        )
        from .terminal_colors import colorize_unified_diff_lines
        import difflib

        account_scope = _resolve_menu_account()
        selected_env = None
        if account_scope:
            try:
                selected_env = load_account_env(account_scope, root=root)
            except Exception as exc:
                print(
                    f"[survey-menu] ERROR: could not load account '{account_scope}': {exc}"
                )
                return
        else:
            selected_env = None

        # Keep this menu anchored to one survey selection until user explicitly exits.
        # On cache errors, let the operator re-try instead of silently returning
        # to the parent menu.
        class _AbortStructuralSelection(Exception):
            """Used as a local control-flow escape from survey preflight selection."""

        def _select_survey_and_cache() -> tuple[str, Path]:
            pending_survey_id = (preselected_survey_id or "").strip() or None
            while True:
                if pending_survey_id:
                    survey_id = pending_survey_id
                    pending_survey_id = None
                    print(f"[survey-menu] Using --survey-id {survey_id}")
                else:
                    survey_id = _pick_survey_id(message="Pick a survey for structural edits:")
                if not survey_id:
                    raise _AbortStructuralSelection

                surveys_dir = resolve_survey_cache_dir(
                    root=root, account=account_scope
                )
                try:
                    refresh_survey_cache(
                        survey_id,
                        env=selected_env,
                        surveys_dir=surveys_dir,
                    )
                    survey = load_cached_survey(
                        survey_id,
                        env=selected_env,
                        surveys_dir=surveys_dir,
                    )
                    if len(survey.questions) == 0:
                        print(
                            f"[survey-menu] ERROR: cached survey {survey_id} has no questions."
                        )
                        print("  Next: run `qsync survey pull --survey-id <ID>` for this account and retry.")
                        if select_from_list(
                            "Retry with another survey?",
                            ["Yes", "No (back to survey menu)"],
                        ) != "Yes":
                            raise _AbortStructuralSelection
                        continue
                    return survey_id, surveys_dir
                except Exception as exc:
                    print(f"[survey-menu] ERROR: could not load cache for {survey_id}: {exc}")
                    print("  Next: run `qsync survey pull --survey-id <ID>` and retry.")
                    if select_from_list(
                        "Retry with another survey?",
                        ["Yes", "No (back to survey menu)"],
                    ) != "Yes":
                        raise _AbortStructuralSelection

        try:
            survey_id, surveys_dir = _select_survey_and_cache()
        except _AbortStructuralSelection:
            return

        # Keep current process deterministic across qsync installs with slightly
        # different signatures for this helper.
        def _call_interactive_choice_wizard(
            *, survey_id: str, qid: str | None, preferred_target: str | None
        ) -> dict[str, Any]:
            return interactive_choice_wizard(
                survey_id=survey_id,
                qid=qid,
                preferred_target=preferred_target,
                allow_delete=False,
                experimental_unsupported=False,
                env=selected_env,
                surveys_dir=surveys_dir,
            )

        resolver = WorkbookResolver()
        xlsx_path = resolver.resolve(survey_id)

        def _load_or_init_pending() -> PendingStagedChanges:
            record = load_pending(survey_id, "items")
            if record and isinstance(record.payload, ItemsPendingPayload):
                return record
            payload = ItemsPendingPayload(
                qids=[],
                workbook=str(xlsx_path) if xlsx_path.exists() else None,
                structural_ops=[],
                structural_summary={},
                push_journal={},
                changes=[],
                embedded_fields=[],
            )
            return PendingStagedChanges(
                survey_id=survey_id, dimension="items", payload=payload
            )

        def _stage_op(op: dict) -> None:
            record = _load_or_init_pending()
            payload = record.payload
            assert isinstance(payload, ItemsPendingPayload)

            ops = list(payload.structural_ops or [])
            ops.append(dict(op))
            payload.structural_ops = ops
            payload.structural_summary = summarize_structural_ops(ops)
            qid = str(op.get("qid") or "").strip()
            if qid and qid not in (payload.qids or []):
                payload.qids = list(dict.fromkeys([*(payload.qids or []), qid]))
            if xlsx_path.exists():
                payload.workbook = str(xlsx_path)
            save_pending(record)

        def _preview_ops(ops: list[dict]) -> None:
            if not ops:
                print("[survey-menu] No staged structural ops.")
                return
            print("\n[survey-menu] Preview: staged structural ops (pending vs cache baseline)")
            for op in ops:
                qid = str(op.get("qid") or "").strip()
                op_type = str(op.get("op") or "").strip()
                surface = str(op.get("surface") or op.get("target") or "").strip()
                cid = str(op.get("choice_id") or "").strip()
                aid = str(op.get("answer_id") or "").strip()
                label = f"{op_type} qid={qid}"
                if surface:
                    label += f" surface={surface}"
                if cid:
                    label += f" id={cid}"
                if aid:
                    label += f"/{aid}"
                print("-" * 80)
                print(label)
                before = str(op.get("prev_html") or "")
                after = str(op.get("html") or "")
                if before or after:
                    diff = list(
                        difflib.unified_diff(
                            before.splitlines(),
                            after.splitlines(),
                            fromfile="cache",
                            tofile="staged",
                            lineterm="",
                        )
                    )
                    for line in colorize_unified_diff_lines(diff[:160]):
                        print("  " + line)
                    if len(diff) > 160:
                        print("  ... (diff truncated)")

        def _clear_structural_pending() -> None:
            record = load_pending(survey_id, "items")
            if not record or not isinstance(record.payload, ItemsPendingPayload):
                return
            record.payload.structural_ops = []
            record.payload.structural_summary = {}
            record.payload.push_journal = {}
            save_pending(record)

        def _offer_workbook_patch(ops: list[dict]) -> None:
            if not xlsx_path.exists():
                print("[survey-menu] No workbook found; skipping workbook patch.")
                return
            qids = sorted(
                {str(op.get("qid") or "").strip() for op in ops if op.get("qid")}
            )
            if not qids:
                return
            print("\n[survey-menu] Workbook patch (dry run):")
            for q in qids:
                notes = _wipe_workbook_qid_cells(
                    survey_id=survey_id, qid=q, dry_run=True
                )
                for n in notes[:6]:
                    print("  -", n)
            if select_from_list("Apply workbook patch now?", ["No", "Yes"]) != "Yes":
                return
            for q in qids:
                _wipe_workbook_qid_cells(survey_id=survey_id, qid=q, dry_run=False)
            print("[survey-menu] Workbook patched for affected QIDs.")

        def _normalize_op_target(op: dict[str, Any]) -> str | None:
            raw = str(op.get("target") or op.get("surface") or "").strip().lower()
            key = raw.replace(" ", "_")
            if key in {"question", "question_text", "question-text"}:
                return "question-text"
            if key in {"options", "option"}:
                return "options"
            if key in {"subitems", "subitem", "answers", "answer"}:
                return "subitems"
            if key in {"sbs_columns", "sbs_column"}:
                return "sbs_columns"
            if key in {"sbs_column_answers", "sbs_column_answer"}:
                return "sbs_column_answers"
            return None

        next_qid: str | None = None
        next_target: str | None = None
        while True:
            try:
                op = _call_interactive_choice_wizard(
                    survey_id=survey_id,
                    qid=next_qid,
                    preferred_target=next_target,
                )
            except ItemsStructuralError as exc:
                if "Cancelled." in str(exc):
                    print(str(exc))
                    if (
                        select_from_list(
                            "No question selected. Choose another question for this survey?",
                            ["Yes", "No (back to survey menu)"],
                        )
                        != "Yes"
                    ):
                        return
                    continue
                print(
                    "[survey-menu] ERROR while selecting QID: "
                    f"{exc!s}\n  Next: verify the survey was pulled for this account, or choose another survey."
                )
                if (
                    select_from_list(
                        "Retry same survey selection?",
                        ["Yes", "No (back to survey menu)"],
                    )
                    != "Yes"
                ):
                    return
                continue
            except Exception as exc:
                print(f"[survey-menu] ERROR: {exc}")
                if (
                    select_from_list(
                        "Retry same survey selection?",
                        ["Yes", "No (back to survey menu)"],
                    )
                    != "Yes"
                ):
                    return
                continue

            if not op:
                print(
                    "[survey-menu] ERROR: structural editor returned no staged op."
                )
                if (
                    select_from_list(
                        "Retry same survey selection?",
                        ["Yes", "No (back to survey menu)"],
                    )
                    != "Yes"
                ):
                    return
                continue
            _stage_op(op)
            current_qid = str(op.get("qid") or "").strip() or None
            current_target = _normalize_op_target(op)
            print(
                f"[survey-menu] Staged: {op.get('op')} qid={op.get('qid')} id={op.get('choice_id') or op.get('answer_id') or ''}"
            )
            again = select_from_list(
                "What next?",
                [
                    "Continue in same target (same question)",
                    "Continue in same question (choose target)",
                    "Choose another question",
                    "Review staged edits",
                ],
            )
            if not again or again.startswith("Review"):
                break
            if again.startswith("Continue in same target"):
                next_qid = current_qid
                next_target = current_target
                continue
            if again.startswith("Continue in same question"):
                next_qid = current_qid
                next_target = None
                continue
            next_qid = None
            next_target = None

        record = load_pending(survey_id, "items")
        ops: list[dict] = []
        if record and isinstance(record.payload, ItemsPendingPayload):
            ops = list(record.payload.structural_ops or [])
        _preview_ops(ops)

        decision = select_from_list(
            "Structural edits staged. Next?",
            [
                "Push now",
                "Revert edits (clear staged + refresh cache)",
                "Abort (leave staged pending)",
            ],
        )
        if not decision or decision.startswith("Abort"):
            return
        if decision.startswith("Revert"):
            _clear_structural_pending()
            try:
                refresh_survey_cache(survey_id)
            except Exception:
                pass
            print("[survey-menu] Cleared staged structural edits.")
            return

        record = _load_or_init_pending()
        payload = record.payload
        assert isinstance(payload, ItemsPendingPayload)
        ops = list(payload.structural_ops or [])
        if not ops:
            print("[survey-menu] No structural ops staged.")
            return

        publish = select_from_list("Publish after push?", ["Yes", "No"]) == "Yes"

        def _save_journal(journal: dict) -> None:
            record.payload.push_journal = dict(journal)
            save_pending(record)

        try:
            survey_cache = load_cached_survey(
                survey_id,
                env=selected_env,
                surveys_dir=surveys_dir,
            )
            push_structural_ops(
                survey_id=survey_id,
                payload=survey_cache.payload,
                structural_ops=ops,
                push_journal=dict(payload.push_journal or {}),
                interactive=True,
                allow_delete=False,
                force_live=False,
                force_preview=False,
                publish=bool(publish),
                dry_run=False,
                refresh_cache=True,
                save_journal_cb=_save_journal,
            )
        except Exception as exc:
            print(f"[survey-menu] ERROR pushing structural ops: {exc}")
            return

        _clear_structural_pending()
        _offer_workbook_patch(ops)

    def _truncate_menu_text(value: str, *, limit: int = 90) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip())
        if not text:
            return "(no text)"
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)].rstrip() + "..."

    def _get_client_for_account(*, account: str | None) -> tuple[str, dict]:
        return _get_client_for_scope(account)

    def _get_surveys_for_account(*, account: str | None) -> list[dict[str, Any]]:
        base, headers = _get_client_for_account(account=account)
        scope_key = str(account or "").strip().lower() or "__ambient__"
        cache_key = f"{scope_key}::{base}"
        cached = survey_cache.get(cache_key)
        if cached is not None:
            return cached
        surveys = list_surveys(base, headers)
        surveys.sort(key=lambda x: x.get("creationDate", ""), reverse=True)
        survey_cache[cache_key] = surveys
        return surveys

    def _question_bank_index_path(*, account: str | None) -> Path:
        account_token = (account or "default").strip() or "default"
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", account_token)
        return (root / ".qsync" / f"question_bank_index__{safe}.json").resolve()

    def _load_question_bank_index(
        *, account: str | None, max_age_seconds: int = 6 * 60 * 60
    ) -> dict[str, Any] | None:
        path = _question_bank_index_path(account=account)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        generated = float(payload.get("generated_at_epoch") or 0.0)
        if generated <= 0:
            return None
        age = time.time() - generated
        if age > max_age_seconds:
            return None
        surveys = payload.get("surveys")
        if not isinstance(surveys, list):
            return None
        return payload

    def _build_question_bank_index(
        *,
        account: str | None,
        surveys: list[dict[str, Any]],
    ) -> dict[str, Any]:
        scoped_surveys_dir = resolve_scoped_dir("surveys", root=root, account=account)
        entries: list[dict[str, Any]] = []
        for survey in surveys:
            survey_id = str(survey.get("id") or "").strip()
            if not survey_id:
                continue
            cached = find_cached_survey_file(survey_id, base_dir=scoped_surveys_dir)
            if not cached:
                continue
            try:
                payload = json.loads(cached.read_text(encoding="utf-8"))
            except Exception:
                continue
            definition = payload.get("result") if isinstance(payload, dict) else None
            if not isinstance(definition, dict):
                continue
            labels = _question_labels_from_definition(definition)
            if not labels:
                continue
            entries.append(
                {
                    "id": survey_id,
                    "name": str(survey.get("name") or survey_id),
                    "creationDate": str(survey.get("creationDate") or ""),
                    "question_labels": labels,
                }
            )

        index_payload = {
            "version": 1,
            "account": (account or "default"),
            "generated_at_epoch": time.time(),
            "survey_count": len(entries),
            "surveys": entries,
        }
        path = _question_bank_index_path(account=account)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(index_payload, indent=2), encoding="utf-8")
        return index_payload

    def _question_labels_from_index_payload(
        index_payload: Mapping[str, Any], *, survey_id: str
    ) -> list[str]:
        def _normalize_label(raw_label: object) -> str | None:
            label = str(raw_label or "").strip()
            if not label:
                return None
            if " - " in label:
                head = _label_head(label)
                if head and head.upper().startswith("QID"):
                    return label
                return None
            token = label.split()[0].strip()
            if token and token.upper().startswith("QID"):
                tail = label[len(token) :].strip(" -") or "(no text)"
                return f"{token} - {_truncate_menu_text(tail)}"
            return None

        surveys = index_payload.get("surveys")
        if not isinstance(surveys, list):
            return []
        for entry in surveys:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("id") or "").strip() != survey_id:
                continue
            labels = entry.get("question_labels")
            if isinstance(labels, list):
                normalized: list[str] = []
                for item in labels:
                    cooked = _normalize_label(item)
                    if cooked:
                        normalized.append(cooked)
                return normalized
        return []

    def _fetch_definition_for_menu(
        survey_id: str, *, account: str | None = None
    ) -> dict[str, Any] | None:
        try:
            base, headers = _get_client_for_account(account=account)
            definition = fetch_survey_definition(base, headers, survey_id)
        except Exception as exc:
            print(
                f"[survey-menu] ERROR: could not fetch survey definition for {survey_id}: {exc}"
            )
            return None
        if not isinstance(definition, dict):
            print("[survey-menu] ERROR: unexpected survey-definition payload shape.")
            return None
        return definition

    def _ordered_qids_from_definition(definition: Mapping[str, Any]) -> list[str]:
        questions = definition.get("Questions")
        if not isinstance(questions, dict):
            return []
        blocks = definition.get("Blocks")
        if not isinstance(blocks, dict):
            return sorted(str(k).strip() for k in questions.keys() if str(k).strip())

        ordered: list[str] = []
        seen: set[str] = set()
        for block_id in _flow_ordered_block_ids(definition):
            block = blocks.get(block_id)
            if not isinstance(block, dict):
                continue
            if _is_trash_block(block):
                continue
            elements = (
                block.get("BlockElements")
                if isinstance(block.get("BlockElements"), list)
                else block.get("Elements")
            )
            if not isinstance(elements, list):
                continue
            for elem in elements:
                if not isinstance(elem, dict):
                    continue
                qid = str(elem.get("QuestionID") or "").strip()
                if not qid or qid in seen:
                    continue
                if qid not in questions:
                    continue
                seen.add(qid)
                ordered.append(qid)
        leftovers = sorted(
            [
                str(qid).strip()
                for qid in questions.keys()
                if str(qid).strip() and str(qid).strip() not in seen
            ]
        )
        ordered.extend(leftovers)
        return ordered

    def _question_labels_from_definition(
        definition: Mapping[str, Any],
        *,
        exclude: set[str] | None = None,
    ) -> list[str]:
        def _question_label_text(payload: Mapping[str, Any]) -> str:
            text = (
                str(payload.get("QuestionText") or "").strip()
                or str(payload.get("QuestionText_Unsafe") or "").strip()
                or str(payload.get("QuestionDescription") or "").strip()
                or str(payload.get("DataExportTag") or "").strip()
            )
            if text:
                return text
            qtype = str(payload.get("QuestionType") or "").strip()
            selector = str(payload.get("Selector") or "").strip()
            if qtype and selector:
                return f"{qtype}/{selector}"
            if qtype:
                return qtype
            return "(no text)"

        questions = definition.get("Questions")
        if not isinstance(questions, dict):
            return []

        exclude_set = {str(item).strip() for item in (exclude or set()) if str(item).strip()}

        qids = _ordered_qids_from_definition(definition)
        labels: list[str] = []
        for qid in qids:
            if qid in exclude_set:
                continue
            payload = questions.get(qid)
            text = _question_label_text(payload) if isinstance(payload, dict) else "(no text)"
            labels.append(f"{qid} - {_truncate_menu_text(text)}")
        return labels

    def _block_labels_from_definition(definition: Mapping[str, Any]) -> list[str]:
        blocks = definition.get("Blocks")
        if not isinstance(blocks, dict):
            return []

        ordered: list[str] = []
        for block_id in _flow_ordered_block_ids(definition):
            if block_id in blocks:
                ordered.append(block_id)
        for block_id in sorted(str(k).strip() for k in blocks.keys() if str(k).strip()):
            if block_id not in ordered:
                ordered.append(block_id)

        labels: list[str] = []
        for block_id in ordered:
            block = blocks.get(block_id)
            if not isinstance(block, dict):
                continue
            if _is_trash_block(block):
                continue
            block_name = (
                str(block.get("Description") or "").strip()
                or str(block.get("BlockDescription") or "").strip()
                or str(block.get("Type") or "").strip()
                or "Block"
            )
            labels.append(f"{block_id} - {_truncate_menu_text(block_name, limit=80)}")
        return labels

    def _label_head(value: str) -> str:
        return str(value or "").split(" - ", 1)[0].strip()

    def _pick_question_id_from_definition(
        definition: Mapping[str, Any],
        *,
        message: str,
        exclude: set[str] | None = None,
    ) -> str | None:
        labels = _question_labels_from_definition(definition, exclude=exclude)
        if not labels:
            print("[survey-menu] No selectable questions found.")
            return None

        if len(labels) > 60:
            from .interactive_menu import autocomplete_from_list

            picked = autocomplete_from_list(
                message=message,
                choices=labels,
                instruction="Type to filter, Enter to pick.",
            )
            if not picked:
                return None
            token = _label_head(picked)
            return token or None

        picked = select_from_list(
            message,
            [*labels, "↩ Back"],
            instruction="Choose a question anchor.",
        )
        if not picked or picked.endswith("Back"):
            return None
        token = _label_head(picked)
        return token or None

    def _pick_question_ids_from_labels(
        *,
        labels: list[str],
        message: str,
        preserve_selection_order: bool = False,
    ) -> list[str] | None:
        from .interactive_menu import autocomplete_from_list, multi_select_from_list

        if not labels:
            print("[survey-menu] No selectable questions found.")
            return None
        if len(labels) == 1:
            token = _label_head(labels[0])
            return [token] if token else []

        narrowed_labels = list(labels)
        if len(labels) > 60:
            sentinel = "→ Continue to multi-select (show full list)"
            narrowed = autocomplete_from_list(
                message=message,
                choices=[sentinel, *labels],
                instruction="Type to filter; press Enter for full list.",
            )
            if narrowed is None:
                return None
            if narrowed != sentinel:
                direct = _label_head(narrowed)
                if direct:
                    return [direct]
                needle = str(narrowed).strip().lower()
                narrowed_labels = [label for label in labels if needle in label.lower()]
                if not narrowed_labels:
                    print("[survey-menu] No questions matched that filter.")
                    return []
                if len(narrowed_labels) == 1:
                    token = _label_head(narrowed_labels[0])
                    return [token] if token else []

        selected = multi_select_from_list(
            message=message,
            choices=narrowed_labels,
            instruction="Space: toggle, Enter: confirm",
        )
        if selected is None:
            return None
        values = [_label_head(item) for item in selected]
        out: list[str] = []
        seen: set[str] = set()
        for qid in values:
            if not qid or qid in seen:
                continue
            seen.add(qid)
            out.append(qid)
        if preserve_selection_order and len(out) > 1:
            label_by_qid: dict[str, str] = {}
            for label in labels:
                qid = _label_head(label)
                if qid:
                    label_by_qid[qid] = label
            ordered: list[str] = []
            selected_qids = list(out)
            while len(ordered) < len(selected_qids):
                display_to_qid: dict[str, str] = {}
                decorated_choices: list[str] = []
                for qid in selected_qids:
                    source_label = label_by_qid.get(qid, qid)
                    if qid in ordered:
                        marker = f"[{ordered.index(qid) + 1}]"
                    else:
                        marker = "[ ]"
                    decorated = f"{marker} {source_label}"
                    decorated_choices.append(decorated)
                    display_to_qid[decorated] = qid
                picked = select_from_list(
                    f"Pick clone order position {len(ordered) + 1}:",
                    [*decorated_choices, "↩ Back"],
                    instruction="Selections show persistent order badges.",
                )
                if not picked or picked.endswith("Back"):
                    return None
                chosen_qid = display_to_qid.get(picked)
                if not chosen_qid:
                    fallback_label = str(picked).split("] ", 1)[-1]
                    token = _label_head(fallback_label)
                    if token in selected_qids:
                        chosen_qid = token
                if chosen_qid and chosen_qid not in ordered:
                    ordered.append(chosen_qid)
            return ordered
        return out

    def _pick_question_ids_from_definition(
        definition: Mapping[str, Any],
        *,
        message: str,
        preserve_selection_order: bool = False,
    ) -> list[str] | None:
        labels = _question_labels_from_definition(definition)
        return _pick_question_ids_from_labels(
            labels=labels,
            message=message,
            preserve_selection_order=preserve_selection_order,
        )

    def _pick_block_id_from_definition(
        definition: Mapping[str, Any],
        *,
        message: str,
    ) -> str | None:
        labels = _block_labels_from_definition(definition)
        if not labels:
            print("[survey-menu] No eligible blocks found.")
            return None
        if len(labels) > 60:
            from .interactive_menu import autocomplete_from_list

            picked = autocomplete_from_list(
                message=message,
                choices=labels,
                instruction="Type to filter, Enter to pick.",
            )
            if not picked:
                return None
            token = _label_head(picked)
            return token or None

        picked = select_from_list(
            message,
            [*labels, "↩ Back"],
            instruction="Choose a target block.",
        )
        if not picked or picked.endswith("Back"):
            return None
        token = _label_head(picked)
        return token or None

    def _block_elements_for_menu(
        definition: Mapping[str, Any],
        *,
        block_id: str,
    ) -> list[dict[str, Any]]:
        blocks = definition.get("Blocks")
        if not isinstance(blocks, dict):
            return []
        block = blocks.get(block_id)
        if not isinstance(block, dict):
            return []
        elements = (
            block.get("BlockElements")
            if isinstance(block.get("BlockElements"), list)
            else block.get("Elements")
        )
        if not isinstance(elements, list):
            return []
        return [elem for elem in elements if isinstance(elem, dict)]

    def _block_name_for_menu(
        definition: Mapping[str, Any],
        *,
        block_id: str,
    ) -> str:
        blocks = definition.get("Blocks")
        if not isinstance(blocks, dict):
            return block_id
        block = blocks.get(block_id)
        if not isinstance(block, dict):
            return block_id
        block_name = (
            str(block.get("Description") or "").strip()
            or str(block.get("BlockDescription") or "").strip()
            or "Block"
        )
        return f"{block_id} - {_truncate_menu_text(block_name, limit=80)}"

    def _block_element_token(elem: Mapping[str, Any]) -> str:
        elem_type = str(elem.get("Type") or "").strip()
        if elem_type == "Question":
            qid = str(elem.get("QuestionID") or "").strip()
            return qid or "Question"
        if elem_type == "Page Break":
            return "PB"
        return elem_type or "Element"

    def _block_element_line(
        definition: Mapping[str, Any],
        elem: Mapping[str, Any],
        *,
        marked_qids: set[str] | None = None,
    ) -> str:
        elem_type = str(elem.get("Type") or "").strip()
        if elem_type == "Question":
            qid = str(elem.get("QuestionID") or "").strip() or "Question"
            questions = definition.get("Questions")
            question = questions.get(qid) if isinstance(questions, dict) else None
            text = ""
            tag = ""
            if isinstance(question, dict):
                text = (
                    str(question.get("QuestionText") or "").strip()
                    or str(question.get("QuestionDescription") or "").strip()
                )
                tag = str(question.get("DataExportTag") or "").strip()
            marker = " [selected]" if marked_qids and qid in marked_qids else ""
            if tag:
                return f"{qid}{marker} (tag={tag}) - {_truncate_menu_text(text, limit=70)}"
            return f"{qid}{marker} - {_truncate_menu_text(text, limit=70)}"
        if elem_type == "Page Break":
            return "--- PB ---"
        return f"[{elem_type or 'Element'}]"

    def _slot_context(elements: list[dict[str, Any]], *, slot_index: int) -> str:
        if not elements:
            return "empty block"
        if slot_index <= 0:
            return f"start (before {_block_element_token(elements[0])})"
        if slot_index >= len(elements):
            return f"end (after {_block_element_token(elements[-1])})"
        prev = _block_element_token(elements[slot_index - 1])
        nxt = _block_element_token(elements[slot_index])
        return f"between {prev} and {nxt}"

    def _pick_insert_slot_in_block(
        definition: Mapping[str, Any],
        *,
        block_id: str,
        message: str,
        marked_qids: set[str] | None = None,
    ) -> int | None:
        elements = _block_elements_for_menu(definition, block_id=block_id)
        block_label = _block_name_for_menu(definition, block_id=block_id)

        menu_items: list[MenuItem] = [
            MenuItem(label=f"{block_label} selected.", enabled=False),
            MenuItem.separator(),
        ]
        for slot_idx in range(len(elements) + 1):
            context = _slot_context(elements, slot_index=slot_idx)
            menu_items.append(
                MenuItem(
                    label=f"[insert here] {context}",
                    value=f"slot:{slot_idx}",
                )
            )
            if slot_idx < len(elements):
                line = _block_element_line(
                    definition,
                    elements[slot_idx],
                    marked_qids=marked_qids,
                )
                menu_items.append(MenuItem(label=f"  {line}", enabled=False))

        menu_items.extend([MenuItem.separator(), MenuItem(label="↩ Back", value="__back__")])
        picked = select_from_list(
            message,
            menu_items,
            instruction="Pick an insertion boundary. Non-selectable rows show current block elements.",
        )
        if not picked or picked == "__back__":
            return None
        token = str(picked)
        if not token.startswith("slot:"):
            return None
        try:
            return int(token.split(":", 1)[1])
        except (TypeError, ValueError):
            return None

    def _prompt_block_slot_placement(
        definition: Mapping[str, Any],
        *,
        title: str,
        marked_qids: set[str] | None = None,
    ) -> tuple[str, int] | None:
        block_id = _pick_block_id_from_definition(
            definition,
            message="Choose target block:",
        )
        if not block_id:
            return None
        slot_index = _pick_insert_slot_in_block(
            definition,
            block_id=block_id,
            message=title,
            marked_qids=marked_qids,
        )
        if slot_index is None:
            return None
        return (block_id, slot_index)

    def _page_break_choices_for_block(
        definition: Mapping[str, Any],
        *,
        block_id: str,
    ) -> list[tuple[str, int]]:
        elements = _block_elements_for_menu(definition, block_id=block_id)
        choices: list[tuple[str, int]] = []
        for idx, elem in enumerate(elements):
            if str(elem.get("Type") or "").strip() != "Page Break":
                continue
            prev_token = _block_element_token(elements[idx - 1]) if idx > 0 else "start"
            next_token = (
                _block_element_token(elements[idx + 1])
                if idx + 1 < len(elements)
                else "end"
            )
            choices.append((f"[{idx}] --- PB --- ({prev_token} -> {next_token})", idx))
        return choices

    def _prompt_question_placement(
        definition: Mapping[str, Any],
        *,
        title: str,
        excluded_anchors: set[str] | None = None,
    ) -> tuple[str | None, str | None, str | None, str] | None:
        placement = select_from_list(
            title,
            [
                "After existing question",
                "Before existing question",
                "Start of block (prepend)",
                "End of block (append)",
                "↩ Back",
            ],
        )
        if not placement or placement.endswith("Back"):
            return None
        if placement.startswith("After"):
            anchor = _pick_question_id_from_definition(
                definition,
                message="Choose anchor question (insert after):",
                exclude=excluded_anchors,
            )
            if not anchor:
                return None
            return (None, anchor, None, "append")
        if placement.startswith("Before"):
            anchor = _pick_question_id_from_definition(
                definition,
                message="Choose anchor question (insert before):",
                exclude=excluded_anchors,
            )
            if not anchor:
                return None
            return (None, None, anchor, "append")

        mode = select_from_list(
            "How should the target block be resolved?",
            [
                "Auto target block",
                "Pick a target block",
                "↩ Back",
            ],
            instruction="Auto uses template/anchor context when possible.",
        )
        if not mode or mode.endswith("Back"):
            return None
        block_id = None
        if mode.startswith("Pick"):
            block_id = _pick_block_id_from_definition(
                definition,
                message="Choose target block:",
            )
            if not block_id:
                return None
        position = "prepend" if placement.startswith("Start") else "append"
        return (block_id, None, None, position)

    def _menu_page_breaks(*, preselected_survey_id: str | None = None) -> None:
        survey_id = (preselected_survey_id or "").strip() or _pick_survey_id(
            message="Pick a survey to edit page break(s):"
        )
        if not survey_id:
            return
        definition = _fetch_definition_for_menu(survey_id)
        if not definition:
            return

        action = select_from_list(
            "Page break action:",
            [
                "Add page break",
                "Remove page break(s)",
                "↩ Back",
            ],
            instruction="Add uses block slot placement. Remove can target one, many, or all page breaks in a block.",
        )
        if not action or action.endswith("Back"):
            return

        if action.startswith("Add page break"):
            placement = _prompt_block_slot_placement(
                definition,
                title="Choose where to insert page break in the selected block:",
            )
            if not placement:
                return
            target_block_id, insert_index = placement

            dry_run = select_from_list("Dry run?", ["No", "Yes"]) == "Yes"
            force_live = False
            publish = False
            publish_description = ""
            if not dry_run:
                force_live = (
                    select_from_list(
                        "Allow writes if finished responses exist?",
                        ["No", "Yes"],
                    )
                    == "Yes"
                )
                publish = (
                    select_from_list("Publish after page-break add?", ["Yes", "No"])
                    == "Yes"
                )
                if publish:
                    publish_description = (
                        input("Publish description (optional): ").strip() or ""
                    )

            _run_action(
                handle_add_page_break,
                argparse.Namespace(
                    survey_id=survey_id,
                    target_block_id=target_block_id,
                    after_qid=None,
                    before_qid=None,
                    position="append",
                    insert_index=insert_index,
                    dry_run=bool(dry_run),
                    force_live=bool(force_live),
                    yes=False,
                    no_publish=not bool(publish),
                    publish_description=publish_description,
                    account=selected_account,
                    interactive_mode=True,
                ),
            )
            return

        block_id = _pick_block_id_from_definition(
            definition,
            message="Choose block containing page break(s):",
        )
        if not block_id:
            return
        page_break_choices = _page_break_choices_for_block(definition, block_id=block_id)
        if not page_break_choices:
            print("[survey-menu] No page breaks found in selected block.")
            return

        label_to_index = {label: idx for label, idx in page_break_choices}
        labels = [label for label, _idx in page_break_choices]

        remove_mode = select_from_list(
            "How do you want to select page break(s) to remove?",
            [
                "Pick one page break",
                "Pick multiple page breaks",
                "Remove all page breaks in block",
                "↩ Back",
            ],
            instruction="Element indices are shown in square brackets to keep removal deterministic.",
        )
        if not remove_mode or remove_mode.endswith("Back"):
            return

        element_indices: list[int]
        if remove_mode.startswith("Pick one"):
            picked = select_from_list(
                "Select page break to remove:",
                [*labels, "↩ Back"],
                instruction="Pick one page-break element by index label.",
            )
            if not picked or picked.endswith("Back"):
                return
            idx = label_to_index.get(str(picked))
            if idx is None:
                return
            element_indices = [idx]
        elif remove_mode.startswith("Pick multiple"):
            from .interactive_menu import multi_select_from_list

            picked_labels = multi_select_from_list(
                "Select page break(s) to remove:",
                labels,
                instruction="Space to toggle, Enter to confirm.",
            )
            if picked_labels is None:
                return
            picked_unique = [
                label for label in dict.fromkeys(picked_labels) if label in label_to_index
            ]
            if not picked_unique:
                print("[survey-menu] No page breaks selected.")
                return
            element_indices = [label_to_index[label] for label in picked_unique]
        else:
            element_indices = [idx for _label, idx in page_break_choices]

        dry_run = select_from_list("Dry run?", ["No", "Yes"]) == "Yes"
        force_live = False
        publish = False
        publish_description = ""
        if not dry_run:
            force_live = (
                select_from_list(
                    "Allow writes if finished responses exist?",
                    ["No", "Yes"],
                )
                == "Yes"
            )
            publish = (
                select_from_list("Publish after page-break removal?", ["Yes", "No"])
                == "Yes"
            )
            if publish:
                publish_description = (
                    input("Publish description (optional): ").strip() or ""
                )

        _run_action(
            handle_remove_page_break,
            argparse.Namespace(
                survey_id=survey_id,
                target_block_id=block_id,
                element_index=[str(idx) for idx in element_indices],
                dry_run=bool(dry_run),
                force_live=bool(force_live),
                yes=False,
                no_publish=not bool(publish),
                publish_description=publish_description,
                account=selected_account,
                interactive_mode=True,
            ),
        )

    def _menu_blocks_staged(*, preselected_survey_id: str | None = None) -> None:
        survey_id = (preselected_survey_id or "").strip() or _pick_survey_id(
            message="Pick a survey for blocks edits (stage-first):"
        )
        if not survey_id:
            return

        try:
            yaml_path = blocks_dimension.ensure_local_surface(survey_id)
            print(f"[survey-menu] Blocks surface: {yaml_path}")
        except Exception as exc:
            print(
                "[survey-menu] ERROR: unable to initialize blocks surface. "
                f"Run `qsync blocks pull --survey-id {survey_id}` and retry. ({exc})"
            )
            return

        def _definition_with_local_blocks() -> dict[str, Any] | None:
            definition = _fetch_definition_for_menu(survey_id)
            if not definition:
                return None
            try:
                _surface_payload, local_blocks = blocks_dimension._load_blocks_surface(survey_id)  # type: ignore[attr-defined]
                if isinstance(local_blocks, dict) and local_blocks:
                    merged = dict(definition)
                    merged["Blocks"] = copy.deepcopy(local_blocks)
                    return merged
            except Exception:
                pass
            return definition

        def _pick_qids(message: str) -> list[str] | None:
            while True:
                definition = _definition_with_local_blocks()
                if not definition:
                    return None
                qids = _pick_question_ids_from_definition(definition, message=message)
                if qids is None:
                    return None
                if qids:
                    return qids
                retry = select_from_list(
                    "No questions selected.",
                    ["Choose question(s) again", "↩ Back"],
                    instruction="Select at least one question to continue.",
                )
                if not retry or retry.endswith("Back"):
                    return None

        while True:
            action = select_from_list(
                "Blocks (stage-first workflow)",
                [
                    "Move question(s) in local blocks.yaml",
                    "Remove question(s) from local blocks.yaml",
                    "Add page break in local blocks.yaml",
                    "Remove page break(s) from local blocks.yaml",
                    "Preview local blocks diff",
                    "Stage local blocks changes",
                    "Push staged blocks changes",
                    "Refresh local blocks from API (overwrite local blocks.yaml)",
                    "↩ Back",
                ],
                instruction=(
                    "Blocks controls in-block question/page-break order. "
                    "Flow controls routing between blocks."
                ),
            )
            if not action or action.endswith("Back"):
                return

            if action.startswith("Move question"):
                definition = _definition_with_local_blocks()
                if not definition:
                    continue
                qids = _pick_qids("Choose question(s) to move:")
                if not qids:
                    continue
                placement = _prompt_block_slot_placement(
                    definition,
                    title="Choose where to move selected question(s) in the target block:",
                    marked_qids=set(qids),
                )
                if not placement:
                    continue
                target_block_id, insert_index = placement
                try:
                    result = blocks_dimension.move_qid(
                        survey_id,
                        qids=qids,
                        target_block_id=target_block_id,
                        insert_index=insert_index,
                        position="append",
                    )
                    print(
                        "[survey-menu] Updated local blocks.yaml: moved "
                        f"{len(result.get('qids') or [])} QID(s) to {result.get('block_id')} "
                        f"at index {result.get('insert_index')}."
                    )
                except Exception as exc:
                    print(f"[survey-menu] ERROR: {exc}")
                continue

            if action.startswith("Remove question"):
                qids = _pick_qids("Choose question(s) to remove:")
                if not qids:
                    continue
                mode = select_from_list(
                    "Remove mode:",
                    [
                        "Move removed QIDs to Trash (default)",
                        "Detach only (do not move to Trash)",
                        "↩ Back",
                    ],
                )
                if not mode or mode.endswith("Back"):
                    continue
                move_to_trash = mode.startswith("Move removed")
                try:
                    result = blocks_dimension.remove_qid(
                        survey_id,
                        qids=qids,
                        move_to_trash=move_to_trash,
                    )
                    if result.get("moved_to_trash"):
                        print(
                            "[survey-menu] Updated local blocks.yaml: removed "
                            f"{len(result.get('qids') or [])} QID(s) and moved to Trash "
                            f"({result.get('trash_block_id')})."
                        )
                    else:
                        print(
                            "[survey-menu] Updated local blocks.yaml: detached "
                            f"{len(result.get('qids') or [])} QID(s) from active blocks."
                        )
                except Exception as exc:
                    print(f"[survey-menu] ERROR: {exc}")
                continue

            if action.startswith("Add page break"):
                definition = _definition_with_local_blocks()
                if not definition:
                    continue
                placement = _prompt_block_slot_placement(
                    definition,
                    title="Choose where to insert page break in the selected block:",
                )
                if not placement:
                    continue
                target_block_id, insert_index = placement
                try:
                    result = blocks_dimension.add_page_break(
                        survey_id,
                        target_block_id=target_block_id,
                        insert_index=insert_index,
                        position="append",
                    )
                    print(
                        "[survey-menu] Updated local blocks.yaml: inserted page break in "
                        f"{result.get('block_id')} at index {result.get('insert_index')}."
                    )
                except Exception as exc:
                    print(f"[survey-menu] ERROR: {exc}")
                continue

            if action.startswith("Remove page break"):
                definition = _definition_with_local_blocks()
                if not definition:
                    continue
                block_id = _pick_block_id_from_definition(
                    definition,
                    message="Choose block containing page break(s):",
                )
                if not block_id:
                    continue
                page_break_choices = _page_break_choices_for_block(
                    definition, block_id=block_id
                )
                if not page_break_choices:
                    print("[survey-menu] No page breaks found in selected block.")
                    continue

                label_to_index = {label: idx for label, idx in page_break_choices}
                labels = [label for label, _ in page_break_choices]
                remove_mode = select_from_list(
                    "How do you want to select page break(s) to remove?",
                    [
                        "Pick one page break",
                        "Pick multiple page breaks",
                        "Remove all page breaks in block",
                        "↩ Back",
                    ],
                    instruction="Element indices are shown in square brackets.",
                )
                if not remove_mode or remove_mode.endswith("Back"):
                    continue

                element_indices: list[int]
                if remove_mode.startswith("Pick one"):
                    picked = select_from_list(
                        "Select page break to remove:",
                        [*labels, "↩ Back"],
                    )
                    if not picked or picked.endswith("Back"):
                        continue
                    idx = label_to_index.get(str(picked))
                    if idx is None:
                        continue
                    element_indices = [idx]
                elif remove_mode.startswith("Pick multiple"):
                    from .interactive_menu import multi_select_from_list

                    picked_labels = multi_select_from_list(
                        "Select page break(s) to remove:",
                        labels,
                        instruction="Space to toggle, Enter to confirm.",
                    )
                    if picked_labels is None:
                        continue
                    picked_unique = [
                        label
                        for label in dict.fromkeys(picked_labels)
                        if label in label_to_index
                    ]
                    if not picked_unique:
                        print("[survey-menu] No page breaks selected.")
                        continue
                    element_indices = [label_to_index[label] for label in picked_unique]
                else:
                    element_indices = [idx for _label, idx in page_break_choices]

                try:
                    result = blocks_dimension.remove_page_break(
                        survey_id,
                        target_block_id=block_id,
                        element_indices=element_indices,
                    )
                    print(
                        "[survey-menu] Updated local blocks.yaml: removed "
                        f"{result.get('removed')} page break(s) from {result.get('block_id')}."
                    )
                except Exception as exc:
                    print(f"[survey-menu] ERROR: {exc}")
                continue

            if action.startswith("Preview local blocks"):
                detailed = (
                    select_from_list(
                        "Preview mode:",
                        ["Summary", "Detailed unified diff"],
                    )
                    == "Detailed unified diff"
                )
                try:
                    blocks_dimension.preview(survey_id, verbose=bool(detailed))
                except Exception as exc:
                    print(f"[survey-menu] ERROR: {exc}")
                continue

            if action.startswith("Stage local blocks"):
                allow_drift = (
                    select_from_list(
                        "Allow stage on drift (baseline != live API)?",
                        ["No", "Yes"],
                    )
                    == "Yes"
                )
                try:
                    ok = blocks_dimension.stage(
                        survey_id,
                        allow_drift=bool(allow_drift),
                        interactive=True,
                    )
                    if ok:
                        print("[survey-menu] Blocks staged successfully.")
                    else:
                        print("[survey-menu] Blocks stage failed.")
                except Exception as exc:
                    print(f"[survey-menu] ERROR: {exc}")
                continue

            if action.startswith("Push staged blocks"):
                force_live = (
                    select_from_list(
                        "Allow push if finished responses exist?",
                        ["No", "Yes"],
                    )
                    == "Yes"
                )
                force_preview = (
                    select_from_list(
                        "Force preview database push even with responses?",
                        ["No", "Yes"],
                    )
                    == "Yes"
                )
                allow_drift = (
                    select_from_list(
                        "Allow push on drift (staged baseline != live API)?",
                        ["No", "Yes"],
                    )
                    == "Yes"
                )
                publish = (
                    select_from_list(
                        "Publish after push?",
                        ["Yes", "No"],
                    )
                    == "Yes"
                )
                try:
                    ok = blocks_dimension.push(
                        survey_id,
                        interactive=True,
                        force_live=bool(force_live),
                        force_preview=bool(force_preview),
                        auto_yes=False,
                        allow_drift=bool(allow_drift),
                        skip_publish=not bool(publish),
                    )
                    if ok:
                        print("[survey-menu] Blocks push complete.")
                    else:
                        print("[survey-menu] Blocks push failed.")
                except Exception as exc:
                    print(f"[survey-menu] ERROR: {exc}")
                continue

            if action.startswith("Refresh local blocks"):
                if (
                    select_from_list(
                        "Overwrite local blocks.yaml from live API?",
                        ["No", "Yes"],
                    )
                    != "Yes"
                ):
                    continue
                try:
                    refreshed = blocks_dimension.pull(survey_id, force=True)
                    print(f"[survey-menu] Refreshed blocks surface: {refreshed}")
                except Exception as exc:
                    print(f"[survey-menu] ERROR: {exc}")
                continue

    def _menu_add_question(*, preselected_survey_id: str | None = None) -> None:
        survey_id = (preselected_survey_id or "").strip() or _pick_survey_id(
            message="Pick a survey to add question(s):"
        )
        if not survey_id:
            return
        definition = _fetch_definition_for_menu(survey_id)
        if not definition:
            return

        source_choice = select_from_list(
            "Question template source:",
            [
                "Clone existing question(s) from question bank",
                "Create new question from scratch",
                "Load question JSON file (advanced)",
                "↩ Back",
            ],
        )
        if not source_choice or source_choice.endswith("Back"):
            return

        from_question_id = None
        question_json = None
        source_account: str | None = None
        source_survey_id: str | None = None
        source_question_ids: list[str] = []
        from_scratch_mcq = False
        from_scratch_type: str | None = None
        choice_texts: list[str] = []
        statement_texts: list[str] = []
        mc_multi_response = False
        question_texts: list[str] = []

        if source_choice.startswith("Clone existing"):
            source_scope = select_from_list(
                "Clone source account:",
                [
                    f"Current account ({_account_label()})",
                    "Another linked account",
                    "↩ Back",
                ],
            )
            if not source_scope or source_scope.endswith("Back"):
                return

            source_account_scope = _resolve_menu_account()
            if source_scope.startswith("Another"):
                discovered = _discover_account_env_files(root=root)
                choices = ["default", *discovered, "↩ Back"]
                picked_account = select_from_list("Pick source account:", choices)
                if not picked_account or picked_account.endswith("Back"):
                    return
                if picked_account == "default":
                    source_account_scope = "default"
                    source_account = "default"
                else:
                    source_account_scope = picked_account
                    source_account = picked_account

            source_lookup_mode = select_from_list(
                "Question bank lookup mode:",
                [
                    "Live survey definitions (default)",
                    "Indexed local question bank cache (fast, uses pulled files)",
                    "↩ Back",
                ],
                instruction="Indexed mode avoids live source-definition fetches when cache exists.",
            )
            if not source_lookup_mode or source_lookup_mode.endswith("Back"):
                return

            indexed_payload: dict[str, Any] | None = None
            source_surveys: list[dict[str, Any]] = []
            if source_lookup_mode.startswith("Indexed"):
                try:
                    source_surveys = _get_surveys_for_account(account=source_account_scope)
                except Exception as exc:
                    print(f"[survey-menu] ERROR: unable to list source surveys: {exc}")
                    return
                indexed_payload = _load_question_bank_index(account=source_account_scope)
                if not indexed_payload:
                    rebuild = (
                        select_from_list(
                            "No fresh question-bank index found. Rebuild now from pulled survey files?",
                            ["Yes", "No (fall back to live mode)"],
                        )
                        == "Yes"
                    )
                    if rebuild:
                        indexed_payload = _build_question_bank_index(
                            account=source_account_scope,
                            surveys=source_surveys,
                        )
                        print(
                            f"[survey-menu] Indexed question bank updated ({indexed_payload.get('survey_count', 0)} surveys)."
                        )
                    else:
                        source_lookup_mode = "Live survey definitions (default)"

            if (
                source_scope.startswith("Current account")
                and preselected_survey_id
                and source_account_scope == _resolve_menu_account()
            ):
                source_survey_id = survey_id
            else:
                if source_lookup_mode.startswith("Indexed") and indexed_payload:
                    index_records: list[dict[str, Any]] = []
                    for entry in indexed_payload.get("surveys") or []:
                        if not isinstance(entry, dict):
                            continue
                        sid = str(entry.get("id") or "").strip()
                        if not sid:
                            continue
                        index_records.append(
                            {
                                "id": sid,
                                "name": str(entry.get("name") or sid),
                                "creationDate": str(entry.get("creationDate") or ""),
                            }
                        )
                    if not index_records:
                        print(
                            "[survey-menu] Index has no selectable surveys; falling back to live mode."
                        )
                        source_lookup_mode = "Live survey definitions (default)"
                    else:
                        source_survey_id = _pick_survey_id_from_records(
                            message="Pick source survey to clone from:",
                            records=index_records,
                        )
                if not source_survey_id:
                    if not source_surveys:
                        try:
                            source_surveys = _get_surveys_for_account(
                                account=source_account_scope
                            )
                        except Exception as exc:
                            print(f"[survey-menu] ERROR: unable to list source surveys: {exc}")
                            return
                    source_survey_id = _pick_survey_id_from_records(
                        message="Pick source survey to clone from:",
                        records=source_surveys,
                    )
                if not source_survey_id:
                    return

            source_labels: list[str] = []
            if source_lookup_mode.startswith("Indexed") and indexed_payload:
                source_labels = _question_labels_from_index_payload(
                    indexed_payload, survey_id=source_survey_id
                )
            if not source_labels:
                source_definition = (
                    definition
                    if source_survey_id == survey_id
                    and source_account_scope == _resolve_menu_account()
                    else _fetch_definition_for_menu(
                        source_survey_id, account=source_account_scope
                    )
                )
                if not source_definition:
                    return
                source_labels = _question_labels_from_definition(source_definition)

            while True:
                picked_source_qids = _pick_question_ids_from_labels(
                    labels=source_labels,
                    message="Choose source question(s) to clone:",
                    preserve_selection_order=True,
                )
                if picked_source_qids is None:
                    return
                if picked_source_qids:
                    source_question_ids = picked_source_qids
                    break
                retry_pick = select_from_list(
                    "No source questions selected.",
                    ["Choose source question(s) again", "↩ Back"],
                    instruction="Select at least one source question to continue cloning.",
                )
                if not retry_pick or retry_pick.endswith("Back"):
                    return

            text_mode = select_from_list(
                "Question text behavior:",
                [
                    "Keep source question text(s)",
                    "Override with one text for all cloned questions",
                    "Override with one text per cloned question",
                    "↩ Back",
                ],
            )
            if not text_mode or text_mode.endswith("Back"):
                return
            if text_mode.startswith("Override with one text for all"):
                one_text = input("Question text override: ").strip()
                if not one_text:
                    return
                question_texts = [one_text]
            elif text_mode.startswith("Override with one text per"):
                from .interactive_menu import edit_text_in_editor

                seed = "\n".join(
                    [f"Question {idx + 1}" for idx in range(len(source_question_ids))]
                )
                blob = edit_text_in_editor(
                    f"Enter exactly {len(source_question_ids)} lines (one per cloned question).",
                    initial_text=seed,
                    suffix=".txt",
                )
                if blob is None:
                    return
                lines = [line.strip() for line in str(blob).splitlines() if line.strip()]
                if len(lines) != len(source_question_ids):
                    print(
                        f"[survey-menu] Expected {len(source_question_ids)} lines, got {len(lines)}."
                    )
                    return
                question_texts = lines
        elif source_choice.startswith("Create new question"):
            from_scratch_type_choice = select_from_list(
                "From-scratch question type:",
                [
                    "Multiple choice (MC)",
                    "Text entry (TE)",
                    "Matrix (Likert)",
                    "Descriptive text (DB)",
                    "↩ Back",
                ],
                instruction="MVP supports all types currently found in local surveys.",
            )
            if not from_scratch_type_choice or from_scratch_type_choice.endswith("Back"):
                return
            if from_scratch_type_choice.startswith("Multiple choice"):
                from_scratch_type = "mc"
                from_scratch_mcq = True
            elif from_scratch_type_choice.startswith("Text entry"):
                from_scratch_type = "te"
            elif from_scratch_type_choice.startswith("Matrix"):
                from_scratch_type = "matrix"
            else:
                from_scratch_type = "db"

            text_mode = select_from_list(
                "How many questions should be created?",
                [
                    "Enter one question text",
                    "Enter multiple question texts (one per line)",
                    "↩ Back",
                ],
            )
            if not text_mode or text_mode.endswith("Back"):
                return
            if text_mode.startswith("Enter one"):
                one_text = input("Question text: ").strip()
                if not one_text:
                    return
                question_texts = [one_text]
            else:
                from .interactive_menu import edit_text_in_editor

                blob = edit_text_in_editor(
                    "Enter one question text per line.",
                    initial_text="",
                    suffix=".txt",
                )
                if blob is None:
                    return
                question_texts = [line.strip() for line in str(blob).splitlines() if line.strip()]
                if not question_texts:
                    print("[survey-menu] No non-empty question lines were provided.")
                    return

            if from_scratch_type == "mc":
                from .interactive_menu import edit_text_in_editor

                choices_blob = edit_text_in_editor(
                    "Enter answer options (one per line, at least 2).",
                    initial_text="Option 1\nOption 2",
                    suffix=".txt",
                )
                if choices_blob is None:
                    return
                choice_texts = [
                    line.strip() for line in str(choices_blob).splitlines() if line.strip()
                ]
                if len(choice_texts) < 2:
                    print("[survey-menu] At least two answer options are required.")
                    return
                mc_multi_response = (
                    select_from_list(
                        "Allow selecting multiple answers?",
                        ["No (single answer)", "Yes (multiple answers)"],
                    )
                    == "Yes (multiple answers)"
                )
            elif from_scratch_type == "matrix":
                from .interactive_menu import edit_text_in_editor

                statements_blob = edit_text_in_editor(
                    "Enter matrix statements/rows (one per line, at least 1).",
                    initial_text="Statement 1",
                    suffix=".txt",
                )
                if statements_blob is None:
                    return
                statement_texts = [
                    line.strip()
                    for line in str(statements_blob).splitlines()
                    if line.strip()
                ]
                if len(statement_texts) < 1:
                    print("[survey-menu] At least one matrix statement is required.")
                    return
                answers_blob = edit_text_in_editor(
                    "Enter matrix answer options (one per line, at least 2).",
                    initial_text="Strongly disagree\nDisagree\nNeutral\nAgree\nStrongly agree",
                    suffix=".txt",
                )
                if answers_blob is None:
                    return
                choice_texts = [
                    line.strip() for line in str(answers_blob).splitlines() if line.strip()
                ]
                if len(choice_texts) < 2:
                    print("[survey-menu] At least two matrix answer options are required.")
                    return
        else:
            question_json = input("Path to question JSON file: ").strip() or None
            if not question_json:
                return
            text_mode = select_from_list(
                "How many questions should be created?",
                [
                    "Use template text (create one question)",
                    "Enter one question text",
                    "Enter multiple question texts (one per line)",
                    "↩ Back",
                ],
            )
            if not text_mode or text_mode.endswith("Back"):
                return
            if text_mode.startswith("Enter one"):
                one_text = input("Question text: ").strip()
                if not one_text:
                    return
                question_texts = [one_text]
            elif text_mode.startswith("Enter multiple"):
                from .interactive_menu import edit_text_in_editor

                blob = edit_text_in_editor(
                    "Enter one question text per line.",
                    initial_text="",
                    suffix=".txt",
                )
                if blob is None:
                    return
                question_texts = [
                    line.strip() for line in str(blob).splitlines() if line.strip()
                ]
                if not question_texts:
                    print("[survey-menu] No non-empty question lines were provided.")
                    return

        placement = _prompt_block_slot_placement(
            definition,
            title="Choose where to insert new question(s) in the selected block:",
        )
        if not placement:
            return
        target_block_id, insert_index = placement
        after_qid = None
        before_qid = None
        position = "append"

        page_break_mode = "none"
        page_break_choice = select_from_list(
            "Page break handling for inserted question(s):",
            [
                "No extra page breaks",
                "Add page break before inserted question(s)",
                "Add page break after inserted question(s)",
                "Add page break between inserted questions",
                "↩ Back",
            ],
            instruction="This controls page-break elements inserted together with the new questions.",
        )
        if not page_break_choice or page_break_choice.endswith("Back"):
            return
        if page_break_choice.startswith("Add page break before"):
            page_break_mode = "before"
        elif page_break_choice.startswith("Add page break after"):
            page_break_mode = "after"
        elif page_break_choice.startswith("Add page break between"):
            page_break_mode = "between"

        data_export_tag = input("Base DataExportTag (optional): ").strip() or None
        allow_duplicate_tags = False
        if data_export_tag:
            allow_duplicate_tags = (
                select_from_list(
                    "Allow duplicate DataExportTag values?",
                    ["No", "Yes"],
                )
                == "Yes"
            )

        dry_run = select_from_list("Dry run?", ["No", "Yes"]) == "Yes"
        force_live = False
        publish = False
        publish_description = ""
        if not dry_run:
            force_live = (
                select_from_list(
                    "Allow writes if finished responses exist?",
                    ["No", "Yes"],
                )
                == "Yes"
            )
            publish = select_from_list("Publish after add?", ["Yes", "No"]) == "Yes"
            if publish:
                publish_description = (
                    input("Publish description (optional): ").strip() or ""
                )

        _run_action(
            handle_add_question,
            argparse.Namespace(
                survey_id=survey_id,
                from_question_id=from_question_id,
                question_json=question_json,
                question_text=question_texts or None,
                question_text_file=None,
                target_block_id=target_block_id,
                after_qid=after_qid,
                before_qid=before_qid,
                position=position,
                insert_index=insert_index,
                page_break_mode=page_break_mode,
                data_export_tag=data_export_tag,
                allow_duplicate_tags=bool(allow_duplicate_tags),
                dry_run=bool(dry_run),
                force_live=bool(force_live),
                yes=False,
                no_publish=not bool(publish),
                publish_description=publish_description,
                account=selected_account,
                source_account=source_account,
                source_survey_id=source_survey_id,
                source_question_id=source_question_ids or None,
                from_scratch_mcq=bool(from_scratch_mcq),
                from_scratch_type=from_scratch_type,
                choice_text=choice_texts or None,
                choice_text_file=None,
                statement_text=statement_texts or None,
                statement_text_file=None,
                mc_multi_response=bool(mc_multi_response),
                interactive_mode=True,
            ),
        )

    def _menu_move_question(*, preselected_survey_id: str | None = None) -> None:
        survey_id = (preselected_survey_id or "").strip() or _pick_survey_id(
            message="Pick a survey to move question(s):"
        )
        if not survey_id:
            return
        definition = _fetch_definition_for_menu(survey_id)
        if not definition:
            return

        while True:
            qids = _pick_question_ids_from_definition(
                definition,
                message="Choose question(s) to move:",
            )
            if qids is None:
                return
            if qids:
                break
            retry_pick = select_from_list(
                "No questions selected to move.",
                ["Choose question(s) again", "↩ Back"],
                instruction="Select at least one question to continue.",
            )
            if not retry_pick or retry_pick.endswith("Back"):
                return

        placement = _prompt_block_slot_placement(
            definition,
            title="Choose where to move selected question(s) in the target block:",
            marked_qids=set(qids),
        )
        if not placement:
            return
        target_block_id, insert_index = placement
        after_qid = None
        before_qid = None
        position = "append"

        dry_run = select_from_list("Dry run?", ["No", "Yes"]) == "Yes"
        force_live = False
        publish = False
        publish_description = ""
        if not dry_run:
            force_live = (
                select_from_list(
                    "Allow writes if finished responses exist?",
                    ["No", "Yes"],
                )
                == "Yes"
            )
            publish = select_from_list("Publish after move?", ["Yes", "No"]) == "Yes"
            if publish:
                publish_description = (
                    input("Publish description (optional): ").strip() or ""
                )

        _run_action(
            handle_move_question,
            argparse.Namespace(
                survey_id=survey_id,
                question_id=qids,
                target_block_id=target_block_id,
                after_qid=after_qid,
                before_qid=before_qid,
                position=position,
                insert_index=insert_index,
                dry_run=bool(dry_run),
                force_live=bool(force_live),
                yes=False,
                no_publish=not bool(publish),
                publish_description=publish_description,
                account=selected_account,
                interactive_mode=True,
            ),
        )

    def _menu_remove_question(*, preselected_survey_id: str | None = None) -> None:
        survey_id = (preselected_survey_id or "").strip() or _pick_survey_id(
            message="Pick a survey to remove question(s):"
        )
        if not survey_id:
            return
        definition = _fetch_definition_for_menu(survey_id)
        if not definition:
            return

        while True:
            qids = _pick_question_ids_from_definition(
                definition,
                message="Choose question(s) to remove:",
            )
            if qids is None:
                return
            if qids:
                break
            retry_pick = select_from_list(
                "No questions selected to remove.",
                ["Choose question(s) again", "↩ Back"],
                instruction="Select at least one question to continue.",
            )
            if not retry_pick or retry_pick.endswith("Back"):
                return

        dry_run = select_from_list("Dry run?", ["No", "Yes"]) == "Yes"
        force_live = False
        publish = False
        publish_description = ""
        if not dry_run:
            force_live = (
                select_from_list(
                    "Allow writes if finished responses exist?",
                    ["No", "Yes"],
                )
                == "Yes"
            )
            publish = (
                select_from_list("Publish after remove?", ["Yes", "No"]) == "Yes"
            )
            if publish:
                publish_description = (
                    input("Publish description (optional): ").strip() or ""
                )

        _run_action(
            handle_remove_question,
            argparse.Namespace(
                survey_id=survey_id,
                question_id=qids,
                dry_run=bool(dry_run),
                force_live=bool(force_live),
                yes=False,
                no_publish=not bool(publish),
                publish_description=publish_description,
                account=selected_account,
                interactive_mode=True,
            ),
        )

    direct_structural = bool(getattr(args, "structural_edit", False))
    direct_add_question = bool(getattr(args, "add_question_interactive", False))
    direct_move_question = bool(getattr(args, "move_question_interactive", False))
    direct_remove_question = bool(
        getattr(args, "remove_question_interactive", False)
    )
    direct_replace_question = bool(
        getattr(args, "replace_question_interactive", False)
    )
    direct_page_break = bool(getattr(args, "page_break_interactive", False))
    direct_survey_id = str(getattr(args, "survey_id", "") or "").strip() or None
    if sum(
        int(flag)
        for flag in (
            direct_structural,
            direct_add_question,
            direct_move_question,
            direct_remove_question,
            direct_replace_question,
            direct_page_break,
        )
    ) > 1:
        raise SystemExit(
            "[survey-menu] ERROR: choose only one direct mode: "
            "--structural-edit, --add-question-interactive, --move-question-interactive, "
            "--remove-question-interactive, --replace-question-interactive, "
            "or --page-break-interactive."
        )
    if direct_survey_id and not (
        direct_structural
        or direct_add_question
        or direct_move_question
        or direct_remove_question
        or direct_replace_question
        or direct_page_break
    ):
        raise SystemExit(
            "[survey-menu] ERROR: --survey-id requires one of "
            "--structural-edit, --add-question-interactive, "
            "--move-question-interactive, --remove-question-interactive, "
            "--replace-question-interactive, "
            "--page-break-interactive."
        )
    if direct_structural:
        _menu_items_structural_edits(preselected_survey_id=direct_survey_id)
        return
    if direct_add_question:
        _menu_add_question(preselected_survey_id=direct_survey_id)
        return
    if direct_move_question:
        _menu_move_question(preselected_survey_id=direct_survey_id)
        return
    if direct_remove_question:
        _menu_remove_question(preselected_survey_id=direct_survey_id)
        return
    if direct_replace_question:
        _menu_replace_question(preselected_survey_id=direct_survey_id)
        return
    if direct_page_break:
        _menu_page_breaks(preselected_survey_id=direct_survey_id)
        return

    quick_action = str(getattr(args, "quick_action", "") or "").strip()
    if quick_action:
        def _qa_setup_list() -> None:
            pattern = input("Optional name regex (blank = all): ").strip() or None
            _run_action(
                handle_list,
                argparse.Namespace(name_pattern=pattern, account=selected_account),
            )

        def _qa_setup_label() -> None:
            sid = _pick_survey_id(message="Pick a survey to label:")
            if sid:
                _run_action(handle_label, argparse.Namespace(survey_id=sid))

        def _qa_setup_focal() -> None:
            newline = select_from_list("One ID per line?", ["No", "Yes"]) == "Yes"
            _run_action(handle_focal, argparse.Namespace(newline=bool(newline)))

        actions: dict[str, Any] = {
            # Setup / selection
            "setup-list": _qa_setup_list,
            "setup-label": _qa_setup_label,
            "setup-focal": _qa_setup_focal,
            "setup-pull": _menu_pull,
            # Edit
            "edit-structural": _menu_items_structural_edits,
            "edit-blocks-staged": _menu_blocks_staged,
            "edit-add-question": _menu_add_question,
            "edit-move-question": _menu_move_question,
            "edit-remove-question": _menu_remove_question,
            "edit-page-breaks": _menu_page_breaks,
            "edit-inspect-question": _menu_inspect_question,
            "edit-push-question": _menu_push_question,
            "edit-replace-question": _menu_replace_question,
            "edit-stage-by-qid": _menu_stage_by_qid,
            # Flow / embedded / integrations
            "flow-add-embedded": lambda: _menu_embedded_field("add-embedded-field"),
            "flow-remove-embedded": lambda: _menu_embedded_field("remove-embedded-field"),
            "flow-rename-embedded": lambda: _menu_embedded_field("rename-embedded-field"),
            "flow-cleanup-embedded": _menu_cleanup_embedded_data,
            "flow-prolific-auth": _menu_prolific_auth,
            "flow-prolific-wiring": _menu_prolific_wiring,
            # Publish / lifecycle
            "publish-activate": lambda: _menu_activate(active=True),
            "publish-deactivate": lambda: _menu_activate(active=False),
            "publish-publish": _menu_publish,
            "publish-versions": _menu_versions,
            "publish-fetch-version": _menu_version_fetch,
            "publish-rollback": _menu_rollback,
            # Copy / slice / parity
            "copy-copy": _menu_copy,
            "copy-slice-language": _menu_slice_language,
            "copy-slice-registry": _menu_slice_registry,
            "copy-parity": _menu_parity_check,
            "copy-cross-account": _menu_copy_cross_account,
            # Exports
            "export-responses": _menu_export_responses,
            "export-translation": _menu_export_translation,
            "export-side-by-side": _menu_export_side_by_side,
            # Workspace / account
            "workspace-switch-account": _menu_switch_account,
            "workspace-show-account": _menu_show_account_info,
            "workspace-check-api": _menu_check_api,
            "workspace-refresh-inventory": _menu_inventory,
            "workspace-refresh-question-bank": _menu_refresh_question_bank_index,
            "workspace-prepare": _menu_prepare,
            "workspace-configure-cache": _menu_configure_cache_folder,
            "workspace-configure-externally-managed": _menu_configure_externally_managed_overrides,
            # Bulk / master
            "bulk-master": _menu_master,
            # Danger zone
            "danger-rename": _menu_rename,
            "danger-delete": _menu_delete,
        }
        handler = actions.get(quick_action)
        if handler is None:
            raise SystemExit(
                f"[survey-menu] ERROR: unknown quick action '{quick_action}'."
            )
        handler()
        return

    def _menu_context(path: str, summary: str, reachable: str) -> None:
        print()
        print(f"[survey-menu] Path: {path}")
        print(f"[survey-menu] {summary}")
        print(f"[survey-menu] Reachable: {reachable}")
        print()

    while True:
        base = _resolve_base_url_for_display() or "(base URL unknown)"
        _menu_context(
            f"Survey Menu (account={_account_label()} base={base})",
            "Choose the task type first; each submenu is organized by user intent.",
            "Setup/Selection, Edit, Flow/Integrations, Publish/Versions, Copy/Compare, Exports, Workspace/Account, Bulk/Master, Danger Zone",
        )
        top = select_from_list(
            message=f"qsync survey menu  (account: {_account_label()}  base: {base})",
            choices=[
                "Survey Setup & Selection — list/label/focal/pull",
                "Edit Questions & Content — structural item edits",
                "Flow, Embedded Data & Integrations — embedded fields + Prolific",
                "Publish, Activation & Versions — go live and rollback",
                "Copy, Slice & Compare — derive and verify surveys",
                "Exports — responses and docs",
                "Workspace & Account — account, API, inventory, prepare",
                "Bulk & Master — focal bulk editing",
                "Danger Zone — rename/delete",
                "Exit",
            ],
            instruction="Pick a task group. You can always return with ↩ Back.",
        )
        if not top or top == "Exit":
            return

        if top.startswith("Survey Setup & Selection"):
            _menu_context(
                "Survey Menu > Survey Setup & Selection",
                "Find/select surveys and pull a local cache copy.",
                "List surveys, label IDs, focal list, pull cache",
            )
            choice = select_from_list(
                "Survey Setup & Selection",
                [
                    "List surveys",
                    "Label survey ID (inventory)",
                    "List focal survey IDs (inventory)",
                    "Pull survey definition (cache)",
                    "↩ Back",
                ],
                instruction="Use this when you are trying to find/select the right survey first.",
            )
            if not choice or choice.endswith("Back"):
                continue
            if choice.startswith("List surveys"):
                pattern = input("Optional name regex (blank = all): ").strip() or None
                _run_action(
                    handle_list,
                    argparse.Namespace(name_pattern=pattern, account=selected_account),
                )
            elif choice.startswith("Label"):
                sid = _pick_survey_id(message="Pick a survey to label:")
                if sid:
                    _run_action(handle_label, argparse.Namespace(survey_id=sid))
            elif choice.startswith("List focal"):
                newline = select_from_list("One ID per line?", ["No", "Yes"]) == "Yes"
                _run_action(handle_focal, argparse.Namespace(newline=bool(newline)))
            else:
                _menu_pull()
            continue

        if top.startswith("Edit Questions & Content"):
            _menu_context(
                "Survey Menu > Edit Questions & Content",
                "Question-level content edits (safe staged workflow).",
                "Items + blocks staged edits, add/move/remove/replace/page-break edits, inspect/push utilities, QID-scoped staging",
            )
            choice = select_from_list(
                "Edit Questions & Content",
                [
                    "Items: structural edits (stage → preview → push)",
                    "Blocks: stage-first block-internal edits (move/remove/page-break)",
                    "Add question(s) (clone template, insert in flow)",
                    "Move question(s) (reorder / move across blocks)",
                    "Remove question(s) (move selected QIDs to Trash)",
                    "Replace question payload (source survey/QID → target QID)",
                    "Page breaks (add/remove in block flow)",
                    "Inspect question payload (local cache)",
                    "Push one question from local cache",
                    "Stage by QID (items/js/translations)",
                    "↩ Back",
                ],
                instruction="Use add/move/page-break for flow placement; utilities help inspect/push/scope by QID.",
            )
            if not choice or choice.endswith("Back"):
                continue
            if choice.startswith("Items:"):
                _menu_items_structural_edits()
            elif choice.startswith("Blocks:"):
                _menu_blocks_staged()
            elif choice.startswith("Add question"):
                _menu_add_question()
            elif choice.startswith("Move question"):
                _menu_move_question()
            elif choice.startswith("Remove question"):
                _menu_remove_question()
            elif choice.startswith("Replace question payload"):
                _menu_replace_question()
            elif choice.startswith("Page breaks"):
                _menu_page_breaks()
            elif choice.startswith("Inspect question"):
                _menu_inspect_question()
            elif choice.startswith("Push one question"):
                _menu_push_question()
            else:
                _menu_stage_by_qid()
            continue

        if top.startswith("Flow, Embedded Data & Integrations"):
            _menu_context(
                "Survey Menu > Flow, Embedded Data & Integrations",
                "SurveyFlow and integration settings (embedded data + Prolific wiring).",
                "Embedded field add/remove/rename, cleanup, Prolific snippet, Prolific wiring",
            )
            choice = select_from_list(
                "Flow, Embedded Data & Integrations",
                [
                    "Add embedded field (stage)",
                    "Remove embedded field (stage)",
                    "Rename embedded field (stage)",
                    "Cleanup embedded data (apply)",
                    "Prolific authenticity snippet",
                    "Prolific wiring (Prolific ↔ Qualtrics)",
                    "↩ Back",
                ],
                instruction="Use Prolific wiring path for pull → propose → review → preview/apply.",
            )
            if not choice or choice.endswith("Back"):
                continue
            if choice.startswith("Add embedded"):
                _menu_embedded_field("add-embedded-field")
            elif choice.startswith("Remove embedded"):
                _menu_embedded_field("remove-embedded-field")
            elif choice.startswith("Rename embedded"):
                _menu_embedded_field("rename-embedded-field")
            elif choice.startswith("Cleanup embedded"):
                _menu_cleanup_embedded_data()
            elif choice.startswith("Prolific authenticity"):
                _menu_prolific_auth()
            else:
                _menu_prolific_wiring()
            continue

        if top.startswith("Publish, Activation & Versions"):
            _menu_context(
                "Survey Menu > Publish, Activation & Versions",
                "Go-live lifecycle operations and version recovery.",
                "Publish, activate/deactivate, version list/fetch, rollback",
            )
            choice = select_from_list(
                "Publish, Activation & Versions",
                [
                    "Activate survey",
                    "Deactivate survey",
                    "Publish survey-definition (new version)",
                    "List versions",
                    "Fetch a version",
                    "Rollback questions to a version",
                    "↩ Back",
                ],
                instruction="Typical path: edit → publish → activate. Use rollback for question restore.",
            )
            if not choice or choice.endswith("Back"):
                continue
            if choice.startswith("Activate"):
                _menu_activate(active=True)
            elif choice.startswith("Deactivate"):
                _menu_activate(active=False)
            elif choice.startswith("Publish"):
                _menu_publish()
            elif choice.startswith("List versions"):
                _menu_versions()
            elif choice.startswith("Fetch"):
                _menu_version_fetch()
            else:
                _menu_rollback()
            continue

        if top.startswith("Copy, Slice & Compare"):
            _menu_context(
                "Survey Menu > Copy, Slice & Compare",
                "Derive new surveys and compare parity across copies/accounts.",
                "Copy, slice-language, slice-registry, parity-check, copy-cross-account",
            )
            choice = select_from_list(
                "Copy, Slice & Compare",
                [
                    "Copy survey",
                    "Slice language(s)",
                    "Slice registry (local)",
                    "Parity check",
                    "Copy cross-account",
                    "↩ Back",
                ],
                instruction="Use parity check after copy/slice if you need verification.",
            )
            if not choice or choice.endswith("Back"):
                continue
            if choice.startswith("Copy survey"):
                _menu_copy()
            elif choice.startswith("Slice language"):
                _menu_slice_language()
            elif choice.startswith("Slice registry"):
                _menu_slice_registry()
            elif choice.startswith("Parity"):
                _menu_parity_check()
            else:
                _menu_copy_cross_account()
            continue

        if top.startswith("Exports"):
            _menu_context(
                "Survey Menu > Exports",
                "Generate response and document exports for review.",
                "Export responses, translation doc, side-by-side doc",
            )
            choice = select_from_list(
                "Exports",
                [
                    "Export responses",
                    "Export translation document",
                    "Export side-by-side document",
                    "↩ Back",
                ],
                instruction="Choose the document/export type needed for sharing or QA.",
            )
            if not choice or choice.endswith("Back"):
                continue
            if choice.startswith("Export responses"):
                _menu_export_responses()
            elif choice.startswith("Export translation"):
                _menu_export_translation()
            else:
                _menu_export_side_by_side()
            continue

        if top.startswith("Workspace & Account"):
            _menu_context(
                "Survey Menu > Workspace & Account",
                "Environment/account controls and local cache preparation.",
                "Switch account, whoami/API checks, inventory refresh, question-bank index refresh, prepare, cache folder, externally managed overrides",
            )
            choice = select_from_list(
                "Workspace & Account",
                [
                    "Switch account",
                    "Show account info",
                    "Check API (/whoami)",
                    "Refresh inventory",
                    "Refresh question-bank index",
                    "Prepare surfaces",
                    "Configure survey cache folder",
                    "Configure externally managed item overrides",
                    "↩ Back",
                ],
                instruction="Use this area before edits when account/workspace setup may be the issue.",
            )
            if not choice or choice.endswith("Back"):
                continue
            if choice.startswith("Switch"):
                _menu_switch_account()
            elif choice.startswith("Show"):
                _menu_show_account_info()
            elif choice.startswith("Check API"):
                _menu_check_api()
            elif choice == "Refresh inventory":
                _menu_inventory()
            elif choice.startswith("Refresh question-bank"):
                _menu_refresh_question_bank_index()
            elif choice.startswith("Prepare"):
                _menu_prepare()
            elif choice.startswith("Configure externally managed"):
                _menu_configure_externally_managed_overrides()
            else:
                _menu_configure_cache_folder()
            continue

        if top.startswith("Bulk & Master"):
            _menu_context(
                "Survey Menu > Bulk & Master",
                "Focal-survey bulk editing workflow and scoped staging helper.",
                "Master pull/preview/stage/push/rollback plus optional stage-by-QID flow",
            )
            _menu_master()
            continue

        if top.startswith("Danger Zone"):
            _menu_context(
                "Survey Menu > Danger Zone",
                "Mutating admin operations with destructive risk.",
                "Rename, delete (guided confirmations required)",
            )
            choice = select_from_list(
                "Danger Zone",
                [
                    "Rename survey",
                    "Delete survey(s) (dry-run + guided gates)",
                    "↩ Back",
                ],
                instruction="Use carefully; delete is permanent.",
            )
            if not choice or choice.endswith("Back"):
                continue
            if choice.startswith("Rename"):
                _menu_rename()
            else:
                _menu_delete()


def handle_prepare(args: argparse.Namespace) -> None:
    """Hydrate all local editing surfaces for one or more surveys (pull-only)."""

    from .survey_prepare import prepare_workspace, resolve_target_surveys
    from .terminal_output import header, info, success, warn, error

    root = _workspace_root()
    raw_account = str(getattr(args, "account", "") or "").strip()
    explicit_default = raw_account.lower() == "default"
    resolved_account = _resolve_account_from_args(args)
    account_scope = "default" if explicit_default else resolved_account
    selected_env = None
    if explicit_default:
        selected_env = load_account_env("default", root=root)
    elif resolved_account:
        selected_env = load_account_env(resolved_account, root=root)

    @contextlib.contextmanager
    def _temporary_account_scope(scope: str | None):
        previous = os.environ.get("QSYNC_ACCOUNT")
        try:
            if scope and scope.strip().lower() != "default":
                os.environ["QSYNC_ACCOUNT"] = scope.strip()
            else:
                os.environ.pop("QSYNC_ACCOUNT", None)
            yield
        finally:
            if previous is None:
                os.environ.pop("QSYNC_ACCOUNT", None)
            else:
                os.environ["QSYNC_ACCOUNT"] = previous

    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    surfaces_raw = (getattr(args, "surfaces", None) or "").strip()
    surfaces = None
    if surfaces_raw:
        parts = [p.strip().lower() for p in surfaces_raw.split(",") if p.strip()]
        surfaces = set(parts)

    languages = _collect_languages_from_args(args)
    try:
        with _temporary_account_scope(account_scope):
            survey_ids = resolve_target_surveys(
                survey_id=getattr(args, "survey_id", None),
                focal=bool(getattr(args, "focal", False)),
                all_surveys=bool(getattr(args, "all_surveys", False)),
                interactive=interactive,
                yes=bool(getattr(args, "yes", False)),
                root=root,
                account=account_scope,
                env=selected_env,
            )
    except Exception as exc:
        raise SystemExit(f"[qsync:survey-prepare] ERROR: {exc}") from exc

    header("[qsync:survey-prepare]", f"Preparing {len(survey_ids)} survey(s)...")
    with _temporary_account_scope(account_scope):
        results = prepare_workspace(
            survey_ids=survey_ids,
            yes=bool(getattr(args, "yes", False)),
            interactive=interactive and not bool(getattr(args, "yes", False)),
            overwrite_js=bool(getattr(args, "overwrite_js", False)),
            shared_js=bool(getattr(args, "shared_js", False)),
            surfaces=surfaces,
            languages=languages,
            root=root,
            account=account_scope,
            env=selected_env,
        )

    for r in results:
        info(None, f"\nSurvey {r.survey_id}:")
        for label, surface in (
            ("cache", r.cache),
            ("workbook", r.workbook),
            ("translations", r.translations),
            ("eos", r.eos),
            ("js", r.js),
            ("js-mapping", r.js_mapping),
        ):
            if surface.errors:
                error(None, f"  - {label}: ERROR ({'; '.join(surface.errors[:2])})")
                continue
            created = surface.created
            skipped = surface.skipped
            if created and not skipped:
                success(None, f"  - {label}: created={created}")
            elif created or skipped:
                info(None, f"  - {label}: created={created} skipped={skipped}")
            else:
                info(None, f"  - {label}: (no-op)")
            for note in getattr(surface, "notes", [])[:3]:
                info(None, f"    note: {note}")

        warn(
            None,
            "  Next: edit under excel/, survey_js/, contents/; then use preview/stage/push workflows.",
        )


def handle_inspect_question(args: argparse.Namespace) -> None:
    """Print a cached question payload (by QID) from a local survey JSON file."""
    from .cli import _prompt_for_survey_id_if_needed

    survey_id = _prompt_for_survey_id_if_needed(args.survey_id, allow_all_surveys=False)
    question_id = args.question_id

    survey_file = getattr(args, "survey_file", None)
    if survey_file:
        survey_path = Path(survey_file)
    else:
        survey_path = find_cached_survey_file(survey_id)
        if not survey_path:
            print(
                f"[inspect-question] ERROR: No cached survey file found for {survey_id}"
            )
            print("  Run `qsync survey pull --survey-id <ID>` first.")
            sys.exit(1)

    if not survey_path.exists():
        print(f"[inspect-question] ERROR: Survey file not found: {survey_path}")
        sys.exit(1)

    payload = json.loads(survey_path.read_text(encoding="utf-8"))
    try:
        question_payload = _extract_question(payload, question_id)
    except KeyError as exc:
        print(f"[inspect-question] ERROR: {exc}")
        sys.exit(1)

    field = getattr(args, "field", None)
    if field:
        if field not in question_payload:
            raise SystemExit(
                f"[inspect-question] Field '{field}' not found in {question_id}."
            )
        value = question_payload[field]
        if isinstance(value, str) and args.raw:
            print(value)
            return
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return

    print(json.dumps(question_payload, ensure_ascii=False, indent=2, sort_keys=True))


def list_surveys(base: str, headers: Dict[str, str]) -> List[Dict[str, Any]]:
    """Fetch list of all surveys (follows pagination until exhausted)."""

    url: str | None = f"https://{base}/API/v3/surveys"
    surveys: list[dict[str, Any]] = []
    first_page = True
    seen_urls: set[str] = set()

    while url:
        if url in seen_urls:
            # Avoid infinite loops on unexpected pagination responses.
            break
        seen_urls.add(url)

        # Qualtrics survey listing pagination uses `nextPage` URLs and
        # `pageSize` on the first request (observed in inventory fetch).
        params = {"pageSize": 100} if first_page else None
        resp = send_api_request(
            action="qsync.survey.list",
            method="GET",
            base_url=base,
            headers=headers,
            path=url,
            log_event=False,
            params=params,
            timeout=60,
        )
        payload = resp.json()
        result = payload.get("result") or {}
        elements = result.get("elements") or []
        if isinstance(elements, list):
            surveys.extend([e for e in elements if isinstance(e, dict)])

        next_url = result.get("nextPage")
        url = str(next_url).strip() if next_url else None
        first_page = False

    return surveys


def _order_surveys_like_inventory(
    surveys: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Apply the same default ordering used for inventory.csv persistence."""

    stage_order = {"main": 0}
    component_order = {"pre": 0}
    cntry_order = {"IE": 0, "NL": 1, "CZ": 2, "FR": 3, "UK": 4, "US": 5}
    true_tokens = {"1", "true", "t", "yes", "y", "on"}

    inventory_by_id: dict[str, dict] = {}
    csv_path = _inventory_csv_path(_workspace_root())
    if csv_path.exists():
        for row in _iter_inventory_rows(csv_path):
            survey_id = str(row.get("id") or "").strip()
            if survey_id:
                inventory_by_id[survey_id] = row

    def _as_bool(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in true_tokens
        return bool(value)

    def _inventory_last_modified(survey: dict) -> str:
        survey_id = str(survey.get("id") or "").strip()
        inv = inventory_by_id.get(survey_id, {})
        return str(
            inv.get("lastModified")
            or survey.get("lastModified")
            or survey.get("creationDate")
            or ""
        )

    def _inventory_sort_key(survey: dict) -> tuple:
        survey_id = str(survey.get("id") or "").strip()
        inv = inventory_by_id.get(survey_id, {})
        focal = _as_bool(inv.get("focal"))
        stage = str(inv.get("stage") or "main").strip()
        component = str(inv.get("component") or "pre").strip()
        cntry = str(inv.get("cntry") or "US").strip()
        return (
            0 if focal else 1,
            stage_order.get(stage, 99),
            component_order.get(component, 99),
            cntry_order.get(cntry, 99),
        )

    sorted_by_modified = sorted(
        surveys,
        key=_inventory_last_modified,
        reverse=True,
    )
    return sorted(sorted_by_modified, key=_inventory_sort_key)


def fetch_survey_definition(
    base: str, headers: Dict[str, str], survey_id: str, fmt: str = "json"
) -> dict:
    """Fetch a survey definition from Qualtrics (JSON payload or QSF format)."""

    params = {}
    if fmt == "qsf":
        params["format"] = "qsf"

    resp = send_api_request(
        action="qsync.survey.fetch.definition",
        method="GET",
        base_url=base,
        headers=headers,
        path=f"survey-definitions/{survey_id}",
        log_event=False,
        params=params,
        timeout=60,
    )
    payload = resp.json().get("result")
    if not payload:
        raise ValueError(
            f"Survey definition for {survey_id} missing 'result' payload"
        )
    return payload


def generate_qsf(content: dict, filename: str) -> Path:
    """Save QSF content to a local file."""
    path = Path(filename)
    path.write_text(json.dumps(content, indent=2))
    return path.resolve()


# ============================================================================
# Helper functions for survey operations (Stage 0: QSYNC-XACCT-005)
# ============================================================================


def prepare_qsf_for_import(
    qsf_content: dict,
    new_name: str,
    *,
    language: str | None = None,
    status: str = "Inactive",
) -> dict:
    """Prepare QSF content for import by modifying survey metadata.

    Args:
        qsf_content: QSF payload from Qualtrics API
        new_name: New survey name
        language: Survey language code (e.g., 'EN'); if None, preserves existing or defaults to 'EN'
        status: Survey status ('Active' or 'Inactive')

    Returns:
        Modified QSF content (modifies in-place and returns for chaining)
    """
    if "SurveyEntry" not in qsf_content:
        return qsf_content

    entry = qsf_content["SurveyEntry"]
    entry["SurveyName"] = new_name
    entry["SurveyStatus"] = status
    entry.pop("SurveyID", None)  # Let Qualtrics assign new ID

    if language:
        entry["SurveyLanguage"] = language
    elif "SurveyLanguage" not in entry:
        entry["SurveyLanguage"] = "EN"

    return qsf_content


def upload_qsf_to_account(
    qsf_content: dict,
    new_name: str,
    base_url: str,
    headers: dict,
    *,
    action: str = "qsync.survey.import",
    log_meta: dict | None = None,
) -> str:
    """Upload QSF content to a Qualtrics account.

    Args:
        qsf_content: Prepared QSF payload
        new_name: Survey name for upload
        base_url: Target account base URL
        headers: Target account headers
        action: Log action identifier
        log_meta: Additional metadata for logging

    Returns:
        New survey ID

    Raises:
        RuntimeError: If upload fails or no survey ID returned
    """
    files = {
        "file": (
            "survey.qsf",
            json.dumps(qsf_content),
            "application/vnd.qualtrics.survey.qsf",
        ),
        "name": (None, new_name),
    }

    upload_headers = headers.copy()
    upload_headers.pop("Content-Type", None)

    resp = send_api_request(
        action=action,
        method="POST",
        base_url=base_url,
        headers=upload_headers,
        path="surveys",
        log_meta=log_meta or {},
        files=files,
        timeout=120,
    )

    result = resp.json().get("result", {})
    new_id = result.get("id")

    if not new_id:
        raise RuntimeError("Qualtrics did not return a new Survey ID")

    return new_id


def activate_survey(
    survey_id: str,
    base_url: str,
    headers: dict,
    *,
    active: bool = True,
    log_meta: dict | None = None,
) -> None:
    """Activate or deactivate a survey.

    Args:
        survey_id: Survey ID to activate/deactivate
        base_url: Account base URL
        headers: Account headers
        active: True to activate, False to deactivate
        log_meta: Additional metadata for logging

    Raises:
        RuntimeError: If activation fails
    """
    verb = "activate" if active else "deactivate"

    resp = send_api_request(
        action=f"qsync.survey.{verb}",
        method="PUT",
        base_url=base_url,
        headers=headers,
        path=f"surveys/{survey_id}",
        survey_id=survey_id,
        json={"isActive": active},
        log_meta=log_meta or {},
    )

    if not resp.ok:
        raise RuntimeError(f"Failed to {verb} survey: {resp.status_code} {resp.reason}")


# ============================================================================
# End of helper functions
# ============================================================================


def handle_list(args: argparse.Namespace) -> None:
    """List surveys visible to the configured Qualtrics account."""

    account = _resolve_account_from_args(args)
    base, headers = _get_client_config_for_args(args)

    print(f"Fetching surveys from {base}...")
    surveys = list_surveys(base, headers)
    if not account:
        surveys = _order_surveys_like_inventory(surveys)

    pattern_raw = (getattr(args, "name_pattern", "") or "").strip()
    matcher: re.Pattern[str] | None = None
    if pattern_raw:
        try:
            matcher = re.compile(pattern_raw, flags=re.IGNORECASE)
        except re.error as exc:
            print(f"ERROR: Invalid regex pattern {pattern_raw!r}: {exc}")
            sys.exit(2)

    def _print_table(title: str, rows: list[dict[str, Any]]) -> None:
        print(f"\n{title} ({len(rows)}):\n")
        print(f"{'Survey ID':<20} | {'Status':<10} | {'Created':<20} | {'Name'}")
        print("-" * 80)

        for survey in rows:
            sid = survey.get("id")
            name = survey.get("name")
            status = survey.get("isActive")
            created = survey.get("creationDate")
            status_str = "Active" if status else "Inactive"
            date_str = created[:10] if created else "N/A"

            print(f"{sid:<20} | {status_str:<10} | {date_str:<20} | {name}")

    if matcher is None:
        _print_table("Found surveys", surveys)
        return

    matched: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for survey in surveys:
        name = str(survey.get("name") or "")
        (matched if matcher.search(name) else unmatched).append(survey)

    print(f"\nApplied name regex (case-insensitive): {pattern_raw!r}")
    _print_table("Matched surveys", matched)
    _print_table("Unmatched surveys", unmatched)


def handle_copy(args: argparse.Namespace) -> None:
    """Copy/import a survey (from Qualtrics or a local QSF) into a new survey."""

    base, headers = _get_client_config_for_args(args)

    # Check if importing from local QSF file
    from_qsf = getattr(args, "from_qsf", None)
    if from_qsf:
        qsf_path = Path(from_qsf)
        if not qsf_path.exists():
            print(f"ERROR: QSF file not found: {qsf_path}")
            sys.exit(1)

        print(f"Loading QSF from {qsf_path}...")
        qsf_content = json.loads(qsf_path.read_text(encoding="utf-8"))
        source_id = qsf_content.get("SurveyEntry", {}).get("SurveyID", "local-qsf")

        new_name = args.name
        if not new_name:
            # Try to get name from QSF, or prompt
            default_name = qsf_content.get("SurveyEntry", {}).get("SurveyName", "")
            if default_name:
                new_name = (
                    input(f"Enter name for new survey [{default_name}]: ").strip()
                    or default_name
                )
            else:
                new_name = input("Enter name for new survey: ").strip()
            if not new_name:
                print("Name is required.")
                sys.exit(1)
    else:
        # Interactive Mode - fetch from Qualtrics
        source_id = _prompt_for_survey_id_api_if_needed(
            survey_id=getattr(args, "source_survey_id", None),
            args=args,
            message="Select a source survey to copy:",
        )

        new_name = args.name
        if not new_name:
            new_name = input("Enter name for new survey: ").strip()
            if not new_name:
                print("Name is required.")
                sys.exit(1)

        print(f"Fetching source definition (QSF) for {source_id}...")
        qsf_content = fetch_survey_definition(base, headers, source_id, fmt="qsf")

    # Check for duplicate names unless generating QSF only
    if not args.generate_qsf and not _resolve_account_from_args(args):
        ensure_unique_survey_name(new_name, allow_duplicate=args.force_duplicate)

    # Determine target language
    target_lang = None
    if args.language:
        target_lang = args.language
    elif (
        "SurveyEntry" in qsf_content and "SurveyLanguage" in qsf_content["SurveyEntry"]
    ):
        target_lang = qsf_content["SurveyEntry"]["SurveyLanguage"]

    # Prepare QSF for import using helper (Stage 0)
    prepare_qsf_for_import(
        qsf_content, new_name, language=target_lang, status="Inactive"
    )

    # Handle project category if specified
    if args.project_category and "SurveyElements" in qsf_content:
        for elem in qsf_content["SurveyElements"]:
            if elem.get("Element") == "PROJ":
                elem["Payload"]["ProjectCategory"] = args.project_category
                elem["PrimaryAttribute"] = args.project_category

    # Generate QSF file only if requested
    if args.generate_qsf:
        print(f"Generating QSF file for '{new_name}'...")
        qsf_path = generate_qsf(qsf_content, new_name)
        print(f"Successfully generated QSF: {qsf_path}")
        return

    print(f"Creating survey '{new_name}'...")

    try:
        # Upload using helper (Stage 0)
        meta = {"source_survey_id": source_id, "new_name": new_name}
        if from_qsf:
            meta["from_qsf"] = str(qsf_path)

        new_id = upload_qsf_to_account(
            qsf_content,
            new_name,
            base,
            headers,
            action="qsync.survey.copy",
            log_meta=meta,
        )

        print(f"Successfully copied {source_id} to {new_name} ({new_id})")

        from .terminal_output import log_confirmation

        log_confirmation("[copy]")

        edit_url = f"https://{base}/survey-builder/{new_id}/edit"
        print(f"\nEdit Link: {edit_url}\n")

    except Exception as e:
        print(f"\nERROR: API Copy failed: {e}")
        sys.exit(1)


def handle_slice_language(args: argparse.Namespace) -> None:
    """Slice a multilingual survey into a new survey rebased to one language."""

    from .terminal_output import header, info, success, warn, error, dim, prompt_yes_no
    from .translations_utils import normalize_language_code, normalize_language_list
    from .survey_slice_language import (
        apply_fallback_translations,
        compute_slice_coverage,
        resolve_keep_languages,
        slice_qsf_to_language,
        sha256_of_json,
        warn_if_flow_text_present,
        write_coverage_report,
        write_dry_run_qsf,
        write_slice_manifest,
        write_split_baseline_snapshot,
        write_batch_manifest,
        sha256_of_qsf_upload_bytes,
    )
    from .translation_export import build_translation_map_from_cache

    import qsync
    import copy

    base, headers = _get_client_config_for_args(args)

    from .dimensions.translations_core import (
        list_enabled_languages as api_list_enabled_languages,
    )

    source_id = _prompt_for_survey_id_api_if_needed(
        survey_id=getattr(args, "source_survey_id", None),
        args=args,
        message="Select a source survey to slice:",
    )

    raw_targets: list[str] = []
    raw_csv = getattr(args, "languages", None)
    if raw_csv:
        raw_targets.extend([s.strip() for s in str(raw_csv).split(",") if s.strip()])
    raw_single = getattr(args, "language", None)
    if raw_single:
        raw_targets.append(str(raw_single).strip())

    target_langs = normalize_language_list(raw_targets)
    if not target_langs:
        error(
            "[qsync:slice-language]",
            "ERROR: --language or --languages is required (e.g. --language DE or --languages DE,FR-CA).",
        )
        sys.exit(1)

    multi = len(target_langs) > 1
    keep_raw = str(
        getattr(args, "keep_languages", "target-only") or "target-only"
    ).strip()
    allow_incomplete = bool(getattr(args, "allow_incomplete", False))
    allow_fallback = bool(getattr(args, "allow_fallback", False))
    dry_run = bool(getattr(args, "dry_run", False))
    verify_parity = bool(getattr(args, "verify_parity", False))
    auto_yes = bool(getattr(args, "yes", False))
    no_flow_text = bool(getattr(args, "no_flow_text", False))

    header("[qsync:slice-language]", "Preview:")

    try:
        enabled_langs = api_list_enabled_languages(
            source_id, base_url=base, headers=headers
        )
    except TypeError:
        # Backward-compat path for patched/mocked call sites that still expose
        # the legacy one-arg signature (survey_id only).
        enabled_langs = api_list_enabled_languages(source_id)
    except Exception as exc:
        error(
            "[qsync:slice-language]",
            f"ERROR: Failed to fetch enabled languages for {source_id}: {exc}",
        )
        sys.exit(1)

    enabled_set = set(normalize_language_list(enabled_langs))
    missing_targets = [lang for lang in target_langs if lang not in enabled_set]
    if missing_targets:
        error(
            "[qsync:slice-language]",
            "ERROR: Target language(s) not enabled on source survey "
            f"{source_id}: {', '.join(missing_targets)}.",
        )
        dim(
            "[qsync:slice-language]",
            f"Enabled languages: {', '.join(enabled_langs) if enabled_langs else '(none)'}",
        )
        sys.exit(1)

    print()
    info("  Source Survey:", "")
    info("    ID:", source_id)
    info("    Account:", base)
    if multi:
        info("  Targets:", "")
        info("    Base languages:", ", ".join(target_langs))
    else:
        info("  Target:", "")
        info("    Base language:", target_langs[0])
    info("    Keep languages:", keep_raw)
    if allow_incomplete:
        warn("    ⚠", "--allow-incomplete enabled (may produce mixed-language output)")
    if allow_fallback:
        warn("    ⚠", "--allow-fallback enabled (fills gaps from base language)")
    if no_flow_text:
        warn("    ⚠", "--no-flow-text enabled (SurveyFlow text left as-is)")
    if dry_run:
        dim("    Dry run:", "write rebased QSF only (no import)")
    if verify_parity:
        dim("    Verify parity:", "enabled (post-create check)")
    if auto_yes:
        dim("    (non-interactive):", "--yes")
    print()

    info(
        "[qsync:slice-language]", f"Fetching source definition (QSF) for {source_id}..."
    )
    try:
        qsf_content = fetch_survey_definition(base, headers, source_id, fmt="qsf")
    except Exception as exc:
        error(
            "[qsync:slice-language]",
            f"ERROR: Failed to fetch source survey definition: {exc}",
        )
        sys.exit(1)

    source_definition_for_manifest: dict[str, Any] = {}
    try:
        source_definition_for_manifest = fetch_survey_definition(
            base,
            headers,
            source_id,
            fmt="json",
        )
    except Exception as exc:
        warn(
            "[qsync:slice-language]",
            (
                "Could not fetch source survey-definition JSON for split manifest "
                f"baseline capture ({exc}). Manifest fingerprints will be empty."
            ),
        )

    source_name = str(qsf_content.get("SurveyEntry", {}).get("SurveyName") or source_id)
    name_template = (getattr(args, "name", None) or "").strip()

    def _apply_name_template(template: str, lang: str) -> str:
        return (
            template.replace("{lang}", lang.lower())
            .replace("{LANG}", lang.upper())
            .strip()
        )

    def _resolve_new_name(lang: str) -> str:
        lang_norm = normalize_language_code(lang)
        if name_template:
            if "{lang}" in name_template or "{LANG}" in name_template:
                return _apply_name_template(name_template, lang_norm)
            if multi:
                return f"{name_template} ({lang_norm})"
            return name_template
        default_name = f"{source_name} ({lang_norm})"
        if multi:
            return default_name
        if auto_yes or not sys.stdin.isatty():
            return default_name
        return (
            input(f"Enter name for new survey [{default_name}]: ").strip()
            or default_name
        )

    root = _workspace_root()

    reports: dict[str, Any] = {}
    coverage_paths: dict[str, Path] = {}

    # Coverage preflight + report (always written to disk for traceability).
    for target_lang in target_langs:
        try:
            report = compute_slice_coverage(qsf_content, target_language=target_lang)
        except Exception as exc:
            error(
                "[qsync:slice-language]",
                f"ERROR: Coverage preflight failed to run for {target_lang}: {exc}",
            )
            sys.exit(1)

        reports[target_lang] = report
        coverage_path = write_coverage_report(
            root,
            source_survey_id=source_id,
            target_language=target_lang,
            report=report,
            source_survey_name=source_name,
        )
        coverage_paths[target_lang] = coverage_path

        if report.inactive_qids_total is not None and report.inactive_qids_total > 0:
            dim(
                "[qsync:slice-language]",
                (
                    f"Coverage scope: {report.active_qids_total} active QID(s); "
                    f"skipped {report.inactive_qids_total} inactive/Trash QID(s)."
                ),
            )

        if report.missing_required_total:
            prefix = f"[{target_lang}] " if multi else ""
            warn(
                "[qsync:slice-language]",
                (
                    f"{prefix}Coverage: {report.ok_required_total}/{report.required_total} required keys "
                    f"({report.pct_required_ok:.1f}%), missing {report.missing_required_total}."
                ),
            )
            shown = 0
            order = [
                "Meta",
                "QuestionText",
                "SubQuestion",
                "ChoiceGroup",
                "Choice",
                "Answer",
                "Label",
            ]
            for type_name in order:
                keys = (report.missing_required_by_type or {}).get(type_name) or []
                if not keys:
                    continue
                remaining = max(0, 10 - shown)
                if remaining <= 0:
                    break
                sample = keys[:remaining]
                shown += len(sample)
                warn(
                    "[qsync:slice-language]",
                    f"{prefix}Missing {type_name} (sample): " + ", ".join(sample),
                )
            if not shown:
                missing_sample = report.missing_required[:10]
                if missing_sample:
                    warn(
                        "[qsync:slice-language]",
                        f"{prefix}Missing keys (sample): " + ", ".join(missing_sample),
                    )
            dim("[qsync:slice-language]", f"Full report: {coverage_path}")
            dim(
                "[qsync:slice-language]",
                f"Remediation: qsync translations doctor --survey-id {source_id} --language {target_lang}",
            )

            if not allow_fallback and not allow_incomplete:
                sys.exit(1)

            if not allow_fallback and not auto_yes:
                if not sys.stdin.isatty():
                    error(
                        "[qsync:slice-language]",
                        "ERROR: Non-interactive run with --allow-incomplete requires --yes.",
                    )
                    sys.exit(1)
                if not prompt_yes_no(
                    f"Target language {target_lang} is incomplete. Create the sliced survey anyway?",
                    default=False,
                ):
                    raise SystemExit("[qsync:slice-language] Aborted by user.")

            if not allow_fallback:
                warn(
                    "[qsync:slice-language]",
                    "Proceeding despite incomplete coverage (--allow-incomplete).",
                )

    if no_flow_text:
        for line in warn_if_flow_text_present(qsf_content):
            warn("[qsync:slice-language]", line)

    batch_entries: list[dict[str, Any]] = []

    for idx, target_lang in enumerate(target_langs, start=1):
        if multi:
            header(
                "[qsync:slice-language]",
                f"Language {target_lang} ({idx}/{len(target_langs)})",
            )

        report = reports[target_lang]
        coverage_path = coverage_paths[target_lang]
        new_name = _resolve_new_name(target_lang)
        fallback_filled: list[str] = []

        qsf_working = copy.deepcopy(qsf_content)

        if allow_fallback and report.missing_required_total:
            fallback_filled = apply_fallback_translations(
                qsf_working,
                target_language=target_lang,
                missing_keys=report.missing_required,
            )
            warn(
                "[qsync:slice-language]",
                f"Applied fallback for {len(fallback_filled)} key(s) from base language.",
            )

        # Resolve keep-languages and validate the request.
        kept_languages = resolve_keep_languages(
            enabled_langs,
            target_language=target_lang,
            base_language=report.base_language,
            keep_languages_raw=keep_raw,
        )
        missing_kept = [lang for lang in kept_languages if lang not in enabled_set]
        if missing_kept:
            error(
                "[qsync:slice-language]",
                "ERROR: --keep-languages includes languages not enabled on the source: "
                + ", ".join(missing_kept),
            )
            dim(
                "[qsync:slice-language]",
                f"Enabled languages: {', '.join(enabled_langs) if enabled_langs else '(none)'}",
            )
            sys.exit(1)

        info(
            "[qsync:slice-language]",
            "Slicing with kept languages: " + ", ".join(kept_languages),
        )

        # Apply the in-place QSF transform.
        transform = slice_qsf_to_language(
            qsf_working,
            target_language=target_lang,
            kept_languages=kept_languages,
            rebase_flow_text=not no_flow_text,
        )
        if transform.warnings:
            warn(
                "[qsync:slice-language]",
                f"{len(transform.warnings)} warning(s) during rebase (showing up to 10):",
            )
            for line in transform.warnings[:10]:
                warn("[qsync:slice-language]", line)

        if dry_run:
            prepare_qsf_for_import(
                qsf_working,
                new_name,
                language=target_lang,
                status="Inactive",
            )
            qsf_sha256 = sha256_of_qsf_upload_bytes(qsf_working)
            dry_path = write_dry_run_qsf(
                root,
                source_survey_id=source_id,
                target_language=target_lang,
                qsf=qsf_working,
                source_survey_name=source_name,
            )
            success(
                "[qsync:slice-language]",
                f"Dry run complete. Wrote rebased QSF: {dry_path}",
            )
            dim("[qsync:slice-language]", f"QSF SHA256: {qsf_sha256}")
            if not multi:
                return
            batch_entries.append(
                {
                    "target_language": target_lang,
                    "new_survey_id": None,
                    "new_survey_name": new_name,
                    "kept_languages": kept_languages,
                    "allow_incomplete": allow_incomplete,
                    "allow_fallback": allow_fallback,
                    "fallback_filled_total": len(fallback_filled),
                    "fallback_filled_sample": fallback_filled[:10],
                    "coverage_report_path": str(coverage_path),
                    "dry_run_qsf_path": str(dry_path),
                    "qsf_sha256": qsf_sha256,
                }
            )
            continue

        # Check for duplicate names against local inventory (default-account only).
        # For alternate accounts (`--account` in menu flows), local inventory is not
        # guaranteed to match remote state, so skip this preflight.
        if not _resolve_account_from_args(args):
            try:
                ensure_unique_survey_name(new_name, allow_duplicate=args.force_duplicate)
            except Exception as exc:
                error("[qsync:slice-language]", f"ERROR: {exc}")
                sys.exit(1)

        # Prepare for import (sets name/status; strips SurveyID).
        prepare_qsf_for_import(
            qsf_working,
            new_name,
            language=target_lang,
            status="Inactive",
        )
        qsf_sha256 = sha256_of_qsf_upload_bytes(qsf_working)

        info("[qsync:slice-language]", f"Creating survey '{new_name}'...")
        try:
            meta = {
                "source_survey_id": source_id,
                "target_language": target_lang,
                "keep_languages": keep_raw,
                "allow_incomplete": allow_incomplete,
            }
            new_id = upload_qsf_to_account(
                qsf_working,
                new_name,
                base,
                headers,
                action="qsync.survey.slice_language",
                log_meta=meta,
            )
        except Exception as exc:
            error("[qsync:slice-language]", f"ERROR: Failed to create survey: {exc}")
            sys.exit(1)

        success(
            "[qsync:slice-language]",
            f"Created {new_name} ({new_id}) from {source_id} (base={target_lang}).",
        )

        edit_url = f"https://{base}/survey-builder/{new_id}/edit"
        print()
        info("Edit Link:", edit_url)

        # Persist manifest for traceability.
        canonical_translation_fingerprint = ""
        baseline_snapshot_ref = ""
        if source_definition_for_manifest:
            try:
                projection = build_translation_map_from_cache(
                    source_definition_for_manifest,
                    language=target_lang,
                    base_language=report.base_language,
                )
                canonical_translation_fingerprint = sha256_of_json(projection)
                baseline_snapshot = write_split_baseline_snapshot(
                    root,
                    source_survey_id=source_id,
                    source_survey_name=str(source_name or source_id),
                    target_language=target_lang,
                    canonical_definition=source_definition_for_manifest,
                    canonical_translation_projection=projection,
                )
                baseline_snapshot_ref = str(baseline_snapshot)
            except Exception as exc:
                warn(
                    "[qsync:slice-language]",
                    f"Failed to write split baseline snapshot for {target_lang}: {exc}",
                )

        manifest_path = write_slice_manifest(
            root,
            source_survey_id=source_id,
            source_survey_name=str(source_name or source_id),
            source_base_language=report.base_language,
            target_language=target_lang,
            new_survey_id=new_id,
            new_survey_name=new_name,
            keep_languages_mode=keep_raw,
            kept_languages=kept_languages,
            allow_incomplete=allow_incomplete,
            allow_fallback=allow_fallback,
            fallback_filled_total=len(fallback_filled),
            fallback_filled_sample=fallback_filled[:10],
            coverage_report_path=coverage_path,
            report=report,
            qsf_sha256=qsf_sha256,
            qsync_version=str(getattr(qsync, "__version__", "0.0.0")),
            canonical_translation_fingerprint=canonical_translation_fingerprint,
            baseline_snapshot_ref=baseline_snapshot_ref,
        )
        dim("[qsync:slice-language]", f"Manifest: {manifest_path}")
        info("[qsync:slice-language]", "Next commands:")
        info(None, f"  qsync survey activate {new_id}")
        info(
            None,
            (
                "  qsync survey publish "
                f'{new_id} --description "slice-language {source_id} -> {target_lang}"'
            ),
        )
        info(None, f"  qsync items pull --survey-id {new_id}")
        info(
            None,
            f"  qsync translations pull --survey-id {new_id}  # cache refresh only",
        )

        if verify_parity:
            from .survey_deep_parity import compare_survey_definition_deep_parity

            info(
                "[qsync:slice-language]",
                "Running deep parity check (profile=split; best-effort)...",
            )
            try:
                source_def = source_definition_for_manifest or fetch_survey_definition(
                    base, headers, source_id, fmt="json"
                )
                target_def = fetch_survey_definition(base, headers, new_id, fmt="json")
                report_deep = compare_survey_definition_deep_parity(
                    source_def,
                    target_def,
                    survey_a=source_id,
                    survey_b=new_id,
                    write_artifacts_on_mismatch=True,
                    profile="split",
                    manifest_path=manifest_path,
                )
                ok = _emit_deep_parity_report(
                    report=report_deep,
                    prefix="[qsync:slice-language:parity]",
                )
                if not ok:
                    raise SystemExit(
                        "[qsync:slice-language] Deep parity check failed; see details above."
                    )
            except Exception as exc:
                warn(
                    "[qsync:slice-language]",
                    f"Deep parity check failed to run: {exc}",
                )

        batch_entries.append(
            {
                "target_language": target_lang,
                "new_survey_id": new_id,
                "new_survey_name": new_name,
                "kept_languages": kept_languages,
                "allow_incomplete": allow_incomplete,
                "allow_fallback": allow_fallback,
                "fallback_filled_total": len(fallback_filled),
                "fallback_filled_sample": fallback_filled[:10],
                "coverage_report_path": str(coverage_path),
                "manifest_path": str(manifest_path),
                "qsf_sha256": qsf_sha256,
            }
        )

    if multi:
        batch_path = write_batch_manifest(
            root,
            source_survey_id=source_id,
            source_survey_name=str(source_name or source_id),
            source_base_language=reports[target_langs[0]].base_language,
            slices=batch_entries,
            qsync_version=str(getattr(qsync, "__version__", "0.0.0")),
        )
        dim("[qsync:slice-language]", f"Batch manifest: {batch_path}")

    from .terminal_output import log_confirmation

    log_confirmation("[slice-language]")


def handle_slice_registry(args: argparse.Namespace) -> None:
    """List local slice manifests and derived surveys."""

    from .terminal_output import info, warn, dim

    root = _workspace_root()
    slices_dir = resolve_scoped_dir("surveys", root=root) / "slices"
    if not slices_dir.exists():
        info("[qsync:slice-registry]", "No slices directory found.")
        return

    source_filter = str(getattr(args, "source", "") or "").strip()
    limit = getattr(args, "limit", None)
    open_links = bool(getattr(args, "open", False))

    entries: list[dict[str, Any]] = []
    for path in sorted(slices_dir.glob("*.json")):
        name = path.name
        if name.startswith(("coverage__", "dryrun__", "batch__")):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            warn(
                "[qsync:slice-registry]",
                f"Skipping unreadable manifest {path.name}: {exc}",
            )
            continue
        if not isinstance(data, dict):
            continue
        if source_filter and str(data.get("source_survey_id") or "") != source_filter:
            continue
        new_id = str(data.get("new_survey_id") or "").strip()
        if not new_id:
            continue
        entries.append(
            {
                "source_survey_id": str(data.get("source_survey_id") or ""),
                "source_survey_name": str(data.get("source_survey_name") or ""),
                "target_language": str(data.get("target_language") or ""),
                "new_survey_id": new_id,
                "new_survey_name": str(data.get("new_survey_name") or ""),
                "created_at_utc": str(data.get("created_at_utc") or ""),
                "manifest_path": str(path),
            }
        )

    if not entries:
        info("[qsync:slice-registry]", "No slice manifests found.")
        return

    entries.sort(key=lambda e: e.get("created_at_utc") or "")
    if isinstance(limit, int) and limit > 0:
        entries = entries[:limit]

    info("[qsync:slice-registry]", f"Found {len(entries)} slice(s).")

    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        grouped.setdefault(entry["source_survey_id"], []).append(entry)

    for source_id, group in grouped.items():
        source_name = (group[0].get("source_survey_name") or "").strip()
        label = f"Source {source_id}"
        if source_name:
            label = f"{label} ({source_name})"
        info("[qsync:slice-registry]", label)
        for entry in group:
            created = entry.get("created_at_utc") or ""
            lang = entry.get("target_language") or ""
            new_id = entry.get("new_survey_id") or ""
            new_name = entry.get("new_survey_name") or ""
            line = f"  - {created} {lang} -> {new_id}"
            if new_name:
                line += f" ({new_name})"
            info(None, line)
            dim(None, f"    Manifest: {entry.get('manifest_path')}")

    if open_links:
        try:
            base, _headers = get_client_config()
            edit_urls = [
                f"https://{base}/survey-builder/{entry['new_survey_id']}/edit"
                for entry in entries
                if entry.get("new_survey_id")
            ]
            if not edit_urls:
                return
            import subprocess

            for url in edit_urls:
                if sys.platform == "darwin":
                    subprocess.run(["open", url], check=False)
                elif os.name == "nt":
                    os.startfile(url)  # type: ignore[attr-defined]
                else:
                    subprocess.run(["xdg-open", url], check=False)
        except Exception as exc:
            warn("[qsync:slice-registry]", f"Could not open edit links ({exc}).")


def handle_export_side_by_side(args: argparse.Namespace) -> None:
    """Export two surveys side-by-side into a single DOCX."""

    from .terminal_output import info, error, success, warn
    from .translation_export import export_surveys_side_by_side_docx
    from .survey_parity import compare_qsf_parity
    from .interactive_menu import is_interactive

    survey_a = str(getattr(args, "source_id", "") or "").strip()
    survey_b = str(getattr(args, "target_id", "") or "").strip()
    if not survey_a or not survey_b:
        error(
            "[qsync:export-side-by-side]",
            "ERROR: --source-id and --target-id are required.",
        )
        sys.exit(1)

    output = getattr(args, "output", None)
    label_a = getattr(args, "label_a", None)
    label_b = getattr(args, "label_b", None)
    refresh = bool(getattr(args, "refresh", False))
    smart_name = bool(getattr(args, "smart_name", False))
    no_html = bool(getattr(args, "no_html", False))
    layout_heuristics = bool(getattr(args, "layout_heuristics", False))
    skip_parity = bool(getattr(args, "skip_parity", False))
    skip_js_strings = bool(getattr(args, "skip_js_strings", False))
    do_open = bool(getattr(args, "open", False))

    if not skip_parity:
        info("[qsync:export-side-by-side]", "Running parity check...")
        try:
            base, headers = get_client_config()
            qsf_a = fetch_survey_definition(base, headers, survey_a, fmt="qsf")
            qsf_b = fetch_survey_definition(base, headers, survey_b, fmt="qsf")
            parity = compare_qsf_parity(qsf_a, qsf_b)
            ok = _emit_parity_report(
                result=parity,
                survey_a=survey_a,
                survey_b=survey_b,
                prefix="[qsync:export-side-by-side:parity]",
            )
            if not ok:
                raise SystemExit(
                    "[qsync:export-side-by-side] Parity check failed; see details above."
                )
        except Exception as exc:
            error("[qsync:export-side-by-side]", f"ERROR: {exc}")
            sys.exit(1)
    else:
        warn("[qsync:export-side-by-side]", "Skipping parity check (--skip-parity).")

    try:
        path = export_surveys_side_by_side_docx(
            survey_a,
            survey_b,
            output_path=output,
            label_a=label_a,
            label_b=label_b,
            smart_name=smart_name,
            refresh=refresh,
            include_html_source=not no_html,
            layout_heuristics=layout_heuristics,
            include_js_strings=not skip_js_strings,
            interactive=is_interactive(),
        )
    except Exception as exc:
        error("[qsync:export-side-by-side]", f"ERROR: {exc}")
        sys.exit(1)

    success("[qsync:export-side-by-side]", f"Exported: {path}")

    if do_open:
        try:
            import subprocess

            if sys.platform == "darwin":
                subprocess.run(["open", str(path)], check=False)
            elif os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                subprocess.run(["xdg-open", str(path)], check=False)
        except Exception:
            warn(
                "[qsync:export-side-by-side]",
                "Could not open document automatically.",
            )


def _emit_parity_report(*, result, survey_a: str, survey_b: str, prefix: str) -> bool:
    from .terminal_output import success, warn, dim

    def _sample(items: list[str], limit: int = 10) -> str:
        return ", ".join(items[:limit])

    def _first_diff(a: list[str], b: list[str]) -> str | None:
        for idx, (left, right) in enumerate(zip(a, b)):
            if left != right:
                return f"idx {idx}: {left} != {right}"
        if len(a) != len(b):
            return f"length {len(a)} != {len(b)}"
        return None

    if result.warnings:
        warn(prefix, f"{len(result.warnings)} warning(s) during flow parsing:")
        for line in result.warnings[:5]:
            warn(prefix, line)

    if result.qids_match:
        success(prefix, "QID set match.")
    else:
        warn(
            prefix,
            f"QID set mismatch: +{len(result.qids_only_in_b)} in {survey_b}, "
            f"+{len(result.qids_only_in_a)} in {survey_a}.",
        )
        if result.qids_only_in_a:
            warn(prefix, f"Only in {survey_a}: {_sample(result.qids_only_in_a)}")
        if result.qids_only_in_b:
            warn(prefix, f"Only in {survey_b}: {_sample(result.qids_only_in_b)}")

    if result.flow_types_match:
        success(prefix, f"SurveyFlow types match (len={len(result.flow_types_a)}).")
    else:
        diff = _first_diff(result.flow_types_a, result.flow_types_b)
        warn(
            prefix,
            f"SurveyFlow types differ (len {len(result.flow_types_a)} vs {len(result.flow_types_b)}).",
        )
        if diff:
            warn(prefix, f"First type diff: {diff}")

    if result.flow_qids_match:
        success(prefix, f"SurveyFlow QID order match (len={len(result.flow_qids_a)}).")
    else:
        diff = _first_diff(result.flow_qids_a, result.flow_qids_b)
        warn(
            prefix,
            f"SurveyFlow QID order differs (len {len(result.flow_qids_a)} vs {len(result.flow_qids_b)}).",
        )
        if diff:
            warn(prefix, f"First QID diff: {diff}")

    if result.block_memberships_match:
        success(prefix, "Block memberships match (best-effort).")
    else:
        warn(
            prefix,
            f"Block membership mismatch: +{len(result.block_memberships_only_in_b)} in {survey_b}, "
            f"+{len(result.block_memberships_only_in_a)} in {survey_a}.",
        )
        if result.block_memberships_only_in_a:
            warn(
                prefix,
                f"Only in {survey_a} (sample): {result.block_memberships_only_in_a[:3]}",
            )
        if result.block_memberships_only_in_b:
            warn(
                prefix,
                f"Only in {survey_b} (sample): {result.block_memberships_only_in_b[:3]}",
            )

    if result.tags_match:
        success(prefix, "DataExportTag set match.")
    else:
        warn(
            prefix,
            f"DataExportTag mismatch: +{len(result.tags_only_in_b)} in {survey_b}, "
            f"+{len(result.tags_only_in_a)} in {survey_a}.",
        )
        if result.tags_only_in_a:
            warn(prefix, f"Only in {survey_a}: {_sample(result.tags_only_in_a)}")
        if result.tags_only_in_b:
            warn(prefix, f"Only in {survey_b}: {_sample(result.tags_only_in_b)}")

    dim(
        prefix,
        "Out of scope: quotas/response sets/scoring and account-specific metadata.",
    )
    return bool(result.ok)


def _emit_deep_parity_report(*, report, prefix: str) -> bool:
    from .terminal_output import success, warn, dim
    from .dimensions.flow_diff import format_diff_for_display, format_diff_summary

    if report.ok:
        success(
            prefix,
            f"Deep parity OK (profile={report.profile}, hash={report.hash_a[:12]}).",
        )
        return True

    warn(
        prefix,
        f"Deep parity FAILED (profile={report.profile}; normalized comparison mismatch).",
    )
    if report.gate_results:
        gate_order = (
            "structural",
            "translation",
            "language_policy",
            "operational_policy",
        )
        gate_parts: list[str] = []
        ordered_states: list[tuple[str, bool]] = []
        seen: set[str] = set()
        for name in gate_order:
            if name not in report.gate_results:
                continue
            seen.add(name)
            state = bool(report.gate_results.get(name))
            gate_parts.append(f"{name}={'ok' if state else 'fail'}")
            ordered_states.append((name, state))
        for name in sorted(report.gate_results.keys()):
            if name in seen:
                continue
            state = bool(report.gate_results.get(name))
            gate_parts.append(f"{name}={'ok' if state else 'fail'}")
            ordered_states.append((name, state))
        warn(prefix, "Gates: " + ", ".join(gate_parts))
        if report.profile == "split":
            dim(prefix, "Split dimension summary:")
            guidance = {
                "structural": "Review hard-fail structural paths and flow semantic diff.",
                "translation": "Use translation notes to locate key mismatches and missing text.",
                "language_policy": "Check target language + keep_languages policy in manifest.",
                "operational_policy": "Validate country/redirect/EOS policy in manifest.",
            }
            for name, state in ordered_states:
                marker = "OK" if state else "FAIL"
                dim(prefix, f"  [{marker}] {name}")
                if not state:
                    hint = guidance.get(name)
                    if hint:
                        dim(prefix, f"       next: {hint}")

    if report.section_counts:
        parts = [
            f"{k}={v}"
            for k, v in sorted(
                report.section_counts.items(),
                key=lambda kv: (-kv[1], kv[0]),
            )
            if v
        ]
        if parts:
            warn(prefix, f"Diff sections: {', '.join(parts)}")
    warn(prefix, f"Diff count: {report.diff_count}")

    if report.hard_fail_paths:
        warn(prefix, f"Hard-fail paths ({len(report.hard_fail_paths)}):")
        for item in report.hard_fail_paths[:30]:
            warn(prefix, f"  - {item}")
        if len(report.hard_fail_paths) > 30:
            dim(
                prefix,
                f"(hard-fail paths truncated; showing 30 of {len(report.hard_fail_paths)})",
            )

    if report.allowed_by_policy_paths:
        dim(
            prefix,
            f"Allowed-by-policy paths: {len(report.allowed_by_policy_paths)}",
        )
        for item in report.allowed_by_policy_paths[:15]:
            dim(prefix, f"  ~ {item}")
        if len(report.allowed_by_policy_paths) > 15:
            dim(
                prefix,
                f"(allowed paths truncated; showing 15 of {len(report.allowed_by_policy_paths)})",
            )

    if report.warning_paths:
        warn(prefix, f"Warnings ({len(report.warning_paths)}):")
        for item in report.warning_paths[:20]:
            warn(prefix, f"  - {item}")
        if len(report.warning_paths) > 20:
            dim(
                prefix,
                f"(warning paths truncated; showing 20 of {len(report.warning_paths)})",
            )

    if report.policy_notes:
        warn(prefix, f"Policy notes ({len(report.policy_notes)}):")
        for note in report.policy_notes[:20]:
            warn(prefix, f"  - {note}")
        if len(report.policy_notes) > 20:
            dim(
                prefix,
                f"(policy notes truncated; showing 20 of {len(report.policy_notes)})",
            )

    if report.diff_paths:
        hard_fail_set = {str(item).strip() for item in report.hard_fail_paths}
        sampled = [p for p in report.diff_paths if str(p).strip() not in hard_fail_set]
        if not sampled and hard_fail_set:
            dim(prefix, "Diff sample omitted (already covered by hard-fail paths).")
            sampled = []
        elif not sampled:
            sampled = list(report.diff_paths)
        if sampled:
            warn(prefix, "Diff paths (sample):")
            for p in sampled[:50]:
                warn(prefix, f"  - {p}")

    if report.flow_changes:
        warn(prefix, f"SurveyFlow: {format_diff_summary(report.flow_changes)}")
        for line in format_diff_for_display(report.flow_changes, verbose=False)[:25]:
            dim(prefix, line)
        if len(report.flow_changes) > 25:
            dim(
                prefix,
                f"(flow diffs truncated; showing 25 of {len(report.flow_changes)})",
            )

    if report.manifest_path:
        dim(prefix, f"Manifest: {report.manifest_path}")

    if report.artifacts:
        dim(
            prefix,
            f"Artifacts: a={report.artifacts.get('a')} b={report.artifacts.get('b')}",
        )
        if report.artifacts.get("diff"):
            dim(prefix, f"Unified diff: {report.artifacts.get('diff')}")

    if getattr(report, "timings_ms", None):
        timings = ", ".join(
            f"{name}={value:.1f}ms"
            for name, value in sorted(report.timings_ms.items())
        )
        dim(prefix, f"Timings: {timings}")

    return False


def handle_parity_check(args: argparse.Namespace) -> None:
    """Compare two surveys for parity (QSF-lite by default; deep via --deep)."""

    from .terminal_output import header, info, error

    base, headers = _get_client_config_for_args(args)

    survey_a = getattr(args, "source_id", None) or ""
    survey_b = getattr(args, "target_id", None) or ""
    deep = bool(getattr(args, "deep", False))
    split_alias = bool(getattr(args, "split_profile", False))
    profile = str(getattr(args, "profile", "cross_account") or "cross_account")
    manifest_path = getattr(args, "manifest", None)
    if split_alias:
        profile = "split"
    if not survey_a or not survey_b:
        error("[qsync:parity-check]", "ERROR: --source-id and --target-id are required.")
        sys.exit(1)
    if (split_alias or profile != "cross_account" or manifest_path) and not deep:
        error(
            "[qsync:parity-check]",
            "ERROR: --profile/--split/--manifest require --deep.",
        )
        sys.exit(1)

    if deep:
        from .survey_deep_parity import compare_survey_definition_deep_parity

        header("[qsync:parity-check]", "Fetching survey definitions (JSON)...")
        try:
            def_a = fetch_survey_definition(base, headers, survey_a, fmt="json")
            def_b = fetch_survey_definition(base, headers, survey_b, fmt="json")
        except Exception as exc:
            error("[qsync:parity-check]", f"ERROR: Failed to fetch definitions: {exc}")
            sys.exit(1)

        info(
            "[qsync:parity-check]",
            f"Deep comparing {survey_a} vs {survey_b} (profile={profile})...",
        )
        report = compare_survey_definition_deep_parity(
            def_a,
            def_b,
            survey_a=survey_a,
            survey_b=survey_b,
            write_artifacts_on_mismatch=True,
            profile=profile,
            manifest_path=manifest_path,
        )
        if _emit_deep_parity_report(report=report, prefix="[qsync:parity-check]"):
            return

        sys.exit(2)

    from .survey_parity import compare_qsf_parity

    header("[qsync:parity-check]", "Fetching survey definitions (QSF)...")
    try:
        qsf_a = fetch_survey_definition(base, headers, survey_a, fmt="qsf")
        qsf_b = fetch_survey_definition(base, headers, survey_b, fmt="qsf")
    except Exception as exc:
        error("[qsync:parity-check]", f"ERROR: Failed to fetch definitions: {exc}")
        sys.exit(1)

    info("[qsync:parity-check]", f"Comparing {survey_a} vs {survey_b}...")
    report = compare_qsf_parity(qsf_a, qsf_b)
    ok = _emit_parity_report(
        result=report,
        survey_a=survey_a,
        survey_b=survey_b,
        prefix="[qsync:parity-check]",
    )
    if not ok:
        sys.exit(2)


def handle_rename(args: argparse.Namespace) -> None:
    """Rename a survey by SurveyID (or interactively select from recent surveys)."""

    base, headers = _get_client_config_for_args(args)

    survey_id = args.survey_id
    old_name = "Unknown"

    if not survey_id:
        print("Fetching available surveys...")
        surveys = list_surveys(base, headers)
        surveys.sort(key=lambda x: x.get("creationDate", ""), reverse=True)

        print("\nAvailable Surveys:")
        print(f"{'#':<4} | {'Name':<40} | {'ID':<20} | {'Created'}")
        print("-" * 80)
        for i, sv in enumerate(surveys):
            created = sv.get("creationDate", "")[:10]
            print(
                f"{i+1:<4} | {sv.get('name', 'N/A')[:40]:<40} | {sv.get('id'):<20} | {created}"
            )

        while True:
            try:
                choice = input("\nSelect survey # to rename (or 'q' to quit): ").strip()
                if choice.lower() == "q":
                    return
                idx = int(choice) - 1
                if 0 <= idx < len(surveys):
                    selected = surveys[idx]
                    survey_id = selected.get("id")
                    old_name = selected.get("name")
                    print(f"Selected: {old_name} ({survey_id})")
                    break
                print("Invalid selection.")
            except ValueError:
                print("Please enter a number.")

    new_name = args.new_name
    if not new_name:
        new_name = input(f"Enter new name for '{old_name}' ({survey_id}): ").strip()
        if not new_name:
            print("New name is required.")
            sys.exit(1)

    print(f"Renaming {survey_id} to '{new_name}'...")
    payload = {"name": new_name}

    try:
        send_api_request(
            action="qsync.survey.rename",
            method="PUT",
            base_url=base,
            headers=headers,
            path=f"surveys/{survey_id}",
            survey_id=survey_id,
            log_meta={"old_name": old_name, "new_name": new_name},
            json=payload,
            timeout=30,
        )
        print(f"Successfully renamed {survey_id} to '{new_name}'")

        from .terminal_output import log_confirmation

        log_confirmation("[rename]")
    except Exception as e:
        print(f"Error renaming survey: {e}")
        sys.exit(1)


def build_client_config_from_args(base_url: str, api_key: str) -> tuple[str, dict]:
    """Build client config from CLI arguments (for target account credentials).

    Args:
        base_url: Target account base URL (e.g., 'iad1.qualtrics.com')
        api_key: Target account API key

    Returns:
        Tuple of (base_url, headers)

    Raises:
        QsyncConfigError: If credentials are invalid
    """
    from .config import build_headers

    env = {
        "QUALTRICS_BASE_URL": base_url,
        "X-API-TOKEN": api_key,
    }
    headers = build_headers(env)  # Will raise QsyncConfigError if invalid
    return base_url, headers


def resolve_target_name_with_conflict(
    target_base: str,
    target_headers: dict,
    requested_name: str,
    force_overwrite: bool,
) -> tuple[str, str | None]:
    """Resolve name conflicts in target account.

    Args:
        target_base: Target account base URL
        target_headers: Target account headers
        requested_name: Desired survey name
        force_overwrite: If True, return existing survey ID for overwrite

    Returns:
        Tuple of (final_name, existing_survey_id_or_none)
    """
    surveys = list_surveys(target_base, target_headers)
    matches = [s for s in surveys if s.get("name") == requested_name]

    if not matches:
        return requested_name, None

    if force_overwrite:
        # Return first match for overwrite
        return requested_name, matches[0].get("id")

    # Auto-suffix: find next available "(N)"
    base_name = requested_name
    counter = 2
    while counter <= 100:  # Safety limit
        candidate = f"{base_name} ({counter})"
        if not any(s.get("name") == candidate for s in surveys):
            return candidate, None
        counter += 1

    raise ValueError(
        f"Unable to generate unique name after 100 attempts for '{requested_name}'"
    )


def handle_copy_cross_account(args: argparse.Namespace) -> None:
    """Copy a survey from one Qualtrics account to another."""
    from .terminal_output import header, info, success, warn, dim
    from .config import load_env, load_env_file, resolve_env_path
    from .translations import (
        _check_placeholders,
        _check_value_length_limit,
    )
    from .translation_export import (
        build_translation_map_from_cache,
        active_qids_in_flow,
        expected_translation_keys_for_qids,
    )
    from .translations_utils import normalize_language_code, normalize_language_list

    # Parse arguments
    source_id = args.source_survey_id
    new_name = args.new_name
    target_api_key = (args.target_api_key or "").strip()
    target_base_url = (args.target_base_url or "").strip()
    target_account = (getattr(args, "target_account", None) or "").strip() or None
    source_api_key = (getattr(args, "source_api_key", None) or "").strip()
    source_base_url = (getattr(args, "source_base_url", None) or "").strip()
    source_account = (getattr(args, "source_account", None) or "").strip() or None
    copy_translations = not bool(getattr(args, "no_translations", False))
    verify = bool(getattr(args, "verify", False))
    verify_deep = bool(getattr(args, "verify_deep", False))
    verify_deep_profile = str(
        getattr(args, "verify_deep_profile", "cross_account") or "cross_account"
    ).strip()
    if bool(getattr(args, "verify_deep_split", False)):
        verify_deep_profile = "split"
    verify_deep_manifest = (getattr(args, "verify_deep_manifest", None) or "").strip()

    # Convenience alias: allow `--target-account default` to mean the primary
    # account credentials from `.env` (QUALTRICS_BASE_URL + X-API-TOKEN), not
    # TARGET_* or ambient active-account credentials.
    target_is_default = False
    if target_account and target_account.lower() == "default":
        target_is_default = True
        target_account = None

    # Mirror the alias for source as well.
    source_is_default = False
    if source_account and source_account.lower() == "default":
        source_is_default = True
        source_account = None

    # Read `.env` (if present) so this command can support TARGET_* defaults.
    root = resolve_root(required=False) or Path.cwd()
    env_path = resolve_env_path(root=root)
    file_env = load_env_file(env_path) if env_path else {}

    def _load_primary_env() -> dict[str, str]:
        # Passing an explicit env path bypasses ambient account selection.
        return load_env(env_path)

    def _env_or_dotenv(key: str) -> str:
        raw = (os.environ.get(key) or "").strip()
        if raw:
            return raw
        return str(file_env.get(key) or "").strip()

    # Resolve target credentials (do not silently fall back to the primary account).
    if target_is_default and (not target_base_url or not target_api_key):
        try:
            default_base, default_headers = get_client_config(_load_primary_env())
        except Exception as exc:
            print(f"ERROR: Invalid default target credentials: {exc}")
            sys.exit(1)
        if not target_base_url:
            target_base_url = default_base
        if not target_api_key:
            target_api_key = str(default_headers.get("X-API-TOKEN") or "").strip()

    if target_account and (not target_base_url or not target_api_key):
        target_file_env = load_account_env(target_account, root=root)
        if not target_base_url:
            target_base_url = str(target_file_env.get("QUALTRICS_BASE_URL") or "").strip()
        if not target_api_key:
            target_api_key = (
                str(target_file_env.get("X-API-TOKEN") or "").strip()
                or str(target_file_env.get("QUALTRICS_API_KEY") or "").strip()
            )

    if not target_base_url:
        if not target_account:
            target_base_url = _env_or_dotenv("TARGET_QUALTRICS_BASE_URL")
    if not target_api_key:
        if not target_account:
            target_api_key = _env_or_dotenv("TARGET_X-API-TOKEN") or _env_or_dotenv(
                "TARGET_QUALTRICS_API_KEY"
            )
    if not target_base_url or not target_api_key:
        print(
            "ERROR: Target credentials missing. Provide --target-base-url/--target-api-key, "
            "or use --target-account <name> (.env.<name>) or --target-account default, "
            "or set TARGET_QUALTRICS_BASE_URL and TARGET_X-API-TOKEN in your environment/.env."
        )
        sys.exit(1)

    def _fetch_base_language(base_url: str, headers: dict, survey_id: str) -> str:
        resp = send_api_request(
            action="qsync.translations.base_language",
            method="GET",
            base_url=base_url,
            headers=headers,
            path=f"survey-definitions/{survey_id}/options",
            survey_id=survey_id,
            log_event=False,
            timeout=30,
        )
        result = resp.json().get("result") or {}
        lang = str(result.get("SurveyLanguage") or "").strip()
        if not lang:
            raise RuntimeError(f"SurveyLanguage missing for {survey_id}")
        normalized = normalize_language_code(lang)
        if not normalized:
            raise ValueError(f"Invalid SurveyLanguage for {survey_id}: {lang!r}")
        return normalized

    def _list_enabled_languages(
        base_url: str, headers: dict, survey_id: str
    ) -> list[str]:
        resp = send_api_request(
            action="qsync.translations.languages.list",
            method="GET",
            base_url=base_url,
            headers=headers,
            path=f"surveys/{survey_id}/languages",
            survey_id=survey_id,
            log_event=False,
            timeout=30,
        )
        result = resp.json().get("result") or {}
        langs = result.get("AvailableLanguages") or result.get("languages") or []
        if isinstance(langs, dict):
            # Qualtrics commonly uses `{ "EN": true, "FR": true }`.
            enabled = [k for k, v in langs.items() if bool(v)]
            return normalize_language_list(enabled)
        if isinstance(langs, (list, tuple, set)):
            return normalize_language_list(langs)
        return []

    def _ensure_languages_enabled(
        base_url: str, headers: dict, survey_id: str, languages: list[str]
    ) -> list[str]:
        current = set(_list_enabled_languages(base_url, headers, survey_id))
        desired = normalize_language_list(list(current.union(languages)))
        send_api_request(
            action="qsync.translations.languages.ensure",
            method="PUT",
            base_url=base_url,
            headers=headers,
            path=f"surveys/{survey_id}/languages",
            survey_id=survey_id,
            json={"AvailableLanguages": desired},
            timeout=30,
        )
        return desired

    def _fetch_survey_definition(base_url: str, headers: dict, survey_id: str) -> dict:
        resp = send_api_request(
            action="qsync.survey.definition.fetch",
            method="GET",
            base_url=base_url,
            headers=headers,
            path=f"survey-definitions/{survey_id}",
            survey_id=survey_id,
            log_event=False,
            timeout=60,
        )
        payload = resp.json()
        if isinstance(payload, dict) and "result" in payload:
            return payload
        return {"result": payload}

    def _sanitize_metadata_translations(meta: dict) -> dict[str, Any]:
        cleaned: dict[str, Any] = {}
        for lang, entry in meta.items():
            if not isinstance(entry, dict):
                continue
            updated = dict(entry)
            if "SurveyDescription" in updated:
                if "SurveyMetaDescription" not in updated:
                    updated["SurveyMetaDescription"] = updated["SurveyDescription"]
                updated.pop("SurveyDescription", None)
            cleaned[lang] = updated
        return cleaned

    def _push_metadata_translations(
        base_url: str, headers: dict, survey_id: str, source_payload: dict
    ) -> None:
        source_options = (source_payload.get("result") or {}).get("SurveyOptions") or {}
        if not isinstance(source_options, dict):
            return
        meta = source_options.get("MetaDataTranslations") or {}
        if not isinstance(meta, dict) or not meta:
            return
        meta = _sanitize_metadata_translations(meta)
        resp = send_api_request(
            action="qsync.translations.push.options",
            method="GET",
            base_url=base_url,
            headers=headers,
            path=f"survey-definitions/{survey_id}/options",
            survey_id=survey_id,
            log_event=False,
            timeout=30,
        )
        current = resp.json().get("result") or {}
        merged = dict(current)
        merged["MetaDataTranslations"] = meta
        send_api_request(
            action="qsync.translations.push.options",
            method="PUT",
            base_url=base_url,
            headers=headers,
            path=f"survey-definitions/{survey_id}/options",
            survey_id=survey_id,
            json=merged,
            timeout=30,
        )

    def _copy_translations(source_survey_id: str, target_survey_id: str) -> None:
        source_langs = _list_enabled_languages(
            source_base, source_headers, source_survey_id
        )
        if not source_langs:
            info(
                "[copy-cross-account]",
                "No enabled languages found in source; skipping translations.",
            )
            return

        source_base_lang = _fetch_base_language(
            source_base, source_headers, source_survey_id
        )
        target_base_lang = _fetch_base_language(
            target_base, target_headers, target_survey_id
        )
        if source_base_lang != target_base_lang:
            raise RuntimeError(
                "Base language differs between source and target "
                f"({source_base_lang} vs {target_base_lang}); aborting translation copy."
            )

        source_payload = _fetch_survey_definition(
            source_base, source_headers, source_survey_id
        )
        target_payload = _fetch_survey_definition(
            target_base, target_headers, target_survey_id
        )

        info(
            "[copy-cross-account]",
            f"Enabling languages in target: {', '.join(source_langs)}",
        )
        _ensure_languages_enabled(
            target_base, target_headers, target_survey_id, source_langs
        )

        active_qids = active_qids_in_flow(source_payload)
        expected_keys: list[str] | None = None
        if active_qids:
            expected_keys = expected_translation_keys_for_qids(
                source_payload, qids=active_qids
            )

        base_map = build_translation_map_from_cache(
            source_payload,
            language=source_base_lang,
            base_language=source_base_lang,
        )

        base_map_scoped: Mapping[str, Any] = base_map
        if expected_keys:
            # Always include metadata keys that are present in the base map.
            meta_keys = [k for k in base_map.keys() if "_" not in str(k)]
            expected = sorted(set(expected_keys).union(meta_keys), key=str)
            expected_keys = expected
            base_map_scoped = {k: base_map.get(k, "") for k in expected_keys}

            source_questions = (
                (source_payload.get("result") or {}).get("Questions") or {}
            )
            if isinstance(source_questions, dict):
                excluded = max(0, len(source_questions) - len(active_qids))
                if excluded > 0:
                    dim(
                        "[copy-cross-account]",
                        (
                            "Translation validation scope: "
                            f"{len(active_qids)} in-flow QID(s); skipped {excluded} out-of-flow/Trash QID(s)."
                        ),
                    )

        allowed_empty_keys = {
            str(k)
            for k, v in base_map_scoped.items()
            if not isinstance(v, str) or not v.strip()
        }

        # Track progress for final summary
        completed_languages: list[str] = []
        skipped_base: str | None = None

        for lang in source_langs:
            if lang == source_base_lang:
                skipped_base = lang
                dim(
                    "[copy-cross-account]",
                    f"Skipping base language {lang} (not writable via translations API).",
                )
                continue

            info("[copy-cross-account]", f"Processing language: {lang}")
            normalized_full = build_translation_map_from_cache(
                source_payload,
                language=lang,
                base_language=source_base_lang,
            )
            normalized: dict[str, str]
            if expected_keys:
                normalized = {k: normalized_full.get(k, "") for k in expected_keys}
            else:
                normalized = normalized_full

            # Calculate statistics
            total_keys = len(normalized)
            filled_keys = sum(1 for v in normalized.values() if str(v or "").strip())
            empty_keys = total_keys - filled_keys

            info(
                "[copy-cross-account]",
                f"  [{lang}] Keys: {total_keys} total, {filled_keys} filled, {empty_keys} empty",
            )

            errors: list[str] = []
            warnings: list[str] = []

            # Coverage check: allow empties that are empty in base language.
            empties = [
                k
                for k, v in normalized.items()
                if not str(v or "").strip() and str(k) not in allowed_empty_keys
            ]
            if empties:
                warnings.append(
                    f"[{lang}] Coverage incomplete: {len(normalized) - len(empties)}/{len(normalized)} filled."
                )
                sample = ", ".join(str(k) for k in empties[:12])
                suffix = f" … (+{len(empties) - 12} more)" if len(empties) > 12 else ""
                warnings.append(f"[{lang}] Empty keys (sample): {sample}{suffix}")

            warnings.extend(_check_value_length_limit(normalized, lang))
            ph_errors, ph_warnings = _check_placeholders(
                base_map_scoped, normalized, lang
            )
            errors.extend(ph_errors)
            warnings.extend(ph_warnings)

            if warnings:
                info(
                    "[copy-cross-account]",
                    f"  [{lang}] {len(warnings)} validation warning(s)",
                )
                for line in warnings[:30]:
                    warn("[copy-cross-account]", line)
                if len(warnings) > 30:
                    warn(
                        "[copy-cross-account]",
                        f"[{lang}] warnings truncated (showing first 30 of {len(warnings)}).",
                    )

            if errors:
                warn(
                    "[copy-cross-account]",
                    f"  [{lang}] {len(errors)} validation error(s)",
                )
                for line in errors[:30]:
                    warn("[copy-cross-account]", line)
                if len(errors) > 30:
                    warn(
                        "[copy-cross-account]",
                        f"[{lang}] errors truncated (showing first 30 of {len(errors)}).",
                    )

                # Show what was completed before this failure
                if completed_languages:
                    warn(
                        "[copy-cross-account]",
                        f"Languages completed before failure: {', '.join(completed_languages)}",
                    )
                warn("[copy-cross-account]", f"Failed language: {lang}")
                warn(
                    "[copy-cross-account]",
                    "Recovery: Fix validation errors in source survey, then re-run copy-cross-account.",
                )
                raise RuntimeError(
                    f"Translation validation failed for {lang}; aborting copy."
                )

            completed_languages.append(lang)
            success("[copy-cross-account]", f"  [{lang}] ✓ Validated")

        source_questions = (source_payload.get("result") or {}).get("Questions") or {}
        target_questions = (target_payload.get("result") or {}).get("Questions") or {}
        updated_qids: list[str] = []
        for qid, target_question in target_questions.items():
            source_question = (
                source_questions.get(qid)
                if isinstance(source_questions, dict)
                else None
            )
            if not isinstance(source_question, dict) or not isinstance(
                target_question, dict
            ):
                continue
            source_lang_block = source_question.get("Language") or {}
            if not isinstance(source_lang_block, dict):
                continue
            filtered: dict[str, Any] = {}
            for lang in source_langs:
                if lang == source_base_lang:
                    continue
                if lang in source_lang_block:
                    filtered[lang] = source_lang_block.get(lang)
            if not filtered:
                continue
            updated_question = dict(target_question)
            existing_lang_block = updated_question.get("Language")
            if not isinstance(existing_lang_block, dict):
                existing_lang_block = {}
            merged_lang_block = dict(existing_lang_block)
            merged_lang_block.update(filtered)
            updated_question["Language"] = merged_lang_block
            send_api_request(
                action="qsync.survey.copy-cross-account.translations.question",
                method="PUT",
                base_url=target_base,
                headers=target_headers,
                path=f"survey-definitions/{target_survey_id}/questions/{qid}",
                survey_id=target_survey_id,
                json=updated_question,
                timeout=60,
            )
            updated_qids.append(str(qid))

        _push_metadata_translations(
            target_base, target_headers, target_survey_id, source_payload
        )
        if updated_qids:
            info(
                "[copy-cross-account]",
                f"Updated {len(updated_qids)} question(s) with Language blocks.",
            )

        # Final summary
        if completed_languages or skipped_base:
            info("[copy-cross-account]", "Translation copy summary:")
            if skipped_base:
                dim("[copy-cross-account]", f"  Base language skipped: {skipped_base}")
            if completed_languages:
                success(
                    "[copy-cross-account]",
                    f"  Languages copied: {', '.join(completed_languages)} ({len(completed_languages)} total)",
                )
            else:
                dim("[copy-cross-account]", "  No non-base languages copied.")

    # Get source credentials (ambient account by default, unless overridden).
    if source_is_default:
        source_env = _load_primary_env()
    elif source_account and (not source_base_url or not source_api_key):
        source_env = load_account_env(source_account, root=root)
    else:
        source_env = load_env()
    if source_base_url:
        source_env["QUALTRICS_BASE_URL"] = source_base_url
    if source_api_key:
        source_env["X-API-TOKEN"] = source_api_key
    try:
        source_base, source_headers = get_client_config(source_env)
    except Exception as e:
        print(f"ERROR: Invalid source credentials: {e}")
        sys.exit(1)

    # Build target credentials (explicit flags or TARGET_*; no fallback).
    try:
        target_base, target_headers = get_client_config(
            {"QUALTRICS_BASE_URL": target_base_url, "X-API-TOKEN": target_api_key}
        )
    except Exception as e:
        print(f"ERROR: Invalid target credentials: {e}")
        sys.exit(1)

    def _whoami(base_url: str, headers: dict) -> dict[str, Any] | None:
        try:
            resp = send_api_request(
                action="qsync.copy-cross-account.whoami",
                method="GET",
                base_url=base_url,
                headers=headers,
                path="whoami",
                log_event=False,
                timeout=15,
            )
            result = resp.json().get("result") or {}
            return result if isinstance(result, dict) else {}
        except Exception:
            return None

    # Fetch source survey
    print(f"Fetching source survey {source_id} from {source_base}...")
    try:
        qsf_content = fetch_survey_definition(
            source_base, source_headers, source_id, fmt="qsf"
        )
        source_name = qsf_content.get("SurveyEntry", {}).get("SurveyName", source_id)
    except Exception as e:
        print(f"ERROR: Failed to fetch source survey: {e}")
        sys.exit(1)

    # Keep an untouched copy for post-copy verification.
    import copy as _copy

    source_qsf_for_verify = _copy.deepcopy(qsf_content)

    source_identity = _whoami(source_base, source_headers) or {}
    target_identity = _whoami(target_base, target_headers) or {}
    source_user_id = str(source_identity.get("userId") or "").strip() or None
    target_user_id = str(target_identity.get("userId") or "").strip() or None
    source_brand = str(source_identity.get("brandId") or "").strip() or None
    target_brand = str(target_identity.get("brandId") or "").strip() or None

    # Check name conflicts in target account
    print("Checking for name conflicts in target account...")
    final_name, existing_id = resolve_target_name_with_conflict(
        target_base, target_headers, new_name, args.force_overwrite
    )

    conflict_msg = None
    if existing_id:
        conflict_msg = f"Will overwrite existing survey {existing_id}"
    elif final_name != new_name:
        conflict_msg = f"Auto-suffixed name to '{final_name}' to avoid conflict"

    # Show preview
    header("[copy-cross-account]", "Preview:")
    print()
    info("  Source Survey:", "")
    info("    Name:", source_name)
    info("    ID:", source_id)
    info("    Account:", source_base)
    if source_user_id:
        dim("    userId:", source_user_id)
    if source_brand:
        dim("    brandId:", source_brand)
    print()
    info("  Target Survey:", "")
    info("    Name:", final_name)
    info("    Account:", target_base)
    if target_user_id:
        dim("    userId:", target_user_id)
    if target_brand:
        dim("    brandId:", target_brand)
    if source_base == target_base and source_user_id and target_user_id:
        if source_user_id == target_user_id:
            warn(
                "    ⚠",
                "Source and target appear to be the same Qualtrics userId; if you expected cross-account copy, check TARGET_* credentials.",
            )
    if conflict_msg:
        dim("    Conflict resolution:", conflict_msg)
    print()
    info("  Operations:", "")
    success(
        "    ✓", "Copy survey definition (all questions, flow, logic, embedded data)"
    )
    if copy_translations:
        success("    ✓", "Copy translations (Language blocks + metadata)")
    else:
        dim("    ✗", "Copy translations (use default; disable via --no-translations)")

    if verify:
        success("    ✓", "Verify parity + translations after copy")
    else:
        dim("    ✗", "Verify parity + translations (use --verify to enable)")

    if verify_deep:
        success(
            "    ✓",
            f"Verify deep parity (survey-definitions, profile={verify_deep_profile}) after copy",
        )
        if verify_deep_manifest:
            dim("      manifest:", verify_deep_manifest)
    else:
        dim("    ✗", "Verify deep parity (use --verify-deep to enable)")

    if args.activate:
        success("    ✓", "Activate survey after import")
    else:
        dim("    ✗", "Activate survey (use --activate to enable)")

    if args.publish or args.publish_description:
        desc = args.publish_description or f"qsync copy-cross-account from {source_id}"
        success(
            "    ✓",
            (
                f"Publish survey with description: '{desc[:50]}...' "
                if len(desc) > 50
                else f"Publish survey with description: '{desc}'"
            ),
        )
    else:
        dim("    ✗", "Publish survey (use --publish to enable)")

    if existing_id:
        warn(
            "    ⚠",
            f"--force-overwrite will DELETE the existing target survey {existing_id}.",
        )
        warn(
            "    ⚠",
            "This permanently loses the target survey's version/publish history and creates a NEW SurveyID for the replacement.",
        )
        warn(
            "    ⚠",
            f"Attempt to create a backup published version before deletion: {existing_id}",
        )
    else:
        dim("    ✗", "Create backup version (not overwriting)")
    print()

    # Confirmation
    if not args.yes:
        if not sys.stdin.isatty():
            raise SystemExit(
                "[copy-cross-account] ERROR: Confirmation required but stdin is not interactive. "
                "Re-run with --yes to proceed."
            )
        try:
            from qsync.interactive_menu import confirm

            if not confirm("Proceed with cross-account copy?", default=True):
                print("Aborted.")
                return
        except Exception:
            confirm = input("Proceed with cross-account copy? [Y/n]: ").strip().lower()
            if confirm and confirm != "y":
                print("Aborted.")
                return

    # If overwriting, backup existing survey
    if existing_id:
        info("[copy-cross-account]", f"Creating backup version of {existing_id}...")
        try:
            publish_survey_definition(
                existing_id,
                description="qsync backup before cross-account overwrite",
                base_url=target_base,
                headers=target_headers,
            )
            info("[copy-cross-account]", f"Backup version created for {existing_id}")
        except Exception as e:
            print(f"ERROR: Failed to create backup version: {e}")
            sys.exit(1)

        # Delete existing survey
        warn(
            "[copy-cross-account]",
            f"WARNING: Deleting {existing_id} will permanently delete its version/publish history in Qualtrics.",
        )
        info("[copy-cross-account]", f"Deleting existing survey {existing_id}...")
        try:
            send_api_request(
                action="qsync.survey.delete",
                method="DELETE",
                base_url=target_base,
                headers=target_headers,
                path=f"surveys/{existing_id}",
                survey_id=existing_id,
                timeout=30,
            )
        except Exception as e:
            print(f"ERROR: Failed to delete existing survey: {e}")
            sys.exit(1)

    # Prepare and upload QSF
    info("[copy-cross-account]", "Preparing survey for import...")
    prepare_qsf_for_import(qsf_content, final_name, status="Inactive")

    info("[copy-cross-account]", "Uploading to target account...")
    try:
        new_id = upload_qsf_to_account(
            qsf_content,
            final_name,
            target_base,
            target_headers,
            action="qsync.survey.copy-cross-account",
            log_meta={
                "source_survey_id": source_id,
                "source_base": source_base,
                "target_base": target_base,
            },
        )
        success("[copy-cross-account]", f"Survey uploaded: {new_id}")
    except Exception as e:
        print(f"ERROR: Failed to upload survey: {e}")
        sys.exit(1)

    if copy_translations:
        info("[copy-cross-account]", "Copying translations...")
        try:
            _copy_translations(source_id, new_id)
            success("[copy-cross-account]", "All translations copied successfully")
        except Exception as e:
            warn("[copy-cross-account]", "=" * 60)
            warn("[copy-cross-account]", f"ERROR: Translation copy failed: {e}")
            warn("[copy-cross-account]", "=" * 60)
            warn(
                "[copy-cross-account]",
                "Survey structure was created successfully, but translations were not fully copied.",
            )
            warn("[copy-cross-account]", "")
            warn("[copy-cross-account]", "Next steps:")
            warn("[copy-cross-account]", f"  1. Survey ID in target account: {new_id}")
            warn(
                "[copy-cross-account]",
                "  2. Fix validation errors in the source survey (see errors above)",
            )
            warn(
                "[copy-cross-account]",
                "  3. Use 'qsync translations push' to manually copy the failed language(s)",
            )
            warn(
                "[copy-cross-account]",
                f"     Or re-run: qsync survey copy-cross-account {source_id} <new-name> --force-overwrite",
            )
            warn("[copy-cross-account]", "=" * 60)
            sys.exit(1)

    if verify:
        from .survey_parity import compare_qsf_parity

        info("[copy-cross-account]", "Running parity check (best-effort)...")
        target_qsf = fetch_survey_definition(target_base, target_headers, new_id, fmt="qsf")
        parity = compare_qsf_parity(source_qsf_for_verify, target_qsf)
        ok = _emit_parity_report(
            result=parity,
            survey_a=source_id,
            survey_b=new_id,
            prefix="[copy-cross-account:parity]",
        )
        if not ok:
            raise SystemExit(
                "[copy-cross-account] Parity check failed; see details above."
            )

        if copy_translations:
            info("[copy-cross-account]", "Verifying translations (best-effort)...")
            source_langs = _list_enabled_languages(
                source_base, source_headers, source_id
            )
            target_langs = _list_enabled_languages(
                target_base, target_headers, new_id
            )
            if set(source_langs) != set(target_langs):
                warn(
                    "[copy-cross-account]",
                    "Enabled languages differ between source and target.",
                )
                warn("[copy-cross-account]", f"Source: {', '.join(source_langs)}")
                warn("[copy-cross-account]", f"Target: {', '.join(target_langs)}")
                raise SystemExit(
                    "[copy-cross-account] Translation verification failed: enabled languages mismatch."
                )

            base_lang = _fetch_base_language(source_base, source_headers, source_id)
            target_base_lang = _fetch_base_language(
                target_base, target_headers, new_id
            )
            if base_lang != target_base_lang:
                raise SystemExit(
                    "[copy-cross-account] Translation verification failed: base language mismatch "
                    f"({base_lang} vs {target_base_lang})."
                )

            source_def = _fetch_survey_definition(source_base, source_headers, source_id)
            target_def = _fetch_survey_definition(target_base, target_headers, new_id)

            diffs: list[str] = []
            for lang in source_langs:
                if lang == base_lang:
                    continue
                src_map = build_translation_map_from_cache(
                    source_def,
                    language=lang,
                    base_language=base_lang,
                )
                tgt_map = build_translation_map_from_cache(
                    target_def,
                    language=lang,
                    base_language=base_lang,
                )
                src_keys = set(src_map.keys())
                tgt_keys = set(tgt_map.keys())
                if src_keys != tgt_keys:
                    missing = sorted(src_keys - tgt_keys)[:10]
                    extra = sorted(tgt_keys - src_keys)[:10]
                    diffs.append(
                        f"[{lang}] key mismatch: -{len(src_keys - tgt_keys)} +{len(tgt_keys - src_keys)} "
                        f"(missing sample={missing} extra sample={extra})"
                    )
                    continue
                changed = [
                    k
                    for k in sorted(src_keys)
                    if (src_map.get(k) or "") != (tgt_map.get(k) or "")
                ]
                if changed:
                    diffs.append(
                        f"[{lang}] value mismatch: {len(changed)} changed (sample={changed[:10]})"
                    )

            if diffs:
                warn("[copy-cross-account]", "Translation diffs detected:")
                for line in diffs[:20]:
                    warn("[copy-cross-account]", f"  {line}")
                if len(diffs) > 20:
                    warn(
                        "[copy-cross-account]",
                        f"  (diffs truncated; showing 20 of {len(diffs)})",
                    )
                raise SystemExit(
                    "[copy-cross-account] Translation verification failed; see diffs above."
                )
            success("[copy-cross-account]", "Translation verification passed")
        else:
            dim(
                "[copy-cross-account]",
                "Translation verification skipped (--no-translations).",
            )

    if verify_deep:
        from .survey_deep_parity import compare_survey_definition_deep_parity

        info(
            "[copy-cross-account]",
            f"Running deep parity check (survey-definitions, profile={verify_deep_profile})...",
        )
        try:
            source_def_deep = fetch_survey_definition(
                source_base, source_headers, source_id, fmt="json"
            )
            target_def_deep = fetch_survey_definition(
                target_base, target_headers, new_id, fmt="json"
            )
        except Exception as exc:
            raise SystemExit(
                f"[copy-cross-account] Deep parity failed: unable to fetch definitions: {exc}"
            ) from exc

        report = compare_survey_definition_deep_parity(
            source_def_deep,
            target_def_deep,
            survey_a=source_id,
            survey_b=new_id,
            write_artifacts_on_mismatch=True,
            profile=verify_deep_profile,
            manifest_path=verify_deep_manifest or None,
        )
        if not _emit_deep_parity_report(
            report=report,
            prefix="[copy-cross-account]",
        ):
            raise SystemExit(
                "[copy-cross-account] Deep parity check failed; see diffs above."
            )

    # Optionally activate
    if args.activate:
        info("[copy-cross-account]", f"Activating survey {new_id}...")
        try:
            activate_survey(
                new_id,
                target_base,
                target_headers,
                active=True,
                log_meta={"context": "copy-cross-account"},
            )
            success("[copy-cross-account]", "Survey activated")
        except Exception as e:
            warn(
                "[copy-cross-account]",
                f"WARNING: Survey created but activation failed: {e}",
            )
            warn("[copy-cross-account]", f"Manually activate {new_id} in Qualtrics UI")

    # Optionally publish
    if args.publish or args.publish_description:
        desc = args.publish_description or f"qsync copy-cross-account from {source_id}"
        if len(desc) > 140:
            desc = desc[:140]
        info("[copy-cross-account]", f"Publishing survey {new_id}...")
        try:
            publish_survey_definition(
                new_id,
                description=desc,
                base_url=target_base,
                headers=target_headers,
            )
            success("[copy-cross-account]", "Survey published")
        except Exception as e:
            warn(
                "[copy-cross-account]",
                f"WARNING: Survey created but publishing failed: {e}",
            )
            warn("[copy-cross-account]", f"Manually publish {new_id} in Qualtrics UI")

    # Success output
    print()
    success(
        "[copy-cross-account]",
        f"Successfully copied {source_id} → {final_name} ({new_id})",
    )
    edit_url = f"https://{target_base}/survey-builder/{new_id}/edit"
    info("[copy-cross-account]", f"Edit link: {edit_url}")
    print()

    from .terminal_output import log_confirmation

    log_confirmation("[copy-cross-account]")


def handle_delete(args: argparse.Namespace) -> None:
    """Delete one or more surveys by SurveyID."""
    from .survey_ref import format_survey_ref

    def _flag(name: str) -> bool:
        value = getattr(args, name, False)
        return bool(value) if isinstance(value, bool) else False

    survey_ids = list(
        dict.fromkeys(
            sid.strip()
            for sid in _normalize_survey_ids(getattr(args, "survey_ids", None))
            if sid and sid.strip()
        )
    )
    if not survey_ids:
        raise SystemExit("[delete] ERROR: at least one survey ID is required.")

    yes = _flag("yes")
    force_live = _flag("force_live")
    dry_run = _flag("dry_run") or not yes
    interactive_mode = bool(sys.stdin.isatty() and sys.stdout.isatty())

    base, headers = _get_client_config_for_args(args)

    if dry_run:
        print(
            "[delete] DRY-RUN mode: no delete requests will be sent. "
            "Use --yes to execute deletes."
        )

    for survey_id in survey_ids:
        survey_id = survey_id.strip()
        if not survey_id:
            continue

        status_payload: Dict[str, Any]
        try:
            status_payload = _fetch_survey_status(base, headers, survey_id)
        except Exception as exc:
            print(f"[delete] Failed to fetch survey details for {survey_id}: {exc}")
            continue

        survey_name = str(status_payload.get("name") or "").strip() or survey_id
        survey_ref = format_survey_ref(survey_id, survey_name)

        active_value = status_payload.get("isActive")
        if isinstance(active_value, bool):
            active_label = "active" if active_value else "inactive"
        else:
            active_label = "unknown"

        live_count, preview_count = _status_response_counts(status_payload)
        counts_unknown = live_count is None or preview_count is None
        counts_source = "status"

        if counts_unknown:
            try:
                ctx = load_push_context(survey_id, base_url=base, headers=headers)
                survey_name = str(getattr(ctx, "survey_name", "") or "").strip() or survey_name
                survey_ref = format_survey_ref(survey_id, survey_name)
                live_count = int(getattr(ctx, "response_count", 0))
                preview_count = int(getattr(ctx, "preview_count", 0))
                counts_unknown = bool(getattr(ctx, "counts_unknown", False))
                counts_source = str(getattr(ctx, "counts_source", "") or "push-context")
            except Exception as exc:
                print(
                    f"[delete] {survey_ref}: NOTE: unable to load fallback response counts: {exc}"
                )
                counts_source = "unknown"

        print()
        print(f"[delete] Survey: {survey_name}")
        print(f"[delete] SurveyID: {survey_id}")
        print(f"[delete] Status: {active_label}")
        if counts_unknown:
            print("[delete] Responses: unknown")
        else:
            print(
                f"[delete] Responses: {int(live_count or 0)} live / "
                f"{int(preview_count or 0)} preview (source: {counts_source})"
            )

        proceed_live = not dry_run
        effective_force_live = force_live

        if dry_run and interactive_mode:
            proceed_live = _confirm_interactive_gate(
                prompt=f"Proceed with LIVE delete for {survey_ref}?",
                default=False,
            )
            if not proceed_live:
                print("[delete] Dry-run only; skipping live delete.")
                continue

        if not proceed_live:
            print("[delete] Dry-run only; skipping live delete.")
            continue

        if counts_unknown and not effective_force_live:
            if dry_run and interactive_mode:
                effective_force_live = _confirm_interactive_gate(
                    prompt=(
                        f"Response counts are unknown for {survey_ref}. "
                        "Proceed with force-live override?"
                    ),
                    default=False,
                )
                if not effective_force_live:
                    print("[delete] Skipping live delete (counts unknown).")
                    continue
            else:
                print(
                    f"[delete] Blocked: unable to verify response counts for {survey_ref}. "
                    "Re-run with --force-live after manual review."
                )
                continue

        live_responses = int(live_count or 0) if not counts_unknown else 0
        if live_responses > 0 and not effective_force_live:
            if dry_run and interactive_mode:
                effective_force_live = _confirm_interactive_gate(
                    prompt=(
                        f"{survey_ref} has {live_responses} finished response(s). "
                        "Proceed with force-live override?"
                    ),
                    default=False,
                )
                if not effective_force_live:
                    print("[delete] Skipping live delete (responses present).")
                    continue
            else:
                print(
                    f"[delete] Blocked: {survey_ref} has {live_responses} finished response(s). "
                    "Re-run with --force-live after double-checking."
                )
                continue

        if not yes:
            if not _typed_confirmation(
                prompt=f"Type exact SurveyID '{survey_id}' to confirm delete: ",
                expected=survey_id,
            ):
                print(f"[delete] Confirmation failed for {survey_ref}; skipped.")
                continue

        print(f"Deleting survey {survey_ref}...")
        try:
            send_api_request(
                action="qsync.survey.delete",
                method="DELETE",
                base_url=base,
                headers=headers,
                path=f"surveys/{survey_id}",
                survey_id=survey_id,
                timeout=30,
            )
        except Exception as exc:
            response = getattr(exc, "response", None)
            if response is not None:
                print(
                    f"Failed to delete {survey_ref}: {response.status_code} {response.text}"
                )
            else:
                print(f"Failed to delete {survey_ref}: {exc}")
        else:
            print(f"Successfully deleted {survey_ref}")

            from .terminal_output import log_confirmation

            log_confirmation("[delete]")


def handle_inventory(args: argparse.Namespace) -> None:
    """Refresh the Qualtrics survey inventory cache."""
    import time

    from .terminal_output import dim, format_elapsed

    base, headers = get_client_config()
    quiet = bool(getattr(args, "quiet", False))
    explicit_progress = bool(
        getattr(args, "progress", False) or getattr(args, "progress_only", False)
    )
    progress_only = bool(getattr(args, "progress_only", False))

    # Parse --survey-id arguments (supports repeated and comma-separated)
    survey_filter: List[str] = []
    if getattr(args, "survey_ids", None):
        for raw in args.survey_ids:
            if not raw:
                continue
            for token in raw.split(","):
                token = token.strip()
                if token:
                    survey_filter.append(token)
    # Preserve insertion order while deduplicating
    survey_filter = list(dict.fromkeys(survey_filter)) if survey_filter else None
    auto_progress = bool(
        not quiet and (survey_filter is None or len(survey_filter) > 1)
    )
    progress = bool(explicit_progress or auto_progress)

    dry_run = getattr(args, "dry_run", False)
    counts_scope = getattr(args, "counts_scope", None)

    if survey_filter and counts_scope:
        print(
            "[inventory] NOTE: --survey-id refresh already fetches response counts; "
            "ignoring --focal/--full."
        )
        counts_scope = None

    if not quiet:
        if survey_filter:
            print(f"[inventory] Refreshing {len(survey_filter)} targeted survey(s)...")
        else:
            print("[inventory] Fetching full survey inventory from Qualtrics...")

    start_time = time.perf_counter()
    inventory, changed_records = refresh_inventory(
        base,
        headers,
        survey_filter=survey_filter,
        dry_run=dry_run,
        counts_scope=counts_scope,
        progress=progress,
        quiet=quiet,
    )
    elapsed = time.perf_counter() - start_time

    editable = sum(1 for record in inventory if record.get("editableViaApi"))
    non_editable = len(inventory) - editable

    if not progress_only:
        if dry_run:
            if not quiet:
                print(
                    f"[DRY RUN] Would save {len(inventory)} surveys (editable={editable}, non-editable={non_editable})"
                )
        else:
            if not quiet:
                print(
                    f"Saved {len(inventory)} surveys to {SURVEY_CACHE} (editable={editable}, non-editable={non_editable})"
                )

    if not quiet and not progress_only:
        if changed_records:
            for survey in changed_records:
                label = "editable" if survey.get("editableViaApi") else "read-only"
                print(
                    f"  - {survey.get('name', '(unnamed)')} | ID={survey.get('id')} | {label}"
                )
        else:
            print("  - No inventory rows changed (ignoring generated_at).")
        from .terminal_output import mark_timing_emitted

        dim("[inventory]", f"Completed in {format_elapsed(elapsed)}")
        mark_timing_emitted()


def _merge_embedded_pending(survey_id: str, additions: list[dict[str, str]]) -> None:
    from .pending_stage import (
        ItemsPendingPayload,
        PendingStagedChanges,
        load_pending,
        save_pending,
    )

    existing = load_pending(survey_id, "items")
    if existing and isinstance(existing.payload, ItemsPendingPayload):
        qids = list(existing.payload.qids or [])
        embedded_fields = list(existing.payload.embedded_fields or [])
        workbook = existing.payload.workbook
        filter_column = existing.payload.filter_column
        filter_value = existing.payload.filter_value
    else:
        qids = []
        embedded_fields = []
        workbook = None
        filter_column = None
        filter_value = None

    seen = {
        (entry.get("flow_id") or "", entry.get("field") or "")
        for entry in embedded_fields
    }
    for entry in additions:
        key = (entry.get("flow_id") or "", entry.get("field") or "")
        if key not in seen:
            embedded_fields.append({"flow_id": key[0], "field": key[1]})
            seen.add(key)

    record = PendingStagedChanges(
        survey_id=survey_id,
        dimension="items",
        payload=ItemsPendingPayload(
            qids=qids,
            embedded_fields=embedded_fields,
            workbook=workbook,
            filter_column=filter_column,
            filter_value=filter_value,
        ),
    )
    save_pending(record)


def _merge_embedded_rename_pending(
    survey_id: str, renames: list[dict[str, str]]
) -> None:
    from .pending_stage import (
        ItemsPendingPayload,
        PendingStagedChanges,
        load_pending,
        save_pending,
    )

    existing = load_pending(survey_id, "items")
    if existing and isinstance(existing.payload, ItemsPendingPayload):
        qids = list(existing.payload.qids or [])
        embedded_fields = list(existing.payload.embedded_fields or [])
        workbook = existing.payload.workbook
        filter_column = existing.payload.filter_column
        filter_value = existing.payload.filter_value
    else:
        qids = []
        embedded_fields = []
        workbook = None
        filter_column = None
        filter_value = None

    replaced_keys = {
        (entry.get("flow_id") or "", entry.get("from_field") or "") for entry in renames
    }
    cleaned = []
    for entry in embedded_fields:
        key = (entry.get("flow_id") or "", entry.get("field") or "")
        if key not in replaced_keys:
            cleaned.append(entry)

    _merge_list = list(cleaned)
    seen = {
        (entry.get("flow_id") or "", entry.get("field") or "") for entry in _merge_list
    }
    for entry in renames:
        key = (entry.get("flow_id") or "", entry.get("field") or "")
        if key not in seen:
            _merge_list.append({"flow_id": key[0], "field": key[1]})
            seen.add(key)

    record = PendingStagedChanges(
        survey_id=survey_id,
        dimension="items",
        payload=ItemsPendingPayload(
            qids=qids,
            embedded_fields=_merge_list,
            workbook=workbook,
            filter_column=filter_column,
            filter_value=filter_value,
        ),
    )
    save_pending(record)


def handle_add_embedded_field(args: argparse.Namespace) -> None:
    from .sync_core import stage_add_embedded_field

    survey_id = _prompt_for_survey_id_api_if_needed(
        survey_id=getattr(args, "survey_id", None),
        args=args,
        message="Select a survey to stage an embedded data add:",
    )
    field = (args.field or "").strip()
    value = args.value
    flow_id = getattr(args, "flow_id", None)
    dry_run = bool(getattr(args, "dry_run", False))
    if not field:
        raise SystemExit("[add-embedded-field] ERROR: --field is required.")
    try:
        entry = stage_add_embedded_field(
            survey_id,
            field=field,
            value=value,
            flow_id=flow_id,
            dry_run=dry_run,
        )
    except ValueError as exc:
        raise SystemExit(f"[add-embedded-field] ERROR: {exc}") from exc
    if dry_run:
        print(
            f"[add-embedded-field] DRY RUN: Would add '{field}' to FlowID={entry.get('flow_id')}."
        )
        return
    _merge_embedded_pending(survey_id, [entry])
    print(
        f"[add-embedded-field] Staged field '{field}' in FlowID={entry.get('flow_id')}. "
        "Run 'qsync push' to upload SurveyFlow."
    )


def handle_remove_embedded_field(args: argparse.Namespace) -> None:
    from .sync_core import stage_remove_embedded_field

    survey_id = _prompt_for_survey_id_api_if_needed(
        survey_id=getattr(args, "survey_id", None),
        args=args,
        message="Select a survey to stage an embedded data removal:",
    )
    field = (args.field or "").strip()
    flow_id = getattr(args, "flow_id", None)
    dry_run = bool(getattr(args, "dry_run", False))
    if not field:
        raise SystemExit("[remove-embedded-field] ERROR: --field is required.")
    try:
        removed = stage_remove_embedded_field(
            survey_id, field=field, flow_id=flow_id, dry_run=dry_run
        )
    except ValueError as exc:
        raise SystemExit(f"[remove-embedded-field] ERROR: {exc}") from exc
    if not removed:
        print(
            f"[remove-embedded-field] No entries found for '{field}'; nothing staged."
        )
        return
    if dry_run:
        flow_list = ", ".join(
            sorted(
                {
                    entry.get("flow_id") or ""
                    for entry in removed
                    if entry.get("flow_id")
                }
            )
        )
        suffix = f" FlowID(s)={flow_list}." if flow_list else ""
        print(
            f"[remove-embedded-field] DRY RUN: Would remove '{field}' from {len(removed)} node(s)."
            f"{suffix}"
        )
        return
    _merge_embedded_pending(survey_id, removed)
    flow_list = ", ".join(
        sorted(
            {entry.get("flow_id") or "" for entry in removed if entry.get("flow_id")}
        )
    )
    suffix = f" FlowID(s)={flow_list}." if flow_list else ""
    print(
        f"[remove-embedded-field] Staged removal of '{field}' from {len(removed)} node(s)."
        f"{suffix} Run 'qsync push' to upload SurveyFlow."
    )


def handle_rename_embedded_field(args: argparse.Namespace) -> None:
    from .sync_core import stage_rename_embedded_field

    survey_id = _prompt_for_survey_id_api_if_needed(
        survey_id=getattr(args, "survey_id", None),
        args=args,
        message="Select a survey to stage an embedded data rename:",
    )
    old_field = (getattr(args, "from_field", None) or "").strip()
    new_field = (getattr(args, "to_field", None) or "").strip()
    flow_id = getattr(args, "flow_id", None)
    all_occurrences = bool(getattr(args, "all_occurrences", False))
    dry_run = bool(getattr(args, "dry_run", False))
    if not old_field:
        raise SystemExit("[rename-embedded-field] ERROR: --from is required.")
    if not new_field:
        raise SystemExit("[rename-embedded-field] ERROR: --to is required.")
    try:
        renamed = stage_rename_embedded_field(
            survey_id,
            old_field=old_field,
            new_field=new_field,
            flow_id=flow_id,
            all_occurrences=all_occurrences,
            dry_run=dry_run,
        )
    except ValueError as exc:
        raise SystemExit(f"[rename-embedded-field] ERROR: {exc}") from exc
    if dry_run:
        flow_list = ", ".join(
            sorted(
                {
                    entry.get("flow_id") or ""
                    for entry in renamed
                    if entry.get("flow_id")
                }
            )
        )
        suffix = f" FlowID(s)={flow_list}." if flow_list else ""
        print(
            f"[rename-embedded-field] DRY RUN: Would rename '{old_field}' -> '{new_field}' "
            f"in {len(renamed)} node(s).{suffix}"
        )
        return
    _merge_embedded_rename_pending(survey_id, renamed)
    flow_list = ", ".join(
        sorted(
            {entry.get("flow_id") or "" for entry in renamed if entry.get("flow_id")}
        )
    )
    suffix = f" FlowID(s)={flow_list}." if flow_list else ""
    print(
        f"[rename-embedded-field] Staged rename '{old_field}' -> '{new_field}' "
        f"in {len(renamed)} node(s).{suffix} "
        f"Run 'qsync push --survey-id {survey_id}' (or 'qsync push') to upload SurveyFlow."
    )


def handle_pull(args: argparse.Namespace) -> None:
    """Download a survey definition JSON to local cache."""
    raw_account = getattr(args, "account", None)
    explicit_default_account = (
        isinstance(raw_account, str) and raw_account.strip().lower() == "default"
    )
    if not explicit_default_account:
        explicit_default_account = (
            (os.environ.get("QSYNC_ACCOUNT") or "").strip().lower() == "default"
        )
    account = _resolve_account_from_args(args)
    # `--account default` must force legacy unscoped cache location even when
    # ambient account context is present in the process environment.
    dest_account_scope: str | None = "" if explicit_default_account else account
    dest_dir: Path | None = _resolve_pull_dest(
        _workspace_root(), dest_account_scope, args.dest
    )
    env = None
    if account:
        env = load_account_env(account, root=_workspace_root())

    from .survey_ref import format_survey_ref

    survey_ids = _normalize_survey_ids(getattr(args, "survey_id", None))
    if not survey_ids:
        survey_ids = _prompt_for_survey_ids_api_if_needed(
            survey_ids=None,
            args=args,
            message="Pick survey(s) to pull (cache JSON):",
            allow_multiple=True,
        )
    survey_ids = list(dict.fromkeys([sid.strip() for sid in survey_ids if sid.strip()]))

    failures: list[tuple[str, str]] = []
    for survey_id in survey_ids:
        print(
            f"[pull] Downloading survey definition for {format_survey_ref(survey_id)}..."
        )
        try:
            saved_path = download_survey_definition(
                survey_id, target_dir=dest_dir, env=env
            )
            print(f"[pull] {survey_id}: Saved to: {saved_path}")
        except Exception as e:
            failures.append((survey_id, str(e)))
            print(
                f"[pull] ERROR: Failed to download survey {format_survey_ref(survey_id)}: {e}"
            )

    if failures:
        sys.exit(1)


def _read_multiline_snippet_interactive(*, prompt: str) -> str:
    print(prompt)
    print("(Finish with an empty line; Ctrl-D cancels.)")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line == "":
            break
        lines.append(line)
    return "\n".join(lines)


def handle_prolific_auth(args: argparse.Namespace) -> None:
    """Set or append a Prolific authenticity-check snippet in SurveyOptions.Header."""
    from .qualtrics_client import (
        ensure_backup,
        publish_survey_definition,
        refresh_survey_cache,
    )
    from .survey_ref import format_survey_ref
    from .terminal_output import error, info, success, warn
    from .interactive_menu import select_from_list
    from .prolific_auth import (
        ProlificSnippetValidation,
        contains_prolific_qualtrics_script,
        excerpt,
        merge_header,
        normalize_html_snippet,
        redact_prolific_token,
        validate_prolific_auth_snippet,
    )

    def _flag(name: str) -> bool:
        value = getattr(args, name, False)
        return value if isinstance(value, bool) else False

    survey_id = _prompt_for_survey_id_api_if_needed(
        survey_id=getattr(args, "survey_id", None),
        args=args,
        message="Select a survey for prolific-auth:",
    )

    base_url, headers = get_client_config()
    resp = send_api_request(
        action="qsync.survey.options.fetch",
        method="GET",
        base_url=base_url,
        headers=headers,
        path=f"survey-definitions/{survey_id}/options",
        survey_id=survey_id,
        timeout=30,
    )
    options = resp.json().get("result", {}) or {}
    current_header = options.get("Header") or ""

    if _flag("print_current"):
        print(current_header)
        return

    # Resolve snippet source: --snippet, --file, stdin pipe, interactive paste.
    raw_snippet = getattr(args, "snippet", None)
    snippet_file = getattr(args, "file", None)
    if raw_snippet is None and snippet_file:
        raw_snippet = Path(str(snippet_file)).read_text(encoding="utf-8")
    if raw_snippet is None and not sys.stdin.isatty():
        raw_snippet = sys.stdin.read()
    if raw_snippet is None:
        raw_snippet = _read_multiline_snippet_interactive(
            prompt=(
                "[auth] Paste the Prolific authenticity-check HTML snippet to set in SurveyOptions.Header:"
            )
        )

    snippet = normalize_html_snippet(raw_snippet or "")
    if not snippet:
        error("[qsync:auth]", "ERROR: no snippet provided.")
        raise SystemExit(1)

    no_validate = _flag("no_validate")
    validation: ProlificSnippetValidation | None = None
    if not no_validate:
        validation = validate_prolific_auth_snippet(snippet)
        for warning_msg in validation.warnings:
            warn("[qsync:auth]", f"Warning: {warning_msg}")
        if not validation.ok:
            for err_msg in validation.errors:
                error("[qsync:auth]", f"Error: {err_msg}")
            if sys.stdin.isatty():
                from .terminal_output import prompt_yes_no

                if not prompt_yes_no(
                    "Snippet does not look like a Prolific authenticity check. Continue anyway?",
                    default=False,
                ):
                    raise SystemExit(1)
            else:
                error(
                    "[qsync:auth]",
                    "Non-interactive mode: pass --no-validate to proceed anyway.",
                )
                raise SystemExit(1)

    if current_header and snippet in current_header:
        success(
            "[qsync:auth]", "No-op: snippet is already present in the current header."
        )
        return

    has_any_header = bool(str(current_header).strip())
    has_prolific = contains_prolific_qualtrics_script(current_header)
    if not has_any_header:
        recommended_mode = "replace"
    elif has_prolific:
        recommended_mode = "replace"
    else:
        recommended_mode = "append"

    mode = getattr(args, "mode", None)
    assume_yes = _flag("yes")
    if mode is None and assume_yes:
        mode = recommended_mode
    if mode is None and sys.stdin.isatty():
        if has_any_header:
            info(
                "[qsync:auth]",
                "Current Header (preview): "
                + excerpt(redact_prolific_token(current_header), max_chars=240),
            )
        choices = []
        if recommended_mode == "replace":
            choices.append("Replace current Header (recommended)")
            if has_any_header:
                choices.append("Append to current Header")
            else:
                choices.append("Append to current Header (same as replace when empty)")
            choices.append("Cancel")
        else:
            choices.append("Append to current Header (recommended)")
            choices.append("Replace current Header")
            choices.append("Cancel")
        selection = select_from_list("How should qsync apply this snippet?", choices)
        if not selection or selection == "Cancel":
            info("[qsync:auth]", "Cancelled.")
            return
        mode = "append" if selection.startswith("Append") else "replace"
    if mode is None:
        error(
            "[qsync:auth]",
            "ERROR: non-interactive mode requires --mode {append,replace} (or --yes).",
        )
        raise SystemExit(1)
    if mode not in {"append", "replace"}:
        error("[qsync:auth]", "ERROR: --mode must be one of: append, replace")
        raise SystemExit(1)

    new_header = merge_header(current_header, snippet, mode=mode)

    dry_run = _flag("dry_run")
    if dry_run:
        info("[qsync:auth]", f"DRY RUN for {format_survey_ref(survey_id)}")
        if has_any_header:
            info(
                "[qsync:auth]",
                "Old Header (preview): "
                + excerpt(redact_prolific_token(current_header), max_chars=240),
            )
        info(
            "[qsync:auth]",
            "New Header (preview): "
            + excerpt(redact_prolific_token(new_header), max_chars=240),
        )
        return

    # Safety: ensure a full definition backup exists before mutating options.
    try:
        backup_path = ensure_backup(survey_id)
        info("[qsync:auth]", f"Backup ensured: {backup_path}")
    except Exception as exc:  # noqa: BLE001
        warn("[qsync:auth]", f"Warning: could not ensure backup: {exc}")

    options["Header"] = new_header
    send_api_request(
        action="qsync.survey.options.write",
        method="PUT",
        base_url=base_url,
        headers=headers,
        path=f"survey-definitions/{survey_id}/options",
        survey_id=survey_id,
        json=options,
        timeout=30,
        log_meta={
            "operation": "set_auth_header",
            "mode": mode,
            "had_header": has_any_header,
            "had_prolific": has_prolific,
        },
    )
    success(
        "[qsync:auth]",
        f"Updated SurveyOptions.Header for {format_survey_ref(survey_id)}.",
    )

    # Auto-publish so the definition change is immediately live.
    no_publish = _flag("no_publish")
    if not no_publish:
        try:
            payload = publish_survey_definition(
                survey_id,
                description="Prolific authenticity header update (auto-publish)",
                base_url=base_url,
                headers=headers,
            )
            version_id = (
                ((payload or {}).get("result") or {}).get("metadata") or {}
            ).get("versionID", "")
            suffix = f" (version {version_id})" if version_id else ""
            success("[qsync:auth]", f"Published survey definition{suffix}.")
        except Exception as exc:  # noqa: BLE001
            warn(
                "[qsync:auth]",
                f"Warning: auto-publish failed: {exc}. "
                "Run 'qsync survey publish' manually to make changes live.",
            )

    no_activate = _flag("no_activate")
    if not no_activate:
        try:
            status = _fetch_survey_status(base_url, headers, survey_id)
            is_active = status.get("isActive")
            if is_active is True:
                info("[qsync:auth]", "Survey is already active.")
            else:
                activation_response = send_api_request(
                    action="qsync.survey.activate",
                    method="PUT",
                    base_url=base_url,
                    headers=headers,
                    path=f"surveys/{survey_id}",
                    survey_id=survey_id,
                    json={"isActive": True},
                    timeout=30,
                    log_meta={
                        "operation": "activate",
                        "trigger": "prolific_auth",
                    },
                )
                if not activation_response.ok:
                    warn(
                        "[qsync:auth]",
                        "Warning: auto-activate request was not OK "
                        f"({activation_response.status_code} {activation_response.reason}). "
                        "Run 'qsync survey activate --survey-id <ID>' manually if needed.",
                    )
                else:
                    success("[qsync:auth]", "Activated survey.")
        except Exception as exc:  # noqa: BLE001
            warn(
                "[qsync:auth]",
                f"Warning: auto-activate failed: {exc}. "
                "Run 'qsync survey activate --survey-id <ID>' manually if needed.",
            )

    try:
        refresh_survey_cache(survey_id)
        success("[qsync:auth]", "Refreshed local survey cache.")
    except Exception as exc:  # noqa: BLE001
        warn("[qsync:auth]", f"Warning: could not refresh local cache: {exc}")


def handle_publish(args: argparse.Namespace) -> None:
    """Publish staged survey-definition changes by creating published version(s)."""
    survey_ids = _normalize_survey_ids(getattr(args, "survey_id", None))
    if not survey_ids:
        survey_ids = _prompt_for_survey_ids_api_if_needed(
            survey_ids=None,
            args=args,
            message="Select survey(s) to publish:",
            allow_multiple=True,
        )
    survey_ids = list(dict.fromkeys([sid.strip() for sid in survey_ids if sid.strip()]))
    if not survey_ids:
        raise SystemExit("[publish] Cancelled.")

    description = (args.description or "").strip()
    if not description:
        raise SystemExit("[publish] ERROR: --description must be non-empty")
    if len(description) > SURVEY_VERSION_DESCRIPTION_MAX_CHARS:
        raise SystemExit(
            f"[publish] ERROR: --description must be <= {SURVEY_VERSION_DESCRIPTION_MAX_CHARS} characters "
            f"(got {len(description)})."
        )

    dry_run = bool(getattr(args, "dry_run", False))
    retry_attempts = int(getattr(args, "retry_attempts", 1) or 1)
    if retry_attempts < 1:
        raise SystemExit("[publish] ERROR: --retry-attempts must be >= 1")

    base_url, headers = _get_client_config_for_args(args)
    batch_mode = len(survey_ids) > 1
    failures: list[tuple[str, str]] = []
    succeeded = 0

    for survey_id in survey_ids:
        print(
            f"[publish] POST survey-definitions/{survey_id}/versions "
            f"json={{'Description': {description!r}, 'Published': True}}"
        )
        if dry_run:
            print(f"[publish] {survey_id}: DRY-RUN: not calling Qualtrics.")
            succeeded += 1
            continue

        payload = None
        last_exc: Exception | None = None
        for attempt in range(1, retry_attempts + 1):
            try:
                payload = publish_survey_definition(
                    survey_id,
                    description=description,
                    published=True,
                    context={"origin": "qsync.cli_survey.publish"},
                    base_url=base_url,
                    headers=headers,
                )
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                if attempt >= retry_attempts:
                    break
                print(
                    f"[publish] {survey_id}: WARNING: publish failed on attempt {attempt}/{retry_attempts}: {exc}. "
                    "Next: verify credentials/permissions (run `qsync doctor --check-api`) and retry."
                )
                print(f"[publish] {survey_id}: Retrying…")
                time.sleep(2)

        if payload is None:
            message = (
                f"publish failed after {retry_attempts} attempt(s): {last_exc}"
            )
            if not batch_mode:
                raise SystemExit(f"[publish] ERROR: {message}")
            failures.append((survey_id, message))
            continue

        metadata = (payload.get("result") or {}).get("metadata") or {}
        version_id = metadata.get("versionID")
        version_num = metadata.get("versionNumber")
        if version_id or version_num:
            extra = []
            if version_num is not None:
                extra.append(f"version={version_num}")
            if version_id is not None:
                extra.append(f"id={version_id}")
            print(f"[publish] {survey_id}: OK: " + " ".join(extra))
        else:
            print(f"[publish] {survey_id}: OK")
        succeeded += 1

    if failures:
        print(
            f"[publish] Summary: {succeeded} succeeded, {len(failures)} failed."
        )
        for survey_id, message in failures:
            print(f"[publish] {survey_id}: ERROR: {message}")
        raise SystemExit(1)

    from .terminal_output import log_confirmation

    log_confirmation("[publish]")


def _fetch_survey_status(
    base_url: str, headers: Dict[str, str], survey_id: str
) -> Dict[str, Any]:
    """Fetch the current survey status payload from Qualtrics."""
    resp = send_api_request(
        action="qsync.survey.fetch.status",
        method="GET",
        base_url=base_url,
        headers=headers,
        path=f"surveys/{survey_id}",
        log_event=False,
        timeout=30,
    )
    result = resp.json().get("result")
    if not result:
        raise ValueError(f"Survey {survey_id} missing 'result' payload")
    return result


def _load_survey_ids_from_file(path: str) -> list[str]:
    file_path = Path(path)
    if not file_path.exists():
        raise SystemExit(f"[activate] ERROR: Survey IDs file not found: {file_path}")

    content = file_path.read_text(encoding="utf-8").strip()
    if not content:
        return []

    if file_path.suffix.lower() == ".csv":
        ids: list[str] = []
        with file_path.open(newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            rows = list(reader)
        if not rows:
            return []
        header = [c.strip().lower() for c in rows[0]]
        col_index = None
        for name in ("survey_id", "surveyid", "id"):
            if name in header:
                col_index = header.index(name)
                break
        if col_index is not None:
            for row in rows[1:]:
                if col_index < len(row):
                    sid = (row[col_index] or "").strip()
                    if sid:
                        ids.append(sid)
        else:
            for row in rows:
                if not row:
                    continue
                sid = (row[0] or "").strip()
                if sid and sid.lower() != "survey_id":
                    ids.append(sid)
        return ids

    return [line.strip() for line in content.splitlines() if line.strip()]


def _should_require_force_live() -> bool:
    value = os.environ.get("QSYNC_ACTIVATION_REQUIRE_FORCE_LIVE")
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _auto_confirm_enabled() -> bool:
    value = os.environ.get("QSYNC_ACTIVATION_AUTO_CONFIRM")
    if value is None:
        return True
    return value.strip().lower() not in {"0", "false", "no", "n", "off"}


def _activation_error_message(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status == 401:
        return "Unauthorized (401). Check QUALTRICS_API_KEY and re-run `qsync doctor`."
    if status == 403:
        return "Forbidden (403). Verify API permissions for this survey."
    if status == 404:
        return "Not found (404). Verify the Survey ID and inventory."
    if status == 429:
        return (
            "Rate limited (429). Wait and retry; consider reducing parallel requests."
        )
    if status == 500:
        return "Server error (500). Retry later."
    if status == 503:
        return "Service unavailable (503). Retry later."
    if status is not None:
        reason = getattr(response, "reason", "")
        return f"HTTP {status} {reason}".strip()
    return str(exc)


def _handle_activation(args: argparse.Namespace, *, target_active: bool) -> None:
    from .terminal_output import dim, error, header, info, success, warn
    from .terminal_colors import Colors, colored

    verb = "activate" if target_active else "deactivate"
    survey_ids = _normalize_survey_ids(getattr(args, "survey_id", None))

    ids_file = getattr(args, "survey_ids_file", None)
    if isinstance(ids_file, str) and ids_file:
        survey_ids.extend(_load_survey_ids_from_file(ids_file))

    if not survey_ids:
        # Offer interactive selection for one or more surveys.
        try:
            survey_ids = _prompt_for_survey_ids_api_if_needed(
                survey_ids=None,
                args=args,
                message=f"Select a survey to {verb}:",
                allow_multiple=True,
            )
        except SystemExit:
            raise SystemExit(
                f"[{verb}] ERROR: --survey-id is required (or provide --survey-ids-file)"
            )

    def _flag(name: str) -> bool:
        value = getattr(args, name, False)
        if isinstance(value, bool):
            return value
        return False

    def _maybe_str(name: str) -> str:
        value = getattr(args, name, "")
        return value.strip() if isinstance(value, str) else ""

    def _maybe_int(name: str, default: int) -> int:
        value = getattr(args, name, default)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str):
            value = value.strip()
            if value.isdigit():
                return int(value)
        return default

    dry_run = _flag("dry_run")
    force_live = _flag("force_live")
    yes = _flag("yes")
    publish_description = _maybe_str("publish_description")
    publish_after = _flag("publish") or bool(publish_description)
    show_versions = _flag("show_versions")
    versions_limit = _maybe_int("versions_limit", 5)
    show_owner = _flag("show_owner")

    if _should_require_force_live() and not force_live and not dry_run:
        raise SystemExit(
            f"[{verb}] ERROR: --force-live is required by QSYNC_ACTIVATION_REQUIRE_FORCE_LIVE"
        )

    if not _auto_confirm_enabled():
        yes = False

    batch_mode = len(survey_ids) > 1
    base_url, headers = _get_client_config_for_args(args)

    if batch_mode and not yes and not dry_run:
        if not sys.stdin.isatty():
            raise SystemExit(
                f"[{verb}] Confirmation required but stdin is not interactive. "
                "Re-run with --yes to proceed."
            )
        try:
            from qsync.interactive_menu import confirm

            if not confirm(
                f"{verb.capitalize()} {len(survey_ids)} surveys?", default=True
            ):
                raise SystemExit(f"[{verb}] Aborted.")
        except Exception:
            confirm = (
                input(f"{verb.capitalize()} {len(survey_ids)} surveys? [Y/n]: ")
                .strip()
                .lower()
            )
            if confirm and confirm != "y":
                raise SystemExit(f"[{verb}] Aborted.")

    results: list[dict[str, Any]] = []
    target_label = "active" if target_active else "inactive"
    target_label_colored = colored(
        target_label, Colors.GREEN if target_active else Colors.YELLOW
    )

    for survey_id in survey_ids:
        start_time = time.monotonic()
        survey_id = (survey_id or "").strip()
        if not survey_id:
            continue
        from .survey_ref import format_survey_ref

        survey_ref = format_survey_ref(survey_id)

        try:
            ensure_unlocked(survey_id)
        except (SurveyLockedError, RuntimeError) as exc:
            from .push_logger import log_push_event
            from .survey_lock import ERROR_ID_SURVEY_LOCKED

            log_push_event(
                action="qsync.survey.locked.blocked",
                method="LOCAL",
                path=f"cli_survey.{verb}",
                survey_id=survey_id,
                status=None,
                error={"error_id": ERROR_ID_SURVEY_LOCKED, "message": str(exc)},
            )
            lines = str(exc).splitlines() or ["Survey is locked."]
            error(f"[{verb}]", f"{survey_ref}: {lines[0]}")
            for ln in lines[1:]:
                print(f"  {ln}", file=sys.stderr)
            results.append(
                {"survey_id": survey_id, "status": "failed", "reason": str(exc)}
            )
            if not batch_mode:
                raise SystemExit(f"[{verb}] ERROR: {exc}") from exc
            continue

        ctx = None
        try:
            ctx = load_push_context(survey_id, base_url=base_url, headers=headers)
            survey_ref = format_survey_ref(survey_id, getattr(ctx, "survey_name", None))
            if ctx.counts_unknown:
                if not force_live and not dry_run:
                    raise SystemExit(
                        f"[{verb}] Unable to verify response counts for {survey_ref}. "
                        "Refresh inventory and retry or pass --force-live after manual review."
                    )
                if force_live:
                    warn(
                        f"[{verb}]",
                        f"{survey_ref}: Response counts unknown; proceeding due to --force-live.",
                    )
                elif dry_run:
                    warn(
                        f"[{verb}]",
                        f"{survey_ref}: Response counts unknown; dry-run continues without counts check.",
                    )
            if ctx.response_count > 0 and not force_live and not dry_run:
                raise SystemExit(
                    f"[{verb}] {survey_ref} has {ctx.response_count} finished response(s). "
                    "Re-run with --force-live after double-checking."
                )
        except Exception as exc:
            if dry_run:
                warn(
                    f"[{verb}]",
                    f"{survey_ref}: NOTE: Could not load push context: {exc}",
                )
            else:
                if batch_mode:
                    error(f"[{verb}]", f"{survey_ref}: {exc}")
                    results.append(
                        {"survey_id": survey_id, "status": "failed", "reason": str(exc)}
                    )
                    continue
                raise

        try:
            status = _fetch_survey_status(base_url, headers, survey_id)
        except Exception as exc:
            error(f"[{verb}]", f"{survey_ref}: Failed to fetch status: {exc}")
            results.append(
                {"survey_id": survey_id, "status": "failed", "reason": str(exc)}
            )
            if not batch_mode:
                raise SystemExit(f"[{verb}] ERROR: {exc}") from exc
            continue

        survey_name = status.get("name") or survey_id
        survey_ref = format_survey_ref(
            survey_id, str(survey_name or "").strip() or None
        )
        current_active = status.get("isActive")
        if current_active is None:
            msg = f"Survey {survey_id} missing isActive field"
            error(f"[{verb}]", msg)
            results.append({"survey_id": survey_id, "status": "failed", "reason": msg})
            if not batch_mode:
                raise SystemExit(f"[{verb}] ERROR: {msg}")
            continue

        current_label = "active" if current_active else "inactive"
        header(f"[{verb}]", f"{survey_name} ({survey_id})")
        dim(f"[{verb}]", "-" * 48)
        if ctx is not None:
            info(f"[{verb}]", ctx.describe_counts())
        creation_date = status.get("creationDate") or status.get("creationDateGMT")
        modified_date = status.get("lastModifiedDate") or status.get("lastModified")
        if creation_date:
            info(f"[{verb}]", f"Created: {creation_date}")
        if modified_date:
            info(f"[{verb}]", f"Last modified: {modified_date}")
        if show_owner:
            owner = status.get("ownerId") or status.get("owner")
            if owner:
                info(f"[{verb}]", f"Owner: {owner}")
        current_label_colored = colored(
            current_label, Colors.GREEN if current_active else Colors.YELLOW
        )
        info(f"[{verb}]", f"Current status: {current_label_colored}")
        info(f"[{verb}]", f"Target status: {target_label_colored}")
        if dry_run:
            dim(f"[{verb}]", "DRY-RUN: no API requests will be made.")

        if show_versions:
            try:
                data = list_survey_versions(
                    survey_id, base_url=base_url, headers=headers
                )
                versions = data.get("versions") or []
                if versions_limit:
                    versions = versions[:versions_limit]
                if versions:
                    info(f"[{verb}]", "Versions:")
                    for meta in versions:
                        num = meta.get("versionNumber")
                        ver_id = meta.get("versionID")
                        pub = "Y" if meta.get("published") else "N"
                        cur = "*" if meta.get("current_published") else ""
                        created = (meta.get("creationDate") or "").strip()[:19]
                        desc = (meta.get("description") or "").strip()
                        info(
                            f"[{verb}]",
                            f"- v{num} {pub}{cur} {created} {desc} (id={ver_id})",
                        )
            except Exception as exc:
                warn(f"[{verb}]", f"{survey_ref}: Unable to list versions: {exc}")

        if current_active == target_active:
            info(
                f"[{verb}]", f"{survey_ref}: already {target_label}; no changes needed."
            )
            results.append(
                {
                    "survey_id": survey_id,
                    "status": "skipped",
                    "reason": "already in target state",
                }
            )
            continue

        if not yes and not dry_run and not batch_mode:
            if not sys.stdin.isatty():
                raise SystemExit(
                    f"[{verb}] Confirmation required but stdin is not interactive. "
                    "Re-run with --yes to proceed."
                )
            try:
                from qsync.interactive_menu import confirm

                if not confirm(
                    f"{verb.capitalize()} survey {survey_ref}?", default=True
                ):
                    raise SystemExit(f"[{verb}] Aborted.")
            except Exception:
                confirm = (
                    input(f"{verb.capitalize()} survey {survey_ref}? [Y/n]: ")
                    .strip()
                    .lower()
                )
                if confirm and confirm != "y":
                    raise SystemExit(f"[{verb}] Aborted.")

        if dry_run:
            info(
                f"[{verb}]",
                f"{survey_ref}: DRY-RUN: would set isActive={target_active}",
            )
            elapsed = time.monotonic() - start_time
            dim(f"[{verb}]", f"{survey_ref}: DRY-RUN complete in {elapsed:.1f}s.")
            results.append(
                {"survey_id": survey_id, "status": "dry_run", "reason": "dry-run"}
            )
            continue

        try:
            response = send_api_request(
                action=f"qsync.survey.{verb}",
                method="PUT",
                base_url=base_url,
                headers=headers,
                path=f"surveys/{survey_id}",
                survey_id=survey_id,
                json={"isActive": target_active},
                log_meta={
                    "operation": verb,
                    "field": "isActive",
                    "previous": current_active,
                    "next": target_active,
                    "response_count": getattr(ctx, "response_count", None),
                    "preview_count": getattr(ctx, "preview_count", None),
                    "forced": force_live,
                    "counts_source": getattr(ctx, "counts_source", None),
                    "counts_unknown": getattr(ctx, "counts_unknown", None),
                },
            )
            if not response.ok:
                raise SystemExit(
                    f"[{verb}] ERROR: {response.status_code} {response.reason}"
                )
        except Exception as exc:
            msg = str(exc)
            error(f"[{verb}]", f"{survey_ref}: {_activation_error_message(exc)}")
            results.append({"survey_id": survey_id, "status": "failed", "reason": msg})
            if not batch_mode:
                raise
            continue

        try:
            updated = _fetch_survey_status(base_url, headers, survey_id)
            updated_active = updated.get("isActive")
            if updated_active is not None and updated_active != target_active:
                warn(
                    f"[{verb}]",
                    f"{survey_ref}: WARNING: status did not update as expected. "
                    "Next: check Qualtrics UI and retry the command if needed.",
                )
        except Exception as exc:
            warn(f"[{verb}]", f"{survey_ref}: NOTE: Unable to verify new status: {exc}")

        if publish_after:
            if not publish_description:
                publish_description = f"qsync {verb}: {survey_id}"
            if len(publish_description) > SURVEY_VERSION_DESCRIPTION_MAX_CHARS:
                publish_description = publish_description[
                    :SURVEY_VERSION_DESCRIPTION_MAX_CHARS
                ]
            publish_survey_definition(
                survey_id,
                description=publish_description,
                published=True,
                context={"origin": f"qsync.cli_survey.{verb}", "survey_id": survey_id},
                base_url=base_url,
                headers=headers,
            )
            info(f"[{verb}]", f"{survey_ref}: Published survey definition.")

        elapsed = time.monotonic() - start_time
        success(f"[{verb}]", f"{survey_ref}: OK — now {target_label} ({elapsed:.1f}s)")
        results.append(
            {"survey_id": survey_id, "status": "updated", "reason": "updated"}
        )

    if batch_mode:
        updated = sum(1 for r in results if r["status"] == "updated")
        skipped = sum(1 for r in results if r["status"] in {"skipped", "dry_run"})
        failed = sum(1 for r in results if r["status"] == "failed")
        info(
            f"[{verb}]",
            f"Summary: {updated} updated, {skipped} skipped, {failed} failed.",
        )
        if failed:
            for r in results:
                if r["status"] == "failed":
                    warn(f"[{verb}]", f"{r['survey_id']}: {r['reason']}")


def handle_activate(args: argparse.Namespace) -> None:
    """Activate a survey (set isActive=true)."""
    _handle_activation(args, target_active=True)


def handle_deactivate(args: argparse.Namespace) -> None:
    """Deactivate a survey (set isActive=false)."""
    _handle_activation(args, target_active=False)


def handle_versions(args: argparse.Namespace) -> None:
    """List survey-definition versions for a survey."""
    survey_id = _prompt_for_survey_id_api_if_needed(
        survey_id=getattr(args, "survey_id", None),
        args=args,
        message="Select a survey to list versions:",
    )

    limit = getattr(args, "limit", None)
    as_json = bool(getattr(args, "json", False))

    base_url, headers = _get_client_config_for_args(args)
    data = list_survey_versions(survey_id, base_url=base_url, headers=headers)
    versions = data.get("versions") or []
    current_id = data.get("current_published_version_id")

    if limit is not None:
        versions = versions[: int(limit)]

    if as_json:
        print(
            json.dumps(
                {
                    "survey_id": survey_id,
                    "current_published_version_id": current_id,
                    "versions": versions,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    print(
        f"[versions] survey_id={survey_id} versions={len(data.get('versions') or [])}"
    )
    if current_id:
        print(f"[versions] current_published_version_id={current_id}")

    if not versions:
        print("[versions] No versions returned.")
        return

    print(f"\n{'Ver':>4}  {'Pub':>3}  {'Cur':>3}  {'Created':<20}  Description")
    print("-" * 90)
    for meta in versions:
        version_num = meta.get("versionNumber")
        version_id = meta.get("versionID")
        published = meta.get("published") is True
        current = meta.get("current_published") is True
        created = (meta.get("creationDate") or "").strip()
        created_short = created[:19] if created else ""
        desc = (meta.get("description") or "").strip()

        ver_label = f"{version_num}" if version_num is not None else "?"
        pub_label = "Y" if published else "N"
        cur_label = "*" if current else ""
        # Keep IDs in output without forcing them into the table columns.
        suffix = f" (id={version_id})" if version_id else ""
        print(
            f"{ver_label:>4}  {pub_label:>3}  {cur_label:>3}  {created_short:<20}  {desc}{suffix}"
        )


def handle_version_fetch(args: argparse.Namespace) -> None:
    """Fetch a specific survey-definition version."""
    survey_id = _prompt_for_survey_id_api_if_needed(
        survey_id=getattr(args, "survey_id", None),
        args=args,
        message="Select a survey to fetch a version:",
    )
    version_id = (args.version_id or "").strip()
    if not version_id:
        raise SystemExit("[version-fetch] ERROR: --version-id is required")

    fmt = (getattr(args, "format", None) or "json").strip().lower()
    out_path = getattr(args, "output", None)
    as_json = bool(getattr(args, "json", False))

    base_url, headers = _get_client_config_for_args(args)
    payload = fetch_survey_version(
        survey_id,
        version_id=version_id,
        fmt=fmt,
        base_url=base_url,
        headers=headers,
    )

    if out_path:
        dest = Path(out_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[version-fetch] Saved to: {dest.resolve()}")

    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    result = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(result, dict) and isinstance(result.get("Questions"), dict):
        q_count = len(result["Questions"])
        print(
            f"[version-fetch] OK: survey_id={survey_id} version_id={version_id} format={fmt} questions={q_count}"
        )
        return

    if isinstance(result, dict):
        keys = sorted(result.keys())
        head = keys[:12]
        extra = "" if len(keys) <= 12 else f" (+{len(keys) - 12} keys)"
        print(
            f"[version-fetch] OK: survey_id={survey_id} version_id={version_id} format={fmt} result_keys={head}{extra}"
        )
        return

    print(
        f"[version-fetch] OK: survey_id={survey_id} version_id={version_id} format={fmt}"
    )


def handle_rollback(args: argparse.Namespace) -> None:
    """Rollback one or more questions to a historical version, then publish."""
    survey_id = _prompt_for_survey_id_api_if_needed(
        survey_id=getattr(args, "survey_id", None),
        args=args,
        message="Select a survey to rollback questions:",
    )
    version_id = (args.version_id or "").strip()
    qids_raw = (args.question_id or "").strip()
    if not version_id:
        raise SystemExit("[rollback] ERROR: --version-id is required")
    if not qids_raw:
        raise SystemExit("[rollback] ERROR: --question-id is required")

    qids = [q.strip() for q in qids_raw.split(",") if q.strip()]
    if not qids:
        raise SystemExit("[rollback] ERROR: No QIDs parsed from --question-id")

    dry_run = bool(getattr(args, "dry_run", False))
    no_publish = bool(getattr(args, "no_publish", False))
    force_live = bool(getattr(args, "force_live", False))
    yes = bool(getattr(args, "yes", False))

    base_url, headers = _get_client_config_for_args(args)

    # Push safeguards (same spirit as push-question / push).
    try:
        ctx = load_push_context(survey_id, base_url=base_url, headers=headers)
        print(f"[rollback] Survey: {ctx.survey_name}")
        print(f"[rollback] {ctx.describe_counts()}")

        if ctx.counts_unknown and not force_live and not dry_run:
            raise SystemExit(
                f"[rollback] Unable to verify response counts for {survey_id}. "
                "Refresh inventory and retry or pass --force-live after manual review."
            )
        if ctx.response_count > 0 and not force_live and not dry_run:
            raise SystemExit(
                f"[rollback] Survey has {ctx.response_count} finished response(s). "
                "Re-run with --force-live after double-checking."
            )
        if (
            (ctx.response_count > 0 or ctx.preview_count > 0)
            and not yes
            and not dry_run
        ):
            if not sys.stdin.isatty():
                raise SystemExit(
                    "[rollback] Confirmation required but stdin is not interactive. "
                    "Re-run with --yes to proceed."
                )
            try:
                from qsync.interactive_menu import confirm

                if not confirm(
                    f"Rollback {len(qids)} question(s) on {survey_id} to {version_id}?",
                    default=True,
                ):
                    raise SystemExit("[rollback] Aborted.")
            except Exception:
                confirm = (
                    input(
                        f"Rollback {len(qids)} question(s) on {survey_id} to {version_id}? [Y/n]: "
                    )
                    .strip()
                    .lower()
                )
                if confirm and confirm != "y":
                    raise SystemExit("[rollback] Aborted.")
    except Exception as exc:
        if dry_run:
            print(f"[rollback] NOTE: Could not load push context: {exc}")
        else:
            raise

    # Fetch historical definition.
    historical = fetch_survey_version(
        survey_id,
        version_id=version_id,
        fmt="json",
        base_url=base_url,
        headers=headers,
    )

    print(f"[rollback] Target version: {version_id}")
    print(f"[rollback] QIDs: {', '.join(qids)}")

    if dry_run:
        for qid in qids:
            try:
                _extract_question(historical, qid)
            except KeyError:
                print(f"[rollback] Missing QID in version payload: {qid}")
        print("[rollback] DRY-RUN: no writes performed.")
        return

    for qid in qids:
        question_payload = _extract_question(historical, qid)
        send_api_request(
            action="qsync.survey.rollback.question.put",
            method="PUT",
            base_url=base_url,
            headers=headers,
            path=f"survey-definitions/{survey_id}/questions/{qid}",
            survey_id=survey_id,
            log_meta={
                "operation": "rollback",
                "source_version_id": version_id,
                "question_id": qid,
                "changed_qids": qids,
            },
            json=question_payload,
        )

    if no_publish:
        print(f"[rollback] Restored {len(qids)} question(s) (not published).")
        return

    desc = (getattr(args, "description", None) or "").strip()
    if not desc:
        desc = make_publish_description(
            operation="rollback",
            changed_qids=qids,
            count=len(qids),
            label=f"from {version_id}",
            max_chars=SURVEY_VERSION_DESCRIPTION_MAX_CHARS,
        )
    if len(desc) > SURVEY_VERSION_DESCRIPTION_MAX_CHARS:
        raise SystemExit(
            f"[rollback] ERROR: publish description must be <= {SURVEY_VERSION_DESCRIPTION_MAX_CHARS} characters "
            f"(got {len(desc)})."
        )

    publish_survey_definition(
        survey_id,
        description=desc,
        published=True,
        context={
            "origin": "qsync.cli_survey.rollback",
            "source_version_id": version_id,
            "changed_qids": qids,
        },
        base_url=base_url,
        headers=headers,
    )
    print(f"[rollback] Restored {len(qids)} question(s) and published {survey_id}.")


def _extract_question(payload: dict, question_id: str) -> dict:
    """Extract a single question from survey definition payload."""
    container = payload.get("result", payload)
    questions = container.get("Questions")
    if not questions or question_id not in questions:
        raise KeyError(f"Question {question_id} not found in survey definition")
    return questions[question_id]


def _fetch_remote_question(
    base_url: str, survey_id: str, question_id: str, headers: Dict[str, str]
) -> dict:
    """Fetch the current question definition from Qualtrics."""
    resp = send_api_request(
        action="qsync.survey.fetch.question",
        method="GET",
        base_url=base_url,
        headers=headers,
        path=f"survey-definitions/{survey_id}/questions/{question_id}",
        log_event=False,
        timeout=60,
    )
    result = resp.json().get("result")
    if not result:
        raise ValueError("Remote question payload missing 'result'")
    return result


def _format_question(obj: dict) -> str:
    """Format question as pretty JSON for diffing."""
    return json.dumps(obj, indent=2, sort_keys=True)


def _normalize_question_ids(value: object) -> list[str]:
    ids: list[str] = []
    if value is None:
        return ids
    if isinstance(value, list):
        values = value
    else:
        values = [value]
    for item in values:
        if item is None:
            continue
        for token in str(item).split(","):
            qid = token.strip()
            if qid:
                ids.append(qid)
    deduped: list[str] = []
    seen: set[str] = set()
    for qid in ids:
        if qid in seen:
            continue
        seen.add(qid)
        deduped.append(qid)
    return deduped


def _is_trash_block(block: Mapping[str, Any]) -> bool:
    return str((block or {}).get("Type") or "").strip().lower() == "trash"


def _block_elements_ref(block: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    elements = block.get("BlockElements")
    if isinstance(elements, list):
        return elements, "BlockElements"
    elements = block.get("Elements")
    if isinstance(elements, list):
        return elements, "Elements"
    block["BlockElements"] = []
    return block["BlockElements"], "BlockElements"


def _question_element_index(
    block: Mapping[str, Any],
    question_id: str,
) -> int | None:
    elements = (
        block.get("BlockElements")
        if isinstance(block.get("BlockElements"), list)
        else block.get("Elements")
    )
    if not isinstance(elements, list):
        return None
    for idx, elem in enumerate(elements):
        if not isinstance(elem, dict):
            continue
        if str(elem.get("QuestionID") or "").strip() == question_id:
            return idx
    return None


def _find_question_blocks(
    definition: Mapping[str, Any],
    question_id: str,
    *,
    include_trash: bool = False,
) -> list[tuple[str, int]]:
    blocks = definition.get("Blocks")
    if not isinstance(blocks, dict):
        return []
    matches: list[tuple[str, int]] = []
    for block_id, block_payload in blocks.items():
        if not isinstance(block_payload, dict):
            continue
        if not include_trash and _is_trash_block(block_payload):
            continue
        idx = _question_element_index(block_payload, question_id)
        if idx is not None:
            matches.append((str(block_id), idx))
    return matches


def _ensure_single_question_block(
    definition: Mapping[str, Any],
    question_id: str,
    *,
    include_trash: bool = False,
) -> tuple[str, int]:
    matches = _find_question_blocks(
        definition, question_id, include_trash=include_trash
    )
    if not matches:
        raise ValueError(f"QID {question_id} is not present in any eligible block.")
    if len(matches) > 1:
        block_ids = ", ".join(sorted({block_id for block_id, _ in matches}))
        raise ValueError(
            f"QID {question_id} appears in multiple blocks ({block_ids}); "
            "use --target-block-id to disambiguate."
        )
    return matches[0]


def _flow_ordered_block_ids(definition: Mapping[str, Any]) -> list[str]:
    flow = definition.get("SurveyFlow")
    if isinstance(flow, dict):
        root = flow.get("Flow")
    elif isinstance(flow, list):
        root = flow
    else:
        root = []

    ordered: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                _walk(child)
            return
        if not isinstance(node, dict):
            return
        node_type = str(node.get("Type") or "").strip()
        if node_type in {"Block", "Standard"}:
            block_id = str(node.get("ID") or "").strip()
            if block_id and block_id not in ordered:
                ordered.append(block_id)
        for key in ("Flow", "Then", "Else", "ElseFlow"):
            _walk(node.get(key))

    _walk(root)
    return ordered


def _first_eligible_block_id(definition: Mapping[str, Any]) -> str:
    blocks = definition.get("Blocks")
    if not isinstance(blocks, dict):
        raise ValueError("Survey definition has no Blocks map.")

    for block_id in _flow_ordered_block_ids(definition):
        block = blocks.get(block_id)
        if isinstance(block, dict) and not _is_trash_block(block):
            return str(block_id)

    for block_id, block in blocks.items():
        if isinstance(block, dict) and not _is_trash_block(block):
            return str(block_id)
    raise ValueError("No non-trash block found in this survey.")


def _resolve_target_block_id(
    definition: Mapping[str, Any],
    *,
    target_block_id: str | None,
    after_qid: str | None,
    before_qid: str | None,
    fallback_qid: str | None = None,
) -> str:
    explicit = (target_block_id or "").strip() or None
    after = (after_qid or "").strip() or None
    before = (before_qid or "").strip() or None
    fallback = (fallback_qid or "").strip() or None

    if after and before:
        raise ValueError("Use only one of --after-qid or --before-qid.")

    blocks = definition.get("Blocks")
    if not isinstance(blocks, dict):
        raise ValueError("Survey definition has no Blocks map.")

    if after:
        block_id, _ = _ensure_single_question_block(definition, after)
        if explicit and explicit != block_id:
            raise ValueError(
                f"--target-block-id ({explicit}) conflicts with --after-qid block ({block_id})."
            )
        return block_id

    if before:
        block_id, _ = _ensure_single_question_block(definition, before)
        if explicit and explicit != block_id:
            raise ValueError(
                f"--target-block-id ({explicit}) conflicts with --before-qid block ({block_id})."
            )
        return block_id

    if explicit:
        block = blocks.get(explicit)
        if not isinstance(block, dict):
            raise ValueError(f"Block {explicit} was not found in this survey.")
        if _is_trash_block(block):
            raise ValueError(f"Block {explicit} is Trash and cannot receive questions.")
        return explicit

    if fallback:
        block_id, _ = _ensure_single_question_block(definition, fallback)
        return block_id

    return _first_eligible_block_id(definition)


def _resolve_insert_index(
    definition: Mapping[str, Any],
    *,
    block_id: str,
    after_qid: str | None,
    before_qid: str | None,
    position: str,
) -> int:
    blocks = definition.get("Blocks")
    if not isinstance(blocks, dict):
        raise ValueError("Survey definition has no Blocks map.")
    block = blocks.get(block_id)
    if not isinstance(block, dict):
        raise ValueError(f"Block {block_id} was not found in this survey.")
    elements, _ = _block_elements_ref(block)

    after = (after_qid or "").strip() or None
    before = (before_qid or "").strip() or None

    if after:
        location_block_id, idx = _ensure_single_question_block(definition, after)
        if location_block_id != block_id:
            raise ValueError(
                f"QID {after} is in block {location_block_id}, not {block_id}."
            )
        return idx + 1
    if before:
        location_block_id, idx = _ensure_single_question_block(definition, before)
        if location_block_id != block_id:
            raise ValueError(
                f"QID {before} is in block {location_block_id}, not {block_id}."
            )
        return idx
    if (position or "").strip().lower() == "prepend":
        return 0
    return len(elements)


def _parse_insert_index_override(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = int(text)
    except (TypeError, ValueError) as exc:
        raise ValueError("--insert-index must be an integer >= 0.") from exc
    if parsed < 0:
        raise ValueError("--insert-index must be >= 0.")
    return parsed


def _resolve_insert_index_with_override(
    definition: Mapping[str, Any],
    *,
    block_id: str,
    after_qid: str | None,
    before_qid: str | None,
    position: str,
    insert_index_override: int | None,
) -> int:
    if insert_index_override is None:
        return _resolve_insert_index(
            definition,
            block_id=block_id,
            after_qid=after_qid,
            before_qid=before_qid,
            position=position,
        )

    blocks = definition.get("Blocks")
    if not isinstance(blocks, dict):
        raise ValueError("Survey definition has no Blocks map.")
    block = blocks.get(block_id)
    if not isinstance(block, dict):
        raise ValueError(f"Block {block_id} was not found in this survey.")
    elements, _ = _block_elements_ref(block)
    if insert_index_override > len(elements):
        raise ValueError(
            f"--insert-index {insert_index_override} exceeds block length {len(elements)}."
        )
    return insert_index_override


def _adjust_insert_index_after_qid_removal(
    definition: Mapping[str, Any],
    *,
    block_id: str,
    insert_index: int,
    qids_to_remove: Iterable[str],
) -> int:
    blocks = definition.get("Blocks")
    if not isinstance(blocks, dict):
        return insert_index
    block = blocks.get(block_id)
    if not isinstance(block, dict):
        return insert_index

    qid_set = {str(qid).strip() for qid in qids_to_remove if str(qid).strip()}
    if not qid_set:
        return insert_index

    elements = (
        block.get("BlockElements")
        if isinstance(block.get("BlockElements"), list)
        else block.get("Elements")
    )
    if not isinstance(elements, list):
        return insert_index

    bounded = max(0, min(int(insert_index), len(elements)))
    removed_before = 0
    for idx, elem in enumerate(elements):
        if idx >= bounded:
            break
        if not isinstance(elem, dict):
            continue
        if str(elem.get("Type") or "").strip() != "Question":
            continue
        qid = str(elem.get("QuestionID") or "").strip()
        if qid in qid_set:
            removed_before += 1
    return max(0, bounded - removed_before)


def _build_insert_elements(
    *,
    qids: list[str],
    page_break_mode: str = "none",
) -> list[dict[str, Any]]:
    mode = str(page_break_mode or "none").strip().lower()
    if mode not in {"none", "before", "after", "between"}:
        raise ValueError("page_break_mode must be one of: none, before, after, between.")

    question_elements = [
        {"Type": "Question", "QuestionID": str(qid)}
        for qid in qids
        if str(qid).strip()
    ]
    if not question_elements:
        return []

    if mode == "none":
        return question_elements
    if mode == "before":
        return [{"Type": "Page Break"}, *question_elements]
    if mode == "after":
        return [*question_elements, {"Type": "Page Break"}]

    # between
    out: list[dict[str, Any]] = []
    for idx, elem in enumerate(question_elements):
        if idx > 0:
            out.append({"Type": "Page Break"})
        out.append(elem)
    return out


def _normalize_element_indices(value: object) -> list[int]:
    values: list[int] = []
    if value is None:
        return values
    items = value if isinstance(value, list) else [value]
    for item in items:
        if item is None:
            continue
        for token in str(item).split(","):
            text = token.strip()
            if not text:
                continue
            try:
                idx = int(text)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid element index '{text}'.") from exc
            if idx < 0:
                raise ValueError("Element indices must be >= 0.")
            values.append(idx)
    deduped: list[int] = []
    seen: set[int] = set()
    for idx in values:
        if idx in seen:
            continue
        seen.add(idx)
        deduped.append(idx)
    return deduped


def _remove_qids_from_all_blocks(
    definition: Mapping[str, Any],
    qids: Iterable[str],
) -> set[str]:
    qid_set = {str(qid).strip() for qid in qids if str(qid).strip()}
    if not qid_set:
        return set()
    blocks = definition.get("Blocks")
    if not isinstance(blocks, dict):
        return set()

    changed: set[str] = set()
    for block_id, block_payload in blocks.items():
        if not isinstance(block_payload, dict):
            continue
        elements, key = _block_elements_ref(block_payload)
        new_elements: list[dict[str, Any]] = []
        removed_any = False
        for elem in elements:
            if not isinstance(elem, dict):
                new_elements.append(elem)
                continue
            qid = str(elem.get("QuestionID") or "").strip()
            if qid and qid in qid_set:
                removed_any = True
                continue
            new_elements.append(elem)
        if removed_any:
            block_payload[key] = new_elements
            changed.add(str(block_id))
    return changed


def _insert_block_elements(
    definition: Mapping[str, Any],
    *,
    block_id: str,
    insert_index: int,
    elements_to_insert: list[dict[str, Any]],
) -> None:
    blocks = definition.get("Blocks")
    if not isinstance(blocks, dict):
        raise ValueError("Survey definition has no Blocks map.")
    block = blocks.get(block_id)
    if not isinstance(block, dict):
        raise ValueError(f"Block {block_id} was not found in this survey.")
    elements, _ = _block_elements_ref(block)
    idx = max(0, min(int(insert_index), len(elements)))
    for elem in elements_to_insert:
        elements.insert(idx, dict(elem))
        idx += 1


def _remove_page_breaks_from_block(
    definition: Mapping[str, Any],
    *,
    block_id: str,
    element_indices: list[int],
) -> int:
    blocks = definition.get("Blocks")
    if not isinstance(blocks, dict):
        raise ValueError("Survey definition has no Blocks map.")
    block = blocks.get(block_id)
    if not isinstance(block, dict):
        raise ValueError(f"Block {block_id} was not found in this survey.")
    elements, key = _block_elements_ref(block)

    if not element_indices:
        raise ValueError("At least one --element-index is required.")

    unique_desc = sorted({int(idx) for idx in element_indices}, reverse=True)
    if unique_desc and unique_desc[0] >= len(elements):
        raise ValueError(
            f"Element index {unique_desc[0]} exceeds block length {len(elements)}."
        )

    removed = 0
    for idx in unique_desc:
        elem = elements[idx]
        if not isinstance(elem, dict):
            raise ValueError(
                f"Element index {idx} is not a valid block element object."
            )
        elem_type = str(elem.get("Type") or "").strip()
        if elem_type != "Page Break":
            raise ValueError(
                f"Element index {idx} is Type='{elem_type or '(missing)'}', expected 'Page Break'."
            )
        del elements[idx]
        removed += 1
    block[key] = elements
    return removed


def _insert_question_elements(
    definition: Mapping[str, Any],
    *,
    block_id: str,
    insert_index: int,
    qids: list[str],
    page_break_mode: str = "none",
) -> None:
    _insert_block_elements(
        definition,
        block_id=block_id,
        insert_index=insert_index,
        elements_to_insert=_build_insert_elements(
            qids=qids, page_break_mode=page_break_mode
        ),
    )


def _update_block(
    *,
    survey_id: str,
    block_id: str,
    block_payload: Mapping[str, Any],
    base_url: str,
    headers: dict[str, str],
    log_meta: dict[str, Any] | None = None,
) -> None:
    send_api_request(
        action="qsync.survey.block.update",
        method="PUT",
        base_url=base_url,
        headers=headers,
        path=f"survey-definitions/{survey_id}/blocks/{block_id}",
        survey_id=survey_id,
        log_meta=log_meta,
        json=dict(block_payload),
        timeout=60,
    )


def _extract_question_id_from_api_payload(payload: Any) -> str | None:
    candidates: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                key_norm = str(key).strip().lower()
                if key_norm in {"questionid", "question_id", "questionidfromlocator"}:
                    qid = str(value or "").strip()
                    if qid:
                        candidates.append(qid)
                else:
                    _walk(value)
            return
        if isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(payload)
    for qid in candidates:
        if qid.upper().startswith("QID"):
            return qid
    return candidates[0] if candidates else None


def _resolve_template_question_payload(
    *,
    definition: Mapping[str, Any],
    from_question_id: str | None,
    question_json_path: str | None,
) -> tuple[dict[str, Any], str | None]:
    source_qid = (from_question_id or "").strip() or None
    json_path = (question_json_path or "").strip() or None
    if bool(source_qid) == bool(json_path):
        raise ValueError(
            "Provide exactly one of --from-question-id or --question-json."
        )

    if source_qid:
        questions = definition.get("Questions")
        if not isinstance(questions, dict):
            raise ValueError("Survey definition has no Questions map.")
        source = questions.get(source_qid)
        if not isinstance(source, dict):
            raise ValueError(f"Template question {source_qid} was not found.")
        return (copy.deepcopy(source), source_qid)

    path = Path(str(json_path))
    if not path.exists():
        raise ValueError(f"Question JSON file not found: {path}")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(loaded, dict) and isinstance(loaded.get("result"), dict):
        loaded = loaded["result"]
    if isinstance(loaded, dict) and isinstance(loaded.get("Questions"), dict):
        questions = loaded["Questions"]
        if len(questions) != 1:
            raise ValueError(
                "Question JSON with 'Questions' map must contain exactly one question."
            )
        loaded = next(iter(questions.values()))
    if not isinstance(loaded, dict):
        raise ValueError("Question JSON must decode to a question object.")
    return (copy.deepcopy(loaded), None)


def _normalize_language_code_token(value: str) -> str:
    token = str(value or "").strip().replace("-", "_").upper()
    return token


def _list_enabled_languages_for_survey(
    *,
    base_url: str,
    headers: dict[str, str],
    survey_id: str,
) -> list[str]:
    resp = send_api_request(
        action="qsync.survey.languages.list",
        method="GET",
        base_url=base_url,
        headers=headers,
        path=f"surveys/{survey_id}/languages",
        survey_id=survey_id,
        log_event=False,
        timeout=30,
    )
    result = resp.json().get("result") or {}
    langs = result.get("AvailableLanguages") or result.get("languages") or []
    normalized: list[str] = []
    if isinstance(langs, dict):
        normalized.extend(
            _normalize_language_code_token(str(k))
            for k, v in langs.items()
            if bool(v)
        )
    elif isinstance(langs, (list, tuple, set)):
        normalized.extend(_normalize_language_code_token(str(v)) for v in langs)
    out: list[str] = []
    seen: set[str] = set()
    for lang in normalized:
        if not lang or lang in seen:
            continue
        seen.add(lang)
        out.append(lang)
    return out


def _normalize_and_filter_question_language_block(
    payload: dict[str, Any],
    *,
    enabled_languages: list[str] | None,
) -> tuple[int, int]:
    """Normalize/validate a question payload Language block in-place.

    Returns:
        tuple[int, int]: (dropped_not_enabled_count, malformed_entry_count)
    """

    lang_block = payload.get("Language")
    if lang_block is None:
        return (0, 0)
    if not isinstance(lang_block, Mapping):
        payload.pop("Language", None)
        return (0, 1)

    enabled_set = {
        _normalize_language_code_token(lang) for lang in (enabled_languages or [])
    }
    filtered_langs: dict[str, Any] = {}
    dropped = 0
    malformed = 0

    for lang_key, lang_value in lang_block.items():
        lang_key_raw = str(lang_key or "").strip()
        normalized_lang = _normalize_language_code_token(lang_key_raw)
        if not lang_key_raw or not normalized_lang:
            malformed += 1
            continue
        if enabled_languages is not None and normalized_lang not in enabled_set:
            dropped += 1
            continue
        if isinstance(lang_value, Mapping):
            filtered_langs[lang_key_raw] = copy.deepcopy(dict(lang_value))
        elif isinstance(lang_value, str):
            # Best-effort fallback for legacy payload shapes.
            filtered_langs[lang_key_raw] = {"QuestionText": lang_value}
            malformed += 1
        else:
            malformed += 1

    if filtered_langs:
        payload["Language"] = filtered_langs
    else:
        payload.pop("Language", None)
    return (dropped, malformed)


def _collect_choice_texts(args: argparse.Namespace) -> list[str]:
    values: list[str] = []
    raw = getattr(args, "choice_text", None)
    if raw:
        for item in raw if isinstance(raw, list) else [raw]:
            for token in str(item or "").split(","):
                text = token.strip()
                if text:
                    values.append(text)
    path_value = str(getattr(args, "choice_text_file", "") or "").strip()
    if path_value:
        path = Path(path_value)
        if not path.exists():
            raise ValueError(f"Choice text file not found: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if text:
                values.append(text)
    deduped: list[str] = []
    for idx, text in enumerate(values):
        if text:
            deduped.append(text)
    return deduped


def _collect_statement_texts(args: argparse.Namespace) -> list[str]:
    values: list[str] = []
    raw = getattr(args, "statement_text", None)
    if raw:
        for item in raw if isinstance(raw, list) else [raw]:
            for token in str(item or "").split(","):
                text = token.strip()
                if text:
                    values.append(text)
    path_value = str(getattr(args, "statement_text_file", "") or "").strip()
    if path_value:
        path = Path(path_value)
        if not path.exists():
            raise ValueError(f"Statement text file not found: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if text:
                values.append(text)
    return [text for text in values if text]


def _build_from_scratch_mc_template(
    *,
    question_text: str | None,
    choice_texts: list[str],
    multi_response: bool,
) -> dict[str, Any]:
    if len(choice_texts) < 2:
        raise ValueError("From-scratch MC questions require at least 2 choices.")

    choices: dict[str, dict[str, str]] = {}
    choice_order: list[int] = []
    for idx, label in enumerate(choice_texts, start=1):
        key = str(idx)
        choices[key] = {"Display": str(label)}
        choice_order.append(idx)

    selector = "MAVR" if multi_response else "SAVR"
    prompt = str(question_text or "").strip() or "New multiple-choice question"

    return {
        "QuestionType": "MC",
        "Selector": selector,
        "SubSelector": "TX",
        "QuestionText": prompt,
        "QuestionText_Unsafe": prompt,
        "Configuration": {"QuestionDescriptionOption": "UseText"},
        "Choices": choices,
        "ChoiceOrder": choice_order,
        "Validation": {"Settings": {"ForceResponse": "OFF", "Type": "None"}},
    }


def _build_from_scratch_te_template(
    *,
    question_text: str | None,
) -> dict[str, Any]:
    prompt = str(question_text or "").strip() or "New text-entry question"
    return {
        "QuestionType": "TE",
        "Selector": "ML",
        "QuestionText": prompt,
        "QuestionText_Unsafe": prompt,
        "Configuration": {"QuestionDescriptionOption": "UseText"},
        "Validation": {"Settings": {"ForceResponse": "OFF", "Type": "None"}},
    }


def _build_from_scratch_db_template(
    *,
    question_text: str | None,
) -> dict[str, Any]:
    prompt = str(question_text or "").strip() or "New descriptive text"
    return {
        "QuestionType": "DB",
        "Selector": "TB",
        "QuestionText": prompt,
        "QuestionText_Unsafe": prompt,
        "Configuration": {"QuestionDescriptionOption": "UseText"},
        "ChoiceOrder": [],
        "Validation": {"Settings": {"Type": "None"}},
    }


def _build_from_scratch_matrix_template(
    *,
    question_text: str | None,
    statements: list[str],
    answers: list[str],
) -> dict[str, Any]:
    if len(statements) < 1:
        raise ValueError("From-scratch matrix questions require at least 1 statement.")
    if len(answers) < 2:
        raise ValueError(
            "From-scratch matrix questions require at least 2 answer options."
        )

    choices = {str(idx): {"Display": str(text)} for idx, text in enumerate(statements, start=1)}
    answer_map = {str(idx): {"Display": str(text)} for idx, text in enumerate(answers, start=1)}
    prompt = str(question_text or "").strip() or "New matrix question"
    return {
        "QuestionType": "Matrix",
        "Selector": "Likert",
        "SubSelector": "SingleAnswer",
        "QuestionText": prompt,
        "QuestionText_Unsafe": prompt,
        "Configuration": {
            "QuestionDescriptionOption": "UseText",
            "TextPosition": "inline",
            "ChoiceColumnWidth": 25,
            "MobileFirst": True,
            "RepeatHeaders": "none",
            "WhiteSpace": "OFF",
        },
        "Choices": choices,
        "ChoiceOrder": [str(idx) for idx in range(1, len(statements) + 1)],
        "Answers": answer_map,
        "AnswerOrder": list(range(1, len(answers) + 1)),
        "Validation": {"Settings": {"ForceResponse": "OFF", "Type": "None"}},
    }


def _resolve_from_scratch_type(args: argparse.Namespace) -> str | None:
    legacy_mcq = bool(getattr(args, "from_scratch_mcq", False))
    explicit = str(getattr(args, "from_scratch_type", "") or "").strip().lower()
    if legacy_mcq and explicit and explicit != "mc":
        raise ValueError(
            "--from-scratch-mcq cannot be combined with --from-scratch-type values other than 'mc'."
        )
    if legacy_mcq:
        return "mc"
    if explicit:
        if explicit not in {"mc", "te", "matrix", "db"}:
            raise ValueError(
                "--from-scratch-type must be one of: mc, te, matrix, db."
            )
        return explicit
    return None


def _resolve_source_client_for_add_question(
    *,
    args: argparse.Namespace,
    target_base_url: str,
    target_headers: dict[str, str],
) -> tuple[str, dict[str, str]]:
    source_account = (getattr(args, "source_account", None) or "").strip()
    if not source_account:
        return (target_base_url, target_headers)
    if source_account.lower() == "default":
        default_env_path = (_workspace_root() / ".env").resolve()
        default_env = load_env(default_env_path)
        return get_client_config(default_env)
    env = load_account_env(source_account, root=_workspace_root())
    base, headers = get_client_config(env)
    return (base, headers)


def _resolve_template_question_payloads(
    *,
    args: argparse.Namespace,
    target_definition: Mapping[str, Any],
    target_survey_id: str,
    target_base_url: str,
    target_headers: dict[str, str],
) -> tuple[list[dict[str, Any]], str | None]:
    """Resolve one or many source question payloads for add-question.

    Returns:
        tuple: (payload templates in desired insertion order, fallback_qid)
    """

    from_question_id = (getattr(args, "from_question_id", None) or "").strip() or None
    question_json = (getattr(args, "question_json", None) or "").strip() or None
    source_survey_id = (getattr(args, "source_survey_id", None) or "").strip() or None
    source_qids = _normalize_question_ids(getattr(args, "source_question_id", None))
    source_account = (getattr(args, "source_account", None) or "").strip() or None
    try:
        from_scratch_type = _resolve_from_scratch_type(args)
    except ValueError as exc:
        raise SystemExit(f"[add-question] ERROR: {exc}") from exc
    source_modes = 0
    if from_scratch_type:
        source_modes += 1
    if from_question_id or question_json:
        source_modes += 1
    if source_survey_id or source_qids or source_account:
        source_modes += 1
    if source_modes != 1:
        raise ValueError(
            "Choose exactly one source mode: "
            "--from-scratch-type/--from-scratch-mcq, --from-question-id/--question-json, "
            "or --source-survey-id + --source-question-id."
        )

    if from_scratch_type:
        if from_scratch_type == "mc":
            choices = _collect_choice_texts(args)
            if not choices:
                raise ValueError(
                    "--from-scratch-mcq/--from-scratch-type mc requires --choice-text or --choice-text-file."
                )
            template = _build_from_scratch_mc_template(
                question_text=None,
                choice_texts=choices,
                multi_response=bool(getattr(args, "mc_multi_response", False)),
            )
            return ([template], None)
        if from_scratch_type == "te":
            template = _build_from_scratch_te_template(question_text=None)
            return ([template], None)
        if from_scratch_type == "db":
            template = _build_from_scratch_db_template(question_text=None)
            return ([template], None)
        if from_scratch_type == "matrix":
            statements = _collect_statement_texts(args)
            answers = _collect_choice_texts(args)
            template = _build_from_scratch_matrix_template(
                question_text=None,
                statements=statements,
                answers=answers,
            )
            return ([template], None)
        raise ValueError(f"Unsupported --from-scratch-type: {from_scratch_type}")

    if source_survey_id or source_qids or source_account:
        if not source_survey_id:
            raise ValueError("--source-survey-id is required with --source-question-id.")
        if not source_qids:
            raise ValueError(
                "Provide at least one --source-question-id when using --source-survey-id."
            )
        source_base, source_headers = _resolve_source_client_for_add_question(
            args=args,
            target_base_url=target_base_url,
            target_headers=target_headers,
        )
        source_definition = fetch_survey_definition(
            source_base,
            source_headers,
            source_survey_id,
        )
        source_questions = source_definition.get("Questions")
        if not isinstance(source_questions, dict):
            raise ValueError("Source survey definition has no Questions map.")
        payloads: list[dict[str, Any]] = []
        for qid in source_qids:
            source = source_questions.get(qid)
            if not isinstance(source, dict):
                raise ValueError(f"Source question {qid} was not found.")
            payloads.append(copy.deepcopy(source))

        # Preserve default block inference only when source == target survey context.
        target_account = _resolve_account_from_args(args) or None
        normalized_source_account = (
            None
            if not source_account or source_account.lower() == "default"
            else source_account
        )
        fallback_qid: str | None = None
        if (
            source_survey_id == target_survey_id
            and (
                normalized_source_account is None
                or normalized_source_account == target_account
            )
            and source_qids
        ):
            fallback_qid = source_qids[0]
        return (payloads, fallback_qid)

    template, template_qid = _resolve_template_question_payload(
        definition=target_definition,
        from_question_id=from_question_id,
        question_json_path=question_json,
    )
    return ([template], template_qid)


def _collect_new_question_texts(args: argparse.Namespace) -> list[str | None]:
    texts: list[str] = []
    raw = getattr(args, "question_text", None)
    if raw:
        for item in raw if isinstance(raw, list) else [raw]:
            text = str(item or "").strip()
            if text:
                texts.append(text)
    path_value = str(getattr(args, "question_text_file", "") or "").strip()
    if path_value:
        path = Path(path_value)
        if not path.exists():
            raise ValueError(f"Question text file not found: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if text:
                texts.append(text)
    if texts:
        return list(texts)
    return [None]


def _normalize_data_export_tag(value: str) -> str:
    cleaned = re.sub(r"\s+", "_", str(value or "").strip())
    return cleaned


def _next_unique_data_export_tag(base: str, existing: set[str]) -> str:
    candidate = _normalize_data_export_tag(base)
    if not candidate:
        return ""
    if candidate not in existing:
        return candidate
    suffix = 2
    while f"{candidate}_{suffix}" in existing:
        suffix += 1
    return f"{candidate}_{suffix}"


def _preflight_question_writes(
    *,
    survey_id: str,
    base_url: str,
    headers: dict[str, str],
    dry_run: bool,
    force_live: bool,
    interactive_override_prompt: bool = False,
    action_label: str = "question-write",
    account_label: str | None = None,
) -> bool:
    effective_force_live = bool(force_live)
    account = (account_label or "default").strip() or "default"
    print(
        f"[account-preflight] action={action_label} "
        f"account={account} base_url={(base_url or '').strip() or '(unknown)'}"
    )

    def _prompt_override(message: str) -> bool:
        if not interactive_override_prompt:
            return False
        try:
            from .interactive_menu import select_from_list

            choice = select_from_list(
                message,
                ["Abort", "Continue with override"],
                instruction="Choose whether to continue without restarting the wizard.",
            )
            return bool(choice and choice.startswith("Continue"))
        except Exception:
            if not sys.stdin.isatty():
                return False
            raw = (
                input(f"{message} Type 'continue' to proceed, anything else to abort: ")
                .strip()
                .lower()
            )
            return raw == "continue"

    try:
        ensure_unlocked(survey_id)
    except (SurveyLockedError, RuntimeError) as exc:
        if dry_run:
            print(f"[question-op] NOTE: lock check failed in dry-run: {exc}")
        elif _prompt_override(
            "[question-op] Survey is locked. Continue anyway?"
        ):
            print("[question-op] Proceeding despite local lock check (interactive override).")
        else:
            raise SystemExit(str(exc)) from exc

    try:
        ctx = load_push_context(survey_id, base_url=base_url, headers=headers)
        print(f"[question-op] Survey: {ctx.survey_name}")
        print(f"[question-op] {ctx.describe_counts()}")
        if ctx.counts_unknown and not effective_force_live and not dry_run:
            if _prompt_override(
                "[question-op] Response counts are unknown. Continue with --force-live?"
            ):
                effective_force_live = True
            else:
                raise SystemExit(
                    f"[question-op] Unable to verify response counts for {survey_id}. "
                    "Refresh inventory and retry or pass --force-live after manual review."
                )
        if ctx.response_count > 0 and not effective_force_live and not dry_run:
            if _prompt_override(
                f"[question-op] Survey has {ctx.response_count} finished response(s). Continue with --force-live?"
            ):
                effective_force_live = True
            else:
                raise SystemExit(
                    f"[question-op] Survey has {ctx.response_count} finished response(s). "
                    "Re-run with --force-live after double-checking."
                )
    except Exception as exc:
        if dry_run:
            print(f"[question-op] NOTE: Could not load push context: {exc}")
            return effective_force_live
        raise
    return effective_force_live


def _confirm_noninteractive_safe(*, yes: bool, prompt: str) -> None:
    if yes:
        return
    if not sys.stdin.isatty():
        raise SystemExit(
            "[question-op] Confirmation required but stdin is not interactive. "
            "Re-run with --yes to proceed."
        )
    try:
        from qsync.interactive_menu import confirm

        if not confirm(prompt, default=True):
            raise SystemExit("[question-op] Aborted.")
    except Exception:
        answer = input(f"{prompt} [Y/n]: ").strip().lower()
        if answer and answer != "y":
            raise SystemExit("[question-op] Aborted.")


def _refresh_cache_after_question_write(
    *,
    survey_id: str,
    args: argparse.Namespace,
) -> None:
    try:
        account = _resolve_account_from_args(args)
        env = load_account_env(account, root=_workspace_root()) if account else None
        path = download_survey_definition(survey_id, env=env)
        print(f"[question-op] Refreshed local cache: {path}")
    except Exception as exc:
        print(f"[question-op] WARNING: Cache refresh failed: {exc}")


def handle_add_question(args: argparse.Namespace) -> None:
    """Create one or more questions and place them into a target block position."""
    survey_id = _prompt_for_survey_id_api_if_needed(
        survey_id=getattr(args, "survey_id", None),
        args=args,
        message="Select a survey to add question(s):",
    )
    dry_run = bool(getattr(args, "dry_run", False))
    force_live = bool(getattr(args, "force_live", False))
    yes = bool(getattr(args, "yes", False))
    no_publish = bool(getattr(args, "no_publish", False))
    after_qid = (getattr(args, "after_qid", None) or "").strip() or None
    before_qid = (getattr(args, "before_qid", None) or "").strip() or None
    target_block_id = (getattr(args, "target_block_id", None) or "").strip() or None
    position = (getattr(args, "position", None) or "append").strip().lower()
    try:
        insert_index_override = _parse_insert_index_override(
            getattr(args, "insert_index", None)
        )
    except ValueError as exc:
        raise SystemExit(f"[add-question] ERROR: {exc}") from exc
    page_break_mode = str(getattr(args, "page_break_mode", None) or "none").strip().lower()
    if page_break_mode not in {"none", "before", "after", "between"}:
        raise SystemExit(
            "[add-question] ERROR: --page-break-mode must be one of none, before, after, between."
        )
    if position not in {"append", "prepend"}:
        raise SystemExit("[add-question] ERROR: --position must be append or prepend.")
    if after_qid and before_qid:
        raise SystemExit(
            "[add-question] ERROR: Use only one of --after-qid or --before-qid."
        )
    if insert_index_override is not None and (after_qid or before_qid):
        raise SystemExit(
            "[add-question] ERROR: --insert-index cannot be combined with --after-qid/--before-qid."
        )

    base_url, headers = _get_client_config_for_args(args)
    force_live = _preflight_question_writes(
        survey_id=survey_id,
        base_url=base_url,
        headers=headers,
        dry_run=dry_run,
        force_live=force_live,
        interactive_override_prompt=bool(getattr(args, "interactive_mode", False)),
        action_label="add-question",
        account_label=_resolve_account_from_args(args) or "default",
    )

    definition = fetch_survey_definition(base_url, headers, survey_id)
    template_qid: str | None = None
    template_payloads: list[dict[str, Any]]
    try:
        template_payloads, template_qid = _resolve_template_question_payloads(
            args=args,
            target_definition=definition,
            target_survey_id=survey_id,
            target_base_url=base_url,
            target_headers=headers,
        )
        texts = _collect_new_question_texts(args)
    except ValueError as exc:
        raise SystemExit(f"[add-question] ERROR: {exc}") from exc

    explicit_tag = (getattr(args, "data_export_tag", None) or "").strip()
    allow_duplicate_tags = bool(getattr(args, "allow_duplicate_tags", False))

    questions = definition.get("Questions")
    if not isinstance(questions, dict):
        raise SystemExit("[add-question] ERROR: Survey definition has no Questions map.")
    existing_tags = {
        str((question or {}).get("DataExportTag") or "").strip()
        for question in questions.values()
        if isinstance(question, dict)
    }
    existing_tags.discard("")

    from_scratch_type = _resolve_from_scratch_type(args)
    mode_from_scratch = bool(from_scratch_type)
    source_survey_id = (getattr(args, "source_survey_id", None) or "").strip() or None
    source_qids = _normalize_question_ids(getattr(args, "source_question_id", None))
    question_json = (getattr(args, "question_json", None) or "").strip() or None

    # If source survey/questions are provided, show as the template plan context.
    if source_survey_id and source_qids:
        print(
            f"[add-question] Source question bank: survey={source_survey_id} "
            f"questions={', '.join(source_qids)}"
        )

    def _apply_text_override(payload: dict[str, Any], text: str | None) -> dict[str, Any]:
        out = copy.deepcopy(payload)
        out.pop("QuestionID", None)
        out.pop("QuestionId", None)
        if text is not None:
            out["QuestionText"] = text
            if "QuestionText_Unsafe" in out:
                out["QuestionText_Unsafe"] = text
        return out

    planned_payloads: list[dict[str, Any]] = []
    if len(template_payloads) == 1:
        base_template = template_payloads[0]
        for text in texts:
            planned_payloads.append(_apply_text_override(base_template, text))
    else:
        if texts == [None]:
            for payload in template_payloads:
                planned_payloads.append(_apply_text_override(payload, None))
        elif len(texts) == 1:
            for payload in template_payloads:
                planned_payloads.append(_apply_text_override(payload, texts[0]))
        elif len(texts) == len(template_payloads):
            for payload, text in zip(template_payloads, texts):
                planned_payloads.append(_apply_text_override(payload, text))
        else:
            raise SystemExit(
                "[add-question] ERROR: question text overrides must be either one value, "
                "or match the number of selected template questions."
            )

    target_langs: list[str] | None = None
    if not dry_run:
        try:
            target_langs = _list_enabled_languages_for_survey(
                base_url=base_url,
                headers=headers,
                survey_id=survey_id,
            )
        except Exception:
            target_langs = None
    dropped_language_total = 0
    malformed_language_total = 0

    for payload in planned_payloads:
        dropped, malformed = _normalize_and_filter_question_language_block(
            payload,
            enabled_languages=target_langs,
        )
        dropped_language_total += dropped
        malformed_language_total += malformed

        base_tag = explicit_tag or str(payload.get("DataExportTag") or "").strip()
        if base_tag:
            if allow_duplicate_tags:
                payload["DataExportTag"] = _normalize_data_export_tag(base_tag)
            else:
                next_tag = _next_unique_data_export_tag(base_tag, existing_tags)
                if next_tag:
                    payload["DataExportTag"] = next_tag
                    existing_tags.add(next_tag)

    if dropped_language_total > 0:
        print(
            f"[add-question] NOTE: filtered {dropped_language_total} translation language block(s) "
            "not enabled in the target survey."
        )
    if malformed_language_total > 0:
        print(
            f"[add-question] NOTE: normalized or dropped {malformed_language_total} "
            "malformed translation language block entries."
        )

    try:
        planned_block_id = _resolve_target_block_id(
            definition,
            target_block_id=target_block_id,
            after_qid=after_qid,
            before_qid=before_qid,
            fallback_qid=template_qid,
        )
        planned_index = _resolve_insert_index_with_override(
            definition,
            block_id=planned_block_id,
            after_qid=after_qid,
            before_qid=before_qid,
            position=position,
            insert_index_override=insert_index_override,
        )
    except ValueError as exc:
        raise SystemExit(f"[add-question] ERROR: {exc}") from exc

    print(
        f"[add-question] Plan: create {len(planned_payloads)} question(s) in block {planned_block_id} at index {planned_index}."
    )
    if page_break_mode != "none":
        print(f"[add-question] Page break mode: {page_break_mode}")
    if mode_from_scratch:
        from_scratch_label = {
            "mc": "multiple-choice",
            "te": "text-entry",
            "matrix": "matrix likert",
            "db": "descriptive text",
        }.get(from_scratch_type or "", from_scratch_type or "from-scratch")
        print(f"[add-question] Template: from-scratch {from_scratch_label} scaffold")
    elif source_survey_id and source_qids:
        print(
            f"[add-question] Template source: {source_survey_id} ({len(source_qids)} question(s))"
        )
    elif template_qid:
        print(f"[add-question] Template: {template_qid}")
    elif question_json:
        print(f"[add-question] Template JSON: {question_json}")

    if dry_run:
        print("[add-question] DRY-RUN: no API writes performed.")
        return

    _confirm_noninteractive_safe(
        yes=yes,
        prompt=f"Create {len(planned_payloads)} question(s) in {survey_id}?",
    )

    created_qids: list[str] = []
    for payload in planned_payloads:
        resp = send_api_request(
            action="qsync.survey.question.create",
            method="POST",
            base_url=base_url,
            headers=headers,
            path=f"survey-definitions/{survey_id}/questions",
            survey_id=survey_id,
            json=payload,
            timeout=60,
        )
        response_payload = resp.json()
        created_qid = _extract_question_id_from_api_payload(response_payload)
        if not created_qid:
            raise SystemExit(
                "[add-question] ERROR: Could not determine created QuestionID from API response."
            )
        created_qids.append(created_qid)
        print(f"[add-question] Created {created_qid}")

    live_definition = fetch_survey_definition(base_url, headers, survey_id)
    try:
        block_id = _resolve_target_block_id(
            live_definition,
            target_block_id=target_block_id,
            after_qid=after_qid,
            before_qid=before_qid,
            fallback_qid=template_qid,
        )
        insert_index = _resolve_insert_index_with_override(
            live_definition,
            block_id=block_id,
            after_qid=after_qid,
            before_qid=before_qid,
            position=position,
            insert_index_override=insert_index_override,
        )
    except ValueError as exc:
        raise SystemExit(f"[add-question] ERROR: {exc}") from exc

    touched_blocks = _remove_qids_from_all_blocks(live_definition, created_qids)
    if insert_index_override is None:
        insert_index = _resolve_insert_index_with_override(
            live_definition,
            block_id=block_id,
            after_qid=after_qid,
            before_qid=before_qid,
            position=position,
            insert_index_override=None,
        )
    else:
        block_map = live_definition.get("Blocks")
        if not isinstance(block_map, dict):
            raise SystemExit("[add-question] ERROR: Survey definition has no Blocks map.")
        block_payload = block_map.get(block_id)
        if not isinstance(block_payload, dict):
            raise SystemExit(f"[add-question] ERROR: Block {block_id} was not found.")
        elements = (
            block_payload.get("BlockElements")
            if isinstance(block_payload.get("BlockElements"), list)
            else block_payload.get("Elements")
        )
        if not isinstance(elements, list):
            elements = []
        insert_index = max(0, min(insert_index_override, len(elements)))
    _insert_question_elements(
        live_definition,
        block_id=block_id,
        insert_index=insert_index,
        qids=created_qids,
        page_break_mode=page_break_mode,
    )
    touched_blocks.add(block_id)

    blocks = live_definition.get("Blocks")
    if not isinstance(blocks, dict):
        raise SystemExit("[add-question] ERROR: Survey definition has no Blocks map.")
    ordered_blocks = [block_id] + sorted(
        [bid for bid in touched_blocks if bid != block_id]
    )
    for bid in ordered_blocks:
        block_payload = blocks.get(bid)
        if not isinstance(block_payload, dict):
            continue
        _update_block(
            survey_id=survey_id,
            block_id=bid,
            block_payload=block_payload,
            base_url=base_url,
            headers=headers,
            log_meta={"operation": "add-question", "question_ids": created_qids},
        )

    if not no_publish:
        description = (getattr(args, "publish_description", None) or "").strip()
        if not description:
            description = make_publish_description(
                operation="add-question",
                changed_qids=created_qids,
                count=len(created_qids),
                label=f"block {block_id}",
                max_chars=SURVEY_VERSION_DESCRIPTION_MAX_CHARS,
            )
        publish_survey_definition(
            survey_id,
            description=description,
            published=True,
            context={
                "origin": "qsync.cli_survey.add-question",
                "changed_qids": created_qids,
                "block_id": block_id,
            },
            base_url=base_url,
            headers=headers,
        )

    _refresh_cache_after_question_write(survey_id=survey_id, args=args)
    print(
        f"[add-question] Added {len(created_qids)} question(s): {', '.join(created_qids)}"
    )


def handle_move_question(args: argparse.Namespace) -> None:
    """Move one or more existing questions to a new position within survey blocks."""
    survey_id = _prompt_for_survey_id_api_if_needed(
        survey_id=getattr(args, "survey_id", None),
        args=args,
        message="Select a survey to move question(s):",
    )
    qids = _normalize_question_ids(getattr(args, "question_id", None))
    if not qids:
        raise SystemExit("[move-question] ERROR: --question-id is required.")

    dry_run = bool(getattr(args, "dry_run", False))
    force_live = bool(getattr(args, "force_live", False))
    yes = bool(getattr(args, "yes", False))
    no_publish = bool(getattr(args, "no_publish", False))
    after_qid = (getattr(args, "after_qid", None) or "").strip() or None
    before_qid = (getattr(args, "before_qid", None) or "").strip() or None
    target_block_id = (getattr(args, "target_block_id", None) or "").strip() or None
    position = (getattr(args, "position", None) or "append").strip().lower()
    try:
        insert_index_override = _parse_insert_index_override(
            getattr(args, "insert_index", None)
        )
    except ValueError as exc:
        raise SystemExit(f"[move-question] ERROR: {exc}") from exc
    if position not in {"append", "prepend"}:
        raise SystemExit("[move-question] ERROR: --position must be append or prepend.")
    if after_qid and before_qid:
        raise SystemExit(
            "[move-question] ERROR: Use only one of --after-qid or --before-qid."
        )
    if insert_index_override is not None and (after_qid or before_qid):
        raise SystemExit(
            "[move-question] ERROR: --insert-index cannot be combined with --after-qid/--before-qid."
        )
    if after_qid and after_qid in qids:
        raise SystemExit(
            "[move-question] ERROR: --after-qid cannot reference a moved question."
        )
    if before_qid and before_qid in qids:
        raise SystemExit(
            "[move-question] ERROR: --before-qid cannot reference a moved question."
        )

    base_url, headers = _get_client_config_for_args(args)
    force_live = _preflight_question_writes(
        survey_id=survey_id,
        base_url=base_url,
        headers=headers,
        dry_run=dry_run,
        force_live=force_live,
        interactive_override_prompt=bool(getattr(args, "interactive_mode", False)),
        action_label="move-question",
        account_label=_resolve_account_from_args(args) or "default",
    )

    definition = fetch_survey_definition(base_url, headers, survey_id)
    questions = definition.get("Questions")
    if not isinstance(questions, dict):
        raise SystemExit("[move-question] ERROR: Survey definition has no Questions map.")
    missing = [qid for qid in qids if qid not in questions]
    if missing:
        raise SystemExit(
            f"[move-question] ERROR: Unknown question ID(s): {', '.join(missing)}"
        )

    blocks = definition.get("Blocks")
    if not isinstance(blocks, dict):
        raise SystemExit("[move-question] ERROR: Survey definition has no Blocks map.")
    try:
        plan_result = blocks_dimension.apply_move_qids(
            copy.deepcopy(blocks),
            qids=qids,
            target_block_id=target_block_id,
            after_qid=after_qid,
            before_qid=before_qid,
            position=position,
            insert_index=insert_index_override,
            allow_trash_target=False,
        )
    except ValueError as exc:
        raise SystemExit(f"[move-question] ERROR: {exc}") from exc
    block_id = str(plan_result.get("block_id") or "").strip()
    insert_index = int(plan_result.get("insert_index") or 0)

    print(
        f"[move-question] Plan: move {len(qids)} question(s) into block {block_id} at index {insert_index}."
    )
    if dry_run:
        print("[move-question] DRY-RUN: no API writes performed.")
        return

    _confirm_noninteractive_safe(
        yes=yes,
        prompt=f"Move {len(qids)} question(s) in {survey_id}?",
    )

    try:
        apply_result = blocks_dimension.apply_move_qids(
            blocks,
            qids=qids,
            target_block_id=target_block_id,
            after_qid=after_qid,
            before_qid=before_qid,
            position=position,
            insert_index=insert_index_override,
            allow_trash_target=False,
        )
    except ValueError as exc:
        raise SystemExit(f"[move-question] ERROR: {exc}") from exc
    touched_blocks = {
        str(block).strip()
        for block in apply_result.get("touched_blocks", [])
        if str(block).strip()
    }
    ordered_blocks = [block_id] + sorted(
        [bid for bid in touched_blocks if bid != block_id]
    )
    for bid in ordered_blocks:
        block_payload = blocks.get(bid)
        if not isinstance(block_payload, dict):
            continue
        _update_block(
            survey_id=survey_id,
            block_id=bid,
            block_payload=block_payload,
            base_url=base_url,
            headers=headers,
            log_meta={"operation": "move-question", "question_ids": qids},
        )

    if not no_publish:
        description = (getattr(args, "publish_description", None) or "").strip()
        if not description:
            description = make_publish_description(
                operation="move-question",
                changed_qids=qids,
                count=len(qids),
                label=f"block {block_id}",
                max_chars=SURVEY_VERSION_DESCRIPTION_MAX_CHARS,
            )
        publish_survey_definition(
            survey_id,
            description=description,
            published=True,
            context={
                "origin": "qsync.cli_survey.move-question",
                "changed_qids": qids,
                "block_id": block_id,
            },
            base_url=base_url,
            headers=headers,
        )

    _refresh_cache_after_question_write(survey_id=survey_id, args=args)
    print(f"[move-question] Moved question(s): {', '.join(qids)}")


def handle_remove_question(args: argparse.Namespace) -> None:
    """Remove one or more questions from active blocks (move to Trash)."""
    survey_id = _prompt_for_survey_id_api_if_needed(
        survey_id=getattr(args, "survey_id", None),
        args=args,
        message="Select a survey to remove question(s):",
    )
    qids = _normalize_question_ids(getattr(args, "question_id", None))
    if not qids:
        raise SystemExit("[remove-question] ERROR: --question-id is required.")

    dry_run = bool(getattr(args, "dry_run", False))
    force_live = bool(getattr(args, "force_live", False))
    yes = bool(getattr(args, "yes", False))
    no_publish = bool(getattr(args, "no_publish", False))

    base_url, headers = _get_client_config_for_args(args)
    force_live = _preflight_question_writes(
        survey_id=survey_id,
        base_url=base_url,
        headers=headers,
        dry_run=dry_run,
        force_live=force_live,
        interactive_override_prompt=bool(getattr(args, "interactive_mode", False)),
        action_label="remove-question",
        account_label=_resolve_account_from_args(args) or "default",
    )

    definition = fetch_survey_definition(base_url, headers, survey_id)
    questions = definition.get("Questions")
    if not isinstance(questions, dict):
        raise SystemExit(
            "[remove-question] ERROR: Survey definition has no Questions map."
        )

    missing = [qid for qid in qids if qid not in questions]
    if missing:
        raise SystemExit(
            f"[remove-question] ERROR: Unknown question ID(s): {', '.join(missing)}"
        )

    qids_with_references: list[str] = []
    qids_without_references: list[str] = []
    total_block_references = 0
    for qid in qids:
        matches = _find_question_blocks(definition, qid, include_trash=True)
        total_block_references += len(matches)
        if matches:
            qids_with_references.append(qid)
        else:
            qids_without_references.append(qid)

    blocks = definition.get("Blocks")
    if not isinstance(blocks, dict):
        raise SystemExit(
            "[remove-question] ERROR: Survey definition has no Blocks map."
        )
    try:
        plan_result = blocks_dimension.apply_remove_qids(
            copy.deepcopy(blocks),
            qids=qids,
            move_to_trash=True,
        )
    except ValueError as exc:
        raise SystemExit(f"[remove-question] ERROR: {exc}") from exc
    touched_blocks = {
        str(block).strip()
        for block in plan_result.get("touched_blocks", [])
        if str(block).strip()
    }
    trash_block_id = (
        str(plan_result.get("trash_block_id") or "").strip() or None
    )
    moved_to_trash = bool(plan_result.get("moved_to_trash"))
    action_label = "move to Trash" if moved_to_trash else "remove from active blocks"
    print(f"[remove-question] Plan: {action_label} for {len(qids)} question(s) in {survey_id}.")
    print(
        f"[remove-question] Block references: {total_block_references} reference(s) across {len(touched_blocks)} block(s)."
    )
    if moved_to_trash and trash_block_id:
        print(f"[remove-question] Trash block: {trash_block_id}")
    if not moved_to_trash:
        print(
            "[remove-question] NOTE: No Trash block found; questions will be detached from blocks only."
        )
    if qids_without_references:
        print(
            "[remove-question] NOTE: these QIDs were not referenced by any block: "
            + ", ".join(qids_without_references)
        )

    if dry_run:
        print("[remove-question] DRY-RUN: no API writes performed.")
        return

    _confirm_noninteractive_safe(
        yes=yes,
        prompt=f"Delete {len(qids)} question(s) in {survey_id}?",
    )

    try:
        apply_result = blocks_dimension.apply_remove_qids(
            blocks,
            qids=qids,
            move_to_trash=True,
        )
    except ValueError as exc:
        raise SystemExit(f"[remove-question] ERROR: {exc}") from exc
    touched_blocks = {
        str(block).strip()
        for block in apply_result.get("touched_blocks", [])
        if str(block).strip()
    }
    trash_block_id = (
        str(apply_result.get("trash_block_id") or "").strip() or None
    )
    moved_to_trash = bool(apply_result.get("moved_to_trash"))

    for block_id in sorted(touched_blocks):
        block_payload = blocks.get(block_id)
        if not isinstance(block_payload, dict):
            continue
        _update_block(
            survey_id=survey_id,
            block_id=block_id,
            block_payload=block_payload,
            base_url=base_url,
            headers=headers,
            log_meta={
                "operation": "remove-question",
                "question_ids": qids,
                "block_id": block_id,
            },
        )

    removed_qids: list[str] = list(qids)

    if not no_publish:
        description = (getattr(args, "publish_description", None) or "").strip()
        if not description:
            description = make_publish_description(
                operation="remove-question",
                changed_qids=removed_qids,
                count=len(removed_qids),
                label=f"{len(touched_blocks)} block(s)",
                max_chars=SURVEY_VERSION_DESCRIPTION_MAX_CHARS,
            )
        publish_survey_definition(
            survey_id,
            description=description,
            published=True,
            context={
                "origin": "qsync.cli_survey.remove-question",
                "changed_qids": removed_qids,
                "updated_blocks": sorted(touched_blocks),
                "moved_to_trash": moved_to_trash,
                "trash_block_id": trash_block_id,
            },
            base_url=base_url,
            headers=headers,
        )

    _refresh_cache_after_question_write(survey_id=survey_id, args=args)
    if moved_to_trash:
        print(
            f"[remove-question] Removed question(s) from active blocks and moved to Trash: {', '.join(removed_qids)}"
        )
    else:
        print(f"[remove-question] Removed question(s) from blocks: {', '.join(removed_qids)}")
    if qids_with_references:
        print(
            f"[remove-question] Removed block references for: {', '.join(qids_with_references)}"
        )


def handle_add_page_break(args: argparse.Namespace) -> None:
    """Insert a Page Break element into a target survey block."""
    survey_id = _prompt_for_survey_id_api_if_needed(
        survey_id=getattr(args, "survey_id", None),
        args=args,
        message="Select a survey to add a page break:",
    )
    dry_run = bool(getattr(args, "dry_run", False))
    force_live = bool(getattr(args, "force_live", False))
    yes = bool(getattr(args, "yes", False))
    no_publish = bool(getattr(args, "no_publish", False))
    after_qid = (getattr(args, "after_qid", None) or "").strip() or None
    before_qid = (getattr(args, "before_qid", None) or "").strip() or None
    target_block_id = (getattr(args, "target_block_id", None) or "").strip() or None
    position = (getattr(args, "position", None) or "append").strip().lower()
    try:
        insert_index_override = _parse_insert_index_override(
            getattr(args, "insert_index", None)
        )
    except ValueError as exc:
        raise SystemExit(f"[add-page-break] ERROR: {exc}") from exc
    if position not in {"append", "prepend"}:
        raise SystemExit(
            "[add-page-break] ERROR: --position must be append or prepend."
        )
    if after_qid and before_qid:
        raise SystemExit(
            "[add-page-break] ERROR: Use only one of --after-qid or --before-qid."
        )
    if insert_index_override is not None and (after_qid or before_qid):
        raise SystemExit(
            "[add-page-break] ERROR: --insert-index cannot be combined with --after-qid/--before-qid."
        )

    base_url, headers = _get_client_config_for_args(args)
    _preflight_question_writes(
        survey_id=survey_id,
        base_url=base_url,
        headers=headers,
        dry_run=dry_run,
        force_live=force_live,
        interactive_override_prompt=bool(getattr(args, "interactive_mode", False)),
        action_label="add-page-break",
        account_label=_resolve_account_from_args(args) or "default",
    )

    definition = fetch_survey_definition(base_url, headers, survey_id)
    blocks = definition.get("Blocks")
    if not isinstance(blocks, dict):
        raise SystemExit("[add-page-break] ERROR: Survey definition has no Blocks map.")
    try:
        plan_result = blocks_dimension.apply_add_page_break(
            copy.deepcopy(blocks),
            target_block_id=target_block_id,
            after_qid=after_qid,
            before_qid=before_qid,
            position=position,
            insert_index=insert_index_override,
            allow_trash_target=False,
        )
    except ValueError as exc:
        raise SystemExit(f"[add-page-break] ERROR: {exc}") from exc
    block_id = str(plan_result.get("block_id") or "").strip()
    insert_index = int(plan_result.get("insert_index") or 0)

    print(
        f"[add-page-break] Plan: insert page break in block {block_id} at index {insert_index}."
    )
    if dry_run:
        print("[add-page-break] DRY-RUN: no API writes performed.")
        return

    _confirm_noninteractive_safe(
        yes=yes,
        prompt=f"Insert page break in {survey_id}?",
    )

    try:
        apply_result = blocks_dimension.apply_add_page_break(
            blocks,
            target_block_id=target_block_id,
            after_qid=after_qid,
            before_qid=before_qid,
            position=position,
            insert_index=insert_index_override,
            allow_trash_target=False,
        )
    except ValueError as exc:
        raise SystemExit(f"[add-page-break] ERROR: {exc}") from exc
    block_id = str(apply_result.get("block_id") or "").strip() or block_id
    insert_index = int(apply_result.get("insert_index") or insert_index)
    block_payload = blocks.get(block_id)
    if not isinstance(block_payload, dict):
        raise SystemExit(f"[add-page-break] ERROR: Block {block_id} was not found.")

    _update_block(
        survey_id=survey_id,
        block_id=block_id,
        block_payload=block_payload,
        base_url=base_url,
        headers=headers,
        log_meta={"operation": "add-page-break", "block_id": block_id},
    )

    if not no_publish:
        description = (getattr(args, "publish_description", None) or "").strip()
        if not description:
            description = make_publish_description(
                operation="add-page-break",
                changed_qids=[],
                count=1,
                label=f"block {block_id}",
                max_chars=SURVEY_VERSION_DESCRIPTION_MAX_CHARS,
            )
        publish_survey_definition(
            survey_id,
            description=description,
            published=True,
            context={
                "origin": "qsync.cli_survey.add-page-break",
                "block_id": block_id,
                "insert_index": insert_index,
            },
            base_url=base_url,
            headers=headers,
        )

    _refresh_cache_after_question_write(survey_id=survey_id, args=args)
    print(f"[add-page-break] Inserted page break in block {block_id} at index {insert_index}.")


def handle_remove_page_break(args: argparse.Namespace) -> None:
    """Remove one or more Page Break elements from a target survey block."""
    survey_id = _prompt_for_survey_id_api_if_needed(
        survey_id=getattr(args, "survey_id", None),
        args=args,
        message="Select a survey to remove page break(s):",
    )
    dry_run = bool(getattr(args, "dry_run", False))
    force_live = bool(getattr(args, "force_live", False))
    yes = bool(getattr(args, "yes", False))
    no_publish = bool(getattr(args, "no_publish", False))
    target_block_id = (getattr(args, "target_block_id", None) or "").strip() or None
    if not target_block_id:
        raise SystemExit("[remove-page-break] ERROR: --target-block-id is required.")
    try:
        element_indices = _normalize_element_indices(getattr(args, "element_index", None))
    except ValueError as exc:
        raise SystemExit(f"[remove-page-break] ERROR: {exc}") from exc
    if not element_indices:
        raise SystemExit(
            "[remove-page-break] ERROR: provide at least one --element-index."
        )

    base_url, headers = _get_client_config_for_args(args)
    _preflight_question_writes(
        survey_id=survey_id,
        base_url=base_url,
        headers=headers,
        dry_run=dry_run,
        force_live=force_live,
        interactive_override_prompt=bool(getattr(args, "interactive_mode", False)),
        action_label="remove-page-break",
        account_label=_resolve_account_from_args(args) or "default",
    )

    definition = fetch_survey_definition(base_url, headers, survey_id)
    blocks = definition.get("Blocks")
    if not isinstance(blocks, dict):
        raise SystemExit(
            "[remove-page-break] ERROR: Survey definition has no Blocks map."
        )
    try:
        plan_result = blocks_dimension.apply_remove_page_break(
            copy.deepcopy(blocks),
            target_block_id=target_block_id,
            element_indices=element_indices,
            allow_trash_target=False,
        )
    except ValueError as exc:
        raise SystemExit(f"[remove-page-break] ERROR: {exc}") from exc
    block_id = str(plan_result.get("block_id") or "").strip() or target_block_id

    print(
        "[remove-page-break] Plan: remove page break element(s) at "
        f"index {', '.join(str(i) for i in element_indices)} from block {block_id}."
    )
    if dry_run:
        print("[remove-page-break] DRY-RUN: no API writes performed.")
        return

    _confirm_noninteractive_safe(
        yes=yes,
        prompt=f"Remove {len(element_indices)} page break(s) in {survey_id}?",
    )

    try:
        apply_result = blocks_dimension.apply_remove_page_break(
            blocks,
            target_block_id=target_block_id,
            element_indices=element_indices,
            allow_trash_target=False,
        )
    except ValueError as exc:
        raise SystemExit(f"[remove-page-break] ERROR: {exc}") from exc
    removed = int(apply_result.get("removed") or 0)
    block_id = str(apply_result.get("block_id") or "").strip() or block_id
    block_payload = blocks.get(block_id)
    if not isinstance(block_payload, dict):
        raise SystemExit(f"[remove-page-break] ERROR: Block {block_id} was not found.")

    _update_block(
        survey_id=survey_id,
        block_id=block_id,
        block_payload=block_payload,
        base_url=base_url,
        headers=headers,
        log_meta={
            "operation": "remove-page-break",
            "block_id": block_id,
            "removed_indices": sorted(element_indices),
        },
    )

    if not no_publish:
        description = (getattr(args, "publish_description", None) or "").strip()
        if not description:
            description = make_publish_description(
                operation="remove-page-break",
                changed_qids=[],
                count=removed,
                label=f"block {block_id}",
                max_chars=SURVEY_VERSION_DESCRIPTION_MAX_CHARS,
            )
        publish_survey_definition(
            survey_id,
            description=description,
            published=True,
            context={
                "origin": "qsync.cli_survey.remove-page-break",
                "block_id": block_id,
                "removed_indices": sorted(element_indices),
            },
            base_url=base_url,
            headers=headers,
        )

    _refresh_cache_after_question_write(survey_id=survey_id, args=args)
    print(f"[remove-page-break] Removed {removed} page break(s) from block {block_id}.")


def handle_push_question(args: argparse.Namespace) -> None:
    """Push a single question from cached survey JSON to Qualtrics."""
    from .cli import _prompt_for_survey_id_if_needed

    survey_id = _prompt_for_survey_id_if_needed(args.survey_id, allow_all_surveys=False)
    question_id = args.question_id
    dry_run = getattr(args, "dry_run", False)
    force_live = getattr(args, "force_live", False)
    yes = getattr(args, "yes", False)
    show_diff = getattr(args, "show_diff", False)
    no_publish = bool(getattr(args, "no_publish", False))

    # Find cached survey file
    survey_file = getattr(args, "survey_file", None)
    if survey_file:
        survey_path = Path(survey_file)
    else:
        survey_path = find_cached_survey_file(survey_id)
        if not survey_path:
            print(f"[push-question] ERROR: No cached survey file found for {survey_id}")
            print("  Run `qsync survey pull --survey-id <ID>` first.")
            sys.exit(1)

    if not survey_path.exists():
        print(f"[push-question] ERROR: Survey file not found: {survey_path}")
        sys.exit(1)

    from .survey_ref import format_survey_ref

    # Load cached question
    print(f"[push-question] Loading question {question_id} from {survey_path}...")
    payload = json.loads(survey_path.read_text())
    try:
        question_payload = _extract_question(payload, question_id)
    except KeyError as e:
        print(f"[push-question] ERROR: {e}")
        sys.exit(1)

    # Check push context (response counts, staleness)
    try:
        ctx = load_push_context(survey_id)
        survey_ref = format_survey_ref(survey_id, getattr(ctx, "survey_name", None))
        print(f"[push-question] Survey: {survey_ref}")
        print(f"[push-question] {ctx.describe_counts()}")

        if ctx.response_count > 0 and not force_live:
            print(
                f"[push-question] ERROR: Survey has {ctx.response_count} live response(s)."
            )
            print("  Use --force-live to push anyway, or remove responses first.")
            sys.exit(1)
    except Exception as e:
        print(
            f"[push-question] WARNING: Could not load push context: {e}. "
            "Next: refresh inventory (`qsync survey inventory`) and retry, or proceed only if you're sure it's safe."
        )
        if dry_run:
            # Dry-runs never write to Qualtrics; allow inspection even if inventory is missing.
            pass
        elif not yes:
            if not sys.stdin.isatty():
                print(
                    "[push-question] ERROR: Confirmation required but stdin is not interactive. "
                    "Re-run with --yes to proceed.",
                    file=sys.stderr,
                )
                sys.exit(2)
            try:
                from qsync.interactive_menu import confirm

                if not confirm("Continue anyway?", default=True):
                    print("Aborted.")
                    sys.exit(1)
            except Exception:
                confirm = input("  Continue anyway? [Y/n]: ").strip().lower()
                if confirm and confirm != "y":
                    print("Aborted.")
                    sys.exit(1)

    base_url, headers = get_client_config()

    # Fetch remote and compare
    try:
        remote_question = _fetch_remote_question(
            base_url, survey_id, question_id, headers
        )
    except Exception as e:
        print(f"[push-question] ERROR: {e}")
        sys.exit(1)

    local_pretty = _format_question(question_payload)
    remote_pretty = _format_question(remote_question)

    if local_pretty == remote_pretty:
        print(f"[push-question] Question {question_id} is already in sync with remote.")
        return

    # Show diff
    if show_diff or not yes:
        print(f"\n[push-question] Diff (remote → local) for {question_id}:")
        diff_lines = list(
            unified_diff(
                remote_pretty.splitlines(),
                local_pretty.splitlines(),
                fromfile="remote",
                tofile="local",
                lineterm="",
            )
        )
        for line in diff_lines[:100]:  # Limit output
            print(line)
        if len(diff_lines) > 100:
            print(f"... ({len(diff_lines) - 100} more lines)")
        print()

    if dry_run:
        print(
            f"[dry-run] Would push question {question_id} to survey {format_survey_ref(survey_id)}"
        )
        return

    # Confirm unless --yes
    if not yes:
        if not sys.stdin.isatty():
            print(
                "[push-question] ERROR: Confirmation required but stdin is not interactive. "
                "Re-run with --yes to proceed.",
                file=sys.stderr,
            )
            sys.exit(2)
        try:
            from qsync.interactive_menu import confirm

            if not confirm(
                f"Push question {question_id} to survey {format_survey_ref(survey_id)}?",
                default=True,
            ):
                print("Aborted.")
                sys.exit(1)
        except Exception:
            confirm = (
                input(
                    f"Push question {question_id} to survey {format_survey_ref(survey_id)}? [Y/n]: "
                )
                .strip()
                .lower()
            )
            if confirm and confirm != "y":
                print("Aborted.")
                sys.exit(1)

    # Push
    meta = {
        "question_id": question_id,
        "source_file": str(survey_path),
    }
    send_api_request(
        action="qsync.survey.push.question",
        method="PUT",
        base_url=base_url,
        headers=headers,
        path=f"survey-definitions/{survey_id}/questions/{question_id}",
        survey_id=survey_id,
        log_meta=meta,
        json=question_payload,
    )

    if not no_publish:
        publish_survey_definition(
            survey_id,
            description=f"qsync push-question: {question_id}",
            published=True,
            context={
                "origin": "qsync.cli_survey.push-question",
                "question_id": question_id,
                "source_file": str(survey_path),
            },
        )

    print(
        f"[push-question] Successfully pushed {question_id} to survey {format_survey_ref(survey_id)}"
    )


def handle_replace_question(args: argparse.Namespace) -> None:
    """Replace one target question payload with a source question payload."""

    target_survey_id = _prompt_for_survey_id_api_if_needed(
        survey_id=getattr(args, "survey_id", None),
        args=args,
        message="Select target survey to replace question in:",
    )
    target_question_id = str(getattr(args, "question_id", "") or "").strip()
    source_survey_id = str(getattr(args, "source_survey_id", "") or "").strip()
    source_question_id = str(getattr(args, "source_question_id", "") or "").strip()
    source_account = str(getattr(args, "source_account", "") or "").strip() or None

    if not target_question_id:
        raise SystemExit("[replace-question] ERROR: --question-id is required.")
    if not source_survey_id:
        raise SystemExit("[replace-question] ERROR: --source-survey-id is required.")
    if not source_question_id:
        raise SystemExit("[replace-question] ERROR: --source-question-id is required.")

    dry_run = bool(getattr(args, "dry_run", False))
    force_live = bool(getattr(args, "force_live", False))
    yes = bool(getattr(args, "yes", False))
    show_diff = bool(getattr(args, "show_diff", False))
    replace_data_export_tag = bool(getattr(args, "replace_data_export_tag", False))
    no_publish = bool(getattr(args, "no_publish", False))

    base_url, headers = _get_client_config_for_args(args)
    force_live = _preflight_question_writes(
        survey_id=target_survey_id,
        base_url=base_url,
        headers=headers,
        dry_run=dry_run,
        force_live=force_live,
        interactive_override_prompt=bool(getattr(args, "interactive_mode", False)),
        action_label="replace-question",
        account_label=_resolve_account_from_args(args) or "default",
    )

    target_definition = fetch_survey_definition(base_url, headers, target_survey_id)
    target_questions = target_definition.get("Questions")
    if not isinstance(target_questions, dict):
        raise SystemExit(
            "[replace-question] ERROR: Target survey definition has no Questions map."
        )
    target_question = target_questions.get(target_question_id)
    if not isinstance(target_question, dict):
        raise SystemExit(
            f"[replace-question] ERROR: Target question {target_question_id} was not found."
        )

    source_base, source_headers = _resolve_source_client_for_add_question(
        args=args,
        target_base_url=base_url,
        target_headers=headers,
    )
    source_definition = fetch_survey_definition(source_base, source_headers, source_survey_id)
    source_questions = source_definition.get("Questions")
    if not isinstance(source_questions, dict):
        raise SystemExit(
            "[replace-question] ERROR: Source survey definition has no Questions map."
        )
    source_question = source_questions.get(source_question_id)
    if not isinstance(source_question, dict):
        raise SystemExit(
            f"[replace-question] ERROR: Source question {source_question_id} was not found."
        )

    replacement_payload = copy.deepcopy(source_question)
    replacement_payload["QuestionID"] = target_question_id
    if "QuestionId" in replacement_payload:
        replacement_payload["QuestionId"] = target_question_id

    if not replace_data_export_tag:
        target_tag = str(target_question.get("DataExportTag") or "").strip()
        if target_tag:
            replacement_payload["DataExportTag"] = target_tag

    target_langs: list[str] | None = None
    if not dry_run:
        try:
            target_langs = _list_enabled_languages_for_survey(
                base_url=base_url,
                headers=headers,
                survey_id=target_survey_id,
            )
        except Exception:
            target_langs = None
    dropped_langs, malformed_langs = _normalize_and_filter_question_language_block(
        replacement_payload,
        enabled_languages=target_langs,
    )

    source_ref = f"{source_survey_id}:{source_question_id}"
    if source_account:
        source_ref = f"{source_ref} (account={source_account})"
    print(
        f"[replace-question] Plan: replace {target_question_id} in {target_survey_id} "
        f"with {source_ref}."
    )
    if not replace_data_export_tag and str(target_question.get("DataExportTag") or "").strip():
        print(
            "[replace-question] DataExportTag policy: preserving target DataExportTag "
            "(use --replace-data-export-tag to copy source tag)."
        )
    if dropped_langs > 0:
        print(
            f"[replace-question] NOTE: filtered {dropped_langs} translation language block(s) "
            "not enabled in target survey."
        )
    if malformed_langs > 0:
        print(
            f"[replace-question] NOTE: normalized or dropped {malformed_langs} malformed "
            "translation language block entries."
        )

    remote_pretty = _format_question(target_question)
    local_pretty = _format_question(replacement_payload)
    if remote_pretty == local_pretty:
        print(
            f"[replace-question] Target {target_question_id} is already in sync with source payload."
        )
        return

    if show_diff or not yes:
        print(
            f"\n[replace-question] Diff (target current → replacement) for {target_question_id}:"
        )
        diff_lines = list(
            unified_diff(
                remote_pretty.splitlines(),
                local_pretty.splitlines(),
                fromfile="target-current",
                tofile="replacement",
                lineterm="",
            )
        )
        for line in diff_lines[:100]:
            print(line)
        if len(diff_lines) > 100:
            print(f"... ({len(diff_lines) - 100} more lines)")
        print()

    if dry_run:
        print(
            f"[replace-question] DRY-RUN: would replace {target_question_id} in {target_survey_id}."
        )
        return

    _confirm_noninteractive_safe(
        yes=yes,
        prompt=(
            f"Replace {target_question_id} in {target_survey_id} with "
            f"{source_survey_id}:{source_question_id}?"
        ),
    )

    send_api_request(
        action="qsync.survey.replace.question",
        method="PUT",
        base_url=base_url,
        headers=headers,
        path=f"survey-definitions/{target_survey_id}/questions/{target_question_id}",
        survey_id=target_survey_id,
        log_meta={
            "operation": "replace-question",
            "target_question_id": target_question_id,
            "source_survey_id": source_survey_id,
            "source_question_id": source_question_id,
            "source_account": source_account or "",
            "changed_qids": [target_question_id],
        },
        json=replacement_payload,
    )

    if not no_publish:
        description = (getattr(args, "publish_description", None) or "").strip()
        if not description:
            description = make_publish_description(
                operation="replace-question",
                changed_qids=[target_question_id],
                count=1,
                label=f"from {source_survey_id}:{source_question_id}",
                max_chars=SURVEY_VERSION_DESCRIPTION_MAX_CHARS,
            )
        publish_survey_definition(
            target_survey_id,
            description=description,
            published=True,
            context={
                "origin": "qsync.cli_survey.replace-question",
                "target_question_id": target_question_id,
                "source_survey_id": source_survey_id,
                "source_question_id": source_question_id,
            },
            base_url=base_url,
            headers=headers,
        )

    _refresh_cache_after_question_write(survey_id=target_survey_id, args=args)
    print(
        "[replace-question] Successfully replaced "
        f"{target_question_id} in {target_survey_id}."
    )


def handle_export_responses(args: argparse.Namespace) -> None:
    """Export survey responses (supports one or more surveys)."""
    root = _workspace_root()
    account = _resolve_account_from_args(args)
    env = load_account_env(account, root=root) if account else None
    try:
        export_format = normalize_response_export_format(
            getattr(args, "export_format", None)
        )
    except ValueError as exc:
        raise SystemExit(f"[export-responses] ERROR: {exc}") from exc

    survey_ids = _normalize_survey_ids(getattr(args, "survey_id", None))
    if not survey_ids:
        survey_ids = _prompt_for_survey_ids_api_if_needed(
            survey_ids=None,
            args=args,
            message="Pick survey(s) to export responses:",
            allow_multiple=True,
        )
    survey_ids = list(dict.fromkeys([sid.strip() for sid in survey_ids if sid.strip()]))
    if not survey_ids:
        raise SystemExit("[export-responses] Cancelled.")

    output_dir = _resolve_responses_output_dir(
        root, account, getattr(args, "output", None)
    )

    base_url, headers = get_client_config(env) if env else get_client_config()
    surveys_lookup: dict[str, str] = {}
    try:
        surveys_lookup = {s["id"]: s["name"] for s in list_surveys(base_url, headers)}
    except Exception:
        surveys_lookup = {}

    from .survey_ref import format_survey_ref

    output_dir.mkdir(parents=True, exist_ok=True)
    failures: list[tuple[str, str]] = []
    for survey_id in survey_ids:
        try:
            print(
                "[export-responses] Starting "
                f"{export_format.upper()} export for {format_survey_ref(survey_id)}..."
            )
            payload = build_response_export_payload(export_format=export_format)

            response = send_api_request(
                action="qsync.survey.export.responses.start",
                method="POST",
                base_url=base_url,
                headers=headers,
                path=f"surveys/{survey_id}/export-responses",
                log_event=False,
                json=payload,
                timeout=60,
            )
            progress_id = response.json()["result"]["progressId"]
            print(
                f"[export-responses] {survey_id}: Export started. Progress ID: {progress_id}"
            )

            progress_status = "inProgress"
            file_id = None
            while progress_status not in ("complete", "failed"):
                print(f"[export-responses] {survey_id}: Checking progress...")
                check_response = send_api_request(
                    action="qsync.survey.export.responses.poll",
                    method="GET",
                    base_url=base_url,
                    headers=headers,
                    path=f"surveys/{survey_id}/export-responses/{progress_id}",
                    log_event=False,
                    timeout=60,
                )
                result = check_response.json()["result"]
                progress_status = result["status"]

                if progress_status == "failed":
                    raise RuntimeError("Export failed")

                if progress_status == "complete":
                    file_id = result["fileId"]
                else:
                    time.sleep(2)

            print(f"[export-responses] {survey_id}: Export complete. File ID: {file_id}")

            print(f"[export-responses] {survey_id}: Downloading file...")
            download_response = send_api_request(
                action="qsync.survey.export.responses.download",
                method="GET",
                base_url=base_url,
                headers=headers,
                path=f"surveys/{survey_id}/export-responses/{file_id}/file",
                log_event=False,
                stream=True,
                timeout=120,
            )

            survey_name = surveys_lookup.get(survey_id, survey_id)
            safe_name = "".join(
                c if c.isalnum() or c in " -_" else "_" for c in survey_name
            ).strip()
            zip_path = output_dir / f"{safe_name}_{survey_id}_{export_format}.zip"

            with open(zip_path, "wb") as f:
                for chunk in download_response.iter_content(chunk_size=8192):
                    f.write(chunk)

            print(f"[export-responses] {survey_id}: Saved zip to {zip_path}")

            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                zip_ref.extractall(output_dir)
                for file in zip_ref.namelist():
                    print(f"  - {file}")
            print(f"[export-responses] {survey_id}: Extracted to {output_dir}")
        except Exception as exc:
            failures.append((survey_id, str(exc)))
            print(f"[export-responses] {survey_id}: ERROR: {exc}")

    if failures:
        raise SystemExit(1)


def handle_export_translation(args: argparse.Namespace) -> None:
    """Export translation-review document(s) for one or more surveys."""

    from .interactive_menu import is_interactive
    from .terminal_output import error, info, success, warn
    from .translation_export import (
        export_survey_to_pdf,
        export_survey_to_word,
        export_surveys_side_by_side_docx,
    )

    def _normalize_filter_values(raw_values: object) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in list(raw_values or []):
            for token in str(raw).replace(",", " ").split():
                value = token.strip()
                if not value or value in seen:
                    continue
                seen.add(value)
                out.append(value)
        return out

    survey_ids = _normalize_survey_ids(getattr(args, "survey_id", None))
    if not survey_ids:
        survey_ids = _prompt_for_survey_ids_api_if_needed(
            survey_ids=None,
            args=args,
            message="Select survey(s) to export translation document(s):",
            allow_multiple=True,
        )
    survey_ids = list(dict.fromkeys([sid.strip() for sid in survey_ids if sid.strip()]))
    if not survey_ids:
        raise SystemExit("[qsync:export-translation] Cancelled.")

    output = getattr(args, "output", None)
    no_html = bool(getattr(args, "no_html", False))
    edf_args = getattr(args, "edf", None) or []
    edf_preset_names = getattr(args, "edf_preset", None) or []
    list_edf_presets = bool(getattr(args, "list_edf_presets", False))
    base_edf_overrides: dict[str, str] = {}
    for raw in edf_args:
        s = str(raw or "").strip()
        if not s:
            continue
        if "=" not in s:
            error(
                "[qsync:export-translation]",
                f"Invalid --edf value (expected KEY=VALUE): {s}",
            )
            sys.exit(1)
        k, v = s.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k:
            error(
                "[qsync:export-translation]",
                f"Invalid --edf value (empty key): {s}",
            )
            sys.exit(1)
        base_edf_overrides[k] = v

    def _load_edf_presets_for_survey(
        survey_id: str,
    ) -> dict[str, dict[str, str]]:
        root = resolve_root(required=False) or Path.cwd()
        preset_path = resolve_edf_presets_path(root=root)
        if not preset_path.exists():
            return {}
        try:
            data = json.loads(preset_path.read_text(encoding="utf-8"))
        except Exception as exc:
            warn(
                "[qsync:export-translation]",
                f"Could not read EDF presets ({preset_path}): {exc}",
            )
            return {}
        if not isinstance(data, dict):
            return {}
        presets: dict[str, dict[str, str]] = {}
        for scope_key in ("default", survey_id):
            bucket = data.get(scope_key)
            if not isinstance(bucket, dict):
                continue
            for name, mapping in bucket.items():
                if not isinstance(mapping, dict):
                    continue
                cleaned = {
                    str(k): str(v)
                    for k, v in mapping.items()
                    if k is not None and v is not None
                }
                if cleaned:
                    presets[str(name)] = cleaned
        return presets

    if list_edf_presets:
        for idx, survey_id in enumerate(survey_ids):
            if len(survey_ids) > 1:
                info("[qsync:export-translation]", f"Survey {survey_id}:")
            presets = _load_edf_presets_for_survey(str(survey_id))
            if not presets:
                root = resolve_root(required=False) or Path.cwd()
                candidates = ", ".join(
                    str(path) for path in edf_presets_candidates(root=root)
                )
                info(
                    "[qsync:export-translation]",
                    f"No EDF presets found. Add one of: {candidates}",
                )
            else:
                info("[qsync:export-translation]", "Available EDF presets:")
                for name in sorted(presets.keys()):
                    preset_vals = ", ".join(
                        f"{k}={v}" for k, v in sorted(presets[name].items())
                    )
                    info(None, f"  - {name}: {preset_vals}")
            if len(survey_ids) > 1 and idx < len(survey_ids) - 1:
                print()
        return

    smart_name = bool(getattr(args, "smart_name", False))
    do_open = bool(getattr(args, "open", False))
    compare_to_base = bool(getattr(args, "compare_to_base", False))
    compare_with = str(getattr(args, "compare_with", "") or "").strip() or None
    include_qids = set(_normalize_filter_values(getattr(args, "include_qid", None)))
    include_tags = set(_normalize_filter_values(getattr(args, "include_tag", None)))
    include_blocks = set(_normalize_filter_values(getattr(args, "block", None)))
    refresh = bool(getattr(args, "refresh", False))
    layout_heuristics = bool(getattr(args, "layout_heuristics", False))
    render_mermaid = bool(getattr(args, "render_mermaid", False))
    include_mermaid = bool(getattr(args, "include_mermaid", False))
    format = getattr(args, "format", "docx")
    skip_js_strings = bool(getattr(args, "skip_js_strings", False))
    flow_trace = bool(getattr(args, "flow_trace", False))
    flow_trace_cb = None
    if flow_trace:
        info("[qsync:export-translation]", "Flow trace enabled (--flow-trace).")

        def flow_trace_cb(message: str) -> None:
            info("[qsync:export-translation]", f"[trace] {message}")

    # Optional rendering language(s)
    render_langs: list[str | None] = []
    lang_single = getattr(args, "language", None)
    lang_csv = getattr(args, "languages", None)
    if lang_csv:
        render_langs = [s.strip() for s in str(lang_csv).split(",") if s.strip()]
    elif lang_single:
        render_langs = [str(lang_single).strip()]
    else:
        render_langs = [None]

    if compare_to_base and all(x is None for x in render_langs):
        error(
            "[qsync:export-translation]",
            "--compare-to-base requires --language/--languages.",
        )
        sys.exit(1)

    if include_mermaid and not render_mermaid:
        error(
            "[qsync:export-translation]",
            "--include-mermaid requires --render-mermaid.",
        )
        sys.exit(1)

    if compare_with:
        if len(survey_ids) != 1:
            error(
                "[qsync:export-translation]",
                "--compare-with supports exactly one primary --survey-id.",
            )
            sys.exit(1)
        if format != "docx":
            error(
                "[qsync:export-translation]",
                "--compare-with currently supports --format docx only.",
            )
            sys.exit(1)
        if compare_to_base or any(lang is not None for lang in render_langs):
            error(
                "[qsync:export-translation]",
                "--compare-with cannot be combined with --language/--languages or --compare-to-base.",
            )
            sys.exit(1)
        if include_qids or include_tags or include_blocks:
            error(
                "[qsync:export-translation]",
                "--compare-with cannot be combined with --include-qid/--include-tag/--block.",
            )
            sys.exit(1)
        try:
            path = export_surveys_side_by_side_docx(
                survey_ids[0],
                compare_with,
                output_path=output,
                smart_name=smart_name,
                refresh=refresh,
                include_html_source=not no_html,
                layout_heuristics=layout_heuristics,
                include_js_strings=not skip_js_strings,
                interactive=is_interactive(),
                flow_trace=flow_trace_cb,
            )
        except Exception as exc:
            error("[qsync:export-translation]", f"compare export failed: {exc}")
            sys.exit(1)

        success("[qsync:export-translation]", f"Exported compare document: {path}")
        if do_open:
            try:
                import subprocess

                if sys.platform == "darwin":
                    subprocess.run(["open", str(path)], check=False)
                elif os.name == "nt":
                    os.startfile(str(path))  # type: ignore[attr-defined]
                else:
                    subprocess.run(["xdg-open", str(path)], check=False)
            except Exception:
                warn(
                    "[qsync:export-translation]",
                    "Could not open document automatically.",
                )
        return

    output_suffix = getattr(output, "suffix", "").lower() if output is not None else ""

    # Validate format + output path combinations
    if format == "both" and output is not None and output_suffix in (".docx", ".pdf"):
        error(
            "[qsync:export-translation]",
            "When using --format both, --output must be a directory (or omitted), not a file path.",
        )
        sys.exit(1)

    # If multiple languages are requested, output must be a directory (or omitted).
    if (
        len([x for x in render_langs if x]) > 1
        and output is not None
        and output_suffix in (".docx", ".pdf")
    ):
        error(
            "[qsync:export-translation]",
            "When exporting multiple languages, --output must be a directory (or omitted).",
        )
        sys.exit(1)

    # If we're generating bilingual exports, we also regenerate the base-language export.
    # That means a single output file path is ambiguous (it would need to hold multiple files).
    if compare_to_base and output is not None and output_suffix in (".docx", ".pdf"):
        error(
            "[qsync:export-translation]",
            "When using --compare-to-base, --output must be a directory (or omitted), not a file path.",
        )
        sys.exit(1)

    # Multiple surveys also produce multiple files by definition.
    if len(survey_ids) > 1 and output is not None and output_suffix in (".docx", ".pdf"):
        error(
            "[qsync:export-translation]",
            "When exporting multiple surveys, --output must be a directory (or omitted), not a file path.",
        )
        sys.exit(1)

    interactive = is_interactive()
    all_paths: list[Path] = []
    failures: list[tuple[str, str]] = []
    if include_qids or include_tags or include_blocks:
        summary_bits: list[str] = []
        if include_qids:
            summary_bits.append(f"qids={','.join(sorted(include_qids))}")
        if include_tags:
            summary_bits.append(f"tags={','.join(sorted(include_tags))}")
        if include_blocks:
            summary_bits.append(f"blocks={','.join(sorted(include_blocks))}")
        info(
            "[qsync:export-translation]",
            f"Applying filters: {'; '.join(summary_bits)}",
        )

    for survey_id in survey_ids:
        edf_overrides = dict(base_edf_overrides)

        if edf_preset_names:
            presets = _load_edf_presets_for_survey(str(survey_id))
            if not presets:
                root = resolve_root(required=False) or Path.cwd()
                candidates = ", ".join(
                    str(path) for path in edf_presets_candidates(root=root)
                )
                msg = f"No EDF presets found. Add one of: {candidates}"
                error("[qsync:export-translation]", f"{survey_id}: {msg}")
                failures.append((survey_id, msg))
                continue
            preset_error = False
            for name in edf_preset_names:
                preset = presets.get(str(name))
                if not preset:
                    available = ", ".join(sorted(presets.keys()))
                    msg = f"Unknown --edf-preset {name}. Available: {available}"
                    error("[qsync:export-translation]", f"{survey_id}: {msg}")
                    failures.append((survey_id, msg))
                    preset_error = True
                    break
                for key, value in preset.items():
                    if key in edf_overrides:
                        warn(
                            "[qsync:export-translation]",
                            f"{survey_id}: EDF preset {name} set {key}={value}, but overridden by --edf {key}={edf_overrides[key]}",
                        )
                    else:
                        edf_overrides[key] = value
            if preset_error:
                continue

        survey_paths: list[Path] = []
        exported_base = False

        try:
            for lang in render_langs:
                formats_to_export = ["docx", "pdf"] if format == "both" else [format]
                for fmt in formats_to_export:
                    if fmt == "docx":
                        survey_paths.append(
                            export_survey_to_word(
                                str(survey_id),
                                output_path=output,
                                edf_overrides=edf_overrides or None,
                                smart_name=smart_name,
                                include_html_source=not no_html,
                                layout_heuristics=layout_heuristics,
                                render_mermaid=render_mermaid,
                                include_mermaid=include_mermaid,
                                render_language=lang,
                                compare_to_base=compare_to_base,
                                include_qids=include_qids or None,
                                include_tags=include_tags or None,
                                include_blocks=include_blocks or None,
                                refresh=refresh,
                                include_js_strings=not skip_js_strings,
                                interactive=interactive,
                                flow_trace=flow_trace_cb,
                            )
                        )
                    elif fmt == "pdf":
                        survey_paths.append(
                            export_survey_to_pdf(
                                str(survey_id),
                                output_path=output,
                                edf_overrides=edf_overrides or None,
                                smart_name=smart_name,
                                include_html_source=not no_html,
                                layout_heuristics=layout_heuristics,
                                render_mermaid=render_mermaid,
                                include_mermaid=include_mermaid,
                                render_language=lang,
                                compare_to_base=compare_to_base,
                                include_qids=include_qids or None,
                                include_tags=include_tags or None,
                                include_blocks=include_blocks or None,
                                refresh=refresh,
                                include_js_strings=not skip_js_strings,
                                interactive=interactive,
                                flow_trace=flow_trace_cb,
                            )
                        )

                # In bilingual mode, also regenerate the base-language export once per run.
                if compare_to_base and not exported_base:
                    exported_base = True
                    for fmt in formats_to_export:
                        if fmt == "docx":
                            survey_paths.append(
                                export_survey_to_word(
                                    str(survey_id),
                                    output_path=output,
                                    edf_overrides=edf_overrides or None,
                                    smart_name=smart_name,
                                    include_html_source=not no_html,
                                    layout_heuristics=layout_heuristics,
                                    render_mermaid=render_mermaid,
                                    include_mermaid=include_mermaid,
                                    render_language=None,
                                    compare_to_base=False,
                                    include_qids=include_qids or None,
                                    include_tags=include_tags or None,
                                    include_blocks=include_blocks or None,
                                    refresh=False,
                                    include_js_strings=not skip_js_strings,
                                    interactive=interactive,
                                    flow_trace=flow_trace_cb,
                                )
                            )
                        elif fmt == "pdf":
                            survey_paths.append(
                                export_survey_to_pdf(
                                    str(survey_id),
                                    output_path=output,
                                    edf_overrides=edf_overrides or None,
                                    smart_name=smart_name,
                                    include_html_source=not no_html,
                                    layout_heuristics=layout_heuristics,
                                    render_mermaid=render_mermaid,
                                    include_mermaid=include_mermaid,
                                    render_language=None,
                                    compare_to_base=False,
                                    include_qids=include_qids or None,
                                    include_tags=include_tags or None,
                                    include_blocks=include_blocks or None,
                                    refresh=False,
                                    include_js_strings=not skip_js_strings,
                                    interactive=interactive,
                                    flow_trace=flow_trace_cb,
                                )
                            )
        except Exception as exc:
            failures.append((survey_id, str(exc)))
            error("[qsync:export-translation]", f"{survey_id}: ERROR: {exc}")
            continue

        for path in survey_paths:
            fmt_label = path.suffix.upper().lstrip(".")
            success(
                "[qsync:export-translation]",
                f"{survey_id}: Exported ({fmt_label}): {path}",
            )
        all_paths.extend(survey_paths)

    if failures:
        warn(
            "[qsync:export-translation]",
            f"Completed with failures: {len(failures)} survey(s) failed.",
        )
        for survey_id, msg in failures:
            warn("[qsync:export-translation]", f"{survey_id}: {msg}")
        raise SystemExit(1)

    if do_open and len(all_paths) == 1:
        try:
            import subprocess

            if sys.platform == "darwin":
                subprocess.run(["open", str(all_paths[0])], check=False)
            elif os.name == "nt":
                os.startfile(str(all_paths[0]))  # type: ignore[attr-defined]
            else:
                subprocess.run(["xdg-open", str(all_paths[0])], check=False)
        except Exception:
            error(
                "[qsync:export-translation]", "Could not open document automatically."
            )


def _add_export_translation_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--survey-id",
        action="append",
        dest="survey_id",
        help="Qualtrics survey ID(s) to export (repeatable/comma-separated; omit to select interactively)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output path for export file (default: export/<SurveyName>__<SurveyID>__translation.{format})",
    )
    parser.add_argument(
        "--format",
        choices=["docx", "pdf", "both"],
        default="docx",
        help="Output format: docx (Word document), pdf (PDF via WeasyPrint), both (generate both formats). Default: docx.",
    )
    parser.add_argument(
        "--edf",
        action="append",
        dest="edf",
        help="Scenario filter embedded data (repeatable): KEY=VALUE; drops provably-irrelevant branch paths",
    )
    parser.add_argument(
        "--edf-preset",
        action="append",
        dest="edf_preset",
        help="Named EDF preset from workspace EDF preset file (repeatable).",
    )
    parser.add_argument(
        "--list-edf-presets",
        action="store_true",
        dest="list_edf_presets",
        help="List available EDF presets for this survey and exit.",
    )
    parser.add_argument(
        "--language",
        help="Render the export using this language code (e.g. FR, NL, CS).",
    )
    parser.add_argument(
        "--languages",
        help="Comma-separated language codes to export (writes one .docx per language).",
    )
    parser.add_argument(
        "--compare-to-base",
        action="store_true",
        dest="compare_to_base",
        help="Bilingual export: include EN + target language for each question.",
    )
    parser.add_argument(
        "--compare-with",
        help=(
            "Export a side-by-side compare document against another survey ID "
            "(docx only; cannot be combined with --compare-to-base or --language)."
        ),
    )
    parser.add_argument(
        "--include-qid",
        action="append",
        dest="include_qid",
        help="Limit export scope to specific QID(s) (repeatable/comma-separated).",
    )
    parser.add_argument(
        "--include-tag",
        action="append",
        dest="include_tag",
        help="Limit export scope to question DataExportTag value(s) (repeatable/comma-separated).",
    )
    parser.add_argument(
        "--block",
        action="append",
        dest="block",
        help="Limit export scope to specific Block ID(s) (repeatable/comma-separated).",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh the cached survey definition from Qualtrics before exporting (network).",
    )
    parser.add_argument(
        "--layout-heuristics",
        action="store_true",
        dest="layout_heuristics",
        help="Enable layout heuristics (e.g., certain lists render as tables). Default is UI-faithful rendering.",
    )
    parser.add_argument(
        "--render-mermaid",
        action="store_true",
        dest="render_mermaid",
        help="Generate Mermaid flow chart artifacts (.flow.mmd/.flow.png). Default: disabled.",
    )
    parser.add_argument(
        "--include-mermaid",
        action="store_true",
        dest="include_mermaid",
        help="Embed the generated Mermaid diagram into the DOCX/PDF export. Requires --render-mermaid.",
    )
    parser.add_argument(
        "--smart-name",
        action="store_true",
        dest="smart_name",
        help="Use SurveyName + timestamp for the output filename (avoids overwriting)",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the exported Word document after generation",
    )
    parser.add_argument(
        "--no-html",
        action="store_true",
        dest="no_html",
        help="Do not include `HTML (source):` blocks when a parsed rendering exists",
    )
    parser.add_argument(
        "--skip-js-strings",
        action="store_true",
        dest="skip_js_strings",
        help="Skip extracting and displaying user-visible strings from QuestionJS code",
    )
    parser.add_argument(
        "--flow-trace",
        action="store_true",
        dest="flow_trace",
        help="Print flow traversal trace entries (what was dropped and why)",
    )


def _resolve_translation_languages(
    survey_id: str,
    args: argparse.Namespace,
    *,
    default_to_enabled: bool,
    allow_pending: bool,
) -> list[str]:
    from .errors import QsyncValidationError
    from .translations import load_pending_languages, resolve_languages_for_cli

    requested = _collect_languages_from_args(args)
    if requested:
        return resolve_languages_for_cli(survey_id, requested)

    if allow_pending:
        pending = load_pending_languages(survey_id)
        if pending:
            return pending

    if default_to_enabled:
        return resolve_languages_for_cli(survey_id, None)

    raise QsyncValidationError(
        error_id="QSYNC-TRANSLATIONS-ARGS-001",
        problem="No languages specified.",
        why="This command requires --language/--languages or a pending translation list.",
        impact="Translation action aborted.",
        action="Provide --language/--languages or run `qsync translations stage` to stage changes.",
        context={"survey_id": survey_id},
    )


def _render_translation_doctor_report(
    report,
    *,
    prefix: str,
) -> None:
    from .terminal_output import error, info, warn

    if report.coverage:
        info(prefix, "Coverage summary:")
        for lang in sorted(report.coverage.keys()):
            stats = report.coverage[lang]
            info(
                None,
                f"  - {lang}: {stats['filled']}/{stats['total']} filled, {stats['empty']} empty",
            )
    # Many IDE consoles / log capture setups show only stdout. Mirror stderr
    # output in those cases to keep doctor output actionable.
    import sys

    mirror_to_stdout = not sys.stderr.isatty()
    for err in report.errors:
        if mirror_to_stdout:
            info(prefix, f"ERROR: {err}")
        error(prefix, err)
    for warning in report.warnings:
        if mirror_to_stdout:
            info(prefix, f"WARNING: {warning}")
        warn(prefix, warning)


def handle_translations_languages_list(args: argparse.Namespace) -> None:
    from .terminal_output import info
    from .translations import list_enabled_languages

    survey_id = args.survey_id
    languages = list_enabled_languages(survey_id)
    if not languages:
        info("[qsync:translations]", "No enabled languages found.")
        return
    info("[qsync:translations]", "Enabled languages:")
    for lang in languages:
        info(None, f"  - {lang}")


def handle_translations_languages_ensure(args: argparse.Namespace) -> None:
    from .errors import QsyncValidationError
    from .terminal_output import info, success
    from .translations import ensure_languages, list_enabled_languages

    survey_id = args.survey_id
    requested = _collect_languages_from_args(args)
    if not requested:
        raise QsyncValidationError(
            error_id="QSYNC-TRANSLATIONS-ARGS-002",
            problem="No languages provided to ensure.",
            why="Expected --language or --languages.",
            impact="No changes applied.",
            action="Pass one or more language codes (e.g. --language FR --language NL).",
            context={"survey_id": survey_id},
        )

    existing = list_enabled_languages(survey_id)
    updated = ensure_languages(survey_id, requested, dry_run=bool(args.dry_run))
    added = [lang for lang in updated if lang not in existing]

    if args.dry_run:
        info(
            "[qsync:translations]",
            f"Dry run: would set AvailableLanguages to {', '.join(updated)}",
        )
        return

    if added:
        success(
            "[qsync:translations]",
            f"Enabled languages: {', '.join(added)}",
        )
    else:
        info("[qsync:translations]", "Languages already enabled; no changes made.")


def handle_translations_languages_set(args: argparse.Namespace) -> None:
    from .errors import QsyncValidationError
    from .terminal_output import info, success
    from .translations import list_enabled_languages, set_languages

    survey_id = args.survey_id
    requested = _collect_languages_from_args(args)
    if not requested:
        raise QsyncValidationError(
            error_id="QSYNC-TRANSLATIONS-ARGS-003",
            problem="No languages provided to set.",
            why="Expected --language or --languages.",
            impact="No changes applied.",
            action="Pass one or more language codes (e.g. --language FR --language NL).",
            context={"survey_id": survey_id},
        )

    existing = list_enabled_languages(survey_id)
    updated = set_languages(survey_id, requested, dry_run=bool(args.dry_run))

    if args.dry_run:
        info(
            "[qsync:translations]",
            f"Dry run: would set AvailableLanguages to {', '.join(updated)}",
        )
        return

    removed = [lang for lang in existing if lang not in updated]
    added = [lang for lang in updated if lang not in existing]
    if added or removed:
        msg_parts = []
        if added:
            msg_parts.append(f"added {', '.join(added)}")
        if removed:
            msg_parts.append(f"removed {', '.join(removed)}")
        success("[qsync:translations]", f"Languages set ({'; '.join(msg_parts)})")
    else:
        info("[qsync:translations]", "Languages already matched; no changes made.")


def handle_translations_pull(args: argparse.Namespace) -> None:
    from .terminal_output import info, success, warn
    from .qualtrics_client import refresh_survey_cache

    survey_ids = _normalize_survey_ids(getattr(args, "survey_id", None))
    if not survey_ids:
        raise SystemExit("[qsync:translations] ERROR: Missing --survey-id")
    survey_ids = list(dict.fromkeys([sid.strip() for sid in survey_ids if sid.strip()]))
    account = _resolve_account_from_args(args)
    languages = _collect_languages_from_args(args)
    if languages:
        warn(
            "[qsync:translations]",
            "Note: --language/--languages are ignored for `translations pull` "
            "(translations live in the survey definition).",
        )

    env = load_account_env(account, root=_workspace_root()) if account else None
    surveys_dir = (
        resolve_survey_cache_dir(root=_workspace_root(), account=account)
        if account
        else None
    )
    failures: list[tuple[str, str]] = []
    for survey_id in survey_ids:
        try:
            if account:
                cache, changed = refresh_survey_cache(
                    survey_id,
                    surveys_dir=surveys_dir,
                    env=env,
                )
            else:
                cache, changed = refresh_survey_cache(survey_id)
        except Exception as exc:
            failures.append((survey_id, str(exc)))
            warn("[qsync:translations]", f"{survey_id}: {exc}")
            continue
        if changed:
            success("[qsync:translations]", f"{survey_id}: Pulled {cache.path}")
        else:
            info("[qsync:translations]", f"{survey_id}: Cache already up to date: {cache.path}")
    if failures:
        raise SystemExit(1)


def _warn_legacy_translations(args: argparse.Namespace) -> None:
    from .terminal_output import warn

    if getattr(args, "legacy_translations", False):
        warn(
            "[qsync:translations]",
            "Legacy namespace: prefer `qsync translations ...` for canonical workflows.",
        )


def handle_translations_preview(args: argparse.Namespace) -> None:
    import sys
    from .terminal_output import info
    from .translations import preview_translations
    from .drift_check import confirm_preview_drift
    from .qualtrics_client import refresh_survey_cache

    survey_id = args.survey_id
    _warn_legacy_translations(args)
    languages = _collect_languages_from_args(args) or None
    scope_expr = getattr(args, "scope", None)
    scope = ScopeFilter.parse(scope_expr) if scope_expr else None

    def _update_cache() -> None:
        refresh_survey_cache(survey_id)
        info("[qsync:translations]", "Refreshed cached survey definition from API.")

    confirm_preview_drift(
        survey_id=survey_id,
        dimension="translations",
        allow_drift=bool(getattr(args, "allow_drift", False)),
        interactive=sys.stdin.isatty(),
        update_cache=_update_cache,
    )
    lines = preview_translations(
        survey_id,
        languages,
        detailed=bool(getattr(args, "detailed", False)),
        scope=scope,
    )
    for line in lines:
        info("[qsync:translations]", line)


def handle_translations_apply(args: argparse.Namespace) -> None:
    from .terminal_output import info, success, warn
    from .translations import apply_translations

    survey_id = args.survey_id
    _warn_legacy_translations(args)
    languages = _collect_languages_from_args(args) or None
    scope_expr = getattr(args, "scope", None)
    scope = ScopeFilter.parse(scope_expr) if scope_expr else None
    record = apply_translations(
        survey_id,
        languages,
        scope=scope,
        allow_drift=bool(getattr(args, "allow_drift", False)),
        interactive=True,
    )
    if not record:
        info("[qsync:translations]", "No translation changes detected.")
        return
    payload = record.payload if hasattr(record, "payload") else None
    staged_langs = payload.languages if payload else []
    staged_qids = payload.qids if payload else []
    warn(
        "[qsync:translations]",
        f"Pending translations staged for {record.survey_id}: "
        f"{len(staged_qids)} QID(s), {', '.join(staged_langs)}",
    )
    info(
        "[qsync:translations]",
        f"Pending: surveys/pending/translations/{record.survey_id}.json (schema v{getattr(record, 'schema_version', 1)})",
    )
    success("[qsync:translations]", "Run `qsync translations push` to publish.")


def handle_translations_doctor(args: argparse.Namespace) -> None:
    from .errors import QsyncValidationError
    from .translations import (
        fetch_base_language,
        run_translation_doctor,
    )
    from .workbook_resolver import WorkbookResolver

    survey_id = args.survey_id
    languages = _resolve_translation_languages(
        survey_id,
        args,
        default_to_enabled=True,
        allow_pending=False,
    )
    base_language = fetch_base_language(survey_id)
    resolver = WorkbookResolver()
    workbook_path = (
        Path(args.workbook) if args.workbook else resolver.default_path(survey_id)
    )
    report = run_translation_doctor(
        survey_id,
        languages,
        base_language=base_language,
        workbook_path=workbook_path,
    )
    _render_translation_doctor_report(report, prefix="[qsync:translations]")
    if report.errors:
        summary = "\n".join(report.errors[:25])
        if len(report.errors) > 25:
            summary += f"\n... ({len(report.errors) - 25} more)"
        raise QsyncValidationError(
            error_id="QSYNC-TRANSLATIONS-DOCTOR-001",
            problem="Translation doctor found errors.",
            why=summary,
            impact="Translations are not safe to push.",
            action="Fix the errors above and rerun the doctor.",
            context={"survey_id": survey_id, "languages": ",".join(languages)},
        )


def handle_translations_drift(args: argparse.Namespace) -> None:
    from .terminal_output import info
    from .translations import fetch_base_language, drift_translations

    survey_id = args.survey_id
    languages = _resolve_translation_languages(
        survey_id,
        args,
        default_to_enabled=True,
        allow_pending=False,
    )
    base_language = fetch_base_language(survey_id)
    lines = drift_translations(survey_id, languages, base_language=base_language)
    for line in lines:
        info("[qsync:translations]", line)


def handle_translations_publish_check(args: argparse.Namespace) -> None:
    from .errors import QsyncValidationError
    from .terminal_output import info, success, warn
    from .translations import run_publish_requirement_check

    survey_id = args.survey_id
    language = (args.language or "").strip()
    if not language:
        raise QsyncValidationError(
            error_id="QSYNC-TRANSLATIONS-PUBLISH-ARGS-001",
            problem="Missing language for publish check.",
            why="The publish check needs a single non-base language.",
            impact="Publish check aborted.",
            action="Pass --language FR (or similar).",
            context={"survey_id": survey_id},
        )
    if not args.confirm:
        raise QsyncValidationError(
            error_id="QSYNC-TRANSLATIONS-PUBLISH-ARGS-002",
            problem="Publish check requires confirmation.",
            why="The check edits a live survey (smoke only).",
            impact="Publish check aborted.",
            action="Re-run with --confirm to proceed.",
            context={"survey_id": survey_id},
        )

    result = run_publish_requirement_check(
        survey_id,
        language,
        key=args.key,
        marker=args.marker,
        publish=bool(args.publish),
        publish_description=args.publish_description,
        restore=not args.no_restore,
        publish_restore=not args.no_publish_restore,
        allow_non_smoke=bool(args.allow_non_smoke),
    )

    info("[qsync:translations]", f"Marker key: {result['key']}")
    info(
        "[qsync:translations]",
        f"Visible before publish: {'yes' if result['pre_publish_visible'] else 'no'}",
    )
    if result["post_publish_visible"] is not None:
        info(
            "[qsync:translations]",
            f"Visible after publish: {'yes' if result['post_publish_visible'] else 'no'}",
        )
    if result["pre_publish_visible"] and not result["post_publish_visible"]:
        warn(
            "[qsync:translations]",
            "Marker visible before publish; publish may not be required.",
        )
    if not result["pre_publish_visible"] and result["post_publish_visible"] is True:
        success(
            "[qsync:translations]",
            "Marker only visible after publish (publish required).",
        )


def handle_translations_keys_snapshot(args: argparse.Namespace) -> None:
    from .terminal_output import success
    from .translations import snapshot_translation_keys

    path = snapshot_translation_keys(args.survey_id, args.language, label=args.label)
    success("[qsync:translations]", f"Snapshot saved: {path}")


def handle_translations_keys_compare(args: argparse.Namespace) -> None:
    from .terminal_output import info
    from .translations import (
        diff_translation_key_snapshots,
        load_translation_key_snapshot,
        translation_key_snapshot_path,
    )

    before_path = translation_key_snapshot_path(
        args.survey_id, args.before, args.language
    )
    after_path = translation_key_snapshot_path(
        args.survey_id, args.after, args.language
    )
    before = load_translation_key_snapshot(before_path)
    after = load_translation_key_snapshot(after_path)
    diff = diff_translation_key_snapshots(before, after)
    info(
        "[qsync:translations]",
        f"Key diff: missing={len(diff.missing_keys)}, extra={len(diff.extra_keys)}",
    )


def handle_translations_keys_check_publish(args: argparse.Namespace) -> None:
    from .errors import QsyncValidationError
    from .terminal_output import info
    from .translations import run_key_stability_check_publish

    if not args.confirm:
        raise QsyncValidationError(
            error_id="QSYNC-TRANSLATIONS-KEYCHECK-ARGS-001",
            problem="Key check requires confirmation.",
            why="This check publishes a new survey version.",
            impact="Operation aborted.",
            action="Re-run with --confirm to proceed.",
            context={"survey_id": args.survey_id},
        )
    result = run_key_stability_check_publish(
        args.survey_id,
        args.language,
        label=args.label,
        publish_description=args.publish_description,
        allow_non_smoke=bool(args.allow_non_smoke),
    )
    info(
        "[qsync:translations]",
        f"Publish key diff: missing={len(result['missing'])}, extra={len(result['extra'])}",
    )


def handle_translations_keys_check_reorder(args: argparse.Namespace) -> None:
    from .errors import QsyncValidationError
    from .terminal_output import info
    from .translations import run_key_stability_check_reorder

    if not args.confirm:
        raise QsyncValidationError(
            error_id="QSYNC-TRANSLATIONS-KEYCHECK-ARGS-002",
            problem="Key check requires confirmation.",
            why="This check mutates question choice order.",
            impact="Operation aborted.",
            action="Re-run with --confirm to proceed.",
            context={"survey_id": args.survey_id},
        )
    result = run_key_stability_check_reorder(
        args.survey_id,
        args.language,
        question_id=args.question_id,
        label=args.label,
        publish_description=args.publish_description,
        allow_non_smoke=bool(args.allow_non_smoke),
    )
    info(
        "[qsync:translations]",
        f"Reorder key diff: missing={len(result['missing'])}, extra={len(result['extra'])}",
    )


def handle_translations_keys_check_add_remove(args: argparse.Namespace) -> None:
    from .errors import QsyncValidationError
    from .terminal_output import info
    from .translations import run_key_stability_check_add_remove

    if not args.confirm:
        raise QsyncValidationError(
            error_id="QSYNC-TRANSLATIONS-KEYCHECK-ARGS-003",
            problem="Key check requires confirmation.",
            why="This check adds/removes a choice in the survey.",
            impact="Operation aborted.",
            action="Re-run with --confirm to proceed.",
            context={"survey_id": args.survey_id},
        )
    result = run_key_stability_check_add_remove(
        args.survey_id,
        args.language,
        question_id=args.question_id,
        label=args.label,
        publish_description=args.publish_description,
        allow_non_smoke=bool(args.allow_non_smoke),
    )
    info(
        "[qsync:translations]",
        f"Add diff: missing={len(result['missing_add'])}, extra={len(result['extra_add'])}",
    )
    info(
        "[qsync:translations]",
        f"Remove diff: missing={len(result['missing_remove'])}, extra={len(result['extra_remove'])}",
    )


def handle_translations_pack(args: argparse.Namespace) -> None:
    from .terminal_output import error, success
    from .translation_pack import build_translation_pack

    survey_id = getattr(args, "survey_id", None)
    if not survey_id:
        error("[qsync:translations]", "Missing --survey-id")
        sys.exit(1)

    edf_args = getattr(args, "edf", None) or []
    edf_overrides = {}
    for raw in edf_args:
        s = str(raw or "").strip()
        if not s:
            continue
        if "=" not in s:
            error(
                "[qsync:translations]",
                f"Invalid --edf value (expected KEY=VALUE): {s}",
            )
            sys.exit(1)
        k, v = s.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k:
            error(
                "[qsync:translations]",
                f"Invalid --edf value (empty key): {s}",
            )
            sys.exit(1)
        edf_overrides[k] = v

    output = getattr(args, "output", None)
    smart_name = bool(getattr(args, "smart_name", False))
    include_base = bool(getattr(args, "include_base", False))
    refresh = bool(getattr(args, "refresh", False))
    keep_staging = bool(getattr(args, "keep_staging", False))
    render_mermaid = bool(getattr(args, "render_mermaid", False))
    workbook = getattr(args, "workbook", None)

    languages = _collect_languages_from_args(args)
    result = build_translation_pack(
        str(survey_id),
        languages=languages or None,
        output_path=output,
        smart_name=smart_name,
        edf_overrides=edf_overrides or None,
        include_base=include_base,
        refresh=refresh,
        workbook_path=Path(workbook) if workbook else None,
        keep_staging=keep_staging,
        render_mermaid=render_mermaid,
    )

    success("[qsync:translations]", f"Pack created: {result.pack_path}")
    if keep_staging:
        success("[qsync:translations]", f"Staging preserved: {result.staging_dir}")


def handle_translations_workbook_pull(args: argparse.Namespace) -> None:
    from .terminal_output import info, success
    from .translations import pull_translations
    from .translations_workbook import populate_workbook_from_translation_maps

    survey_id = args.survey_id
    xlsx_path = args.xlsx or _default_xlsx_path_for_survey(survey_id)
    languages = _collect_languages_from_args(args)

    if args.refresh:
        pull_translations(survey_id, languages)

    used = populate_workbook_from_translation_maps(
        survey_id,
        Path(xlsx_path),
        languages=languages,
        overwrite=bool(args.overwrite),
    )
    info(
        "[qsync:translations]",
        f"Workbook updated: {xlsx_path}",
    )
    success(
        "[qsync:translations]",
        f"Populated translation columns for: {', '.join(used)}",
    )


def handle_translations_workbook_apply(args: argparse.Namespace) -> None:
    from .terminal_output import info, success, warn
    from .translations import apply_translations, fetch_base_language
    from .translations_workbook import extract_translation_maps_from_workbook

    survey_id = args.survey_id
    xlsx_path = args.xlsx or _default_xlsx_path_for_survey(survey_id)
    languages = _collect_languages_from_args(args)

    paths = extract_translation_maps_from_workbook(
        survey_id,
        Path(xlsx_path),
        languages=languages,
        allow_empty=bool(args.allow_empty),
    )
    for path in paths:
        info("[qsync:translations]", f"Wrote: {path}")

    staged_languages = [path.stem for path in paths]
    base_language = fetch_base_language(survey_id)
    staged_languages = [lang for lang in staged_languages if lang != base_language]
    if not staged_languages:
        warn(
            "[qsync:translations]",
            "No non-base languages to stage from workbook output.",
        )
        return
    staged = apply_translations(survey_id, staged_languages)
    if staged is None:
        warn("[qsync:translations]", "No translation changes detected.")
        return
    success(
        "[qsync:translations]",
        f"Pending translations staged: {', '.join(staged.payload.languages)}",
    )


def handle_translations_push(args: argparse.Namespace) -> None:
    from .errors import QsyncValidationError
    from .terminal_output import info, success, warn
    from .pending_stage import TranslationsPendingPayload, load_pending
    from .translations import (
        preview_translations,
        push_translations,
    )

    survey_id = args.survey_id
    mode = getattr(args, "mode", "apply")
    dry_run = mode == "validate"

    if "--mode" in sys.argv:
        warn(
            "[qsync:translations]",
            "Flag `--mode` is deprecated. Omit it to push, or use `--validate/--dry-run` to avoid writes.",
        )

    _warn_legacy_translations(args)
    languages = _collect_languages_from_args(args) or None
    scope_expr = getattr(args, "scope", None)
    scope = ScopeFilter.parse(scope_expr) if scope_expr else None

    preview_lines = preview_translations(
        survey_id,
        languages,
        detailed=bool(getattr(args, "detailed", False)),
        scope=scope,
    )
    for line in preview_lines:
        info("[qsync:translations]", line)

    if dry_run:
        success("[qsync:translations]", "Validation complete (no API writes).")
        return

    pending = load_pending(survey_id, "translations")
    if pending and isinstance(pending.payload, TranslationsPendingPayload):
        info(
            "[qsync:translations]",
            f"Pending: surveys/pending/translations/{survey_id}.json (schema v{getattr(pending, 'schema_version', 1)})",
        )

    if not args.yes:
        lang_label = ", ".join(languages) if languages else "auto"
        if not sys.stdin.isatty():
            raise QsyncValidationError(
                error_id="QSYNC-TRANSLATIONS-CONFIRM-001",
                problem="Confirmation required but stdin is not interactive.",
                why="Push was requested without --yes in a non-interactive shell.",
                impact="Push aborted.",
                action="Re-run with --yes to proceed.",
                context={"survey_id": survey_id},
            )
        try:
            from qsync.interactive_menu import confirm

            if not confirm(
                f"Push translations for {survey_id} ({lang_label})?", default=True
            ):
                info("[qsync:translations]", "Aborted.")
                return
        except Exception:
            confirm = (
                input(f"Push translations for {survey_id} ({lang_label})? [Y/n]: ")
                .strip()
                .lower()
            )
            if confirm and confirm != "y":
                info("[qsync:translations]", "Aborted.")
                return

    pushed = push_translations(
        survey_id=survey_id,
        languages=languages,
        scope=scope,
        dry_run=False,
        force_live=bool(getattr(args, "force_live", False)),
        force_preview=bool(getattr(args, "force_preview", False)),
        interactive=not bool(getattr(args, "yes", False)),
        allow_drift=bool(getattr(args, "allow_drift", False)),
        publish=not bool(getattr(args, "no_publish", False)),
        prefer_pending=getattr(args, "use_pending", None),
    )
    success(
        "[qsync:translations]",
        f"Pushed translations for {survey_id}: {len(pushed)} question(s)",
    )


def handle_master_pull(args: argparse.Namespace) -> None:
    """Pull survey master snapshots and generate/merge master CSV."""
    from .survey_master import pull_master

    mapping_csv = getattr(args, "mapping_csv", None)
    if mapping_csv:
        os.environ["QSYNC_MAPPING_CSV"] = str(Path(mapping_csv).expanduser().resolve())

    survey_ids = (
        args.survey_ids if hasattr(args, "survey_ids") and args.survey_ids else None
    )
    verbose = bool(getattr(args, "verbose", False))
    force_overwrite = bool(getattr(args, "force_overwrite", False))

    try:
        snapshots_created, csv_path = pull_master(
            survey_ids=survey_ids,
            verbose=verbose,
            force_overwrite=force_overwrite,
        )
        print(f"\n✓ Pull complete: {snapshots_created} snapshots, CSV at {csv_path}")
        if not force_overwrite:
            print("  (User edits preserved via merge)")
        print("\n💡 Next: Edit CSV, then run 'qsync survey master stage'")
    except Exception as e:
        print(f"[qsync:master-pull] ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def handle_master_columns(args: argparse.Namespace) -> None:
    """Interactive TUI to configure Survey Master columns (order + visibility)."""
    from .interactive_menu import is_interactive

    if not is_interactive():
        print(
            "[qsync:master-columns] ERROR: Interactive TTY required (non-TTY/CI not supported).",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if os.environ.get("QSYNC_JSON_MODE", "").strip():
        print(
            "[qsync:master-columns] ERROR: JSON mode is not compatible with the TUI.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    try:
        from .tui.master_columns import MasterColumnsApp  # lazy import (Textual is optional)
    except Exception:
        print(
            "[qsync:master-columns] ERROR: TUI dependencies are not installed.",
            file=sys.stderr,
        )
        print("Install: pip install 'qsync[tui]'", file=sys.stderr)
        print(
            "If using pipx: pipx install --force 'qsync[tui] @ <git-ref>'",
            file=sys.stderr,
        )
        raise SystemExit(1)

    from .survey_master import _parse_mapping_csv, _get_default_column_order
    from .survey_master_columns import (
        master_columns_config_path,
        load_master_columns_yaml,
        resolve_master_columns,
    )
    from .config import resolve_root

    root = resolve_root(required=False) or Path.cwd()
    mapping = _parse_mapping_csv()
    default_order = _get_default_column_order(mapping)

    config_path = master_columns_config_path(root=root)
    config_data = load_master_columns_yaml(config_path)
    columns, warnings = resolve_master_columns(
        available_in_default_order=default_order,
        config_data=config_data,
        default_enabled_when_no_config=True,
        default_enabled_when_missing=False,
    )

    if warnings:
        for warning in warnings:
            print(f"[qsync:master-columns] WARNING: {warning}", file=sys.stderr)

    MasterColumnsApp(columns=columns, config_path=config_path).run()
    print(f"[qsync:master-columns] Config path: {config_path}")


def handle_master_preview(args: argparse.Namespace) -> None:
    """Preview changes that would be applied to Qualtrics."""
    import difflib
    import json
    from .survey_master import preview_master, load_master_csv
    from .survey_tags import parse_tag_filters, filter_surveys_by_tags
    from .survey_inventory import load_focal_snapshot
    from .terminal_colors import (
        colored,
        Colors,
        diff_colored,
        colors_enabled,
        colorize_unified_diff_lines,
    )

    survey_id = getattr(args, "survey_id", None)
    output_format = getattr(args, "format", "text")
    tag_specs = getattr(args, "tags", None)
    all_surveys = bool(getattr(args, "all_surveys", False))
    use_color = colors_enabled()

    def _needs_unified_diff(value: str) -> bool:
        if "\n" in value:
            return True
        return len(value) > 140

    mapping_csv = getattr(args, "mapping_csv", None)
    if mapping_csv:
        os.environ["QSYNC_MAPPING_CSV"] = str(Path(mapping_csv).expanduser().resolve())

    try:
        # Load CSV and apply tag filtering if specified
        csv_headers, csv_rows = load_master_csv()

        if tag_specs:
            try:
                tag_filters = parse_tag_filters(tag_specs)

                # Extract survey IDs from CSV
                survey_ids_in_csv = [
                    row.get("SurveyID", "").strip()
                    for row in csv_rows
                    if row.get("SurveyID", "").strip()
                ]

                # Filter by tags
                filtered_ids = filter_surveys_by_tags(survey_ids_in_csv, tag_filters)
                filtered_ids_set = set(filtered_ids)

                # Filter CSV rows to only include matching surveys
                csv_rows = [
                    row
                    for row in csv_rows
                    if row.get("SurveyID", "").strip() in filtered_ids_set
                ]

                if output_format == "text" and not survey_id:
                    print(
                        f"[qsync:master-preview] Filtered to {len(csv_rows)} surveys matching tags: {', '.join(tag_specs)}"
                    )

            except ValueError as e:
                print(f"❌ Tag filter error: {e}", file=sys.stderr)
                sys.exit(1)

        # Default: focal-only (inventory-driven). Use --all-surveys to include non-focal.
        if not survey_id and not all_surveys:
            focal_snapshot = load_focal_snapshot()
            focal_ids = {sid for sid, is_focal in focal_snapshot.items() if is_focal}
            if focal_ids:
                before = len(csv_rows)
                csv_rows = [
                    row
                    for row in csv_rows
                    if row.get("SurveyID", "").strip() in focal_ids
                ]
                if output_format == "text" and before != len(csv_rows):
                    print(
                        f"[qsync:master-preview] Filtered to {len(csv_rows)}/{before} focal survey row(s) (use --all-surveys to include non-focal)"
                    )

        result = preview_master(
            csv_headers=csv_headers,
            csv_rows=csv_rows,
            verbose=bool(getattr(args, "verbose", False)) and output_format == "text",
            survey_id=survey_id,
        )

        # Output as JSON if requested
        if output_format == "json":
            print(json.dumps(result, indent=2))
            if result["validation_errors"]:
                sys.exit(1)
            return

        # Check for validation errors (text format)
        if result["validation_errors"]:
            error_header = "❌ Validation failed:"
            if use_color:
                error_header = colored(error_header, Colors.RED, bold=True)
            print(f"\n{error_header}")
            for err in result["validation_errors"]:
                if use_color:
                    err_colored = colored(err, Colors.RED)
                else:
                    err_colored = err
                print(f"  - {err_colored}")
            sys.exit(1)

        # Print summary
        summary = result.get("summary", {})
        survey_diffs = result.get("survey_diffs", [])

        if summary["total_changes"] == 0:
            no_changes = "✓ No changes to apply"
            if use_color:
                no_changes = colored(no_changes, Colors.GREEN)
            print(f"\n{no_changes}")
            return

        summary_header = "📊 Preview Summary:"
        if use_color:
            summary_header = colored(summary_header, Colors.CYAN, bold=True)
        print(f"\n{summary_header}")

        changes_line = f"  Surveys with changes: {summary['surveys_with_changes']}/{summary['total_surveys']}"
        if use_color:
            changes_line = colored(changes_line, Colors.WHITE)
        print(changes_line)

        fields_line = f"  Total fields to change: {summary['total_changes']}"
        if use_color:
            fields_line = colored(fields_line, Colors.WHITE)
        print(fields_line)

        if summary["requires_publish"]:
            pub_line = "  ⚠️  Publishing required: YES (definition changes detected)"
            if use_color:
                pub_line = colored(pub_line, Colors.YELLOW, bold=True)
            print(pub_line)

        if summary["has_dangerous"]:
            danger_line = (
                "  ⚠️  Dangerous changes: YES (requires --allow-dangerous flag)"
            )
            if use_color:
                danger_line = colored(danger_line, Colors.RED, bold=True)
            print(danger_line)

        # Print per-survey details if requested
        if getattr(args, "detail", False):
            detail_header = "📋 Detailed Changes:"
            if use_color:
                detail_header = colored(detail_header, Colors.CYAN, bold=True)
            print(f"\n{detail_header}")

            for diff in survey_diffs:
                if diff.get("error"):
                    error_line = f"\n  ❌ {diff['survey_id']}: {diff['error']}"
                    if use_color:
                        error_line = colored(error_line, Colors.RED)
                    print(error_line)
                    continue

                if not diff["changes"]:
                    continue

                survey_header = f"\n  📝 {diff['survey_id']} - {diff['survey_name']}:"
                if use_color:
                    survey_header = colored(survey_header, Colors.CYAN)
                print(survey_header)

                for change in diff["changes"]:
                    is_dangerous = change.get("is_dangerous", False)
                    endpoint = change.get("endpoint", "unknown")
                    field = change.get("field_name", change.get("field", "unknown"))
                    old_value = change.get("old_value", "")
                    new_value = change.get("new_value", "")

                    # Color marker based on danger status
                    marker = "⚠️ " if is_dangerous else "   "
                    if use_color and is_dangerous:
                        marker = colored(marker, Colors.RED, bold=True)

                    # Color endpoint label
                    endpoint_label = f"[{endpoint}]"
                    if use_color:
                        if endpoint == "metadata":
                            endpoint_label = colored(endpoint_label, Colors.CYAN)
                        elif endpoint == "options":
                            endpoint_label = colored(endpoint_label, Colors.BLUE)
                        elif endpoint == "status":
                            endpoint_label = colored(endpoint_label, Colors.YELLOW)

                    # Color field name
                    field_label = field
                    if use_color:
                        field_label = colored(field_label, Colors.WHITE, bold=True)

                    print(f"    {marker}{endpoint_label} {field_label}")

                    show_unified = False
                    if isinstance(old_value, str) and isinstance(new_value, str):
                        if _needs_unified_diff(old_value) or _needs_unified_diff(
                            new_value
                        ):
                            show_unified = True

                    if show_unified:
                        diff_lines = list(
                            difflib.unified_diff(
                                old_value.splitlines(),
                                new_value.splitlines(),
                                fromfile="current",
                                tofile="master",
                                lineterm="",
                            )
                        )
                        if diff_lines:
                            if use_color:
                                diff_lines = colorize_unified_diff_lines(diff_lines)
                            max_lines = 120
                            for line in diff_lines[:max_lines]:
                                print(f"       {line}")
                            if len(diff_lines) > max_lines:
                                print(
                                    f"       ... ({len(diff_lines) - max_lines} more lines)"
                                )
                        else:
                            print("       (no diff)")
                        continue

                    # Format diff with colors
                    if use_color:
                        old_fmt, new_fmt = diff_colored(
                            old_value, new_value, max_width=70
                        )
                    else:
                        old_fmt = old_value[: 70 - 3] + (
                            "..." if len(old_value) > 70 else ""
                        )
                        new_fmt = new_value[: 70 - 3] + (
                            "..." if len(new_value) > 70 else ""
                        )

                    print(f"       {old_fmt} → {new_fmt}")

        next_line = "💡 Next: Run 'qsync survey master stage' to stage changes, then 'qsync survey master push'"
        if use_color:
            next_line = colored(next_line, Colors.GREEN)
        print(f"\n{next_line}")

    except Exception as e:
        error_msg = f"[qsync:master-preview] ERROR: {e}"
        if use_color:
            error_msg = colored(error_msg, Colors.RED)
        print(error_msg, file=sys.stderr)
        sys.exit(1)


def handle_master_stage(args: argparse.Namespace) -> None:
    """Stage changes from master CSV to pending."""
    from .survey_master import stage_master, load_master_csv
    from .survey_inventory import load_focal_snapshot
    from .terminal_colors import colors_enabled, colored, Colors

    use_color = colors_enabled()
    survey_id = getattr(args, "survey_id", None)
    verbose = bool(getattr(args, "verbose", False))
    all_surveys = bool(getattr(args, "all_surveys", False))

    try:
        csv_headers, csv_rows = load_master_csv()

        # Apply tag filtering if provided
        tags_str = getattr(args, "tags", None)
        if tags_str:
            try:
                from .survey_tags import parse_tag_filters, filter_surveys_by_tags

                tag_filters = parse_tag_filters(tags_str)
                survey_ids_in_csv = [
                    row.get("SurveyID", "").strip()
                    for row in csv_rows
                    if row.get("SurveyID", "").strip()
                ]
                filtered_ids = filter_surveys_by_tags(survey_ids_in_csv, tag_filters)
                filtered_ids_set = set(filtered_ids)
                csv_rows = [
                    row
                    for row in csv_rows
                    if row.get("SurveyID", "").strip() in filtered_ids_set
                ]

                if not survey_id:
                    print(
                        f"[qsync:master-stage] Filtered to {len(csv_rows)} surveys matching tags: {', '.join(tags_str)}"
                    )
            except ValueError as e:
                print(f"❌ Tag filter error: {e}", file=sys.stderr)
                sys.exit(1)

        # Default: focal-only (inventory-driven). Use --all-surveys to include non-focal.
        if not survey_id and not all_surveys:
            focal_snapshot = load_focal_snapshot()
            focal_ids = {sid for sid, is_focal in focal_snapshot.items() if is_focal}
            if focal_ids:
                before = len(csv_rows)
                csv_rows = [
                    row
                    for row in csv_rows
                    if row.get("SurveyID", "").strip() in focal_ids
                ]
                if before != len(csv_rows):
                    print(
                        f"[qsync:master-stage] Filtered to {len(csv_rows)}/{before} focal survey row(s) (use --all-surveys to include non-focal)"
                    )

        result = stage_master(
            csv_headers=csv_headers,
            csv_rows=csv_rows,
            verbose=verbose,
            survey_id=survey_id,
        )

        if result["validation_errors"]:
            error_header = "[qsync:master-stage] Validation failed:"
            if use_color:
                error_header = colored(error_header, Colors.RED)
            print(error_header)
            for err in result["validation_errors"]:
                error_line = f"  - {err}"
                if use_color:
                    error_line = colored(error_line, Colors.RED)
                print(error_line)
            sys.exit(1)

        # Display summary
        summary_header = "\nStage Summary:"
        if use_color:
            summary_header = colored(summary_header, Colors.CYAN, bold=True)
        print(summary_header)

        info_lines = [
            f"  Surveys staged: {result['staged_surveys']}",
            f"  Total changes: {result['total_changes']}",
        ]
        for line in info_lines:
            if use_color:
                line = colored(line, Colors.WHITE)
            print(line)

        if result["staged_surveys"] > 0:
            success_msg = f"\n✓ Staged {result['staged_surveys']} survey(s)"
            next_step = "\n💡 Next: Run 'qsync survey master push'"
            if use_color:
                success_msg = colored(success_msg, Colors.GREEN, bold=True)
                next_step = colored(next_step, Colors.GREEN)
            print(success_msg)
            print(next_step)
        else:
            info_msg = "\nNo changes to stage"
            if use_color:
                info_msg = colored(info_msg, Colors.YELLOW)
            print(info_msg)

    except Exception as e:
        error_msg = f"[qsync:master-stage] ERROR: {e}"
        if use_color:
            error_msg = colored(error_msg, Colors.RED)
        print(error_msg, file=sys.stderr)
        sys.exit(1)


def handle_master_apply(args: argparse.Namespace) -> None:
    """Apply changes from master CSV to Qualtrics."""
    from .survey_master import apply_master, load_master_csv
    from .survey_inventory import load_focal_snapshot
    from .survey_tags import parse_tag_filters, filter_surveys_by_tags
    from .terminal_output import error, header, info, success, warn

    allow_dangerous = getattr(args, "allow_dangerous", False)
    force = getattr(args, "force", False)
    survey_id = getattr(args, "survey_id", None)
    skip_drift = getattr(args, "skip_drift", False)
    dry_run = getattr(args, "dry_run", False)
    tag_specs = getattr(args, "tags", None)
    all_surveys = bool(getattr(args, "all_surveys", False))
    warn(
        "[qsync:master-apply]",
        "Legacy command: prefer 'qsync survey master stage' then 'qsync survey master push'.",
    )

    try:
        mapping_csv = getattr(args, "mapping_csv", None)
        if mapping_csv:
            os.environ["QSYNC_MAPPING_CSV"] = str(
                Path(mapping_csv).expanduser().resolve()
            )

        # Load CSV and apply tag filtering if specified
        csv_headers, csv_rows = load_master_csv()
        filtered_csv_rows = csv_rows

        if tag_specs:
            try:
                tag_filters = parse_tag_filters(tag_specs)

                # Extract survey IDs from CSV
                survey_ids_in_csv = [
                    row.get("SurveyID", "").strip()
                    for row in csv_rows
                    if row.get("SurveyID", "").strip()
                ]

                # Filter by tags
                filtered_ids = filter_surveys_by_tags(survey_ids_in_csv, tag_filters)
                filtered_ids_set = set(filtered_ids)

                # Filter CSV rows to only include matching surveys
                filtered_csv_rows = [
                    row
                    for row in csv_rows
                    if row.get("SurveyID", "").strip() in filtered_ids_set
                ]

                info(
                    "[qsync:master-apply]",
                    f"Filtered to {len(filtered_csv_rows)} surveys matching tags: {', '.join(tag_specs)}",
                )

            except ValueError as e:
                error("[qsync:master-apply]", f"Tag filter error: {e}")
                sys.exit(1)

        # Default: focal-only (inventory-driven). Use --all-surveys to include non-focal.
        if not survey_id and not all_surveys:
            focal_snapshot = load_focal_snapshot()
            focal_ids = {sid for sid, is_focal in focal_snapshot.items() if is_focal}
            if focal_ids:
                before = len(filtered_csv_rows)
                filtered_csv_rows = [
                    row
                    for row in filtered_csv_rows
                    if row.get("SurveyID", "").strip() in focal_ids
                ]
                if before != len(filtered_csv_rows):
                    info(
                        "[qsync:master-apply]",
                        f"Filtered to {len(filtered_csv_rows)}/{before} focal survey row(s) (use --all-surveys to include non-focal)",
                    )

        result = apply_master(
            allow_dangerous=allow_dangerous,
            force=force,
            verbose=bool(getattr(args, "verbose", False)),
            survey_id=survey_id,
            skip_drift=skip_drift,
            dry_run=dry_run,
            csv_rows=filtered_csv_rows,
        )

        # Check for errors
        if result["errors"]:
            error("[qsync:master-apply]", "Apply failed.")
            for err in result["errors"]:
                error(None, f"- {err}")
            sys.exit(1)

        # Print summary
        dry_run_marker = "[DRY RUN] " if result.get("dry_run", False) else ""
        header(None, f"{dry_run_marker}Apply Summary:")
        info(None, f"Total surveys: {result['total_surveys']}")
        info(None, f"Surveys applied: {result['surveys_applied']}")
        info(None, f"Surveys failed: {result['surveys_failed']}")

        # Print details (by default, only show applied and failures; suppress no-op noise)
        if result["details"]:
            show_all = bool(getattr(args, "verbose", False))
            filtered = []
            for d in result["details"]:
                if d.get("applied"):
                    filtered.append(d)
                    continue
                reason = str(d.get("reason") or "")
                if show_all:
                    filtered.append(d)
                else:
                    if reason and reason != "No changes":
                        filtered.append(d)

            if filtered:
                header(None, "Details:")
                for detail in filtered:
                    status = "✓" if detail["applied"] else "✗"
                    if detail["applied"]:
                        success(
                            None, f"{status} {detail['survey_id']}: {detail['reason']}"
                        )
                    else:
                        warn(None, f"{status} {detail['survey_id']}: {detail['reason']}")

        if result["surveys_applied"] > 0:
            if result.get("dry_run", False):
                success(
                    "[qsync:master-apply]",
                    f"Dry run complete: {result['surveys_applied']} survey/surveys would be updated",
                )
                info("[qsync:master-apply]", "Run without --dry-run to apply changes")
            else:
                success(
                    "[qsync:master-apply]",
                    f"Apply complete: {result['surveys_applied']} survey/surveys updated",
                )
                info(
                    "[qsync:master-apply]",
                    "💡 Next: Run 'qsync survey master push' to publish the changes",
                )
        else:
            info("[qsync:master-apply]", "No surveys were updated")

    except Exception as e:
        error("[qsync:master-apply]", f"ERROR: {e}")
        sys.exit(1)


def handle_master_push(args: argparse.Namespace) -> None:
    """Handle 'qsync survey master push' command (NEW: applies staged changes)."""

    from .survey_master import push_master
    from .terminal_output import error, header, info, success, warn

    description = getattr(args, "description", None)
    survey_id = getattr(args, "survey_id", None)
    all_surveys = bool(getattr(args, "all_surveys", False))
    no_publish = bool(getattr(args, "no_publish", False))
    force_live = bool(getattr(args, "force_live", False))
    force_preview = bool(getattr(args, "force_preview", False))
    auto_yes = bool(getattr(args, "yes", False))
    allow_dangerous = bool(getattr(args, "allow_dangerous", False))
    allow_locked = bool(getattr(args, "allow_locked", False))

    try:
        mapping_csv = getattr(args, "mapping_csv", None)
        if mapping_csv:
            os.environ["QSYNC_MAPPING_CSV"] = str(
                Path(mapping_csv).expanduser().resolve()
            )

        result = push_master(
            description=description,
            verbose=bool(getattr(args, "verbose", False)),
            survey_id=survey_id,
            all_surveys=all_surveys,
            no_publish=no_publish,
            force_live=force_live,
            force_preview=force_preview,
            auto_yes=auto_yes,
            allow_dangerous=allow_dangerous,
            allow_locked=allow_locked,
        )

        # Check for errors
        if result["errors"]:
            error("[qsync:master-push]", "Push failed.")
            for err in result["errors"]:
                error(None, f"- {err}")
            sys.exit(1)

        # Print summary
        header(None, "Push Summary:")
        info(None, f"Total surveys: {result['total_surveys']}")
        info(None, f"Surveys pushed (API write): {result['surveys_pushed']}")
        info(None, f"Surveys published: {result['surveys_published']}")
        info(None, f"Surveys failed: {result['surveys_failed']}")

        # Print details (default: only show failures; use --verbose for all)
        if result["details"]:
            show_all = bool(getattr(args, "verbose", False))
            filtered = [
                d
                for d in result["details"]
                if show_all or not bool(d.get("pushed"))
            ]
            if filtered:
                header(None, "Details:")
                for detail in filtered:
                    pushed = detail.get("pushed", False)
                    status = "✓" if pushed else "✗"
                    if pushed:
                        success(
                            None,
                            f"{status} {detail['survey_id']}: {detail['reason']}",
                        )
                    else:
                        warn(None, f"{status} {detail['survey_id']}: {detail['reason']}")

        if result["surveys_pushed"] > 0:
            success(
                "[qsync:master-push]",
                f"Push complete: {result['surveys_pushed']} survey(s) pushed, {result['surveys_published']} published",
            )
        else:
            info("[qsync:master-push]", "No surveys were pushed")

        if result["surveys_failed"] > 0:
            sys.exit(1)

    except Exception as e:
        error("[qsync:master-push]", f"ERROR: {e}")
        sys.exit(1)


def handle_master_rollback(args: argparse.Namespace) -> None:
    """Handle 'qsync survey master rollback' command."""
    from .survey_master import list_rollback_versions, rollback_master
    from .terminal_output import error, header, info, success, warn

    survey_id = getattr(args, "survey_id", None)
    list_only = bool(getattr(args, "list", False))

    if list_only:
        entries = list_rollback_versions(survey_id=survey_id)
        if not entries:
            info(
                "[qsync:master-rollback]",
                "No rollback snapshots found"
                + (f" for {survey_id}" if survey_id else ""),
            )
            return

        header(None, "Available rollback snapshots:")
        current_survey = None
        for entry in entries:
            sid = entry.get("survey_id")
            if sid != current_survey:
                current_survey = sid
                info(None, f"{sid}:")
            fields = ", ".join(entry.get("fields") or [])
            if len(fields) > 80:
                fields = fields[:77] + "..."
            info(
                None,
                f"  v{entry.get('version')} | {entry.get('captured_at')} | "
                f"{entry.get('changes_count')} field(s)"
                + (f" | {fields}" if fields else ""),
            )
        return

    if not survey_id:
        error(
            "[qsync:master-rollback]", "--survey-id is required unless --list is used"
        )
        sys.exit(1)

    version = int(getattr(args, "version", 1))
    dry_run = bool(getattr(args, "dry_run", False))
    yes = bool(getattr(args, "yes", False))
    force = bool(getattr(args, "force", False))
    allow_dangerous = bool(getattr(args, "allow_dangerous", False))
    publish = not bool(getattr(args, "no_publish", False))
    description = getattr(args, "description", None)

    preview_result = rollback_master(
        survey_id=survey_id,
        version=version,
        dry_run=True,
        force=force,
        allow_dangerous=allow_dangerous,
        publish=False,
        publish_description=description,
        verbose=True,
    )

    if preview_result.get("error"):
        error("[qsync:master-rollback]", preview_result["error"])
        if preview_result.get("drifted_fields"):
            warn(None, "Drifted fields:")
            for item in preview_result["drifted_fields"]:
                warn(
                    None,
                    f"- {item.get('field')}: expected '{item.get('expected_post_apply')}', "
                    f"current '{item.get('current')}'",
                )
        sys.exit(1)

    changes = preview_result.get("changes", [])
    snapshot_path = preview_result.get("snapshot_path")
    if snapshot_path:
        info(None, f"Snapshot: {snapshot_path}")

    if not changes:
        success(
            "[qsync:master-rollback]", "No changes needed (already at rollback target)"
        )
        return

    header(None, "Rollback preview:")
    for change in changes:
        marker = "⚠️ " if change.get("is_dangerous") else "✏️ "
        info(
            None,
            f"{marker}[{change.get('endpoint')}] {change.get('field')}: "
            f"'{change.get('old_value')}' → '{change.get('new_value')}'",
        )

    if dry_run:
        success(
            "[qsync:master-rollback]",
            f"Dry run complete: would restore {len(changes)} field(s)",
        )
        return

    if not yes:
        if not sys.stdin.isatty():
            error(
                "[qsync:master-rollback]",
                "Confirmation required but stdin is not interactive. Re-run with --yes.",
            )
            sys.exit(1)
        try:
            from qsync.interactive_menu import confirm

            if not confirm(
                f"Rollback {survey_id} using snapshot version {version}?", default=False
            ):
                info("[qsync:master-rollback]", "Aborted.")
                return
        except Exception:
            ans = (
                input(f"Rollback {survey_id} using snapshot version {version}? [y/N]: ")
                .strip()
                .lower()
            )
            if ans != "y":
                info("[qsync:master-rollback]", "Aborted.")
                return

    # Execute after confirmation to ensure the live check is fresh.
    result = rollback_master(
        survey_id=survey_id,
        version=version,
        dry_run=False,
        force=force,
        allow_dangerous=allow_dangerous,
        publish=publish,
        publish_description=description,
        verbose=True,
    )
    if result.get("error"):
        error("[qsync:master-rollback]", result["error"])
        sys.exit(1)

    published_suffix = " and published" if result.get("published") else ""
    success(
        "[qsync:master-rollback]",
        f"Rollback complete: restored {len(result.get('changes', []))} field(s){published_suffix}",
    )


def register_survey_commands(subparsers: argparse._SubParsersAction) -> None:
    """Register `qsync survey ...` subcommands."""

    from .argparse_support import resolve_raw_description_formatter

    p_survey = subparsers.add_parser(
        "survey",
        help="Manage Qualtrics surveys (group; includes master)",
        formatter_class=resolve_raw_description_formatter(),
        description=(
            "Manage Qualtrics surveys.\n\n"
            "Groups:\n"
            "  Inventory/cache: list, label, focal, inventory, pull, prepare\n"
            "  Copy/derive: copy, slice-language, copy-cross-account, slice-registry, parity-check\n"
            "  Embedded/options: items structural edits, add-embedded-field, remove-embedded-field, rename-embedded-field, cleanup-embedded-data, prolific-auth\n"
            "  Prolific wiring: prolific-wiring (alias to qsync prolific)\n"
            "  Lifecycle/versions: publish, activate, deactivate, versions, version-fetch, rollback\n"
            "  Utilities: inspect-question, add-question, move-question, remove-question, add-page-break, remove-page-break, push-question, replace-question\n"
            "  Exports: export-responses, export-translation, export-side-by-side\n"
            "  Bulk: master (group; has subcommands)\n"
            "  Admin: rename, delete\n"
        ),
    )
    survey_subs = p_survey.add_subparsers(
        dest="survey_command",
        required=True,
        metavar="COMMAND",
    )

    # menu
    p_menu = survey_subs.add_parser("menu", help="Interactive survey admin menu")
    p_menu.add_argument(
        "--tui",
        action="store_true",
        help="Launch Textual TUI survey menu (requires qsync[tui]; keeps default menu unchanged).",
    )
    p_menu.add_argument(
        "--structural-edit",
        action="store_true",
        help="Jump directly to Items structural edits (skip category navigation).",
    )
    p_menu.add_argument(
        "--add-question-interactive",
        action="store_true",
        help="Jump directly to guided add-question flow (skip category navigation).",
    )
    p_menu.add_argument(
        "--move-question-interactive",
        action="store_true",
        help="Jump directly to guided move-question flow (skip category navigation).",
    )
    p_menu.add_argument(
        "--remove-question-interactive",
        action="store_true",
        help="Jump directly to guided remove-question flow (skip category navigation).",
    )
    p_menu.add_argument(
        "--replace-question-interactive",
        action="store_true",
        help="Jump directly to guided replace-question flow (skip category navigation).",
    )
    p_menu.add_argument(
        "--page-break-interactive",
        action="store_true",
        help="Jump directly to guided page-break flow (skip category navigation).",
    )
    p_menu.add_argument(
        "--survey-id",
        dest="survey_id",
        help=(
            "Preselect SurveyID for direct interactive modes "
            "(--structural-edit/--add-question-interactive/"
            "--move-question-interactive/--remove-question-interactive/"
            "--replace-question-interactive/"
            "--page-break-interactive)."
        ),
    )
    p_menu.add_argument(
        "--account",
        help="Use this account for the menu session (or 'default' for primary .env).",
    )
    p_menu.add_argument(
        "--quick-action",
        dest="quick_action",
        help=argparse.SUPPRESS,
    )
    p_menu.set_defaults(func=handle_menu)

    # label
    p_label = survey_subs.add_parser(
        "label",
        help="Print '<SurveyID> - <Name>' using surveys/inventory.csv (legacy: surveys/qualtrics_surveys.csv)",
    )
    p_label.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Survey ID (omit to select interactively)",
    )
    p_label.set_defaults(func=handle_label)

    # focal
    p_focal = survey_subs.add_parser(
        "focal",
        help="List SurveyIDs marked focal in surveys/inventory.csv (legacy: surveys/qualtrics_surveys.csv)",
    )
    p_focal.add_argument(
        "--newline",
        action="store_true",
        help="Print one survey ID per line (default: space-delimited)",
    )
    p_focal.set_defaults(func=handle_focal)

    # list
    p_list = survey_subs.add_parser("list", help="List all surveys")
    p_list.add_argument(
        "name_pattern",
        nargs="?",
        help="Optional regex to match survey names (case-insensitive)",
    )
    p_list.add_argument(
        "--account",
        help=(
            "Use credentials from `.env.<account>` under the workspace root "
            "(API-only; skips inventory-based ordering)."
        ),
    )
    p_list.set_defaults(func=handle_list)

    # copy
    p_copy = survey_subs.add_parser("copy", help="Copy a survey")
    p_copy.add_argument(
        "source_survey_id", nargs="?", help="Existing Qualtrics survey ID to copy"
    )
    p_copy.add_argument("name", nargs="?", help="Name for the new survey")
    p_copy.add_argument(
        "--from-qsf",
        dest="from_qsf",
        help="Import from local QSF file instead of Qualtrics",
    )
    p_copy.add_argument("--project-category", help="Optional project category")
    p_copy.add_argument("--language", help="Base language for the new survey")
    p_copy.add_argument(
        "--force-duplicate", action="store_true", help="Allow duplicate names"
    )
    p_copy.add_argument(
        "--generate-qsf", action="store_true", help="Generate QSF locally only"
    )
    p_copy.set_defaults(func=handle_copy)

    # slice-language
    p_slice = survey_subs.add_parser(
        "slice-language",
        help="Copy a multilingual survey into a new survey rebased to one language",
    )
    p_slice.add_argument(
        "source_survey_id", nargs="?", help="Existing Qualtrics survey ID to slice"
    )
    p_slice.add_argument(
        "--language",
        help="Target language code to use as the new base language (e.g., DE, FR-CA)",
    )
    p_slice.add_argument(
        "--languages",
        help="Comma-separated language codes to slice in one run (e.g., DE,FR,NL)",
    )
    p_slice.add_argument(
        "--name",
        help="Name for the new survey (default: '<SourceName> (<LANG>)')",
    )
    p_slice.add_argument(
        "--keep-languages",
        default="target-only",
        help="Languages to keep enabled: target-only|all|<comma-list> (default: target-only)",
    )
    p_slice.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="Proceed even if required keys are missing in the target language (writes a coverage report)",
    )
    p_slice.add_argument(
        "--allow-fallback",
        action="store_true",
        help="Fill missing target-language keys from the source base language (writes a coverage report)",
    )
    p_slice.add_argument(
        "--no-flow-text",
        action="store_true",
        help="Do not rebase SurveyFlow participant-visible text (warns if present)",
    )
    p_slice.add_argument(
        "--dry-run",
        action="store_true",
        help="Write the rebased QSF to disk without importing a new survey",
    )
    p_slice.add_argument(
        "--verify-parity",
        action="store_true",
        help="Run a parity check between the source and newly created survey",
    )
    p_slice.add_argument(
        "--force-duplicate",
        action="store_true",
        help="Allow duplicate survey names (uses local inventory to detect conflicts)",
    )
    p_slice.set_defaults(func=handle_slice_language)

    # slice-registry
    p_slice_registry = survey_subs.add_parser(
        "slice-registry",
        help="List derived surveys from local slice manifests",
    )
    p_slice_registry.add_argument(
        "--source",
        help="Filter results by source survey ID",
    )
    p_slice_registry.add_argument(
        "--limit",
        type=int,
        help="Limit the number of listed slices",
    )
    p_slice_registry.add_argument(
        "--open",
        action="store_true",
        help="Open edit links for listed surveys",
    )
    p_slice_registry.set_defaults(func=handle_slice_registry)

    # parity-check
    p_parity = survey_subs.add_parser(
        "parity-check",
        help="Compare two surveys for parity (flow/QID/tag-lite; optional deep)",
    )
    p_parity.add_argument(
        "--source-id",
        required=True,
        dest="source_id",
        help="Survey ID A (source)",
    )
    p_parity.add_argument(
        "--target-id",
        required=True,
        dest="target_id",
        help="Survey ID B (target)",
    )
    p_parity.add_argument(
        "--deep",
        action="store_true",
        help="Run deep parity against survey-definitions JSON.",
    )
    p_parity.add_argument(
        "--profile",
        choices=["strict", "cross_account", "split"],
        default="cross_account",
        help="Deep parity profile (default: cross_account).",
    )
    p_parity.add_argument(
        "--split",
        dest="split_profile",
        action="store_true",
        help="Alias for '--profile split'.",
    )
    p_parity.add_argument(
        "--manifest",
        help="Path to split manifest JSON (required for split profile).",
    )
    p_parity.set_defaults(func=handle_parity_check)

    # copy-cross-account
    p_copy_xacct = survey_subs.add_parser(
        "copy-cross-account", help="Copy a survey from one Qualtrics account to another"
    )
    p_copy_xacct.add_argument(
        "source_survey_id", help="Survey ID to copy from source account"
    )
    p_copy_xacct.add_argument("new_name", help="Name for the survey in target account")
    p_copy_xacct.add_argument(
        "--target-api-key", help="API key for target Qualtrics account"
    )
    p_copy_xacct.add_argument(
        "--target-base-url",
        help="Base URL for target Qualtrics account (e.g., iad1.qualtrics.com)",
    )
    p_copy_xacct.add_argument(
        "--target-account",
        help=(
            "Load target credentials from `.env.<account>` under the workspace root "
            "(overrides TARGET_* defaults; explicit --target-* flags still win). "
            "Use `default` to target the primary `.env` credentials."
        ),
    )
    p_copy_xacct.add_argument(
        "--source-api-key",
        help="API key for source account (optional; defaults to .env)",
    )
    p_copy_xacct.add_argument(
        "--source-base-url",
        help="Base URL for source account (optional; defaults to .env)",
    )
    p_copy_xacct.add_argument(
        "--source-account",
        help=(
            "Load source credentials from `.env.<account>` under the workspace root "
            "(explicit --source-* flags still win)."
        ),
    )
    p_copy_xacct.add_argument(
        "--activate", action="store_true", help="Activate the survey after copying"
    )
    p_copy_xacct.add_argument(
        "--publish",
        action="store_true",
        help="Publish the survey after copying (creates version)",
    )
    p_copy_xacct.add_argument(
        "--publish-description",
        help="Description for published version (max 140 chars, implies --publish)",
    )
    p_copy_xacct.add_argument(
        "--force-overwrite",
        action="store_true",
        help=(
            "If name exists in target, delete and replace. WARNING: this permanently deletes "
            "the target survey (including its version/publish history) and the replacement will have a NEW SurveyID."
        ),
    )
    p_copy_xacct.add_argument(
        "--no-translations",
        action="store_true",
        help="Do not copy survey translations (languages + strings) (default: copy translations).",
    )
    p_copy_xacct.add_argument(
        "--verify",
        action="store_true",
        help="After copy, verify parity (QIDs/flow/tags) and translations (best-effort); exits non-zero on mismatch.",
    )
    p_copy_xacct.add_argument(
        "--verify-deep",
        action="store_true",
        help="After copy, verify deep parity against survey-definitions JSON; exits non-zero on mismatch.",
    )
    p_copy_xacct.add_argument(
        "--verify-deep-profile",
        choices=["strict", "cross_account", "split"],
        default="cross_account",
        help="Deep parity profile used with --verify-deep (default: cross_account).",
    )
    p_copy_xacct.add_argument(
        "--verify-deep-split",
        action="store_true",
        help="Alias for '--verify-deep-profile split'.",
    )
    p_copy_xacct.add_argument(
        "--verify-deep-manifest",
        help="Split manifest path used for --verify-deep-profile split.",
    )
    p_copy_xacct.set_defaults(func=handle_copy_cross_account)

    # rename
    p_rename = survey_subs.add_parser("rename", help="Rename a survey")
    p_rename.add_argument("survey_id", nargs="?", help="Qualtrics survey ID to rename")
    p_rename.add_argument("new_name", nargs="?", help="New name for the survey")
    p_rename.set_defaults(func=handle_rename)

    # delete
    p_delete = survey_subs.add_parser("delete", help="Delete survey(s)")
    p_delete.add_argument(
        "--account",
        help="Use credentials from `.env.<account>` under the workspace root.",
    )
    p_delete.add_argument(
        "--force-live",
        action="store_true",
        help=(
            "Allow deletion even when finished responses exist "
            "(normally blocked)."
        ),
    )
    p_delete.add_argument(
        "survey_ids", nargs="+", help="One or more Survey IDs to delete"
    )
    p_delete.set_defaults(func=handle_delete)

    # inventory
    p_inventory = survey_subs.add_parser(
        "inventory",
        help="Refresh the Qualtrics survey inventory cache",
    )
    p_inventory_counts = p_inventory.add_mutually_exclusive_group()
    p_inventory_counts.add_argument(
        "--focal",
        dest="counts_scope",
        action="store_const",
        const="focal",
        help="Fetch response counts for focal surveys",
    )
    p_inventory_counts.add_argument(
        "--full",
        dest="counts_scope",
        action="store_const",
        const="full",
        help="Fetch response counts for all surveys",
    )
    p_inventory.add_argument(
        "--survey-id",
        action="append",
        dest="survey_ids",
        help="Limit refresh to specific survey ID(s) (repeatable, comma-separated)",
    )
    p_inventory.add_argument(
        "--dry-run",
        action="store_true",
        help="Print stats without writing to disk",
    )
    p_inventory.set_defaults(func=handle_inventory)

    # prepare (workspace hydration)
    p_prepare = survey_subs.add_parser(
        "prepare",
        help="Hydrate local editing surfaces for one or more surveys (pull-only)",
    )
    p_prepare.add_argument(
        "--survey-id",
        action="append",
        dest="survey_id",
        help="Qualtrics survey ID(s) (repeatable/comma-separated; omit to select interactively)",
    )
    sel = p_prepare.add_mutually_exclusive_group()
    sel.add_argument(
        "--focal",
        action="store_true",
        help="Prepare all focal surveys from surveys/inventory.csv",
    )
    sel.add_argument(
        "--all-surveys",
        dest="all_surveys",
        action="store_true",
        help="Prepare all surveys from surveys/inventory.csv (can be slow)",
    )
    p_prepare.add_argument(
        "--surfaces",
        help=(
            "Comma-separated surfaces to hydrate (default: all). "
            "Choices: inventory,items,workbook,translations,eos,js"
        ),
    )
    p_prepare.add_argument(
        "--language",
        action="append",
        dest="language",
        help="Limit translation languages (repeatable)",
    )
    p_prepare.add_argument(
        "--languages",
        help="Comma-separated language codes to limit translations/workbook columns",
    )
    p_prepare.add_argument(
        "--overwrite-js",
        action="store_true",
        help="Allow overwriting existing survey_js/core files when generating JS surfaces",
    )
    p_prepare.add_argument(
        "--shared-js",
        action="store_true",
        help="Also extract shared (duplicate) QuestionJS blocks into a single file and map all QIDs to it",
    )
    p_prepare.set_defaults(func=handle_prepare)

    # embedded field mutations (stage-only)
    p_add_embedded = survey_subs.add_parser(
        "add-embedded-field",
        help="Stage a new embedded data field in SurveyFlow (requires qsync push)",
    )
    p_add_embedded.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Qualtrics survey ID to update (omit to select interactively)",
    )
    p_add_embedded.add_argument(
        "--field",
        required=True,
        dest="field",
        help="Embedded data field name to add",
    )
    p_add_embedded.add_argument(
        "--value",
        dest="value",
        help="Optional default value for the new field (default: empty string)",
    )
    p_add_embedded.add_argument(
        "--flow-id",
        dest="flow_id",
        help="Target EmbeddedData FlowID (default: first EmbeddedData block)",
    )
    p_add_embedded.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without staging",
    )
    p_add_embedded.set_defaults(func=handle_add_embedded_field)

    p_remove_embedded = survey_subs.add_parser(
        "remove-embedded-field",
        help="Stage removal of an embedded data field in SurveyFlow (requires qsync push)",
    )
    p_remove_embedded.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Qualtrics survey ID to update (omit to select interactively)",
    )
    p_remove_embedded.add_argument(
        "--field",
        required=True,
        dest="field",
        help="Embedded data field name to remove",
    )
    p_remove_embedded.add_argument(
        "--flow-id",
        dest="flow_id",
        help="Target EmbeddedData FlowID (default: all EmbeddedData blocks)",
    )
    p_remove_embedded.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without staging",
    )
    p_remove_embedded.set_defaults(func=handle_remove_embedded_field)

    p_rename_embedded = survey_subs.add_parser(
        "rename-embedded-field",
        help="Stage renaming an embedded data field in SurveyFlow (requires qsync push)",
    )
    p_rename_embedded.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Qualtrics survey ID to update (omit to select interactively)",
    )
    p_rename_embedded.add_argument(
        "--from",
        dest="from_field",
        required=True,
        help="Existing embedded data field name",
    )
    p_rename_embedded.add_argument(
        "--to",
        dest="to_field",
        required=True,
        help="New embedded data field name",
    )
    p_rename_embedded.add_argument(
        "--flow-id",
        dest="flow_id",
        help="Target EmbeddedData FlowID (default: requires unique match unless --all)",
    )
    p_rename_embedded.add_argument(
        "--all",
        dest="all_occurrences",
        action="store_true",
        help="Rename across all matching EmbeddedData blocks",
    )
    p_rename_embedded.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without staging",
    )
    p_rename_embedded.set_defaults(func=handle_rename_embedded_field)

    # pull
    p_pull = survey_subs.add_parser(
        "pull",
        help="Download a survey definition JSON to local cache",
    )
    p_pull.add_argument(
        "--survey-id",
        action="append",
        dest="survey_id",
        help="Qualtrics survey ID(s) to download (repeatable/comma-separated; omit to select interactively)",
    )
    p_pull.add_argument(
        "--dest",
        help=(
            "Destination directory (default: surveys/, or "
            "surveys/.<account>/ when --account is used)."
        ),
    )
    p_pull.add_argument(
        "--account",
        help="Use credentials from `.env.<account>` under the workspace root.",
    )
    p_pull.set_defaults(func=handle_pull)

    # cleanup embedded data
    p_cleanup = survey_subs.add_parser(
        "cleanup-embedded-data",
        help="Remove duplicate embedded data placeholder rows in SurveyFlow",
    )
    p_cleanup.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Qualtrics survey ID to clean (omit to select interactively)",
    )
    p_cleanup.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be removed (default if --apply not set)",
    )
    p_cleanup.add_argument(
        "--apply",
        action="store_true",
        help="Apply cleanup to Qualtrics SurveyFlow",
    )
    p_cleanup.add_argument(
        "--publish",
        action="store_true",
        help="Publish a new version after cleanup",
    )
    p_cleanup.add_argument(
        "--description",
        default="qsync cleanup: embedded data placeholders",
        help=f"Publish description (max {SURVEY_VERSION_DESCRIPTION_MAX_CHARS} chars)",
    )
    p_cleanup.add_argument(
        "--all-duplicates",
        action="store_true",
        help="Remove duplicate embedded data entries for any field (dangerous)",
    )
    p_cleanup.set_defaults(func=handle_cleanup_embedded_data)

    # prolific-auth (Prolific authenticity checks)
    p_prolific_auth = survey_subs.add_parser(
        "prolific-auth",
        help="Set (or append) a Prolific authenticity-check HTML snippet in SurveyOptions.Header",
    )
    p_prolific_auth.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Qualtrics survey ID to update (omit to select interactively or enter manually)",
    )
    p_prolific_auth.add_argument(
        "--snippet",
        dest="snippet",
        help="HTML snippet to set (useful for scripting; otherwise you'll be prompted to paste)",
    )
    p_prolific_auth.add_argument(
        "--file",
        dest="file",
        help="Read the snippet from a file path (UTF-8)",
    )
    p_prolific_auth.add_argument(
        "--mode",
        choices=["append", "replace"],
        help="How to apply the snippet when Header already exists (default: prompt; non-interactive requires this or --yes)",
    )
    p_prolific_auth.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the change without calling the API",
    )
    p_prolific_auth.add_argument(
        "--print-current",
        dest="print_current",
        action="store_true",
        help="Print the current SurveyOptions.Header and exit",
    )
    p_prolific_auth.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip Prolific-specific snippet validation checks",
    )
    p_prolific_auth.add_argument(
        "--no-publish",
        action="store_true",
        help="Skip auto-publish after writing the header (by default, qsync publishes so changes are immediately live)",
    )
    p_prolific_auth.add_argument(
        "--no-activate",
        action="store_true",
        help="Skip auto-activate after updating the header (by default, qsync sets isActive=true)",
    )
    p_prolific_auth.set_defaults(func=handle_prolific_auth)

    # prolific-wiring: backward-compat alias of `qsync prolific`, kept for discoverability
    # under the survey namespace.  Both registration sites (cli.py for `qsync prolific`
    # and here for `qsync survey prolific-wiring`) call the same
    # register_prolific_commands() from cli_prolific.py, so all subcommand parsers and
    # set_defaults(func=...) handler bindings are shared -- no handler logic is duplicated.
    from .cli_prolific import register_prolific_commands

    register_prolific_commands(
        survey_subs,
        command_name="prolific-wiring",
        help_text="Automate Prolific ↔ Qualtrics wiring workflows (alias of `qsync prolific`)",
    )

    # publish
    p_publish = survey_subs.add_parser(
        "publish",
        help="Publish staged survey-definition changes (create a published version)",
    )
    p_publish.add_argument(
        "--survey-id",
        action="append",
        dest="survey_id",
        help="Qualtrics survey ID(s) to publish (repeatable/comma-separated; omit to select interactively)",
    )
    p_publish.add_argument(
        "--description",
        default="Published via qsync",
        help=f"Version description recorded in Qualtrics (max {SURVEY_VERSION_DESCRIPTION_MAX_CHARS} chars)",
    )
    p_publish.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the request without calling the API",
    )
    p_publish.add_argument(
        "--retry-attempts",
        type=int,
        default=1,
        help="Retry the publish operation this many times (in addition to built-in HTTP retries).",
    )
    p_publish.set_defaults(func=handle_publish)

    # activate
    p_activate = survey_subs.add_parser(
        "activate",
        help="Activate a survey (set isActive=true)",
    )
    p_activate.add_argument(
        "--survey-id",
        action="append",
        dest="survey_id",
        help="Qualtrics Survey IDs to activate (repeatable; omit for interactive selection)",
    )
    p_activate.add_argument(
        "--survey-ids-file",
        dest="survey_ids_file",
        help="Path to a newline- or CSV-delimited list of Survey IDs",
    )
    p_activate.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the intended change without calling the API",
    )
    p_activate.add_argument(
        "--force-live",
        action="store_true",
        help="Allow activation even if finished responses exist",
    )
    p_activate.add_argument(
        "--show-versions",
        action="store_true",
        help="Show recent version history in the preview",
    )
    p_activate.add_argument(
        "--versions-limit",
        type=int,
        default=5,
        help="Limit version history rows when --show-versions is set (default: 5)",
    )
    p_activate.add_argument(
        "--show-owner",
        action="store_true",
        help="Show owner info (if present in status payload)",
    )
    p_activate.add_argument(
        "--publish",
        action="store_true",
        help="Publish a new version after activation (optional)",
    )
    p_activate.add_argument(
        "--publish-description",
        help=f"Publish description override (max {SURVEY_VERSION_DESCRIPTION_MAX_CHARS} chars).",
    )
    p_activate.set_defaults(func=handle_activate)

    # deactivate
    p_deactivate = survey_subs.add_parser(
        "deactivate",
        help="Deactivate a survey (set isActive=false)",
    )
    p_deactivate.add_argument(
        "--survey-id",
        action="append",
        dest="survey_id",
        help="Qualtrics survey ID to deactivate (repeatable)",
    )
    p_deactivate.add_argument(
        "--survey-ids-file",
        dest="survey_ids_file",
        help="Path to a newline- or CSV-delimited list of Survey IDs",
    )
    p_deactivate.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the intended change without calling the API",
    )
    p_deactivate.add_argument(
        "--force-live",
        action="store_true",
        help="Allow deactivation even if finished responses exist",
    )
    p_deactivate.add_argument(
        "--show-versions",
        action="store_true",
        help="Show recent version history in the preview",
    )
    p_deactivate.add_argument(
        "--versions-limit",
        type=int,
        default=5,
        help="Limit version history rows when --show-versions is set (default: 5)",
    )
    p_deactivate.add_argument(
        "--show-owner",
        action="store_true",
        help="Show owner info (if present in status payload)",
    )
    p_deactivate.add_argument(
        "--publish",
        action="store_true",
        help="Publish a new version after deactivation (optional)",
    )
    p_deactivate.add_argument(
        "--publish-description",
        help=f"Publish description override (max {SURVEY_VERSION_DESCRIPTION_MAX_CHARS} chars).",
    )
    p_deactivate.set_defaults(func=handle_deactivate)

    # versions
    p_versions = survey_subs.add_parser(
        "versions",
        help="List survey-definition versions for a survey",
    )
    p_versions.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Qualtrics survey ID (omit to select interactively)",
    )
    p_versions.add_argument(
        "--limit",
        type=int,
        help="Limit the number of versions shown (newest-first). Default: show all returned.",
    )
    p_versions.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON for automation",
    )
    p_versions.set_defaults(func=handle_versions)

    # version-fetch
    p_version_fetch = survey_subs.add_parser(
        "version-fetch",
        help="Fetch a specific survey-definition version (by VersionID)",
    )
    p_version_fetch.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Qualtrics survey ID (omit to select interactively)",
    )
    p_version_fetch.add_argument(
        "--version-id",
        required=True,
        dest="version_id",
        help="Qualtrics VersionID (from `qsync survey versions`)",
    )
    p_version_fetch.add_argument(
        "--format",
        choices=["json", "qsf"],
        default="json",
        help="Response format (qsf returns a QSF-like JSON payload).",
    )
    p_version_fetch.add_argument(
        "--output",
        help="Write the fetched payload to this file path (JSON).",
    )
    p_version_fetch.add_argument(
        "--json",
        action="store_true",
        help="Print the full fetched payload as JSON",
    )
    p_version_fetch.set_defaults(func=handle_version_fetch)

    # rollback
    p_rollback = survey_subs.add_parser(
        "rollback",
        help="Restore question(s) from a historical version and publish the restore",
    )
    p_rollback.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Survey ID (omit to select interactively)",
    )
    p_rollback.add_argument(
        "--version-id",
        required=True,
        dest="version_id",
        help="Qualtrics VersionID (from `qsync survey versions`)",
    )
    p_rollback.add_argument(
        "--question-id",
        required=True,
        dest="question_id",
        help="Comma-separated list of QIDs to restore (e.g., QID1,QID7).",
    )
    p_rollback.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and show the plan without writing to Qualtrics.",
    )
    p_rollback.add_argument(
        "--no-publish",
        action="store_true",
        help="Restore questions but do not publish the survey afterwards.",
    )
    p_rollback.add_argument(
        "--description",
        help=f"Publish description override (max {SURVEY_VERSION_DESCRIPTION_MAX_CHARS} chars).",
    )
    p_rollback.add_argument(
        "--force-live",
        action="store_true",
        help="Allow rollback even if finished responses exist.",
    )
    p_rollback.set_defaults(func=handle_rollback)

    # inspect-question
    p_inspect = survey_subs.add_parser(
        "inspect-question",
        help="Print a cached question payload from surveys/ (no API calls)",
    )
    p_inspect.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Qualtrics survey ID (omit to select interactively)",
    )
    p_inspect.add_argument(
        "--question-id",
        required=True,
        dest="question_id",
        help="Question ID to inspect (e.g., QID15)",
    )
    p_inspect.add_argument(
        "--survey-file",
        dest="survey_file",
        help="Path to survey JSON (default: auto-detect from surveys/)",
    )
    p_inspect.add_argument(
        "--field",
        help="Print only a single field from the question payload (e.g., QuestionJS, QuestionText)",
    )
    p_inspect.add_argument(
        "--raw",
        action="store_true",
        help="When used with --field and the field is a string, print without JSON quoting",
    )
    p_inspect.set_defaults(func=handle_inspect_question)

    # add-question
    p_add_question = survey_subs.add_parser(
        "add-question",
        help="Create one or more questions and place them into a block position",
    )
    p_add_question.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Target Qualtrics survey ID (omit to select interactively)",
    )
    p_add_question.add_argument(
        "--from-question-id",
        dest="from_question_id",
        help="Template question ID to clone (recommended)",
    )
    p_add_question.add_argument(
        "--source-account",
        dest="source_account",
        help=(
            "Optional source account for cross-account clone "
            "(defaults to target/current account; use 'default' for primary .env)."
        ),
    )
    p_add_question.add_argument(
        "--source-survey-id",
        dest="source_survey_id",
        help="Source survey ID for cross-account question cloning",
    )
    p_add_question.add_argument(
        "--source-question-id",
        action="append",
        dest="source_question_id",
        help="Source question ID(s) to clone (repeatable/comma-separated; preserves provided order)",
    )
    p_add_question.add_argument(
        "--question-json",
        dest="question_json",
        help=(
            "Path to a JSON file containing one question payload "
            "(alternative to --from-question-id)"
        ),
    )
    p_add_question.add_argument(
        "--question-text",
        action="append",
        dest="question_text",
        help=(
            "Question text to set on created question(s). Repeat for multiple questions. "
            "If omitted, one question is created with template text."
        ),
    )
    p_add_question.add_argument(
        "--question-text-file",
        dest="question_text_file",
        help="Path to a newline-delimited file of question texts (one question per line)",
    )
    p_add_question.add_argument(
        "--from-scratch-mcq",
        action="store_true",
        help="Create a new multiple-choice question scaffold from scratch.",
    )
    p_add_question.add_argument(
        "--from-scratch-type",
        choices=["mc", "te", "matrix", "db"],
        help=(
            "Create a new question scaffold from scratch. "
            "Types: mc (multiple choice), te (text entry), matrix (Likert), db (descriptive text). "
            "--from-scratch-mcq is kept as a legacy alias for --from-scratch-type mc."
        ),
    )
    p_add_question.add_argument(
        "--choice-text",
        action="append",
        dest="choice_text",
        help=(
            "Choice text for from-scratch MC and matrix answer options "
            "(repeatable/comma-separated via repeated args)."
        ),
    )
    p_add_question.add_argument(
        "--choice-text-file",
        dest="choice_text_file",
        help="Path to newline-delimited choice texts for from-scratch MC/matrix answers.",
    )
    p_add_question.add_argument(
        "--mc-multi-response",
        action="store_true",
        help="When using from-scratch MC, create a multi-select question (MAVR).",
    )
    p_add_question.add_argument(
        "--statement-text",
        action="append",
        dest="statement_text",
        help="Statement/row text for from-scratch matrix questions (repeatable/comma-separated).",
    )
    p_add_question.add_argument(
        "--statement-text-file",
        dest="statement_text_file",
        help="Path to newline-delimited statement/row texts for from-scratch matrix questions.",
    )
    p_add_question.add_argument(
        "--target-block-id",
        dest="target_block_id",
        help="Target Block ID (default: inferred from anchor/template/first eligible block)",
    )
    p_add_question.add_argument(
        "--after-qid",
        dest="after_qid",
        help="Insert after this QID",
    )
    p_add_question.add_argument(
        "--before-qid",
        dest="before_qid",
        help="Insert before this QID",
    )
    p_add_question.add_argument(
        "--position",
        choices=["append", "prepend"],
        default="append",
        help="Placement when no --after-qid/--before-qid is provided (default: append)",
    )
    p_add_question.add_argument(
        "--insert-index",
        type=int,
        help=(
            "0-based insertion boundary inside target block BlockElements; "
            "overrides anchor/position placement."
        ),
    )
    p_add_question.add_argument(
        "--page-break-mode",
        choices=["none", "before", "after", "between"],
        default="none",
        help=(
            "Insert page break(s) with newly created questions: "
            "none | before | after | between."
        ),
    )
    p_add_question.add_argument(
        "--data-export-tag",
        dest="data_export_tag",
        help="Base DataExportTag for created questions (auto-deduped by default)",
    )
    p_add_question.add_argument(
        "--allow-duplicate-tags",
        action="store_true",
        help="Allow duplicate DataExportTag values (dangerous; disabled by default)",
    )
    p_add_question.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the plan without calling create/update endpoints",
    )
    p_add_question.add_argument(
        "--force-live",
        action="store_true",
        help="Allow writes even if finished responses exist",
    )
    p_add_question.add_argument(
        "--no-publish",
        action="store_true",
        help="Skip publishing the survey after adding questions",
    )
    p_add_question.add_argument(
        "--publish-description",
        help=f"Publish description override (max {SURVEY_VERSION_DESCRIPTION_MAX_CHARS} chars).",
    )
    p_add_question.add_argument(
        "--account",
        help="Use credentials from `.env.<account>` under the workspace root.",
    )
    p_add_question.set_defaults(func=handle_add_question)

    # move-question
    p_move_question = survey_subs.add_parser(
        "move-question",
        help="Move one or more existing questions to a different block position",
    )
    p_move_question.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Target Qualtrics survey ID (omit to select interactively)",
    )
    p_move_question.add_argument(
        "--question-id",
        action="append",
        dest="question_id",
        help="Question ID(s) to move (repeatable, comma-separated)",
    )
    p_move_question.add_argument(
        "--target-block-id",
        dest="target_block_id",
        help="Target Block ID (default: inferred from anchor/current question block)",
    )
    p_move_question.add_argument(
        "--after-qid",
        dest="after_qid",
        help="Insert after this QID",
    )
    p_move_question.add_argument(
        "--before-qid",
        dest="before_qid",
        help="Insert before this QID",
    )
    p_move_question.add_argument(
        "--position",
        choices=["append", "prepend"],
        default="append",
        help="Placement when no --after-qid/--before-qid is provided (default: append)",
    )
    p_move_question.add_argument(
        "--insert-index",
        type=int,
        help=(
            "0-based insertion boundary inside target block BlockElements; "
            "overrides anchor/position placement."
        ),
    )
    p_move_question.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the plan without calling update endpoints",
    )
    p_move_question.add_argument(
        "--force-live",
        action="store_true",
        help="Allow writes even if finished responses exist",
    )
    p_move_question.add_argument(
        "--no-publish",
        action="store_true",
        help="Skip publishing the survey after moving questions",
    )
    p_move_question.add_argument(
        "--publish-description",
        help=f"Publish description override (max {SURVEY_VERSION_DESCRIPTION_MAX_CHARS} chars).",
    )
    p_move_question.add_argument(
        "--account",
        help="Use credentials from `.env.<account>` under the workspace root.",
    )
    p_move_question.set_defaults(func=handle_move_question)

    # remove-question
    p_remove_question = survey_subs.add_parser(
        "remove-question",
        help="Remove one or more questions from active blocks (moves them to Trash)",
    )
    p_remove_question.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Target Qualtrics survey ID (omit to select interactively)",
    )
    p_remove_question.add_argument(
        "--question-id",
        action="append",
        dest="question_id",
        help="Question ID(s) to remove (repeatable, comma-separated)",
    )
    p_remove_question.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the plan without calling delete/update endpoints",
    )
    p_remove_question.add_argument(
        "--force-live",
        action="store_true",
        help="Allow writes even if finished responses exist",
    )
    p_remove_question.add_argument(
        "--no-publish",
        action="store_true",
        help="Skip publishing the survey after deleting questions",
    )
    p_remove_question.add_argument(
        "--publish-description",
        help=f"Publish description override (max {SURVEY_VERSION_DESCRIPTION_MAX_CHARS} chars).",
    )
    p_remove_question.add_argument(
        "--account",
        help="Use credentials from `.env.<account>` under the workspace root.",
    )
    p_remove_question.set_defaults(func=handle_remove_question)

    # add-page-break
    p_add_page_break = survey_subs.add_parser(
        "add-page-break",
        help="Insert a page-break element into a target block position",
    )
    p_add_page_break.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Target Qualtrics survey ID (omit to select interactively)",
    )
    p_add_page_break.add_argument(
        "--target-block-id",
        dest="target_block_id",
        help="Target Block ID (default: inferred from anchor/first eligible block)",
    )
    p_add_page_break.add_argument(
        "--after-qid",
        dest="after_qid",
        help="Insert after this QID",
    )
    p_add_page_break.add_argument(
        "--before-qid",
        dest="before_qid",
        help="Insert before this QID",
    )
    p_add_page_break.add_argument(
        "--position",
        choices=["append", "prepend"],
        default="append",
        help="Placement when no --after-qid/--before-qid is provided (default: append)",
    )
    p_add_page_break.add_argument(
        "--insert-index",
        type=int,
        help=(
            "0-based insertion boundary inside target block BlockElements; "
            "overrides anchor/position placement."
        ),
    )
    p_add_page_break.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the plan without calling update endpoints",
    )
    p_add_page_break.add_argument(
        "--force-live",
        action="store_true",
        help="Allow writes even if finished responses exist",
    )
    p_add_page_break.add_argument(
        "--no-publish",
        action="store_true",
        help="Skip publishing the survey after adding page break(s)",
    )
    p_add_page_break.add_argument(
        "--publish-description",
        help=f"Publish description override (max {SURVEY_VERSION_DESCRIPTION_MAX_CHARS} chars).",
    )
    p_add_page_break.add_argument(
        "--account",
        help="Use credentials from `.env.<account>` under the workspace root.",
    )
    p_add_page_break.set_defaults(func=handle_add_page_break)

    # remove-page-break
    p_remove_page_break = survey_subs.add_parser(
        "remove-page-break",
        help="Remove one or more page-break elements from a target block",
    )
    p_remove_page_break.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Target Qualtrics survey ID (omit to select interactively)",
    )
    p_remove_page_break.add_argument(
        "--target-block-id",
        required=True,
        dest="target_block_id",
        help="Target Block ID containing page-break elements to remove",
    )
    p_remove_page_break.add_argument(
        "--element-index",
        action="append",
        required=True,
        dest="element_index",
        help=(
            "0-based block element index of page break(s) to remove "
            "(repeatable/comma-separated)."
        ),
    )
    p_remove_page_break.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the plan without calling update endpoints",
    )
    p_remove_page_break.add_argument(
        "--force-live",
        action="store_true",
        help="Allow writes even if finished responses exist",
    )
    p_remove_page_break.add_argument(
        "--no-publish",
        action="store_true",
        help="Skip publishing the survey after removing page break(s)",
    )
    p_remove_page_break.add_argument(
        "--publish-description",
        help=f"Publish description override (max {SURVEY_VERSION_DESCRIPTION_MAX_CHARS} chars).",
    )
    p_remove_page_break.add_argument(
        "--account",
        help="Use credentials from `.env.<account>` under the workspace root.",
    )
    p_remove_page_break.set_defaults(func=handle_remove_page_break)

    # push-question
    p_push_q = survey_subs.add_parser(
        "push-question",
        help="Push a single question from cached survey JSON to Qualtrics",
    )
    p_push_q.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Target Qualtrics survey ID (omit to select interactively)",
    )
    p_push_q.add_argument(
        "--question-id",
        required=True,
        dest="question_id",
        help="Question ID to push (e.g., QID15)",
    )
    p_push_q.add_argument(
        "--survey-file",
        help="Path to survey JSON (default: auto-detect from surveys/)",
    )
    p_push_q.add_argument(
        "--dry-run",
        action="store_true",
        help="Show diff without pushing",
    )
    p_push_q.add_argument(
        "--force-live",
        action="store_true",
        help="Allow push even if survey has live responses",
    )
    p_push_q.add_argument(
        "--show-diff",
        action="store_true",
        help="Always show diff (even with --yes)",
    )
    p_push_q.add_argument(
        "--no-publish",
        action="store_true",
        help="Skip publishing the survey after pushing the question definition",
    )
    p_push_q.set_defaults(func=handle_push_question)

    # replace-question
    p_replace_q = survey_subs.add_parser(
        "replace-question",
        help="Replace one target question with a source question payload",
    )
    p_replace_q.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Target Qualtrics survey ID (omit to select interactively)",
    )
    p_replace_q.add_argument(
        "--question-id",
        dest="question_id",
        help="Target question ID to replace (e.g., QID15)",
    )
    p_replace_q.add_argument(
        "--source-account",
        dest="source_account",
        help=(
            "Optional source account for source question lookup "
            "(defaults to target/current account; use 'default' for primary .env)."
        ),
    )
    p_replace_q.add_argument(
        "--source-survey-id",
        dest="source_survey_id",
        help="Source survey ID that contains the replacement question",
    )
    p_replace_q.add_argument(
        "--source-question-id",
        dest="source_question_id",
        help="Source question ID to copy payload from (e.g., QID28)",
    )
    p_replace_q.add_argument(
        "--dry-run",
        action="store_true",
        help="Show diff and plan without pushing",
    )
    p_replace_q.add_argument(
        "--force-live",
        action="store_true",
        help="Allow replace even if survey has live responses",
    )
    p_replace_q.add_argument(
        "--show-diff",
        action="store_true",
        help="Always show diff (even with --yes)",
    )
    p_replace_q.add_argument(
        "--replace-data-export-tag",
        action="store_true",
        help="Also replace target DataExportTag with source tag (default preserves target tag)",
    )
    p_replace_q.add_argument(
        "--no-publish",
        action="store_true",
        help="Skip publishing after replacing question definition",
    )
    p_replace_q.add_argument(
        "--publish-description",
        help=f"Publish description override (max {SURVEY_VERSION_DESCRIPTION_MAX_CHARS} chars).",
    )
    p_replace_q.add_argument(
        "--account",
        help="Use credentials from `.env.<account>` under the workspace root.",
    )
    p_replace_q.set_defaults(func=handle_replace_question)

    # export-responses
    p_export = survey_subs.add_parser(
        "export-responses",
        help="Export survey responses",
    )
    p_export.add_argument(
        "--survey-id",
        action="append",
        dest="survey_id",
        help="Qualtrics survey ID(s) to export responses from (repeatable/comma-separated; omit to select interactively)",
    )
    p_export.add_argument(
        "--format",
        dest="export_format",
        choices=SUPPORTED_RESPONSE_EXPORT_FORMATS,
        default=DEFAULT_RESPONSE_EXPORT_FORMAT,
        help=(
            "Response export format "
            f"(default: {DEFAULT_RESPONSE_EXPORT_FORMAT})"
        ),
    )
    p_export.add_argument(
        "--output",
        help="Output directory (default: responses/)",
    )
    p_export.add_argument(
        "--account",
        help=(
            "Use credentials from `.env.<account>` under the workspace root "
            "(default output: responses/.<account>/ unless --output is set)."
        ),
    )
    p_export.set_defaults(func=handle_export_responses)

    # export-translation
    p_export_translation = survey_subs.add_parser(
        "export-translation",
        help="Export survey content to a Word document for translation validation",
    )
    _add_export_translation_args(p_export_translation)
    p_export_translation.set_defaults(func=handle_export_translation)

    # export-side-by-side
    p_export_side = survey_subs.add_parser(
        "export-side-by-side",
        help="Export two surveys side-by-side into a single DOCX",
    )
    p_export_side.add_argument(
        "--source-id", required=True, dest="source_id", help="Survey ID A"
    )
    p_export_side.add_argument(
        "--target-id", required=True, dest="target_id", help="Survey ID B"
    )
    p_export_side.add_argument(
        "--output",
        type=Path,
        help="Output path for the DOCX (file or directory)",
    )
    p_export_side.add_argument("--label-a", help="Left column label")
    p_export_side.add_argument("--label-b", help="Right column label")
    p_export_side.add_argument(
        "--skip-parity",
        action="store_true",
        help="Skip parity check (not recommended)",
    )
    p_export_side.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh cached survey definitions before exporting",
    )
    p_export_side.add_argument(
        "--smart-name",
        action="store_true",
        help="Append a timestamp to the output filename",
    )
    p_export_side.add_argument(
        "--no-html",
        action="store_true",
        help="Do not include HTML source blocks",
    )
    p_export_side.add_argument(
        "--layout-heuristics",
        action="store_true",
        help="Apply reviewer-friendly layout heuristics",
    )
    p_export_side.add_argument(
        "--skip-js-strings",
        action="store_true",
        help="Skip JS user-visible string extraction",
    )
    p_export_side.add_argument(
        "--open",
        action="store_true",
        help="Open the exported document after generation",
    )
    p_export_side.set_defaults(func=handle_export_side_by_side)

    # master
    p_master = survey_subs.add_parser(
        "master",
        help="Manage survey master (group; focal-only bulk editing)",
    )
    master_subs = p_master.add_subparsers(
        dest="master_command",
        required=True,
        metavar="COMMAND",
    )

    # master columns
    p_master_columns = master_subs.add_parser(
        "columns",
        help="Configure Survey Master columns (order + visibility) via TUI/YAML",
    )
    p_master_columns.set_defaults(func=handle_master_columns)

    # master pull
    p_master_pull = master_subs.add_parser(
        "pull",
        help="Pull focal survey snapshots and generate master CSV",
    )
    p_master_pull.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-survey status lines (disables progress bar)",
    )
    p_master_pull.add_argument(
        "--mapping-csv",
        type=Path,
        dest="mapping_csv",
        help="Path to a Qualtrics API field mapping CSV for Survey Master (overrides packaged defaults)",
    )
    p_master_pull.add_argument(
        "--survey-id",
        action="append",
        dest="survey_ids",
        help="Limit to specific survey ID(s) (repeatable); default: all focal surveys",
    )
    p_master_pull.add_argument(
        "--force-overwrite",
        action="store_true",
        help="Discard existing CSV and generate fresh (default: merge to preserve user edits)",
    )
    p_master_pull.set_defaults(func=handle_master_pull)

    # master preview
    p_master_preview = master_subs.add_parser(
        "preview",
        help="Preview changes that would be staged and pushed",
    )
    p_master_preview.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-survey status lines (disables progress bar)",
    )
    p_master_preview.add_argument(
        "--mapping-csv",
        type=Path,
        dest="mapping_csv",
        help="Path to a Qualtrics API field mapping CSV for Survey Master (overrides packaged defaults)",
    )
    p_master_preview.add_argument(
        "--detail",
        action="store_true",
        help="Show detailed per-field changes",
    )
    p_master_preview.add_argument(
        "--survey-id",
        help="Preview only this specific survey (by SurveyID)",
    )
    p_master_preview.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    p_master_preview.add_argument(
        "--tag",
        action="append",
        dest="tags",
        help="Filter surveys by tag (e.g., --tag component=pre --tag stage=prod)",
    )
    p_master_preview.add_argument(
        "--all-surveys",
        dest="all_surveys",
        action="store_true",
        help="Include non-focal surveys from qualtrics_master.csv (default: focal-only)",
    )
    p_master_preview.set_defaults(func=handle_master_preview)

    # master stage
    p_master_stage = master_subs.add_parser(
        "stage",
        help="Stage changes from master CSV to pending (no API writes)",
    )
    p_master_stage.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-survey status lines",
    )
    p_master_stage.add_argument(
        "--survey-id",
        help="Stage only this specific survey (by SurveyID)",
    )
    p_master_stage.add_argument(
        "--tag",
        action="append",
        dest="tags",
        help="Filter surveys by tag (e.g., --tag component=pre --tag stage=prod)",
    )
    p_master_stage.add_argument(
        "--all-surveys",
        dest="all_surveys",
        action="store_true",
        help="Include non-focal surveys from qualtrics_master.csv (default: focal-only)",
    )
    p_master_stage.set_defaults(func=handle_master_stage)

    # master push
    p_master_push = master_subs.add_parser(
        "push",
        help="Push staged changes to Qualtrics (publishes by default)",
    )
    p_master_push.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-survey status lines (disables progress bar)",
    )
    p_master_push.add_argument(
        "--mapping-csv",
        type=Path,
        dest="mapping_csv",
        help="Path to a Qualtrics API field mapping CSV for Survey Master (overrides packaged defaults)",
    )
    p_master_push.add_argument(
        "--description",
        help="Description for the published version (default: 'qsync master push')",
    )
    p_master_push.add_argument(
        "--survey-id",
        help="Push only this specific survey (by SurveyID)",
    )
    p_master_push.add_argument(
        "--all-surveys",
        dest="all_surveys",
        action="store_true",
        help="Push staged changes for all surveys with pending records (including non-focal)",
    )
    p_master_push.add_argument(
        "--no-publish",
        action="store_true",
        help="Skip publish step (API write only)",
    )
    p_master_push.add_argument(
        "--force-live",
        action="store_true",
        help="Allow push even with live responses (requires confirmation)",
    )
    p_master_push.add_argument(
        "--force-preview",
        action="store_true",
        help="Skip preview response warnings",
    )
    p_master_push.add_argument(
        "--allow-dangerous",
        action="store_true",
        help="Allow changes to dangerous fields (isActive, EOSRedirectURL, etc.)",
    )
    p_master_push.add_argument(
        "--allow-locked",
        action="store_true",
        help="Allow push to locked surveys",
    )
    p_master_push.set_defaults(func=handle_master_push)

    # master rollback
    p_master_rollback = master_subs.add_parser(
        "rollback",
        help="List or rollback pre-apply survey master snapshots",
    )
    p_master_rollback.add_argument(
        "--survey-id",
        help="Survey ID to rollback (required unless --list)",
    )
    p_master_rollback.add_argument(
        "--list",
        action="store_true",
        help="List available rollback versions (optionally filtered by --survey-id)",
    )
    p_master_rollback.add_argument(
        "--version",
        type=int,
        default=1,
        help="Rollback snapshot version to use (1 = most recent, default: 1)",
    )
    p_master_rollback.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview rollback changes without applying writes",
    )
    p_master_rollback.add_argument(
        "--force",
        action="store_true",
        help="Allow rollback even if drift is detected from the expected post-apply state",
    )
    p_master_rollback.add_argument(
        "--allow-dangerous",
        action="store_true",
        help="Allow rollback of dangerous fields (isActive, redirect URLs, etc.)",
    )
    p_master_rollback.add_argument(
        "--no-publish",
        action="store_true",
        help="Skip publishing after rollback writes",
    )
    p_master_rollback.add_argument(
        "--description",
        help="Description to use when publishing rollback changes",
    )
    p_master_rollback.set_defaults(func=handle_master_rollback)

    # Help output ordering: keep related commands together.
    from .argparse_support import reorder_subparser_choices

    reorder_subparser_choices(
        survey_subs,
        [
            # Inventory/cache
            "list",
            "label",
            "focal",
            "inventory",
            "pull",
            "prepare",
            # Copy/derive
            "copy",
            "slice-language",
            "copy-cross-account",
            "slice-registry",
            "parity-check",
            # Survey definition edits / options
            "add-embedded-field",
            "remove-embedded-field",
            "rename-embedded-field",
            "cleanup-embedded-data",
            "prolific-auth",
            "prolific-wiring",
            # Lifecycle / versions
            "publish",
            "activate",
            "deactivate",
            "versions",
            "version-fetch",
            "rollback",
            # Utilities
            "inspect-question",
            "add-question",
            "move-question",
            "add-page-break",
            "remove-page-break",
            "push-question",
            "replace-question",
            # Exports
            "export-responses",
            "export-translation",
            "export-side-by-side",
            # Bulk
            "master",
            # Admin
            "rename",
            "delete",
        ],
    )

    reorder_subparser_choices(
        master_subs,
        [
            "columns",
            "pull",
            "preview",
            "stage",
            "push",
            "rollback",
        ],
    )
