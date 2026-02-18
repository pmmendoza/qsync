"""Command-line interface for `qsync` (Qualtrics sync tooling)."""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .argparse_support import (
    QsyncArgumentParser,
    hide_subparser_choices,
    reorder_subparser_choices,
)

SURVEYS_DIR = Path("surveys")
DEFAULT_MAPPING_PATH = Path("survey_js") / "survey_qid_js_map.csv"

# Internal: how the ambient QSYNC_ACCOUNT context was chosen for this invocation.
# Values: "flag" | "env" | "workspace" | "none"
_ACCOUNT_CONTEXT_SOURCE = "none"

# Scope help text is shared across many commands; keep it consistent so users
# don't have to guess what the expression language supports.
_SCOPE_HELP_ITEMS = (
    "Scope filter expression (qid:<QID>, tag:<DataExportTag>; supports AND/OR/()). "
    "See docs/reference/scope-semantics.md."
)
_SCOPE_HELP_TRANSLATIONS = _SCOPE_HELP_ITEMS
_SCOPE_HELP_JS = (
    "Scope filter expression (qid:<QID>, tag:<DataExportTag>, js:<file>; supports AND/OR/()). "
    "See docs/reference/scope-semantics.md."
)
_SCOPE_HELP_SYNC = (
    "Scope filter expression passed to per-dimension workflows where supported (items/js/translations). "
    "See docs/reference/scope-semantics.md."
)
_SCOPE_HELP_EOS = (
    "Scope filter expression (accepted but currently ignored for EOS). "
    "See docs/reference/scope-semantics.md."
)

if TYPE_CHECKING:
    from .push_policy import PushContext
    from .sync_core import PreviewChange


def _extract_global_path_flags(
    argv: list[str],
) -> tuple[Path | None, Path | None, str | None, str | None, list[str]]:
    """
    Extract qsync global path flags from argv, regardless of position.

    Supports:
      --root <path> / --root=<path>
      --env-path <path> / --env-path=<path>
      --color <auto|always|never> / --color=<...>
      --account <name> / --account=<name>
    """
    root: Path | None = None
    env_path: Path | None = None
    color: str | None = None
    account: str | None = None
    cleaned: list[str] = []

    i = 0
    while i < len(argv):
        token = argv[i]

        if token.startswith("--root="):
            value = token.split("=", 1)[1]
            if not value:
                raise SystemExit("[qsync] ERROR: --root requires a value")
            root = Path(value)
            i += 1
            continue
        if token == "--root":
            if i + 1 >= len(argv):
                raise SystemExit("[qsync] ERROR: --root requires a value")
            root = Path(argv[i + 1])
            i += 2
            continue

        if token.startswith("--env-path="):
            value = token.split("=", 1)[1]
            if not value:
                raise SystemExit("[qsync] ERROR: --env-path requires a value")
            env_path = Path(value)
            i += 1
            continue
        if token == "--env-path":
            if i + 1 >= len(argv):
                raise SystemExit("[qsync] ERROR: --env-path requires a value")
            env_path = Path(argv[i + 1])
            i += 2
            continue

        if token.startswith("--color="):
            value = token.split("=", 1)[1].strip()
            if not value:
                raise SystemExit("[qsync] ERROR: --color requires a value")
            color = value
            i += 1
            continue
        if token == "--color":
            if i + 1 >= len(argv):
                raise SystemExit("[qsync] ERROR: --color requires a value")
            color = argv[i + 1].strip()
            i += 2
            continue

        if token.startswith("--account="):
            value = token.split("=", 1)[1].strip()
            if not value:
                raise SystemExit("[qsync] ERROR: --account requires a value")
            account = value
            i += 1
            continue
        if token == "--account":
            if i + 1 >= len(argv):
                raise SystemExit("[qsync] ERROR: --account requires a value")
            account = argv[i + 1].strip()
            i += 2
            continue

        cleaned.append(token)
        i += 1

    return root, env_path, color, account, cleaned


def _read_git_sha(git_dir: Path) -> str | None:
    head_path = git_dir / "HEAD"
    if not head_path.exists():
        return None
    head = head_path.read_text().strip()
    if head.startswith("ref:"):
        ref = head.split(" ", 1)[1].strip()
        ref_path = git_dir / ref
        if ref_path.exists():
            return ref_path.read_text().strip()
        packed_refs = git_dir / "packed-refs"
        if packed_refs.exists():
            for line in packed_refs.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("^"):
                    continue
                sha, ref_name = line.split(" ", 1)
                if ref_name == ref:
                    return sha
        return None
    if len(head) >= 7:
        return head
    return None


def _find_git_sha(start: Path, max_depth: int = 6) -> str | None:
    current = start
    for _ in range(max_depth):
        git_marker = current / ".git"
        if git_marker.is_dir():
            return _read_git_sha(git_marker)
        if git_marker.is_file():
            contents = git_marker.read_text().strip()
            if contents.startswith("gitdir:"):
                git_dir = contents.split(":", 1)[1].strip()
                return _read_git_sha((current / git_dir).resolve())
        if current.parent == current:
            break
        current = current.parent
    return None


def _infer_pipx_venv(executable: Path) -> Path | None:
    exe_str = str(executable)
    match = re.search(r"(?P<root>.*[\\/])pipx[\\/]venvs[\\/](?P<name>[^\\/]+)", exe_str)
    if not match:
        return None
    root = Path(match.group("root"))
    name = match.group("name")
    return (root / "pipx" / "venvs" / name).resolve()


def _version_diagnostics() -> list[str]:
    from . import __version__

    python_exe = Path(sys.executable).resolve()
    package_root = Path(__file__).resolve().parent
    entrypoint = shutil.which("qsync")
    entrypoint_path = Path(entrypoint).resolve() if entrypoint else None

    pipx_venv = _infer_pipx_venv(python_exe) or _infer_pipx_venv(Path(sys.prefix))
    is_venv = bool(os.environ.get("VIRTUAL_ENV")) or sys.prefix != sys.base_prefix
    if pipx_venv:
        install_label = f"pipx ({pipx_venv})"
    elif is_venv:
        install_label = f"venv ({Path(sys.prefix).resolve()})"
    else:
        install_label = "system"

    git_sha = (
        os.environ.get("QSYNC_GIT_SHA")
        or os.environ.get("GITHUB_SHA")
        or _find_git_sha(package_root)
    )
    short_sha = git_sha[:10] if git_sha else None

    lines = [
        f"qsync {__version__}",
        f"install: {install_label}",
        f"python: {platform.python_version()} ({python_exe})",
        f"package: {package_root}",
    ]
    if entrypoint_path:
        lines.append(f"entrypoint: {entrypoint_path}")
    if short_sha:
        lines.append(f"git: {short_sha}")
    return lines


def _print_version() -> None:
    for line in _version_diagnostics():
        print(line)


def _add_common_args(
    parser: argparse.ArgumentParser,
    *,
    include_xlsx: bool,
    survey_id_action: str | None = None,
    survey_id_help: str | None = None,
) -> None:
    survey_kwargs: dict[str, object] = {}
    if survey_id_action:
        survey_kwargs["action"] = survey_id_action
    parser.add_argument(
        "--survey-id",
        help=survey_id_help
        or "Target Qualtrics Survey ID (omit to select interactively)",
        **survey_kwargs,
    )
    if include_xlsx:
        parser.add_argument(
            "--xlsx",
            type=Path,
            help="Path to the Excel workbook for this survey (default: derived)",
        )
    parser.add_argument(
        "--filter-column",
        help="Optional filter column on Questions sheet (e.g. InPre, InPost)",
    )
    parser.add_argument(
        "--filter-value",
        help="Value to match in the filter column (default: TRUE)",
    )
    _add_include_args(parser, include_js=False)


def _add_js_common_args(
    parser: argparse.ArgumentParser,
    *,
    survey_id_action: str | None = None,
    survey_id_help: str | None = None,
) -> None:
    def _default_mapping_path() -> Path:
        # Account-aware default: survey_js/.<account>/survey_qid_js_map.csv when selected.
        from .config import resolve_root, resolve_scoped_dir

        root = resolve_root(required=False) or Path.cwd()
        return resolve_scoped_dir("survey_js", root=root) / "survey_qid_js_map.csv"

    survey_kwargs: dict[str, object] = {}
    if survey_id_action:
        survey_kwargs["action"] = survey_id_action
    parser.add_argument(
        "--survey-id",
        help=survey_id_help
        or "Target Qualtrics Survey ID (omit to select interactively)",
        **survey_kwargs,
    )
    parser.add_argument(
        "--mapping",
        type=Path,
        default=_default_mapping_path(),
        help=(
            "Path to survey_qid_js_map.csv (default: survey_js/survey_qid_js_map.csv, "
            "or survey_js/.<account>/survey_qid_js_map.csv when --account is used)."
        ),
    )
    _add_include_args(parser, include_js=True)


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


def _add_include_args(parser: argparse.ArgumentParser, *, include_js: bool) -> None:
    parser.add_argument(
        "--include-qid",
        action="append",
        dest="include_qids",
        default=[],
        help="Limit to specific Qualtrics QIDs (can be repeated).",
    )
    parser.add_argument(
        "--include-tag",
        action="append",
        dest="include_tags",
        default=[],
        help="Limit to specific DataExportTag values (can be repeated).",
    )
    if include_js:
        parser.add_argument(
            "--include-js",
            action="append",
            dest="include_js",
            default=[],
            help="Limit JS operations to specific core filenames.",
        )


def _to_set(values: list[str] | None) -> set[str] | None:
    if not values:
        return None
    cleaned = {v for v in values if v}
    return cleaned or None


def _resolve_tags_to_qids(survey_id: str, tags: list[str] | None) -> set[str] | None:
    if not tags:
        return None
    from .qualtrics_client import load_cached_survey

    survey = load_cached_survey(survey_id)
    tag_map = {}
    for qid, payload in survey.questions.items():
        tag = (payload.get("DataExportTag") or "").strip()
        if tag:
            tag_map[tag.lower()] = qid
    resolved = {
        tag_map[tag.strip().lower()]
        for tag in tags
        if tag and tag.strip().lower() in tag_map
    }
    missing = [tag for tag in tags if not tag or tag.strip().lower() not in tag_map]
    for tag in missing:
        print(
            f"[qsync] WARNING: DataExportTag '{tag}' not found in cached survey {survey_id}. "
            "Next: verify spelling/case, or refresh cache via `qsync survey pull --survey-id ...`."
        )
    return resolved or None


def _ensure_mapping_column(mapping_path: Path, survey_id: str) -> None:
    if not mapping_path.exists():
        raise SystemExit(
            f"[qsync:js] Mapping CSV not found at {mapping_path}. Run 'qsync js pull' first."
        )
    with mapping_path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, [])
    for field in header:
        if field == "js_file":
            continue
        prefix = field.split("-", 1)[0]
        if prefix == survey_id:
            return
    raise SystemExit(
        "[qsync:js] Mapping CSV is missing a column for this survey. "
        "Rebuild it via 'qsync js pull'."
    )


def _format_counts(ctx: PushContext) -> str:
    stamp = ctx.generated_at.isoformat() if ctx.generated_at else "unknown"
    return (
        f"{ctx.response_count} live / {ctx.preview_count} preview"
        f" (source: {ctx.counts_source}, inventory @ {stamp})"
    )


def _normalize_survey_ids(value: object) -> list[str]:
    ids: list[str] = []
    if value is None:
        return ids
    values = value if isinstance(value, list) else [value]
    for item in values:
        if item is None:
            continue
        text = str(item).strip()
        if not text:
            continue
        ids.extend(part.strip() for part in text.split(",") if part.strip())
    # Deduplicate while preserving order.
    return list(dict.fromkeys(ids))


def _prompt_for_survey_ids_if_needed(
    survey_ids: object,
    *,
    allow_all_surveys: bool = False,
) -> list[str]:
    ids = _normalize_survey_ids(survey_ids)
    if ids:
        return ids

    # Import here to avoid circular dependency
    from .survey_inventory import prompt_for_survey_ids

    prompted = prompt_for_survey_ids(
        allow_all_surveys=allow_all_surveys,
        interactive=sys.stdin.isatty(),
    )
    if not prompted:
        if sys.stdin.isatty():
            print("[qsync] Operation cancelled.")
        else:
            print("[qsync] ERROR: --survey-id required in non-interactive mode")
        raise SystemExit(1)
    return prompted


def _prompt_for_survey_id_if_needed(
    survey_id: object,
    *,
    allow_all_surveys: bool = False,
) -> str:
    """Prompt for survey ID if not provided via command line.

    Args:
        survey_id: Survey ID from args (may be None)
        allow_all_surveys: Whether to show "Show all surveys" option (for pull commands)

    Returns:
        Survey ID (from args or prompted)

    Raises:
        SystemExit: If no survey ID provided and prompt cancelled/non-interactive
    """
    ids = _prompt_for_survey_ids_if_needed(
        survey_id,
        allow_all_surveys=allow_all_surveys,
    )
    if len(ids) > 1:
        raise SystemExit(
            "[qsync] ERROR: This command accepts only one --survey-id value."
        )
    return ids[0]


def _prompt_confirmation(message: str) -> bool:
    try:
        from .interactive_menu import confirm

        return confirm(message, default=True)
    except Exception:
        resp = input(f"{message} [Y/n] ").strip().lower()
        if not resp:
            return True
        return resp in {"y", "yes"}


def _should_offer_workspace_onboard_hint(args: argparse.Namespace) -> bool:
    """Return True if we should offer `qsync onboard` for this command.

    Some commands are API-only (or otherwise workspace-optional) and should not
    prompt users to onboard just because the current directory isn't a qsync
    workspace. The command itself will fail with a config error if credentials
    are missing; onboarding should remain a suggestion for workspace-heavy flows.
    """

    raw = (os.environ.get("QSYNC_SKIP_ONBOARD_HINT") or "").strip().lower()
    if raw in {"1", "true", "t", "yes", "y", "on"}:
        return False

    cmd = getattr(args, "command", None)
    if cmd in {None, "doctor", "onboard", "self-update", "account"}:
        return False

    if cmd == "survey":
        sub = getattr(args, "survey_command", None)
        # API-only commands: should run anywhere (credentials permitting).
        api_only = {
            "list",
            "copy",
            "copy-cross-account",
            "rename",
            "delete",
            "cleanup-embedded-data",
            "publish",
            "activate",
            "deactivate",
            "versions",
            "version-fetch",
            "rollback",
            "push-question",
            "export-responses",
            "export-translation",
        }
        if sub in api_only:
            return False

        # Some "mostly-workspace" commands can operate without a workspace if an
        # explicit path is provided.
        if sub == "pull" and getattr(args, "dest", None):
            return False
        if sub == "inspect-question" and getattr(args, "survey_file", None):
            return False

        # Everything else under `qsync survey` is workspace-centric.
        return True

    # Default: most top-level commands are workspace-centric.
    return True


def _workspace_dirs_for_onboard_hint(args: argparse.Namespace) -> list[str]:
    """Return the workspace dirs required for this command (best-effort)."""

    cmd = getattr(args, "command", None)
    if cmd == "survey":
        sub = getattr(args, "survey_command", None)
        if sub in {"label", "focal", "inventory"}:
            return ["surveys"]
        if sub in {"prepare", "master"}:
            return ["surveys", "excel", "survey_js"]
        if sub == "pull":
            if getattr(args, "dest", None):
                return []
            return ["surveys"]
        if sub == "inspect-question":
            if getattr(args, "survey_file", None):
                return []
            return ["surveys"]
        return ["surveys"]

    if cmd == "items":
        return ["surveys", "excel"]
    if cmd == "js":
        return ["surveys", "survey_js"]
    if cmd == "sync":
        return ["surveys", "excel", "survey_js"]
    if cmd in {"compare", "init", "preview", "apply", "push", "translations"}:
        return ["surveys", "excel"]
    if cmd in {"export", "eos"}:
        return ["surveys"]

    return ["surveys", "excel", "survey_js"]


def _parse_extras_args(raw_extras: list[str] | None) -> list[str]:
    extras: list[str] = []
    if not raw_extras:
        return extras
    for raw in raw_extras:
        if not raw:
            continue
        for part in str(raw).split(","):
            part = part.strip()
            if part:
                extras.append(part)
    return extras


_EXTRA_ALLOWLIST = {"tui"}


def _extract_extras_from_spec(spec: str) -> list[str]:
    if not spec:
        return []
    try:
        import re

        match = re.search(r"\bqsync\[(?P<extras>[^\]]+)\]", spec)
        if not match:
            return []
        raw = match.group("extras")
        return [part.strip() for part in raw.split(",") if part.strip()]
    except Exception:
        return []


def _get_installed_extras() -> list[str]:
    return _get_installed_extras_for_installer(None)


def _infer_installed_extras_from_optional_deps(available_extras: list[str]) -> set[str]:
    """Infer "effectively installed" extras from installed optional dependencies.

    Packaging tools do not reliably persist which extras were originally requested
    (especially for pipx + VCS installs). For a better UX, infer extras by checking
    whether the optional-dependency packages for an extra are present.
    """

    inferred: set[str] = set()
    if not available_extras:
        return inferred

    try:
        from importlib.metadata import PackageNotFoundError, distribution, version
    except Exception:
        return inferred

    try:
        dist = distribution("qsync")
    except Exception:
        return inferred

    requires = list(dist.requires or [])
    if not requires:
        return inferred

    # Prefer the real packaging module if available; otherwise use pip's vendored copy.
    try:
        from packaging.markers import default_environment  # type: ignore
        from packaging.requirements import Requirement  # type: ignore
    except Exception:  # pragma: no cover - packaging may be absent in pipx venvs
        try:
            from pip._vendor.packaging.markers import default_environment
            from pip._vendor.packaging.requirements import Requirement
        except Exception:
            return inferred

    env_base = default_environment()
    extra_to_reqs: dict[str, list[Requirement]] = {e: [] for e in available_extras}

    for raw in requires:
        raw = str(raw).strip()
        if not raw:
            continue
        try:
            req = Requirement(raw)
        except Exception:
            continue

        if not req.marker:
            continue
        # Only treat requirements gated by "extra" (skip base env markers).
        if "extra" not in str(req.marker):
            continue

        for extra in available_extras:
            env = dict(env_base)
            env["extra"] = extra
            try:
                if req.marker.evaluate(env):
                    extra_to_reqs[extra].append(req)
            except Exception:
                continue

    for extra, reqs in extra_to_reqs.items():
        if not reqs:
            continue

        ok = True
        for req in reqs:
            try:
                installed = version(req.name)
            except PackageNotFoundError:
                ok = False
                break
            try:
                if req.specifier and not req.specifier.contains(
                    installed, prereleases=True
                ):
                    ok = False
                    break
            except Exception:
                # If parsing fails, treat presence as good enough.
                pass

        if ok:
            inferred.add(extra)

    return inferred


def _get_installed_extras_for_installer(installer: str | None) -> list[str]:
    extras: set[str] = set()

    # Prefer direct_url metadata when available (captures requested_extras).
    try:
        from importlib.metadata import distribution

        dist = distribution("qsync")
        raw = dist.read_text("direct_url.json")
        if raw:
            payload = json.loads(raw)
            for item in payload.get("requested_extras") or []:
                item = str(item).strip()
                if item:
                    extras.add(item)
    except Exception:
        pass

    # Fall back to pipx metadata only when pipx is the active/preferred installer.
    use_pipx_metadata = installer == "pipx" if installer else _looks_like_pipx_env()
    if use_pipx_metadata and shutil.which("pipx"):
        try:
            result = subprocess.run(
                ["pipx", "list", "--json"],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                payload = json.loads(result.stdout or "{}")
                venv = (payload.get("venvs") or {}).get("qsync") or {}
                main_pkg = (venv.get("metadata") or {}).get("main_package") or {}
                spec = str(main_pkg.get("package_or_url") or "").strip()
                for item in _extract_extras_from_spec(spec):
                    extras.add(item)
        except Exception:
            pass

    # As a final fallback, infer extras by checking installed optional-deps.
    try:
        extras.update(_infer_installed_extras_from_optional_deps(_get_available_extras()))
    except Exception:
        pass

    return sorted(extras)


def _get_available_extras() -> list[str]:
    try:
        from importlib.metadata import metadata

        provides = metadata("qsync").get_all("Provides-Extra") or []
        extras = sorted({str(item).strip() for item in provides if str(item).strip()})
        if extras:
            extras.extend(sorted(_EXTRA_ALLOWLIST - set(extras)))
            return sorted(set(extras))
    except Exception:
        pass

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if not pyproject.exists():
        return []
    try:
        try:
            import tomllib  # py311+
        except ImportError:  # pragma: no cover - py310 fallback if tomli is available
            import tomli as tomllib  # type: ignore
        data = tomllib.loads(pyproject.read_bytes())
        extras_map = data.get("project", {}).get("optional-dependencies", {}) or {}
        extras = sorted(str(key) for key in extras_map.keys())
        extras.extend(sorted(_EXTRA_ALLOWLIST - set(extras)))
        return sorted(set(extras))
    except Exception:
        # Minimal fallback: scan section header and parse keys.
        extras: list[str] = []
        in_section = False
        for line in pyproject.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                in_section = stripped == "[project.optional-dependencies]"
                continue
            if in_section and stripped and not stripped.startswith("#"):
                key = stripped.split("=", 1)[0].strip()
                if key:
                    extras.append(key)
        extras = sorted(set(extras))
        extras.extend(sorted(_EXTRA_ALLOWLIST - set(extras)))
        return sorted(set(extras))


def _normalize_repo_url(repo: str) -> str:
    repo = (repo or "").strip()
    if not repo:
        return ""
    if repo.startswith("git+"):
        return repo
    if repo.startswith("git@"):
        return f"git+ssh://{repo}"
    if repo.startswith(("https://", "ssh://")):
        return f"git+{repo}"
    if repo.count("/") == 1:
        return f"git+https://github.com/{repo}.git"
    return f"git+{repo}"


def _build_self_update_spec(
    repo: str,
    ref: str | None,
    extras: list[str],
) -> str:
    base = _normalize_repo_url(repo)
    if ref:
        base = f"{base}@{ref}"
    extras_part = f"[{','.join(extras)}]" if extras else ""
    return f"qsync{extras_part} @ {base}"


def _looks_like_pipx_env() -> bool:
    # In venvs, sys.executable can be a symlink to the base interpreter, and
    # resolving it can erase the venv path. sys.prefix reliably points at the venv.
    candidates = (Path(sys.prefix), Path(sys.executable))
    for candidate in candidates:
        parts = {p.lower() for p in candidate.parts}
        if "pipx" in parts and "venvs" in parts:
            return True
    return False


def _pipx_has_qsync() -> bool:
    if not shutil.which("pipx"):
        return False
    try:
        result = subprocess.run(
            ["pipx", "list", "--json"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return False
        payload = json.loads(result.stdout or "{}")
        venvs = payload.get("venvs", {}) or {}
        return "qsync" in venvs
    except Exception:
        return False


def _active_installer() -> str:
    return "pipx" if _looks_like_pipx_env() else "pip"


def _resolve_installer(force_pip: bool, force_pipx: bool) -> str:
    if force_pip and force_pipx:
        raise SystemExit("Choose only one of --pip or --pipx.")
    if force_pipx:
        return "pipx"
    if force_pip:
        return "pip"
    # Canonical default: update the installer used by the currently running qsync.
    return _active_installer()


def _qsync_entrypoints_on_path() -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    executable_name = "qsync.exe" if os.name == "nt" else "qsync"
    for path_entry in os.environ.get("PATH", "").split(os.pathsep):
        path_entry = path_entry.strip()
        if not path_entry:
            continue
        candidate = Path(path_entry) / executable_name
        if not candidate.exists():
            continue
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        found.append(resolved)
    return found


def _get_qsync_install_info(path: Path) -> dict[str, str]:
    """Get information about a qsync installation."""
    try:
        result = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode != 0:
            return {"path": str(path), "status": "error", "info": "Failed to get version"}

        output = result.stdout.strip()
        lines = output.split("\n")
        info_dict = {"path": str(path)}

        for line in lines:
            if line.startswith("install:"):
                install_type = line.split(":", 1)[1].strip().split()[0]
                info_dict["install_type"] = install_type
            elif line.startswith("python:"):
                python_ver = line.split(":", 1)[1].strip().split()[0]
                info_dict["python"] = python_ver
            elif line.startswith("package:"):
                pkg_path = line.split(":", 1)[1].strip()
                info_dict["package"] = pkg_path

        return info_dict
    except Exception as e:
        return {"path": str(path), "status": "error", "info": str(e)}


def _parallel_install_warnings(installer: str) -> list[str]:
    warnings: list[str] = []

    qsync_bins = _qsync_entrypoints_on_path()
    if len(qsync_bins) > 1:
        rendered = ", ".join(str(path) for path in qsync_bins)
        warnings.append(
            f"Multiple qsync executables are on PATH: {rendered}. "
            "Keep only one install location first on PATH to avoid version drift."
        )

    if installer == "pip" and _pipx_has_qsync():
        warnings.append(
            "A separate pipx qsync install is also present. "
            "This run updates the active pip install only. "
            "Optional cleanup: `pipx uninstall qsync`."
        )
    return warnings


def _handle_self_update(args: argparse.Namespace) -> None:
    from .terminal_output import info, success, warn
    from .interactive_menu import (
        confirm,
        is_interactive,
        multi_select_from_list,
        select_from_list,
    )

    # Check for multiple installations
    qsync_bins = _qsync_entrypoints_on_path()
    if len(qsync_bins) > 1 and is_interactive() and not getattr(args, "yes", False):
        info("[qsync:self-update]", f"Found {len(qsync_bins)} qsync installations:")
        install_infos = []
        for i, path in enumerate(qsync_bins, 1):
            install_info = _get_qsync_install_info(path)
            install_infos.append(install_info)
            install_type = install_info.get("install_type", "unknown")
            python_ver = install_info.get("python", "unknown")
            active = " (active)" if i == 1 else ""
            info(None, f"  {i}. {path}{active}")
            info(None, f"     Type: {install_type}, Python: {python_ver}")

        # Ask which installation to update
        choices = [
            f"{i}. {qsync_bins[i-1]} ({install_infos[i-1].get('install_type', 'unknown')})"
            for i in range(1, len(qsync_bins) + 1)
        ]
        selection = select_from_list(
            "Which qsync installation do you want to update?",
            choices,
            default=choices[0],
        )
        if selection is None:
            info("[qsync:self-update]", "Cancelled.")
            return

        selected_idx = choices.index(selection)
        selected_path = qsync_bins[selected_idx]
        selected_info = install_infos[selected_idx]

        # Update PATH to prioritize selected installation if not already first
        if selected_idx != 0:
            warn(
                "[qsync:self-update]",
                f"Selected installation is not first in PATH. Consider reordering PATH.",
            )

        # Ask about cleaning up other installations
        other_installations = [
            (i, path, install_infos[i])
            for i, path in enumerate(qsync_bins)
            if i != selected_idx
        ]
        if other_installations and confirm(
            f"Remove {len(other_installations)} other qsync installation(s)?",
            default=False,
        ):
            for idx, path, inst_info in other_installations:
                install_type = inst_info.get("install_type", "unknown")
                if install_type == "pipx":
                    info("[qsync:self-update]", f"Removing pipx installation: {path}")
                    subprocess.run(["pipx", "uninstall", "qsync"], check=False)
                elif install_type == "system" or "pyenv" in str(path):
                    # Try to find which Python owns this
                    try:
                        python_path = Path(path).parent.parent / "bin" / "python"
                        if python_path.exists():
                            info(
                                "[qsync:self-update]",
                                f"Removing pip installation: {path}",
                            )
                            subprocess.run(
                                [str(python_path), "-m", "pip", "uninstall", "-y", "qsync"],
                                check=False,
                            )
                        else:
                            warn(
                                "[qsync:self-update]",
                                f"Could not auto-remove {path} - please remove manually",
                            )
                    except Exception as e:
                        warn(
                            "[qsync:self-update]",
                            f"Failed to remove {path}: {e}",
                        )
                else:
                    warn(
                        "[qsync:self-update]",
                        f"Unknown install type for {path} - please remove manually",
                    )

    default_repo = (
        os.environ.get("QSYNC_UPDATE_REPO") or "https://github.com/pmmendoza/qsync.git"
    )
    repo = getattr(args, "repo", None) or default_repo
    ref = getattr(args, "ref", None) or os.environ.get("QSYNC_UPDATE_REF") or "main"

    installer = _resolve_installer(
        force_pip=bool(getattr(args, "pip", False)),
        force_pipx=bool(getattr(args, "pipx", False)),
    )
    available_extras = _get_available_extras()
    extras = _parse_extras_args(getattr(args, "extras", None))
    installed_extras = _get_installed_extras_for_installer(installer)

    if getattr(args, "all_extras", False) and available_extras:
        extras = list(available_extras)

    if not extras:
        if is_interactive() and not getattr(args, "yes", False):
            if available_extras:
                selection = multi_select_from_list(
                    "Select extras to include (optional):",
                    available_extras,
                    instruction="Space to toggle, Enter to confirm",
                    default=installed_extras or None,
                )
                if selection is None:
                    info("[qsync:self-update]", "Cancelled.")
                    return
                extras = list(selection)
        elif installed_extras:
            extras = list(installed_extras)

    extras = sorted({extra for extra in extras if extra})
    unknown = (
        [e for e in extras if e not in available_extras] if available_extras else []
    )
    if unknown:
        warn(
            "[qsync:self-update]",
            f"Unknown extras requested: {', '.join(unknown)} (continuing anyway).",
        )

    spec = _build_self_update_spec(repo, ref, extras)

    if installer == "pipx":
        # pipx reinstall does not accept --spec; use install --force with VCS spec.
        cmd = ["pipx", "install", "--force", spec]
    else:
        # VCS specs can resolve to new commits without version bumps.
        # Force reinstall so `qsync self-update` always applies the resolved ref.
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--force-reinstall",
            spec,
        ]

    info("[qsync:self-update]", f"Installer: {installer}")
    info("[qsync:self-update]", f"Target: {spec}")
    for msg in _parallel_install_warnings(installer):
        warn("[qsync:self-update]", msg)

    if getattr(args, "dry_run", False):
        info("[qsync:self-update]", "Dry run; would execute:")
        print("  " + " ".join(cmd))
        return

    if not getattr(args, "yes", False) and is_interactive():
        if not confirm("Proceed with self-update?", default=True):
            info("[qsync:self-update]", "Cancelled.")
            return

    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise SystemExit(
            f"[qsync:self-update] Update failed (exit {result.returncode})."
        )

    success("[qsync:self-update]", "Update complete.")


_HELP_TOPICS: dict[str, tuple[str, str]] = {
    "topics": (
        "Available Topics",
        "Run `qsync help <topic>` to show one topic.\n",
    ),
    "sync": (
        "Sync Workflow",
        "\n".join(
            [
                "Core model: pull -> preview -> stage -> push.",
                "",
                "Start here:",
                "- qsync sync --survey-id SV_...              (guided sync for one survey)",
                "- qsync sync --tui                           (same sync entrypoint, TUI mode)",
                "- qsync tui --sync                           (direct TUI entrypoint)",
                "",
                "Dimension workflows (manual control):",
                "- qsync items preview|stage|push --survey-id SV_...",
                "- qsync js    preview|stage|push --survey-id SV_...",
                "- qsync translations preview|stage|push --survey-id SV_...",
                "",
                "Notes:",
                "- Use --dimensions items,js,translations to narrow sync work.",
                "- Non-interactive runs (--yes / CI) apply extra safety gates.",
                "- If you see drift warnings, run the suggested pull/repair command before pushing.",
                "- For partner accounts: use --account <name> (or `qsync account use <name>`).",
            ]
        )
        + "\n",
    ),
    "pending": (
        "Pending / Staging",
        "\n".join(
            [
                "Staging writes pending artifacts under `surveys/pending/<dimension>/`.",
                "",
                "Typical usage:",
                "- Stage: qsync <dimension> stage --survey-id SV_...",
                "- Push:  qsync <dimension> push  --survey-id SV_...",
                "",
                "Tips:",
                "- Use `qsync sync` for a guided per-survey flow across dimensions.",
                "- If pending exists and the local surface changes, qsync will warn (drift).",
                "- To clear staged changes, use the interactive 'Clear staged changes' action in `qsync sync`.",
            ]
        )
        + "\n",
    ),
    "drift": (
        "Drift Checks",
        "\n".join(
            [
                "Drift means local edits are out of sync with the last known baseline.",
                "",
                "Typical remediation:",
                "- Run the recommended pull/repair command shown in the warning.",
                "- Re-run preview/stage after refreshing baseline.",
                "",
                "Examples:",
                "- qsync translations drift --survey-id SV_...        (translations-specific drift check)",
            ]
        )
        + "\n",
    ),
    "onboard": (
        "Onboarding",
        "\n".join(
            [
                "First-time setup:",
                "- qsync onboard",
                "",
                "It creates workspace folders and a `.env` with:",
                "- QUALTRICS_BASE_URL",
                "- X-API-TOKEN",
                "",
                "If credentials look wrong, run:",
                "- qsync doctor --check-api",
            ]
        )
        + "\n",
    ),
    "accounts": (
        "Accounts (.env.<account>)",
        "\n".join(
            [
                "Some commands support selecting an alternate account via `.env.<account>` files.",
                "",
                "One-off usage:",
                "- qsync doctor --check-api --account damian",
                "- qsync survey list --account damian",
                "- qsync survey pull --account damian",
                "",
                "Workspace default (no shell exports required):",
                "- qsync account list",
                "- qsync account use damian",
                "- qsync account status",
                "- qsync account clear",
                "",
                "Adopt existing unscoped artifacts into an account folder:",
                "- qsync account adopt damian --dry-run",
                "",
                "Account selection is strict: it never silently falls back to default credentials.",
            ]
        )
        + "\n",
    ),
    "settings": (
        "Settings Command Center",
        "\n".join(
            [
                "Use a single interactive hub for account/workspace controls:",
                "- qsync settings",
                "",
                "From there you can run:",
                "- account status/list/use/clear",
                "- inventory refresh (no counts / focal counts)",
                "- prepare surfaces",
                "- cache-folder preference changes",
                "- doctor --check-api",
                "",
                "Optional TUI settings screen (requires qsync[tui]):",
                "- qsync settings --tui",
                "- qsync survey menu --tui",
            ]
        )
        + "\n",
    ),
}


def _handle_help(args: argparse.Namespace) -> None:
    """Print short workflow help topics (CLI-safe, no Rich/TUI requirements)."""
    from .terminal_output import header, info, error

    topic = (getattr(args, "topic", None) or "").strip().lower() or "topics"
    if topic in {"list", "ls"}:
        topic = "topics"

    if topic == "topics":
        header("[qsync:help]", "Help topics:")
        for key in sorted(k for k in _HELP_TOPICS.keys() if k != "topics"):
            title, _ = _HELP_TOPICS[key]
            info(None, f"  - {key}: {title}")
        info(None, "\nRun: qsync help <topic>")
        return

    entry = _HELP_TOPICS.get(topic)
    if not entry:
        error("[qsync:help]", f"Unknown topic: {topic}")
        info(None, "Run: qsync help topics")
        raise SystemExit(1)

    title, body = entry
    header("[qsync:help]", title)
    print(body, end="")


def _handle_tui(args: argparse.Namespace) -> None:
    """Launch the optional Textual TUI (requires `qsync[tui]`)."""
    from .terminal_output import error, info
    from .interactive_menu import is_interactive

    if not is_interactive():
        error("[qsync:tui]", "Interactive TTY required (non-TTY/CI not supported).")
        raise SystemExit(1)
    if os.environ.get("QSYNC_JSON_MODE", "").strip():
        error("[qsync:tui]", "JSON mode is not compatible with the TUI.")
        raise SystemExit(1)

    try:
        from .tui.app import QsyncTuiApp  # lazy import (Textual is optional)
    except Exception:
        error("[qsync:tui]", "TUI dependencies are not installed.")
        info(None, "Install: pip install 'qsync[tui]'")
        info(None, "If using pipx: pipx install --force 'qsync[tui] @ <git-ref>'")
        raise SystemExit(1)

    start_screen = None
    if bool(getattr(args, "sync", False)) or str(getattr(args, "command", "") or "") == "sync":
        start_screen = "sync"
    QsyncTuiApp(start_screen=start_screen).run()


def _push_items_pending_record(
    *,
    survey_id: str,
    prefix: str,
    yes: bool,
    dry_run: bool,
    force_live: bool,
    force_preview: bool,
    no_publish: bool,
    allow_delete: bool,
    scope_expr: str | None,
    allow_drift: bool,
    prefer_pending: bool | None,
    workbook_tip: bool,
) -> None:
    from .terminal_output import info, warn
    from .pending_stage import (
        ItemsPendingPayload,
        clear_pending,
        load_pending,
        save_pending,
    )
    from .sync_core import push_staged_changes

    record = load_pending(survey_id, "items")
    if record is None or not isinstance(record.payload, ItemsPendingPayload):
        info(
            prefix,
            f"No staged changes found for {survey_id}. Run `qsync items stage` first.",
        )
        return
    info(
        prefix,
        f"Using pending schema v{getattr(record, 'schema_version', 1)} from surveys/pending/items/{survey_id}.json",
    )

    qids = list(record.payload.qids or [])
    embedded_fields = list(record.payload.embedded_fields or [])
    structural_ops = list(getattr(record.payload, "structural_ops", None) or [])
    if not qids and not embedded_fields:
        if not structural_ops:
            warn(prefix, "Pending record is empty; clearing.")
            clear_pending(survey_id, "items")
            return

    if bool(dry_run):
        info(
            prefix,
            f"[dry-run] Would push {len(qids)} question(s), {len(embedded_fields)} embedded field(s), "
            f"and {len(structural_ops)} structural op(s).",
        )
        if qids:
            info(prefix, f"QIDs: {', '.join(qids)}")
        if structural_ops:
            structural_qids = sorted(
                {
                    str(op.get("qid") or "").strip()
                    for op in structural_ops
                    if op.get("qid")
                }
            )
            info(prefix, f"Structural QIDs: {', '.join(structural_qids)}")
        return

    if prefer_pending is True and structural_ops:
        warn(
            prefix,
            "⚠️  Staged structural ops detected; ignoring `--use-pending` and re-staging from Excel.",
        )
        prefer_pending = False

    if qids or embedded_fields:
        from .workbook_resolver import WorkbookResolver
        from .dimensions.items_core import preview_changes

        resolver = WorkbookResolver()
        xlsx_path = (
            Path(record.payload.workbook)
            if record.payload.workbook
            else resolver.resolve(survey_id)
        )
        workbook_diffs = []
        if xlsx_path.exists():
            try:
                workbook_diffs = preview_changes(
                    survey_id,
                    xlsx_path,
                    scope_expr=scope_expr,
                    check_drift=False,
                    annotate_dirty=False,
                    self_heal_system_columns=False,
                )
            except Exception:
                workbook_diffs = []

        if workbook_diffs:
            import sys

            decision = prefer_pending
            if decision is None:
                if not bool(yes) and sys.stdin.isatty():
                    from .interactive_menu import select_from_list

                    choices = [
                        "Use staged changes (ignore Excel)",
                        "Restage from Excel (overwrite pending)",
                        "↩ Abort push",
                    ]
                    selection = select_from_list(
                        message="Excel differs from cache and staged changes exist. Which should be pushed?",
                        choices=choices,
                        default=choices[1],  # default is “restage” (legacy behavior)
                    )
                    if selection is None or selection.startswith("↩"):
                        decision = None
                    elif selection.startswith("Use staged"):
                        decision = True
                    else:
                        decision = False
                else:
                    decision = False

            if decision is True:
                info(prefix, "Using staged changes and ignoring workbook differences.")
            elif decision is None:
                raise SystemExit(f"{prefix} Aborted by user.")
            else:
                from .dimensions.items import _build_pending_payload_from_workbook

                info(
                    prefix,
                    "Excel differs from cache, re-staging from current Excel (overriding stale staging)...",
                )

                include_qids = (
                    set(record.payload.qids or [])
                    if not record.payload.filter_column
                    and not record.payload.filter_value
                    and record.payload.qids
                    else None
                )
                rebuilt = _build_pending_payload_from_workbook(
                    survey_id,
                    xlsx_path,
                    scope_expr=scope_expr,
                    filter_column=getattr(record.payload, "filter_column", None),
                    filter_value=getattr(record.payload, "filter_value", None),
                    include_qids=include_qids,
                    ignore_embedded=not bool(record.payload.embedded_fields),
                    allow_drift=bool(allow_drift),
                    interactive=not bool(yes),
                    existing=record.payload,
                )
                if not rebuilt:
                    warn(prefix, "No stageable changes after staging; clearing.")
                    clear_pending(survey_id, "items")
                    return
                record.payload = rebuilt
                record.schema_version = 2
                save_pending(record)
                qids = list(rebuilt.qids or [])
                embedded_fields = list(rebuilt.embedded_fields or [])

    if structural_ops:
        from .dimensions.items_structural import push_structural_ops
        from .qualtrics_client import refresh_survey_cache

        # Refresh cache once up-front (then restage workbook diffs if needed).
        refresh_survey_cache(survey_id)

        if record.payload.workbook and (qids or embedded_fields):
            wb_path = Path(record.payload.workbook)
            if wb_path.exists():
                from .dimensions.items import _build_pending_payload_from_workbook

                rebuilt = _build_pending_payload_from_workbook(
                    survey_id,
                    wb_path,
                    scope_expr=scope_expr,
                    filter_column=getattr(record.payload, "filter_column", None),
                    filter_value=getattr(record.payload, "filter_value", None),
                    ignore_embedded=False,
                    allow_drift=allow_drift,
                    interactive=not bool(yes),
                    existing=record.payload,
                )
                if rebuilt:
                    record.payload = rebuilt
                    record.schema_version = 2
                    save_pending(record)
                    qids = list(rebuilt.qids or [])
                    embedded_fields = list(rebuilt.embedded_fields or [])

        def _save_journal(updated: dict) -> None:
            record.payload.push_journal = dict(updated or {})
            save_pending(record)

        publish_now = (not bool(no_publish)) and not (qids or embedded_fields)

        push_structural_ops(
            survey_id=survey_id,
            payload={},
            structural_ops=structural_ops,
            push_journal=dict(getattr(record.payload, "push_journal", {}) or {}),
            interactive=not bool(yes),
            allow_delete=bool(allow_delete),
            force_live=bool(force_live),
            force_preview=bool(force_preview),
            publish=publish_now,
            dry_run=False,
            refresh_cache=False,
            save_journal_cb=_save_journal,
        )

        record.payload.structural_ops = []
        record.payload.structural_summary = {}
        record.payload.push_journal = {}
        save_pending(record)

        structural_qids = sorted(
            {str(op.get("qid") or "").strip() for op in structural_ops if op.get("qid")}
        )
        qids = [qid for qid in qids if qid not in set(structural_qids)]

        if not qids and not embedded_fields:
            clear_pending(survey_id, "items")
            info(prefix, "Changes pushed to Qualtrics")
            if workbook_tip:
                info(
                    prefix,
                    f"Tip: refresh your workbook: `qsync items pull --survey-id {survey_id}`",
                )
            return

    if record.schema_version < 2 or not getattr(record.payload, "changes", None):
        if record.payload.workbook:
            wb_path = Path(record.payload.workbook)
            if wb_path.exists():
                from .dimensions.items import _build_pending_payload_from_workbook

                rebuilt = _build_pending_payload_from_workbook(
                    survey_id,
                    wb_path,
                    scope_expr=scope_expr,
                    filter_column=getattr(record.payload, "filter_column", None),
                    filter_value=getattr(record.payload, "filter_value", None),
                    ignore_embedded=False,
                    allow_drift=bool(allow_drift),
                    interactive=not bool(yes),
                    existing=record.payload,
                )
                if rebuilt:
                    record.payload = rebuilt
                    record.schema_version = 2
                    save_pending(record)
                    qids = list(rebuilt.qids or [])
                    embedded_fields = list(rebuilt.embedded_fields or [])

    push_staged_changes(
        survey_id=survey_id,
        qids=qids,
        embedded_fields=embedded_fields,
        pending_changes=list(getattr(record.payload, "changes", None) or []),
        workbook=record.payload.workbook,
        filter_column=record.payload.filter_column,
        filter_value=record.payload.filter_value,
        publish=not bool(no_publish),
        force_live=bool(force_live),
        force_preview=bool(force_preview),
        interactive=not bool(yes),
        allow_drift=bool(allow_drift),
    )
    from .qualtrics_client import refresh_survey_cache

    refresh_survey_cache(survey_id)
    clear_pending(survey_id, "items")
    info(prefix, "Changes pushed to Qualtrics")
    if workbook_tip:
        info(
            prefix,
            f"Tip: refresh your workbook: `qsync items pull --survey-id {survey_id}`",
        )


def _enforce_push_safeguards(
    survey_id: str,
    *,
    dimension: str,
    force_live: bool,
    force_preview_items: bool,
    auto_yes: bool,
) -> PushContext:
    from .push_policy import FRESHNESS_LIMIT, load_push_context
    from .survey_lock import ERROR_ID_SURVEY_LOCKED, SurveyLockedError, ensure_unlocked
    from .survey_ref import format_survey_ref

    ctx = load_push_context(survey_id)
    survey_ref = format_survey_ref(survey_id, getattr(ctx, "survey_name", None))
    try:
        ensure_unlocked(survey_id)
    except (SurveyLockedError, RuntimeError) as exc:
        from .push_logger import log_push_event

        log_push_event(
            action="qsync.survey.locked.blocked",
            method="LOCAL",
            path=f"cli._enforce_push_safeguards:{dimension}",
            survey_id=survey_id,
            status=None,
            error={"error_id": ERROR_ID_SURVEY_LOCKED, "message": str(exc)},
        )
        raise SystemExit(f"[qsync:{dimension}] ERROR: {exc}") from exc

    summary = _format_counts(ctx)
    if ctx.counts_unknown and not force_live:
        raise SystemExit(
            f"[qsync:{dimension}] Unable to verify response counts for {survey_ref}. "
            "Run 'make pull' (which refreshes inventory) or "
            "'qsync survey inventory', then retry or pass --force-live "
            "after reviewing the survey manually."
        )

    if ctx.response_count > 0:
        if not force_live:
            raise SystemExit(
                f"[qsync:{dimension}] {survey_ref} has {ctx.response_count} finished response(s). "
                "Re-run with --force-live after double-checking the diffs."
            )
        print(
            f"[qsync:{dimension}] WARNING: pushing {survey_ref} despite live responses -- {summary}. "
            "Next: double-check diffs and confirm this is safe."
        )
        if not auto_yes and not _prompt_confirmation("Proceed with push?"):
            raise SystemExit(f"[qsync:{dimension}] Aborted by user.")
        return ctx

    preview_only = ctx.preview_count > 0

    if dimension == "items" and preview_only and not force_live:
        if not force_preview_items:
            raise SystemExit(
                f"[qsync:{dimension}] Survey has preview/test responses. "
                "Re-run with --force-preview-items (or --force-live) to proceed."
            )
        print(
            f"[qsync:{dimension}] WARNING: pushing {survey_ref} items with preview/test responses -- {summary}. "
            "Next: confirm preview/test responses can be overwritten."
        )
        if not auto_yes and not _prompt_confirmation("Push item wording anyway?"):
            raise SystemExit(f"[qsync:{dimension}] Aborted by user.")
        return ctx

    if dimension == "js" and preview_only and not force_live:
        print(
            f"[qsync:{dimension}] WARNING: JS push for {survey_ref} will overwrite preview responses -- {summary}. "
            "Next: confirm preview/test responses can be overwritten."
        )
        if not auto_yes and not _prompt_confirmation("Continue with JS push?"):
            raise SystemExit(f"[qsync:{dimension}] Aborted by user.")

    if ctx.stale:
        print(
            f"[qsync:{dimension}] NOTE: inventory timestamp is older than {FRESHNESS_LIMIT} for {survey_ref} -- "
            f"{summary}"
        )

    return ctx


def _summarize_preview(changes: list[PreviewChange]) -> None:
    sheets = {
        "question": "Questions",
        "question_setting": "Questions",
        "option": "Options",
        "subitem": "Subitems",
        "sbs_column": "SBS_Columns",
        "sbs_column_answer": "SBS_ColumnAnswers",
        "embedded": "Embedded_Data",
    }

    def diff_summary(diff_lines: list[str] | None) -> str:
        if not diff_lines:
            return "-"
        plus = sum(
            1
            for line in diff_lines
            if line.startswith("+") and not line.startswith("+++")
        )
        minus = sum(
            1
            for line in diff_lines
            if line.startswith("-") and not line.startswith("---")
        )
        return f"+{plus}/-{minus}" if (plus or minus) else "-"

    rows: list[tuple[str, str, str, str, str, str]] = []
    for change in changes:
        sheet = sheets.get(change.kind, "?")
        tag = change.data_export_tag or ""
        if change.kind == "question":
            target = "Text"
            desc = "Question wording"
        elif change.kind == "question_setting":
            target = "Validation"
            desc = "Question response settings"
        elif change.kind == "option":
            target = f"Choice {change.choice_id}"
            desc = "Option label"
        elif change.kind == "subitem":
            target = f"Subitem {change.answer_id}"
            desc = "Subitem label"
        elif change.kind == "sbs_column":
            target = f"Column {change.choice_id}"
            desc = "SBS column header"
        elif change.kind == "sbs_column_answer":
            target = f"Column {change.choice_id} Answer {change.answer_id}"
            desc = "SBS column answer label"
        elif change.kind == "embedded":
            tag = change.flow_id or ""
            target = "Value"
            desc = "Embedded data default"
            if change.is_dangerous:
                desc = "Embedded data default ⚠️"
        else:
            target = "-"
            desc = "Field changed"
        rows.append(
            (sheet, change.qid, tag, target, desc, diff_summary(change.diff_lines))
        )

    if not rows:
        return

    header = ("Sheet", "QID", "Tag", "Target", "Description", "Δ(+/-)")
    widths = [len(col) for col in header]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(str(cell)))

    line = " ".join(col.ljust(widths[idx]) for idx, col in enumerate(header))
    print(line)
    print("-" * len(line))
    for row in rows:
        print(" ".join(str(cell).ljust(widths[idx]) for idx, cell in enumerate(row)))


def _slugify(value: str) -> str:
    """Make a filesystem-safe slug from a human-readable value."""

    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(value))


def _default_xlsx_path(survey_id: str) -> Path:
    """Derive a default workbook path for a survey.

    Now uses WorkbookResolver for consistent path resolution.
    Maintains backward compatibility with old-format files.

    Preference order:
    1) `name` column from surveys/inventory.csv (legacy: surveys/qualtrics_surveys.csv) (internal label).
    2) Online SurveyTitle from the Qualtrics payload.
    3) Fall back to the SurveyID itself.

    Returns:
        Path in format: excel/{slug}-{survey-id}.xlsx
        Or if old format exists: excel/{survey-id}-{slug}.xlsx
    """
    from .workbook_resolver import WorkbookResolver

    resolver = WorkbookResolver()
    return resolver.default_path(survey_id)


def _main_impl(argv: Optional[list[str]] = None) -> None:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    root_flag, env_path_flag, color_flag, account_flag, cleaned_argv = (
        _extract_global_path_flags(raw_argv)
    )

    if "--version" in cleaned_argv or "-V" in cleaned_argv:
        _print_version()
        return

    # Shell completion calls (argcomplete) should not cause side effects like
    # changing CWD or mutating environment variables.
    is_completion = os.environ.get("_ARGCOMPLETE") is not None

    if not is_completion:
        global _ACCOUNT_CONTEXT_SOURCE

        if env_path_flag:
            env_abs = Path(env_path_flag).expanduser().resolve()
            os.environ["QSYNC_ENV_PATH"] = str(env_abs)

        if root_flag:
            root_abs = Path(root_flag).expanduser().resolve()
            os.environ["QSYNC_ROOT"] = str(root_abs)
            os.chdir(root_abs)
        else:
            try:
                from .config import resolve_root

                discovered = resolve_root(required=False)
                if discovered and discovered != Path.cwd():
                    os.environ["QSYNC_ROOT"] = str(discovered)
                    os.chdir(discovered)
            except Exception:
                pass

        # Resolve account selection precedence:
        #   --account flag > QSYNC_ACCOUNT env var > workspace preference (.qsync/preferences.json)
        from .config import validate_account_name

        if account_flag:
            os.environ["QSYNC_ACCOUNT"] = validate_account_name(str(account_flag))
            _ACCOUNT_CONTEXT_SOURCE = "flag"
        else:
            raw_env = (os.environ.get("QSYNC_ACCOUNT") or "").strip()
            if raw_env:
                os.environ["QSYNC_ACCOUNT"] = validate_account_name(raw_env)
                _ACCOUNT_CONTEXT_SOURCE = "env"
            else:
                try:
                    from .workspace_prefs import get_workspace_active_account

                    root_for_prefs = Path(
                        os.environ.get("QSYNC_ROOT") or str(Path.cwd())
                    ).resolve()
                    ws_account = get_workspace_active_account(root_for_prefs)
                    if ws_account:
                        os.environ["QSYNC_ACCOUNT"] = validate_account_name(ws_account)
                        _ACCOUNT_CONTEXT_SOURCE = "workspace"
                except Exception:
                    # Workspace preferences are best-effort: if missing/unreadable,
                    # keep legacy default behavior.
                    pass

    parser = QsyncArgumentParser(
        prog="qsync",
        description=(
            "Qualtrics sync and survey management for NEWSFLOWS surveys.\n\n"
            "First-time setup: run `qsync onboard` to create folders and .env."
        ),
    )
    parser.add_argument(
        "--root",
        type=Path,
        help="Workspace root directory (contains surveys/, excel/, survey_js/, etc.).",
    )
    parser.add_argument(
        "--env-path",
        type=Path,
        help="Path to a .env file with credentials (overrides QSYNC_ENV_PATH and <root>/.env).",
    )
    parser.add_argument(
        "--account",
        help=(
            "Use credentials from `.env.<account>` under the workspace root "
            "(and scope workspace writes under `.<account>/` directories)."
        ),
    )
    parser.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        help="Color output: auto (default), always, or never. NO_COLOR forces never.",
    )
    parser.add_argument(
        "--version",
        "-V",
        action="store_true",
        help="Print diagnostic version info and exit.",
    )
    parser.add_argument(
        "--allow-locked",
        action="store_true",
        help="Bypass surveys/inventory.csv lock checks (dangerous).",
    )
    # Use `metavar` so help output doesn't inline every command in the Usage line.
    # With many subcommands, the default `{a,b,c,...}` form becomes unreadable.
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="COMMAND",
    )

    # doctor
    p_doctor = subparsers.add_parser(
        "doctor",
        help="Print resolved workspace/config paths for debugging",
    )
    p_doctor.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON to stdout (no other output)",
    )
    p_doctor.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress non-error output",
    )
    p_doctor.add_argument(
        "--check-api",
        action="store_true",
        help="Call GET /whoami to validate credentials and detect datacenter mismatch (requires network).",
    )
    p_doctor.add_argument(
        "--account",
        help=(
            "Use credentials from `.env.<account>` under the workspace root "
            "(affects credential checks and --check-api)."
        ),
    )

    # account
    p_account = subparsers.add_parser(
        "account",
        help="Manage the workspace default account selection (without exporting env vars)",
    )
    account_subs = p_account.add_subparsers(
        dest="account_command",
        required=True,
        metavar="SUBCOMMAND",
    )

    from .cli_account import (
        handle_account_adopt,
        handle_account_cache_dir,
        handle_account_clear,
        handle_account_list,
        handle_account_status,
        handle_account_use,
    )
    from .cli_settings import handle_settings

    p_account_status = account_subs.add_parser(
        "status",
        help="Show resolved active account and workspace preference state",
    )
    p_account_status.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON to stdout (no other output)",
    )
    p_account_status.set_defaults(func=handle_account_status)

    p_account_list = account_subs.add_parser(
        "list",
        help="List available `.env.<account>` files (best-effort validation)",
    )
    p_account_list.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON to stdout (no other output)",
    )
    p_account_list.set_defaults(func=handle_account_list)

    p_account_use = account_subs.add_parser(
        "use",
        help="Persist an active workspace account selection (acts like implicit --account)",
    )
    p_account_use.add_argument("account", help="Account name (maps to `.env.<account>`)")
    p_account_use.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON to stdout (no other output)",
    )
    p_account_use.set_defaults(func=handle_account_use)

    p_account_clear = account_subs.add_parser(
        "clear",
        help="Clear the active workspace account selection (restore legacy default `.env`)",
    )
    p_account_clear.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON to stdout (no other output)",
    )
    p_account_clear.set_defaults(func=handle_account_clear)

    p_account_adopt = account_subs.add_parser(
        "adopt",
        help="Move existing unscoped qsync-managed artifacts under `.<account>/` directories",
    )
    p_account_adopt.add_argument("account", help="Account name to adopt/migrate into")
    p_account_adopt.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the move plan without changing anything",
    )
    p_account_adopt.add_argument(
        "--yes",
        action="store_true",
        help="Skip typed confirmation prompt",
    )
    p_account_adopt.add_argument(
        "--merge",
        action="store_true",
        help="Skip items whose destination already exists (non-zero exit on conflicts)",
    )
    p_account_adopt.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite items whose destination already exists (dangerous)",
    )
    p_account_adopt.add_argument(
        "--no-copy-env",
        action="store_true",
        help="Do not create `.env.<account>` by copying `.env` when missing",
    )
    p_account_adopt.add_argument(
        "--use",
        action="store_true",
        help="After adoption, set this account as the active workspace account",
    )
    p_account_adopt.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON to stdout (no other output)",
    )
    p_account_adopt.set_defaults(func=handle_account_adopt)

    p_account_cache_dir = account_subs.add_parser(
        "cache-dir",
        help=(
            "Show or set the workspace survey cache subfolder name "
            "(used under surveys/, defaults to `caches` with fallback to surveys/)"
        ),
    )
    p_account_cache_dir.add_argument(
        "value",
        nargs="?",
        help="Set cache subfolder name (e.g. `caches` or `defs`).",
    )
    p_account_cache_dir.add_argument(
        "--clear",
        action="store_true",
        help="Clear workspace preference and revert to default `caches` resolution.",
    )
    p_account_cache_dir.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON to stdout (no other output)",
    )
    p_account_cache_dir.set_defaults(func=handle_account_cache_dir)

    # settings
    p_settings = subparsers.add_parser(
        "settings",
        help="Interactive account/workspace settings command center",
    )
    p_settings.add_argument(
        "--tui",
        action="store_true",
        help="Launch Textual settings screen (requires qsync[tui]).",
    )
    p_settings.set_defaults(func=handle_settings)

    # help
    p_help = subparsers.add_parser(
        "help",
        help="Show short workflow help topics",
    )
    p_help.add_argument(
        "topic",
        nargs="?",
        help="Help topic (run without args to list topics)",
    )

    # tui (optional extra)
    p_tui = subparsers.add_parser(
        "tui",
        help="Launch interactive TUI (requires qsync[tui])",
    )
    p_tui.add_argument(
        "--sync",
        action="store_true",
        help="Start in the sync wizard screen",
    )

    # onboard
    p_onboard = subparsers.add_parser(
        "onboard",
        help="Interactive workspace setup (folders, .env, gitignore)",
    )
    p_onboard.add_argument(
        "--datacenter",
        help="Qualtrics datacenter host (e.g., iad1.qualtrics.com)",
    )
    p_onboard.add_argument(
        "--root",
        type=Path,
        help="Workspace root directory (overrides current directory).",
    )
    p_onboard.add_argument(
        "--token",
        help="Qualtrics API token (X-API-TOKEN)",
    )
    p_onboard.add_argument(
        "--skip-gitignore",
        action="store_true",
        help="Skip updating .gitignore",
    )
    p_onboard.add_argument(
        "--non-interactive",
        action="store_true",
        help="Run without prompts (uses provided flags and defaults)",
    )
    p_onboard.add_argument(
        "--resume",
        action="store_true",
        help="Resume onboarding from .qsync/onboard-state.json",
    )
    p_onboard.add_argument(
        "--with-inventory",
        action="store_true",
        help="Fetch survey inventory during onboarding",
    )
    p_onboard.add_argument(
        "--with-focal",
        action="store_true",
        help="Select focal surveys during onboarding (requires inventory)",
    )
    p_onboard.add_argument(
        "--with-fasttext",
        action="store_true",
        help="Install fasttext + download model during onboarding",
    )
    p_onboard.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview onboarding actions without writing files",
    )

    # self-update
    p_self_update = subparsers.add_parser(
        "self-update",
        help="Update qsync from GitHub (supports optional extras)",
    )
    p_self_update.add_argument(
        "--extras",
        action="append",
        help="Comma-separated extras to install (e.g., tui,langcheck).",
    )
    p_self_update.add_argument(
        "--all-extras",
        action="store_true",
        help="Install all available extras.",
    )
    p_self_update.add_argument(
        "--repo",
        help="GitHub repo URL or owner/name (default: QSYNC_UPDATE_REPO or upstream).",
    )
    p_self_update.add_argument(
        "--ref",
        help="Git ref to install (branch, tag, or SHA; default: main).",
    )
    p_self_update.add_argument(
        "--pipx",
        action="store_true",
        help="Force pipx reinstall (auto-detected by default).",
    )
    p_self_update.add_argument(
        "--pip",
        action="store_true",
        help="Force pip install (auto-detected by default).",
    )
    p_self_update.add_argument(
        "--yes",
        action="store_true",
        help="Run without confirmation prompts.",
    )
    p_self_update.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the install command without executing it.",
    )

    # compare
    p_compare = subparsers.add_parser(
        "compare",
        help="Compare two surveys (items + JS + metadata) using cached or refreshed definitions.",
    )
    p_compare.add_argument("--source-id", required=True, help="Source SurveyID")
    p_compare.add_argument("--target-id", required=True, help="Target SurveyID")
    p_compare.add_argument(
        "--no-refresh",
        action="store_true",
        help="Use cached surveys without refreshing from Qualtrics",
    )
    p_compare.add_argument(
        "--include-tag",
        action="append",
        dest="include_tags",
        default=[],
        help="Only compare questions with these DataExportTag values (can repeat)",
    )
    p_compare.add_argument(
        "--exclude-tag",
        action="append",
        dest="exclude_tags",
        default=[],
        help="Skip questions with these DataExportTag values (can repeat)",
    )
    p_compare.add_argument(
        "--json-output", type=Path, help="Optional path to write JSON report"
    )
    p_compare.add_argument(
        "--fail-on",
        default="any",
        choices=["any", "question", "metadata"],
        help="Exit non-zero when mismatches exist (default: any)",
    )
    p_compare.add_argument(
        "--with-diffs",
        action="store_true",
        help="Include per-field before/after and unified diffs in the JSON output",
    )

    # init
    p_init = subparsers.add_parser(
        "init",
        help="Initialise or refresh the Excel workbook for a survey from Qualtrics",
    )
    _add_common_args(p_init, include_xlsx=False)
    p_init.add_argument(
        "--xlsx",
        type=Path,
        help="Path to the Excel workbook (default: excel/<SurveyTitle>-<SurveyID>.xlsx)",
    )
    p_init.add_argument(
        "--language",
        action="append",
        dest="language",
        help="Add translation columns for a language (repeatable). If omitted, auto-detects all enabled languages from Qualtrics.",
    )
    p_init.add_argument(
        "--languages",
        help="Comma-separated language codes to add as translation columns. If omitted, auto-detects all enabled languages from Qualtrics.",
    )

    # preview
    p_preview = subparsers.add_parser(
        "preview",
        help="Show what would change in Qualtrics based on the workbook",
    )
    _add_common_args(p_preview, include_xlsx=True)
    p_preview.add_argument(
        "--detailed",
        action="store_true",
        help="Print full old/new HTML for each detected change",
    )
    p_preview.add_argument(
        "--embedded-data-only",
        action="store_true",
        help="Only show Embedded_Data changes",
    )
    p_preview.add_argument(
        "--allow-drift",
        action="store_true",
        help="Allow preview against a drifted cache without prompting",
    )

    # apply
    p_apply = subparsers.add_parser(
        "apply",
        help="Apply the changes from the workbook to Qualtrics",
    )
    _add_common_args(p_apply, include_xlsx=True)
    p_apply.add_argument(
        "--yes",
        action="store_true",
        help="Proceed without an interactive confirmation prompt",
    )
    p_apply.add_argument(
        "--force-live",
        action="store_true",
        help="Allow pushes even if finished responses exist in Qualtrics",
    )
    p_apply.add_argument(
        "--force-preview-items",
        action="store_true",
        help="Allow item pushes when only preview/test responses exist",
    )
    p_apply.add_argument(
        "--embedded-data-only",
        action="store_true",
        help="Only apply Embedded_Data changes",
    )
    p_apply.add_argument(
        "--allow-dangerous",
        action="store_true",
        help="Allow dangerous embedded data edits (fields without defaults).",
    )
    p_apply.add_argument(
        "--allow-drift",
        action="store_true",
        help="Proceed even if cached survey differs from the live API",
    )

    # push
    p_push = subparsers.add_parser(
        "push",
        help="Push staged wording changes from the cached JSON to Qualtrics",
    )
    p_push.add_argument(
        "--survey-id",
        help="Target Qualtrics Survey ID (omit to select interactively)",
    )
    p_push.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt",
    )
    p_push.add_argument(
        "--force-live",
        action="store_true",
        help="Allow pushes even if finished responses exist in Qualtrics",
    )
    p_push.add_argument(
        "--force-preview-items",
        action="store_true",
        help="Allow item pushes when only preview/test responses exist",
    )
    p_push.add_argument(
        "--allow-drift",
        action="store_true",
        help="Proceed even if cached survey differs from the live API",
    )
    p_push.add_argument(
        "--no-publish",
        action="store_true",
        help="Skip publishing the survey after pushing question updates",
    )

    # items command group
    p_items = subparsers.add_parser(
        "items",
        help="Manage survey items (questions, options, subitems) via Excel workbook (group)",
    )
    items_subparsers = p_items.add_subparsers(
        dest="items_command",
        required=True,
        metavar="COMMAND",
    )

    p_items_pull = items_subparsers.add_parser(
        "pull",
        help="Pull survey to Excel workbook",
    )
    _add_common_args(
        p_items_pull,
        include_xlsx=False,
        survey_id_action="append",
        survey_id_help=(
            "Target survey ID(s) (repeatable/comma-separated; omit to select interactively)"
        ),
    )
    p_items_pull.add_argument("--xlsx", type=Path, help="Path to Excel workbook")
    p_items_pull.add_argument(
        "--language", action="append", dest="language", help="Add translation columns"
    )
    p_items_pull.add_argument("--languages", help="Comma-separated language codes")
    p_items_pull.add_argument("--scope", help=_SCOPE_HELP_ITEMS)

    p_items_preview = items_subparsers.add_parser("preview", help="Show workbook diffs")
    _add_common_args(p_items_preview, include_xlsx=True)
    p_items_preview.add_argument("--detailed", action="store_true", help="Full diffs")
    p_items_preview.add_argument("--embedded-data-only", action="store_true")
    p_items_preview.add_argument("--scope", help=_SCOPE_HELP_ITEMS)
    p_items_preview.add_argument(
        "--allow-externally-managed-qids",
        dest="allow_externally_managed_qids",
        help=(
            "Comma/space-separated QIDs (or SV_xxx:QIDyy) to allow editing options/subitems "
            "even when the question DataExportTag is externally managed. "
            "Overrides QSYNC_ITEMS_ALLOW_EXTERNALLY_MANAGED_QIDS."
        ),
    )
    p_items_preview.add_argument(
        "--allow-drift",
        action="store_true",
        help="Allow preview against a drifted cache without prompting",
    )

    p_items_stage = items_subparsers.add_parser(
        "stage", help="Stage changes to local cache"
    )
    _add_common_args(p_items_stage, include_xlsx=True)
    p_items_stage.add_argument("--yes", action="store_true")
    p_items_stage.add_argument("--embedded-data-only", action="store_true")
    p_items_stage.add_argument("--allow-dangerous", action="store_true")
    p_items_stage.add_argument("--scope", help=_SCOPE_HELP_ITEMS)
    p_items_stage.add_argument(
        "--allow-externally-managed-qids",
        dest="allow_externally_managed_qids",
        help=(
            "Comma/space-separated QIDs (or SV_xxx:QIDyy) to allow staging/pushing "
            "option/subitem edits for externally managed questions. "
            "Overrides QSYNC_ITEMS_ALLOW_EXTERNALLY_MANAGED_QIDS."
        ),
    )
    p_items_stage.add_argument(
        "--allow-drift",
        action="store_true",
        help="Proceed even if cached survey differs from the live API",
    )

    p_items_repair_edf = items_subparsers.add_parser(
        "repair-edf",
        help="Repair only the Embedded_Data sheet in the workbook",
    )
    p_items_repair_edf.add_argument(
        "--survey-id", help="Target survey ID (omit to select interactively)"
    )
    p_items_repair_edf.add_argument("--xlsx", type=Path, help="Path to Excel workbook")
    p_items_repair_edf.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the repair without writing the workbook",
    )
    p_items_repair_edf.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Refresh cached survey JSON from the API before repair",
    )
    p_items_repair_edf.add_argument(
        "--retain-backups",
        type=int,
        default=5,
        help="Number of `.bak` repair backups to retain (default: 5)",
    )
    p_items_repair_edf.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt when applying the repair",
    )

    p_items_inspect = items_subparsers.add_parser(
        "inspect",
        help="Inspect a single question (QID) from the cached survey definition",
    )
    p_items_inspect.add_argument(
        "--survey-id",
        help="Target survey ID (omit to select interactively)",
    )
    p_items_inspect.add_argument(
        "--qid",
        help="Question ID to inspect (omit to select interactively from SurveyFlow)",
    )
    p_items_inspect.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh cached survey definition from API before inspecting",
    )

    p_items_edit = items_subparsers.add_parser(
        "edit",
        help="Terminal-first structural edits (choices/options and answers/subitems)",
    )
    p_items_edit.add_argument(
        "--survey-id",
        help="Target survey ID (omit to select interactively)",
    )
    p_items_edit.add_argument(
        "--qid",
        help="Question ID (omit to select interactively from SurveyFlow)",
    )
    p_items_edit.add_argument(
        "--target",
        choices=["choices", "answers", "subitems", "question", "question-text"],
        default="choices",
        help="Edit target (choices/options, answers/subitems, or question text)",
    )
    p_items_edit.add_argument(
        "--action",
        choices=["add", "edit", "remove"],
        help="Action to perform (omit to run interactive wizard)",
    )
    p_items_edit.add_argument(
        "--id",
        dest="item_id",
        help="Choice/Answer ID to edit/remove (required for non-interactive edit/remove)",
    )
    p_items_edit.add_argument(
        "--text",
        help="HTML for add/edit (required for non-interactive add/edit)",
    )
    p_items_edit.add_argument(
        "--text-file",
        help="Read --text content from a file (useful for multiline edits).",
    )
    p_items_edit.add_argument(
        "--text-format",
        choices=["html", "md"],
        help="Input format for --target question (default: md)",
    )
    p_items_edit.add_argument(
        "--yes",
        action="store_true",
        help="Non-interactive mode (skip prompts); still requires allow flags for destructive actions",
    )
    p_items_edit.add_argument(
        "--allow-delete",
        action="store_true",
        help="Allow destructive deletes (required for non-interactive removes; re-checked at push time)",
    )
    p_items_edit.add_argument(
        "--ignore-workbook-drift",
        action="store_true",
        help="Proceed even if the Excel workbook differs from the refreshed cache (dangerous)",
    )
    p_items_edit.add_argument(
        "--experimental-unsupported",
        action="store_true",
        help="Allow editing unsupported question types (experimental; always logged)",
    )

    p_items_push = items_subparsers.add_parser(
        "push", help="Push staged changes to Qualtrics"
    )
    p_items_push.add_argument(
        "--survey-id", help="Target survey ID (omit to select interactively)"
    )
    p_items_push.add_argument("--yes", action="store_true")
    p_items_push.add_argument("--force-live", action="store_true")
    p_items_push.add_argument("--force-preview", action="store_true")
    p_items_push.add_argument("--no-publish", action="store_true")
    p_items_push.add_argument(
        "--allow-delete",
        action="store_true",
        help="Allow destructive structural deletes if staged (enforced at push time)",
    )
    p_items_push.add_argument("--dry-run", action="store_true")
    p_items_push.add_argument("--scope", help=_SCOPE_HELP_ITEMS)
    p_items_push.add_argument(
        "--allow-externally-managed-qids",
        dest="allow_externally_managed_qids",
        help=(
            "Comma/space-separated QIDs (or SV_xxx:QIDyy) to allow pushing option/subitem "
            "edits for externally managed questions. "
            "Overrides QSYNC_ITEMS_ALLOW_EXTERNALLY_MANAGED_QIDS."
        ),
    )
    p_items_push.add_argument(
        "--allow-drift",
        action="store_true",
        help="Proceed even if cached survey differs from the live API",
    )
    p_items_push.add_argument(
        "--use-pending",
        action="store_true",
        default=None,
        help="If staged changes exist and Excel differs, push staged changes instead of re-staging from Excel",
    )

    # sync command (orchestrator)
    p_sync = subparsers.add_parser(
        "sync",
        help="Orchestrate multi-dimension sync for one or more surveys",
    )
    p_sync.add_argument(
        "--survey-id",
        help="Target survey ID (omit to scan all focal surveys)",
    )
    p_sync.add_argument(
        "--tui",
        action="store_true",
        help="Launch Textual sync wizard (requires TUI dependencies; keeps default sync flow unchanged).",
    )
    p_sync.add_argument(
        "--all",
        action="store_true",
        help="Process all focal surveys without prompting (for automation)",
    )
    p_sync.add_argument(
        "--dimensions",
        help="Comma-separated dimensions to sync (default: auto-detect)",
    )
    p_sync.add_argument(
        "--scope",
        help=_SCOPE_HELP_SYNC,
    )
    p_sync.add_argument(
        "--per-dimension",
        action="store_true",
        help="Preview and approve each dimension separately (default: batch per-survey)",
    )
    p_sync.add_argument(
        "--yes",
        action="store_true",
        help="Skip all confirmation prompts (non-interactive)",
    )
    p_sync.add_argument(
        "--pending-action",
        choices=["push", "discard", "abort"],
        default="abort",
        help="If staged pending changes exist when running with --yes: push/discard/abort (default: abort)",
    )
    p_sync.add_argument(
        "--force-live",
        action="store_true",
        help="Force push despite live responses",
    )
    p_sync.add_argument(
        "--force-preview",
        action="store_true",
        help="Suppress preview-only response warnings",
    )
    p_sync.add_argument(
        "--skip-publish",
        action="store_true",
        help="Skip auto-publish step (no version snapshot)",
    )
    p_sync.add_argument(
        "--refresh-workbooks",
        action="store_true",
        help="Refresh Excel workbooks after successful sync (runs qsync items pull)",
    )
    p_sync.add_argument(
        "--allow-externally-managed-qids",
        dest="allow_externally_managed_qids",
        help=(
            "Comma/space-separated QIDs (or SV_xxx:QIDyy) to allow staging/pushing "
            "option/subitem edits for externally managed questions during sync. "
            "Overrides QSYNC_ITEMS_ALLOW_EXTERNALLY_MANAGED_QIDS."
        ),
    )
    p_sync.add_argument(
        "--skip-refresh",
        action="store_true",
        help="(Legacy/deprecated) Refresh is disabled by default; use --refresh-workbooks to enable",
    )
    p_sync.add_argument(
        "--allow-drift",
        action="store_true",
        help="Proceed even if cached survey differs from the live API",
    )
    p_sync.add_argument(
        "--allow-skip-embedded",
        action="store_true",
        help="Allow sync to proceed when Embedded_Data is invalid by skipping embedded defaults",
    )
    p_sync.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON when blocked by pending changes",
    )
    p_sync.add_argument(
        "--fix",
        help=(
            "Optional auto-repair selector before sync. "
            "Use `safe`/`all-safe` to apply all safe repairs, "
            "or `type:<ISSUE_TYPE>` to repair one issue class "
            "(for example `type:FLOW_NOT_INITIALIZED`)."
        ),
    )

    # export command group (alias for survey export-translation)
    p_export = subparsers.add_parser(
        "export",
        help="Export survey content for review (group)",
    )
    export_subs = p_export.add_subparsers(
        dest="export_command",
        required=True,
        metavar="COMMAND",
    )
    p_export_survey = export_subs.add_parser(
        "survey",
        help="Export survey content (alias for `qsync survey export-translation`)",
    )
    from .cli_survey import _add_export_translation_args, handle_export_translation

    _add_export_translation_args(p_export_survey)
    p_export_survey.set_defaults(func=handle_export_translation)

    # survey command group
    from .cli_survey import register_survey_commands

    register_survey_commands(subparsers)

    # prolific command group
    from .cli_prolific import register_prolific_commands

    register_prolific_commands(subparsers)

    # logs command group
    from .cli_logs import register_logs_commands

    register_logs_commands(subparsers)

    # js command group
    p_js = subparsers.add_parser(
        "js",
        help="Manage Qualtrics QuestionJS via the mapping CSV (group)",
    )
    js_subparsers = p_js.add_subparsers(
        dest="js_command",
        required=True,
        metavar="COMMAND",
    )

    # js pull
    p_js_pull = js_subparsers.add_parser(
        "pull",
        help="Rebuild survey_qid_js_map.csv and ensure mappings exist",
    )
    _add_js_common_args(
        p_js_pull,
        survey_id_action="append",
        survey_id_help=(
            "Target Qualtrics Survey ID(s) (repeatable/comma-separated; omit to select interactively)"
        ),
    )
    p_js_pull.add_argument(
        "--dry-run",
        action="store_true",
        help="Show a summary without writing the CSV",
    )

    # js preview
    p_js_preview = js_subparsers.add_parser(
        "preview",
        help="Preview differences between local JS and cached QuestionJS",
    )
    _add_js_common_args(p_js_preview)
    p_js_preview.add_argument(
        "--show-equal",
        action="store_true",
        help="Include matches with no differences",
    )
    p_js_preview.add_argument(
        "--detailed",
        action="store_true",
        help="Print unified diffs for each pair",
    )
    p_js_preview.add_argument(
        "--scope",
        help=_SCOPE_HELP_JS,
    )
    p_js_preview.add_argument(
        "--allow-drift",
        action="store_true",
        help="Allow preview against a drifted cache without prompting",
    )

    # js stage (renamed from apply)
    p_js_stage = js_subparsers.add_parser(
        "stage",
        help="Stage local QuestionJS changes into pending (no cache mutation)",
    )
    _add_js_common_args(p_js_stage)
    p_js_stage.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute staged entries without writing pending changes",
    )
    p_js_stage.add_argument(
        "--create-missing",
        action="store_true",
        help="Create QuestionJS blocks when they are missing",
    )
    p_js_stage.add_argument(
        "--allow-diff",
        action="store_true",
        help="Overwrite cached JS even when substantive diffs exist",
    )
    p_js_stage.add_argument(
        "--no-include-match",
        action="store_true",
        help="Skip syncing when cached JS already matches",
    )
    p_js_stage.add_argument(
        "--allow-drift",
        action="store_true",
        help="Allow staging against a drifted cache without prompting",
    )
    p_js_stage.add_argument(
        "--scope",
        help=_SCOPE_HELP_JS,
    )

    # js apply (legacy alias for backward compatibility)
    p_js_apply = js_subparsers.add_parser(
        "apply",
        help="[DEPRECATED: use 'stage'] Stage local QuestionJS changes into pending",
    )
    _add_js_common_args(p_js_apply)
    p_js_apply.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute staged entries without writing pending changes",
    )
    p_js_apply.add_argument(
        "--create-missing",
        action="store_true",
        help="Create QuestionJS blocks when they are missing",
    )
    p_js_apply.add_argument(
        "--allow-diff",
        action="store_true",
        help="Overwrite cached JS even when substantive diffs exist",
    )
    p_js_apply.add_argument(
        "--no-include-match",
        action="store_true",
        help="Skip syncing when cached JS already matches",
    )
    p_js_apply.add_argument(
        "--allow-drift",
        action="store_true",
        help="Allow staging against a drifted cache without prompting",
    )

    # js push
    p_js_push = js_subparsers.add_parser(
        "push",
        help="Push cached QuestionJS for mapped QIDs to Qualtrics",
    )
    _add_js_common_args(p_js_push)
    p_js_push.add_argument(
        "--include-trash",
        action="store_true",
        help="Also push QIDs that live in Trash blocks",
    )
    p_js_push.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which QIDs would be pushed without calling the API",
    )
    p_js_push.add_argument(
        "--push-all",
        action="store_true",
        help="Ignore staged JS and push all mapped QIDs (still filtered by include flags).",
    )
    p_js_push.add_argument(
        "--force-live",
        action="store_true",
        help="Allow JS pushes even if finished responses exist",
    )
    p_js_push.add_argument(
        "--force-preview",
        action="store_true",
        help="Force push to preview database even with responses",
    )
    p_js_push.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompts for JS pushes",
    )
    p_js_push.add_argument(
        "--no-publish",
        action="store_true",
        help="Skip publishing the survey after pushing QuestionJS updates",
    )
    p_js_push.add_argument(
        "--scope",
        help=_SCOPE_HELP_JS,
    )
    p_js_push.add_argument(
        "--allow-drift",
        action="store_true",
        help="Proceed even if cached survey differs from the live API",
    )

    # eos command group
    p_eos = subparsers.add_parser(
        "eos",
        help="Manage Qualtrics EndSurvey (EOS) library messages (group)",
    )
    eos_subparsers = p_eos.add_subparsers(
        dest="eos_command",
        required=True,
        metavar="COMMAND",
    )

    def _add_eos_common_args(
        parser: argparse.ArgumentParser,
        *,
        survey_id_action: str | None = None,
        survey_id_help: str | None = None,
    ) -> None:
        survey_kwargs: dict[str, object] = {}
        if survey_id_action:
            survey_kwargs["action"] = survey_id_action
        parser.add_argument(
            "--survey-id",
            dest="survey_id",
            help=survey_id_help
            or "Target survey ID (omit to select interactively)",
            **survey_kwargs,
        )
        parser.add_argument(
            "--allow-shared-message-edit",
            action="store_true",
            help="Allow edits even if a library message is detected as shared (local scan only).",
        )
        parser.add_argument(
            "--include-backups-scan",
            action="store_true",
            help="Also scan surveys/backups when detecting shared message usage (local-only).",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Skip interactive confirmations (required for push).",
        )

    # eos pull
    p_eos_pull = eos_subparsers.add_parser(
        "pull",
        help="Pull EOS library messages referenced by a survey into contents/",
    )
    _add_eos_common_args(
        p_eos_pull,
        survey_id_action="append",
        survey_id_help=(
            "Target survey ID(s) (repeatable/comma-separated; omit to select interactively)"
        ),
    )

    # eos preview
    p_eos_preview = eos_subparsers.add_parser(
        "preview",
        help="Preview differences between local EOS message files and live API content",
    )
    _add_eos_common_args(p_eos_preview)
    p_eos_preview.add_argument(
        "--detailed",
        action="store_true",
        help="Include unified diffs for changed keys",
    )
    p_eos_preview.add_argument(
        "--allow-drift",
        action="store_true",
        help="Allow preview against a drifted cache without prompting",
    )
    p_eos_preview.add_argument(
        "--scope",
        help=_SCOPE_HELP_EOS,
    )

    # eos repair
    p_eos_repair = eos_subparsers.add_parser(
        "repair",
        help="Re-fetch EOS library messages for a survey and update local files",
    )
    _add_eos_common_args(p_eos_repair)

    # eos stage (renamed from apply)
    p_eos_stage = eos_subparsers.add_parser(
        "stage",
        help="Stage EOS message pushes under surveys/pending/eos/ (no API writes)",
    )
    _add_eos_common_args(p_eos_stage)
    p_eos_stage.add_argument(
        "--allow-destructive",
        action="store_true",
        help="Allow destructive key deletions (missing message keys) for push.",
    )
    p_eos_stage.add_argument(
        "--scope",
        help=_SCOPE_HELP_EOS,
    )

    # eos apply (legacy alias for backward compatibility)
    p_eos_apply = eos_subparsers.add_parser(
        "apply",
        help="[DEPRECATED: use 'stage'] Stage EOS message pushes under surveys/pending/eos/ (no API writes)",
    )
    _add_eos_common_args(p_eos_apply)
    p_eos_apply.add_argument(
        "--allow-destructive",
        action="store_true",
        help="Allow destructive key deletions (missing message keys) for push.",
    )

    # eos push
    p_eos_push = eos_subparsers.add_parser(
        "push",
        help="Push staged EOS message updates to Qualtrics (requires --yes)",
    )
    _add_eos_common_args(p_eos_push)
    p_eos_push.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which messages would be pushed without calling the API",
    )
    p_eos_push.add_argument(
        "--force-live",
        action="store_true",
        help="Allow pushes even if finished responses exist",
    )
    p_eos_push.add_argument(
        "--force-preview",
        action="store_true",
        help="Force push to preview database even with responses",
    )
    p_eos_push.add_argument(
        "--allow-drift",
        action="store_true",
        help="Proceed even if cached EOS messages differ from the live API",
    )
    p_eos_push.add_argument(
        "--no-publish",
        action="store_true",
        help="Skip publishing the survey after pushing EOS updates",
    )
    p_eos_push.add_argument(
        "--scope",
        help=_SCOPE_HELP_EOS,
    )

    # eos references (local scan)
    p_eos_refs = eos_subparsers.add_parser(
        "references",
        help="List cached surveys that reference a given EOS library message (local scan)",
    )
    p_eos_refs.add_argument("--library-id", required=True, dest="library_id")
    p_eos_refs.add_argument("--message-id", required=True, dest="message_id")
    p_eos_refs.add_argument(
        "--include-backups-scan",
        action="store_true",
        help="Also scan surveys/backups (local-only).",
    )
    p_eos_refs.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON to stdout.",
    )

    # eos clone-shared (extension)
    p_eos_clone_shared = eos_subparsers.add_parser(
        "clone-shared",
        help="Clone shared EOS library messages and rewrite SurveyFlow to reference the clones (API writes)",
    )
    _add_eos_common_args(p_eos_clone_shared)
    p_eos_clone_shared.add_argument(
        "--allow-non-smoke",
        action="store_true",
        help="Allow running on surveys whose names do not include 'smoke' (dangerous).",
    )
    p_eos_clone_shared.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be cloned/rewired without API writes.",
    )
    p_eos_clone_shared.add_argument(
        "--allow-drift",
        action="store_true",
        help="Proceed even if cached survey differs from the live API.",
    )
    p_eos_clone_shared.add_argument(
        "--no-publish",
        action="store_true",
        help="Skip publishing the survey after rewriting SurveyFlow.",
    )

    # flow command group
    p_flow = subparsers.add_parser(
        "flow",
        help="Manage survey flow (branching logic, block ordering, routing)",
    )
    flow_subparsers = p_flow.add_subparsers(dest="flow_command", required=True)

    def _add_flow_common_args(
        parser: argparse.ArgumentParser,
        *,
        survey_id_action: str | None = None,
        survey_id_help: str | None = None,
    ) -> None:
        survey_kwargs: dict[str, object] = {}
        if survey_id_action:
            survey_kwargs["action"] = survey_id_action
        parser.add_argument(
            "--survey-id",
            dest="survey_id",
            help=survey_id_help
            or "Target survey ID (omit to select interactively)",
            **survey_kwargs,
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Skip interactive confirmations.",
        )

    # flow pull
    p_flow_pull = flow_subparsers.add_parser(
        "pull",
        help="Pull survey flow from Qualtrics and save as YAML",
    )
    _add_flow_common_args(
        p_flow_pull,
        survey_id_action="append",
        survey_id_help=(
            "Target survey ID(s) (repeatable/comma-separated; omit to select interactively)"
        ),
    )
    p_flow_pull.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing YAML even if it has local changes",
    )

    # flow preview
    p_flow_preview = flow_subparsers.add_parser(
        "preview",
        help="Preview differences between local flow YAML and cached baseline",
    )
    _add_flow_common_args(p_flow_preview)
    p_flow_preview.add_argument(
        "--verbose",
        action="store_true",
        help="Include detailed diff output with old/new values",
    )
    p_flow_preview.add_argument(
        "--visual",
        action="store_true",
        help="Generate Mermaid diagrams for visual diff",
    )

    # flow stage
    p_flow_stage = flow_subparsers.add_parser(
        "stage",
        help="Stage flow changes into pending cache (no API writes)",
    )
    _add_flow_common_args(p_flow_stage)
    p_flow_stage.add_argument(
        "--allow-drift",
        action="store_true",
        help="Allow staging even if remote has drifted",
    )

    # flow push
    p_flow_push = flow_subparsers.add_parser(
        "push",
        help="Push staged flow changes to Qualtrics",
    )
    _add_flow_common_args(p_flow_push)
    p_flow_push.add_argument(
        "--force-live",
        action="store_true",
        help="Allow pushes even if finished responses exist",
    )
    p_flow_push.add_argument(
        "--force-preview",
        action="store_true",
        help="Force push to preview database even with responses",
    )
    p_flow_push.add_argument(
        "--allow-drift",
        action="store_true",
        help="Proceed even if baseline differs from the live API",
    )
    p_flow_push.add_argument(
        "--no-publish",
        action="store_true",
        help="Skip publishing the survey after pushing flow updates",
    )

    # translations command group
    p_translations = subparsers.add_parser(
        "translations",
        help="Manage survey translations (group; includes languages)",
    )
    p_translations.set_defaults(legacy_translations=False)
    translations_subparsers = p_translations.add_subparsers(
        dest="translations_command",
        required=True,
        metavar="COMMAND",
    )

    # translations languages
    p_trans_lang = translations_subparsers.add_parser(
        "languages",
        help="List or enable survey languages (group)",
    )
    trans_lang_subs = p_trans_lang.add_subparsers(
        dest="translations_languages_command",
        required=True,
        metavar="COMMAND",
    )
    p_trans_lang_list = trans_lang_subs.add_parser(
        "list",
        help="List enabled languages for a survey",
    )
    p_trans_lang_list.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Target survey ID (omit to select interactively)",
    )
    p_trans_lang_ensure = trans_lang_subs.add_parser(
        "ensure",
        help="Ensure languages are enabled (adds missing languages only)",
    )
    p_trans_lang_ensure.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Target survey ID (omit to select interactively)",
    )
    p_trans_lang_ensure.add_argument(
        "--language",
        action="append",
        dest="language",
        help="Language code to enable (repeatable)",
    )
    p_trans_lang_ensure.add_argument(
        "--languages",
        help="Comma-separated language codes to enable",
    )
    p_trans_lang_ensure.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview language changes without writing",
    )
    p_trans_lang_set = trans_lang_subs.add_parser(
        "set",
        help="Replace enabled languages (overwrites AvailableLanguages)",
    )
    p_trans_lang_set.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Target survey ID (omit to select interactively)",
    )
    p_trans_lang_set.add_argument(
        "--language",
        action="append",
        dest="language",
        help="Language code to set (repeatable)",
    )
    p_trans_lang_set.add_argument(
        "--languages",
        help="Comma-separated language codes to set",
    )
    p_trans_lang_set.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview language changes without writing",
    )

    # translations preview
    p_trans_preview = translations_subparsers.add_parser(
        "preview",
        help="Preview workbook vs cached survey definition translations",
    )
    p_trans_preview.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Target survey ID (omit to select interactively)",
    )
    p_trans_preview.add_argument(
        "--language",
        action="append",
        dest="language",
        help="Language code to preview (repeatable)",
    )
    p_trans_preview.add_argument(
        "--languages",
        help="Comma-separated language codes to preview",
    )
    p_trans_preview.add_argument(
        "--allow-drift",
        action="store_true",
        help="Allow preview against a drifted cache without prompting",
    )
    p_trans_preview.add_argument(
        "--detailed",
        action="store_true",
        help="Include unified diffs for changed translation keys",
    )
    p_trans_preview.add_argument(
        "--scope",
        help=_SCOPE_HELP_TRANSLATIONS,
    )

    # translations pull (cache refresh alias)
    p_trans_pull = translations_subparsers.add_parser(
        "pull",
        help="Refresh cached survey definition (translations live in the survey definition)",
    )
    p_trans_pull.add_argument(
        "--survey-id",
        action="append",
        dest="survey_id",
        help="Target survey ID(s) (repeatable/comma-separated; omit to select interactively)",
    )
    p_trans_pull.add_argument(
        "--language",
        action="append",
        dest="language",
        help="(ignored) Kept for compatibility with legacy translation map pulls",
    )
    p_trans_pull.add_argument(
        "--languages",
        help="(ignored) Comma-separated language codes (legacy compatibility)",
    )
    p_trans_pull.add_argument(
        "--account",
        help=(
            "Use credentials from `.env.<account>` under the workspace root "
            "(writes cache under surveys/.<account>/ by default)."
        ),
    )

    # translations apply (legacy alias for stage)
    p_trans_apply = translations_subparsers.add_parser(
        "apply",
        help="(deprecated) Stage workbook translations (use `qsync translations stage`)",
    )
    p_trans_apply.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Target survey ID (omit to select interactively)",
    )
    p_trans_apply.add_argument(
        "--language",
        action="append",
        dest="language",
        help="Language code to apply (repeatable)",
    )
    p_trans_apply.add_argument(
        "--languages",
        help="Comma-separated language codes to apply",
    )
    p_trans_apply.add_argument(
        "--allow-drift",
        action="store_true",
        help="Allow staging against a drifted cache without prompting",
    )
    p_trans_apply.add_argument(
        "--yes",
        action="store_true",
        help="No-op (kept for CLI compatibility with `stage`)",
    )
    p_trans_apply.add_argument(
        "--scope",
        help=_SCOPE_HELP_TRANSLATIONS,
    )

    # translations stage (preferred)
    p_trans_stage = translations_subparsers.add_parser(
        "stage",
        help="Stage workbook translations into pending changes",
    )
    p_trans_stage.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Target survey ID (omit to select interactively)",
    )
    p_trans_stage.add_argument(
        "--language",
        action="append",
        dest="language",
        help="Language code to stage (repeatable)",
    )
    p_trans_stage.add_argument(
        "--languages",
        help="Comma-separated language codes to stage",
    )
    p_trans_stage.add_argument(
        "--allow-drift",
        action="store_true",
        help="Allow staging against a drifted cache without prompting",
    )
    p_trans_stage.add_argument(
        "--yes",
        action="store_true",
        help="Skip prompts (staging is non-interactive by default)",
    )
    p_trans_stage.add_argument(
        "--scope",
        help=_SCOPE_HELP_TRANSLATIONS,
    )

    # translations doctor
    p_trans_doctor = translations_subparsers.add_parser(
        "doctor",
        help="Run translation validation checks",
    )
    p_trans_doctor.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Target survey ID (omit to select interactively)",
    )
    p_trans_doctor.add_argument(
        "--language",
        action="append",
        dest="language",
        help="Language code to check (repeatable)",
    )
    p_trans_doctor.add_argument(
        "--languages",
        help="Comma-separated language codes to check",
    )
    p_trans_doctor.add_argument(
        "--workbook",
        help="Workbook path to validate translation cells (optional; defaults to survey workbook)",
    )

    # translations drift
    p_trans_drift = translations_subparsers.add_parser(
        "drift",
        help="Report drift between repo and Qualtrics translations",
    )
    p_trans_drift.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Target survey ID (omit to select interactively)",
    )
    p_trans_drift.add_argument(
        "--language",
        action="append",
        dest="language",
        help="Language code to inspect (repeatable)",
    )
    p_trans_drift.add_argument(
        "--languages",
        help="Comma-separated language codes to inspect",
    )

    # translations pack
    p_trans_pack = translations_subparsers.add_parser(
        "pack",
        help="Create a translator pack zip (docx + translations)",
    )
    p_trans_pack.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Target survey ID (omit to select interactively)",
    )
    p_trans_pack.add_argument(
        "--languages",
        required=True,
        help="Comma-separated language codes to include",
    )
    p_trans_pack.add_argument(
        "--output",
        help="Output path for the pack zip (optional)",
    )
    p_trans_pack.add_argument(
        "--include-base",
        action="store_true",
        help="Include base language content in the pack",
    )
    p_trans_pack.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh the cached survey definition before building the pack",
    )
    p_trans_pack.add_argument(
        "--workbook",
        help="Workbook path to include in the pack (optional)",
    )
    p_trans_pack.add_argument(
        "--edf",
        action="append",
        dest="edf",
        help="Scenario filter embedded data (repeatable): KEY=VALUE",
    )
    p_trans_pack.add_argument(
        "--keep-staging",
        action="store_true",
        help="Keep staging directory for inspection (default: remove after zip)",
    )

    # translations push
    p_trans_push = translations_subparsers.add_parser(
        "push",
        help="Push staged translations via survey definition",
    )
    p_trans_push.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Target survey ID (omit to select interactively)",
    )
    p_trans_push.add_argument(
        "--language",
        action="append",
        dest="language",
        help="Language code to push (repeatable)",
    )
    p_trans_push.add_argument(
        "--languages",
        help="Comma-separated language codes to push",
    )
    p_trans_push.add_argument(
        "--mode",
        choices=["validate", "apply"],
        default="apply",
        help="(deprecated) validate: run checks only; apply: push to Qualtrics",
    )
    p_trans_push.add_argument(
        "--validate",
        action="store_const",
        const="validate",
        dest="mode",
        help="Run checks only (no API writes)",
    )
    p_trans_push.add_argument(
        "--dry-run",
        action="store_const",
        const="validate",
        dest="mode",
        help="Alias for --mode validate",
    )
    p_trans_push.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt when pushing",
    )
    p_trans_push.add_argument(
        "--detailed",
        action="store_true",
        help="Include unified diffs for changed translation keys",
    )
    p_trans_push.add_argument(
        "--force-live",
        action="store_true",
        help="Allow pushes even if finished responses exist",
    )
    p_trans_push.add_argument(
        "--force-preview",
        action="store_true",
        help="Allow pushes that affect preview/test responses without extra confirmation",
    )
    p_trans_push.add_argument(
        "--allow-drift",
        action="store_true",
        help="Proceed even if the cached survey definition differs from live",
    )
    p_trans_push.add_argument(
        "--no-publish",
        action="store_true",
        help="Skip publishing the survey after push",
    )
    p_trans_push.add_argument(
        "--use-pending",
        action="store_true",
        default=None,
        help="If staged changes exist and Excel differs, push staged changes instead of re-staging from Excel",
    )
    p_trans_push.add_argument(
        "--scope",
        help=_SCOPE_HELP_TRANSLATIONS,
    )

    # translations check-language
    p_trans_check_lang = translations_subparsers.add_parser(
        "check-language",
        help="Check if translations are in the correct language using language detection",
    )
    p_trans_check_lang.add_argument(
        "--survey-id",
        dest="survey_id",
        help="Target survey ID (omit to select interactively)",
    )
    p_trans_check_lang.add_argument(
        "--language",
        action="append",
        dest="language",
        help="Restrict checks to a language (repeatable).",
    )
    p_trans_check_lang.add_argument(
        "--languages",
        help="Comma-separated language codes to check (e.g., FR,NL,CS).",
    )
    p_trans_check_lang.add_argument(
        "--edf",
        action="append",
        dest="edf",
        help="Scenario filter embedded data (repeatable): KEY=VALUE; restricts checks to reachable flow paths",
    )
    p_trans_check_lang.add_argument(
        "--min-confidence",
        type=float,
        default=0.85,
        help="Minimum confidence for accepting the target language (default: 0.85).",
    )
    p_trans_check_lang.add_argument(
        "--min-margin",
        type=float,
        default=0.15,
        help="Minimum margin vs runner-up for confident acceptance (default: 0.15).",
    )
    p_trans_check_lang.add_argument(
        "--skip-meta",
        action="store_true",
        help="Skip system/meta questions (Timing/Meta/etc.).",
    )
    p_trans_check_lang.add_argument(
        "--skip-js",
        action="store_true",
        help="Skip JavaScript COPY blocks.",
    )
    p_trans_check_lang.add_argument(
        "--skip-eos",
        action="store_true",
        help="Skip End-of-Survey messages.",
    )
    dedupe_group = p_trans_check_lang.add_mutually_exclusive_group()
    dedupe_group.add_argument(
        "--dedupe",
        action="store_true",
        help="Deduplicate identical issue text across QIDs (default).",
    )
    dedupe_group.add_argument(
        "--no-dedupe",
        action="store_true",
        help="Show each issue row per QID without deduplication.",
    )
    single_word_group = p_trans_check_lang.add_mutually_exclusive_group()
    single_word_group.add_argument(
        "--allow-single-word",
        action="store_true",
        help="Allow single-word strings to bypass detection (default).",
    )
    single_word_group.add_argument(
        "--disallow-single-word",
        action="store_true",
        help="Require detection even for single-word strings.",
    )

    # Help output ordering: keep core workflows first; push deprecated aliases down.
    reorder_subparser_choices(
        subparsers,
        [
            # Setup / onboarding
            "onboard",
            # Survey inventory + admin
            "survey",
            "prolific",
            # Daily workflows
            "sync",
            "init",
            "preview",
            "apply",
            "push",
            "items",
            "translations",
            "flow",
            "js",
            "eos",
            # Utilities
            "export",
            "compare",
            "logs",
            "settings",
            "tui",
            "help",
            "doctor",
            "self-update",
        ],
    )

    reorder_subparser_choices(
        items_subparsers,
        [
            "pull",
            "preview",
            "stage",
            "push",
            "edit",
            "inspect",
            "repair-edf",
        ],
    )
    reorder_subparser_choices(
        js_subparsers,
        [
            "pull",
            "preview",
            "stage",
            "push",
        ],
    )
    hide_subparser_choices(js_subparsers, ["apply"])
    reorder_subparser_choices(
        eos_subparsers,
        [
            "pull",
            "preview",
            "repair",
            "stage",
            "push",
            "references",
            "clone-shared",
        ],
    )
    hide_subparser_choices(eos_subparsers, ["apply"])
    reorder_subparser_choices(
        translations_subparsers,
        [
            "languages",
            "pull",
            "preview",
            "stage",
            "push",
            "pack",
            "drift",
            "doctor",
            "check-language",
        ],
    )
    hide_subparser_choices(translations_subparsers, ["apply"])

    # Optional shell completion support (bash/zsh) via argcomplete.
    # Safe no-op when argcomplete is not installed.
    try:
        import argcomplete  # type: ignore[import-not-found]
    except Exception:
        argcomplete = None
    if argcomplete is not None:
        try:
            argcomplete.autocomplete(parser)
        except Exception:
            pass

    args = parser.parse_args(cleaned_argv)
    args._account_source = _ACCOUNT_CONTEXT_SOURCE
    if getattr(args, "root", None) is None and root_flag:
        args.root = root_flag
    if getattr(args, "env_path", None) is None and env_path_flag:
        args.env_path = env_path_flag
    if getattr(args, "color", None) is None and color_flag:
        args.color = color_flag
    if getattr(args, "allow_locked", False):
        os.environ["QSYNC_ALLOW_LOCKED"] = "1"
    allow_externally_managed_qids = getattr(args, "allow_externally_managed_qids", None)
    if allow_externally_managed_qids is not None:
        os.environ["QSYNC_ITEMS_ALLOW_EXTERNALLY_MANAGED_QIDS"] = str(
            allow_externally_managed_qids
        )

    from .terminal_colors import set_color_mode
    from .terminal_output import operation_timer, reset_timing_emitted

    if os.environ.get("NO_COLOR") is not None:
        set_color_mode("never")
    else:
        set_color_mode(getattr(args, "color", None) or "auto")

    reset_timing_emitted()
    if getattr(args, "json", False):
        os.environ["QSYNC_JSON_MODE"] = "1"

    _timer_cm = operation_timer(f"[qsync:{args.command}]")
    _timer_cm.__enter__()
    try:
        if args.command == "onboard":
            from .onboarding import run_onboard

            run_onboard(args)
            return

        if args.command == "self-update":
            _handle_self_update(args)
            return

        if args.command == "help":
            _handle_help(args)
            return

        if args.command == "tui":
            _handle_tui(args)
            return

        # First-run hint: suggest onboarding when a workspace-centric command is
        # executed outside a qsync workspace.
        try:
            from .config import resolve_root

            if _should_offer_workspace_onboard_hint(args):
                root_hint = resolve_root(required=False) or Path.cwd()
                required_dirs = _workspace_dirs_for_onboard_hint(args)
                missing_workspace = any(
                    not (root_hint / name).exists() for name in required_dirs
                )
                if missing_workspace:
                    from .interactive_menu import confirm, is_interactive

                    if is_interactive():
                        wants_onboard = confirm(
                            "No workspace found. Run `qsync onboard` now?",
                            default=True,
                        )
                        if wants_onboard:
                            from .onboarding import run_onboard

                            run_onboard(
                                argparse.Namespace(
                                    root=getattr(args, "root", None),
                                    datacenter=None,
                                    token=None,
                                    skip_gitignore=False,
                                    non_interactive=False,
                                )
                            )
                            print("✅ Onboarding complete. Re-run your command.")
                            return
                    print(
                        "ℹ️  No workspace found. Run `qsync onboard` to set up this workspace."
                    )
        except Exception:
            pass

        if args.command == "doctor":
            from .config import (
                ENV_PATH,
                ROOT,
                get_active_account,
                load_account_env,
                load_env,
                load_env_file,
                resolve_account_env_path,
                resolve_env_path,
                resolve_root,
                resolve_scoped_dir,
            )
            from .api_push import send_api_request
            from .interactive_menu import QUESTIONARY_AVAILABLE, should_use_questionary

            root = resolve_root(required=False) or ROOT
            account = (getattr(args, "account", None) or "").strip() or None
            if not account:
                account = get_active_account()
            if account:
                env_path = resolve_account_env_path(account, root=root)
            else:
                env_path = resolve_env_path(root=root) or ENV_PATH
            warnings: list[str] = []
            ok = True
            surveys_dir_base = root / "surveys"
            excel_dir_base = root / "excel"
            survey_js_dir_base = root / "survey_js"

            surveys_dir = resolve_scoped_dir("surveys", root=root, account=account)
            excel_dir = resolve_scoped_dir("excel", root=root, account=account)
            survey_js_dir = resolve_scoped_dir("survey_js", root=root, account=account)

            inventory_csv = surveys_dir / "inventory.csv"
            legacy_inventory_csv = surveys_dir / "qualtrics_surveys.csv"
            inventory_resolved = (
                inventory_csv
                if inventory_csv.exists()
                else (
                    legacy_inventory_csv
                    if legacy_inventory_csv.exists()
                    else inventory_csv
                )
            )

            if not surveys_dir_base.exists():
                ok = False
                warnings.append("surveys/ directory not found under root")
            if not excel_dir_base.exists():
                ok = False
                warnings.append("excel/ directory not found under root")
            if not survey_js_dir_base.exists():
                ok = False
                warnings.append("survey_js/ directory not found under root")
            if not inventory_resolved.exists():
                ok = False
                warnings.append(
                    "surveys/inventory.csv not found (run `qsync survey inventory`)"
                )
            elif (
                inventory_resolved == legacy_inventory_csv
                and not inventory_csv.exists()
            ):
                warnings.append(
                    "Using legacy inventory filename surveys/qualtrics_surveys.csv (preferred: surveys/inventory.csv)"
                )

            mapping_override = os.environ.get("QSYNC_MAPPING_CSV")
            mapping_workspace_surveys = (
                root / "surveys" / "qualtrics_api_key_mapping.csv"
            )
            mapping_workspace_appendices = (
                root / "appendices" / "qualtrics_api_key_mapping.csv"
            )
            if mapping_override:
                warnings.append(
                    f"Survey Master mapping override: QSYNC_MAPPING_CSV={mapping_override}"
                )
            elif mapping_workspace_surveys.exists():
                pass
            elif mapping_workspace_appendices.exists():
                warnings.append(
                    "Survey Master mapping file found under appendices/ (preferred: surveys/qualtrics_api_key_mapping.csv)"
                )
            else:
                warnings.append(
                    "Survey Master mapping file not found under surveys/qualtrics_api_key_mapping.csv (Survey Master will use packaged defaults; "
                    "override via --mapping-csv or QSYNC_MAPPING_CSV if you need a richer allowlist)."
                )

            # Credentials / config checks (local-only by default).
            if account:
                # Deterministic: account selection uses `.env.<account>` values (no env override).
                env = load_account_env(account, root=root)
                file_env = env
            else:
                file_env = load_env_file(env_path) if env_path else {}
                env = load_env(env_path)
            base_url = (env.get("QUALTRICS_BASE_URL") or "").strip()
            api_token = (
                env.get("X-API-TOKEN") or env.get("QUALTRICS_API_KEY") or ""
            ).strip()
            base_url_ok = bool(base_url)
            token_ok = bool(api_token)
            token_source = "missing"
            if account:
                token_source = "dotenv" if token_ok else "missing"
            else:
                if (os.environ.get("X-API-TOKEN") or "").strip() or (
                    os.environ.get("QUALTRICS_API_KEY") or ""
                ).strip():
                    token_source = "env"
                elif (file_env.get("X-API-TOKEN") or "").strip() or (
                    file_env.get("QUALTRICS_API_KEY") or ""
                ).strip():
                    token_source = "dotenv"
                elif token_ok:
                    token_source = "keyring"

            if not base_url_ok:
                ok = False
                warnings.append(
                    "QUALTRICS_BASE_URL missing (set in .env or environment; host only, e.g. iad1.qualtrics.com)"
                )
            if base_url and (
                base_url.startswith("http://") or base_url.startswith("https://")
            ):
                warnings.append(
                    "QUALTRICS_BASE_URL should be host-only (remove the scheme, e.g. use iad1.qualtrics.com)"
                )
            if not token_ok:
                ok = False
                warnings.append(
                    "Qualtrics API token missing (set X-API-TOKEN or QUALTRICS_API_KEY, or store it in keychain via keyring; then re-run `qsync doctor`)"
                )

            whoami_result: dict | None = None
            datacenter: str | None = None
            datacenter_mismatch: bool | None = None
            if getattr(args, "check_api", False) and base_url_ok and token_ok:
                try:
                    whoami_resp = send_api_request(
                        action="qsync.doctor.whoami",
                        method="GET",
                        base_url=base_url,
                        headers={
                            "Accept": "application/json",
                            "X-API-TOKEN": api_token,
                        },
                        path="whoami",
                        log_event=False,
                        timeout=15,
                    )
                    whoami_result = whoami_resp.json().get("result", {}) or {}
                    datacenter = (whoami_result.get("datacenter") or "").strip() or None
                    if datacenter and base_url:
                        # Flag mismatch only when base_url looks like a datacenter hostname.
                        # Typical form: "<datacenter>.qualtrics.com" (e.g. iad1.qualtrics.com).
                        left_label = base_url.split(".", 1)[0].strip().lower()
                        if re.fullmatch(r"[a-z]{2,5}\d+", left_label or ""):
                            datacenter_mismatch = (
                                left_label != datacenter.strip().lower()
                            )
                        else:
                            # If base_url isn't in datacenter-host form, we can't reliably assert mismatch.
                            datacenter_mismatch = None
                except Exception as exc:
                    ok = False
                    warnings.append(
                        f"Failed to call /whoami: {exc} (check QUALTRICS_BASE_URL/token)"
                    )

            # Additional guidance when base_url/token checks fail.
            if not base_url_ok or not token_ok:
                warnings.append("See: README.md#workspace-configuration")

            if getattr(args, "json", False):
                try:
                    cwd = Path.cwd()
                except FileNotFoundError:
                    cwd = None
                payload = {
                    "ok": ok,
                    "cwd": str(cwd) if cwd else None,
                    "root": str(root),
                    "account": account,
                    "env_path": str(env_path) if env_path else None,
                    "env_exists": bool(env_path and env_path.exists()),
                    "surveys_dir": str(surveys_dir),
                    "excel_dir": str(excel_dir),
                    "survey_js_dir": str(survey_js_dir),
                    "inventory_csv": str(inventory_resolved),
                    "inventory_csv_canonical": str(inventory_csv),
                    "inventory_csv_legacy": str(legacy_inventory_csv),
                    "tty": {"stdin": sys.stdin.isatty(), "stdout": sys.stdout.isatty()},
                    "questionary": {
                        "available": bool(QUESTIONARY_AVAILABLE),
                        "enabled": bool(should_use_questionary()),
                        "env": os.environ.get("QSYNC_USE_QUESTIONARY"),
                    },
                    "qualtrics_base_url": base_url or None,
                    "qualtrics_token_present": token_ok,
                    "qualtrics_token_source": token_source,
                    "check_api": bool(getattr(args, "check_api", False)),
                    "whoami": whoami_result,
                    "datacenter": datacenter,
                    "datacenter_mismatch": datacenter_mismatch,
                    "warnings": warnings,
                    "notes": [
                        "Most qsync commands require a workspace root.",
                        "Use --root (or QSYNC_ROOT) when running outside the workspace.",
                    ],
                }
                print(json.dumps(payload, ensure_ascii=False))
                if not ok:
                    raise SystemExit(2)
                return

            if not getattr(args, "quiet", False):
                try:
                    cwd = Path.cwd()
                except FileNotFoundError:
                    cwd = None
                print("[qsync:doctor]")
                print(f"  cwd:      {cwd if cwd else '(not available)'}")
                print(f"  root:     {root}")
                if account:
                    print(f"  account:  {account}")
                print(f"  env_path: {env_path if env_path else '(not resolved)'}")
                if env_path:
                    print(f"  env_exists: {env_path.exists()}")
                print(f"  surveys_dir: {surveys_dir}")
                print(f"  excel_dir:   {excel_dir}")
                print(f"  survey_js:   {survey_js_dir}")
                print(f"  inventory:   {inventory_resolved}")
                print(
                    f"  tty:         stdin={sys.stdin.isatty()} stdout={sys.stdout.isatty()}"
                )
                print(
                    f"  questionary: available={QUESTIONARY_AVAILABLE} enabled={should_use_questionary()} env={os.environ.get('QSYNC_USE_QUESTIONARY')}"
                )
                print(f"  qualtrics_base_url: {base_url if base_url else '(missing)'}")
                print(f"  qualtrics_token_present: {token_ok}")
                print(f"  qualtrics_token_source: {token_source}")
                if getattr(args, "check_api", False):
                    print(
                        f"  whoami_datacenter: {datacenter if datacenter else '(not available)'}"
                    )
                    if datacenter_mismatch is True:
                        print(
                            "  warn: Datacenter mismatch detected (base URL does not match /whoami datacenter)",
                            file=sys.stderr,
                        )
                for w in warnings:
                    print(f"  warn: {w}", file=sys.stderr)
                print("  notes:")
                print("    - Most qsync commands require a workspace root.")
                print(
                    "    - Use --root (or QSYNC_ROOT) when running outside the workspace."
                )
            if not ok:
                raise SystemExit(2)
            return

        if args.command == "eos":
            from .terminal_output import error, header, info, success, warn
            from .pending_stage import load_pending, clear_pending, EosPendingPayload
            from .eos_messages import (
                apply_eos_messages,
                clone_shared_eos_messages,
                confirm_shared_override,
                detect_shared_messages,
                extract_eos_message_refs,
                CloneSharedEosResult,
                get_eos_message_references,
                preview_eos_messages,
                pull_eos_messages,
                push_eos_messages,
            )
            from .qualtrics_client import load_cached_survey

            if args.eos_command == "references":
                contexts = get_eos_message_references(
                    library_id=args.library_id,
                    message_id=args.message_id,
                    include_backups_scan=bool(
                        getattr(args, "include_backups_scan", False)
                    ),
                )
                if getattr(args, "json", False):
                    print(
                        json.dumps(
                            {
                                "library_id": args.library_id,
                                "message_id": args.message_id,
                                "include_backups_scan": bool(
                                    getattr(args, "include_backups_scan", False)
                                ),
                                "references": contexts,
                            },
                            ensure_ascii=False,
                        )
                    )
                    return

                header(
                    "[qsync:eos]",
                    f"References for {args.library_id}/{args.message_id}:",
                )
                if not contexts:
                    info(
                        "[qsync:eos]",
                        "No references found in cached surveys (local scan).",
                    )
                    return
                for ctx in contexts:
                    sid = str(ctx.get("survey_id") or "")
                    flow_id = ctx.get("flow_id")
                    flow_part = f" flow_id={flow_id}" if flow_id else ""
                    print(f"- survey_id={sid}{flow_part} source={ctx.get('source')}")
                return

            allow_shared = bool(getattr(args, "allow_shared_message_edit", False))
            include_backups_scan = bool(getattr(args, "include_backups_scan", False))
            yes = bool(getattr(args, "yes", False))

            if args.eos_command == "pull":
                survey_ids = _prompt_for_survey_ids_if_needed(
                    getattr(args, "survey_id", None),
                    allow_all_surveys=True,
                )
                header("[qsync:eos]", "Pulling EOS messages...")
                failures = 0
                for survey_id in survey_ids:
                    try:
                        cache = load_cached_survey(survey_id)
                        refs = extract_eos_message_refs(survey_id, cache.payload)
                        shared = detect_shared_messages(
                            survey_id=survey_id,
                            refs={(r.library_id, r.message_id) for r in refs},
                            include_backups=include_backups_scan,
                        )
                        if shared and not allow_shared:
                            warn(
                                "[qsync:eos]",
                                f"{survey_id}: shared library message detected (local scan only). "
                                "Continuing for inspection only; apply/push will require "
                                "--allow-shared-message-edit.",
                            )
                        if allow_shared:
                            confirm_shared_override(shared=shared, yes=yes)
                        paths = pull_eos_messages(
                            survey_id=survey_id,
                            allow_shared=allow_shared,
                            include_backups_scan=include_backups_scan,
                        )
                    except Exception as e:
                        failures += 1
                        error("[qsync:eos]", f"{survey_id}: ERROR: {e}")
                        continue
                    if not paths:
                        info(
                            "[qsync:eos]",
                            f"{survey_id}: no EndSurvey DisplayMessage references found.",
                        )
                        continue
                    for p in paths:
                        success("[qsync:eos]", f"{survey_id}: pulled {p}")
                if failures:
                    raise SystemExit(2)
                return

            survey_id = _prompt_for_survey_id_if_needed(
                getattr(args, "survey_id", None),
                allow_all_surveys=False,
            )

            cache = load_cached_survey(survey_id)
            refs = extract_eos_message_refs(survey_id, cache.payload)
            shared = detect_shared_messages(
                survey_id=survey_id,
                refs={(r.library_id, r.message_id) for r in refs},
                include_backups=include_backups_scan,
            )

            if shared and not allow_shared and args.eos_command in {"pull", "preview"}:
                warn(
                    "[qsync:eos]",
                    "Shared library message detected (local scan only). Continuing for inspection only; "
                    "apply/push will require --allow-shared-message-edit.",
                )
            if allow_shared:
                confirm_shared_override(shared=shared, yes=yes)

            if args.eos_command == "clone-shared":
                header(
                    "[qsync:eos]", "Cloning shared EOS messages (rewire SurveyFlow)..."
                )
                if not shared:
                    info(
                        "[qsync:eos]",
                        "No shared EOS messages detected; nothing to clone.",
                    )
                    return
                if bool(getattr(args, "dry_run", False)):
                    print("[qsync:eos] Shared message(s) that would be cloned:")
                    for lib_id, msg_id in sorted(shared):
                        print(f"- {lib_id}/{msg_id}")
                    print(
                        "\n[qsync:eos] Dry run complete. Re-run without --dry-run to apply changes."
                    )
                    return
                try:
                    result: CloneSharedEosResult = clone_shared_eos_messages(
                        survey_id=survey_id,
                        include_backups_scan=include_backups_scan,
                        yes=yes,
                        allow_non_smoke=bool(getattr(args, "allow_non_smoke", False)),
                        allow_drift=bool(getattr(args, "allow_drift", False)),
                        publish=not bool(getattr(args, "no_publish", False)),
                    )
                except Exception as e:
                    error("[qsync:eos]", f"ERROR: {e}")
                    raise SystemExit(2)
                if not result.replacements:
                    info("[qsync:eos]", "No shared EOS messages were cloned.")
                    return
                for (lib_id, old_id), new_id in sorted(result.replacements.items()):
                    success("[qsync:eos]", f"Cloned: {lib_id}/{old_id} -> {new_id}")
                if result.updated_flow_ids:
                    info(
                        "[qsync:eos]",
                        f"Updated FlowID(s): {', '.join(result.updated_flow_ids)}",
                    )
                for p in result.pulled_paths:
                    success("[qsync:eos]", f"Pulled: {p}")
                return

            if args.eos_command == "preview":
                from .drift_check import confirm_preview_drift

                header("[qsync:eos]", "Previewing EOS message diffs...")
                try:

                    def _update_cache() -> None:
                        pull_eos_messages(
                            survey_id=survey_id,
                            allow_shared=allow_shared,
                            include_backups_scan=include_backups_scan,
                            check_drift=False,
                        )
                        info("[qsync:eos]", "Refreshed local EOS messages from API.")

                    confirm_preview_drift(
                        survey_id=survey_id,
                        dimension="eos",
                        allow_drift=bool(getattr(args, "allow_drift", False)),
                        interactive=sys.stdin.isatty(),
                        update_cache=_update_cache,
                    )
                    lines = preview_eos_messages(
                        survey_id=survey_id,
                        allow_shared=allow_shared,
                        detailed=bool(getattr(args, "detailed", False)),
                        include_backups_scan=include_backups_scan,
                        check_drift=False,
                    )
                except Exception as e:
                    error("[qsync:eos]", f"ERROR: {e}")
                    raise SystemExit(2)
                for ln in lines:
                    print(ln)
                return

            if args.eos_command == "repair":
                header("[qsync:eos]", "Repairing (re-fetching) EOS messages...")
                try:
                    paths = pull_eos_messages(
                        survey_id=survey_id,
                        allow_shared=allow_shared,
                        include_backups_scan=include_backups_scan,
                    )
                except Exception as e:
                    error("[qsync:eos]", f"ERROR: {e}")
                    raise SystemExit(2)
                if not paths:
                    info("[qsync:eos]", "No EndSurvey DisplayMessage references found.")
                    return
                for p in paths:
                    success("[qsync:eos]", f"Repaired: {p}")
                return

            if args.eos_command in ("stage", "apply"):
                # Emit deprecation warning for "apply"
                if args.eos_command == "apply":
                    from .terminal_output import warn

                    warn(
                        "[DEPRECATION] 'eos apply' is deprecated. Use 'eos stage' instead."
                    )

                header("[qsync:eos]", "Staging EOS pushes (no API writes)...")
                try:
                    record = apply_eos_messages(
                        survey_id=survey_id,
                        allow_shared=allow_shared,
                        allow_destructive=bool(
                            getattr(args, "allow_destructive", False)
                        ),
                        include_backups_scan=include_backups_scan,
                        scope_expr=getattr(args, "scope", None),
                    )
                except Exception as e:
                    error("[qsync:eos]", f"ERROR: {e}")
                    raise SystemExit(2)
                if (
                    record is None
                    or not isinstance(record.payload, EosPendingPayload)
                    or not record.payload.operations
                ):
                    clear_pending(survey_id, "eos")
                    info(
                        "[qsync:eos]", "No local EOS changes detected; nothing staged."
                    )
                    return
                success(
                    "[qsync:eos]",
                    f"Staged {len(record.payload.operations)} message(s) (pending schema v{getattr(record, 'schema_version', 1)})",
                )
                info(
                    "[qsync:eos]",
                    f"Pending: surveys/pending/eos/{survey_id}.json (schema v{getattr(record, 'schema_version', 1)})",
                )
                return

            if args.eos_command == "push":
                header("[qsync:eos]", "Pushing staged EOS messages...")
                record = load_pending(survey_id, "eos")
                if record is None or not isinstance(record.payload, EosPendingPayload):
                    error(
                        "[qsync:eos]",
                        "No pending EOS record found. Run 'qsync eos stage' first.",
                    )
                    raise SystemExit(2)
                info(
                    "[qsync:eos]",
                    f"Using pending schema v{getattr(record, 'schema_version', 1)} from surveys/pending/eos/{survey_id}.json",
                )
                dry_run = bool(getattr(args, "dry_run", False))
                try:
                    pushed = push_eos_messages(
                        survey_id=survey_id,
                        record=record,
                        allow_shared=allow_shared,
                        yes=yes,
                        include_backups_scan=include_backups_scan,
                        dry_run=dry_run,
                        force_live=getattr(args, "force_live", False),
                        force_preview=getattr(args, "force_preview", False),
                        interactive=not yes,
                        publish=not bool(getattr(args, "no_publish", False)),
                        allow_drift=bool(getattr(args, "allow_drift", False)),
                        scope_expr=getattr(args, "scope", None),
                    )
                    if dry_run:
                        info(
                            "[qsync:eos]",
                            f"Dry run: would push {len(pushed)} message(s).",
                        )
                        for lib_id, msg_id in pushed:
                            info("[qsync:eos]", f"Would push: {lib_id}/{msg_id}")
                        return

                    from .qualtrics_client import refresh_survey_cache

                    try:
                        refresh_survey_cache(survey_id)
                        clear_pending(survey_id, "eos")
                    except Exception as exc:
                        warn(
                            "[qsync:eos]",
                            f"Push succeeded but cache refresh failed: {exc}",
                        )
                except Exception as e:
                    error("[qsync:eos]", f"ERROR: {e}")
                    raise SystemExit(2)
                for lib_id, msg_id in pushed:
                    success("[qsync:eos]", f"Pushed: {lib_id}/{msg_id}")
                return

            error("[qsync:eos]", f"Unknown eos command: {args.eos_command}")
            raise SystemExit(2)

        # flow command dispatcher
        if args.command == "flow":
            from .terminal_output import error, header, info, success, warn
            from .dimensions import flow as flow_dimension

            yes = bool(getattr(args, "yes", False))
            interactive = sys.stdin.isatty() and not yes

            if args.flow_command == "pull":
                survey_ids = _prompt_for_survey_ids_if_needed(
                    getattr(args, "survey_id", None),
                    allow_all_surveys=True,
                )
                header("[qsync:flow]", "Pulling survey flow...")
                failures = 0
                exit_code = 1
                for survey_id in survey_ids:
                    try:
                        yaml_path = flow_dimension.pull(
                            survey_id,
                            force=bool(getattr(args, "force", False)),
                        )
                        success(
                            "[qsync:flow]",
                            f"{survey_id}: pulled flow to {yaml_path}",
                        )
                    except FileExistsError as e:
                        failures += 1
                        warn("[qsync:flow]", f"{survey_id}: {e}")
                    except Exception as e:
                        failures += 1
                        exit_code = 2
                        error("[qsync:flow]", f"{survey_id}: ERROR: {e}")
                if failures:
                    raise SystemExit(exit_code)
                return

            survey_id = _prompt_for_survey_id_if_needed(
                getattr(args, "survey_id", None),
                allow_all_surveys=False,
            )

            if args.flow_command == "preview":
                header("[qsync:flow]", "Previewing flow changes...")
                try:
                    changes = flow_dimension.preview(
                        survey_id,
                        verbose=bool(getattr(args, "verbose", False)),
                        visual=bool(getattr(args, "visual", False)),
                    )
                    if not changes:
                        info("[qsync:flow]", "No changes to preview")
                except Exception as e:
                    error("[qsync:flow]", f"ERROR: {e}")
                    raise SystemExit(2)
                return

            if args.flow_command == "stage":
                header("[qsync:flow]", "Staging flow changes...")
                try:
                    staged = flow_dimension.stage(
                        survey_id,
                        allow_drift=bool(getattr(args, "allow_drift", False)),
                        interactive=interactive,
                    )
                    if staged:
                        success("[qsync:flow]", "Flow changes staged successfully")
                    else:
                        warn("[qsync:flow]", "Staging failed or no changes to stage")
                        raise SystemExit(1)
                except SystemExit:
                    raise
                except Exception as e:
                    error("[qsync:flow]", f"ERROR: {e}")
                    raise SystemExit(2)
                return

            if args.flow_command == "push":
                header("[qsync:flow]", "Pushing staged flow changes...")
                try:
                    pushed = flow_dimension.push(
                        survey_id,
                        interactive=interactive,
                        force_live=bool(getattr(args, "force_live", False)),
                        force_preview=bool(getattr(args, "force_preview", False)),
                        auto_yes=yes,
                        allow_drift=bool(getattr(args, "allow_drift", False)),
                        skip_publish=bool(getattr(args, "no_publish", False)),
                    )
                    if pushed:
                        success("[qsync:flow]", "Flow pushed successfully")
                    else:
                        warn("[qsync:flow]", "Push failed")
                        raise SystemExit(1)
                except SystemExit:
                    raise
                except Exception as e:
                    error("[qsync:flow]", f"ERROR: {e}")
                    raise SystemExit(2)
                return

            error("[qsync:flow]", f"Unknown flow command: {args.flow_command}")
            raise SystemExit(2)

        # translations command dispatcher
        if args.command == "translations":
            from .terminal_output import error
            from .cli_survey import (
                handle_translations_languages_list,
                handle_translations_languages_ensure,
                handle_translations_languages_set,
                handle_translations_pull,
                handle_translations_preview,
                handle_translations_apply,
                handle_translations_doctor,
                handle_translations_drift,
                handle_translations_pack,
                handle_translations_push,
            )
            from .cli_translations_check import handle_translations_check_language

            args.legacy_translations = False

            if args.translations_command == "pull":
                args.survey_id = _prompt_for_survey_ids_if_needed(
                    getattr(args, "survey_id", None),
                    allow_all_surveys=True,
                )
                handle_translations_pull(args)
                return

            if hasattr(args, "survey_id"):
                args.survey_id = _prompt_for_survey_id_if_needed(
                    getattr(args, "survey_id", None),
                    allow_all_surveys=bool(
                        args.translations_command == "check-language"
                    ),
                )

            if args.translations_command == "languages":
                if args.translations_languages_command == "list":
                    handle_translations_languages_list(args)
                    return
                if args.translations_languages_command == "ensure":
                    handle_translations_languages_ensure(args)
                    return
                if args.translations_languages_command == "set":
                    handle_translations_languages_set(args)
                    return

            if args.translations_command == "preview":
                handle_translations_preview(args)
                return

            if args.translations_command == "apply":
                handle_translations_apply(args)
                return

            if args.translations_command == "stage":
                handle_translations_apply(args)
                return

            if args.translations_command == "doctor":
                handle_translations_doctor(args)
                return

            if args.translations_command == "drift":
                handle_translations_drift(args)
                return

            if args.translations_command == "pack":
                handle_translations_pack(args)
                return

            if args.translations_command == "push":
                handle_translations_push(args)
                return

            if args.translations_command == "check-language":
                handle_translations_check_language(args)
                return

            error(
                "[qsync:translations]",
                f"Unknown translations command: {args.translations_command}",
            )
            raise SystemExit(2)

        if hasattr(args, "func"):
            args.func(args)
            return

        from .js_preview import preview_differences as js_preview_differences
        from .js_push import push_js_from_cache as js_push_from_cache
        from .js_mapping import rebuild_mapping as rebuild_js_mapping
        from .compare import CompareInputs, compare, render_report, to_jsonable
        from .sync_core import (
            init_survey_to_excel,
            preview_changes,
            push_staged_changes,
        )
        from .pending_stage import (
            PendingStagedChanges,
            ItemsPendingPayload,
            JsPendingPayload,
            load_pending,
            save_pending,
            clear_pending,
        )

        if args.command == "js":
            from .terminal_output import error, info, success, warn

            if args.js_command == "pull":
                survey_ids: list[str] = []
                if not args.dry_run:
                    survey_ids = _prompt_for_survey_ids_if_needed(
                        getattr(args, "survey_id", None),
                        allow_all_surveys=True,
                    )
                rebuild_js_mapping(args.mapping, dry_run=bool(args.dry_run))
                if not args.dry_run:
                    for survey_id in survey_ids:
                        _ensure_mapping_column(args.mapping, survey_id)
                return

            _ensure_mapping_column(args.mapping, args.survey_id)

            if args.js_command == "preview":
                from .drift_check import confirm_preview_drift
                from .qualtrics_client import refresh_survey_cache

                include_qids = _to_set(args.include_qids)
                tag_qids = _resolve_tags_to_qids(args.survey_id, args.include_tags)
                if tag_qids:
                    include_qids = (include_qids or set()) | tag_qids
                include_js = _to_set(args.include_js)

                def _update_cache() -> None:
                    refresh_survey_cache(args.survey_id)
                    print("[qsync:js] Refreshed cached survey definition from API.")

                drift_report = confirm_preview_drift(
                    survey_id=args.survey_id,
                    dimension="js",
                    allow_drift=bool(getattr(args, "allow_drift", False)),
                    interactive=sys.stdin.isatty(),
                    update_cache=_update_cache,
                )

                results = js_preview_differences(
                    survey_id=args.survey_id,
                    mapping_csv=args.mapping,
                    show_equal=bool(args.show_equal),
                    detailed=bool(args.detailed),
                    include_qids=include_qids,
                    include_js=include_js,
                    check_drift=False,
                )
                if drift_report.has_drift and (
                    not results or all(r.status == "equal" for r in results)
                ):
                    warn(
                        "[qsync:js]",
                        "Preview compares local JS files to the cached survey. They currently match, "
                        "so there are no local diffs to show. Any push will still apply the cached "
                        "JS to live and may overwrite the drift shown above.",
                    )
                return

            if args.js_command in ("stage", "apply"):
                # Emit deprecation warning for "apply"
                if args.js_command == "apply":
                    from .terminal_output import warn

                    warn(
                        "[DEPRECATION] 'js apply' is deprecated. Use 'js stage' instead."
                    )

                from .drift_check import confirm_preview_drift
                from .qualtrics_client import refresh_survey_cache
                from .dimensions.js import _select_stage_entries

                include_qids = _to_set(args.include_qids)
                tag_qids = _resolve_tags_to_qids(args.survey_id, args.include_tags)
                if tag_qids:
                    include_qids = (include_qids or set()) | tag_qids
                include_js = _to_set(args.include_js)
                scope_expr = getattr(args, "scope", None)

                def _update_cache() -> None:
                    refresh_survey_cache(args.survey_id)
                    print("[qsync:js] Refreshed cached survey definition from API.")

                confirm_preview_drift(
                    survey_id=args.survey_id,
                    dimension="js",
                    allow_drift=bool(getattr(args, "allow_drift", False)),
                    interactive=sys.stdin.isatty(),
                    update_cache=_update_cache,
                )
                entries = _select_stage_entries(
                    survey_id=args.survey_id,
                    mapping_csv=args.mapping,
                    include_qids=include_qids,
                    include_js=include_js,
                    scope_expr=scope_expr,
                    include_match=not args.no_include_match,
                    allow_diff=bool(args.allow_diff),
                    create_missing=bool(args.create_missing),
                    interactive=sys.stdin.isatty(),
                )
                if bool(args.dry_run):
                    info(
                        "[qsync:js]",
                        f"Dry run: {len(entries)} QuestionJS block(s) would be staged.",
                    )
                    return
                if entries:
                    record = PendingStagedChanges(
                        survey_id=args.survey_id,
                        dimension="js",
                        payload=JsPendingPayload(entries=entries),
                        schema_version=2,
                    )
                    save_pending(record)
                    info(
                        "[qsync:js]",
                        f"Staged {len(entries)} QuestionJS block(s): "
                        + ", ".join(entry["qid"] for entry in entries),
                    )
                    info(
                        "[qsync:js]",
                        f"Pending: surveys/pending/js/{args.survey_id}.json (schema v{record.schema_version})",
                    )
                else:
                    clear_pending(args.survey_id, "js")
                    info("[qsync:js]", "No updates staged.")
                return

            if args.js_command == "push":
                include_qids = _to_set(args.include_qids)
                tag_qids = _resolve_tags_to_qids(args.survey_id, args.include_tags)
                if tag_qids:
                    include_qids = (include_qids or set()) | tag_qids
                include_js = _to_set(args.include_js)
                from .js_preview import load_mapping as js_load_mapping

                def _filter_entries(
                    entries: list[dict[str, str]],
                ) -> list[dict[str, str]]:
                    filtered = list(entries)
                    if include_qids:
                        filtered = [e for e in filtered if e.get("qid") in include_qids]
                    if include_js:
                        filtered = [
                            e for e in filtered if e.get("js_file") in include_js
                        ]
                    return filtered

                def _build_mapping_entries() -> list[dict[str, str]]:
                    mapping = js_load_mapping(args.mapping, args.survey_id)
                    if include_js:
                        mapping = {
                            js_file: qids
                            for js_file, qids in mapping.items()
                            if js_file in include_js
                        }
                    entries: list[dict[str, str]] = []
                    for js_file, qids in mapping.items():
                        for qid in qids:
                            if include_qids and qid not in include_qids:
                                continue
                            entries.append(
                                {"js_file": js_file, "qid": qid, "status": "mapped"}
                            )
                    return entries

                if args.push_all:
                    pending_entries = _build_mapping_entries()
                    qids_preview = js_push_from_cache(
                        survey_id=args.survey_id,
                        mapping_csv=args.mapping,
                        include_trash=bool(args.include_trash),
                        dry_run=True,
                        pending_entries=pending_entries,
                    )
                    if not qids_preview:
                        info("[qsync:js]", "No QuestionJS blocks qualified for push.")
                        return
                    qid_list = ", ".join(qids_preview)
                    info(
                        "[qsync:js]",
                        f"Ready to push {len(qids_preview)} question(s): {qid_list}.",
                    )
                    if args.dry_run:
                        return
                    if not args.yes:
                        try:
                            from .interactive_menu import confirm

                            if not confirm(
                                "Push these QuestionJS blocks?", default=True
                            ):
                                warn("[qsync:js]", "Aborted.")
                                return
                        except Exception:
                            resp = (
                                input("Push these QuestionJS blocks? [Y/n] ")
                                .strip()
                                .lower()
                            )
                            if resp and resp not in {"y", "yes"}:
                                warn("[qsync:js]", "Aborted.")
                                return
                    # Safeguards are now handled inside push_js_from_cache
                    try:
                        js_push_from_cache(
                            survey_id=args.survey_id,
                            mapping_csv=args.mapping,
                            include_trash=bool(args.include_trash),
                            dry_run=False,
                            pending_entries=pending_entries,
                            publish=not bool(getattr(args, "no_publish", False)),
                            force_live=bool(args.force_live),
                            force_preview=bool(args.force_preview),
                            interactive=not bool(args.yes),
                            allow_drift=bool(getattr(args, "allow_drift", False)),
                        )
                        from .qualtrics_client import refresh_survey_cache

                        try:
                            refresh_survey_cache(args.survey_id)
                        except Exception as exc:
                            warn(
                                "[qsync:js]",
                                f"Push succeeded but cache refresh failed: {exc}",
                            )
                    except Exception:
                        error("[qsync:js]", "Push failed; cached JS left untouched.")
                        raise
                    success(
                        "[qsync:js]",
                        f"Uploaded {len(qids_preview)} question(s): {qid_list}.",
                    )
                    return

                record = load_pending(args.survey_id, "js")
                if record is None or not isinstance(record.payload, JsPendingPayload):
                    info(
                        "[qsync:js]",
                        "No staged JS changes found. Run 'qsync js stage' first.",
                    )
                    return
                info(
                    "[qsync:js]",
                    f"Using pending schema v{getattr(record, 'schema_version', 1)} from surveys/pending/js/{args.survey_id}.json",
                )
                entries = _filter_entries(record.payload.entries)
                if not entries:
                    info(
                        "[qsync:js]",
                        "No staged JS entries match the requested filters.",
                    )
                    return
                qids_to_push = [
                    qid for qid in (entry.get("qid") for entry in entries) if qid
                ]
                summary_lines = [
                    f"- {entry.get('qid')} ({entry.get('js_file')}) status={entry.get('status')}"
                    for entry in entries
                ]
                info("[qsync:js]", "Pending QuestionJS blocks:")
                for line in summary_lines:
                    print(line)
                if args.dry_run:
                    js_push_from_cache(
                        survey_id=args.survey_id,
                        mapping_csv=args.mapping,
                        include_trash=bool(args.include_trash),
                        dry_run=True,
                        qids_override=qids_to_push,
                        pending_entries=entries,
                    )
                    return
                if not args.yes:
                    try:
                        from .interactive_menu import confirm

                        if not confirm("Push these QuestionJS blocks?", default=True):
                            warn("[qsync:js]", "Aborted.")
                            return
                    except Exception:
                        resp = (
                            input("Push these QuestionJS blocks? [Y/n] ")
                            .strip()
                            .lower()
                        )
                        if resp and resp not in {"y", "yes"}:
                            warn("[qsync:js]", "Aborted.")
                            return
                try:
                    refreshed = False
                    js_push_from_cache(
                        survey_id=args.survey_id,
                        mapping_csv=args.mapping,
                        include_trash=bool(args.include_trash),
                        dry_run=False,
                        qids_override=qids_to_push,
                        pending_entries=entries,
                        publish=not bool(getattr(args, "no_publish", False)),
                        force_live=bool(args.force_live),
                        force_preview=bool(args.force_preview),
                        interactive=not bool(args.yes),
                        allow_drift=bool(getattr(args, "allow_drift", False)),
                    )
                    from .qualtrics_client import refresh_survey_cache

                    try:
                        refresh_survey_cache(args.survey_id)
                        refreshed = True
                    except Exception as exc:
                        warn(
                            "[qsync:js]",
                            f"Push succeeded but cache refresh failed: {exc}",
                        )
                except Exception:
                    error("[qsync:js]", "Push failed; pending JS changes preserved.")
                    raise
                if refreshed:
                    clear_pending(args.survey_id, "js")
                qid_list = ", ".join(qids_to_push)
                success(
                    "[qsync:js]",
                    f"Uploaded {len(qids_to_push)} question(s): {qid_list}.",
                )
                return

            raise SystemExit(f"Unknown qsync js subcommand: {args.js_command}")

        if args.command == "compare":
            inputs = CompareInputs(
                source_id=args.source_id,
                target_id=args.target_id,
                refresh=not args.no_refresh,
                include_tags=_to_set(args.include_tags),
                exclude_tags=_to_set(args.exclude_tags),
                json_output=args.json_output,
                with_diffs=bool(args.with_diffs),
            )
            result = compare(inputs)
            report = render_report(result)
            print(report)

            if args.json_output:
                args.json_output.parent.mkdir(parents=True, exist_ok=True)
                args.json_output.write_text(
                    json.dumps(to_jsonable(result), indent=2), encoding="utf-8"
                )
                print(f"[qsync:compare] Wrote JSON report to {args.json_output}")

            should_fail = False
            if args.fail_on == "any":
                should_fail = result.has_blocking()
            elif args.fail_on == "question":
                should_fail = bool(
                    result.missing_in_target
                    or result.missing_in_source
                    or [d for d in result.question_diffs if d.status == "mismatch"]
                )
            elif args.fail_on == "metadata":
                should_fail = bool(result.metadata_diffs)

            if should_fail:
                raise SystemExit(1)
            return

        # sync command (orchestrator)
        if args.command == "sync":
            if bool(getattr(args, "tui", False)):
                _handle_tui(args)
                return

            from .sync_orchestrator import (
                sync_survey,
                sync_focal_surveys,
                display_recovery_instructions,
            )
            from .scope_filter import ScopeFilter
            import time

            from .terminal_output import info, error, dim, format_elapsed

            json_output = bool(getattr(args, "json", False))

            # Parse scope filter if provided
            scope = None
            if getattr(args, "scope", None):
                try:
                    scope = ScopeFilter.parse(
                        args.scope,
                        survey_id=(
                            args.survey_id if hasattr(args, "survey_id") else None
                        ),
                    )
                except Exception as e:
                    error("[qsync:sync]", f"Invalid scope filter: {e}")
                    raise SystemExit(1)

            # Parse dimensions if provided
            dimensions = None
            if getattr(args, "dimensions", None):
                dimensions = [d.strip() for d in args.dimensions.split(",")]
                valid_dims = {"items", "js", "translations", "eos", "flow"}
                invalid = [d for d in dimensions if d not in valid_dims]
                if invalid:
                    error("[qsync:sync]", f"Invalid dimensions: {', '.join(invalid)}")
                    error(
                        "[qsync:sync]",
                        f"Valid dimensions: {', '.join(sorted(valid_dims))}",
                    )
                    raise SystemExit(1)

            # Handle --refresh-workbooks and --skip-refresh flags
            refresh_workbooks = bool(getattr(args, "refresh_workbooks", False))
            skip_refresh = bool(getattr(args, "skip_refresh", False))

            # Warn if --skip-refresh is used
            if skip_refresh:
                if not refresh_workbooks:
                    warn(
                        "[qsync:sync]",
                        "--skip-refresh is deprecated; workbook refresh is disabled by default.",
                    )
                    warn(
                        "[qsync:sync]",
                        "Use --refresh-workbooks to enable post-sync workbook refresh.",
                    )
                else:
                    # --skip-refresh overrides --refresh-workbooks (last-flag-wins)
                    refresh_workbooks = False
                    warn(
                        "[qsync:sync]",
                        "--skip-refresh overrides --refresh-workbooks; workbook refresh disabled.",
                    )

            # Single survey or multi-survey?
            if getattr(args, "survey_id", None):
                from .survey_ref import format_survey_ref

                if not json_output:
                    info(
                        "[qsync:sync]",
                        f"Syncing survey {format_survey_ref(args.survey_id)}...",
                    )
                start_time = time.perf_counter()
                summary = sync_survey(
                    survey_id=args.survey_id,
                    dimensions=dimensions,
                    interactive=not bool(args.yes),
                    force_live=bool(args.force_live),
                    force_preview=bool(args.force_preview),
                    auto_yes=bool(args.yes),
                    pending_action=str(getattr(args, "pending_action", "abort")),
                    scope=scope,
                    per_dimension=bool(getattr(args, "per_dimension", False)),
                    skip_publish=bool(getattr(args, "skip_publish", False)),
                    refresh_workbooks=refresh_workbooks,
                    allow_drift=bool(getattr(args, "allow_drift", False)),
                    allow_skip_embedded=bool(
                        getattr(args, "allow_skip_embedded", False)
                    ),
                    json_output=json_output,
                    fix=getattr(args, "fix", None),
                )
                elapsed = time.perf_counter() - start_time
                success = summary.success if summary else True
                if summary and not summary.success and not json_output:
                    display_recovery_instructions(
                        [summary],
                        force_live=bool(args.force_live),
                        force_preview=bool(args.force_preview),
                        scope_expr=getattr(args, "scope", None),
                        auto_yes=bool(args.yes),
                    )
                if not json_output:
                    from .terminal_output import mark_timing_emitted

                    dim("[qsync:sync]", f"Completed in {format_elapsed(elapsed)}")
                    mark_timing_emitted()
            else:
                if not json_output:
                    info("[qsync:sync]", "Syncing focal surveys...")
                success = sync_focal_surveys(
                    interactive=not bool(args.yes),
                    force_live=bool(args.force_live),
                    force_preview=bool(args.force_preview),
                    auto_yes=bool(args.yes),
                    pending_action=str(getattr(args, "pending_action", "abort")),
                    scope=scope,
                    process_all=bool(getattr(args, "all", False)),
                    per_dimension=bool(getattr(args, "per_dimension", False)),
                    skip_publish=bool(getattr(args, "skip_publish", False)),
                    refresh_workbooks=refresh_workbooks,
                    allow_drift=bool(getattr(args, "allow_drift", False)),
                    allow_skip_embedded=bool(
                        getattr(args, "allow_skip_embedded", False)
                    ),
                    json_output=json_output,
                    fix=getattr(args, "fix", None),
                )

            if not success:
                error("[qsync:sync]", "❌ Sync failed")
                raise SystemExit(1)
            return

        if args.command == "export":
            from .cli_survey import handle_export_translation

            if getattr(args, "export_command", None) == "survey":
                handle_export_translation(args)
                return
            raise SystemExit("Unknown qsync export subcommand")

        # items command dispatcher
        if args.command == "items":
            if args.items_command == "pull":
                from .terminal_output import error, info

                survey_ids = _prompt_for_survey_ids_if_needed(
                    getattr(args, "survey_id", None),
                    allow_all_surveys=True,
                )
                if args.xlsx is not None and len(survey_ids) > 1:
                    error(
                        "[qsync:items]",
                        "--xlsx supports only one survey at a time. Omit --xlsx for multi-survey pulls.",
                    )
                    raise SystemExit(2)
                languages = _collect_languages_from_args(args)
                for survey_id in survey_ids:
                    xlsx_path = args.xlsx or _default_xlsx_path(survey_id)
                    init_survey_to_excel(
                        survey_id,
                        xlsx_path,
                        languages=languages,
                    )
                    info("[qsync:items]", f"{survey_id}: survey pulled to {xlsx_path}")
                return

            if args.items_command == "preview":
                from .terminal_colors import colorize_unified_diff_lines
                from .terminal_output import info, warn
                from .drift_check import confirm_preview_drift
                from .qualtrics_client import refresh_survey_cache

                xlsx_path = args.xlsx or _default_xlsx_path(args.survey_id)
                include_qids = _to_set(getattr(args, "include_qid", None))
                include_tags = _to_set(getattr(args, "include_tag", None))

                def _update_cache() -> None:
                    refresh_survey_cache(args.survey_id)
                    info(
                        "[qsync:items]", "Refreshed cached survey definition from API."
                    )

                confirm_preview_drift(
                    survey_id=args.survey_id,
                    dimension="items",
                    allow_drift=bool(getattr(args, "allow_drift", False)),
                    interactive=sys.stdin.isatty(),
                    update_cache=_update_cache,
                )

                changes = preview_changes(
                    args.survey_id,
                    xlsx_path,
                    include_qids=include_qids,
                    include_tags=include_tags,
                    embedded_only=getattr(args, "embedded_data_only", False),
                    scope_expr=getattr(args, "scope", None),
                    check_drift=False,
                )

                if not changes:
                    info(
                        "[qsync:items]",
                        "No differences between Excel and cached survey.",
                    )
                    return

                info("[qsync:items]", f"{len(changes)} change(s) detected.")
                print()
                info("[qsync:items]", "Change overview:")
                _summarize_preview(changes)

                dangerous_embedded = [
                    c for c in changes if c.kind == "embedded" and c.is_dangerous
                ]
                if dangerous_embedded:
                    warn(
                        "[qsync:items]",
                        f"{len(dangerous_embedded)} dangerous embedded data change(s) "
                        "require --allow-dangerous to stage.",
                    )

                if args.detailed:
                    print()
                    info("[qsync:items]", "Detailed diffs (cached vs Excel):")
                    for change in changes:
                        print("-" * 80)
                        if change.kind == "embedded":
                            flow = (
                                f", flow_id={change.flow_id}" if change.flow_id else ""
                            )
                            header = f"{change.kind.upper()} field={change.field or change.qid}{flow}"
                        else:
                            header = f"{change.kind.upper()} qid={change.qid}"
                        if change.choice_id is not None:
                            header += f", choice_id={change.choice_id}"
                        if change.answer_id is not None:
                            header += f", answer_id={change.answer_id}"
                        print(header)
                        diff_lines = change.diff_lines or []
                        if diff_lines:
                            for line in colorize_unified_diff_lines(diff_lines):
                                print("  " + line)
                        else:
                            old_html = (change.old_html or "").strip()
                            new_html = (change.new_html or "").strip()
                            print("  OLD:", old_html)
                            print("  NEW:", new_html)
                return

            if args.items_command == "stage":
                from .terminal_output import info, warn
                from .pending_stage import PendingStagedChanges, save_pending
                from .dimensions.items import _build_pending_payload_from_workbook

                xlsx_path = args.xlsx or _default_xlsx_path(args.survey_id)
                include_qids = _to_set(getattr(args, "include_qid", None))
                include_tags = _to_set(getattr(args, "include_tag", None))

                payload = _build_pending_payload_from_workbook(
                    args.survey_id,
                    Path(xlsx_path),
                    scope_expr=getattr(args, "scope", None),
                    filter_column=getattr(args, "filter_column", None),
                    filter_value=getattr(args, "filter_value", None),
                    include_qids=include_qids,
                    include_tags=include_tags,
                    ignore_embedded=bool(getattr(args, "embedded_data_only", False)),
                    allow_drift=bool(getattr(args, "allow_drift", False)),
                    interactive=not bool(getattr(args, "yes", False)),
                    allow_dangerous=bool(getattr(args, "allow_dangerous", False)),
                    existing=None,
                )
                if not payload:
                    warn("[qsync:items]", "No changes to stage.")
                    clear_pending(args.survey_id, "items")
                    return
                record = PendingStagedChanges(
                    survey_id=args.survey_id,
                    dimension="items",
                    payload=payload,
                    schema_version=2,
                )
                save_pending(record)
                info(
                    "[qsync:items]",
                    f"Staged {len(payload.qids)} question(s) (pending schema v{record.schema_version})",
                )
                info(
                    "[qsync:items]",
                    f"Pending: surveys/pending/items/{args.survey_id}.json (schema v{record.schema_version})",
                )
                return

            if args.items_command == "repair-edf":
                from .terminal_output import dim, error, info, success, warn
                from .dimensions import edf as edf_dimension
                from .interactive_menu import confirm

                survey_id = _prompt_for_survey_id_if_needed(
                    getattr(args, "survey_id", None),
                    allow_all_surveys=False,
                )
                xlsx_path = Path(
                    getattr(args, "xlsx", None) or _default_xlsx_path(survey_id)
                )

                if not xlsx_path.exists():
                    raise SystemExit(
                        f"[qsync:items:repair-edf] Workbook not found at {xlsx_path}. "
                        f"Run `qsync items pull --survey-id {survey_id}` first."
                    )

                retain_backups = int(getattr(args, "retain_backups", 5) or 0)
                if retain_backups < 0:
                    raise SystemExit(
                        "[qsync:items:repair-edf] --retain-backups must be >= 0"
                    )

                def _print_report(
                    report: edf_dimension.EdfRepairReport, *, prefix: str
                ) -> None:
                    before_issues = (
                        len(report.before_health.missing_fields)
                        + len(report.before_health.extra_fields)
                        + len(report.before_health.duplicate_fields)
                        + len(report.before_health.ambiguous_fields)
                    )
                    after_issues = (
                        len(report.after_health.missing_fields)
                        + len(report.after_health.extra_fields)
                        + len(report.after_health.duplicate_fields)
                        + len(report.after_health.ambiguous_fields)
                    )
                    info(prefix, f"Workbook: {report.workbook_path}")
                    info(
                        prefix,
                        (
                            f"Rows: {report.rows_before} -> {report.rows_after} "
                            f"(+{report.rows_added}/-{report.rows_removed}, "
                            f"duplicates removed={report.duplicate_rows_removed}, "
                            f"unchanged={report.unchanged_rows})"
                        ),
                    )
                    info(
                        prefix,
                        f"Issues: {before_issues} -> {after_issues} (extra rows preserved={report.extra_rows_preserved})",
                    )
                    if report.after_health.extra_fields:
                        warn(
                            prefix,
                            "Unknown extra Embedded_Data rows remain; remove them manually if they are stale.",
                        )
                    if report.after_health.ambiguous_fields:
                        warn(
                            prefix,
                            "Ambiguous rows remain (same field without FlowID in multiple nodes). "
                            "Use `qsync items pull` if repair is incomplete.",
                        )

                try:
                    planned = edf_dimension.repair_workbook(
                        survey_id,
                        xlsx_path=xlsx_path,
                        dry_run=True,
                        refresh_cache=bool(getattr(args, "refresh_cache", False)),
                        retain_backups=retain_backups,
                    )
                except Exception as exc:
                    error("[qsync:items:repair-edf]", str(exc))
                    raise SystemExit(2)

                _print_report(planned, prefix="[qsync:items:repair-edf]")

                if getattr(args, "dry_run", False):
                    dim(
                        "[qsync:items:repair-edf]",
                        "Dry run complete; no workbook changes written.",
                    )
                    return

                if not planned.changed:
                    success(
                        "[qsync:items:repair-edf]",
                        "Embedded_Data is already aligned; no changes applied.",
                    )
                    return

                if not bool(getattr(args, "yes", False)):
                    if not confirm(
                        message=f"Apply Embedded_Data repair to {xlsx_path}?",
                        default=True,
                    ):
                        dim("[qsync:items:repair-edf]", "Aborted by user.")
                        return

                try:
                    applied = edf_dimension.repair_workbook(
                        survey_id,
                        xlsx_path=xlsx_path,
                        dry_run=False,
                        refresh_cache=False,
                        retain_backups=retain_backups,
                    )
                except Exception as exc:
                    error("[qsync:items:repair-edf]", str(exc))
                    raise SystemExit(2)

                _print_report(applied, prefix="[qsync:items:repair-edf]")
                if applied.backup_path:
                    success(
                        "[qsync:items:repair-edf]",
                        f"Applied repair (backup: {applied.backup_path})",
                    )
                else:
                    success("[qsync:items:repair-edf]", "Applied repair.")
                return

            if args.items_command == "inspect":
                from .interactive_menu import select_from_list
                from .survey_inventory import prompt_for_survey_id
                from .qualtrics_client import load_cached_survey, refresh_survey_cache
                from .dimensions.items_structural import (
                    inspect_question,
                    iter_active_qids_in_flow,
                )

                interactive = sys.stdin.isatty() and sys.stdout.isatty()

                survey_id = args.survey_id
                if not survey_id:
                    if not interactive:
                        raise RuntimeError(
                            "Provide --survey-id (or run in an interactive terminal)."
                        )
                    survey_id = prompt_for_survey_id(
                        allow_all_surveys=True, interactive=True
                    )

                if bool(getattr(args, "refresh", False)):
                    refresh_survey_cache(survey_id)

                qid = args.qid
                if not qid:
                    if not interactive:
                        raise RuntimeError(
                            "Provide --qid (or run in an interactive terminal)."
                        )
                    survey = load_cached_survey(survey_id)
                    qids = list(iter_active_qids_in_flow(survey))
                    if not qids:
                        raise RuntimeError(
                            "No active QIDs found in SurveyFlow for this survey."
                        )
                    qid = select_from_list("Select a QID to inspect", qids)
                    if not qid:
                        return

                print(inspect_question(survey_id=survey_id, qid=qid, refresh=False))
                return

            if args.items_command == "edit":
                from .terminal_output import info
                from .pending_stage import (
                    ItemsPendingPayload,
                    PendingStagedChanges,
                    load_pending,
                    save_pending,
                )
                from .survey_inventory import prompt_for_survey_id
                from .dimensions.items_structural import (
                    ItemsStructuralError,
                    interactive_choice_wizard,
                    preflight_items_edit,
                    summarize_structural_ops,
                    stage_structural_op,
                )

                interactive = (
                    sys.stdin.isatty()
                    and sys.stdout.isatty()
                    and not bool(getattr(args, "yes", False))
                )

                survey_id = args.survey_id
                if not survey_id:
                    if not interactive:
                        raise RuntimeError(
                            "Provide --survey-id (or run in an interactive terminal)."
                        )
                    survey_id = prompt_for_survey_id(
                        allow_all_surveys=True, interactive=True
                    )

                preflight_items_edit(
                    survey_id=survey_id,
                    ignore_workbook_drift=bool(
                        getattr(args, "ignore_workbook_drift", False)
                    ),
                    interactive=interactive,
                )

                try:
                    if interactive and not getattr(args, "action", None):
                        op = interactive_choice_wizard(
                            survey_id=survey_id,
                            qid=getattr(args, "qid", None),
                            allow_delete=bool(getattr(args, "allow_delete", False)),
                            experimental_unsupported=bool(
                                getattr(args, "experimental_unsupported", False)
                            ),
                        )
                    else:
                        if not getattr(args, "action", None):
                            raise ItemsStructuralError(
                                "[qsync:items:edit] Provide --action for non-interactive runs."
                            )
                        if not getattr(args, "qid", None):
                            raise ItemsStructuralError(
                                "[qsync:items:edit] Provide --qid for non-interactive runs."
                            )
                        text = getattr(args, "text", None)
                        text_file = getattr(args, "text_file", None)
                        if text_file:
                            try:
                                text = Path(text_file).read_text(encoding="utf-8")
                            except Exception as e:
                                raise ItemsStructuralError(
                                    f"[qsync:items:edit] Failed reading --text-file: {e}"
                                )
                        op = stage_structural_op(
                            survey_id=survey_id,
                            qid=str(getattr(args, "qid", "")).strip(),
                            target=str(getattr(args, "target", "choices")).strip(),
                            action=str(getattr(args, "action", "")).strip(),
                            html=text,
                            text_format=getattr(args, "text_format", None),
                            item_id=getattr(args, "item_id", None),
                            allow_delete=bool(getattr(args, "allow_delete", False)),
                            interactive=interactive,
                            experimental_unsupported=bool(
                                getattr(args, "experimental_unsupported", False)
                            ),
                        )
                except ItemsStructuralError as e:
                    raise SystemExit(str(e))

                existing = load_pending(survey_id, "items")
                if existing and isinstance(existing.payload, ItemsPendingPayload):
                    payload = existing.payload
                    ops = list(payload.structural_ops or [])
                    ops.append(op)
                    payload.structural_ops = ops
                    payload.structural_summary = summarize_structural_ops(ops)
                    payload.push_journal = {}
                    record = PendingStagedChanges(
                        survey_id=survey_id,
                        dimension="items",
                        payload=payload,
                    )
                else:
                    record = PendingStagedChanges(
                        survey_id=survey_id,
                        dimension="items",
                        payload=ItemsPendingPayload(
                            qids=[],
                            embedded_fields=[],
                            workbook=None,
                            filter_column=None,
                            filter_value=None,
                            structural_ops=[op],
                            structural_summary=summarize_structural_ops([op]),
                            push_journal={},
                        ),
                    )
                save_pending(record)

                staged_id = op.get("choice_id") or op.get("answer_id")
                info(
                    "[qsync:items:edit]",
                    f"Staged structural op: {op.get('op')} qid={op.get('qid')} id={staged_id}",
                )
                info(
                    "[qsync:items:edit]",
                    f"Next: run `qsync items push --survey-id {survey_id}` to upload changes.",
                )
                info(
                    "[qsync:items:edit]",
                    f"Tip: refresh your workbook after structural edits: `qsync items pull --survey-id {survey_id}`",
                )

                if interactive:
                    from .interactive_menu import select_from_list
                    from .terminal_output import warn

                    next_step = select_from_list(
                        "What do you want to do next?",
                        [
                            "Stage only (return to shell)",
                            "Push now (run the equivalent of `qsync items push`)",
                            "↩ Exit",
                        ],
                    )
                    if next_step and next_step.startswith("Push now"):
                        _push_items_pending_record(
                            survey_id=survey_id,
                            prefix="[qsync:items:edit]",
                            yes=False,
                            dry_run=False,
                            force_live=False,
                            force_preview=False,
                            no_publish=False,
                            allow_delete=bool(getattr(args, "allow_delete", False)),
                            scope_expr=None,
                            allow_drift=False,
                            prefer_pending=None,
                            workbook_tip=True,
                        )
                return

            if args.items_command == "push":
                survey_id = _prompt_for_survey_id_if_needed(
                    getattr(args, "survey_id", None),
                    allow_all_surveys=False,
                )
                _push_items_pending_record(
                    survey_id=survey_id,
                    prefix="[qsync:items]",
                    yes=bool(getattr(args, "yes", False)),
                    dry_run=bool(getattr(args, "dry_run", False)),
                    force_live=bool(getattr(args, "force_live", False)),
                    force_preview=bool(getattr(args, "force_preview", False)),
                    no_publish=bool(getattr(args, "no_publish", False)),
                    allow_delete=bool(getattr(args, "allow_delete", False)),
                    scope_expr=getattr(args, "scope", None),
                    allow_drift=bool(getattr(args, "allow_drift", False)),
                    prefer_pending=getattr(args, "use_pending", None),
                    workbook_tip=False,
                )
                return

        # Legacy commands (backward compatibility)
        if args.command == "init":
            survey_id = _prompt_for_survey_id_if_needed(
                getattr(args, "survey_id", None),
                allow_all_surveys=True,
            )
            if args.xlsx is not None:
                xlsx_path = args.xlsx
            else:
                xlsx_path = _default_xlsx_path(survey_id)
            languages = _collect_languages_from_args(args)
            init_survey_to_excel(survey_id, xlsx_path, languages=languages)
            return

        if args.command == "preview":
            from .terminal_colors import colorize_unified_diff_lines
            from .terminal_output import info, warn
            from .drift_check import confirm_preview_drift
            from .qualtrics_client import refresh_survey_cache

            xlsx_path = args.xlsx or _default_xlsx_path(args.survey_id)
            include_qids = _to_set(args.include_qids)
            include_tags = _to_set(args.include_tags)

            def _update_cache() -> None:
                refresh_survey_cache(args.survey_id)
                info("[qsync:preview]", "Refreshed cached survey definition from API.")

            confirm_preview_drift(
                survey_id=args.survey_id,
                dimension="items",
                allow_drift=bool(getattr(args, "allow_drift", False)),
                interactive=sys.stdin.isatty(),
                update_cache=_update_cache,
            )
            changes = preview_changes(
                survey_id=args.survey_id,
                xlsx_path=xlsx_path,
                filter_column=args.filter_column,
                filter_value=args.filter_value,
                include_qids=include_qids,
                include_tags=include_tags,
                embedded_only=bool(args.embedded_data_only),
                check_drift=False,
            )
            if not changes:
                info(
                    "[qsync:preview]", "No differences between Excel and cached survey."
                )
                return
            info("[qsync:preview]", f"{len(changes)} change(s) detected.")
            print()
            info("[qsync:preview]", "Change overview:")
            _summarize_preview(changes)
            dangerous_embedded = [
                c for c in changes if c.kind == "embedded" and c.is_dangerous
            ]
            if dangerous_embedded:
                warn(
                    "[qsync:preview]",
                    f"{len(dangerous_embedded)} dangerous embedded data change(s) "
                    "require --allow-dangerous to apply.",
                )
            if args.detailed:
                print()
                info("[qsync:preview]", "Detailed diffs (cached vs Excel):")
                for change in changes:
                    print("-" * 80)
                    if change.kind == "embedded":
                        flow = f", flow_id={change.flow_id}" if change.flow_id else ""
                        header = f"{change.kind.upper()} field={change.field or change.qid}{flow}"
                    else:
                        header = f"{change.kind.upper()} qid={change.qid}"
                    if change.choice_id is not None:
                        header += f", choice_id={change.choice_id}"
                    if change.answer_id is not None:
                        header += f", answer_id={change.answer_id}"
                    print(header)
                    diff_lines = change.diff_lines or []
                    if diff_lines:
                        for line in colorize_unified_diff_lines(diff_lines):
                            print("  " + line)
                    else:
                        old_html = (change.old_html or "").strip()
                        new_html = (change.new_html or "").strip()
                        print("  OLD:", old_html)
                        print("  NEW:", new_html)
            return

        if args.command == "apply":
            from .terminal_output import error, info, success, warn
            from .dimensions.items import _build_pending_payload_from_workbook

            xlsx_path = args.xlsx or _default_xlsx_path(args.survey_id)
            include_qids = _to_set(args.include_qids)
            include_tags = _to_set(args.include_tags)
            pending = preview_changes(
                survey_id=args.survey_id,
                xlsx_path=xlsx_path,
                filter_column=args.filter_column,
                filter_value=args.filter_value,
                include_qids=include_qids,
                include_tags=include_tags,
                embedded_only=bool(args.embedded_data_only),
                check_drift=False,
            )
            if not pending:
                info(
                    "[qsync:apply]",
                    "No differences between Excel and cached survey; skipping.",
                )
                return
            dangerous_embedded = [
                c for c in pending if c.kind == "embedded" and c.is_dangerous
            ]
            if dangerous_embedded and not args.allow_dangerous:
                warn(
                    "[qsync:apply]",
                    f"{len(dangerous_embedded)} dangerous embedded data change(s) "
                    "will be skipped unless --allow-dangerous is set.",
                )
            change_count = len(pending)
            info("[qsync:apply]", f"{change_count} change(s) ready to stage.")
            if not args.yes:
                try:
                    from .interactive_menu import confirm

                    if not confirm(
                        f"Stage {change_count} change(s) for {args.survey_id}?",
                        default=True,
                    ):
                        warn("[qsync:apply]", "Aborted.")
                        return
                except Exception:
                    resp = (
                        input(
                            f"Stage {change_count} change(s) for {args.survey_id}? [Y/n] "
                        )
                        .strip()
                        .lower()
                    )
                    if resp and resp not in {"y", "yes"}:
                        warn("[qsync:apply]", "Aborted.")
                        return

            payload = _build_pending_payload_from_workbook(
                args.survey_id,
                Path(xlsx_path),
                scope_expr=None,
                filter_column=args.filter_column,
                filter_value=args.filter_value,
                include_qids=include_qids,
                include_tags=include_tags,
                ignore_embedded=bool(args.embedded_data_only),
                allow_drift=bool(getattr(args, "allow_drift", False)),
                interactive=not bool(getattr(args, "yes", False)),
                allow_dangerous=bool(args.allow_dangerous),
                existing=None,
            )
            if payload:
                record = PendingStagedChanges(
                    survey_id=args.survey_id,
                    dimension="items",
                    payload=payload,
                    schema_version=2,
                )
                save_pending(record)
            else:
                clear_pending(args.survey_id, "items")
                success("[qsync:apply]", "No staged changes remain; pending cleared.")
            return

        if args.command == "push":
            from .terminal_output import error, info, success, warn

            survey_id = _prompt_for_survey_id_if_needed(
                args.survey_id, allow_all_surveys=False
            )
            args.survey_id = survey_id
            record = load_pending(survey_id, "items")
            if record is None or not isinstance(record.payload, ItemsPendingPayload):
                info(
                    "[qsync:push]",
                    f"No staged changes found for {survey_id}. Run 'qsync apply' first.",
                )
                return

            qids = list(record.payload.qids or [])
            embedded_fields = list(record.payload.embedded_fields or [])
            if record.schema_version < 2 or not getattr(
                record.payload, "changes", None
            ):
                if record.payload.workbook:
                    wb_path = Path(record.payload.workbook)
                    if wb_path.exists():
                        from .dimensions.items import (
                            _build_pending_payload_from_workbook,
                        )

                        rebuilt = _build_pending_payload_from_workbook(
                            survey_id,
                            wb_path,
                            scope_expr=None,
                            ignore_embedded=False,
                            allow_drift=bool(getattr(args, "allow_drift", False)),
                            interactive=not bool(args.yes),
                            existing=record.payload,
                        )
                        if rebuilt:
                            record.payload = rebuilt
                            record.schema_version = 2
                            save_pending(record)
                            qids = list(rebuilt.qids or [])
                            embedded_fields = list(rebuilt.embedded_fields or [])
            if not qids and not embedded_fields:
                warn("[qsync:push]", "Pending record is empty; clearing.")
                clear_pending(survey_id, "items")
                return

            change_count = len(qids)
            embedded_count = len(embedded_fields or [])
            qid_list = ", ".join(qids)
            if change_count and embedded_count:
                info(
                    "[qsync:push]",
                    f"{change_count} staged question(s) and {embedded_count} embedded field(s) "
                    f"ready to upload: {qid_list}.",
                )
            elif embedded_count:
                info(
                    "[qsync:push]",
                    f"{embedded_count} embedded field(s) ready to upload.",
                )
            else:
                info(
                    "[qsync:push]",
                    f"{change_count} staged question(s) ready to upload: {qid_list}.",
                )
            if not args.yes:
                prompt_suffix = ""
                if embedded_count and not change_count:
                    prompt_suffix = f"{embedded_count} embedded field(s)"
                elif embedded_count:
                    prompt_suffix = f"{change_count} question(s) and {embedded_count} embedded field(s)"
                else:
                    prompt_suffix = f"{change_count} question(s)"
                try:
                    from .interactive_menu import confirm

                    if not confirm(
                        f"Push {prompt_suffix} to Qualtrics for {survey_id}?",
                        default=True,
                    ):
                        warn("[qsync:push]", "Aborted.")
                        return
                except Exception:
                    resp = (
                        input(
                            f"Push {prompt_suffix} to Qualtrics for {survey_id}? [Y/n] "
                        )
                        .strip()
                        .lower()
                    )
                    if resp and resp not in {"y", "yes"}:
                        warn("[qsync:push]", "Aborted.")
                        return

            # Safeguards are now handled inside push_staged_changes
            try:
                push_staged_changes(
                    survey_id=survey_id,
                    qids=qids,
                    embedded_fields=embedded_fields,
                    pending_changes=list(
                        getattr(record.payload, "changes", None) or []
                    ),
                    workbook=record.payload.workbook,
                    filter_column=record.payload.filter_column,
                    filter_value=record.payload.filter_value,
                    publish=not bool(getattr(args, "no_publish", False)),
                    force_live=bool(args.force_live),
                    force_preview=bool(args.force_preview_items),
                    interactive=not bool(args.yes),
                    allow_drift=bool(getattr(args, "allow_drift", False)),
                )
                from .qualtrics_client import refresh_survey_cache

                refresh_survey_cache(survey_id)
            except Exception:
                error("[qsync:push]", "Wording push failed; pending changes preserved.")
                raise
            clear_pending(survey_id, "items")
            if change_count and embedded_count:
                success(
                    "[qsync:push]",
                    f"Uploaded {change_count} question(s) and {embedded_count} embedded field(s).",
                )
            elif embedded_count:
                success("[qsync:push]", f"Uploaded {embedded_count} embedded field(s).")
            else:
                success(
                    "[qsync:push]", f"Uploaded {change_count} question(s): {qid_list}."
                )
            return
    finally:
        _timer_cm.__exit__(*sys.exc_info())


def main(argv: Optional[list[str]] = None) -> None:
    """Entry point for the `qsync` CLI."""

    # Some subcommands (and `--root`) intentionally change process CWD / env.
    # This is fine for real CLI usage (process exits), but breaks unit tests
    # that call `main()` multiple times in one process.
    try:
        original_cwd = os.getcwd()
    except FileNotFoundError:
        original_cwd = None
    original_env = {
        "QSYNC_ROOT": os.environ.get("QSYNC_ROOT"),
        "QSYNC_DATA_DIR": os.environ.get("QSYNC_DATA_DIR"),
        "QSYNC_ENV_PATH": os.environ.get("QSYNC_ENV_PATH"),
        "QSYNC_ACCOUNT": os.environ.get("QSYNC_ACCOUNT"),
        "QSYNC_ALLOW_LOCKED": os.environ.get("QSYNC_ALLOW_LOCKED"),
        "QSYNC_JSON_MODE": os.environ.get("QSYNC_JSON_MODE"),
        "QSYNC_ITEMS_ALLOW_EXTERNALLY_MANAGED_QIDS": os.environ.get(
            "QSYNC_ITEMS_ALLOW_EXTERNALLY_MANAGED_QIDS"
        ),
    }

    from .errors import QsyncError

    try:
        _main_impl(argv)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        # Ensure Ctrl+C yields a clean cancellation message rather than a stack trace.
        print("[qsync] Cancelled.", file=sys.stderr)
        raise SystemExit(130)
    except QsyncError as exc:
        from .terminal_output import error
        from .push_logger import log_push_event

        lines = exc.to_lines()
        error("[qsync]", lines[0])
        for ln in lines[1:]:
            print(ln, file=sys.stderr)
        log_push_event(
            action="qsync.error",
            method="LOCAL",
            path="cli.main",
            survey_id=None,
            status=None,
            error=exc.to_log_error(),
        )
        raise SystemExit(exc.exit_code)
    except (RuntimeError, ValueError) as exc:
        from .errors import QsyncValidationError
        from .error_catalog import get_docs_url
        from .terminal_output import error
        from .push_logger import log_push_event

        wrapped = QsyncValidationError(
            error_id="QSYNC-VALIDATION-UNSTRUCTURED-001",
            problem=str(exc) or exc.__class__.__name__,
            why=f"{exc.__class__.__name__} raised by qsync.",
            impact="Command failed due to invalid input or workspace state.",
            action="Fix the issue described above and retry. If this is unexpected, run `qsync doctor` and inspect logs.",
            docs_url=get_docs_url(),
            context={"exc_type": exc.__class__.__name__},
        )

        lines = wrapped.to_lines()
        error("[qsync]", lines[0])
        for ln in lines[1:]:
            print(ln, file=sys.stderr)
        log_push_event(
            action="qsync.error",
            method="LOCAL",
            path="cli.main",
            survey_id=None,
            status=None,
            error=wrapped.to_log_error(),
        )
        raise SystemExit(wrapped.exit_code)
    except Exception as exc:
        print(f"❌ [qsync] Unexpected error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    finally:
        # Best-effort restore for test/process safety.
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if original_cwd:
            try:
                os.chdir(original_cwd)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    main()
