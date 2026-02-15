"""Workspace onboarding wizard for qsync (MVP + Stage 2)."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

from .interactive_menu import (
    CUSTOM_STYLE,
    confirm,
    is_interactive,
    select_from_list,
    should_use_questionary,
)

QUALTRICS_DOC_DATACENTER_ID = "https://www.qualtrics.com/support/integrations/api-integration/finding-qualtrics-ids/#LocatingtheDatacenterID"
QUALTRICS_DOC_API_TOKEN = "https://www.qualtrics.com/support/integrations/api-integration/overview/#GeneratingAnAPIToken"


def _normalize_datacenter(raw: str | None) -> str | None:
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    text = text.replace("https://", "").replace("http://", "")
    text = text.split("/", 1)[0].strip()
    if not text:
        return None
    if text.endswith("qualtrics.com"):
        return text
    if "." in text:
        return text
    return f"{text}.qualtrics.com"


def _print_header(title: str) -> None:
    print()
    print(title)
    print("=" * len(title))


def _preflight_checklist() -> bool:
    _print_header("qsync onboarding")
    print("You'll need:")
    print("- Qualtrics datacenter subdomain (e.g., iad1)")
    print(f"  Docs: {QUALTRICS_DOC_DATACENTER_ID}")
    print("- Qualtrics API token (X-API-TOKEN)")
    print(f"  Docs: {QUALTRICS_DOC_API_TOKEN}")
    print("Estimated time: ~2–3 minutes")
    print()
    if should_use_questionary() and is_interactive():
        return confirm("Continue setup?", default=True)
    resp = input("Continue setup? [Y/n] ").strip().lower()
    return resp in {"", "y", "yes"}


def _menu_choice_text(label: str, done: bool) -> str:
    status = "✓" if done else "•"
    return f"[{status}] {label}"


def _step_banner(step: int, total: int, title: str) -> None:
    print()
    print(f"Step {step} of {total}: {title}")
    print("-" * (len(title) + 12))


def _detect_existing_workspace(root: Path) -> Dict[str, bool]:
    return {
        "env (.env)": (root / ".env").exists(),
        "state (.qsync)": (root / ".qsync").exists(),
        "surveys": (root / "surveys").exists(),
        "excel": (root / "excel").exists(),
        "survey_js": (root / "survey_js").exists(),
        "contents": (root / "contents").exists(),
        "logs": (root / "logs").exists(),
        "export": (root / "export").exists(),
        "responses": (root / "responses").exists(),
        "tmp": (root / "tmp").exists(),
    }


def _workspace_artifact_paths(root: Path) -> Dict[str, Path]:
    return {
        "env (.env)": root / ".env",
        "state (.qsync)": root / ".qsync",
        "surveys": root / "surveys",
        "excel": root / "excel",
        "survey_js": root / "survey_js",
        "contents": root / "contents",
        "logs": root / "logs",
        "export": root / "export",
        "responses": root / "responses",
        "tmp": root / "tmp",
    }


def _unique_destination(base: Path) -> Path:
    if not base.exists():
        return base
    for i in range(1, 1000):
        candidate = base.with_name(f"{base.name}.{i}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Unable to choose a unique path under: {base.parent}")


def _archive_workspace_artifacts(root: Path, existing: Dict[str, bool]) -> Path | None:
    artifact_paths = _workspace_artifact_paths(root)
    to_move = [
        (label, artifact_paths[label]) for label, present in existing.items() if present
    ]
    if not to_move:
        return None

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    archive_dir = root / ".qsync_archives" / stamp
    archive_dir.mkdir(parents=True, exist_ok=True)

    for label, path in to_move:
        dest = _unique_destination(archive_dir / path.name)
        shutil.move(str(path), str(dest))
        print(f"- archived {label}: {path} -> {dest}")

    return archive_dir


def _delete_workspace_artifacts(root: Path, existing: Dict[str, bool]) -> None:
    artifact_paths = _workspace_artifact_paths(root)
    to_delete = [
        (label, artifact_paths[label]) for label, present in existing.items() if present
    ]
    for label, path in to_delete:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
        print(f"- deleted {label}: {path}")


def _pick_root(default_root: Path) -> Path:
    from .interactive_menu import text_input

    resp = text_input(
        "Workspace root",
        default=str(default_root),
        instruction="Path that will contain surveys/, excel/, survey_js/, and .env",
        validate_while_typing=False,
    )
    return Path(resp).expanduser() if resp else default_root


def _collect_credentials() -> Tuple[str | None, str | None]:
    from .interactive_menu import text_input
    from .input_validators import DatacenterHostValidator

    print(f"Docs: {QUALTRICS_DOC_DATACENTER_ID}")
    datacenter = text_input(
        "Qualtrics datacenter subdomain (example: iad1)",
        instruction="We will save https://<subdomain>.qualtrics.com",
        validator=DatacenterHostValidator(),
        validate_while_typing=True,
    )
    print(f"Docs: {QUALTRICS_DOC_API_TOKEN}")
    token = text_input(
        "API token (X-API-TOKEN)",
        instruction="Paste the token from Qualtrics (hidden input)",
        secret=True,
    )

    token_value = (token or "").strip() or None
    if token_value is not None and len(token_value) < 10 and (should_use_questionary() and is_interactive()):
        print("Token looks too short; please paste the full X-API-TOKEN value.")
        token = text_input(
            "API token (X-API-TOKEN)",
            instruction="Paste the token from Qualtrics (hidden input)",
            secret=True,
        )
        token_value = (token or "").strip() or None

    return _normalize_datacenter(datacenter), token_value


def _ensure_dirs(root: Path) -> List[Path]:
    dirs = [
        root / "surveys",
        root / "surveys" / "pending",
        root / "excel",
        root / "survey_js" / "core",
        root / "contents" / "qualtrics_library_messages",
        root / "contents" / "qualtrics_survey_translations",
        root / "logs",
        root / "export",
        root / "responses",
        root / "tmp",
    ]
    created: List[Path] = []
    for d in dirs:
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created.append(d)
    return created


def _state_dir(root: Path) -> Path:
    return root / ".qsync"


def _state_path(root: Path) -> Path:
    return _state_dir(root) / "onboard-state.json"


def _prefs_path(root: Path) -> Path:
    return _state_dir(root) / "preferences.json"


def _log_path(root: Path) -> Path:
    return _state_dir(root) / "setup-log.json"


def _load_state(root: Path) -> Dict[str, object]:
    path = _state_path(root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(root: Path, state: Dict[str, object]) -> None:
    state_dir = _state_dir(root)
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(state)
    if isinstance(payload.get("root"), Path):
        payload["root"] = str(payload["root"])
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    _state_path(root).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _load_prefs(root: Path) -> Dict[str, object]:
    path = _prefs_path(root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_prefs(root: Path, prefs: Dict[str, object]) -> None:
    state_dir = _state_dir(root)
    state_dir.mkdir(parents=True, exist_ok=True)
    _prefs_path(root).write_text(json.dumps(prefs, indent=2), encoding="utf-8")


def _append_setup_log(root: Path, entry: Dict[str, object]) -> None:
    state_dir = _state_dir(root)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = _log_path(root)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, list):
                existing.append(entry)
                path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
                return
        except Exception:
            pass
    path.write_text(json.dumps([entry], indent=2), encoding="utf-8")


def _read_env(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    data: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.strip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def _write_env(path: Path, new_values: Dict[str, str], *, allow_overwrite: bool) -> str:
    if not path.exists():
        lines = [f"{k}={v}" for k, v in new_values.items()]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return "created"

    if not allow_overwrite:
        existing = _read_env(path)
        merged = dict(existing)
        merged.update({k: v for k, v in new_values.items() if v})
        lines = [f"{k}={v}" for k, v in merged.items()]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return "merged"

    lines = [f"{k}={v}" for k, v in new_values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return "overwritten"


def _format_env_preview(values: Dict[str, str]) -> str:
    redacted = dict(values)
    if "X-API-TOKEN" in redacted:
        redacted["X-API-TOKEN"] = "********"
    lines = [f"{k}={v}" for k, v in redacted.items()]
    return "\n".join(lines)


def _update_gitignore(path: Path, patterns: List[str]) -> str:
    if not path.exists():
        path.write_text("\n".join(patterns) + "\n", encoding="utf-8")
        return "created"

    existing = set(path.read_text(encoding="utf-8").splitlines())
    missing = [p for p in patterns if p not in existing]
    if not missing:
        return "unchanged"
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
        handle.write("\n".join(missing))
        handle.write("\n")
    return "updated"


def _ensure_dirs_maybe(root: Path, *, dry_run: bool) -> List[Path]:
    if dry_run:
        return []
    return _ensure_dirs(root)


def _write_env_maybe(
    path: Path,
    new_values: Dict[str, str],
    *,
    allow_overwrite: bool,
    dry_run: bool,
) -> str:
    if dry_run:
        return "dry-run"
    return _write_env(path, new_values, allow_overwrite=allow_overwrite)


def _update_gitignore_maybe(path: Path, patterns: List[str], *, dry_run: bool) -> str:
    if dry_run:
        return "dry-run"
    return _update_gitignore(path, patterns)


def _validate_credentials(env_path: Path) -> bool:
    try:
        from .config import load_env
        from .api_push import send_api_request
        from .rich_support import rich_status

        env = load_env(env_path)
        base_url = (env.get("QUALTRICS_BASE_URL") or "").strip()
        api_token = (
            env.get("X-API-TOKEN") or env.get("QUALTRICS_API_KEY") or ""
        ).strip()
        if not base_url or not api_token:
            print("Missing QUALTRICS_BASE_URL or API token; skip validation.")
            print("Next: set values in .env or run `qsync onboard` again.")
            return False
        with rich_status("Validating credentials..."):
            resp = send_api_request(
                action="qsync.onboard.whoami",
                method="GET",
                base_url=base_url,
                headers={"Accept": "application/json", "X-API-TOKEN": api_token},
                path="whoami",
                log_event=False,
                timeout=15,
            )
        result = resp.json().get("result", {}) or {}
        datacenter = (result.get("datacenter") or "").strip()
        if datacenter:
            print(f"✓ Credentials validated. Datacenter: {datacenter}")
        else:
            print("✓ Credentials validated.")
        return True
    except Exception as exc:
        print(f"✗ Credential validation failed: {exc}")
        print("Next: verify QUALTRICS_BASE_URL and API token, then retry.")
        return False


def _inventory_count(root: Path) -> int | None:
    try:
        from .survey_inventory import INVENTORY_CSV, LEGACY_SURVEY_CACHE

        inventory_path = (
            INVENTORY_CSV
            if INVENTORY_CSV.exists()
            else (LEGACY_SURVEY_CACHE if LEGACY_SURVEY_CACHE.exists() else None)
        )
        if not inventory_path or not inventory_path.exists():
            return None
        return max(0, len(inventory_path.read_text(encoding="utf-8").splitlines()) - 1)
    except Exception:
        return None


def _run_inventory(root: Path, *, dry_run: bool) -> bool:
    from .cli_survey import handle_inventory

    class _Args:
        pass

    args = _Args()
    args.command = "survey"
    args.survey_command = "inventory"
    args.root = root
    args.env_path = None
    args.dry_run = dry_run
    args.survey_ids = None
    args.counts_scope = None
    args.progress_only = True
    args.quiet = True

    try:
        handle_inventory(args)
        count = _inventory_count(root)
        if count is not None:
            print(f"Inventory rows: {count}")
        return True
    except Exception as exc:
        print(f"[qsync] Inventory fetch failed: {exc}")
        return False


def _select_focal_surveys(
    root: Path, *, dry_run: bool, interactive: bool = True
) -> bool:
    if not interactive:
        print("[qsync] Skipping focal selection in non-interactive mode.")
        return False
    try:
        from .survey_inventory import load_cached_inventory_records, persist_surveys
    except Exception:
        print("[qsync] Inventory helpers unavailable.")
        return False

    print("Focal surveys are the subset you plan to work on most frequently.")
    print("They power default batch operations and focal-only workflows.")

    records_map = load_cached_inventory_records()
    if not records_map:
        print("[qsync] No inventory found. Run `qsync survey inventory` first.")
        return False

    records = list(records_map.values())
    records.sort(key=lambda r: (r.get("lastModified") or ""), reverse=True)

    def _is_focal(record: dict) -> bool:
        raw = record.get("focal")
        if isinstance(raw, bool):
            return raw
        if raw is None:
            return False
        if isinstance(raw, (int, float)):
            return bool(raw)
        text = str(raw).strip().lower()
        return text in {"1", "true", "t", "yes", "y"}

    if not (should_use_questionary() and is_interactive()):
        raw = input(
            "Enter focal survey IDs (comma-separated), or blank to skip: "
        ).strip()
        if not raw:
            return False
        chosen_ids = {token.strip() for token in raw.split(",") if token.strip()}
    else:
        import questionary

        top10 = records[:10]
        menu = questionary.select(
            "Select focal surveys",
            choices=[
                "✓ Choose from top 10 (most recent)",
                "✓ Show all surveys",
                "✓ Search/filter",
                "✗ Skip",
            ],
            style=CUSTOM_STYLE,
        ).ask()
        if not menu or "Skip" in menu:
            return False

        def _choices_from(items: list[dict]) -> list[questionary.Choice]:
            choices: list[questionary.Choice] = []
            for record in items:
                sid = record.get("id")
                if not sid:
                    continue
                name = record.get("name") or "Untitled"
                focal_tag = " (focal)" if _is_focal(record) else ""
                title = f"{sid} - {name}{focal_tag}"
                choices.append(questionary.Choice(title=title, value=sid))
            return choices

        target_records = records
        if "top 10" in menu.lower():
            target_records = top10
        elif "search" in menu.lower():
            query = questionary.text(
                "Search by name or ID:",
                instruction='Example: SV_123 or "customer"',
                style=CUSTOM_STYLE,
            ).ask()
            if query:
                q = query.strip().lower()
                target_records = [
                    r
                    for r in records
                    if q in (r.get("id") or "").lower()
                    or q in (r.get("name") or "").lower()
                ]
            else:
                target_records = records

        if not target_records:
            print("[qsync] No surveys matched your filter.")
            return False

        default_ids = [
            r.get("id") for r in target_records if r.get("id") and _is_focal(r)
        ]
        choice_items = _choices_from(target_records)
        available_defaults = set()
        for choice in choice_items:
            value = getattr(choice, "value", choice)
            available_defaults.add(value)
        default_ids = [d for d in default_ids if d in available_defaults]
        try:
            chosen = questionary.checkbox(
                "Mark focal surveys (space to toggle):",
                choices=choice_items,
                default=default_ids,
                style=CUSTOM_STYLE,
            ).ask()
        except ValueError:
            chosen = questionary.checkbox(
                "Mark focal surveys (space to toggle):",
                choices=choice_items,
                style=CUSTOM_STYLE,
            ).ask()
        if not chosen:
            return False
        chosen_ids = set(chosen)

    if dry_run:
        print(f"[DRY RUN] Would mark {len(chosen_ids)} surveys as focal.")
        return True

    for record in records:
        sid = record.get("id")
        if not sid:
            continue
        record["focal"] = sid in chosen_ids
    persist_surveys(records, current_user_id=None)
    print(f"Updated focal surveys: {len(chosen_ids)} selected.")
    return True


def run_onboard(args) -> None:
    try:
        non_interactive = getattr(args, "non_interactive", False)
        dry_run = getattr(args, "dry_run", False)
        if non_interactive:
            _run_non_interactive(args)
            return
        if not _preflight_checklist():
            print("Setup cancelled.")
            return

        default_root = Path(getattr(args, "root", None) or Path.cwd())
        if getattr(args, "resume", False):
            resume_state = _load_state(default_root)
            resumed_root = resume_state.get("root")
            if resumed_root:
                default_root = Path(str(resumed_root))
                print(f"Resuming onboarding from {default_root}")
            elif not resume_state:
                print("No onboarding state found; starting fresh.")

        existing = _detect_existing_workspace(default_root)
        if any(existing.values()):
            _step_banner(1, 8, "Existing workspace detected")
            print("Detected existing workspace artifacts:")
            for key, present in existing.items():
                status = "found" if present else "missing"
                print(f"- {key}: {status}")
            if is_interactive():
                mode_choices = [
                    "Repair/merge existing workspace (recommended)",
                    "Fresh start (archive existing artifacts)",
                    "Fresh start (delete existing artifacts)",
                    "Exit",
                ]
                choice = select_from_list(
                    "How would you like to proceed?",
                    mode_choices,
                    default=mode_choices[0],
                )
                if choice is None or choice == "Exit":
                    print("Setup cancelled.")
                    return
                if "archive" in choice.lower():
                    print("Archiving existing workspace artifacts...")
                    archive_dir = _archive_workspace_artifacts(default_root, existing)
                    if archive_dir:
                        print(f"Archive created: {archive_dir}")
                    existing = _detect_existing_workspace(default_root)
                elif "delete" in choice.lower():
                    print(
                        "This will permanently delete the workspace artifacts listed above."
                    )
                    if not confirm("Proceed with delete?", default=False):
                        print("Setup cancelled.")
                        return
                    _delete_workspace_artifacts(default_root, existing)
                    existing = _detect_existing_workspace(default_root)
            else:
                print("Proceeding with repair/merge mode (default).")

        state = {
            "root": None,
            "folders": False,
            "creds": False,
            "env": False,
            "gitignore": False,
            "inventory": False,
            "focal": False,
            "translations": False,
            "fasttext": False,
            "validated": False,
        }
        # Always attempt to load existing state if present
        loaded = _load_state(default_root)
        if loaded:
            for key in state.keys():
                if key in loaded:
                    state[key] = loaded[key]
            if loaded.get("root"):
                state["root"] = loaded["root"]
        total_steps = 8

        while True:
            task_items = [
                ("Workspace root", state["root"] is not None),
                ("Create folders", state["folders"]),
                ("Credentials + .env", state["env"]),
                ("Gitignore", state["gitignore"]),
                ("Inventory (optional)", state["inventory"]),
                ("Focal surveys (optional)", state["focal"]),
                ("Translations + fasttext (optional)", state["fasttext"]),
            ]
            unfinished = [
                _menu_choice_text(label, done) for label, done in task_items if not done
            ]
            finished = [
                _menu_choice_text(label, done) for label, done in task_items if done
            ]
            choices = []
            if unfinished:
                choices.extend(unfinished)
            if finished:
                choices.append("─" * 40)
                choices.extend(finished)
            choices.append("─" * 40)
            choices.append("Finish")
            choices.append("Exit")
            default_choice = unfinished[0] if unfinished else None
            choice = select_from_list(
                "Onboarding steps",
                choices,
                default=default_choice,
            )
            if choice is None or choice == "Exit":
                print("Setup incomplete. You can re-run `qsync onboard` anytime.")
                return
            if choice == "Finish":
                break

            if "Workspace root" in choice:
                _step_banner(1, total_steps, "Workspace root")
                # Best-effort git root detection
                git_root = None
                try:
                    import subprocess

                    result = subprocess.run(
                        ["git", "rev-parse", "--show-toplevel"],
                        cwd=str(default_root),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        text=True,
                        check=False,
                    )
                    if result.returncode == 0:
                        git_root = Path(result.stdout.strip())
                except Exception:
                    git_root = None

                suggested_root = git_root or default_root
                state["root"] = _pick_root(suggested_root)
                os.environ["QSYNC_ROOT"] = str(state["root"])
                existing = _detect_existing_workspace(Path(state["root"]))
                if any(existing.values()):
                    print("Detected existing workspace artifacts:")
                    for key, present in existing.items():
                        status = "found" if present else "missing"
                        print(f"- {key}: {status}")
                _save_state(Path(state["root"]), state)
                continue

            root = Path(state["root"] or Path.cwd())
            os.environ["QSYNC_ROOT"] = str(root)

            if "Create folders" in choice:
                _step_banner(2, total_steps, "Create folders")
                created = _ensure_dirs_maybe(root, dry_run=dry_run)
                state["folders"] = True
                created_set = {str(p) for p in created}
                print("Folder checklist:")
                for d in [
                    root / "surveys",
                    root / "surveys" / "pending",
                    root / "excel",
                    root / "survey_js" / "core",
                    root / "contents" / "qualtrics_library_messages",
                    root / "contents" / "qualtrics_survey_translations",
                    root / "logs",
                    root / "export",
                    root / "responses",
                    root / "tmp",
                ]:
                    status = "created" if str(d) in created_set else "exists"
                    if dry_run and not d.exists():
                        status = "would create"
                    print(f"- {d}: {status}")
                _save_state(root, state)
                continue

            if "Credentials + .env" in choice:
                _step_banner(3, total_steps, "Credentials + .env")
                datacenter, token = _collect_credentials()
                if datacenter and token:
                    env_path = root / ".env"
                    allow_overwrite = False
                    if (
                        env_path.exists()
                        and should_use_questionary()
                        and is_interactive()
                    ):
                        allow_overwrite = confirm(
                            f"{env_path} exists. Overwrite? (No = merge)",
                            default=False,
                        )
                    print("About to write .env with:")
                    print(
                        _format_env_preview(
                            {"QUALTRICS_BASE_URL": datacenter, "X-API-TOKEN": token}
                        )
                    )
                    print(
                        "Security note: .env contains secrets. Ensure it is gitignored."
                    )
                    if should_use_questionary() and is_interactive():
                        if not confirm("Proceed with writing .env?", default=True):
                            print("Skipped .env write.")
                            continue
                    result = _write_env_maybe(
                        env_path,
                        {"QUALTRICS_BASE_URL": datacenter, "X-API-TOKEN": token},
                        allow_overwrite=allow_overwrite,
                        dry_run=dry_run,
                    )
                    state["creds"] = True
                    state["env"] = True
                    print(f".env {result}.")
                    if should_use_questionary() and is_interactive():
                        if confirm(
                            "Validate credentials now? (network)", default=False
                        ):
                            state["validated"] = _validate_credentials(env_path)
                else:
                    print("Skipped credentials (missing input).")
                _save_state(root, state)
                continue

            if "Gitignore" in choice:
                _step_banner(4, total_steps, ".gitignore")
                if getattr(args, "skip_gitignore", False):
                    print("Skipped gitignore.")
                    _save_state(root, state)
                    continue
                gitignore = root / ".gitignore"
                patterns = [
                    ".env",
                    "surveys/",
                    "excel/",
                    "survey_js/",
                    "contents/",
                    "logs/",
                    "export/",
                    "responses/",
                    "tmp/",
                ]
                result = _update_gitignore_maybe(gitignore, patterns, dry_run=dry_run)
                state["gitignore"] = True
                print(f".gitignore {result}.")
                git_dir = root / ".git"
                env_path = root / ".env"
                if git_dir.exists() and env_path.exists():
                    try:
                        import subprocess

                        tracked = (
                            subprocess.run(
                                [
                                    "git",
                                    "-C",
                                    str(root),
                                    "ls-files",
                                    "--error-unmatch",
                                    ".env",
                                ],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                check=False,
                            ).returncode
                            == 0
                        )
                    except Exception:
                        tracked = False
                    if tracked:
                        print("Warning: .env is tracked by git.")
                        print("Suggested fix: git rm --cached .env")
                _save_state(root, state)
                continue

            if "Inventory" in choice:
                _step_banner(5, total_steps, "Inventory (optional)")
                state["inventory"] = _run_inventory(root, dry_run=dry_run)
                _save_state(root, state)
                continue

            if "Focal surveys" in choice:
                _step_banner(6, total_steps, "Focal surveys (optional)")
                state["focal"] = _select_focal_surveys(root, dry_run=dry_run)
                _save_state(root, state)
                continue

            if "Translations + fasttext" in choice:
                _step_banner(7, total_steps, "Translations + fasttext (optional)")
                prefs = _load_prefs(root)
                wants_translations = prefs.get("translations")
                if wants_translations is None:
                    wants_translations = confirm(
                        "Will you be working with survey translations?",
                        default=False,
                    )
                    prefs["translations"] = bool(wants_translations)
                    _save_prefs(root, prefs)
                state["translations"] = bool(wants_translations)
                if wants_translations:
                    print("FastText improves language detection.")
                    print("Model size: ~125MB (lid.176.ftz)")
                    wants_fasttext = confirm(
                        "Install fasttext + download model now?",
                        default=False,
                    )
                    if wants_fasttext:
                        try:
                            from rich.console import Console
                            from .cli_translations_check import _ensure_fasttext_setup

                            _ensure_fasttext_setup(True, root, Console())
                            state["fasttext"] = True
                        except Exception as exc:
                            print(f"Fasttext setup failed: {exc}")
                _save_state(root, state)
                continue
        _print_summary(state)
        _finalize_stage2(args, root=Path(state["root"] or Path.cwd()), state=state)
    except KeyboardInterrupt:
        print("\nSetup incomplete. You can re-run `qsync onboard` anytime.")
        return


def _print_summary(state: Dict[str, object]) -> None:
    _print_header("Setup complete")
    root = state.get("root") or Path.cwd()
    print(f"Workspace root: {root}")
    print(f"Folders created: {'yes' if state.get('folders') else 'no'}")
    print(f".env written: {'yes' if state.get('env') else 'no'}")
    print(f".gitignore updated: {'yes' if state.get('gitignore') else 'no'}")
    if state.get("inventory"):
        print("Inventory fetched: yes")
    if state.get("focal"):
        print("Focal surveys selected: yes")
    if state.get("validated"):
        print("Credentials validated: yes")
    print()
    print("Next steps:")
    print("- qsync doctor")
    print("- qsync survey inventory")


def _finalize_stage2(args, root: Path, state: Dict[str, object]) -> None:
    dry_run = getattr(args, "dry_run", False)
    os.environ["QSYNC_ROOT"] = str(root)

    if getattr(args, "with_inventory", False) and not state.get("inventory"):
        state["inventory"] = _run_inventory(root, dry_run=dry_run)

    if getattr(args, "with_focal", False) and not state.get("focal"):
        state["focal"] = _select_focal_surveys(root, dry_run=dry_run)

    if getattr(args, "with_fasttext", False) and not state.get("fasttext"):
        try:
            from rich.console import Console
            from .cli_translations_check import _ensure_fasttext_setup

            _ensure_fasttext_setup(True, root, Console())
            state["fasttext"] = True
        except Exception as exc:
            print(f"Fasttext setup failed: {exc}")

    if should_use_questionary() and is_interactive():
        if not state.get("inventory"):
            if confirm("Fetch inventory now? (network)", default=False):
                state["inventory"] = _run_inventory(root, dry_run=dry_run)
        if state.get("inventory") and not state.get("focal"):
            if confirm("Select focal surveys now?", default=False):
                state["focal"] = _select_focal_surveys(root, dry_run=dry_run)

        prefs = _load_prefs(root)
        if prefs.get("translations") is None:
            wants_translations = confirm(
                "Will you be working with survey translations?",
                default=False,
            )
            prefs["translations"] = bool(wants_translations)
            _save_prefs(root, prefs)
            state["translations"] = bool(wants_translations)
            if wants_translations and not state.get("fasttext"):
                print("FastText improves language detection.")
                print("Model size: ~125MB (lid.176.ftz)")
                if confirm("Install fasttext + download model now?", default=False):
                    try:
                        from rich.console import Console
                        from .cli_translations_check import _ensure_fasttext_setup

                        _ensure_fasttext_setup(True, root, Console())
                        state["fasttext"] = True
                    except Exception as exc:
                        print(f"Fasttext setup failed: {exc}")

        if confirm("Run `qsync doctor` now?", default=False):
            env_path = root / ".env"
            state["validated"] = _validate_credentials(env_path)

    _save_state(root, state)
    _append_setup_log(
        root,
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "root": str(root),
            "folders": bool(state.get("folders")),
            "env": bool(state.get("env")),
            "gitignore": bool(state.get("gitignore")),
            "inventory": bool(state.get("inventory")),
            "focal": bool(state.get("focal")),
            "translations": bool(state.get("translations")),
            "fasttext": bool(state.get("fasttext")),
            "validated": bool(state.get("validated")),
            "dry_run": bool(getattr(args, "dry_run", False)),
        },
    )
    print()
    print("Stage 2 summary:")
    print(f"- Inventory fetched: {'yes' if state.get('inventory') else 'no'}")
    print(f"- Focal surveys selected: {'yes' if state.get('focal') else 'no'}")
    print(f"- Translations workflow: {'yes' if state.get('translations') else 'no'}")
    print(f"- Fasttext installed: {'yes' if state.get('fasttext') else 'no'}")
    if state.get("validated") is not None:
        print(f"- Credentials validated: {'yes' if state.get('validated') else 'no'}")


def _run_non_interactive(args) -> None:
    root = Path(getattr(args, "root", None) or Path.cwd())
    dry_run = getattr(args, "dry_run", False)
    _ensure_dirs_maybe(root, dry_run=dry_run)
    datacenter = getattr(args, "datacenter", None)
    token = getattr(args, "token", None)
    if datacenter and token:
        print("Security note: .env contains secrets. Ensure it is gitignored.")
        _write_env_maybe(
            root / ".env",
            {"QUALTRICS_BASE_URL": datacenter, "X-API-TOKEN": token},
            allow_overwrite=False,
            dry_run=dry_run,
        )
    if not getattr(args, "skip_gitignore", False):
        _update_gitignore_maybe(
            root / ".gitignore",
            [
                ".env",
                "surveys/",
                "excel/",
                "survey_js/",
                "contents/",
                "logs/",
                "export/",
                "responses/",
                "tmp/",
            ],
            dry_run=dry_run,
        )
    _print_summary(
        {
            "root": root,
            "folders": True,
            "env": bool(datacenter and token),
            "gitignore": not getattr(args, "skip_gitignore", False),
        }
    )

    if getattr(args, "with_inventory", False):
        _run_inventory(root, dry_run=dry_run)
    if getattr(args, "with_focal", False):
        _select_focal_surveys(root, dry_run=dry_run, interactive=False)
    if getattr(args, "with_fasttext", False):
        try:
            from .cli_translations_check import (
                _download_fasttext_model,
                _install_fasttext_module,
            )

            ok, err = _install_fasttext_module()
            if not ok:
                print(f"fasttext install failed: {err}")
            else:
                model_path = root / "models" / "lid.176.ftz"
                if not model_path.exists():
                    ok, err = _download_fasttext_model(model_path)
                    if not ok:
                        print(f"fasttext model download failed: {err}")
        except Exception as exc:
            print(f"Fasttext setup failed: {exc}")

    state = {
        "root": str(root),
        "folders": True,
        "creds": bool(datacenter and token),
        "env": bool(datacenter and token),
        "gitignore": not getattr(args, "skip_gitignore", False),
        "inventory": bool(getattr(args, "with_inventory", False)),
        "focal": False,
        "translations": False,
        "fasttext": bool(getattr(args, "with_fasttext", False)),
        "validated": False,
    }
    _save_state(root, state)
    _append_setup_log(
        root,
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "root": str(root),
            "folders": True,
            "env": bool(datacenter and token),
            "gitignore": not getattr(args, "skip_gitignore", False),
            "inventory": bool(getattr(args, "with_inventory", False)),
            "focal": False,
            "translations": False,
            "fasttext": bool(getattr(args, "with_fasttext", False)),
            "validated": False,
            "dry_run": bool(getattr(args, "dry_run", False)),
        },
    )
