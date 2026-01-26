"""
Survey management CLI commands for qsync.
"""

from __future__ import annotations

import argparse
import json
import sys
import os
import time
import zipfile
import csv
from difflib import unified_diff
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .config import get_client_config, resolve_root
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
    """Fetch list of all surveys."""
    params = {"limit": 100}
    resp = send_api_request(
        action="qsync.survey.list",
        method="GET",
        base_url=base,
        headers=headers,
        path="surveys",
        log_event=False,
        params=params,
        timeout=60,
    )
    return resp.json().get("result", {}).get("elements", [])


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

    base, headers = get_client_config()

    print(f"Fetching surveys from {base}...")
    surveys = list_surveys(base, headers)

    print(f"\nFound {len(surveys)} surveys:\n")
    print(f"{'Survey ID':<20} | {'Status':<10} | {'Created':<20} | {'Name'}")
    print("-" * 80)

    for survey in surveys:
        sid = survey.get("id")
        name = survey.get("name")
        status = survey.get("isActive")
        created = survey.get("creationDate")
        status_str = "Active" if status else "Inactive"
        date_str = created[:10] if created else "N/A"

        print(f"{sid:<20} | {status_str:<10} | {date_str:<20} | {name}")


def handle_copy(args: argparse.Namespace) -> None:
    """Copy/import a survey (from Qualtrics or a local QSF) into a new survey."""

    base, headers = get_client_config()

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
    if not args.generate_qsf:
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
    from .qualtrics_client import publish_survey_definition
    from .config import load_env
    from .translations import (
        _check_html_hazards,
        _check_placeholders,
        _check_value_length_limit,
        normalize_translation_map,
    )

    # Parse arguments
    source_id = args.source_survey_id
    new_name = args.new_name
    target_api_key = args.target_api_key
    target_base_url = args.target_base_url
    copy_translations = not bool(getattr(args, "no_translations", False))

    def _coerce_result_payload(payload: dict) -> dict:
        result = payload.get("result")
        if isinstance(result, dict):
            return result
        if isinstance(payload, dict):
            return payload
        return {}

    def _normalize_language_list(raw: list[str] | None) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in raw or []:
            code = str(item or "").strip()
            if not code:
                continue
            code = "-".join(
                part.strip().upper() for part in code.split("-") if part.strip()
            )
            if not code or code in seen:
                continue
            seen.add(code)
            cleaned.append(code)
        return cleaned

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
        return "-".join(
            part.strip().upper() for part in lang.split("-") if part.strip()
        )

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
        return _normalize_language_list(list(langs) if isinstance(langs, list) else [])

    def _ensure_languages_enabled(
        base_url: str, headers: dict, survey_id: str, languages: list[str]
    ) -> list[str]:
        current = set(_list_enabled_languages(base_url, headers, survey_id))
        desired = _normalize_language_list(list(current.union(languages)))
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

    def _pull_translation_map(
        base_url: str, headers: dict, survey_id: str, language: str
    ) -> dict:
        resp = send_api_request(
            action="qsync.translations.pull",
            method="GET",
            base_url=base_url,
            headers=headers,
            path=f"surveys/{survey_id}/translations/{language}",
            survey_id=survey_id,
            log_event=False,
            timeout=60,
        )
        return _coerce_result_payload(resp.json())

    def _push_translation_map(
        base_url: str, headers: dict, survey_id: str, language: str, payload: dict
    ) -> None:
        send_api_request(
            action="qsync.translations.push",
            method="PUT",
            base_url=base_url,
            headers=headers,
            path=f"surveys/{survey_id}/translations/{language}",
            survey_id=survey_id,
            json=payload,
            timeout=60,
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

        info(
            "[copy-cross-account]",
            f"Enabling languages in target: {', '.join(source_langs)}",
        )
        _ensure_languages_enabled(
            target_base, target_headers, target_survey_id, source_langs
        )

        base_map = _pull_translation_map(
            source_base, source_headers, source_survey_id, source_base_lang
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
            raw_map = _pull_translation_map(
                source_base, source_headers, source_survey_id, lang
            )
            normalized = normalize_translation_map(raw_map, coerce_nulls=True)

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

            _push_translation_map(
                target_base, target_headers, target_survey_id, lang, normalized
            )
            completed_languages.append(lang)
            success("[copy-cross-account]", f"  [{lang}] ✓ Copied successfully")

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
    source_env = load_env()
    if getattr(args, "source_base_url", None):
        source_env["QUALTRICS_BASE_URL"] = args.source_base_url
    if getattr(args, "source_api_key", None):
        source_env["X-API-TOKEN"] = args.source_api_key
    try:
        source_base, source_headers = get_client_config(source_env)
    except Exception as e:
        print(f"ERROR: Invalid source credentials: {e}")
        sys.exit(1)

    # Build target credentials (default account, unless overridden)
    try:
        target_env = load_env()
        if target_base_url:
            target_env["QUALTRICS_BASE_URL"] = target_base_url
        if target_api_key:
            target_env["X-API-TOKEN"] = target_api_key
        target_base, target_headers = get_client_config(target_env)
    except Exception as e:
        print(f"ERROR: Invalid target credentials: {e}")
        sys.exit(1)

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
    print()
    info("  Target Survey:", "")
    info("    Name:", final_name)
    info("    Account:", target_base)
    if conflict_msg:
        dim("    Conflict resolution:", conflict_msg)
    print()
    info("  Operations:", "")
    success(
        "    ✓", "Copy survey definition (all questions, flow, logic, embedded data)"
    )
    if copy_translations:
        success("    ✓", "Copy translations (enabled languages + maps)")
    else:
        dim("    ✗", "Copy translations (use default; disable via --no-translations)")

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
    base, headers = get_client_config()

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

    dry_run = getattr(args, "dry_run", False)
    counts_scope = getattr(args, "counts_scope", None)

    if survey_filter and counts_scope:
        print(
            "[inventory] NOTE: --survey-id refresh already fetches response counts; "
            "ignoring --focal/--full."
        )
        counts_scope = None

    if survey_filter:
        print(f"[inventory] Refreshing {len(survey_filter)} targeted survey(s)...")
    else:
        print("[inventory] Fetching full survey inventory from Qualtrics...")

    inventory, changed_records = refresh_inventory(
        base,
        headers,
        survey_filter=survey_filter,
        dry_run=dry_run,
        counts_scope=counts_scope,
    )

    editable = sum(1 for record in inventory if record.get("editableViaApi"))
    non_editable = len(inventory) - editable

    if dry_run:
        print(
            f"[DRY RUN] Would save {len(inventory)} surveys (editable={editable}, non-editable={non_editable})"
        )
    else:
        print(
            f"Saved {len(inventory)} surveys to {SURVEY_CACHE} (editable={editable}, non-editable={non_editable})"
        )

    if changed_records:
        for survey in changed_records:
            label = "editable" if survey.get("editableViaApi") else "read-only"
            print(
                f"  - {survey.get('name', '(unnamed)')} | ID={survey.get('id')} | {label}"
            )
    else:
        print("  - No inventory rows changed (ignoring generated_at).")


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


def handle_pull(args: argparse.Namespace) -> None:
    """Download a survey definition JSON to local cache."""
    survey_id = args.survey_id
    dest_dir = Path(args.dest) if args.dest else None

    from .survey_ref import format_survey_ref
    from .cli import _prompt_for_survey_id_if_needed

    survey_id = _prompt_for_survey_id_if_needed(
        survey_id,
        allow_all_surveys=True,
    )

    print(f"[pull] Downloading survey definition for {format_survey_ref(survey_id)}...")

    try:
        saved_path = download_survey_definition(survey_id, target_dir=dest_dir)
        print(f"[pull] Saved to: {saved_path}")
    except Exception as e:
        print(
            f"[pull] ERROR: Failed to download survey {format_survey_ref(survey_id)}: {e}"
        )
        sys.exit(1)


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
    from .terminal_output import success
    from .translations import pull_translations

    survey_id = args.survey_id
    languages = _collect_languages_from_args(args)
    paths = pull_translations(survey_id, languages)
    for path in paths:
        success("[qsync:translations]", f"Pulled: {path}")


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
    """Pull survey master snapshots and generate master CSV."""
    from .survey_master import pull_master

    mapping_csv = getattr(args, "mapping_csv", None)
    if mapping_csv:
        os.environ["QSYNC_MAPPING_CSV"] = str(Path(mapping_csv).expanduser().resolve())

    survey_ids = (
        args.survey_ids if hasattr(args, "survey_ids") and args.survey_ids else None
    )
    verbose = bool(getattr(args, "verbose", False))

    try:
        snapshots_created, csv_path = pull_master(
            survey_ids=survey_ids, verbose=verbose
        )
        print(f"\n✓ Pull complete: {snapshots_created} snapshots, CSV at {csv_path}")
    except Exception as e:
        print(f"[qsync:master-pull] ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def handle_master_preview(args: argparse.Namespace) -> None:
    """Preview changes that would be applied to Qualtrics."""
    import difflib
    import json
    from .survey_master import preview_master, load_master_csv
    from .survey_tags import parse_tag_filters, filter_surveys_by_tags
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

        result = preview_master(
            csv_headers=csv_headers,
            csv_rows=csv_rows,
            verbose=output_format == "text",
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

        next_line = "💡 Next: Run 'qsync survey master apply' to write changes (then 'push' to publish)"
        if use_color:
            next_line = colored(next_line, Colors.GREEN)
        print(f"\n{next_line}")

    except Exception as e:
        error_msg = f"[qsync:master-preview] ERROR: {e}"
        if use_color:
            error_msg = colored(error_msg, Colors.RED)
        print(error_msg, file=sys.stderr)
        sys.exit(1)


def handle_master_apply(args: argparse.Namespace) -> None:
    """Apply changes from master CSV to Qualtrics."""
    from .survey_master import apply_master, load_master_csv
    from .survey_tags import parse_tag_filters, filter_surveys_by_tags
    from .terminal_output import error, header, info, success, warn

    allow_dangerous = getattr(args, "allow_dangerous", False)
    force = getattr(args, "force", False)
    survey_id = getattr(args, "survey_id", None)
    skip_drift = getattr(args, "skip_drift", False)
    dry_run = getattr(args, "dry_run", False)
    tag_specs = getattr(args, "tags", None)

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

        result = apply_master(
            allow_dangerous=allow_dangerous,
            force=force,
            verbose=True,
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

        # Print details
        if result["details"]:
            header(None, "Details:")
            for detail in result["details"]:
                status = "✓" if detail["applied"] else "✗"
                if detail["applied"]:
                    success(None, f"{status} {detail['survey_id']}: {detail['reason']}")
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
    """Handle 'qsync survey master push' command."""

    from .survey_master import push_master
    from .terminal_output import error, header, info, success, warn

    description = getattr(args, "description", None)
    survey_id = getattr(args, "survey_id", None)

    try:
        mapping_csv = getattr(args, "mapping_csv", None)
        if mapping_csv:
            os.environ["QSYNC_MAPPING_CSV"] = str(
                Path(mapping_csv).expanduser().resolve()
            )

        result = push_master(
            description=description,
            verbose=True,
            survey_id=survey_id,
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
        info(None, f"Surveys published: {result['surveys_published']}")
        info(None, f"Surveys failed: {result['surveys_failed']}")

        # Print details
        if result["details"]:
            header(None, "Details:")
            for detail in result["details"]:
                status = "✓" if detail["published"] else "✗"
                if detail["published"]:
                    success(None, f"{status} {detail['survey_id']}: {detail['reason']}")
                else:
                    warn(None, f"{status} {detail['survey_id']}: {detail['reason']}")

        if result["surveys_published"] > 0:
            success(
                "[qsync:master-push]",
                f"Push complete: {result['surveys_published']} survey/surveys published",
            )
        else:
            info("[qsync:master-push]", "No surveys were published")

        if result["surveys_failed"] > 0:
            sys.exit(1)

    except Exception as e:
        error("[qsync:master-push]", f"ERROR: {e}")
        sys.exit(1)


def register_survey_commands(subparsers: argparse._SubParsersAction) -> None:
    """Register `qsync survey ...` subcommands."""

    p_survey = subparsers.add_parser(
        "survey",
        help=(
            "Manage Qualtrics surveys (inventory, copy/rename/delete, "
            "publish/version/rollback, master)"
        ),
    )
    survey_subs = p_survey.add_subparsers(dest="survey_command", required=True)

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
        "--source-api-key",
        help="API key for source account (optional; defaults to .env)",
    )
    p_copy_xacct.add_argument(
        "--source-base-url",
        help="Base URL for source account (optional; defaults to .env)",
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
            "Choices: inventory,items,translations,eos,js"
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
        help="Destination directory (default: surveys/)",
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

    # master
    p_master = survey_subs.add_parser(
        "master",
        help="Manage survey master (focal-only bulk editing)",
    )
    master_subs = p_master.add_subparsers(dest="master_command", required=True)

    # master pull
    p_master_pull = master_subs.add_parser(
        "pull",
        help="Pull focal survey snapshots and generate master CSV",
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
    p_master_pull.set_defaults(func=handle_master_pull)

    # master preview
    p_master_preview = master_subs.add_parser(
        "preview",
        help="Preview changes that would be applied by master apply",
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
    p_master_preview.set_defaults(func=handle_master_preview)

    # master apply
    p_master_apply = master_subs.add_parser(
        "apply",
        help="Apply changes from master CSV to Qualtrics",
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
    p_master_apply.set_defaults(func=handle_master_apply)

    # master push
    p_master_push = master_subs.add_parser(
        "push",
        help="Publish surveys after applying changes",
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
        help="Publish only this specific survey (by SurveyID)",
    )
    p_master_push.set_defaults(func=handle_master_push)
