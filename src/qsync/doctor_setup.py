from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import (
    WORKSPACE_LAYOUT_ACCOUNT_ROOT_V1,
    WORKSPACE_LAYOUT_LEGACY,
    resolve_root,
    validate_account_name,
)
from .workspace_prefs import load_prefs, save_prefs

SURFACE_NAMES: tuple[str, ...] = (
    "surveys",
    "excel",
    "survey_js",
    "contents",
    "export",
    "responses",
    "tmp",
)
MIGRATIONS_DIRNAME = "migrations"
LOCKS_DIRNAME = "locks"
LOCK_FILENAME = "doctor-setup.lock"


@dataclass(frozen=True)
class SetupMove:
    src: Path
    dst: Path
    account: str
    surface: str

    def to_dict(self) -> dict[str, str]:
        return {
            "src": str(self.src),
            "dst": str(self.dst),
            "account": self.account,
            "surface": self.surface,
        }


def _state_dir(root: Path) -> Path:
    return (root / ".qsync").resolve()


def _migrations_dir(root: Path) -> Path:
    return (_state_dir(root) / MIGRATIONS_DIRNAME).resolve()


def _locks_dir(root: Path) -> Path:
    return (_state_dir(root) / LOCKS_DIRNAME).resolve()


def _lock_path(root: Path) -> Path:
    return (_locks_dir(root) / LOCK_FILENAME).resolve()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def _is_legacy_account_dot_dir(path: Path) -> bool:
    name = path.name
    if not path.is_dir() or not name.startswith("."):
        return False
    raw = name[1:].strip()
    if not raw:
        return False
    try:
        validate_account_name(raw)
    except Exception:
        return False
    return True


def _discover_accounts(root: Path) -> list[str]:
    names: set[str] = set()

    for env_file in root.glob(".env.*"):
        raw = env_file.name[len(".env.") :].strip()
        if not raw:
            continue
        try:
            names.add(validate_account_name(raw))
        except Exception:
            continue

    for surface in SURFACE_NAMES:
        base = (root / surface).resolve()
        if not base.exists() or not base.is_dir():
            continue
        for child in base.iterdir():
            if _is_legacy_account_dot_dir(child):
                names.add(validate_account_name(child.name[1:]))

    prefs, _err = load_prefs(root)
    raw_active = prefs.get("active_account")
    if isinstance(raw_active, str) and raw_active.strip():
        try:
            names.add(validate_account_name(raw_active.strip()))
        except Exception:
            pass

    names.discard("default")
    return sorted(names)


def _resolve_target_accounts(root: Path, target: str) -> list[str]:
    raw = (target or "all").strip().lower()
    if raw in {"", "all"}:
        discovered = _discover_accounts(root)
        return ["default", *discovered]
    if raw == "default":
        return ["default"]
    return [validate_account_name(raw)]


def _legacy_source_dir(root: Path, *, surface: str, account: str) -> Path:
    base = (root / surface).resolve()
    if account == "default":
        return base
    return (base / f".{account}").resolve()


def _account_root(root: Path, account: str) -> Path:
    return (root / "accounts" / account).resolve()


def _destination_for(
    *,
    root: Path,
    surface: str,
    account: str,
    child: Path,
) -> Path | None:
    account_root = _account_root(root, account)
    name = child.name

    if surface == "surveys":
        if account == "default" and _is_legacy_account_dot_dir(child):
            return None
        # Keep shared Survey Master mapping at workspace root.
        if account == "default" and name == "qualtrics_api_key_mapping.csv":
            return None
        return account_root / name

    if surface == "excel":
        if account == "default" and _is_legacy_account_dot_dir(child):
            return None
        return account_root / "excel" / name

    if surface == "survey_js":
        if account == "default" and _is_legacy_account_dot_dir(child):
            return None
        if name == "survey_qid_js_map.csv":
            return account_root / "js" / name
        return account_root / "js" / name

    if surface == "contents":
        if account == "default" and _is_legacy_account_dot_dir(child):
            return None
        if name == "qualtrics_survey_translations":
            return account_root / "translations"
        if name == "qualtrics_library_messages":
            return account_root / "library_messages"
        return account_root / "contents" / name

    if surface == "export":
        if account == "default" and _is_legacy_account_dot_dir(child):
            return None
        return account_root / "derived" / "export" / name

    if surface == "responses":
        if account == "default" and _is_legacy_account_dot_dir(child):
            return None
        return account_root / "derived" / "responses" / name

    if surface == "tmp":
        if account == "default" and _is_legacy_account_dot_dir(child):
            return None
        return account_root / "state" / "tmp" / name

    return account_root / surface / name


def build_setup_moves(root: Path, *, target_account: str) -> list[SetupMove]:
    accounts = _resolve_target_accounts(root, target_account)
    moves: list[SetupMove] = []

    for account in accounts:
        for surface in SURFACE_NAMES:
            src_dir = _legacy_source_dir(root, surface=surface, account=account)
            if not src_dir.exists() or not src_dir.is_dir():
                continue
            for child in sorted(src_dir.iterdir(), key=lambda p: p.name):
                dst = _destination_for(
                    root=root,
                    surface=surface,
                    account=account,
                    child=child,
                )
                if dst is None:
                    continue
                if child.resolve() == dst.resolve():
                    continue
                moves.append(
                    SetupMove(
                        src=child.resolve(),
                        dst=dst.resolve(),
                        account=account,
                        surface=surface,
                    )
                )

    # Deterministic order: shortest sources first then lexical.
    moves.sort(key=lambda m: (len(str(m.src)), str(m.src), str(m.dst)))
    return moves


def _acquire_lock(root: Path) -> None:
    path = _lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise SystemExit(
            f"[qsync:doctor:setup] ERROR: setup lock exists at {path}. "
            "If no migration is running, remove the lock file and retry."
        )
    try:
        payload = {"pid": os.getpid(), "created_at": _now_iso(), "command": "doctor setup"}
        os.write(fd, (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def _release_lock(root: Path) -> None:
    try:
        _lock_path(root).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _confirm_apply(label: str) -> None:
    if not sys.stdin.isatty():
        raise SystemExit(
            f"[qsync:doctor:setup] Confirmation required for {label}. Re-run with --yes."
        )
    typed = input(f"Type '{label}' to continue: ").strip()
    if typed != label:
        raise SystemExit("[qsync:doctor:setup] Aborted.")


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"[qsync:doctor:setup] ERROR: failed to read manifest {path}: {exc}")
    if not isinstance(payload, dict):
        raise SystemExit(f"[qsync:doctor:setup] ERROR: invalid manifest payload in {path}")
    return payload


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _apply_moves(
    moves: list[SetupMove],
) -> tuple[list[SetupMove], list[SetupMove], list[dict[str, str]]]:
    moved: list[SetupMove] = []
    conflicts: list[SetupMove] = []
    errors: list[dict[str, str]] = []

    for move in moves:
        try:
            if not move.src.exists():
                continue
            if move.dst.exists():
                conflicts.append(move)
                continue
            move.dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(move.src), str(move.dst))
            moved.append(move)
        except Exception as exc:  # noqa: BLE001
            errors.append(
                {
                    "src": str(move.src),
                    "dst": str(move.dst),
                    "error": str(exc),
                }
            )
    return moved, conflicts, errors


def _restore_preferences(root: Path, prefs: dict[str, Any]) -> None:
    save_prefs(root, dict(prefs))


def _load_current_prefs(root: Path) -> dict[str, Any]:
    prefs, _err = load_prefs(root)
    return dict(prefs)


def _write_active_account_file(root: Path, active_account: str) -> None:
    active = str(active_account or "").strip() or "default"
    state_dir = _state_dir(root)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "active_account.txt").write_text(active + "\n", encoding="utf-8")


def _planned_post_migration_preferences(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    before = _load_current_prefs(root)
    after = dict(before)
    after["workspace_layout"] = WORKSPACE_LAYOUT_ACCOUNT_ROOT_V1
    after["survey_cache_subdir"] = "cache"
    return before, after


def _apply_post_migration_preferences(
    root: Path,
    *,
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    try:
        save_prefs(root, dict(after))
        _write_active_account_file(root, str(after.get("active_account") or "default"))
        return dict(after)
    except Exception:
        # Best-effort rollback: if post-migration preference writes fail, keep the
        # workspace in its original preference state.
        _restore_preferences(root, before)
        _write_active_account_file(root, str(before.get("active_account") or "default"))
        raise


def _set_post_undo_preferences(root: Path, expected: dict[str, Any]) -> None:
    _restore_preferences(root, expected)
    active = str(expected.get("active_account") or "").strip() or "default"
    _write_active_account_file(root, active)


def _manifest_payload(
    *,
    root: Path,
    target_account: str,
    moves: list[SetupMove],
    moved: list[SetupMove],
    conflicts: list[SetupMove],
    errors: list[dict[str, str]],
    preferences_before: dict[str, Any],
    preferences_after: dict[str, Any],
) -> dict[str, Any]:
    return {
        "version": 1,
        "created_at": _now_iso(),
        "workspace_root": str(root),
        "target_account": target_account,
        "layout_from": preferences_before.get("workspace_layout", WORKSPACE_LAYOUT_LEGACY),
        "layout_to": preferences_after.get("workspace_layout", WORKSPACE_LAYOUT_ACCOUNT_ROOT_V1),
        "planned_moves": [m.to_dict() for m in moves],
        "moved": [m.to_dict() for m in moved],
        "conflicts": [m.to_dict() for m in conflicts],
        "errors": errors,
        "preferences_before": preferences_before,
        "preferences_after": preferences_after,
    }


def _undo_manifest_payload(
    *,
    forward_manifest_path: Path,
    forward_payload: dict[str, Any],
) -> dict[str, Any]:
    moved_entries = forward_payload.get("moved") or []
    reverse_actions: list[dict[str, str]] = []
    for entry in moved_entries:
        if not isinstance(entry, dict):
            continue
        src = str(entry.get("src") or "").strip()
        dst = str(entry.get("dst") or "").strip()
        if not src or not dst:
            continue
        reverse_actions.append(
            {
                "src": dst,
                "dst": src,
                "account": str(entry.get("account") or ""),
                "surface": str(entry.get("surface") or ""),
            }
        )
    return {
        "version": 1,
        "created_at": _now_iso(),
        "workspace_root": forward_payload.get("workspace_root"),
        "undo_of": str(forward_manifest_path),
        "layout_from": forward_payload.get("layout_to", WORKSPACE_LAYOUT_ACCOUNT_ROOT_V1),
        "layout_to": forward_payload.get("layout_from", WORKSPACE_LAYOUT_LEGACY),
        "actions": reverse_actions,
        "preferences_before": forward_payload.get("preferences_after", {}),
        "preferences_after": forward_payload.get("preferences_before", {}),
    }


def _apply_undo_actions(actions: list[SetupMove]) -> tuple[int, int, list[dict[str, str]]]:
    restored = 0
    conflicts = 0
    errors: list[dict[str, str]] = []
    for action in actions:
        try:
            if not action.src.exists():
                continue
            if action.dst.exists():
                conflicts += 1
                continue
            action.dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(action.src), str(action.dst))
            restored += 1
        except Exception as exc:  # noqa: BLE001
            errors.append({"src": str(action.src), "dst": str(action.dst), "error": str(exc)})
    return restored, conflicts, errors


def _parse_undo_actions(payload: dict[str, Any]) -> list[SetupMove]:
    raw_actions = payload.get("actions")
    if not isinstance(raw_actions, list):
        return []
    actions: list[SetupMove] = []
    for item in raw_actions:
        if not isinstance(item, dict):
            continue
        src = Path(str(item.get("src") or "")).resolve()
        dst = Path(str(item.get("dst") or "")).resolve()
        if not str(src) or not str(dst):
            continue
        actions.append(
            SetupMove(
                src=src,
                dst=dst,
                account=str(item.get("account") or ""),
                surface=str(item.get("surface") or ""),
            )
        )
    return actions


def _print_plan(root: Path, moves: list[SetupMove], *, target_account: str, apply: bool) -> None:
    print("[qsync:doctor:setup]")
    print(f"  root:           {root}")
    print(f"  target_account: {target_account}")
    print(f"  mode:           {'apply' if apply else 'dry-run'}")
    print(f"  planned_moves:  {len(moves)}")
    for move in moves:
        print(f"    - {move.src} -> {move.dst}")


def _handle_apply(
    *,
    root: Path,
    target_account: str,
    moves: list[SetupMove],
    json_mode: bool,
    yes: bool,
) -> None:
    if not yes:
        _confirm_apply("apply")

    _acquire_lock(root)
    try:
        prefs_before, prefs_target = _planned_post_migration_preferences(root)
        moved, conflicts, errors = _apply_moves(moves)
        prefs_after = dict(prefs_before)

        if not errors:
            try:
                prefs_after = _apply_post_migration_preferences(
                    root,
                    before=prefs_before,
                    after=prefs_target,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    {
                        "src": "<preferences>",
                        "dst": str(_state_dir(root)),
                        "error": str(exc),
                    }
                )

        stamp = _stamp()
        migrations_dir = _migrations_dir(root)
        manifest_path = migrations_dir / f"{stamp}_manifest.json"
        undo_manifest_path = migrations_dir / f"{stamp}_undo_manifest.json"

        manifest_payload = _manifest_payload(
            root=root,
            target_account=target_account,
            moves=moves,
            moved=moved,
            conflicts=conflicts,
            errors=errors,
            preferences_before=prefs_before,
            preferences_after=prefs_after,
        )
        _write_manifest(manifest_path, manifest_payload)

        undo_payload = _undo_manifest_payload(
            forward_manifest_path=manifest_path,
            forward_payload=manifest_payload,
        )
        _write_manifest(undo_manifest_path, undo_payload)

        if json_mode:
            print(
                json.dumps(
                    {
                        "ok": not errors,
                        "mode": "apply",
                        "root": str(root),
                        "target_account": target_account,
                        "planned_moves": len(moves),
                        "moved": len(moved),
                        "conflicts": len(conflicts),
                        "errors": errors,
                        "manifest": str(manifest_path),
                        "undo_manifest": str(undo_manifest_path),
                    },
                    ensure_ascii=False,
                )
            )
            if errors:
                raise SystemExit(1)
            return

        print(f"  moved:          {len(moved)}")
        print(f"  conflicts:      {len(conflicts)}")
        print(f"  errors:         {len(errors)}")
        print(f"  manifest:       {manifest_path}")
        print(f"  undo_manifest:  {undo_manifest_path}")
        if conflicts:
            print("  note: destination conflicts were skipped.")
        if errors:
            raise SystemExit("[qsync:doctor:setup] Completed with errors.")
    finally:
        _release_lock(root)


def _handle_undo(
    *,
    root: Path,
    undo_manifest_path: Path,
    json_mode: bool,
    yes: bool,
) -> None:
    payload = _read_manifest(undo_manifest_path)
    actions = _parse_undo_actions(payload)

    if not yes:
        _confirm_apply("undo")

    _acquire_lock(root)
    try:
        restored, conflicts, errors = _apply_undo_actions(actions)
        expected_prefs = payload.get("preferences_after")
        if isinstance(expected_prefs, dict):
            _set_post_undo_preferences(root, expected_prefs)

        if json_mode:
            print(
                json.dumps(
                    {
                        "ok": not errors,
                        "mode": "undo",
                        "root": str(root),
                        "undo_manifest": str(undo_manifest_path),
                        "actions": len(actions),
                        "restored": restored,
                        "conflicts": conflicts,
                        "errors": errors,
                    },
                    ensure_ascii=False,
                )
            )
            if errors:
                raise SystemExit(1)
            return

        print("[qsync:doctor:setup]")
        print(f"  mode:           undo")
        print(f"  undo_manifest:  {undo_manifest_path}")
        print(f"  actions:        {len(actions)}")
        print(f"  restored:       {restored}")
        print(f"  conflicts:      {conflicts}")
        print(f"  errors:         {len(errors)}")
        if errors:
            raise SystemExit("[qsync:doctor:setup] Undo completed with errors.")
    finally:
        _release_lock(root)


def handle_doctor_setup(args) -> None:
    root = resolve_root(required=False) or Path.cwd()
    target_account = str(getattr(args, "target_account", "all") or "all").strip()
    apply_mode = bool(getattr(args, "apply", False))
    undo_raw = getattr(args, "undo", None)
    undo_manifest = Path(undo_raw).expanduser().resolve() if undo_raw else None
    json_mode = bool(getattr(args, "json", False))
    yes = bool(getattr(args, "yes", False))

    if apply_mode and undo_manifest:
        raise SystemExit("[qsync:doctor:setup] ERROR: choose either --apply or --undo.")

    if undo_manifest:
        _handle_undo(
            root=root,
            undo_manifest_path=undo_manifest,
            json_mode=json_mode,
            yes=yes,
        )
        return

    moves = build_setup_moves(root, target_account=target_account)

    if json_mode and not apply_mode:
        print(
            json.dumps(
                {
                    "ok": True,
                    "mode": "dry-run",
                    "root": str(root),
                    "target_account": target_account,
                    "planned_moves": len(moves),
                    "moves": [m.to_dict() for m in moves],
                },
                ensure_ascii=False,
            )
        )
        return

    if not json_mode:
        _print_plan(
            root=root,
            moves=moves,
            target_account=target_account,
            apply=apply_mode,
        )
        if not apply_mode:
            print("  next: run `qsync doctor setup --apply` to execute this plan.")

    if not apply_mode:
        return

    _handle_apply(
        root=root,
        target_account=target_account,
        moves=moves,
        json_mode=json_mode,
        yes=yes,
    )
