"""
Survey management CLI commands for qsync.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import zipfile
from difflib import unified_diff
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .config import get_client_config, load_account_env, resolve_root
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
from .publish_description import make_publish_description
from .push_policy import load_push_context
from .survey_lock import SurveyLockedError, ensure_unlocked
from .scope_filter import ScopeFilter


def _workspace_root() -> Path:
    return resolve_root(required=False) or Path.cwd()


def _inventory_csv_path(root: Path) -> Path:
    path = (root / "surveys" / "inventory.csv").resolve()
    if path.exists():
        return path
    legacy = (root / "surveys" / "qualtrics_surveys.csv").resolve()
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
        return Path(explicit_dest)
    if account:
        return (root / "surveys" / f".{account}").resolve()
    return (root / "surveys").resolve()


def _slugify(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(value))


def _default_xlsx_path_for_survey(survey_id: str) -> Path:
    root = _workspace_root()
    csv_path = _inventory_csv_path(root)
    suffix_slug = None
    if csv_path.exists():
        for row in _iter_inventory_rows(csv_path):
            if (row.get("id") or "").strip() == survey_id:
                name = (row.get("name") or "").strip()
                if name:
                    suffix_slug = _slugify(name)
                break

    if not suffix_slug:
        try:
            from .qualtrics_client import load_cached_survey

            survey = load_cached_survey(survey_id)
            title = (
                survey.payload.get("result", {})
                .get("SurveyOptions", {})
                .get("SurveyTitle")
                or survey_id
            )
            suffix_slug = _slugify(title)
        except Exception:
            suffix_slug = _slugify(survey_id)

    return root / "excel" / f"{survey_id}-{suffix_slug}.xlsx"


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
    from .cli import _prompt_for_survey_id_if_needed

    survey_id = _prompt_for_survey_id_if_needed(args.survey_id, allow_all_surveys=False)
    placeholder_only = not bool(args.all_duplicates)
    apply_changes = bool(args.apply)
    dry_run = bool(args.dry_run) or not apply_changes

    flow = _fetch_survey_flow(survey_id)
    removed, details = _dedupe_embedded_data(flow, placeholder_only=placeholder_only)

    if removed == 0:
        print("[cleanup-embedded-data] No duplicate embedded data rows found.")
        return

    scope = "placeholder duplicates only" if placeholder_only else "all duplicates"
    print(
        f"[cleanup-embedded-data] Found {removed} duplicate embedded data row(s) "
        f"({scope})."
    )
    for item in details[:10]:
        flow_id = item.get("flow_id") or "?"
        field = item.get("field") or "?"
        print(f"  - FlowID {flow_id}: {field}")
    if len(details) > 10:
        print(f"  - ... {len(details) - 10} more")

    if dry_run:
        print("[cleanup-embedded-data] Dry run only; no changes applied.")
        return

    if not args.yes:
        try:
            from qsync.interactive_menu import confirm

            if not confirm(
                f"Apply embedded data cleanup to {survey_id}?", default=True
            ):
                print("[cleanup-embedded-data] Aborted.")
                return
        except Exception:
            resp = (
                input(f"Apply embedded data cleanup to {survey_id}? [Y/n] ")
                .strip()
                .lower()
            )
            if resp and resp not in {"y", "yes"}:
                print("[cleanup-embedded-data] Aborted.")
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
        print("[cleanup-embedded-data] Applied and published cleanup.")
    else:
        print("[cleanup-embedded-data] Applied cleanup (not published).")


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
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    return name or None


def _get_client_config_for_args(args: argparse.Namespace) -> tuple[str, dict]:
    account = _resolve_account_from_args(args)
    if account:
        env = load_account_env(account, root=_workspace_root())
        return get_client_config(env)
    return get_client_config()


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


def handle_menu(_args: argparse.Namespace) -> None:
    """Interactive wizard for common `qsync survey ...` operations."""

    from .interactive_menu import is_interactive, select_from_list, confirm

    if not is_interactive():
        raise SystemExit("[survey-menu] ERROR: Interactive TTY required.")

    root = _workspace_root()
    selected_account: str | None = None  # None = default

    # Cache survey lists per base_url for responsiveness within a menu session.
    survey_cache: dict[str, list[dict[str, Any]]] = {}

    def _account_label() -> str:
        return selected_account or "default"

    def _resolve_base_url_for_display() -> str | None:
        if selected_account:
            try:
                env = load_account_env(selected_account, root=root)
                base = (env.get("QUALTRICS_BASE_URL") or "").strip()
                return base or None
            except Exception:
                return None
        # Default account: best-effort (avoid requiring token just to show base url).
        try:
            from .config import load_env, resolve_env_path

            env_path = resolve_env_path(root=root)
            env = load_env(env_path)
            base = (env.get("QUALTRICS_BASE_URL") or "").strip()
            return base or None
        except Exception:
            return None

    def _get_client() -> tuple[str, dict]:
        if selected_account:
            env = load_account_env(selected_account, root=root)
            return get_client_config(env)
        return get_client_config()

    def _get_surveys() -> list[dict[str, Any]]:
        base, headers = _get_client()
        cached = survey_cache.get(base)
        if cached is not None:
            return cached
        surveys = list_surveys(base, headers)
        surveys.sort(key=lambda x: x.get("creationDate", ""), reverse=True)
        survey_cache[base] = surveys
        return surveys

    def _pick_survey_id(*, message: str) -> str | None:
        try:
            surveys = _get_surveys()
        except Exception as exc:
            print(f"[survey-menu] ERROR: unable to list surveys: {exc}")
            return None

        # Keep arrow menus usable: require a filter when the list is large.
        filtered = surveys
        if len(filtered) > 60:
            raw = input(
                "Filter surveys by name/ID substring (blank to show all): "
            ).strip()
            if raw:
                needle = raw.lower()
                filtered = [
                    s
                    for s in surveys
                    if needle in str(s.get("id") or "").lower()
                    or needle in str(s.get("name") or "").lower()
                ]
                if not filtered:
                    print("[survey-menu] No surveys matched that filter.")
                    return None
            else:
                if not confirm(
                    f"List all {len(filtered)} surveys in an interactive menu? (may be slow)",
                    default=False,
                ):
                    return None

        choices = [
            f"{s.get('id')} - {s.get('name', 'Untitled')}"
            for s in filtered
            if s.get("id")
        ]
        choices.append("─" * 60)
        choices.append("✎ Enter SurveyID manually")
        choices.append("↩ Back")
        selection = select_from_list(message=message, choices=choices)
        if not selection or selection.endswith("Back"):
            return None
        if selection.startswith("✎"):
            manual = input("Enter Qualtrics SurveyID (e.g. SV_...): ").strip()
            return manual or None
        return selection.split(" - ", 1)[0].strip()

    def _run_action(func, ns: argparse.Namespace) -> None:
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
        if selected_account is None:
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
            selected_account = None
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
            f"[survey-menu] whoami datacenter={datacenter or '(unknown)'} userId={user_id or '(unknown)'}"
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

        base = _resolve_base_url_for_display() or "(unknown)"
        print()
        print("[survey-menu] WARNING: This will permanently delete surveys in Qualtrics.")
        print("[survey-menu] Account:", _account_label())
        print("[survey-menu] Base URL:", base)
        print("[survey-menu] Surveys:", ", ".join(survey_ids))
        print()

        if not _typed_confirmation(
            prompt="Type 'delete' to confirm: ",
            expected="delete",
        ):
            print("[survey-menu] Aborted.")
            return

        _run_action(
            handle_delete,
            argparse.Namespace(
                survey_ids=survey_ids,
                account=selected_account,
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
        survey_id = _pick_survey_id(
            message="Pick a survey to activate:"
            if active
            else "Pick a survey to deactivate:"
        )
        if not survey_id:
            return
        handler = handle_activate if active else handle_deactivate
        _run_action(
            handler,
            argparse.Namespace(
                survey_id=survey_id,
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
        survey_id = _pick_survey_id(message="Pick a survey to publish:")
        if not survey_id:
            return
        desc = input("Version description (max 140 chars): ").strip()
        if not desc:
            return
        _run_action(
            handle_publish,
            argparse.Namespace(
                survey_id=survey_id,
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
            selected_account = None if source_acct == "default" else source_acct
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
                target_account=None if target_acct == "default" else target_acct,
                source_api_key="",
                source_base_url="",
                source_account=None if source_acct == "default" else source_acct,
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
        out = input("Output directory (optional; default: responses/): ").strip() or None
        _run_action(
            handle_export_responses,
            argparse.Namespace(survey_id=survey_id, output=out, account=selected_account),
        )

    def _menu_export_translation() -> None:
        if not _require_default_account(action="export-translation"):
            return
        _run_action(handle_export_translation, argparse.Namespace())

    def _menu_export_side_by_side() -> None:
        if not _require_default_account(action="export-side-by-side"):
            return
        _run_action(handle_export_side_by_side, argparse.Namespace())

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

    def _menu_pull() -> None:
        if not _require_default_account(action="survey pull"):
            return
        survey_id = _pick_survey_id(message="Pick a survey to pull (cache JSON):")
        if not survey_id:
            return
        dest = input("Destination directory (optional; default: surveys/): ").strip() or None
        _run_action(handle_pull, argparse.Namespace(survey_id=survey_id, dest=dest))

    def _menu_prepare() -> None:
        if not _require_default_account(action="survey prepare"):
            return
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
            ),
        )

    def _menu_embedded_field(action: str) -> None:
        if not _require_default_account(action=action):
            return
        if action == "add-embedded-field":
            _run_action(
                handle_add_embedded_field,
                argparse.Namespace(
                    survey_id=None,
                    field=None,
                    value=None,
                    flow_id=None,
                    dry_run=False,
                ),
            )
        elif action == "remove-embedded-field":
            _run_action(
                handle_remove_embedded_field,
                argparse.Namespace(
                    survey_id=None,
                    field=None,
                    flow_id=None,
                    dry_run=False,
                ),
            )
        elif action == "rename-embedded-field":
            _run_action(
                handle_rename_embedded_field,
                argparse.Namespace(
                    survey_id=None,
                    from_field=None,
                    to_field=None,
                    flow_id=None,
                    all_occurrences=False,
                    dry_run=False,
                ),
            )

    def _menu_cleanup_embedded_data() -> None:
        if not _require_default_account(action="cleanup-embedded-data"):
            return
        _run_action(
            handle_cleanup_embedded_data,
            argparse.Namespace(
                survey_id=None,
                all_duplicates=False,
                apply=False,
                dry_run=False,
                yes=False,
                publish=False,
                description="",
            ),
        )

    def _menu_prolific_auth() -> None:
        if not _require_default_account(action="prolific-auth"):
            return
        _run_action(
            handle_prolific_auth,
            argparse.Namespace(
                survey_id=None,
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

    while True:
        base = _resolve_base_url_for_display() or "(base URL unknown)"
        top = select_from_list(
            message=f"qsync survey menu  (account: {_account_label()}  base: {base})",
            choices=[
                "Account & Diagnostics",
                "Browse",
                "Lifecycle & Versions",
                "Admin",
                "Copy & Derive",
                "Embedded & Options",
                "Exports",
                "Workspace",
                "Exit",
            ],
        )
        if not top or top == "Exit":
            return

        if top == "Account & Diagnostics":
            choice = select_from_list(
                "Account & Diagnostics",
                [
                    "Switch account",
                    "Show account info",
                    "Check API (/whoami)",
                    "↩ Back",
                ],
            )
            if not choice or choice.endswith("Back"):
                continue
            if choice.startswith("Switch"):
                _menu_switch_account()
            elif choice.startswith("Show"):
                _menu_show_account_info()
            else:
                _menu_check_api()
            continue

        if top == "Browse":
            choice = select_from_list(
                "Browse",
                [
                    "List surveys",
                    "Label survey ID (inventory)",
                    "List focal survey IDs (inventory)",
                    "↩ Back",
                ],
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
            else:
                newline = select_from_list("One ID per line?", ["No", "Yes"]) == "Yes"
                _run_action(handle_focal, argparse.Namespace(newline=bool(newline)))
            continue

        if top == "Lifecycle & Versions":
            choice = select_from_list(
                "Lifecycle & Versions",
                [
                    "Activate survey",
                    "Deactivate survey",
                    "Publish survey-definition (new version)",
                    "List versions",
                    "Fetch a version",
                    "Rollback questions to a version",
                    "↩ Back",
                ],
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

        if top == "Admin":
            choice = select_from_list(
                "Admin",
                [
                    "Rename survey",
                    "Delete survey(s) (type 'delete' to confirm)",
                    "↩ Back",
                ],
            )
            if not choice or choice.endswith("Back"):
                continue
            if choice.startswith("Rename"):
                _menu_rename()
            else:
                _menu_delete()
            continue

        if top == "Copy & Derive":
            choice = select_from_list(
                "Copy & Derive",
                [
                    "Copy survey",
                    "Slice language(s)",
                    "Slice registry (local)",
                    "Parity check",
                    "Copy cross-account",
                    "↩ Back",
                ],
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

        if top == "Embedded & Options":
            choice = select_from_list(
                "Embedded & Options",
                [
                    "Add embedded field (stage)",
                    "Remove embedded field (stage)",
                    "Rename embedded field (stage)",
                    "Cleanup embedded data (apply)",
                    "Prolific authenticity snippet",
                    "↩ Back",
                ],
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
            else:
                _menu_prolific_auth()
            continue

        if top == "Exports":
            choice = select_from_list(
                "Exports",
                [
                    "Export responses",
                    "Export translation document",
                    "Export side-by-side document",
                    "↩ Back",
                ],
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

        if top == "Workspace":
            choice = select_from_list(
                "Workspace",
                [
                    "Refresh inventory",
                    "Pull survey definition (cache)",
                    "Prepare surfaces",
                    "↩ Back",
                ],
            )
            if not choice or choice.endswith("Back"):
                continue
            if choice.startswith("Refresh"):
                _menu_inventory()
            elif choice.startswith("Pull"):
                _menu_pull()
            else:
                _menu_prepare()


def handle_prepare(args: argparse.Namespace) -> None:
    """Hydrate all local editing surfaces for one or more surveys (pull-only)."""

    from .survey_prepare import prepare_workspace, resolve_target_surveys
    from .terminal_output import header, info, success, warn, error

    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    surfaces_raw = (getattr(args, "surfaces", None) or "").strip()
    surfaces = None
    if surfaces_raw:
        parts = [p.strip().lower() for p in surfaces_raw.split(",") if p.strip()]
        surfaces = set(parts)

    languages = _collect_languages_from_args(args)
    try:
        survey_ids = resolve_target_surveys(
            survey_id=getattr(args, "survey_id", None),
            focal=bool(getattr(args, "focal", False)),
            all_surveys=bool(getattr(args, "all_surveys", False)),
            interactive=interactive,
            yes=bool(getattr(args, "yes", False)),
        )
    except Exception as exc:
        raise SystemExit(f"[qsync:survey-prepare] ERROR: {exc}") from exc

    header("[qsync:survey-prepare]", f"Preparing {len(survey_ids)} survey(s)...")
    results = prepare_workspace(
        survey_ids=survey_ids,
        yes=bool(getattr(args, "yes", False)),
        interactive=interactive and not bool(getattr(args, "yes", False)),
        overwrite_js=bool(getattr(args, "overwrite_js", False)),
        shared_js=bool(getattr(args, "shared_js", False)),
        surfaces=surfaces,
        languages=languages,
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
        raise RuntimeError(
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

    account = (getattr(args, "account", None) or "").strip() or None
    if account:
        env = load_account_env(account, root=_workspace_root())
        base, headers = get_client_config(env)
    else:
        base, headers = get_client_config()

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
        from .cli import _prompt_for_survey_id_if_needed

        source_id = _prompt_for_survey_id_if_needed(
            getattr(args, "source_survey_id", None),
            allow_all_surveys=True,
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
        warn_if_flow_text_present,
        write_coverage_report,
        write_dry_run_qsf,
        write_slice_manifest,
        write_batch_manifest,
        sha256_of_qsf_upload_bytes,
    )

    import qsync
    import copy

    base, headers = _get_client_config_for_args(args)

    from .cli import _prompt_for_survey_id_if_needed
    from .dimensions.translations_core import (
        list_enabled_languages as api_list_enabled_languages,
    )

    source_id = _prompt_for_survey_id_if_needed(
        getattr(args, "source_survey_id", None),
        allow_all_surveys=True,
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

        # Check for duplicate names unless forced.
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
            from .survey_parity import compare_qsf_parity

            info("[qsync:slice-language]", "Running parity check (best-effort)...")
            try:
                source_qsf = fetch_survey_definition(
                    base, headers, source_id, fmt="qsf"
                )
                target_qsf = fetch_survey_definition(base, headers, new_id, fmt="qsf")
                parity = compare_qsf_parity(source_qsf, target_qsf)
                ok = _emit_parity_report(
                    result=parity,
                    survey_a=source_id,
                    survey_b=new_id,
                    prefix="[qsync:slice-language:parity]",
                )
                if not ok:
                    raise SystemExit(
                        "[qsync:slice-language] Parity check failed; see details above."
                    )
            except Exception as exc:
                warn(
                    "[qsync:slice-language]",
                    f"Parity check failed to run: {exc}",
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
    slices_dir = root / "surveys" / "slices"
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

    survey_a = str(getattr(args, "a", "") or "").strip()
    survey_b = str(getattr(args, "b", "") or "").strip()
    if not survey_a or not survey_b:
        error("[qsync:export-side-by-side]", "ERROR: --a and --b are required.")
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


def handle_parity_check(args: argparse.Namespace) -> None:
    """Compare two surveys for parity (QSF-lite by default; deep via --deep)."""

    from .terminal_output import header, info, error, success, warn, dim

    base, headers = _get_client_config_for_args(args)

    survey_a = getattr(args, "a", None) or ""
    survey_b = getattr(args, "b", None) or ""
    deep = bool(getattr(args, "deep", False))
    if not survey_a or not survey_b:
        error("[qsync:parity-check]", "ERROR: --a and --b are required.")
        sys.exit(1)

    if deep:
        from .survey_deep_parity import compare_survey_definition_deep_parity
        from .dimensions.flow_diff import format_diff_for_display, format_diff_summary

        header("[qsync:parity-check]", "Fetching survey definitions (JSON)...")
        try:
            def_a = fetch_survey_definition(base, headers, survey_a, fmt="json")
            def_b = fetch_survey_definition(base, headers, survey_b, fmt="json")
        except Exception as exc:
            error("[qsync:parity-check]", f"ERROR: Failed to fetch definitions: {exc}")
            sys.exit(1)

        info("[qsync:parity-check]", f"Deep comparing {survey_a} vs {survey_b}...")
        report = compare_survey_definition_deep_parity(
            def_a,
            def_b,
            survey_a=survey_a,
            survey_b=survey_b,
            write_artifacts_on_mismatch=True,
        )
        if report.ok:
            success(
                "[qsync:parity-check]",
                f"Deep parity OK (hash={report.hash_a[:12]}).",
            )
            return

        warn(
            "[qsync:parity-check]",
            "Deep parity FAILED (normalized hashes differ).",
        )
        if report.section_counts:
            parts = [
                f"{k}={v}"
                for k, v in sorted(report.section_counts.items(), key=lambda kv: (-kv[1], kv[0]))
                if v
            ]
            if parts:
                warn("[qsync:parity-check]", f"Diff sections: {', '.join(parts)}")
        warn("[qsync:parity-check]", f"Diff count: {report.diff_count}")
        if report.diff_paths:
            warn("[qsync:parity-check]", "Diff paths (sample):")
            for p in report.diff_paths[:50]:
                warn("[qsync:parity-check]", f"  - {p}")

        if report.flow_changes:
            warn(
                "[qsync:parity-check]",
                f"SurveyFlow: {format_diff_summary(report.flow_changes)}",
            )
            for line in format_diff_for_display(report.flow_changes, verbose=False)[:25]:
                dim("[qsync:parity-check]", line)
            if len(report.flow_changes) > 25:
                dim(
                    "[qsync:parity-check]",
                    f"(flow diffs truncated; showing 25 of {len(report.flow_changes)})",
                )

        if report.artifacts:
            dim(
                "[qsync:parity-check]",
                f"Artifacts: a={report.artifacts.get('a')} b={report.artifacts.get('b')}",
            )
            if report.artifacts.get("diff"):
                dim("[qsync:parity-check]", f"Unified diff: {report.artifacts.get('diff')}")

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

    base, headers = get_client_config()

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

    raise RuntimeError(
        f"Unable to generate unique name after 100 attempts for '{requested_name}'"
    )


def handle_copy_cross_account(args: argparse.Namespace) -> None:
    """Copy a survey from one Qualtrics account to another."""
    from .terminal_output import header, info, success, warn, dim
    from .config import load_env, load_env_file, resolve_env_path
    from .translations import (
        _check_html_hazards,
        _check_placeholders,
        _check_value_length_limit,
    )
    from .translation_export import build_translation_map_from_cache
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

    # Read `.env` (if present) so this command can support TARGET_* defaults.
    root = resolve_root(required=False) or Path.cwd()
    env_path = resolve_env_path(root=root)
    file_env = load_env_file(env_path) if env_path else {}

    def _env_or_dotenv(key: str) -> str:
        raw = (os.environ.get(key) or "").strip()
        if raw:
            return raw
        return str(file_env.get(key) or "").strip()

    # Resolve target credentials (do not silently fall back to the primary account).
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
            "or use --target-account <name> (.env.<name>), "
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

        base_map = build_translation_map_from_cache(
            source_payload,
            language=source_base_lang,
            base_language=source_base_lang,
        )
        allowed_empty_keys = {
            str(k)
            for k, v in base_map.items()
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
            normalized = build_translation_map_from_cache(
                source_payload,
                language=lang,
                base_language=source_base_lang,
            )

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

            errors.extend(_check_html_hazards(normalized, lang))
            errors.extend(_check_value_length_limit(normalized, lang))
            ph_errors, ph_warnings = _check_placeholders(base_map, normalized, lang)
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

    # Get source credentials (default account, unless overridden)
    if source_account and (not source_base_url or not source_api_key):
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
        success("    ✓", "Verify deep parity (survey-definitions) after copy")
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
        from .dimensions.flow_diff import format_diff_for_display, format_diff_summary

        info("[copy-cross-account]", "Running deep parity check (survey-definitions)...")
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
        )
        if report.ok:
            success(
                "[copy-cross-account]",
                f"Deep parity OK (hash={report.hash_a[:12]}).",
            )
        else:
            warn(
                "[copy-cross-account]",
                "Deep parity FAILED (normalized hashes differ).",
            )
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
                    warn("[copy-cross-account]", f"Diff sections: {', '.join(parts)}")
            warn("[copy-cross-account]", f"Diff count: {report.diff_count}")
            if report.diff_paths:
                warn("[copy-cross-account]", "Diff paths (sample):")
                for p in report.diff_paths[:50]:
                    warn("[copy-cross-account]", f"  - {p}")

            if report.flow_changes:
                warn(
                    "[copy-cross-account]",
                    f"SurveyFlow: {format_diff_summary(report.flow_changes)}",
                )
                for line in format_diff_for_display(
                    report.flow_changes, verbose=False
                )[:25]:
                    dim("[copy-cross-account]", line)
                if len(report.flow_changes) > 25:
                    dim(
                        "[copy-cross-account]",
                        f"(flow diffs truncated; showing 25 of {len(report.flow_changes)})",
                    )

            if report.artifacts:
                dim(
                    "[copy-cross-account]",
                    f"Artifacts: a={report.artifacts.get('a')} b={report.artifacts.get('b')}",
                )
                if report.artifacts.get("diff"):
                    dim(
                        "[copy-cross-account]",
                        f"Unified diff: {report.artifacts.get('diff')}",
                    )

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

    base, headers = get_client_config()

    for survey_id in args.survey_ids:
        print(f"Deleting survey {format_survey_ref(survey_id)}...")
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
                    f"Failed to delete {format_survey_ref(survey_id)}: {response.status_code} {response.text}"
                )
            else:
                print(f"Failed to delete {format_survey_ref(survey_id)}: {exc}")
        else:
            print(f"Successfully deleted {format_survey_ref(survey_id)}")

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
    from .cli import _prompt_for_survey_id_if_needed

    survey_id = _prompt_for_survey_id_if_needed(args.survey_id, allow_all_surveys=False)
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
    from .cli import _prompt_for_survey_id_if_needed

    survey_id = _prompt_for_survey_id_if_needed(args.survey_id, allow_all_surveys=False)
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
    from .cli import _prompt_for_survey_id_if_needed

    survey_id = _prompt_for_survey_id_if_needed(args.survey_id, allow_all_surveys=False)
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
    survey_id = args.survey_id
    account = getattr(args, "account", None)
    dest_dir: Path | None = _resolve_pull_dest(_workspace_root(), account, args.dest)
    env = None
    if account:
        env = load_account_env(account, root=_workspace_root())

    from .survey_ref import format_survey_ref
    from .cli import _prompt_for_survey_id_if_needed

    survey_id = _prompt_for_survey_id_if_needed(
        survey_id,
        allow_all_surveys=True,
    )

    print(f"[pull] Downloading survey definition for {format_survey_ref(survey_id)}...")

    try:
        saved_path = download_survey_definition(survey_id, target_dir=dest_dir, env=env)
        print(f"[pull] Saved to: {saved_path}")
    except Exception as e:
        print(
            f"[pull] ERROR: Failed to download survey {format_survey_ref(survey_id)}: {e}"
        )
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


def _prompt_for_any_survey_id(survey_id: str | None) -> str:
    if survey_id:
        return str(survey_id).strip()

    if not sys.stdin.isatty():
        print("[qsync] ERROR: --survey-id required in non-interactive mode")
        raise SystemExit(1)

    from .interactive_menu import select_from_list, autocomplete_from_list
    from .survey_inventory import (
        INVENTORY_CSV,
        LEGACY_SURVEY_CACHE,
        _refresh_inventory_for_prompt,
        _load_all_survey_records,
    )

    def _manual_entry() -> str:
        manual = input("Enter Qualtrics SurveyID (e.g. SV_...): ").strip()
        if not manual:
            print("[qsync] Operation cancelled.")
            raise SystemExit(1)
        return manual

    has_inventory = INVENTORY_CSV.exists() or LEGACY_SURVEY_CACHE.exists()
    if not has_inventory:
        selection = select_from_list(
            message="Inventory file missing. What do you want to do?",
            choices=[
                "✓ Run `qsync survey inventory` now",
                "✎ Enter SurveyID manually",
                "✗ Cancel",
            ],
        )
        if selection is None or "Cancel" in selection:
            print("[qsync] Operation cancelled.")
            raise SystemExit(1)
        if "inventory" in selection.lower():
            if not _refresh_inventory_for_prompt():
                print(
                    "[qsync] Could not refresh inventory. Next: verify credentials "
                    "(run `qsync doctor --check-api`) or pass --survey-id."
                )
                raise SystemExit(1)
        else:
            return _manual_entry()

    records = _load_all_survey_records()
    if not records:
        print("[qsync] No surveys found in inventory. Entering manual SurveyID.")
        return _manual_entry()

    labels = []
    for record in records:
        focal_tag = " (focal)" if record.get("focal") else ""
        labels.append(f"{record['id']} - {record.get('name', 'Untitled')}{focal_tag}")

    label_to_id = {
        label.strip().lower(): record["id"] for label, record in zip(labels, records)
    }

    def _resolve_selected_survey(raw_selection: str | None) -> str | None:
        if raw_selection is None:
            return None
        raw = str(raw_selection).strip()
        if not raw:
            return None

        # Exact label match from autocomplete list.
        exact_label = label_to_id.get(raw.lower())
        if exact_label:
            return exact_label

        # Accept direct SurveyID input (case-insensitive).
        first_token = raw.split(" - ", 1)[0].strip()
        for record in records:
            sid = str(record.get("id", "")).strip()
            if sid and sid.lower() == first_token.lower():
                return sid

        # Try exact survey-name match.
        exact_name_matches = [
            record
            for record in records
            if str(record.get("name", "")).strip().lower() == raw.lower()
        ]
        if len(exact_name_matches) == 1:
            return str(exact_name_matches[0]["id"])

        # Fallback for users pressing Enter on partial autocomplete input.
        prefix_matches = [
            record
            for record in records
            if str(record.get("id", "")).lower().startswith(raw.lower())
            or str(record.get("name", "")).lower().startswith(raw.lower())
        ]
        if len(prefix_matches) == 1:
            return str(prefix_matches[0]["id"])

        contains_matches = [
            record
            for record in records
            if raw.lower() in str(record.get("id", "")).lower()
            or raw.lower() in str(record.get("name", "")).lower()
        ]
        if len(contains_matches) == 1:
            return str(contains_matches[0]["id"])

        candidate_matches = prefix_matches or contains_matches
        if len(candidate_matches) > 1:
            candidate_labels = []
            for record in candidate_matches:
                focal_tag = " (focal)" if record.get("focal") else ""
                candidate_labels.append(
                    f"{record['id']} - {record.get('name', 'Untitled')}{focal_tag}"
                )
            matched = select_from_list(
                "Multiple surveys match your search:",
                candidate_labels + ["Cancel"],
            )
            if not matched or matched == "Cancel":
                return None
            return matched.split(" - ", 1)[0].strip()

        return None

    def _select_via_autocomplete() -> str:
        while True:
            selected = autocomplete_from_list(
                message="Search survey (name or ID)",
                choices=labels,
                instruction="type to filter, enter to select",
            )
            if not selected:
                print("[qsync] Operation cancelled.")
                raise SystemExit(1)
            resolved = _resolve_selected_survey(selected)
            if resolved:
                return resolved
            print(
                "[qsync] Could not match that input to a unique survey. "
                "Please refine your search, select a listed option, or enter SurveyID manually."
            )

    if len(labels) > 40:
        mode = select_from_list(
            "How do you want to select a survey?",
            [
                "Search by name/ID (autocomplete)",
                "Browse all surveys (arrow list)",
                "Enter SurveyID manually",
                "Cancel",
            ],
        )
        if not mode or "Cancel" in mode:
            print("[qsync] Operation cancelled.")
            raise SystemExit(1)
        if mode.startswith("Enter"):
            return _manual_entry()
        if mode.startswith("Browse"):
            selection = select_from_list("Select a survey:", labels)
            if not selection:
                print("[qsync] Operation cancelled.")
                raise SystemExit(1)
            return selection.split(" - ", 1)[0].strip()
        return _select_via_autocomplete()

    choices = list(labels)
    choices.append("─" * 60)
    choices.append("Search by name/ID (autocomplete)")
    choices.append("Enter SurveyID manually")
    choices.append("Cancel")
    selection = select_from_list("Select a survey:", choices)
    if not selection or selection == "Cancel":
        print("[qsync] Operation cancelled.")
        raise SystemExit(1)
    if selection.startswith("Search"):
        return _select_via_autocomplete()
    if selection.startswith("Enter"):
        return _manual_entry()
    return selection.split(" - ", 1)[0].strip()


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

    survey_id = _prompt_for_any_survey_id(args.survey_id)

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
    """Publish staged survey-definition changes by creating a new published version."""
    from .cli import _prompt_for_survey_id_if_needed

    survey_id = _prompt_for_survey_id_if_needed(
        getattr(args, "survey_id", None),
        allow_all_surveys=False,
    )

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

    print(
        f"[publish] POST survey-definitions/{survey_id}/versions "
        f"json={{'Description': {description!r}, 'Published': True}}"
    )
    if dry_run:
        print("[publish] DRY-RUN: not calling Qualtrics.")
        return

    payload = None
    last_exc: Exception | None = None
    for attempt in range(1, retry_attempts + 1):
        try:
            payload = publish_survey_definition(
                survey_id,
                description=description,
                published=True,
                context={"origin": "qsync.cli_survey.publish"},
            )
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            if attempt >= retry_attempts:
                break
            print(
                f"[publish] WARNING: publish failed on attempt {attempt}/{retry_attempts}: {exc}. "
                "Next: verify credentials/permissions (run `qsync doctor --check-api`) and retry."
            )
            print("[publish] Retrying…")
            time.sleep(2)

    if payload is None:
        raise SystemExit(
            f"[publish] ERROR: publish failed after {retry_attempts} attempt(s): {last_exc}"
        )

    metadata = (payload.get("result") or {}).get("metadata") or {}
    version_id = metadata.get("versionID")
    version_num = metadata.get("versionNumber")
    if version_id or version_num:
        extra = []
        if version_num is not None:
            extra.append(f"version={version_num}")
        if version_id is not None:
            extra.append(f"id={version_id}")
        print("[publish] OK: " + " ".join(extra))
    else:
        print("[publish] OK")

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
        raise RuntimeError(f"Survey {survey_id} missing 'result' payload")
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
    survey_ids: list[str] = []
    raw_ids = getattr(args, "survey_id", None)
    if isinstance(raw_ids, list):
        survey_ids.extend([sid.strip() for sid in raw_ids if sid and sid.strip()])
    elif raw_ids:
        survey_ids.append(str(raw_ids).strip())

    ids_file = getattr(args, "survey_ids_file", None)
    if isinstance(ids_file, str) and ids_file:
        survey_ids.extend(_load_survey_ids_from_file(ids_file))

    if not survey_ids:
        # Offer interactive selection for single survey
        from .cli import _prompt_for_survey_id_if_needed

        try:
            prompted_id = _prompt_for_survey_id_if_needed(
                None,
                allow_all_surveys=False,
            )
            survey_ids.append(prompted_id)
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
    base_url, headers = get_client_config()

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
            ctx = load_push_context(survey_id)
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
                data = list_survey_versions(survey_id)
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
    from .cli import _prompt_for_survey_id_if_needed

    survey_id = _prompt_for_survey_id_if_needed(
        getattr(args, "survey_id", None),
        allow_all_surveys=False,
    )

    limit = getattr(args, "limit", None)
    as_json = bool(getattr(args, "json", False))

    data = list_survey_versions(survey_id)
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
    from .cli import _prompt_for_survey_id_if_needed

    survey_id = _prompt_for_survey_id_if_needed(
        getattr(args, "survey_id", None),
        allow_all_surveys=False,
    )
    version_id = (args.version_id or "").strip()
    if not version_id:
        raise SystemExit("[version-fetch] ERROR: --version-id is required")

    fmt = (getattr(args, "format", None) or "json").strip().lower()
    out_path = getattr(args, "output", None)
    as_json = bool(getattr(args, "json", False))

    payload = fetch_survey_version(survey_id, version_id=version_id, fmt=fmt)

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
    from .cli import _prompt_for_survey_id_if_needed

    survey_id = _prompt_for_survey_id_if_needed(
        getattr(args, "survey_id", None),
        allow_all_surveys=False,
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

    # Push safeguards (same spirit as push-question / push).
    try:
        ctx = load_push_context(survey_id)
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
    historical = fetch_survey_version(survey_id, version_id=version_id, fmt="json")

    base_url, headers = get_client_config()

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
        raise RuntimeError("Remote question payload missing 'result'")
    return result


def _format_question(obj: dict) -> str:
    """Format question as pretty JSON for diffing."""
    return json.dumps(obj, indent=2, sort_keys=True)


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


def handle_export_responses(args: argparse.Namespace) -> None:
    """Export survey responses to CSV."""
    from .cli import _prompt_for_survey_id_if_needed

    survey_id = _prompt_for_survey_id_if_needed(
        getattr(args, "survey_id", None),
        allow_all_surveys=False,
    )
    output_dir = Path(args.output) if args.output else _workspace_root() / "responses"

    base_url, headers = get_client_config()

    # Start export
    from .survey_ref import format_survey_ref

    print(f"[export-responses] Starting export for {format_survey_ref(survey_id)}...")
    payload = {
        "format": "csv",
        "useLabels": True,
        "seenUnansweredRecode": 999,
        "timeZone": "UTC",
    }

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
    print(f"[export-responses] Export started. Progress ID: {progress_id}")

    # Poll for completion
    progress_status = "inProgress"
    file_id = None

    while progress_status not in ("complete", "failed"):
        print("[export-responses] Checking progress...")
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
            print("[export-responses] ERROR: Export failed")
            sys.exit(1)

        if progress_status == "complete":
            file_id = result["fileId"]
        else:
            time.sleep(2)

    print(f"[export-responses] Export complete. File ID: {file_id}")

    # Download file
    print("[export-responses] Downloading file...")
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

    # Save and extract
    output_dir.mkdir(parents=True, exist_ok=True)

    # Get survey name for filename
    try:
        surveys = list_surveys(base_url, headers)
        survey_name = next(
            (s["name"] for s in surveys if s["id"] == survey_id), survey_id
        )
        # Sanitize filename
        safe_name = "".join(
            c if c.isalnum() or c in " -_" else "_" for c in survey_name
        ).strip()
    except Exception:
        safe_name = survey_id

    zip_path = output_dir / f"{safe_name}_{survey_id}.zip"

    with open(zip_path, "wb") as f:
        for chunk in download_response.iter_content(chunk_size=8192):
            f.write(chunk)

    print(f"[export-responses] Saved zip to {zip_path}")

    # Extract CSV
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        zip_ref.extractall(output_dir)
        for file in zip_ref.namelist():
            print(f"  - {file}")
    print(f"[export-responses] Extracted to {output_dir}")


def handle_export_translation(args: argparse.Namespace) -> None:
    """Export a translation-review document for a survey (DOCX or PDF)."""

    from .terminal_output import error, info, success, warn
    from .translation_export import export_survey_to_pdf, export_survey_to_word
    from .interactive_menu import is_interactive
    from .cli import _prompt_for_survey_id_if_needed

    survey_id = _prompt_for_survey_id_if_needed(
        getattr(args, "survey_id", None),
        allow_all_surveys=False,
    )

    output = getattr(args, "output", None)
    no_html = bool(getattr(args, "no_html", False))
    edf_args = getattr(args, "edf", None) or []
    edf_preset_names = getattr(args, "edf_preset", None) or []
    list_edf_presets = bool(getattr(args, "list_edf_presets", False))
    edf_overrides = {}
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
        edf_overrides[k] = v

    def _load_edf_presets_for_survey(
        survey_id: str,
    ) -> dict[str, dict[str, str]]:
        root = resolve_root(required=False) or Path.cwd()
        preset_path = root / "surveys" / "edf_presets.json"
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
        presets = _load_edf_presets_for_survey(str(survey_id))
        if not presets:
            info(
                "[qsync:export-translation]",
                "No EDF presets found. Add surveys/edf_presets.json to define them.",
            )
            return
        info("[qsync:export-translation]", "Available EDF presets:")
        for name in sorted(presets.keys()):
            preset_vals = ", ".join(
                f"{k}={v}" for k, v in sorted(presets[name].items())
            )
            info(None, f"  - {name}: {preset_vals}")
        return

    if edf_preset_names:
        presets = _load_edf_presets_for_survey(str(survey_id))
        if not presets:
            error(
                "[qsync:export-translation]",
                "No EDF presets found. Add surveys/edf_presets.json to define them.",
            )
            sys.exit(1)
        for name in edf_preset_names:
            preset = presets.get(str(name))
            if not preset:
                available = ", ".join(sorted(presets.keys()))
                error(
                    "[qsync:export-translation]",
                    f"Unknown --edf-preset {name}. Available: {available}",
                )
                sys.exit(1)
            for key, value in preset.items():
                if key in edf_overrides:
                    warn(
                        "[qsync:export-translation]",
                        f"EDF preset {name} set {key}={value}, but overridden by --edf {key}={edf_overrides[key]}",
                    )
                else:
                    edf_overrides[key] = value
    smart_name = bool(getattr(args, "smart_name", False))
    do_open = bool(getattr(args, "open", False))
    compare_to_base = bool(getattr(args, "compare_to_base", False))
    refresh = bool(getattr(args, "refresh", False))
    layout_heuristics = bool(getattr(args, "layout_heuristics", False))
    format = getattr(args, "format", "docx")
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

    # Validate format + output path combinations
    if format == "both" and output is not None:
        output_suffix = getattr(output, "suffix", "").lower()
        if output_suffix in (".docx", ".pdf"):
            error(
                "[qsync:export-translation]",
                "When using --format both, --output must be a directory (or omitted), not a file path.",
            )
            sys.exit(1)

    # If multiple languages are requested, output must be a directory (or omitted).
    if len([x for x in render_langs if x]) > 1 and output is not None:
        output_suffix = getattr(output, "suffix", "").lower()
        if output_suffix in (".docx", ".pdf"):
            error(
                "[qsync:export-translation]",
                "When exporting multiple languages, --output must be a directory (or omitted).",
            )
            sys.exit(1)
    # If we're generating bilingual exports, we also regenerate the base-language export.
    # That means a single output file path is ambiguous (it would need to hold multiple files).
    if compare_to_base and output is not None:
        output_suffix = getattr(output, "suffix", "").lower()
        if output_suffix in (".docx", ".pdf"):
            error(
                "[qsync:export-translation]",
                "When using --compare-to-base, --output must be a directory (or omitted), not a file path.",
            )
            sys.exit(1)

    try:
        paths: list = []
        exported_base = False
        interactive = is_interactive()
        for lang in render_langs:
            # Determine which format(s) to export
            formats_to_export = []
            if format == "both":
                formats_to_export = ["docx", "pdf"]
            else:
                formats_to_export = [format]

            for fmt in formats_to_export:
                if fmt == "docx":
                    path = export_survey_to_word(
                        str(survey_id),
                        output_path=output,
                        edf_overrides=edf_overrides or None,
                        smart_name=smart_name,
                        include_html_source=not no_html,
                        layout_heuristics=layout_heuristics,
                        render_language=lang,
                        compare_to_base=compare_to_base,
                        refresh=refresh,
                        include_js_strings=not args.skip_js_strings,
                        interactive=interactive,
                        flow_trace=flow_trace_cb,
                    )
                    paths.append(path)
                elif fmt == "pdf":
                    path = export_survey_to_pdf(
                        str(survey_id),
                        output_path=output,
                        edf_overrides=edf_overrides or None,
                        smart_name=smart_name,
                        include_html_source=not no_html,
                        layout_heuristics=layout_heuristics,
                        render_language=lang,
                        compare_to_base=compare_to_base,
                        refresh=refresh,
                        include_js_strings=not args.skip_js_strings,
                        interactive=interactive,
                        flow_trace=flow_trace_cb,
                    )
                    paths.append(path)

            # In bilingual mode, also regenerate the base-language export once per run.
            if compare_to_base and not exported_base:
                exported_base = True
                for fmt in formats_to_export:
                    if fmt == "docx":
                        paths.append(
                            export_survey_to_word(
                                str(survey_id),
                                output_path=output,
                                edf_overrides=edf_overrides or None,
                                smart_name=smart_name,
                                include_html_source=not no_html,
                                layout_heuristics=layout_heuristics,
                                render_language=None,
                                compare_to_base=False,
                                refresh=False,
                                include_js_strings=not args.skip_js_strings,
                                interactive=interactive,
                                flow_trace=flow_trace_cb,
                            )
                        )
                    elif fmt == "pdf":
                        paths.append(
                            export_survey_to_pdf(
                                str(survey_id),
                                output_path=output,
                                edf_overrides=edf_overrides or None,
                                smart_name=smart_name,
                                include_html_source=not no_html,
                                layout_heuristics=layout_heuristics,
                                render_language=None,
                                compare_to_base=False,
                                refresh=False,
                                include_js_strings=not args.skip_js_strings,
                                interactive=interactive,
                                flow_trace=flow_trace_cb,
                            )
                        )
    except Exception as e:
        error("[qsync:export-translation]", f"ERROR: {e}")
        sys.exit(1)

    # Report all exported files with format indicator
    for path in paths:
        fmt_label = path.suffix.upper().lstrip(".")
        success("[qsync:export-translation]", f"Exported ({fmt_label}): {path}")

    if do_open and len(paths) == 1:
        try:
            import subprocess

            if sys.platform == "darwin":
                subprocess.run(["open", str(paths[0])], check=False)
            elif os.name == "nt":
                os.startfile(str(paths[0]))  # type: ignore[attr-defined]
            else:
                subprocess.run(["xdg-open", str(paths[0])], check=False)
        except Exception:
            error(
                "[qsync:export-translation]", "Could not open document automatically."
            )


def _add_export_translation_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Qualtrics survey ID to export (omit to select interactively)",
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
        help="Named EDF preset from surveys/edf_presets.json (repeatable).",
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
        action="Provide --language/--languages or run `qsync translations apply` to stage changes.",
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

    survey_id = args.survey_id
    languages = _collect_languages_from_args(args)
    if languages:
        warn(
            "[qsync:translations]",
            "Note: --language/--languages are ignored for `translations pull` "
            "(translations live in the survey definition).",
        )

    cache, changed = refresh_survey_cache(survey_id)
    if changed:
        success("[qsync:translations]", f"Pulled: {cache.path}")
    else:
        info("[qsync:translations]", f"Cache already up to date: {cache.path}")


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
    if getattr(args, "translations_command", "") == "apply":
        warn(
            "[qsync:translations]",
            "Command `qsync translations apply` is deprecated. Use `qsync translations stage` instead.",
        )
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

        # Default: focal-only (inventory-driven). Use --all to include non-focal.
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
                        f"[qsync:master-preview] Filtered to {len(csv_rows)}/{before} focal survey row(s) (use --all to include non-focal)"
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

        # Default: focal-only (inventory-driven). Use --all to include non-focal.
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
                        f"[qsync:master-stage] Filtered to {len(csv_rows)}/{before} focal survey row(s) (use --all to include non-focal)"
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

        # Default: focal-only (inventory-driven). Use --all to include non-focal.
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
                        f"Filtered to {len(filtered_csv_rows)}/{before} focal survey row(s) (use --all to include non-focal)",
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
            "  Embedded/options: add-embedded-field, remove-embedded-field, rename-embedded-field, cleanup-embedded-data, prolific-auth\n"
            "  Lifecycle/versions: publish, activate, deactivate, versions, version-fetch, rollback\n"
            "  Utilities: inspect-question, push-question\n"
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
        "--yes",
        action="store_true",
        help="Skip confirmation prompts (required for --allow-incomplete in non-interactive runs)",
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
    p_parity.add_argument("--a", required=True, help="Survey ID A")
    p_parity.add_argument("--b", required=True, help="Survey ID B")
    p_parity.add_argument(
        "--deep",
        action="store_true",
        help="Run deep parity against survey-definitions JSON (strict; ignores only cross-account volatile fields).",
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
            "(overrides TARGET_* defaults; explicit --target-* flags still win)."
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
        "--yes", action="store_true", help="Skip confirmation prompt"
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
        help="After copy, verify deep parity against survey-definitions JSON (strict; ignores only cross-account volatile fields); exits non-zero on mismatch.",
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
        dest="survey_id",
        help="Qualtrics survey ID (omit to select interactively)",
    )
    sel = p_prepare.add_mutually_exclusive_group()
    sel.add_argument(
        "--focal",
        action="store_true",
        help="Prepare all focal surveys from surveys/inventory.csv",
    )
    sel.add_argument(
        "--all",
        dest="all_surveys",
        action="store_true",
        help="Prepare all surveys from surveys/inventory.csv (can be slow)",
    )
    p_prepare.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive prompts (still pull-only)",
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
        dest="survey_id",
        help="Qualtrics survey ID to download (omit to select interactively)",
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
    p_cleanup.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt",
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
        "--yes",
        action="store_true",
        help="Skip prompts and use the recommended mode (replace if Prolific is already present; otherwise append)",
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

    # publish
    p_publish = survey_subs.add_parser(
        "publish",
        help="Publish staged survey-definition changes (create a published version)",
    )
    p_publish.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Qualtrics survey ID to publish (omit to select interactively)",
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
        help="Qualtrics survey ID to activate (repeatable; omit for interactive selection of single survey)",
    )
    p_activate.add_argument(
        "--survey-ids-file",
        dest="survey_ids_file",
        help="Path to a newline- or CSV-delimited list of Survey IDs",
    )
    p_activate.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt",
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
        "--yes",
        action="store_true",
        help="Skip confirmation prompt",
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
    p_rollback.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompts.",
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
    p_inspect.add_argument("--question-id", required=True, dest="question_id")
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
        "--yes",
        "-y",
        action="store_true",
        help="Skip confirmation prompt",
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

    # export-responses
    p_export = survey_subs.add_parser(
        "export-responses",
        help="Export survey responses to CSV",
    )
    p_export.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Qualtrics survey ID to export responses from (omit to select interactively)",
    )
    p_export.add_argument(
        "--output",
        help="Output directory (default: responses/)",
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
    p_export_side.add_argument("--a", required=True, help="Survey ID A")
    p_export_side.add_argument("--b", required=True, help="Survey ID B")
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
        "--all",
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
        "--all",
        dest="all_surveys",
        action="store_true",
        help="Include non-focal surveys from qualtrics_master.csv (default: focal-only)",
    )
    p_master_stage.set_defaults(func=handle_master_stage)

    # master apply
    p_master_apply = master_subs.add_parser(
        "apply",
        help="Legacy: apply changes directly from master CSV to Qualtrics (bypasses pending)",
    )
    p_master_apply.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-survey status lines (disables progress bar)",
    )
    p_master_apply.add_argument(
        "--mapping-csv",
        type=Path,
        dest="mapping_csv",
        help="Path to a Qualtrics API field mapping CSV for Survey Master (overrides packaged defaults)",
    )
    p_master_apply.add_argument(
        "--allow-dangerous",
        action="store_true",
        help="Allow changes to dangerous fields (isActive, redirect URLs, etc.)",
    )
    p_master_apply.add_argument(
        "--force",
        action="store_true",
        help="Override drift detection (proceed even if values changed since last pull)",
    )
    p_master_apply.add_argument(
        "--survey-id",
        help="Apply only to this specific survey (by SurveyID); useful for testing",
    )
    p_master_apply.add_argument(
        "--skip-drift",
        action="store_true",
        help="Skip drift detection (faster but riskier; assumes no concurrent changes)",
    )
    p_master_apply.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be applied without actually writing changes",
    )
    p_master_apply.add_argument(
        "--tag",
        action="append",
        dest="tags",
        help="Filter surveys by tag (e.g., --tag component=pre --tag stage=prod)",
    )
    p_master_apply.add_argument(
        "--all",
        dest="all_surveys",
        action="store_true",
        help="Include non-focal surveys from qualtrics_master.csv (default: focal-only)",
    )
    p_master_apply.set_defaults(func=handle_master_apply)

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
        "--all",
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
        "--yes",
        "-y",
        action="store_true",
        help="Skip confirmation prompts",
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
    p_master_rollback.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt for non-dry-run rollbacks",
    )
    p_master_rollback.set_defaults(func=handle_master_rollback)

    # Help output ordering: keep related commands together.
    from .argparse_support import hide_subparser_choices, reorder_subparser_choices

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
            # Lifecycle / versions
            "publish",
            "activate",
            "deactivate",
            "versions",
            "version-fetch",
            "rollback",
            # Utilities
            "inspect-question",
            "push-question",
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
            "pull",
            "preview",
            "stage",
            "push",
            "rollback",
        ],
    )
    hide_subparser_choices(master_subs, ["apply"])
