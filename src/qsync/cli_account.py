"""Account-related workspace commands for qsync.

`qsync account …` manages a workspace-default account selection that behaves
like an implicit `--account` for subsequent commands, without modifying the
user's shell environment.

The selection is stored in `<root>/.qsync/preferences.json` as `active_account`.
When a named account is active, qsync scopes most workspace artifacts under
`.<account>/` subdirectories (for example `surveys/.damian/`, `excel/.damian/`).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import (
    get_active_account,
    load_account_env,
    load_env,
    load_env_file,
    resolve_account_env_path,
    resolve_env_path,
    resolve_root,
    resolve_scoped_dir,
    resolve_survey_cache_base_dir,
    resolve_survey_cache_dir,
    resolve_survey_cache_subdir,
    validate_survey_cache_subdir,
    validate_account_name,
)
from .errors import QsyncConfigError
from .workspace_prefs import (
    get_workspace_active_account,
    get_workspace_survey_cache_subdir,
    load_prefs,
    prefs_path,
    set_workspace_active_account,
    set_workspace_survey_cache_subdir,
)
from .survey_naming import resolve_survey_path


def _root() -> Path:
    return resolve_root(required=False) or Path.cwd()


def _format_account_label(account: str | None) -> str:
    if not account or account.strip().lower() == "default":
        return "default (.env)"
    return account


def _load_default_env_for_root(root: Path) -> dict[str, str]:
    """Load the primary `.env` (or `--env-path`/`QSYNC_ENV_PATH`) explicitly.

    This bypasses account selection even if `QSYNC_ACCOUNT` is set.
    """

    env_path = resolve_env_path(root=root)
    return load_env(env_path)


def _bootstrap_default_account_env(root: Path) -> Path | None:
    """Best-effort create `.env.default` from primary `.env` credentials.

    This lets users explicitly target the primary account via `--account default`
    after switching to named accounts.
    """

    default_alias = resolve_account_env_path("default", root=root)
    if default_alias.exists():
        return None

    src_env = resolve_env_path(root=root) or (root / ".env")
    if not src_env.exists() or not src_env.is_file():
        return None

    file_env = load_env_file(src_env)
    base_url = (
        (file_env.get("QUALTRICS_BASE_URL") or "").strip()
        or (file_env.get("TARGET_QUALTRICS_BASE_URL") or "").strip()
    )
    api_token = (
        (file_env.get("X-API-TOKEN") or "").strip()
        or (file_env.get("QUALTRICS_API_KEY") or "").strip()
        or (file_env.get("TARGET_X-API-TOKEN") or "").strip()
        or (file_env.get("TARGET_QUALTRICS_API_KEY") or "").strip()
    )
    if not base_url or not api_token:
        return None

    default_alias.parent.mkdir(parents=True, exist_ok=True)
    default_alias.write_text(
        "\n".join(
            [
                f"QUALTRICS_BASE_URL={base_url}",
                f"X-API-TOKEN={api_token}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return default_alias


def _discover_env_accounts(root: Path) -> list[str]:
    accounts: list[str] = []
    for path in sorted(root.glob(".env.*")):
        if not path.is_file():
            continue
        if path.name in {".env.example", ".env.template"}:
            continue
        raw = path.name.split(".env.", 1)[-1].strip()
        if not raw or raw == path.name:
            continue
        if raw.lower() == "default":
            continue
        accounts.append(raw)
    return accounts


@dataclass(frozen=True)
class AccountListEntry:
    account: str
    env_path: Path
    ok: bool
    base_url: str | None = None
    error: str | None = None


def _build_account_list(root: Path) -> list[AccountListEntry]:
    entries: list[AccountListEntry] = []
    for raw in _discover_env_accounts(root):
        env_path = resolve_account_env_path(raw, root=root)
        try:
            env = load_account_env(raw, root=root)
            base = (env.get("QUALTRICS_BASE_URL") or "").strip() or None
            entries.append(
                AccountListEntry(
                    account=validate_account_name(raw),
                    env_path=env_path,
                    ok=True,
                    base_url=base,
                )
            )
        except Exception as exc:  # noqa: BLE001
            # Keep list robust: surface errors without raising.
            try:
                name = validate_account_name(raw)
            except Exception:
                name = raw
            entries.append(
                AccountListEntry(
                    account=name,
                    env_path=env_path,
                    ok=False,
                    error=str(exc),
                )
            )
    return entries


def handle_account_status(args) -> None:
    root = _root()

    active = get_active_account()
    ws_active = get_workspace_active_account(root)
    prefs, prefs_err = load_prefs(root)

    source = getattr(args, "_account_source", None) or "none"
    env_source = os.environ.get("QSYNC_ACCOUNT") or None

    # Resolve base_url without ever printing secrets.
    base_url: str | None = None
    env_path: Path | None = None
    env_ok: bool | None = None
    env_error: str | None = None
    try:
        if active and active.strip().lower() != "default":
            env_path = resolve_account_env_path(active, root=root)
            env = load_account_env(active, root=root)
            base_url = (env.get("QUALTRICS_BASE_URL") or "").strip() or None
            env_ok = True
        else:
            env_path = resolve_env_path(root=root) or (root / ".env")
            env = _load_default_env_for_root(root)
            base_url = (env.get("QUALTRICS_BASE_URL") or "").strip() or None
            env_ok = bool(base_url)
    except Exception as exc:  # noqa: BLE001
        env_ok = False
        env_error = str(exc)

    payload = {
        "root": str(root),
        "active_account": active,
        "active_account_label": _format_account_label(active),
        "account_source": source,
        "workspace_active_account": ws_active,
        "prefs_path": str(prefs_path(root)),
        "prefs_ok": prefs_err is None,
        "prefs_error": prefs_err,
        "env_path": str(env_path) if env_path else None,
        "env_ok": env_ok,
        "base_url": base_url,
        "survey_cache_subdir_pref": get_workspace_survey_cache_subdir(root),
        "survey_cache_subdir_resolved": resolve_survey_cache_subdir(root=root),
        "survey_cache_dir_effective": str(
            resolve_survey_cache_dir(root=root, account=active)
        ),
        "scoped_dirs": {
            "surveys": str(resolve_scoped_dir("surveys", root=root, account=active)),
            "excel": str(resolve_scoped_dir("excel", root=root, account=active)),
            "survey_js": str(resolve_scoped_dir("survey_js", root=root, account=active)),
            "contents": str(resolve_scoped_dir("contents", root=root, account=active)),
            "export": str(resolve_scoped_dir("export", root=root, account=active)),
            "responses": str(resolve_scoped_dir("responses", root=root, account=active)),
            "tmp": str(resolve_scoped_dir("tmp", root=root, account=active)),
        },
        "env": {"QSYNC_ACCOUNT": env_source},
    }

    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False))
        return

    print("[qsync:account:status]")
    print(f"  root: {payload['root']}")
    print(f"  active: {payload['active_account_label']}")
    print(f"  source: {payload['account_source']}")
    if ws_active:
        print(f"  workspace_active: {ws_active}")
    print(f"  prefs: {payload['prefs_path']}")
    if prefs_err:
        print(f"  prefs_error: {prefs_err}", file=sys.stderr)
    print(f"  env_path: {payload['env_path'] or '(not resolved)'}")
    if env_ok is False and env_error:
        print(f"  env_error: {env_error}", file=sys.stderr)
    print(f"  base_url: {base_url or '(missing)'}")
    pref = payload.get("survey_cache_subdir_pref")
    resolved_subdir = payload.get("survey_cache_subdir_resolved")
    effective_dir = payload.get("survey_cache_dir_effective")
    print(
        f"  survey_cache_subdir: pref={pref or '(default)'} resolved={resolved_subdir}"
    )
    print(f"  survey_cache_dir: {effective_dir}")
    print("  dirs:")
    for k, v in (payload.get("scoped_dirs") or {}).items():
        print(f"    {k}: {v}")


def handle_account_list(args) -> None:
    root = _root()
    entries = _build_account_list(root)

    if getattr(args, "json", False):
        payload = [
            {
                "account": e.account,
                "env_path": str(e.env_path),
                "ok": e.ok,
                "base_url": e.base_url,
                "error": e.error,
            }
            for e in entries
        ]
        print(json.dumps(payload, ensure_ascii=False))
        return

    print("[qsync:account:list]")
    print("  default (.env):", str(resolve_env_path(root=root) or (root / ".env")))
    if not entries:
        print("  (no .env.<account> files found)")
        return
    for e in entries:
        status = "ok" if e.ok else "invalid"
        suffix = f" base_url={e.base_url}" if e.base_url else ""
        print(f"  - {e.account}: {status}{suffix} ({e.env_path})")
        if not e.ok and e.error:
            print(f"    error: {e.error}", file=sys.stderr)


def handle_account_use(args) -> None:
    root = _root()
    account = validate_account_name(str(getattr(args, "account") or ""))

    # Ensure a stable explicit alias for the primary `.env` account.
    # This keeps `--account default` usable after users adopt/switch accounts.
    bootstrapped_default_env = _bootstrap_default_account_env(root)

    # Validate env file is present and usable.
    _ = load_account_env(account, root=root)

    set_workspace_active_account(root, account)

    if getattr(args, "json", False):
        payload: dict[str, Any] = {"ok": True, "active_account": account}
        if bootstrapped_default_env is not None:
            payload["bootstrapped_default_env"] = str(bootstrapped_default_env)
        print(json.dumps(payload, ensure_ascii=False))
        return
    print(f"[qsync:account:use] Active workspace account set to: {account}")
    if bootstrapped_default_env is not None:
        print(
            "[qsync:account:use] Bootstrapped default account env alias: "
            f"{bootstrapped_default_env.name}"
        )
    print("[qsync:account:use] Next: run `qsync account status` or `qsync doctor`.")


def handle_account_ensure_default_alias(args) -> None:
    root = _root()
    alias_path = resolve_account_env_path("default", root=root)
    created_path = _bootstrap_default_account_env(root)
    created = created_path is not None

    if not created and not alias_path.exists():
        src_env = resolve_env_path(root=root) or (root / ".env")
        raise QsyncConfigError(
            error_id="QSYNC-CONFIG-DEFAULTALIAS-001",
            problem="Unable to create `.env.default` alias from primary `.env`.",
            why=(
                "The primary `.env` must exist and include both "
                "`QUALTRICS_BASE_URL` and an API token."
            ),
            impact=(
                "A compatibility alias could not be created for workflows that "
                "explicitly target `default`."
            ),
            action=(
                f"Ensure `{src_env}` exists and includes:\n"
                "  QUALTRICS_BASE_URL=iad1.qualtrics.com\n"
                "  X-API-TOKEN=<token>\n"
                "(or QUALTRICS_API_KEY), then rerun this command."
            ),
            context={"root": str(root), "source_env": str(src_env), "alias": str(alias_path)},
            exit_code=1,
        )

    payload = {"ok": True, "env_path": str(alias_path), "created": created}
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False))
        return

    print("[qsync:account:ensure-default-alias]")
    print(f"  env_path: {alias_path}")
    print(f"  created: {created}")
    if not created:
        print("  note: `.env.default` already existed.")


def handle_account_clear(args) -> None:
    root = _root()
    set_workspace_active_account(root, None)
    if getattr(args, "json", False):
        print(json.dumps({"ok": True, "active_account": None}, ensure_ascii=False))
        return
    print("[qsync:account:clear] Cleared active workspace account (legacy default restored).")


def handle_account_cache_dir(args) -> None:
    root = _root()
    value = getattr(args, "value", None)
    clear = bool(getattr(args, "clear", False))

    if clear and value:
        raise SystemExit("[qsync:account:cache-dir] ERROR: provide either VALUE or --clear, not both.")

    if clear:
        set_workspace_survey_cache_subdir(root, None)
    elif value is not None:
        normalized = validate_survey_cache_subdir(str(value))
        set_workspace_survey_cache_subdir(root, normalized)

    pref = get_workspace_survey_cache_subdir(root)
    resolved_subdir = resolve_survey_cache_subdir(root=root)
    active = get_active_account()
    surveys_dir = resolve_scoped_dir("surveys", root=root, account=active)
    cache_base_dir = resolve_survey_cache_base_dir(root=root, account=active)
    preferred_dir = (cache_base_dir / resolved_subdir).resolve()
    effective_dir = resolve_survey_cache_dir(root=root, account=active)
    source = "subdir" if effective_dir == preferred_dir else "surveys_root_fallback"

    payload = {
        "ok": True,
        "root": str(root),
        "active_account": active,
        "survey_cache_subdir_pref": pref,
        "survey_cache_subdir_resolved": resolved_subdir,
        "surveys_dir": str(surveys_dir),
        "preferred_cache_dir": str(preferred_dir),
        "preferred_cache_dir_exists": preferred_dir.exists() and preferred_dir.is_dir(),
        "effective_cache_dir": str(effective_dir),
        "effective_source": source,
    }

    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False))
        return

    print("[qsync:account:cache-dir]")
    print(f"  active_account: {active or 'default'}")
    print(f"  pref: {pref or '(default)'}")
    print(f"  resolved_subdir: {resolved_subdir}")
    print(f"  preferred_cache_dir: {preferred_dir}")
    print(
        "  preferred_cache_dir_exists: "
        f"{payload['preferred_cache_dir_exists']}"
    )
    print(f"  effective_cache_dir: {effective_dir} ({source})")
    if not payload["preferred_cache_dir_exists"]:
        print(
            "  note: create the preferred cache dir to activate subfolder caching "
            f"(e.g. `mkdir -p {preferred_dir}`)."
        )


@dataclass(frozen=True)
class MoveItem:
    src: Path
    dst: Path


def _iter_unscoped_children(dir_: Path) -> Iterable[Path]:
    if not dir_.exists():
        return []
    children: list[Path] = []
    for child in sorted(dir_.iterdir()):
        # Never touch account-scoped dotdirs.
        if child.name.startswith("."):
            continue
        children.append(child)
    return children


_SURVEY_ID_RE = re.compile(r"^SV_[A-Za-z0-9]+$")


def _survey_id_from_named_segment(value: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if _SURVEY_ID_RE.fullmatch(text):
        return text
    marker = "-SV_"
    if marker not in text:
        return None
    suffix = text.rsplit(marker, 1)[-1]
    candidate = f"SV_{suffix}"
    if _SURVEY_ID_RE.fullmatch(candidate):
        return candidate
    return None


def _build_adopt_move_plan(root: Path, *, account: str) -> list[MoveItem]:
    """Return a conservative move plan from unscoped -> scoped dirs."""

    moves: list[MoveItem] = []

    # surveys/
    surveys_base = (root / "surveys").resolve()
    surveys_dst = resolve_scoped_dir("surveys", root=root, account=account)
    cache_subdir = resolve_survey_cache_subdir(root=root)
    cache_src_candidate = (surveys_base / cache_subdir).resolve()
    surveys_cache_src = (
        cache_src_candidate
        if cache_src_candidate.exists() and cache_src_candidate.is_dir()
        else surveys_base
    )
    # Preserve explicit cache-subdir layouts during account adoption.
    surveys_cache_dst = (
        (surveys_dst / cache_subdir).resolve()
        if surveys_cache_src != surveys_base
        else resolve_survey_cache_dir(root=root, account=account)
    )
    if surveys_base.exists():
        # Files
        for name in (
            "inventory.csv",
            "qualtrics_surveys.csv",
            ".focal_snapshot.json",
            "qualtrics_master.csv",
            "qualtrics_master.xlsx",
        ):
            src = surveys_base / name
            if src.exists() and src.is_file():
                moves.append(MoveItem(src=src, dst=surveys_dst / name))

        # Cached survey JSON: <Name>__SV_xxx.json
        for src in sorted(surveys_cache_src.glob("*__SV_*.json")):
            if src.is_file():
                moves.append(MoveItem(src=src, dst=surveys_cache_dst / src.name))

        # Directories
        for name in (
            "pending",
            "archive",
            "flow",
            "slices",
            "qualtrics_master_snapshots",
            "qualtrics_master_rollback",
        ):
            src = surveys_base / name
            if src.exists() and src.is_dir():
                moves.append(MoveItem(src=src, dst=surveys_dst / name))
        legacy_snapshots_src = surveys_base / "translation_key_snapshots"
        if legacy_snapshots_src.exists() and legacy_snapshots_src.is_dir():
            translations_dst_root = (
                resolve_scoped_dir("contents", root=root, account=account)
                / "qualtrics_survey_translations"
            )
            fallback_dst_root = surveys_dst / "translation_key_snapshots"
            for src in sorted(legacy_snapshots_src.iterdir()):
                if src.is_file():
                    moves.append(MoveItem(src=src, dst=fallback_dst_root / src.name))
                    continue
                if not src.is_dir():
                    continue
                survey_id = _survey_id_from_named_segment(src.name)
                if not survey_id:
                    moves.append(MoveItem(src=src, dst=fallback_dst_root / src.name))
                    continue
                dst_survey_dir = resolve_survey_path(
                    translations_dst_root,
                    survey_id,
                    is_dir=True,
                    root=root,
                    prefer_existing=True,
                    migrate_existing=False,
                )
                moves.append(MoveItem(src=src, dst=dst_survey_dir / "key_snapshots"))
        backups_src = surveys_cache_src / "backups"
        if backups_src.exists() and backups_src.is_dir():
            moves.append(MoveItem(src=backups_src, dst=surveys_cache_dst / "backups"))
        legacy_backups_src = surveys_base / "backups"
        if (
            surveys_cache_src != surveys_base
            and legacy_backups_src.exists()
            and legacy_backups_src.is_dir()
        ):
            moves.append(MoveItem(src=legacy_backups_src, dst=surveys_dst / "backups"))

    # excel/
    excel_base = (root / "excel").resolve()
    excel_dst = resolve_scoped_dir("excel", root=root, account=account)
    if excel_base.exists():
        archive = excel_base / "archive"
        if archive.exists() and archive.is_dir():
            moves.append(MoveItem(src=archive, dst=excel_dst / "archive"))
        for src in sorted(excel_base.glob("*.xlsx*")):
            if src.is_file():
                moves.append(MoveItem(src=src, dst=excel_dst / src.name))

    # contents/
    contents_base = (root / "contents").resolve()
    contents_dst = resolve_scoped_dir("contents", root=root, account=account)
    if contents_base.exists():
        for name in ("qualtrics_library_messages", "qualtrics_survey_translations"):
            src = contents_base / name
            if src.exists() and src.is_dir():
                moves.append(MoveItem(src=src, dst=contents_dst / name))

    # survey_js/ (non-core)
    survey_js_base = (root / "survey_js").resolve()
    survey_js_dst = resolve_scoped_dir("survey_js", root=root, account=account)
    mapping = survey_js_base / "survey_qid_js_map.csv"
    if mapping.exists() and mapping.is_file():
        moves.append(MoveItem(src=mapping, dst=survey_js_dst / mapping.name))

    # export/, responses/, tmp/: move all unscoped children (best-effort).
    for dirname in ("export", "responses", "tmp"):
        base = (root / dirname).resolve()
        dst = resolve_scoped_dir(dirname, root=root, account=account)
        for child in _iter_unscoped_children(base):
            moves.append(MoveItem(src=child, dst=dst / child.name))

    # Ensure deterministic ordering.
    moves.sort(key=lambda m: str(m.src))
    return moves


def _lock_path(root: Path) -> Path:
    return (root / ".qsync" / "account-adopt.lock").resolve()


def _acquire_lock(root: Path) -> None:
    path = _lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise QsyncConfigError(
            error_id="QSYNC-CONFIG-ADOPT-LOCK-001",
            problem="Workspace is locked for account adoption.",
            why="An account adoption/migration may already be running (or a previous run did not clean up).",
            impact="Refusing to run to avoid partial moves or corrupted state.",
            action=f"Remove `{path}` if you are sure no adoption is running, then retry.",
            context={"lock_path": str(path)},
            exit_code=2,
        )
    try:
        payload = {
            "pid": os.getpid(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
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


def _confirm_adopt(account: str) -> None:
    if not sys.stdin.isatty():
        raise SystemExit(
            "[qsync:account:adopt] Confirmation required but stdin is not interactive. "
            "Re-run with --yes to proceed."
        )
    typed = input("Type 'adopt' (or the account name) to confirm: ").strip()
    if typed not in {"adopt", account}:
        raise SystemExit("[qsync:account:adopt] Aborted.")


def _remove_existing(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def handle_account_adopt(args) -> None:
    root = _root()
    account = validate_account_name(str(getattr(args, "account") or ""))
    dry_run = bool(getattr(args, "dry_run", False))
    yes = bool(getattr(args, "yes", False))
    merge = bool(getattr(args, "merge", False))
    overwrite = bool(getattr(args, "overwrite", False))
    no_copy_env = bool(getattr(args, "no_copy_env", False))
    set_active = bool(getattr(args, "use", False))

    if overwrite and merge:
        raise SystemExit("[qsync:account:adopt] ERROR: choose at most one of --merge/--overwrite")

    # Optional env copy: `.env` -> `.env.<account>` if missing.
    env_src = (root / ".env").resolve()
    env_dst = resolve_account_env_path(account, root=root)
    will_copy_env = False
    if not no_copy_env and env_src.exists() and env_src.is_file() and not env_dst.exists():
        will_copy_env = True

    plan = _build_adopt_move_plan(root, account=account)
    if getattr(args, "json", False):
        payload = {
            "root": str(root),
            "account": account,
            "dry_run": dry_run,
            "copy_env": will_copy_env,
            "moves": [{"src": str(m.src), "dst": str(m.dst)} for m in plan],
        }
        print(json.dumps(payload, ensure_ascii=False))
        return

    print("[qsync:account:adopt]")
    print(f"  root:    {root}")
    print(f"  account: {account}")
    if will_copy_env:
        print(f"  will_copy_env: {env_src} -> {env_dst}")
    if not plan and not will_copy_env:
        print("  (nothing to move)")
        return

    if plan:
        print("  move_plan:")
        for m in plan:
            print(f"    - {m.src} -> {m.dst}")

    # Conflicts
    conflicts = [m for m in plan if m.dst.exists()]
    if conflicts and not (merge or overwrite):
        print("  conflicts detected (destination already exists):", file=sys.stderr)
        for m in conflicts:
            print(f"    - {m.dst}", file=sys.stderr)
        raise SystemExit(
            "[qsync:account:adopt] Refusing to proceed. Re-run with --merge or --overwrite."
        )

    if dry_run:
        print("  dry-run: no changes made.")
        return

    if not yes:
        _confirm_adopt(account)

    _acquire_lock(root)
    try:
        if will_copy_env:
            env_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(env_src, env_dst)

        moved = 0
        skipped = 0
        for m in plan:
            if m.dst.exists():
                if overwrite:
                    _remove_existing(m.dst)
                elif merge:
                    skipped += 1
                    continue

            m.dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(m.src), str(m.dst))
            moved += 1

        if set_active:
            # Validate env file for safety before setting as active.
            _ = load_account_env(account, root=root)
            if account != "default":
                _bootstrap_default_account_env(root)
            set_workspace_active_account(root, account)

        print(f"  moved:   {moved}")
        if skipped:
            print(f"  skipped (conflicts): {skipped}", file=sys.stderr)
            raise SystemExit(
                "[qsync:account:adopt] Completed with conflicts. Review skipped items."
            )
    finally:
        _release_lock(root)
