"""Sync orchestrator for multi-dimension coordination.

This module provides the `qsync sync` command that orchestrates changes across
multiple dimensions (items, edf, js, translations, eos, flow, master) for one or more surveys.

Features:
- Automatic change detection across all dimensions
- Interactive dimension selection
- Per-dimension workflow (pull, preview, stage, push)
- Cross-dimension conflict detection and resolution
- Non-interactive automation with --yes
- Optional scope filtering via --scope (qid/tag/js boolean DSL)

Created: 2026-01-22 for QSYNC-HARM-022 (Stage 3: Orchestration)
"""

import json
import logging
import re
import shlex
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .pending_stage import clear_pending, list_pending, load_pending
from .dimensions import edf as edf_dimension
from .dimensions import eos as eos_dimension
from .dimensions import flow as flow_dimension
from .dimensions import items as items_dimension
from .dimensions import js as js_dimension
from .dimensions import master_detect
from .dimensions import translations as translations_dimension
from .dimensions.types import DimensionChanges
from .scope_filter import ScopeFilter
from .survey_inventory import get_focal_survey_ids, load_inventory_record
from .terminal_colors import Colors, colorize_unified_diff_lines

logger = logging.getLogger(__name__)

# Performance optimization: Cache inventory records
_inventory_cache: Optional[Dict[str, dict]] = None

BASE_DIMENSION_ORDER = ["items", "edf", "js", "translations", "eos", "flow"]
MASTER_DIMENSION_ORDER = BASE_DIMENSION_ORDER + ["master"]
ISSUE_DETAIL_MENU_THRESHOLD = 10

_EMBEDDED_FIELD_TOKEN_RE = re.compile(r"\$\{e://Field/([^}]+)\}")
_ISSUE_KEYS_SEEN: set[str] = set()


def _filter_new_issue_lines(
    issues: list[tuple[str, str, str]],
) -> list[tuple[str, str, str]]:
    fresh: list[tuple[str, str, str]] = []
    for survey_label, dim, detail in issues:
        key = f"{survey_label}::{dim}::{detail}"
        if key in _ISSUE_KEYS_SEEN:
            continue
        _ISSUE_KEYS_SEEN.add(key)
        fresh.append((survey_label, dim, detail))
    return fresh


def _get_inventory_cached(survey_id: str) -> Optional[dict]:
    """Get inventory record with caching for multi-survey operations.

    Args:
        survey_id: Survey ID

    Returns:
        Inventory record dict or None
    """
    global _inventory_cache

    if _inventory_cache is None:
        _inventory_cache = {}

    if survey_id not in _inventory_cache:
        try:
            record = load_inventory_record(survey_id)
            _inventory_cache[survey_id] = record
        except Exception:
            _inventory_cache[survey_id] = None

    return _inventory_cache[survey_id]


def _clear_inventory_cache():
    """Clear the inventory cache."""
    global _inventory_cache
    _inventory_cache = None


def _extract_embedded_field_refs(text: str) -> set[str]:
    if not text:
        return set()
    return {match.group(1).strip() for match in _EMBEDDED_FIELD_TOKEN_RE.finditer(text)}


def _collect_embedded_refs_from_changes(changes: list) -> set[str]:
    refs: set[str] = set()
    for change in changes or []:
        if getattr(change, "kind", None) == "embedded":
            continue
        new_html = getattr(change, "new_html", "") or ""
        refs.update(_extract_embedded_field_refs(new_html))
    return {ref for ref in refs if ref}


def _autofix_command(dimension: str, survey_id: str) -> Optional[str]:
    if dimension == "items":
        return f"qsync items pull --survey-id {survey_id}"
    if dimension == "edf":
        return f"qsync items repair-edf --survey-id {survey_id}"
    if dimension == "translations":
        return f"qsync items pull --survey-id {survey_id}"
    if dimension == "eos":
        return f"qsync eos pull --survey-id {survey_id}"
    if dimension == "flow":
        return f"qsync flow pull --survey-id {survey_id}"
    return None


def _fixable_detail(info: DimensionChanges) -> Optional[str]:
    """Return the actionable issue detail when a dimension can be auto-fixed."""
    if not info.safe_to_autofix:
        return None
    return info.error_detail or info.warning_detail


def _run_autofix(dimension: str, survey_id: str) -> str:
    if dimension == "items":
        from .sync_core import init_survey_to_excel
        from .workbook_resolver import WorkbookResolver

        resolver = WorkbookResolver()
        xlsx_path = resolver.resolve(survey_id)
        init_survey_to_excel(survey_id, xlsx_path)
        return f"Regenerated Excel file at {xlsx_path}"
    if dimension == "edf":
        from .workbook_resolver import WorkbookResolver

        resolver = WorkbookResolver()
        xlsx_path = resolver.resolve(survey_id)
        report = edf_dimension.repair_workbook(
            survey_id,
            xlsx_path=xlsx_path,
            dry_run=False,
            refresh_cache=False,
        )
        if not report.changed:
            return f"Embedded_Data already aligned at {xlsx_path}"
        return f"Repaired Embedded_Data in {xlsx_path}"
    if dimension == "translations":
        from .sync_core import init_survey_to_excel
        from .workbook_resolver import WorkbookResolver
        from .qualtrics_client import load_cached_survey
        from .dimensions.translations_language_blocks import (
            get_base_language as get_base_language_from_options,
            list_enabled_languages as list_enabled_languages_from_options,
        )

        resolver = WorkbookResolver()
        xlsx_path = resolver.resolve(survey_id)
        cache = load_cached_survey(survey_id)
        base_lang = get_base_language_from_options(cache.payload)
        languages = [
            lang
            for lang in list_enabled_languages_from_options(cache.payload)
            if not base_lang or lang != base_lang
        ]
        init_survey_to_excel(survey_id, xlsx_path, languages=languages)
        return f"Refreshed translation columns in {xlsx_path}"
    if dimension == "eos":
        from .eos_messages import pull_eos_messages

        pull_eos_messages(survey_id=survey_id, allow_shared=True)
        return "Pulled EOS messages to contents/qualtrics_library_messages"
    if dimension == "flow":
        flow_dimension.pull(survey_id, force=True)
        return f"Pulled flow to surveys/flow/{survey_id}/flow.yaml"
    raise ValueError(f"Unknown auto-fix dimension: {dimension}")


@dataclass
class SurveyChanges:
    """Detected changes across all dimensions for a survey."""

    survey_id: str
    survey_name: str
    dimensions: Dict[str, DimensionChanges]

    @property
    def has_any_changes(self) -> bool:
        """Check if any dimension has changes."""
        return any(d.has_changes for d in self.dimensions.values())

    @property
    def changed_dimensions(self) -> List[str]:
        """Get list of dimensions with changes."""
        return [
            name for name, changes in self.dimensions.items() if changes.has_changes
        ]

    @property
    def has_any_issues(self) -> bool:
        """Check if any dimension reports warnings/errors."""
        return any(
            bool(d.error_detail) or bool(d.warning_detail)
            for d in self.dimensions.values()
        )


@dataclass
class Conflict:
    """Represents a conflict between dimensions."""

    qid: str
    dimensions: List[str]
    descriptions: Dict[str, str]  # dimension -> description of change

    def __str__(self) -> str:
        dim_list = ", ".join(self.dimensions)
        return f"QID {self.qid} modified in: {dim_list}"


@dataclass
class DimensionSyncResult:
    """Result of syncing a single dimension."""

    dimension: str
    success: bool
    applied_changes: bool = False
    error_message: Optional[str] = None


@dataclass
class SurveySyncSummary:
    """Summary of sync results for a survey."""

    survey_id: str
    survey_name: str
    dimension_results: Dict[str, DimensionSyncResult]

    @property
    def success(self) -> bool:
        """Check if all dimensions synced successfully."""
        return all(r.success for r in self.dimension_results.values())

    @property
    def synced_dimensions(self) -> List[str]:
        """Get list of successfully synced dimensions."""
        return [
            name for name, result in self.dimension_results.items() if result.success
        ]


def _requires_force_live_retry(result: Optional[DimensionSyncResult]) -> bool:
    if result is None or result.success:
        return False
    msg = (result.error_message or "").lower()
    return "re-run with --force-live" in msg


def _is_dimension_staged(survey_id: str, dimension: str) -> bool:
    """Check if dimension has staged changes in pending cache.

    Args:
        survey_id: Survey ID
        dimension: Dimension name

    Returns:
        True if dimension has staged changes
    """
    pending = load_pending(survey_id, dimension)
    return pending is not None


def _js_pending_out_of_sync(
    survey_id: str,
    *,
    scope: Optional[ScopeFilter] = None,
) -> bool:
    from .pending_stage import JsPendingPayload
    from .dimensions.js_preview import preview_differences
    from .config import resolve_root

    pending = load_pending(survey_id, "js")
    if not pending or not isinstance(pending.payload, JsPendingPayload):
        return False

    qids = [
        entry.get("qid")
        for entry in pending.payload.entries
        if isinstance(entry, dict) and entry.get("qid")
    ]
    if not qids:
        return False

    root = resolve_root(required=False) or Path.cwd()
    mapping_csv = root / "survey_js" / "survey_qid_js_map.csv"
    if not mapping_csv.exists():
        return False

    scope_expr = scope.expression if scope and scope.expression else None
    try:
        results = preview_differences(
            survey_id=survey_id,
            mapping_csv=mapping_csv,
            show_equal=True,
            detailed=False,
            include_qids=set(qids),
            interactive=False,
            verbose=False,
            scope_expr=scope_expr,
            check_drift=False,
        )
    except Exception:
        return False

    if not results:
        return False

    return any(result.status != "match" for result in results)


def detect_dimension_changes(survey_id: str, dimension: str) -> DimensionChanges:
    """Detect changes for a specific dimension.

    Args:
        survey_id: Survey ID
        dimension: Dimension name (items, js, translations, eos, flow)

    Returns:
        DimensionChanges with detection status and affected QIDs
    """
    try:
        if dimension == "items":
            return items_dimension.detect_changes(survey_id)
        if dimension == "edf":
            return edf_dimension.detect_changes(survey_id)
        if dimension == "js":
            return js_dimension.detect_changes(survey_id)
        if dimension == "translations":
            return translations_dimension.detect_changes(survey_id)
        if dimension == "eos":
            return eos_dimension.detect_changes(survey_id)
        if dimension == "flow":
            return flow_dimension.detect_changes(survey_id)

        return DimensionChanges(
            dimension=dimension,
            has_changes=False,
            change_summary="No changes",
            affected_qids=set(),
            status_kind="none",
            edit_count=0,
        )

    except Exception as e:
        # Don't log full error during detection - just return error status
        # Error will be shown in the table with explanation below
        error_msg = str(e).split("\n")[0]  # First line only

        # Create user-friendly explanation and check if auto-fix is safe
        safe_to_fix = False
        if "Embedded_Data sheet is missing rows" in error_msg:
            # Check if there are unstaged changes - if not, it's safe to reinit
            try:
                from .sync_core import preview_changes
                from .workbook_resolver import WorkbookResolver

                resolver = WorkbookResolver()
                xlsx_path = resolver.resolve(survey_id)
                if xlsx_path.exists():
                    changes = preview_changes(survey_id, xlsx_path, check_drift=False)
                    safe_to_fix = not changes  # Safe if no unstaged changes
            except Exception:
                pass  # If we can't check, assume not safe

            if safe_to_fix:
                detail = "Excel workbook missing embedded data fields. Can auto-fix (no unstaged changes detected)."
            else:
                detail = (
                    "Excel workbook missing embedded data fields. Manual fix needed: "
                    f"qsync items pull --survey-id {survey_id} "
                    "(warning: may overwrite unstaged changes)"
                )
        elif "Mapping CSV missing a column" in error_msg:
            detail = "Survey not in JS mapping file. Add column to survey_js/survey_qid_js_map.csv"
            safe_to_fix = False  # Requires manual editing
        else:
            detail = error_msg[:100]  # Generic truncated error
            safe_to_fix = False

        return DimensionChanges(
            dimension=dimension,
            has_changes=True,  # Mark as having changes so it appears in table
            change_summary="✗ error",
            affected_qids=set(),
            error_detail=detail.replace("{survey_id}", survey_id),
            safe_to_autofix=safe_to_fix,
            status_kind="error",
            edit_count=0,
        )


def detect_conflicts(changes: SurveyChanges) -> List[Conflict]:
    """Detect conflicts between dimension changes.

    Conflicts occur when multiple dimensions modify the same QIDs.

    Args:
        changes: Detected changes for a survey

    Returns:
        List of detected conflicts
    """
    conflicts = []

    # Build map of QID -> dimensions that modify it
    qid_dimensions: Dict[str, List[str]] = {}

    for dim_name, dim_changes in changes.dimensions.items():
        if not dim_changes.has_changes:
            continue

        for qid in dim_changes.affected_qids:
            if qid not in qid_dimensions:
                qid_dimensions[qid] = []
            qid_dimensions[qid].append(dim_name)

    # Identify conflicts (QIDs modified by multiple dimensions)
    for qid, dims in qid_dimensions.items():
        if len(dims) > 1:
            descriptions = {}
            for dim in dims:
                descriptions[dim] = changes.dimensions[dim].change_summary

            conflicts.append(
                Conflict(qid=qid, dimensions=dims, descriptions=descriptions)
            )

    return conflicts


def detect_master_conflicts(changes: SurveyChanges) -> List[str]:
    """Detect potential conflicts involving master dimension.

    Master dimension doesn't track QIDs, so conflicts are detected based on:
    - Both master and translations having staged changes (metadata overlap)
    - Master having changes when other dimensions have unstaged changes

    Args:
        changes: Detected changes for a survey

    Returns:
        List of warning messages
    """
    warnings = []
    master_changes = changes.dimensions.get("master")

    if not master_changes or not master_changes.has_changes:
        return warnings

    # Check if master + translations both have staged changes
    translations_changes = changes.dimensions.get("translations")
    if translations_changes and translations_changes.has_changes:
        if master_changes.status_kind == "staged" and translations_changes.status_kind == "staged":
            warnings.append(
                "⚠ Both master and translations have staged changes. "
                "Consider pushing translations first to avoid metadata conflicts."
            )

    # Check if master has changes and other dimensions have unstaged changes
    has_unstaged_other = False
    for dim_name, dim_changes in changes.dimensions.items():
        if dim_name == "master":
            continue
        if dim_changes.has_changes and dim_changes.status_kind == "unstaged":
            has_unstaged_other = True
            break

    if master_changes.status_kind == "staged" and has_unstaged_other:
        warnings.append(
            "⚠ Master has staged changes while other dimensions have unstaged changes. "
            "Review changes carefully to avoid conflicts."
        )

    return warnings


def resolve_conflict_interactive(conflict: Conflict) -> List[str]:
    """Prompt user to resolve a conflict interactively.

    Args:
        conflict: Conflict to resolve

    Returns:
        List of dimension names to apply (in order)
    """
    from .interactive_menu import select_from_list

    print(f"\n{Colors.YELLOW}⚠ Conflict detected on {conflict.qid}{Colors.RESET}")
    print(f"{Colors.DIM}Modified in: {', '.join(conflict.dimensions)}{Colors.RESET}")

    for dim in conflict.dimensions:
        print(f"  • {dim}: {conflict.descriptions[dim]}")

    # Build choices
    choices = []
    for dim in conflict.dimensions:
        choices.append(f"Apply {dim} only")
    choices.append("─" * 40)
    choices.append(
        "✓ Apply all (safe merge: items → edf → js → translations → eos → flow → master)"
    )
    choices.append("✗ Skip this QID")

    selection = select_from_list(
        message="Resolve conflict:",
        choices=choices,
    )

    if selection is None or "Skip" in selection:
        return []
    elif "Apply all" in selection:
        # Safe merge order: items first, then js, then translations, then flow
        order = ["items", "js", "translations", "eos", "flow"]
        return [d for d in order if d in conflict.dimensions]
    elif "─" in selection:
        return []
    else:
        # Extract dimension name from "Apply {dim} only"
        for dim in conflict.dimensions:
            if dim in selection:
                return [dim]
        return []


def resolve_conflicts_interactive(conflicts: List[Conflict]) -> Dict[str, List[str]]:
    """Resolve all conflicts interactively.

    Args:
        conflicts: List of conflicts to resolve

    Returns:
        Dict mapping QID to list of dimensions to apply (in order)
    """
    resolutions = {}

    print(f"\n[sync:conflict] {len(conflicts)} conflict(s) detected")

    for conflict in conflicts:
        dims = resolve_conflict_interactive(conflict)
        resolutions[conflict.qid] = dims

    return resolutions


def resolve_conflicts_auto(conflicts: List[Conflict]) -> Dict[str, List[str]]:
    """Resolve conflicts automatically using safe merge strategy.

    Safe merge order: items → edf → js → translations → eos → flow → master

    Args:
        conflicts: List of conflicts to resolve

    Returns:
        Dict mapping QID to list of dimensions to apply (in order)
    """
    resolutions = {}
    order = ["items", "js", "translations", "eos", "flow"]

    for conflict in conflicts:
        # Apply all dimensions in safe merge order
        resolutions[conflict.qid] = [d for d in order if d in conflict.dimensions]

        print(
            f"[sync:conflict] Auto-resolving {conflict.qid}: {' → '.join(resolutions[conflict.qid])}"
        )

    return resolutions


def detect_survey_changes(survey_id: str) -> SurveyChanges:
    """Detect changes across all dimensions for a survey.

    Args:
        survey_id: Survey ID

    Returns:
        SurveyChanges with detected changes per dimension
    """
    logger.info(f"[sync] Detecting changes for survey {survey_id}...")

    # Get survey name from cached inventory
    record = _get_inventory_cached(survey_id)
    survey_name = record.get("name", survey_id) if record else survey_id

    # Detect changes in each dimension
    dimensions = {
        "items": detect_dimension_changes(survey_id, "items"),
        "edf": detect_dimension_changes(survey_id, "edf"),
        "js": detect_dimension_changes(survey_id, "js"),
        "translations": detect_dimension_changes(survey_id, "translations"),
        "eos": detect_dimension_changes(survey_id, "eos"),
        "flow": detect_dimension_changes(survey_id, "flow"),
        "master": detect_dimension_changes(survey_id, "master"),
    }

    return SurveyChanges(
        survey_id=survey_id, survey_name=survey_name, dimensions=dimensions
    )


def _visible_length(text: str) -> int:
    """Calculate visible length of text excluding ANSI color codes."""
    import re

    ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
    return len(ansi_escape.sub("", text))


def _pad_to_width(text: str, width: int) -> str:
    """Pad text to specified width, accounting for ANSI codes."""
    visible_len = _visible_length(text)
    if visible_len < width:
        return text + " " * (width - visible_len)
    return text


def render_cell(status: DimensionChanges) -> str:
    """Pure cell renderer for compact table badges."""
    if status.error_detail:
        return "✗ error"

    if status.has_changes:
        count = int(status.edit_count or 0)
        if status.status_kind == "staged":
            base = f"✓ {count}" if count > 0 else "✓ staged"
        else:
            base = f"⚡ {count}" if count > 0 else "⚡"
    elif status.warning_detail:
        base = "⚠"
    else:
        base = "─"

    if status.warning_detail and "⚠" not in base:
        base = f"{base} ⚠"
    return base


def display_change_detection_table(
    all_changes: List[SurveyChanges],
    show_all: bool = False,
    *,
    interactive: bool = False,
    issue_detail_threshold: int = ISSUE_DETAIL_MENU_THRESHOLD,
):
    """Display survey × dimension change detection table.

    Args:
        all_changes: List of detected changes for all surveys
        show_all: If True, show all surveys including those with no changes
        interactive: Enable issue-detail selection menus for long issue lists
        issue_detail_threshold: Hide issue details behind a menu when list length exceeds this threshold
    """

    # Filter to surveys with changes unless show_all is True
    display_changes = (
        all_changes if show_all else [c for c in all_changes if c.has_any_changes]
    )

    if not display_changes:
        return

    print(f"\n{Colors.BLUE}═══ Change Detection Results ═══{Colors.RESET}")

    # Column widths
    col_survey_id = 22
    col_name = 30
    col_dim = 14

    # Header
    header = (
        f"{Colors.DIM}"
        f"{'Survey ID':<{col_survey_id}} "
        f"{'Name':<{col_name}} "
        f"{'Items':<{col_dim}} "
        f"{'EDF':<{col_dim}} "
        f"{'JS':<{col_dim}} "
        f"{'Trans':<{col_dim}} "
        f"{'EOS':<{col_dim}} "
        f"{'Flow':<{col_dim}} "
        f"{'Master':<{col_dim}}"
        f"{Colors.RESET}"
    )
    separator = (
        f"{Colors.DIM}"
        f"{'─' * (col_survey_id + col_name + col_dim * 7 + 7)}"
        f"{Colors.RESET}"
    )

    print(header)
    print(separator)

    for changes in display_changes:
        # Get status for each dimension - show actual summary or dash
        def format_status(dim_changes: DimensionChanges) -> str:
            summary = render_cell(dim_changes)
            max_len = col_dim - 1
            if len(summary) > max_len:
                summary = summary[: max_len - 1] + "…"
            if summary.startswith("✗"):
                return f"{Colors.RED}{summary}{Colors.RESET}"
            if summary.startswith("✓"):
                return f"{Colors.GREEN}{summary}{Colors.RESET}"
            if summary.startswith("⚡"):
                return f"{Colors.YELLOW}{summary}{Colors.RESET}"
            if summary.startswith("⚠"):
                return f"{Colors.YELLOW}{summary}{Colors.RESET}"
            if summary == "─":
                return f"{Colors.DIM}{summary}{Colors.RESET}"
            return summary

        items_status = format_status(changes.dimensions["items"])
        edf_status = format_status(changes.dimensions["edf"])
        js_status = format_status(changes.dimensions["js"])
        trans_status = format_status(changes.dimensions["translations"])
        eos_status = format_status(changes.dimensions["eos"])
        flow_status = format_status(changes.dimensions["flow"])
        master_status = format_status(changes.dimensions["master"])

        # Truncate name if needed
        name = (
            changes.survey_name[: col_name - 2]
            if len(changes.survey_name) > col_name
            else changes.survey_name
        )

        # Survey ID with optional highlighting
        if changes.has_any_changes:
            sid_display = f"{Colors.YELLOW}{changes.survey_id}{Colors.RESET}"
        else:
            sid_display = changes.survey_id

        # Build row with proper padding
        row = (
            f"{_pad_to_width(sid_display, col_survey_id)} "
            f"{_pad_to_width(name, col_name)} "
            f"{_pad_to_width(items_status, col_dim)} "
            f"{_pad_to_width(edf_status, col_dim)} "
            f"{_pad_to_width(js_status, col_dim)} "
            f"{_pad_to_width(trans_status, col_dim)} "
            f"{_pad_to_width(eos_status, col_dim)} "
            f"{_pad_to_width(flow_status, col_dim)} "
            f"{_pad_to_width(master_status, col_dim)}"
        )
        print(row)

    # Print error explanations if any
    errors: list[tuple[str, str, str]] = []
    warnings: list[tuple[str, str, str]] = []
    for changes in display_changes:
        from .survey_ref import format_survey_ref

        survey_label = format_survey_ref(changes.survey_id, changes.survey_name)
        for dim_name, dim_changes in changes.dimensions.items():
            if dim_changes.error_detail:
                errors.append((survey_label, dim_name, dim_changes.error_detail))
            if dim_changes.warning_detail:
                warnings.append((survey_label, dim_name, dim_changes.warning_detail))
    errors = list(dict.fromkeys(errors))
    warnings = list(dict.fromkeys(warnings))

    errors = _filter_new_issue_lines(errors)
    warnings = _filter_new_issue_lines(warnings)

    def _render_issue_detail(detail: str, separators: tuple[str, ...]) -> str:
        for separator in separators:
            if separator not in detail:
                continue
            parts = detail.split(separator, 1)
            if len(parts) != 2:
                continue
            prefix = parts[0].strip()
            cmd = parts[1].strip()
            return f"{prefix} {separator} {Colors.CYAN}{cmd}{Colors.RESET}"
        return detail

    def _select_issue_rows(
        issue_type: str,
        rows: list[tuple[str, str, str]],
    ) -> list[tuple[str, str, str]]:
        if not interactive or len(rows) <= issue_detail_threshold:
            return rows

        from .interactive_menu import select_from_list

        choices = [
            f"Show first {issue_detail_threshold} {issue_type}",
            f"Show all {len(rows)} {issue_type}",
            f"Continue without {issue_type}",
        ]
        selection = select_from_list(
            message=(
                f"{len(rows)} {issue_type} detected. "
                f"Show details?"
            ),
            choices=choices,
        )
        if selection is None or selection == choices[2]:
            print(
                f"{Colors.DIM}{len(rows)} {issue_type} hidden. Continue to survey selection.{Colors.RESET}"
            )
            return []
        if selection == choices[1]:
            return rows
        return rows[:issue_detail_threshold]

    def _print_issue_rows(
        title: str,
        issue_type: str,
        rows: list[tuple[str, str, str]],
        *,
        separators: tuple[str, ...],
    ) -> None:
        if not rows:
            return
        print(f"\n{Colors.YELLOW}{title}{Colors.RESET}")
        selected_rows = _select_issue_rows(issue_type, rows)
        for survey_name, dimension, detail in selected_rows:
            rendered = _render_issue_detail(detail, separators)
            print(
                f"  {Colors.DIM}•{Colors.RESET} {survey_name} ({dimension}): {rendered}"
            )
        hidden_count = len(rows) - len(selected_rows)
        if hidden_count > 0 and selected_rows:
            print(
                f"  {Colors.DIM}… {hidden_count} more {issue_type} hidden{Colors.RESET}"
            )

    _print_issue_rows(
        title="⚠️  Errors detected:",
        issue_type="errors",
        rows=errors,
        separators=("Run:", "Add"),
    )
    _print_issue_rows(
        title="⚠️  Warnings:",
        issue_type="warnings",
        rows=warnings,
        separators=("Run:", "Repair:"),
    )


def display_sync_summary_table(summaries: List[SurveySyncSummary]):
    """Display survey × dimension sync results table.

    Args:
        summaries: List of sync summaries for processed surveys
    """

    if not summaries:
        return

    print(f"\n{Colors.BLUE}═══ Sync Results ═══{Colors.RESET}")
    print(
        f"{Colors.DIM}{'Survey ID':<22} {'Name':<30} {'Items':<12} {'EDF':<12} {'JS':<12} {'Trans':<12} {'EOS':<12} {'Flow':<12} {'Master':<12}{Colors.RESET}"
    )
    print(f"{Colors.DIM}{'─' * 142}{Colors.RESET}")

    for summary in summaries:
        # Get status for each dimension with colors
        def get_status(dim: str) -> str:
            if dim in summary.dimension_results:
                result = summary.dimension_results[dim]
                if not result.success:
                    return f"{Colors.RED}✗ failed{Colors.RESET}"
                if result.applied_changes:
                    return f"{Colors.GREEN}✓ pushed{Colors.RESET}"
                return f"{Colors.GREEN}✓ no changes{Colors.RESET}"
            return f"{Colors.DIM}─{Colors.RESET}"

        items_status = get_status("items")
        edf_status = get_status("edf")
        js_status = get_status("js")
        trans_status = get_status("translations")
        eos_status = get_status("eos")
        flow_status = get_status("flow")
        master_status = get_status("master")

        # Truncate name if needed
        name = (
            summary.survey_name[:28]
            if len(summary.survey_name) > 28
            else summary.survey_name
        )

        print(
            f"{summary.survey_id:<22} {name:<30} {items_status:<16} {edf_status:<16} {js_status:<16} {trans_status:<16} {eos_status:<16} {flow_status:<16} {master_status:<16}"
        )

    print(
        f"\n{Colors.DIM}Legend: {Colors.GREEN}✓ pushed{Colors.RESET} = updated, {Colors.GREEN}✓ no changes{Colors.RESET} = no-op, {Colors.RED}✗ failed{Colors.RESET} = error, {Colors.DIM}─{Colors.RESET} = skipped{Colors.RESET}"
    )


def display_qid_mode_change_table(
    survey_ref: str,
    *,
    scope_label: str,
    unstaged: Dict[str, DimensionChanges],
) -> None:
    """Display a small change table for QID-mode scoped changes."""

    col_scope = 22
    col_dim = 14

    def _format_status(dim_changes: DimensionChanges) -> str:
        summary = render_cell(dim_changes)
        max_len = col_dim - 1
        if len(summary) > max_len:
            summary = summary[: max_len - 1] + "…"
        if summary.startswith("✗"):
            return f"{Colors.RED}{summary}{Colors.RESET}"
        if summary.startswith("⚠"):
            return f"{Colors.YELLOW}{summary}{Colors.RESET}"
        if summary.startswith("⚡"):
            return f"{Colors.YELLOW}{summary}{Colors.RESET}"
        if summary.startswith("✓"):
            return f"{Colors.GREEN}{summary}{Colors.RESET}"
        if summary == "─":
            return f"{Colors.DIM}{summary}{Colors.RESET}"
        return summary

    print(
        f"\n{Colors.BLUE}═══ QID-mode Change Detection {survey_ref} ═══{Colors.RESET}"
    )

    header = (
        f"{Colors.DIM}"
        f"{'Scope':<{col_scope}} "
        f"{'Items':<{col_dim}} "
        f"{'EDF':<{col_dim}} "
        f"{'JS':<{col_dim}} "
        f"{'Trans':<{col_dim}}"
        f"{Colors.RESET}"
    )
    separator = f"{Colors.DIM}{'─' * (col_scope + col_dim * 4 + 4)}{Colors.RESET}"

    scope_display = (
        scope_label[: col_scope - 1] + "…"
        if len(scope_label) > col_scope
        else scope_label
    )
    row = (
        f"{_pad_to_width(scope_display, col_scope)} "
        f"{_pad_to_width(_format_status(unstaged['items']), col_dim)} "
        f"{_pad_to_width(_format_status(unstaged['edf']), col_dim)} "
        f"{_pad_to_width(_format_status(unstaged['js']), col_dim)} "
        f"{_pad_to_width(_format_status(unstaged['translations']), col_dim)}"
    )

    print(header)
    print(separator)
    print(row)


def _prompt_qid_mode_dimension_selection(
    unstaged: Dict[str, DimensionChanges],
    *,
    allow_force: bool = True,
) -> List[str]:
    """Prompt for a dimension to sync in QID-mode, using scoped detection."""
    from .interactive_menu import select_from_list

    dims = ["items", "js", "translations"]

    def count_label(dim_changes: DimensionChanges) -> str:
        if dim_changes.error_detail:
            return "✗ error"
        if not dim_changes.has_changes:
            if dim_changes.warning_detail:
                return "⚠"
            return "none"
        count = int(dim_changes.edit_count or 0)
        if count > 0:
            prefix = "✓" if dim_changes.status_kind == "staged" else "⚡"
            return f"{prefix} {count}"
        return render_cell(dim_changes)

    changed = [
        d for d in dims if unstaged[d].has_changes and not unstaged[d].error_detail
    ]

    def build_force_choices() -> List[str]:
        return [
            f"items ({count_label(unstaged['items'])})",
            f"js ({count_label(unstaged['js'])})",
            f"translations ({count_label(unstaged['translations'])})",
            "all (items/js/translations)",
            "↩ Cancel",
        ]

    if not changed:
        print(
            f"{Colors.DIM}No scoped changes detected for items/js/translations.{Colors.RESET}"
        )
        if not allow_force:
            return []
        selection = select_from_list(
            message="Choose a dimension to force-sync anyway:",
            choices=build_force_choices(),
        )
        if selection is None or "Cancel" in selection:
            return []
        if selection.startswith("all"):
            return dims
        return [selection.split(" ", 1)[0]]

    changed_choices = [f"{d} ({count_label(unstaged[d])})" for d in changed]
    choices: List[str] = []
    choices.extend(changed_choices)
    if len(changed) > 1:
        choices.append(f"all changed ({'/'.join(changed)})")
    if allow_force:
        choices.append("─" * 40)
        choices.append("Show all dimensions (force)")
    choices.append("↩ Cancel")

    selection = select_from_list(
        message="Which dimension to sync for this scope?",
        choices=choices,
    )
    if selection is None or "Cancel" in selection:
        return []
    if selection.startswith("all changed"):
        return changed
    if selection.startswith("Show all"):
        selection2 = select_from_list(
            message="Choose a dimension to sync:",
            choices=build_force_choices(),
        )
        if selection2 is None or "Cancel" in selection2:
            return []
        if selection2.startswith("all"):
            return dims
        return [selection2.split(" ", 1)[0]]
    return [selection.split(" ", 1)[0]]


def display_recovery_instructions(
    summaries: List[SurveySyncSummary],
    force_live: bool = False,
    force_preview: bool = False,
    scope_expr: Optional[str] = None,
    auto_yes: bool = False,
):
    """Display recovery commands for failed syncs.

    Args:
        summaries: List of sync summaries for processed surveys
        force_live: Whether --force-live was used
        force_preview: Whether --force-preview was used
        scope_expr: Optional scope expression to include in retry commands
        auto_yes: Whether --yes was used in the original run
    """
    from shlex import quote

    # Find any failures
    failed_summaries = [s for s in summaries if not s.success]

    if not failed_summaries:
        return

    print(f"\n{Colors.YELLOW}═══ Recovery Instructions ═══{Colors.RESET}")
    print(
        f"{Colors.DIM}The following commands can retry failed operations:{Colors.RESET}\n"
    )

    for summary in failed_summaries:
        # Get failed dimensions
        failed_dims = [
            name
            for name, result in summary.dimension_results.items()
            if not result.success
        ]

        # Get successful dimensions
        success_dims = [
            name for name, result in summary.dimension_results.items() if result.success
        ]

        if not failed_dims:
            continue

        # Build recovery command
        cmd_parts = ["qsync sync"]
        cmd_parts.append(f"--survey-id {summary.survey_id}")
        cmd_parts.append(f"--dimensions {','.join(failed_dims)}")
        if scope_expr:
            cmd_parts.append(f"--scope {quote(scope_expr)}")

        # Add force flags if they were used
        if force_live:
            cmd_parts.append("--force-live")
        if force_preview:
            cmd_parts.append("--force-preview")
        if auto_yes:
            cmd_parts.append("--yes")

        cmd = " ".join(cmd_parts)
        pending_cmd = f"ls surveys/pending/*/{summary.survey_id}.json"
        review_cmd = f"qsync sync --survey-id {summary.survey_id}"
        if scope_expr:
            review_cmd += f" --scope {quote(scope_expr)}"

        print(
            f"{Colors.BOLD}{summary.survey_name}{Colors.RESET} ({summary.survey_id}):"
        )
        print(f"  {Colors.RED}Failed:{Colors.RESET} {', '.join(failed_dims)}")
        if success_dims:
            print(f"  {Colors.GREEN}Succeeded:{Colors.RESET} {', '.join(success_dims)}")
        print(f"  {Colors.CYAN}Retry:{Colors.RESET} {cmd}")
        print(f"  {Colors.CYAN}Pending:{Colors.RESET} {pending_cmd}")
        print(f"  {Colors.CYAN}Review:{Colors.RESET} {review_cmd}")
        print()

    print(
        f"{Colors.DIM}Note: Successful dimensions are preserved and won't be re-synced.{Colors.RESET}"
    )
    if auto_yes:
        print(
            f"{Colors.DIM}Tip: Re-run without --yes for the interactive review menu.{Colors.RESET}"
        )


def prompt_dimension_selection(changes: SurveyChanges, interactive: bool) -> List[str]:
    """Prompt user to select dimensions to sync.

    Args:
        changes: Detected changes for survey
        interactive: Whether to prompt interactively

    Returns:
        List of dimension names to sync
    """
    from .interactive_menu import select_from_list

    changed = changes.changed_dimensions

    if not changed:
        print(f"[sync] No staged changes detected for survey {changes.survey_id}")
        print("[sync] Run pull/preview/stage commands first to prepare changes")
        return []

    if not interactive:
        # Non-interactive: sync all changed dimensions
        return changed

    # Interactive prompt
    print(
        f"\n{Colors.BLUE}[sync]{Colors.RESET} Changes detected in: {Colors.BOLD}{', '.join(changed)}{Colors.RESET}"
    )
    for dim in changed:
        summary = changes.dimensions[dim].change_summary
        print(f"  • {dim}: {Colors.DIM}{summary}{Colors.RESET}")

    # Show error explanations if any
    errors = []
    for dim in ["items", "js", "translations", "eos", "flow"]:
        if changes.dimensions[dim].error_detail:
            errors.append((dim, changes.dimensions[dim].error_detail))

    if errors:
        from .survey_ref import format_survey_ref

        survey_label = format_survey_ref(changes.survey_id, changes.survey_name)
        errors = _filter_new_issue_lines(
            [(survey_label, dim, detail) for dim, detail in errors]
        )
        print(f"\n{Colors.YELLOW}⚠️  Errors:{Colors.RESET}")
        for _, dimension, detail in errors:
            # Highlight commands in error messages
            if "Run:" in detail:
                parts = detail.split("Run:")
                if len(parts) == 2:
                    prefix = parts[0].strip()
                    cmd = parts[1].strip()
                    print(
                        f"  {Colors.DIM}•{Colors.RESET} {dimension}: {prefix} Run: {Colors.CYAN}{cmd}{Colors.RESET}"
                    )
                else:
                    print(f"  {Colors.DIM}•{Colors.RESET} {dimension}: {detail}")
            else:
                print(f"  {Colors.DIM}•{Colors.RESET} {dimension}: {detail}")
        print()  # Blank line after errors

    warnings = []
    for dim in MASTER_DIMENSION_ORDER:
        if dim not in changes.dimensions:
            continue
        if changes.dimensions[dim].warning_detail:
            warnings.append((dim, changes.dimensions[dim].warning_detail))

    if warnings:
        from .survey_ref import format_survey_ref

        survey_label = format_survey_ref(changes.survey_id, changes.survey_name)
        warnings = _filter_new_issue_lines(
            [(survey_label, dim, detail) for dim, detail in warnings]
        )
        print(f"\n{Colors.YELLOW}⚠️  Warnings:{Colors.RESET}")
        for _, dimension, detail in warnings:
            if "Run:" in detail or "Repair:" in detail:
                parts = (
                    detail.split("Run:")
                    if "Run:" in detail
                    else detail.split("Repair:")
                )
                if len(parts) == 2:
                    prefix = parts[0].strip()
                    cmd = parts[1].strip()
                    separator = "Run:" if "Run:" in detail else "Repair:"
                    print(
                        f"  {Colors.DIM}•{Colors.RESET} {dimension}: {prefix} {separator} {Colors.CYAN}{cmd}{Colors.RESET}"
                    )
                else:
                    print(f"  {Colors.DIM}•{Colors.RESET} {dimension}: {detail}")
            else:
                print(f"  {Colors.DIM}•{Colors.RESET} {dimension}: {detail}")
        print()

    # Build choices
    choices = []
    for dim in changed:
        choices.append(dim)
    choices.append("─" * 40)
    choices.append("✓ All dimensions")
    choices.append("✗ Skip this survey")

    staged_dims = [
        dim for dim in changed if _is_dimension_staged(changes.survey_id, dim)
    ]
    if staged_dims:
        choices.append("─" * 40)
        staged_label = ", ".join(staged_dims)
        choices.append(f"🧹 Clear staged changes ({staged_label})")

    # Add auto-fix section if there are fixable errors
    fixable_errors = [
        (dim, changes.dimensions[dim])
        for dim in MASTER_DIMENSION_ORDER
        if dim in changes.dimensions
        if changes.dimensions[dim].error_detail
        and changes.dimensions[dim].safe_to_autofix
    ]

    if fixable_errors:
        choices.append("")  # Empty line for spacing
        choices.append("─" * 40)
        choices.append("🔧 Fix errors:")
        for dim, dim_changes in fixable_errors:
            cmd = _autofix_command(dim, changes.survey_id)
            if cmd:
                choices.append(f"  → Fix {dim} error (run {cmd})")
            else:
                choices.append(f"  → Fix {dim} error (manual)")

    selection = select_from_list(
        message="Select dimensions to sync:",
        choices=choices,
    )

    if selection is None or "Skip" in selection:
        return []
    elif "All dimensions" in selection:
        return changed
    elif selection.startswith("🧹 Clear staged changes"):
        from .interactive_menu import confirm

        if not staged_dims:
            return []

        should_clear = confirm(
            message=f"Clear staged changes for {changes.survey_id}?",
            default=False,
        )
        if not should_clear:
            return []

        for dim in staged_dims:
            clear_pending(changes.survey_id, dim)
        print(
            f"{Colors.GREEN}✓{Colors.RESET} Cleared staged changes: {', '.join(staged_dims)}"
        )
        print(f"{Colors.DIM}Re-run 'qsync sync' to detect changes.{Colors.RESET}")
        return []
    elif selection.startswith("  → Fix"):
        # Extract dimension name from "  → Fix {dim} error"
        dim_name = selection.split("Fix ")[1].split(" error")[0]
        # Auto-fix the error
        from .terminal_output import info

        info("[sync]", f"Auto-fixing {dim_name} error for {changes.survey_id}...")

        try:
            result = _run_autofix(dim_name, changes.survey_id)
            print(f"{Colors.GREEN}✓{Colors.RESET} {result}")
            print(f"{Colors.DIM}Re-run 'qsync sync' to detect changes{Colors.RESET}")
        except Exception as e:
            print(f"{Colors.RED}✗ Failed to fix: {e}{Colors.RESET}")

        return []  # Don't sync after fixing - let user re-run
    elif "─" in selection or selection == "" or "Fix errors:" in selection:
        # Separator, empty line, or section header - ignore
        return []
    elif selection in changed:
        return [selection]
    else:
        return []


def stage_dimension(
    survey_id: str,
    dimension: str,
    *,
    scope: Optional[ScopeFilter] = None,
    ignore_embedded: bool = False,
    allow_drift: bool = False,
    interactive: bool = True,
) -> bool:
    """Stage changes for a dimension without pushing.

    Args:
        survey_id: Survey ID
        dimension: Dimension name (items, edf, js, translations, eos, flow, master)
        scope: Optional scope filter

    Returns:
        True if staging succeeded, False otherwise
    """
    try:
        if dimension == "items":
            return items_dimension.stage(
                survey_id,
                scope=scope,
                ignore_embedded=True if not ignore_embedded else ignore_embedded,
                allow_drift=allow_drift,
                interactive=interactive,
            )

        if dimension == "edf":
            return edf_dimension.stage(
                survey_id,
                scope=scope,
                allow_drift=allow_drift,
                interactive=interactive,
            )

        if dimension == "js":
            return js_dimension.stage(
                survey_id,
                scope=scope,
                allow_drift=allow_drift,
                interactive=interactive,
            )

        if dimension == "translations":
            return translations_dimension.stage(
                survey_id,
                scope=scope,
                allow_drift=allow_drift,
                interactive=interactive,
            )

        if dimension == "eos":
            return eos_dimension.stage(
                survey_id,
                scope=scope,
                allow_drift=allow_drift,
                interactive=interactive,
            )

        if dimension == "flow":
            return flow_dimension.stage(
                survey_id,
                allow_drift=allow_drift,
                interactive=interactive,
            )

        logger.warning(f"[sync:stage] Unknown dimension: {dimension}")
        return False

    except Exception as e:
        logger.error(f"[sync:stage] Error staging {dimension}: {e}", exc_info=True)
        return False


def sync_dimension(
    survey_id: str,
    dimension: str,
    interactive: bool,
    force_live: bool,
    force_preview: bool,
    auto_yes: bool,
    allow_drift: bool,
    skip_publish: bool,
    scope: Optional[ScopeFilter] = None,
    prefer_pending: bool | None = None,
    ignore_embedded: bool = False,
) -> DimensionSyncResult:
    """Sync a single dimension for a survey (push staged changes).

    Args:
        survey_id: Survey ID
        dimension: Dimension name (items, edf, js, translations, eos, flow, master)
        interactive: Whether to prompt interactively
        force_live: Force push despite live responses
        force_preview: Suppress preview-only response warnings
        auto_yes: Skip all confirmation prompts
        scope: Optional scope filter

    Returns:
        DimensionSyncResult with success + whether any changes were applied
    """
    from .survey_ref import format_survey_ref

    inv = _get_inventory_cached(survey_id) or {}
    survey_ref = format_survey_ref(
        survey_id, str(inv.get("name") or "").strip() or None
    )
    print(f"\n[sync:{dimension}] Pushing {survey_ref}...")

    def _pending_has_changes() -> bool:
        from .pending_stage import (
            ItemsPendingPayload,
            JsPendingPayload,
            TranslationsPendingPayload,
            EosPendingPayload,
            FlowPendingPayload,
            load_pending,
        )

        pending = load_pending(survey_id, dimension)
        if pending is None:
            return False

        payload = getattr(pending, "payload", None)
        if dimension == "items" and isinstance(payload, ItemsPendingPayload):
            return bool(
                list(payload.qids or [])
                or list(getattr(payload, "structural_ops", None) or [])
            )
        if dimension == "edf" and isinstance(payload, ItemsPendingPayload):
            return bool(list(payload.embedded_fields or []))
        if dimension == "js" and isinstance(payload, JsPendingPayload):
            return bool(list(payload.entries or []))
        if dimension == "translations" and isinstance(
            payload, TranslationsPendingPayload
        ):
            return bool(list(payload.qids or []) or list(payload.metadata_keys or []))
        if dimension == "eos" and isinstance(payload, EosPendingPayload):
            return bool(list(payload.operations or []))
        if dimension == "flow" and isinstance(payload, FlowPendingPayload):
            return bool(list(payload.changes or []))

        return True

    has_changes_to_apply = _pending_has_changes()
    if not has_changes_to_apply and dimension == "items":
        from .workbook_resolver import WorkbookResolver
        from .dimensions.items_core import preview_changes

        resolver = WorkbookResolver()
        xlsx_path = resolver.resolve(survey_id)
        if xlsx_path.exists():
            scope_expr = scope.expression if scope and scope.expression else None
            try:
                has_changes_to_apply = bool(
                    preview_changes(
                        survey_id,
                        xlsx_path,
                        scope_expr=scope_expr,
                        check_drift=False,
                        annotate_dirty=False,
                        self_heal_system_columns=False,
                        skip_embedded=True,
                    )
                )
            except Exception:
                has_changes_to_apply = False
    if not has_changes_to_apply and dimension == "edf":
        try:
            has_changes_to_apply = bool(
                edf_dimension.detect_unstaged_changes(survey_id).has_changes
            )
        except Exception:
            has_changes_to_apply = False
    if not has_changes_to_apply and dimension == "translations":
        try:
            has_changes_to_apply = bool(
                translations_dimension.detect_unstaged_changes(
                    survey_id,
                    scope=scope,
                ).has_changes
            )
        except Exception:
            has_changes_to_apply = False
    if not has_changes_to_apply and dimension == "master":
        try:
            has_changes_to_apply = bool(
                detect_dimension_changes(survey_id, "master").has_changes
            )
        except Exception:
            has_changes_to_apply = False

    try:
        # Push staged changes
        if dimension == "items":
            ok = items_dimension.push(
                survey_id,
                scope=scope,
                interactive=interactive,
                force_live=force_live,
                force_preview=force_preview,
                auto_yes=auto_yes,
                allow_drift=allow_drift,
                skip_publish=skip_publish,
                prefer_pending=prefer_pending,
                ignore_embedded=True if not ignore_embedded else ignore_embedded,
            )

        elif dimension == "edf":
            ok = edf_dimension.push(
                survey_id,
                scope=scope,
                interactive=interactive,
                force_live=force_live,
                force_preview=force_preview,
                auto_yes=auto_yes,
                allow_drift=allow_drift,
                skip_publish=skip_publish,
                prefer_pending=prefer_pending,
            )

        elif dimension == "js":
            ok = js_dimension.push(
                survey_id,
                interactive=interactive,
                force_live=force_live,
                force_preview=force_preview,
                auto_yes=auto_yes,
                allow_drift=allow_drift,
                skip_publish=skip_publish,
            )

        elif dimension == "translations":
            ok = translations_dimension.push(
                survey_id,
                scope=scope,
                interactive=interactive,
                force_live=force_live,
                force_preview=force_preview,
                auto_yes=auto_yes,
                allow_drift=allow_drift,
                skip_publish=skip_publish,
                prefer_pending=prefer_pending,
            )

        elif dimension == "eos":
            ok = eos_dimension.push(
                survey_id,
                interactive=interactive,
                force_live=force_live,
                force_preview=force_preview,
                auto_yes=auto_yes,
                allow_drift=allow_drift,
                skip_publish=skip_publish,
            )

        elif dimension == "flow":
            ok = flow_dimension.push(
                survey_id,
                interactive=interactive,
                force_live=force_live,
                auto_yes=auto_yes,
                allow_drift=allow_drift,
                skip_publish=skip_publish,
            )
        else:
            logger.warning(f"[sync] Unknown dimension: {dimension}")
            return DimensionSyncResult(
                dimension=dimension,
                success=False,
                applied_changes=False,
                error_message="Unknown dimension",
            )

        if not ok:
            print(f"[sync:{dimension}] {Colors.RED}✗{Colors.RESET} Failed")
            return DimensionSyncResult(
                dimension=dimension,
                success=False,
                applied_changes=False,
                error_message="Push failed",
            )

        print(f"[sync:{dimension}] {Colors.GREEN}✓{Colors.RESET} Complete")
        return DimensionSyncResult(
            dimension=dimension,
            success=True,
            applied_changes=bool(has_changes_to_apply),
            error_message=None,
        )

    except SystemExit as e:
        # push_safeguards and other CLI-style helpers may raise SystemExit;
        # treat it as a failed sync step rather than terminating the orchestrator.
        msg = str(e).strip() or "SystemExit"
        if "Aborted by user" in msg:
            # User chose an explicit abort path; do not treat as an internal error.
            logger.info(f"[sync:{dimension}] Cancelled: {msg}")
            print(f"[sync:{dimension}] {Colors.YELLOW}↩{Colors.RESET} Cancelled: {msg}")
        else:
            logger.error(f"[sync:{dimension}] SystemExit: {msg}")
            print(f"[sync:{dimension}] {Colors.RED}✗{Colors.RESET} Failed: {msg}")
        return DimensionSyncResult(
            dimension=dimension,
            success=False,
            applied_changes=False,
            error_message=msg,
        )
    except Exception as e:
        logger.error(f"[sync:{dimension}] Error syncing dimension: {e}", exc_info=True)
        print(f"[sync:{dimension}] {Colors.RED}✗{Colors.RESET} Failed: {e}")
        return DimensionSyncResult(
            dimension=dimension,
            success=False,
            applied_changes=False,
            error_message=str(e),
        )


def _summarize_pending_record(dimension: str, pending) -> str:
    if not pending:
        return "none"

    try:
        payload = pending.payload
    except Exception:
        return "staged"

    if dimension == "items":
        qid_count = len(payload.qids) if getattr(payload, "qids", None) else 0
        emb_count = (
            len(payload.embedded_fields)
            if getattr(payload, "embedded_fields", None)
            else 0
        )
        structural_ops = list(getattr(payload, "structural_ops", None) or [])
        structural_summary = getattr(payload, "structural_summary", None) or {}
        summary_parts = []
        if qid_count:
            summary_parts.append(f"{qid_count} QID(s)")
        if emb_count:
            summary_parts.append(f"{emb_count} embedded field(s)")
        if structural_ops:
            if structural_summary:
                total_qids = len(structural_summary)
                totals = {"add": 0, "edit": 0, "remove": 0, "other": 0}
                for counts in structural_summary.values():
                    for key in totals:
                        totals[key] += int(counts.get(key, 0) or 0)
                summary_parts.append(
                    "structural: "
                    f"{total_qids} QID(s) (+{totals['add']}/~{totals['edit']}/-{totals['remove']})"
                )
            else:
                structural_qids = {
                    str(op.get("qid") or "").strip()
                    for op in structural_ops
                    if op.get("qid")
                }
                summary_parts.append(f"structural: {len(structural_qids)} QID(s)")
        return f"staged: {', '.join(summary_parts) if summary_parts else 'no changes'}"

    if dimension == "edf":
        emb_count = (
            len(payload.embedded_fields)
            if getattr(payload, "embedded_fields", None)
            else 0
        )
        return f"staged: {emb_count} field(s)"

    if dimension == "js":
        count = len(payload.entries) if getattr(payload, "entries", None) else 0
        return f"staged: {count} JS file(s)"

    if dimension == "translations":
        qid_count = len(payload.qids) if getattr(payload, "qids", None) else 0
        langs = getattr(payload, "languages", None) or []
        lang_label = ", ".join(langs) if langs else "none"
        return f"staged: {qid_count} QID(s), languages: {lang_label}"

    if dimension == "eos":
        count = len(payload.operations) if getattr(payload, "operations", None) else 0
        return f"staged: {count} operation(s)"

    if dimension == "flow":
        count = len(payload.changes) if getattr(payload, "changes", None) else 0
        return f"staged: {count} change(s)"

    return "staged"


def _quote_scope_expr(scope_expr: Optional[str]) -> Optional[str]:
    if scope_expr is None:
        return None
    cleaned = str(scope_expr).strip()
    if not cleaned:
        return None
    return shlex.quote(cleaned)


def _build_pending_abort_guidance(
    *,
    survey_id: str,
    pending: Dict[str, object],
    force_live: bool,
    force_preview: bool,
    scope_expr: Optional[str],
) -> tuple[str, dict[str, object]]:
    ordered_dims = ["items", "js", "translations", "eos", "flow"]
    pending_summary = {
        dim: _summarize_pending_record(dim, pending.get(dim)) for dim in ordered_dims
    }
    pending_dims = [dim for dim in ordered_dims if pending.get(dim)]

    scope_token = _quote_scope_expr(scope_expr)

    def _append_common_flags(tokens: list[str]) -> None:
        if force_live:
            tokens.append("--force-live")
        if force_preview:
            tokens.append("--force-preview")
        if scope_token:
            tokens.extend(["--scope", scope_token])

    def _build_sync_command(*, yes: bool, pending_action: Optional[str]) -> str:
        tokens = ["qsync", "sync", "--survey-id", survey_id]
        if yes:
            tokens.append("--yes")
        if pending_action:
            tokens.extend(["--pending-action", pending_action])
        _append_common_flags(tokens)
        return " ".join(tokens)

    def _build_dimension_push_command(dimension: str) -> str:
        if dimension == "master":
            tokens = [
                "qsync",
                "survey",
                "master",
                "push",
                "--survey-id",
                survey_id,
                "--yes",
            ]
            _append_common_flags(tokens)
            return " ".join(tokens)
        if dimension == "edf":
            tokens = [
                "qsync",
                "sync",
                "--survey-id",
                survey_id,
                "--dimensions",
                "edf",
                "--yes",
            ]
            _append_common_flags(tokens)
            return " ".join(tokens)
        tokens = ["qsync", dimension, "push", "--survey-id", survey_id, "--yes"]
        _append_common_flags(tokens)
        return " ".join(tokens)

    next_commands = {
        "interactive_review": _build_sync_command(yes=False, pending_action=None),
        "push_all": _build_sync_command(yes=True, pending_action="push"),
        "discard_all": _build_sync_command(yes=True, pending_action="discard"),
        "pending_inspect": f"ls surveys/pending/*/{survey_id}.json",
        "push_by_dimension": {
            dim: _build_dimension_push_command(dim) for dim in pending_dims
        },
    }

    allow_drift_note = "If you hit drift, re-run with --allow-drift."

    payload = {
        "error": "pending_staged_changes",
        "survey_id": survey_id,
        "pending_dims": pending_dims,
        "pending_summary": pending_summary,
        "next_commands": next_commands,
        "notes": {"allow_drift": allow_drift_note},
    }

    lines = [
        f"Pending staged changes detected for {survey_id}.",
        "",
        "Pending summary:",
    ]
    for dim in ordered_dims:
        lines.append(f"  {dim}: {pending_summary[dim]}")
    lines.extend(
        [
            "",
            "Next commands:",
            f"  interactive review: {next_commands['interactive_review']}",
            f"  push all staged: {next_commands['push_all']}",
            f"  discard all staged: {next_commands['discard_all']}",
            f"  inspect pending: {next_commands['pending_inspect']}",
        ]
    )

    if pending_dims:
        lines.append("")
        lines.append("Per-dimension push:")
        for dim in pending_dims:
            lines.append(f"  {dim}: {next_commands['push_by_dimension'][dim]}")

    lines.extend(["", allow_drift_note])
    message = "\n".join(lines)
    return message, payload


def _detect_unstaged_items(
    survey_id: str,
    *,
    scope: Optional[ScopeFilter] = None,
) -> DimensionChanges:
    from .sync_core import preview_changes
    from .workbook_resolver import WorkbookResolver

    resolver = WorkbookResolver()
    xlsx_path = resolver.resolve(survey_id)
    if xlsx_path.exists():
        scope_expr = scope.expression if scope and scope.expression else None
        changes = preview_changes(
            survey_id,
            xlsx_path,
            scope_expr=scope_expr,
            check_drift=False,
            skip_embedded=True,
        )
        if changes:
            qids = set(c.qid for c in changes if c.qid)
            return DimensionChanges(
                dimension="items",
                has_changes=True,
                change_summary=f"⚡ Unstaged: {len(changes)} change(s) in {len(qids)} QID(s)",
                affected_qids=qids,
                status_kind="unstaged",
                edit_count=len(qids),
            )
    return DimensionChanges(
        dimension="items",
        has_changes=False,
        change_summary="No changes",
        affected_qids=set(),
        status_kind="none",
        edit_count=0,
    )


def _detect_unstaged_edf(
    survey_id: str,
    *,
    scope: Optional[ScopeFilter] = None,
) -> DimensionChanges:
    del scope
    return edf_dimension.detect_unstaged_changes(survey_id)


def _detect_unstaged_js(
    survey_id: str,
    *,
    scope: Optional[ScopeFilter] = None,
) -> DimensionChanges:
    from .js_preview import preview_differences
    from .config import resolve_root

    root = resolve_root(required=False) or Path.cwd()
    mapping_csv = root / "survey_js" / "survey_qid_js_map.csv"
    if not mapping_csv.exists():
        return DimensionChanges(
            dimension="js",
            has_changes=False,
            change_summary="No changes",
            affected_qids=set(),
            status_kind="none",
            edit_count=0,
        )

    scope_expr = scope.expression if scope and scope.expression else None
    try:
        changes = preview_differences(
            survey_id,
            mapping_csv,
            show_equal=False,
            interactive=False,
            verbose=False,
            scope_expr=scope_expr,
            check_drift=False,
        )
        changes = [c for c in changes if getattr(c, "status", None) != "unmapped"]
        if changes:
            qids = set(c.qid for c in changes if hasattr(c, "qid") and c.qid)
            return DimensionChanges(
                dimension="js",
                has_changes=True,
                change_summary=f"⚡ Unstaged: {len(changes)} JS file(s) changed",
                affected_qids=qids,
                status_kind="unstaged",
                edit_count=len(changes),
            )
    except Exception:
        return DimensionChanges(
            dimension="js",
            has_changes=False,
            change_summary="✗ Error",
            affected_qids=set(),
            error_detail="JS detection failed.",
            safe_to_autofix=False,
            status_kind="error",
            edit_count=0,
        )

    return DimensionChanges(
        dimension="js",
        has_changes=False,
        change_summary="No changes",
        affected_qids=set(),
        status_kind="none",
        edit_count=0,
    )


def _detect_unstaged_translations(
    survey_id: str,
    *,
    scope: Optional[ScopeFilter] = None,
) -> DimensionChanges:
    return translations_dimension.detect_unstaged_changes(survey_id, scope=scope)


def _detect_unstaged_eos(survey_id: str) -> DimensionChanges:
    return eos_dimension.detect_unstaged_changes(survey_id)


def _detect_unstaged_flow(survey_id: str) -> DimensionChanges:
    return flow_dimension.detect_changes(survey_id)


def _detect_unstaged_changes(
    survey_id: str,
    *,
    scope: Optional[ScopeFilter] = None,
) -> Dict[str, DimensionChanges]:
    master_info = detect_dimension_changes(survey_id, "master")
    # Staged master changes are already represented in pending; keep unstaged section focused.
    if master_info.status_kind == "staged":
        master_info = DimensionChanges(
            dimension="master",
            has_changes=False,
            change_summary="No unstaged changes",
            affected_qids=set(),
            status_kind="none",
        )
    return {
        "items": _detect_unstaged_items(survey_id, scope=scope),
        "edf": _detect_unstaged_edf(survey_id, scope=scope),
        "js": _detect_unstaged_js(survey_id, scope=scope),
        "translations": _detect_unstaged_translations(survey_id, scope=scope),
        "eos": _detect_unstaged_eos(survey_id),
        "flow": _detect_unstaged_flow(survey_id),
    }


def _display_survey_overview(
    survey_id: str,
    survey_ref: str,
    *,
    staged: Dict[str, str],
    unstaged: Dict[str, DimensionChanges],
    has_pending: bool,
) -> None:

    print(f"\n{Colors.BLUE}═══ Survey Overview {survey_ref} ═══{Colors.RESET}")
    print(f"{Colors.BOLD}Staged changes:{Colors.RESET}")
    for dim in ["items", "js", "translations", "eos", "flow"]:
        summary = staged.get(dim, "none")
        print(f"  • {dim}: {summary}")

    print(f"\n{Colors.BOLD}Unstaged changes:{Colors.RESET}")
    col_dim = 14

    def _format_status(dim_changes: DimensionChanges) -> str:
        summary = render_cell(dim_changes)
        max_len = col_dim - 1
        if len(summary) > max_len:
            summary = summary[: max_len - 1] + "…"
        if summary.startswith("✗"):
            return f"{Colors.RED}{summary}{Colors.RESET}"
        if summary.startswith("⚠"):
            return f"{Colors.YELLOW}{summary}{Colors.RESET}"
        if summary.startswith("✓"):
            return f"{Colors.GREEN}{summary}{Colors.RESET}"
        if summary.startswith("⚡"):
            return f"{Colors.YELLOW}{summary}{Colors.RESET}"
        if summary == "─":
            return f"{Colors.DIM}{summary}{Colors.RESET}"
        return summary

    header = (
        f"{Colors.DIM}"
        f"{'Items':<{col_dim}} "
        f"{'EDF':<{col_dim}} "
        f"{'JS':<{col_dim}} "
        f"{'Trans':<{col_dim}} "
        f"{'EOS':<{col_dim}} "
        f"{'Flow':<{col_dim}}"
        f"{Colors.RESET}"
    )
    separator = f"{Colors.DIM}{'─' * (col_dim * 5 + 4)}{Colors.RESET}"
    row = (
        f"{_pad_to_width(_format_status(unstaged['items']), col_dim)} "
        f"{_pad_to_width(_format_status(unstaged['edf']), col_dim)} "
        f"{_pad_to_width(_format_status(unstaged['js']), col_dim)} "
        f"{_pad_to_width(_format_status(unstaged['translations']), col_dim)} "
        f"{_pad_to_width(_format_status(unstaged['eos']), col_dim)} "
        f"{_pad_to_width(_format_status(unstaged['flow']), col_dim)}"
    )
    print(header)
    print(separator)
    print(row)

    errors: list[tuple[str, str]] = []
    for dim in ["items", "js", "translations", "eos", "flow"]:
        info = unstaged.get(dim)
        if info and info.error_detail:
            errors.append((survey_ref, dim, info.error_detail))
        if info and info.warning_detail:
            warnings.append((survey_ref, dim, info.warning_detail))
    errors = _filter_new_issue_lines(errors)
    warnings = _filter_new_issue_lines(warnings)
    if errors:
        print(f"\n{Colors.YELLOW}⚠️  Errors:{Colors.RESET}")
        for _, dim, detail in errors:
            print(f"  {Colors.DIM}•{Colors.RESET} {dim}: {detail}")
    if warnings:
        print(f"\n{Colors.YELLOW}⚠️  Warnings:{Colors.RESET}")
        for _, dim, detail in warnings:
            print(f"  {Colors.DIM}•{Colors.RESET} {dim}: {detail}")

    print(f"\n{Colors.BOLD}Next actions:{Colors.RESET}")
    sync_actions: list[str] = []
    repair_actions: list[str] = []

    if has_pending:
        sync_actions.append("Preview drift (live vs cache) / staged (pending vs cache)")
        sync_actions.append("Push staged changes now")
        sync_actions.append("Discard staged changes (clear pending + refresh cache)")
    if any(info.has_changes for info in unstaged.values()):
        sync_actions.append("Sync dimensions (preview → stage → push)")
        sync_actions.append("QID-mode (items/js/translations + global EDF status)")

    edf_info = unstaged.get("edf")
    if edf_info and (edf_info.warning_detail or edf_info.error_detail):
        cmd = _autofix_command("edf", survey_id)
        if cmd:
            repair_actions.append(f"Repair workbook issues only (no API writes): {cmd}")

    if sync_actions:
        print(f"  {Colors.BOLD}Sync:{Colors.RESET}")
        for action in sync_actions:
            print(f"    • {action}")
    if repair_actions:
        print(f"  {Colors.BOLD}Repair:{Colors.RESET}")
        for action in repair_actions:
            print(f"    • {action}")
    if not sync_actions and not repair_actions:
        print("  • No pending or unstaged changes detected")


def _preview_staged_changes(
    survey_id: str,
    pending: Dict[str, object],
    *,
    interactive: bool,
) -> None:
    from .drift_check import check_drift
    from .interactive_menu import confirm, select_from_list
    from .dimensions.translations_core import (
        format_translation_changes,
        ensure_pending_changes_record,
    )
    from .dimensions.items import _ensure_pending_changes
    from .dimensions.items_core import _diff_lines, _display_embedded_value
    from .pending_stage import (
        ItemsPendingPayload,
        JsPendingPayload,
        TranslationsPendingPayload,
        FlowPendingPayload,
        MasterPendingPayload,
    )

    if not pending:
        print(f"{Colors.DIM}No staged changes to preview.{Colors.RESET}")
        return

    print(f"\n{Colors.BLUE}═══ Preview: Drift + Staged Changes ═══{Colors.RESET}")
    safe_order = ["items", "js", "translations", "eos", "flow"]
    use_context = True
    shown_no_drift_note = False
    if interactive:
        scope_choice = select_from_list(
            message="What do you want to preview?",
            choices=[
                "Drift (live vs cache) — full survey",
                "Staged changes (pending vs cache) — scoped where possible",
                "↩ Cancel",
            ],
        )
        if scope_choice is None or "Cancel" in scope_choice:
            return
        use_context = scope_choice.startswith("Staged changes")

    for dim in safe_order:
        record = pending.get(dim)
        if not record:
            continue
        context: dict[str, object] = {}
        try:
            payload = record.payload  # type: ignore[attr-defined]
        except Exception:
            payload = None

        if use_context:
            if dim in ("items", "js"):
                qids = getattr(payload, "qids", None) if payload else None
                if qids:
                    context["qids"] = list(qids)
            elif dim == "translations":
                if payload:
                    if getattr(payload, "qids", None):
                        context["qids"] = list(payload.qids)
                    if getattr(payload, "languages", None):
                        context["languages"] = list(payload.languages)
            elif dim == "eos":
                if payload and getattr(payload, "operations", None):
                    ops = []
                    for op in payload.operations:
                        if hasattr(op, "to_dict"):
                            ops.append(op.to_dict())
                        elif isinstance(op, dict):
                            ops.append(op)
                    if ops:
                        context["operations"] = ops
            elif dim == "flow":
                if payload and getattr(payload, "changes", None):
                    context["changes"] = list(payload.changes)

        print(f"\n{Colors.BOLD}{dim}{Colors.RESET}:")
        if not use_context:
            if dim == "master":
                if isinstance(getattr(record, "payload", None), MasterPendingPayload):
                    pending_changes = list(record.payload.changes or [])
                    field_count = sum(
                        len(list((diff or {}).get("changes") or []))
                        for diff in pending_changes
                        if isinstance(diff, dict)
                    )
                    print(
                        f"{Colors.DIM}Staged master changes:{Colors.RESET} {field_count} field(s)"
                    )
                else:
                    print(
                        f"{Colors.DIM}Master drift preview unavailable in this view. Use staged preview mode.{Colors.RESET}"
                    )
                continue
            drift_dim = "items" if dim == "edf" else dim
            report = check_drift(
                survey_id, drift_dim, interactive=interactive, context=None
            )
            show_full = False
            if report.has_drift and report.diff_lines and interactive:
                show_full = confirm("Show full diff?", default=False)
            report.display(interactive=interactive, show_full=show_full)
            if not report.has_drift and record and not shown_no_drift_note:
                print(
                    f"{Colors.DIM}Note:{Colors.RESET} Drift preview compares live vs cache. "
                    "Staged changes are local pending—use 'Staged changes (pending vs cache)' to preview them."
                )
                shown_no_drift_note = True
            continue

        # Staged-only preview (pending vs cache), with live drift warning (cache may be stale vs live).
        if dim != "master":
            drift_report = check_drift(survey_id, dim, interactive=False, context=None)
            if drift_report.has_drift:
                print(
                    f"{Colors.YELLOW}⚠ Live drift detected; preview shows staged vs cache.{Colors.RESET}"
                )

        if dim == "master" and record:
            if not isinstance(getattr(record, "payload", None), MasterPendingPayload):
                print(f"{Colors.DIM}No staged master changes to preview.{Colors.RESET}")
                continue

            staged_diffs = list(record.payload.changes or [])
            if not staged_diffs:
                print(f"{Colors.DIM}No staged master changes to preview.{Colors.RESET}")
                continue

            for survey_diff in staged_diffs:
                if not isinstance(survey_diff, dict):
                    continue
                sid = str(survey_diff.get("survey_id") or survey_id).strip() or survey_id
                sname = str(survey_diff.get("survey_name") or "").strip()
                title = f"MASTER survey={sid}"
                if sname:
                    title += f" ({sname})"
                print(title)

                changes = list(survey_diff.get("changes") or [])
                if not changes:
                    print(f"  {Colors.DIM}(no staged fields){Colors.RESET}")
                    continue

                for change in changes:
                    field = str(change.get("field_name") or change.get("field") or "").strip()
                    endpoint = str(change.get("endpoint") or "unknown").strip()
                    old_value = str(change.get("old_value") or "")
                    new_value = str(change.get("new_value") or "")
                    marker = "⚠ " if change.get("is_dangerous") else "  "
                    print(f"{marker}[{endpoint}] {field}")
                    diff_lines = _diff_lines(old_value, new_value, context=f"Field: {field}")
                    for line in colorize_unified_diff_lines(diff_lines):
                        print("  " + line)
            continue

        if dim == "js" and record:
            from .config import resolve_root
            from .dimensions.js_preview import compare_js_pair
            from .qualtrics_client import find_cached_survey_file

            if not isinstance(getattr(record, "payload", None), JsPendingPayload):
                print(f"{Colors.DIM}No staged JS changes to preview.{Colors.RESET}")
                continue

            core_dir = (
                (resolve_root(required=False) or Path.cwd()) / "survey_js" / "core"
            ).resolve()
            cache_path = find_cached_survey_file(survey_id, in_backups=False)
            if not cache_path or not cache_path.exists():
                print(
                    f"{Colors.YELLOW}⚠{Colors.RESET} No cached survey JSON found for {survey_id}."
                )
                print(f"  Run: qsync survey pull --survey-id {survey_id}")
                continue

            try:
                cached_root = json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception as exc:
                print(
                    f"{Colors.YELLOW}⚠{Colors.RESET} Failed to read cached survey JSON: {exc}"
                )
                continue
            cached_payload = (
                cached_root.get("result")
                if isinstance(cached_root, dict)
                and isinstance(cached_root.get("result"), dict)
                else cached_root
            )
            questions = (
                cached_payload.get("Questions")
                if isinstance(cached_payload, dict)
                else {}
            ) or {}

            entries = list(getattr(record.payload, "entries", None) or [])
            if not entries:
                print(f"{Colors.DIM}No staged JS entries to preview.{Colors.RESET}")
                continue

            rows: list[dict[str, object]] = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                qid = str(entry.get("qid") or "").strip()
                js_file = str(entry.get("js_file") or "").strip()
                if not qid or not js_file:
                    continue

                local_path = (core_dir / js_file).resolve()
                if core_dir not in local_path.parents:
                    rows.append(
                        {
                            "qid": qid,
                            "js_file": js_file,
                            "status": "missing",
                            "detail": "Unsafe js_file path (outside survey_js/core).",
                            "diff_lines": [],
                        }
                    )
                    continue

                if not local_path.exists():
                    rows.append(
                        {
                            "qid": qid,
                            "js_file": js_file,
                            "status": "missing",
                            "detail": f"Local JS file not found: {local_path}",
                            "diff_lines": [],
                        }
                    )
                    continue

                question = questions.get(qid) if isinstance(questions, dict) else None
                if not isinstance(question, dict):
                    rows.append(
                        {
                            "qid": qid,
                            "js_file": js_file,
                            "status": "missing",
                            "detail": f"QID {qid} not found in cached survey JSON.",
                            "diff_lines": [],
                        }
                    )
                    continue

                question_js = (
                    question.get("QuestionJS")
                    or question.get("QuestionJSContent")
                    or ""
                ).strip()
                if not question_js:
                    rows.append(
                        {
                            "qid": qid,
                            "js_file": js_file,
                            "status": "missing",
                            "detail": "Cached question has no QuestionJS/QuestionJSContent block.",
                            "diff_lines": [],
                        }
                    )
                    continue

                local_code = local_path.read_text(encoding="utf-8")
                diff = compare_js_pair(
                    local_code,
                    question_js,
                    label=js_file,
                    from_label=f"{qid}:cache [{cache_path.name}]",
                    to_label=f"{qid}:local [{js_file}]",
                )
                rows.append(
                    {
                        "qid": qid,
                        "js_file": js_file,
                        "status": diff.status,
                        "detail": diff.detail,
                        "diff_lines": list(diff.diff_lines or []),
                        "local_path": local_path,
                        "cache_path": cache_path,
                    }
                )

            changed = [
                r
                for r in rows
                if r.get("status") in {"diff", "comments-only", "missing"}
            ]
            if not changed:
                print(f"{Colors.DIM}No staged JS diffs to preview.{Colors.RESET}")
                continue

            # Print a compact summary first.
            total = len(rows)
            diffs = sum(1 for r in rows if r.get("status") == "diff")
            comment_only = sum(1 for r in rows if r.get("status") == "comments-only")
            missing = sum(1 for r in rows if r.get("status") == "missing")
            matches = sum(1 for r in rows if r.get("status") == "match")
            print(
                f"Staged JS entries: {total} total "
                f"({diffs} diff, {comment_only} comments-only, {missing} missing, {matches} match)"
            )

            show_diffs = True
            if interactive and len(changed) > 3:
                show_diffs = confirm(
                    f"Show unified diffs for {len(changed)} JS entry/entries?",
                    default=False,
                )

            if show_diffs:
                for r in changed:
                    qid = r.get("qid") or ""
                    js_file = r.get("js_file") or ""
                    status = r.get("status") or ""
                    detail = r.get("detail") or ""
                    print(f"JS qid={qid}, file={js_file}: {status}")
                    if detail:
                        print(f"  {detail}")
                    diff_lines = list(r.get("diff_lines") or [])
                    if diff_lines:
                        local_path = r.get("local_path")
                        print(f"  context: local={local_path}, cache={cache_path}")
                        for line in colorize_unified_diff_lines(diff_lines):
                            print("  " + line)
                    print()
            continue

        if dim == "edf" and record:
            if not isinstance(getattr(record, "payload", None), ItemsPendingPayload):  # type: ignore[name-defined]
                print(f"{Colors.DIM}No staged EDF changes to preview.{Colors.RESET}")
                continue
            embedded = list(getattr(record.payload, "embedded_fields", None) or [])
            if not embedded:
                print(f"{Colors.DIM}No staged EDF changes to preview.{Colors.RESET}")
                continue
            for change in embedded:
                field = change.get("field") or ""
                flow_id = change.get("flow_id") or ""
                header = f"EMBEDDED field={field}"
                if flow_id:
                    header += f", flow_id={flow_id}"
                print(header)
                old_display = _display_embedded_value(change.get("old_value"))
                new_display = _display_embedded_value(change.get("new_value"))
                diff_lines = _diff_lines(
                    str(old_display or ""),
                    str(new_display or ""),
                    context=f"Field: {field}",
                )
                for line in colorize_unified_diff_lines(diff_lines):
                    print("  " + line)
            continue

        if dim == "items" and record:
            ensured = _ensure_pending_changes(
                survey_id,
                record,  # type: ignore[arg-type]
                scope_expr=None,
                allow_drift=True,
                interactive=interactive,
            )
            if not ensured or not isinstance(ensured.payload, ItemsPendingPayload):  # type: ignore[name-defined]
                print(f"{Colors.DIM}No staged item changes to preview.{Colors.RESET}")
                continue
            changes = list(getattr(ensured.payload, "changes", None) or [])
            embedded = list(getattr(ensured.payload, "embedded_fields", None) or [])
            if not changes and not embedded:
                print(f"{Colors.DIM}No staged item changes to preview.{Colors.RESET}")
                continue
            for change in changes:
                header = f"ITEMS qid={change.get('qid')}"
                if change.get("choice_id"):
                    header += f", choice_id={change.get('choice_id')}"
                if change.get("answer_id"):
                    header += f", answer_id={change.get('answer_id')}"
                print(header)
                diff_lines = _diff_lines(
                    str(change.get("old_html") or ""),
                    str(change.get("new_html") or ""),
                )
                for line in colorize_unified_diff_lines(diff_lines):
                    print("  " + line)
            for change in embedded:
                field = change.get("field") or ""
                flow_id = change.get("flow_id") or ""
                header = f"EMBEDDED field={field}"
                if flow_id:
                    header += f", flow_id={flow_id}"
                print(header)
                old_display = _display_embedded_value(change.get("old_value"))
                new_display = _display_embedded_value(change.get("new_value"))
                diff_lines = _diff_lines(
                    str(old_display or ""),
                    str(new_display or ""),
                    context=f"Field: {field}",
                )
                for line in colorize_unified_diff_lines(diff_lines):
                    print("  " + line)
            continue

        if dim == "translations" and record:
            ensured = ensure_pending_changes_record(
                survey_id,
                record,  # type: ignore[arg-type]
                scope=None,
                allow_drift=True,
                interactive=interactive,
            )
            if not ensured or not isinstance(ensured.payload, TranslationsPendingPayload):  # type: ignore[name-defined]
                print(
                    f"{Colors.DIM}No staged translation changes to preview.{Colors.RESET}"
                )
                continue
            lines = format_translation_changes(
                list(getattr(ensured.payload, "changes", None) or []),
                detailed=interactive,
            )
            for line in lines:
                print(line)
            continue

        if dim == "flow" and record:
            if not isinstance(getattr(record, "payload", None), FlowPendingPayload):
                print(f"{Colors.DIM}No staged flow changes to preview.{Colors.RESET}")
                continue
            changes = flow_dimension.preview(
                survey_id,
                verbose=interactive,
                visual=False,
                validate=True,
            )
            if not changes:
                print(f"{Colors.DIM}No staged flow differences detected.{Colors.RESET}")
            continue

        report = check_drift(
            survey_id,
            dim,
            interactive=interactive,
            context=context if use_context else None,
        )
        show_full = False
        if report.has_drift and report.diff_lines and interactive:
            show_full = confirm("Show full diff?", default=False)
        report.display(interactive=interactive, show_full=show_full)


def _resolve_staged_changes_interactive(
    survey_id: str,
    *,
    pending: Dict[str, object],
    dimension_results: Dict[str, DimensionSyncResult],
    force_live: bool,
    force_preview: bool,
    auto_yes: bool,
    allow_drift: bool,
    skip_publish: bool,
    scope: Optional[ScopeFilter] = None,
    per_dimension: bool = False,
) -> bool:
    from .interactive_menu import confirm, select_from_list
    from .qualtrics_client import refresh_survey_cache

    safe_order = ["items", "js", "translations", "eos", "flow"]

    while True:
        choices = [
            "👀 Preview drift (live vs cache) / staged (pending vs cache)",
            "📝 Preview unstaged changes (source vs cache)",
            "🚀 Push staged changes now",
            "🧹 Discard staged changes (clear pending + refresh cache)",
            "↩ Exit sync",
        ]
        selection = select_from_list(
            message="Staged changes detected — choose an action:",
            choices=choices,
        )

        if selection is None or "Exit" in selection:
            print(f"{Colors.DIM}Sync cancelled by user.{Colors.RESET}")
            return False

        if selection.startswith("👀"):
            _preview_staged_changes(survey_id, pending, interactive=True)
            continue

        if selection.startswith("📝"):
            unstaged = _detect_unstaged_changes(survey_id, scope=scope)
            unstaged_dims = [
                dim
                for dim in safe_order
                if dim in unstaged and bool(unstaged[dim].has_changes)
            ]
            if not unstaged_dims:
                print(f"{Colors.DIM}No unstaged changes detected.{Colors.RESET}")
                continue

            display_unified_preview(
                survey_id=survey_id,
                dimensions=unstaged_dims,
                per_dimension=per_dimension,
                detailed=True,
                scope=scope,
                allow_drift=allow_drift,
                interactive=True,
                skip_embedded=True,
            )
            continue

        if selection.startswith("🧹"):
            for dim in list(pending.keys()):
                clear_pending(survey_id, dim)  # type: ignore[arg-type]
            try:
                refresh_survey_cache(survey_id)
                print(
                    f"{Colors.GREEN}✓{Colors.RESET} Cleared pending and refreshed cache"
                )
            except Exception as e:
                print(
                    f"{Colors.YELLOW}⚠{Colors.RESET} Cleared pending, but failed to refresh cache: {e}"
                )
            return True

        if selection.startswith("🚀"):
            # Get survey ref for reporting
            from .survey_ref import format_survey_ref

            inv = _get_inventory_cached(survey_id) or {}
            survey_ref = format_survey_ref(
                survey_id, str(inv.get("name") or "").strip() or None
            )

            # Push all staged dimensions (suppress per-dimension publish)
            for dim in safe_order:
                if dim not in pending:
                    continue
                dimension_results[dim] = sync_dimension(
                    survey_id,
                    dim,
                    interactive=True,
                    force_live=force_live,
                    force_preview=force_preview,
                    auto_yes=auto_yes,
                    allow_drift=allow_drift,
                    skip_publish=True,  # Suppress per-dimension publish
                    prefer_pending=True,
                )

            force_live_retry_dims = [
                dim
                for dim in safe_order
                if dim in pending
                and _requires_force_live_retry(dimension_results.get(dim))
            ]
            if force_live_retry_dims and not force_live:
                retry_label = ", ".join(force_live_retry_dims)
                retry_force_live = confirm(
                    message=(
                        f"{retry_label} push blocked by live-response safeguards. "
                        "Retry blocked dimension(s) with --force-live?"
                    ),
                    default=False,
                )
                if retry_force_live:
                    for dim in force_live_retry_dims:
                        print(
                            f"{Colors.BLUE}[sync:{dim}]{Colors.RESET} Retrying with --force-live..."
                        )
                        dimension_results[dim] = sync_dimension(
                            survey_id,
                            dim,
                            interactive=True,
                            force_live=True,
                            force_preview=force_preview,
                            auto_yes=auto_yes,
                            allow_drift=allow_drift,
                            skip_publish=True,  # Suppress per-dimension publish
                            prefer_pending=True,
                        )

            # Display push report
            _display_push_report(survey_ref, dimension_results)

            # Orchestrated publish step
            _orchestrated_publish(
                survey_id=survey_id,
                survey_ref=survey_ref,
                dimension_results=dimension_results,
                skip_publish=skip_publish,
                interactive=True,
                auto_yes=auto_yes,
            )

            return True


def _generate_composite_publish_description(
    dimension_results: Dict[str, DimensionSyncResult],
    survey_id: str,
) -> str:
    """Generate a composite publish description from multiple dimension pushes.

    Args:
        dimension_results: Results from each dimension push
        survey_id: Survey ID

    Returns:
        Composite description string (truncated to fit limits)
    """
    from .pending_stage import load_pending
    from .qualtrics_client import SURVEY_VERSION_DESCRIPTION_MAX_CHARS

    successful_dims = [
        dim
        for dim, result in dimension_results.items()
        if result.success and result.applied_changes
    ]

    if not successful_dims:
        return "qsync sync (no changes)"

    # Build description parts
    parts = []

    for dim in successful_dims:
        pending = load_pending(survey_id, dim)
        if not pending:
            continue

        if dim == "items":
            qid_count = len(pending.payload.qids) if pending.payload.qids else 0
            if qid_count:
                parts.append(f"items:{qid_count}Q")
        elif dim == "edf":
            emb_count = (
                len(pending.payload.embedded_fields)
                if getattr(pending.payload, "embedded_fields", None)
                else 0
            )
            if emb_count:
                parts.append(f"edf:{emb_count}")
        elif dim == "js":
            count = len(pending.payload.entries) if pending.payload.entries else 0
            if count:
                parts.append(f"js:{count}file(s)")
        elif dim == "translations":
            langs = pending.payload.languages if pending.payload.languages else []
            if langs:
                parts.append(f"trans:{','.join(langs)}")
        elif dim == "eos":
            count = len(pending.payload.operations) if pending.payload.operations else 0
            if count:
                parts.append(f"eos:{count}op(s)")
        elif dim == "flow":
            count = (
                len(getattr(pending.payload, "changes", None) or [])
                if getattr(pending, "payload", None)
                else 0
            )
            if count:
                parts.append(f"flow:{count}")
        elif dim == "master":
            field_count = 0
            for diff in list(getattr(pending.payload, "changes", None) or []):
                if isinstance(diff, dict):
                    field_count += len(list(diff.get("changes") or []))
            if field_count:
                parts.append(f"master:{field_count}field(s)")

    if not parts:
        desc = f"qsync sync: {', '.join(successful_dims)}"
    else:
        desc = f"qsync sync: {' | '.join(parts)}"

    # Truncate if needed
    if len(desc) > SURVEY_VERSION_DESCRIPTION_MAX_CHARS:
        desc = desc[: SURVEY_VERSION_DESCRIPTION_MAX_CHARS - 3] + "..."

    return desc


def _display_push_report(
    survey_ref: str,
    dimension_results: Dict[str, DimensionSyncResult],
) -> None:
    """Display a summary report of push results.

    Args:
        survey_ref: Formatted survey reference (name + ID)
        dimension_results: Results from each dimension push
    """

    pushed = [
        dim
        for dim, res in dimension_results.items()
        if res.success and res.applied_changes
    ]
    no_changes = [
        dim
        for dim, res in dimension_results.items()
        if res.success and not res.applied_changes
    ]
    failed = [
        (dim, res.error_message)
        for dim, res in dimension_results.items()
        if not res.success
    ]

    print(f"\n{Colors.BLUE}═══ Push Report ═══{Colors.RESET}")
    print(f"{Colors.DIM}Survey:{Colors.RESET} {survey_ref}")
    print()

    if pushed:
        print(f"{Colors.GREEN}✓ Successfully pushed:{Colors.RESET}")
        for dim in pushed:
            print(f"  • {dim}")

    if no_changes:
        if pushed:
            print()
        print(f"{Colors.GREEN}✓ No changes:{Colors.RESET}")
        for dim in no_changes:
            print(f"  • {dim}")

    if failed:
        print()
        print(f"{Colors.RED}✗ Failed to push:{Colors.RESET}")
        for dim, error in failed:
            error_text = error or "Unknown error"
            print(f"  • {dim}: {error_text}")

    print()


def _orchestrated_publish(
    survey_id: str,
    survey_ref: str,
    dimension_results: Dict[str, DimensionSyncResult],
    *,
    skip_publish: bool,
    interactive: bool,
    auto_yes: bool,
) -> Optional[str]:
    """Perform single orchestrated publish after all dimensions have pushed.

    Args:
        survey_id: Survey ID
        survey_ref: Formatted survey reference
        dimension_results: Results from dimension pushes
        skip_publish: If True, skip publishing
        interactive: Interactive mode flag
        auto_yes: Auto-yes flag (non-interactive automation)

    Returns:
        Published version description, or None if skipped
    """
    from .terminal_output import info, success, warn
    from .qualtrics_client import publish_survey_definition

    if skip_publish:
        info("[sync:publish]", "Publishing skipped (--skip-publish)")
        return None

    # Only publish if ALL dimensions succeeded
    all_succeeded = all(res.success for res in dimension_results.values())
    if not all_succeeded:
        warn("[sync:publish]", "Publishing skipped due to dimension push failures")
        return None

    if not any(res.applied_changes for res in dimension_results.values()):
        info("[sync:publish]", "Publishing skipped (no changes were pushed)")
        return None

    # Generate composite description
    default_desc = _generate_composite_publish_description(dimension_results, survey_id)

    # Determine final description
    if auto_yes:
        description = default_desc
        info("[sync:publish]", f"Auto-publishing: {description}")
    elif interactive:
        from .interactive_menu import confirm

        print(f"\n{Colors.BLUE}═══ Publish Survey ═══{Colors.RESET}")
        print(f"{Colors.DIM}Survey:{Colors.RESET} {survey_ref}")
        print(f"{Colors.DIM}Default description:{Colors.RESET} {default_desc}")
        print()

        should_publish = confirm(message="Create version snapshot?", default=True)

        if not should_publish:
            info("[sync:publish]", "Publishing skipped by user")
            return None

        # Prompt for custom description
        print()
        custom_input = input(
            f"{Colors.DIM}Custom description (leave empty for default):{Colors.RESET} "
        ).strip()

        if custom_input:
            from .qualtrics_client import SURVEY_VERSION_DESCRIPTION_MAX_CHARS

            if len(custom_input) > SURVEY_VERSION_DESCRIPTION_MAX_CHARS:
                warn(
                    "[sync:publish]",
                    f"Description too long (max {SURVEY_VERSION_DESCRIPTION_MAX_CHARS} chars), using default",
                )
                description = default_desc
            else:
                description = custom_input
        else:
            description = default_desc
    else:
        # Non-interactive without auto_yes (edge case)
        description = default_desc

    # Publish
    try:
        publish_survey_definition(
            survey_id=survey_id,
            description=description,
            published=True,
        )
        success("[sync:publish]", f"✓ Published: {description}")
        return description
    except Exception as e:
        warn("[sync:publish]", f"Failed to publish: {e}")
        return None


def _sync_dimensions_once(
    survey_id: str,
    dimensions: List[str],
    *,
    interactive: bool,
    force_live: bool,
    force_preview: bool,
    auto_yes: bool,
    allow_drift: bool,
    skip_publish: bool,
    scope: Optional[ScopeFilter],
    per_dimension: bool,
    ignore_embedded: bool = False,
    allow_skip_embedded: bool = False,
    prefer_pending: bool | None = None,
) -> Optional[SurveySyncSummary]:
    from .survey_ref import format_survey_ref

    if not dimensions:
        return None

    changes = detect_survey_changes(survey_id)

    # Detect conflicts between selected dimensions (skip if scoped)
    if not scope:
        conflicts = detect_conflicts(changes)
        relevant_conflicts = [
            c for c in conflicts if any(d in dimensions for d in c.dimensions)
        ]

        if relevant_conflicts:
            print(
                f"\n[sync:conflict] Detected {len(relevant_conflicts)} conflict(s) between dimensions"
            )

            if interactive and not auto_yes:
                resolve_conflicts_interactive(relevant_conflicts)
            else:
                resolve_conflicts_auto(relevant_conflicts)

            print(
                "[sync:conflict] Using safe merge order: items → edf → js → translations → eos → flow → master"
            )

        # Detect master-specific conflicts and warnings
        master_warnings = detect_master_conflicts(changes)
        if master_warnings:
            print()
            for warning in master_warnings:
                print(f"[sync:conflict] {warning}")

    # Sort dimensions in safe merge order
    safe_order = ["items", "js", "translations", "eos", "flow"]
    dimensions_sorted = [d for d in safe_order if d in dimensions]

    edf_info = changes.dimensions.get("edf")
    edf_unhealthy = bool(
        edf_info and (edf_info.warning_detail or edf_info.error_detail)
    )
    if "items" in dimensions_sorted and edf_unhealthy:
        if (auto_yes or not interactive) and not allow_skip_embedded:
            print(
                f"[sync:items] {Colors.RED}✗{Colors.RESET} Embedded_Data is unhealthy. "
                "Non-interactive sync requires --allow-skip-embedded to proceed with items while skipping embedded defaults."
            )
            repair_cmd = _autofix_command("edf", survey_id)
            if repair_cmd:
                print(f"[sync:items] Repair first: {repair_cmd}")
            return None
        if interactive and not auto_yes:
            from .interactive_menu import confirm

            print(
                f"{Colors.YELLOW}⚠{Colors.RESET} Embedded_Data is unhealthy; items sync will skip embedded defaults."
            )
            repair_cmd = _autofix_command("edf", survey_id)
            if repair_cmd:
                print(f"{Colors.DIM}Repair command:{Colors.RESET} {repair_cmd}")
            if not confirm(
                message="Continue with items-only (skip embedded defaults)?",
                default=False,
            ):
                print(f"{Colors.DIM}Sync cancelled by user.{Colors.RESET}")
                return None

    # Items is intentionally non-embedded in orchestrated sync (EDF is separate).
    skip_embedded = True

    if skip_embedded and "items" in dimensions_sorted:
        from .sync_core import preview_changes
        from .workbook_resolver import WorkbookResolver

        resolver = WorkbookResolver()
        xlsx_path = resolver.resolve(survey_id)
        if xlsx_path.exists():
            scope_expr = scope.expression if scope and scope.expression else None
            try:
                changes = preview_changes(
                    survey_id,
                    xlsx_path,
                    scope_expr=scope_expr,
                    check_drift=False,
                    annotate_dirty=False,
                    self_heal_system_columns=False,
                    skip_embedded=True,
                )
                refs = _collect_embedded_refs_from_changes(changes)
                if refs:
                    from .terminal_output import warn

                    warn(
                        "[sync:items]",
                        "Detected embedded-field references in wording changes while skipping embedded defaults "
                        f"(fields: {', '.join(sorted(refs))}).",
                    )
                    if interactive and not auto_yes:
                        from .interactive_menu import confirm

                        if not confirm(message="Proceed anyway?", default=False):
                            print(f"{Colors.DIM}Sync cancelled by user.{Colors.RESET}")
                            return None
            except Exception:
                pass

    # Show preview before syncing (unless --yes bypasses all prompts)
    if not auto_yes:
        print(f"\n{Colors.BLUE}═══ Preview Changes ═══{Colors.RESET}")
        preview_success = display_unified_preview(
            survey_id=survey_id,
            dimensions=dimensions_sorted,
            per_dimension=per_dimension,
            detailed=True,
            scope=scope,
            allow_drift=allow_drift,
            interactive=interactive and not auto_yes,
            skip_embedded=skip_embedded,
        )

        if not preview_success:
            print(f"{Colors.YELLOW}⚠ Warning:{Colors.RESET} Some previews failed")

        # Re-detect unstaged state so explicit dimension selections do not
        # force staging when no source-vs-cache diffs actually exist.
        selected_unstaged = _detect_unstaged_changes(survey_id, scope=scope)
        selected_staged_dims = [
            dim for dim in dimensions_sorted if _is_dimension_staged(survey_id, dim)
        ]

        # For unstaged dimensions, prompt to stage
        js_stale_pending = False
        if "js" in dimensions_sorted and _is_dimension_staged(survey_id, "js"):
            js_stale_pending = _js_pending_out_of_sync(survey_id, scope=scope)
            if js_stale_pending:
                print(
                    f"{Colors.YELLOW}⚠ Staged JS no longer matches local files.{Colors.RESET} "
                    "Re-stage to refresh the cache (or clear staged changes)."
                )

        unstaged_dims: List[str] = []
        for dim in dimensions_sorted:
            dim_info = selected_unstaged.get(dim)
            has_unstaged = bool(
                dim_info
                and dim_info.has_changes
                and dim_info.status_kind == "unstaged"
            )
            if has_unstaged:
                unstaged_dims.append(dim)

        if js_stale_pending and "js" in dimensions_sorted and "js" not in unstaged_dims:
            unstaged_dims.append("js")

        if not unstaged_dims and not selected_staged_dims:
            print(
                f"{Colors.DIM}No staged or unstaged changes detected for selected dimensions.{Colors.RESET}"
            )
            return None

        if unstaged_dims:
            from .interactive_menu import confirm

            print(
                f"\n{Colors.YELLOW}⚡ Unstaged dimensions:{Colors.RESET} {', '.join(unstaged_dims)}"
            )
            print(
                f"{Colors.DIM}These changes need to be staged before pushing.{Colors.RESET}"
            )

            if interactive:
                should_stage = confirm(message="Stage these changes now?", default=True)

                if not should_stage:
                    print(f"{Colors.DIM}Staging cancelled by user.{Colors.RESET}")
                    return None

                # Stage each unstaged dimension
                for dim in unstaged_dims:
                    print(f"\n[sync:stage] Staging {dim}...")
                    stage_success = stage_dimension(
                        survey_id,
                        dim,
                        scope=scope,
                        ignore_embedded=skip_embedded if dim == "items" else False,
                        allow_drift=allow_drift,
                        interactive=interactive and not auto_yes,
                    )

                    if not stage_success:
                        print(f"{Colors.RED}✗ Failed to stage {dim}{Colors.RESET}")
                        return None

                    print(f"{Colors.GREEN}✓ Staged {dim}{Colors.RESET}")

                # If we just staged in this same session, prefer pushing the staged payload.
                # This avoids prompting “Excel differs from cache” immediately after staging,
                # which is expected (Excel != cache is the reason we staged).
                if prefer_pending is None:
                    prefer_pending = True

    # Push approval menu (unless --yes bypasses all prompts)
    if not auto_yes and interactive:
        from .interactive_menu import select_from_list, confirm

        print(f"\n{Colors.BLUE}═══ Push Approval ═══{Colors.RESET}")

        # Show summary of what will be pushed
        from .survey_ref import format_survey_ref

        inv = _get_inventory_cached(survey_id) or {}
        survey_ref = format_survey_ref(
            survey_id, str(inv.get("name") or "").strip() or None
        )
        print(f"{Colors.DIM}Survey:{Colors.RESET} {survey_ref}")
        print(f"{Colors.DIM}Dimensions to push:{Colors.RESET}")

        for dim in dimensions_sorted:
            pending = load_pending(survey_id, dim)
            if pending:
                summary = _summarize_pending_record(dim, pending)
                print(f"  • {Colors.BOLD}{dim}{Colors.RESET}: {summary}")
            else:
                print(
                    f"  • {Colors.BOLD}{dim}{Colors.RESET}: {Colors.DIM}no staged changes{Colors.RESET}"
                )

        print()  # Blank line

        # Prompt for approval
        choices = [
            "✓ Push to Qualtrics",
            "✗ Skip push",
            "↩ Cancel workflow",
        ]

        selection = select_from_list(
            message="Approve push to Qualtrics?",
            choices=choices,
        )

        if selection is None or "Cancel" in selection:
            print(f"{Colors.DIM}Push cancelled by user.{Colors.RESET}")
            return None
        elif "Skip" in selection:
            print(f"{Colors.DIM}Push skipped by user.{Colors.RESET}")
            return None

        # If user didn't use --force-live, check if survey has live responses
        # and prompt for final confirmation to override the safeguard
        if not force_live:
            from .push_policy import load_push_context

            ctx = load_push_context(survey_id)
            response_count = ctx.response_count

            if response_count > 0:
                print(f"\n{Colors.YELLOW}⚠ WARNING{Colors.RESET}")
                print(
                    f"{Colors.DIM}This survey has {Colors.BOLD}{response_count}{Colors.RESET}{Colors.DIM} finished response(s).{Colors.RESET}"
                )
                print(
                    f"{Colors.DIM}Pushing changes will affect live data and may invalidate existing responses.{Colors.RESET}"
                )
                print(
                    f"{Colors.DIM}Please double-check your diffs before proceeding.{Colors.RESET}\n"
                )

                override_confirmed = confirm(
                    message="Push despite live responses?", default=False
                )

                if not override_confirmed:
                    print(
                        f"{Colors.DIM}Push cancelled due to live responses.{Colors.RESET}"
                    )
                    print(
                        f"{Colors.DIM}Tip: Use --force-live flag to skip this prompt in future.{Colors.RESET}"
                    )
                    return None
                else:
                    # User confirmed - override force_live for this push
                    print(
                        f"{Colors.YELLOW}Overriding safeguard - proceeding with push...{Colors.RESET}"
                    )
                    force_live = True

    # Sync each dimension and track results
    # Note: We suppress per-dimension publish (skip_publish=True) to enable
    # a single orchestrated publish after all pushes complete
    dimension_results: Dict[str, DimensionSyncResult] = {}
    total_dims = len(dimensions_sorted)

    inv = _get_inventory_cached(survey_id) or {}
    survey_ref = format_survey_ref(
        survey_id, str(inv.get("name") or "").strip() or None
    )

    print(f"\n{Colors.BLUE}═══ Pushing to Qualtrics ═══{Colors.RESET}")
    print(
        f"{Colors.DIM}Survey: {survey_ref} | Dimensions: {total_dims}{Colors.RESET}\n"
    )

    for idx, dimension in enumerate(dimensions_sorted, 1):
        print(
            f"{Colors.BLUE}[Step {idx}/{total_dims}]{Colors.RESET} Pushing {Colors.BOLD}{dimension}{Colors.RESET} dimension..."
        )

        try:
            dimension_results[dimension] = sync_dimension(
                survey_id,
                dimension,
                interactive=interactive,
                force_live=force_live,
                force_preview=force_preview,
                auto_yes=auto_yes,
                allow_drift=allow_drift,
                skip_publish=True,  # Suppress per-dimension publish for orchestrated flow
                scope=scope,
                prefer_pending=prefer_pending,
                ignore_embedded=skip_embedded if dimension == "items" else False,
            )
        except Exception as e:
            dimension_results[dimension] = DimensionSyncResult(
                dimension=dimension, success=False, error_message=str(e)
            )

    # Display push report
    _display_push_report(survey_ref, dimension_results)

    # Orchestrated publish step (single publish after all pushes)
    _orchestrated_publish(
        survey_id=survey_id,
        survey_ref=survey_ref,
        dimension_results=dimension_results,
        skip_publish=skip_publish,
        interactive=interactive,
        auto_yes=auto_yes,
    )

    record = _get_inventory_cached(survey_id)
    survey_name = record.get("name", survey_id) if record else survey_id

    return SurveySyncSummary(
        survey_id=survey_id,
        survey_name=survey_name,
        dimension_results=dimension_results,
    )


def sync_survey(
    survey_id: str,
    dimensions: Optional[List[str]] = None,
    interactive: bool = True,
    force_live: bool = False,
    force_preview: bool = False,
    auto_yes: bool = False,
    pending_action: str = "abort",
    scope: Optional[ScopeFilter] = None,
    per_dimension: bool = False,
    skip_publish: bool = False,
    refresh_workbooks: bool = False,
    allow_drift: bool = False,
    allow_skip_embedded: bool = False,
    json_output: bool = False,
) -> Optional[SurveySyncSummary]:
    """Sync one or more dimensions for a survey.

    Args:
        survey_id: Survey ID
        dimensions: List of dimensions to sync (None = auto-detect from pending)
        interactive: Whether to prompt interactively
        force_live: Force push despite live responses
        force_preview: Suppress preview-only response warnings
        auto_yes: Skip all confirmation prompts
        pending_action: If pending staged changes exist and auto_yes is True, what to do: push/discard/abort
        scope: Optional scope filter (qid/tag/js boolean DSL) passed to items/js/translations where supported. See docs/reference/scope-semantics.md.
        per_dimension: Preview and approve each dimension separately
        skip_publish: Skip auto-publish step
        refresh_workbooks: Refresh Excel workbooks after successful sync
        allow_drift: Allow drift during sync
        allow_skip_embedded: Allow skip-embedded pushes when EDF is invalid
        json_output: Emit machine-readable JSON for blocked runs

    Returns:
        SurveySyncSummary with per-dimension results, or None if nothing synced
    """
    from .survey_ref import format_survey_ref
    from .interactive_menu import select_from_list, confirm, autocomplete_from_list

    dimension_results: Dict[str, DimensionSyncResult] = {}
    summary_name: Optional[str] = None

    from .rich_support import rich_status

    # Detect staged changes
    with rich_status("Detecting staged changes..."):
        changes = detect_survey_changes(survey_id)
    survey_ref = format_survey_ref(survey_id, getattr(changes, "survey_name", None))

    # Check for fixable errors in interactive single-survey mode.
    # This is advisory only: users can continue syncing selected dimensions
    # without running auto-fixes first.
    if interactive and not auto_yes:
        selected_dims = set(dimensions or [])
        fixable_errors = [
            (dim, info)
            for dim, info in changes.dimensions.items()
            if _fixable_detail(info)
            and (not selected_dims or dim in selected_dims)
        ]

        if fixable_errors:
            print(f"\n{Colors.YELLOW}⚠ Fixable Issues Detected{Colors.RESET}")
            for dim, info in fixable_errors:
                detail = _fixable_detail(info) or "Issue requires repair."
                print(f"  • {Colors.BOLD}{dim}{Colors.RESET}: {detail}")

            ordered = MASTER_DIMENSION_ORDER
            fixable_errors.sort(
                key=lambda entry: ordered.index(entry[0]) if entry[0] in ordered else 99
            )
            fix_cmds = [
                (dim, _autofix_command(dim, survey_id)) for dim, _ in fixable_errors
            ]
            fix_cmds = [(dim, cmd) for dim, cmd in fix_cmds if cmd]

            if not fix_cmds:
                print(
                    f"{Colors.DIM}No auto-fix command available for selected issues; continuing sync.{Colors.RESET}"
                )
            else:
                print(
                    f"\n{Colors.DIM}These issues can be fixed automatically by running:{Colors.RESET}"
                )
                for dim, cmd in fix_cmds:
                    print(f"  • {dim}: {Colors.CYAN}{cmd}{Colors.RESET}")

                should_fix = confirm(message="Fix these issues now?", default=True)

                if should_fix:
                    autofix_failed = False
                    for dim, cmd in fix_cmds:
                        print(f"\n[sync:fix] Running {cmd} for {survey_ref}...")
                        try:
                            result = _run_autofix(dim, survey_id)
                            print(f"{Colors.GREEN}✓{Colors.RESET} {result}")
                        except Exception as e:
                            print(f"{Colors.RED}✗ Failed to fix {dim}: {e}{Colors.RESET}")
                            autofix_failed = True
                            break

                    print("\n[sync] Re-detecting changes after fix...")
                    with rich_status("Re-detecting staged changes..."):
                        changes = detect_survey_changes(survey_id)
                    survey_ref = format_survey_ref(
                        survey_id, getattr(changes, "survey_name", None)
                    )
                    if autofix_failed:
                        print(
                            f"{Colors.DIM}Continuing sync without applying remaining auto-fixes.{Colors.RESET}"
                        )
                else:
                    print(
                        f"{Colors.DIM}Fix cancelled. You can run manually:{Colors.RESET}"
                    )
                    for _, cmd in fix_cmds:
                        print(f"  {Colors.CYAN}{cmd}{Colors.RESET}")
                    print(
                        f"{Colors.DIM}Continuing sync without auto-fix.{Colors.RESET}"
                    )

    pending = list_pending(survey_id)
    if auto_yes and pending:
        action = (pending_action or "abort").strip().lower()
        if action == "abort":
            message, payload = _build_pending_abort_guidance(
                survey_id=survey_id,
                pending=pending,
                force_live=force_live,
                force_preview=force_preview,
                scope_expr=scope.expression if scope else None,
            )
            if json_output:
                print(json.dumps(payload, indent=2, sort_keys=True))
                raise SystemExit(1)
            raise SystemExit(message)
        if action == "discard":
            from .pending_stage import clear_pending
            from .qualtrics_client import refresh_survey_cache

            for dim in list(pending.keys()):
                clear_pending(survey_id, dim)  # type: ignore[arg-type]
            try:
                refresh_survey_cache(survey_id)
                print(
                    f"{Colors.GREEN}✓{Colors.RESET} Cleared pending and refreshed cache"
                )
            except Exception as e:
                print(
                    f"{Colors.YELLOW}⚠{Colors.RESET} Cleared pending, but failed to refresh cache: {e}"
                )
            pending = {}
            changes = detect_survey_changes(survey_id)
            survey_ref = format_survey_ref(
                survey_id, getattr(changes, "survey_name", None)
            )
        elif action == "push":
            safe_order = ["items", "js", "translations", "eos", "flow"]
            pending_dims = [d for d in safe_order if d in pending]
            if dimensions is not None:
                pending_dims = [d for d in pending_dims if d in set(dimensions)]
            if not pending_dims:
                print(f"[sync] No staged dimensions selected for {survey_ref}")
                return None
            return _sync_dimensions_once(
                survey_id,
                pending_dims,
                interactive=interactive,
                force_live=force_live,
                force_preview=force_preview,
                auto_yes=auto_yes,
                allow_drift=allow_drift,
                skip_publish=skip_publish,
                scope=scope,
                per_dimension=per_dimension,
                allow_skip_embedded=allow_skip_embedded,
                prefer_pending=True,
            )
        else:
            raise SystemExit(f"Unknown pending action: {pending_action}")

    if not interactive:
        if dimensions is None:
            dimensions = prompt_dimension_selection(changes, interactive=False)
        if not dimensions:
            print(f"[sync] No dimensions selected for {survey_ref}")
            return None
        return _sync_dimensions_once(
            survey_id,
            dimensions,
            interactive=interactive,
            force_live=force_live,
            force_preview=force_preview,
            auto_yes=auto_yes,
            allow_drift=allow_drift,
            skip_publish=skip_publish,
            scope=scope,
            per_dimension=per_dimension,
            allow_skip_embedded=allow_skip_embedded,
        )

    # Interactive overview loop
    while True:
        pending = list_pending(survey_id)
        unstaged = _detect_unstaged_changes(survey_id, scope=scope)
        record = _get_inventory_cached(survey_id)
        survey_name = record.get("name", survey_id) if record else survey_id
        summary_name = survey_name
        survey_ref = format_survey_ref(survey_id, survey_name)

        staged_summary = {
            dim: _summarize_pending_record(dim, pending.get(dim))
            for dim in MASTER_DIMENSION_ORDER
        }

        _display_survey_overview(
            survey_id,
            survey_ref,
            staged=staged_summary,
            unstaged=unstaged,
            has_pending=bool(pending),
        )

        if pending:
            resolved = _resolve_staged_changes_interactive(
                survey_id,
                pending=pending,
                dimension_results=dimension_results,
                force_live=force_live,
                force_preview=force_preview,
                auto_yes=auto_yes,
                allow_drift=allow_drift,
                skip_publish=skip_publish,
                scope=scope,
                per_dimension=per_dimension,
            )
            if not resolved:
                break
            continue

        if dimensions is not None:
            summary = _sync_dimensions_once(
                survey_id,
                dimensions,
                interactive=interactive,
                force_live=force_live,
                force_preview=force_preview,
                auto_yes=auto_yes,
                allow_drift=allow_drift,
                skip_publish=skip_publish,
                scope=scope,
                per_dimension=per_dimension,
                allow_skip_embedded=allow_skip_embedded,
            )
            if summary:
                dimension_results.update(summary.dimension_results)
            break

        has_unstaged = any(info.has_changes for info in unstaged.values())
        if not has_unstaged:
            print(f"[sync] No unstaged changes detected for {survey_ref}")
            break

        choices = [
            "✓ Sync dimensions",
            "🔎 QID-mode (items/js/translations + EDF status)",
            "↩ Exit sync",
        ]
        selection = select_from_list(
            message="Select next action:",
            choices=choices,
        )

        if selection is None or "Exit" in selection:
            break

        if selection.startswith("🔎"):
            qid_choice = select_from_list(
                message="Select QID selection mode:",
                choices=[
                    "Cycle through all QIDs with changes",
                    "Enter specific QID(s)",
                    "Search by ExportTag (autocomplete)",
                    "↩ Cancel",
                ],
            )
            if qid_choice is None or "Cancel" in qid_choice:
                continue

            qids: List[str] = []

            def _collect_qid_candidates() -> set[str]:
                qid_candidates: set[str] = set()
                for dim_name in ("items", "js", "translations"):
                    info = unstaged.get(dim_name)
                    if info and info.affected_qids:
                        qid_candidates.update(info.affected_qids)
                return qid_candidates

            if qid_choice.startswith("Cycle"):
                qid_candidates = _collect_qid_candidates()
                qids = sorted(qid_candidates)
                if not qids:
                    print(f"{Colors.DIM}No QIDs with changes detected.{Colors.RESET}")
                    continue
            elif qid_choice.startswith("Enter"):
                raw = input("Enter QID(s) (comma-separated): ").strip()
                if not raw:
                    print(f"{Colors.DIM}No QIDs provided.{Colors.RESET}")
                    continue
                qids = [q.strip() for q in raw.split(",") if q.strip()]
                if not qids:
                    print(f"{Colors.DIM}No QIDs provided.{Colors.RESET}")
                    continue
            else:
                from .workbook_resolver import WorkbookResolver
                from . import excel_io

                resolver = WorkbookResolver()
                xlsx_path = resolver.resolve(survey_id)
                if not xlsx_path.exists():
                    print(f"{Colors.DIM}No workbook found at {xlsx_path}{Colors.RESET}")
                    print(
                        f"{Colors.DIM}Run: qsync items pull --survey-id {survey_id}{Colors.RESET}"
                    )
                    continue
                questions_excel = excel_io.load_questions_from_workbook(xlsx_path)
                qid_candidates = _collect_qid_candidates()
                if not qid_candidates:
                    print(
                        f"{Colors.DIM}No QIDs with detected changes to filter by ExportTag.{Colors.RESET}"
                    )
                    print(
                        f"{Colors.DIM}Use 'Enter specific QID(s)' to force a selection.{Colors.RESET}"
                    )
                    continue
                tag_choices: List[str] = []
                tag_to_qid: Dict[str, str] = {}
                for qid, row in questions_excel.items():
                    if qid not in qid_candidates:
                        continue
                    tag = (row.data_export_tag or "").strip()
                    if not tag:
                        continue
                    display = f"{tag}  •  {qid}"
                    tag_choices.append(display)
                    tag_to_qid[display] = qid
                if not tag_choices:
                    print(
                        f"{Colors.DIM}No DataExportTag values found in workbook.{Colors.RESET}"
                    )
                    continue
                selected_qids: List[str] = []
                while True:
                    selected = autocomplete_from_list(
                        message="Search by ExportTag:",
                        choices=tag_choices,
                        instruction="type to filter, enter to select",
                    )
                    if selected is None:
                        break
                    qid = tag_to_qid.get(selected)
                    if not qid:
                        needle = selected.strip().lower()
                        if not needle:
                            print(f"{Colors.DIM}No ExportTag provided.{Colors.RESET}")
                            continue
                        matches = [
                            choice for choice in tag_choices if needle in choice.lower()
                        ]
                        if len(matches) == 1:
                            qid = tag_to_qid.get(matches[0])
                        elif len(matches) > 1:
                            match = select_from_list(
                                message="Select ExportTag match:",
                                choices=matches,
                            )
                            if match is None:
                                continue
                            qid = tag_to_qid.get(match)
                        else:
                            print(
                                f"{Colors.DIM}No ExportTag matches found.{Colors.RESET}"
                            )
                            continue
                    if qid and qid not in selected_qids:
                        selected_qids.append(qid)
                    if not confirm("Add another ExportTag?", default=False):
                        break
                if not selected_qids:
                    continue
                qids = selected_qids

            if qid_choice.startswith("Cycle"):
                for qid in qids:
                    scope_expr = f"qid:{qid}"
                    qid_scope = ScopeFilter.parse(scope_expr)
                    scoped_unstaged = _detect_unstaged_changes(
                        survey_id, scope=qid_scope
                    )
                    scoped_subset = {
                        "items": scoped_unstaged["items"],
                        "edf": scoped_unstaged["edf"],
                        "js": scoped_unstaged["js"],
                        "translations": scoped_unstaged["translations"],
                    }
                    display_qid_mode_change_table(
                        survey_ref,
                        scope_label=qid,
                        unstaged=scoped_subset,
                    )
                    dims = _prompt_qid_mode_dimension_selection(
                        scoped_subset, allow_force=True
                    )
                    if not dims:
                        break
                    print(f"\n{Colors.BLUE}═══ QID-mode: {qid} ═══{Colors.RESET}")
                    summary = _sync_dimensions_once(
                        survey_id,
                        dims,
                        interactive=interactive,
                        force_live=force_live,
                        force_preview=force_preview,
                        auto_yes=auto_yes,
                        allow_drift=allow_drift,
                        skip_publish=skip_publish,
                        scope=qid_scope,
                        per_dimension=per_dimension,
                        ignore_embedded=True,
                        allow_skip_embedded=allow_skip_embedded,
                    )
                    if summary:
                        dimension_results.update(summary.dimension_results)
                    else:
                        break
                continue

            scope_expr = " OR ".join([f"qid:{qid}" for qid in qids])
            qid_scope = ScopeFilter.parse(scope_expr)
            scoped_unstaged = _detect_unstaged_changes(survey_id, scope=qid_scope)
            scoped_subset = {
                "items": scoped_unstaged["items"],
                "edf": scoped_unstaged["edf"],
                "js": scoped_unstaged["js"],
                "translations": scoped_unstaged["translations"],
            }
            scope_label = ",".join(qids) if len(qids) <= 3 else f"{len(qids)} QIDs"
            display_qid_mode_change_table(
                survey_ref,
                scope_label=scope_label,
                unstaged=scoped_subset,
            )
            dims = _prompt_qid_mode_dimension_selection(scoped_subset, allow_force=True)
            if not dims:
                continue
            summary = _sync_dimensions_once(
                survey_id,
                dims,
                interactive=interactive,
                force_live=force_live,
                force_preview=force_preview,
                auto_yes=auto_yes,
                allow_drift=allow_drift,
                skip_publish=skip_publish,
                scope=qid_scope,
                per_dimension=per_dimension,
                ignore_embedded=True,
                allow_skip_embedded=allow_skip_embedded,
            )
            if summary:
                dimension_results.update(summary.dimension_results)
            continue

        # Sync dimensions (unstaged selection)
        changes_for_selection = SurveyChanges(
            survey_id=survey_id,
            survey_name=survey_name,
            dimensions=unstaged,
        )
        selected = prompt_dimension_selection(changes_for_selection, interactive=True)
        if not selected:
            continue
        summary = _sync_dimensions_once(
            survey_id,
            selected,
            interactive=interactive,
            force_live=force_live,
            force_preview=force_preview,
            auto_yes=auto_yes,
            allow_drift=allow_drift,
            skip_publish=skip_publish,
            scope=scope,
            per_dimension=per_dimension,
            allow_skip_embedded=allow_skip_embedded,
        )
        if summary:
            dimension_results.update(summary.dimension_results)

    if dimension_results:
        # Refresh workbook if requested
        if refresh_workbooks:
            from .sync_core import init_survey_to_excel
            from .workbook_resolver import WorkbookResolver
            from .terminal_output import success, warn, info, dim

            # Resolve workbook path early for messaging
            resolver = WorkbookResolver()
            try:
                xlsx_path = resolver.resolve(survey_id)
            except Exception:
                from .config import resolve_root

                root = resolve_root(required=False) or Path.cwd()
                record = _get_inventory_cached(survey_id)
                survey_name = record.get("name", survey_id) if record else survey_id
                safe_name = survey_name.replace(" ", "_").replace("/", "_")
                xlsx_path = root / "excel" / f"{safe_name}-{survey_id}.xlsx"

            # Prompt in interactive mode (unless --yes)
            should_refresh = True
            if interactive and not auto_yes:
                from .interactive_menu import confirm

                # Show safety warning before prompt
                print(f"\n{Colors.YELLOW}⚠ Workbook Refresh Warning{Colors.RESET}")
                print(
                    f"{Colors.DIM}Refreshing rebuilds workbook structure from the survey definition/cache.{Colors.RESET}"
                )
                print(
                    f"{Colors.DIM}Non-empty translation cells are preserved, but unstaged local edits in non-translation fields may be replaced.{Colors.RESET}"
                )
                print(f"{Colors.DIM}Target workbook: {xlsx_path}{Colors.RESET}\n")

                should_refresh = confirm(
                    message=f"Refresh Excel workbook for {survey_ref}?", default=True
                )

            if should_refresh:
                info("[sync:refresh]", f"Refreshing workbook for {survey_ref}...")
                dim("[sync:refresh]", f"Target: {xlsx_path}")

                try:
                    init_survey_to_excel(survey_id, xlsx_path)
                    success("[sync:refresh]", "✓ Workbook refreshed successfully")
                    info("[sync:refresh]", f"Location: {xlsx_path}")
                except Exception as e:
                    warn("[sync:refresh]", f"Failed to refresh workbook: {e}")
                    warn("[sync:refresh]", f"Workbook path: {xlsx_path}")

        return SurveySyncSummary(
            survey_id=survey_id,
            survey_name=summary_name or survey_id,
            dimension_results=dimension_results,
        )
    return None


def sync_focal_surveys(
    interactive: bool = True,
    force_live: bool = False,
    force_preview: bool = False,
    auto_yes: bool = False,
    pending_action: str = "abort",
    scope: Optional[ScopeFilter] = None,
    process_all: bool = False,
    per_dimension: bool = False,
    skip_publish: bool = False,
    refresh_workbooks: bool = False,
    allow_drift: bool = False,
    allow_skip_embedded: bool = False,
    json_output: bool = False,
) -> bool:
    """Sync all focal surveys with detected changes.

    Args:
        interactive: Whether to prompt interactively
        force_live: Force push despite live responses
        force_preview: Suppress preview-only response warnings
        auto_yes: Skip all confirmation prompts
        pending_action: If pending staged changes exist and auto_yes is True, what to do: push/discard/abort
        scope: Optional scope filter (qid/tag/js boolean DSL) passed to items/js/translations where supported. See docs/reference/scope-semantics.md.
        process_all: Process all focal surveys without prompting
        per_dimension: Preview and approve each dimension separately
        skip_publish: Skip auto-publish step
        refresh_workbooks: Refresh Excel workbooks after successful sync
        allow_drift: Allow drift during sync
        allow_skip_embedded: Allow skip-embedded pushes when EDF is invalid
        json_output: Emit machine-readable JSON for blocked runs

    Returns:
        True if all syncs succeeded, False otherwise
    """
    import time

    from .rich_support import progress_context, should_use_rich, track_iterable
    from .terminal_output import format_elapsed

    start_time = time.perf_counter()

    focal_ids = get_focal_survey_ids()

    if not focal_ids:
        print("[sync] No focal surveys found in inventory")
        return True

    # Performance optimization: Parallel change detection
    print(f"[sync] Scanning {len(focal_ids)} focal surveys for staged changes...")
    use_rich = should_use_rich()
    progress_enabled = use_rich and len(focal_ids) > 1

    all_changes = []
    if len(focal_ids) > 3:
        # Use parallel detection for multiple surveys
        with ThreadPoolExecutor(max_workers=min(10, len(focal_ids))) as executor:
            future_to_id = {
                executor.submit(detect_survey_changes, sid): sid for sid in focal_ids
            }

            if progress_enabled:
                with progress_context(
                    "Detecting staged changes", total=len(focal_ids)
                ) as prog:
                    for future in as_completed(future_to_id):
                        try:
                            changes = future.result()
                            all_changes.append(changes)
                        except Exception as e:
                            survey_id = future_to_id[future]
                            logger.error(
                                f"[sync] Error detecting changes for {survey_id}: {e}"
                            )
                        if prog:
                            progress, task_id = prog
                            progress.advance(task_id)
            else:
                for future in as_completed(future_to_id):
                    try:
                        changes = future.result()
                        all_changes.append(changes)
                    except Exception as e:
                        survey_id = future_to_id[future]
                        logger.error(
                            f"[sync] Error detecting changes for {survey_id}: {e}"
                        )
    else:
        # Serial detection for small numbers
        ids_iter = (
            track_iterable(focal_ids, description="Detecting staged changes")
            if progress_enabled
            else focal_ids
        )
        for sid in ids_iter:
            try:
                all_changes.append(detect_survey_changes(sid))
            except Exception as e:
                # Create error entry for this survey
                logger.warning(f"[sync] Error detecting changes for {sid}: {e}")
                record = _get_inventory_cached(sid)
                survey_name = record.get("name", sid) if record else sid
                error_msg = str(e)
                if "Embedded_Data sheet is missing rows" in error_msg:
                    detail = f"Excel workbook missing embedded data fields. Run: qsync items pull --survey-id {sid}"
                elif "Mapping CSV missing a column" in error_msg:
                    detail = "Survey not in JS mapping file. Add column to survey_js/survey_qid_js_map.csv"
                else:
                    detail = error_msg[:100]

                all_changes.append(
                    SurveyChanges(
                        survey_id=sid,
                        survey_name=survey_name,
                        dimensions={
                            "items": DimensionChanges(
                                "items", True, "✗ error", set(), error_detail=detail
                            ),
                            "edf": DimensionChanges("edf", False, "No changes", set()),
                            "js": DimensionChanges("js", False, "No changes", set()),
                            "translations": DimensionChanges(
                                "translations", False, "No changes", set()
                            ),
                            "eos": DimensionChanges("eos", False, "No changes", set()),
                            "flow": DimensionChanges("flow", False, "No changes", set()),
                            "master": DimensionChanges(
                                "master", False, "No changes", set()
                            ),
                        },
                    )
                )

    surveys_with_changes = [c for c in all_changes if c.has_any_changes]

    # Also include surveys with fixable errors
    surveys_with_fixable_errors = [
        c
        for c in all_changes
        if not c.has_any_changes
        and any(_fixable_detail(dim) for dim in c.dimensions.values())
    ]

    surveys_with_issues = [
        c
        for c in all_changes
        if (not c.has_any_changes)
        and c.has_any_issues
        and c not in surveys_with_fixable_errors
    ]

    # Combine both lists
    surveys_to_process = (
        surveys_with_changes + surveys_with_fixable_errors + surveys_with_issues
    )

    # Sort by lastModified (newest first)
    surveys_to_process.sort(
        key=lambda s: (_get_inventory_cached(s.survey_id) or {}).get(
            "lastModified", ""
        ),
        reverse=True,
    )

    elapsed = time.perf_counter() - start_time
    print(
        f"{Colors.DIM}[sync] Change detection complete ({format_elapsed(elapsed)}){Colors.RESET}"
    )

    if not surveys_to_process:
        # Show table of all surveys with no changes
        display_change_detection_table(
            all_changes,
            show_all=True,
            interactive=interactive and not auto_yes,
        )
        print(
            f"\n{Colors.GREEN}✓{Colors.RESET} No changes detected in any focal survey"
        )
        print(
            f"{Colors.DIM}Run pull/preview/stage commands first to prepare changes{Colors.RESET}"
        )
        _clear_inventory_cache()
        return True

    def _display_focal_status() -> None:
        """Display change detection table and status summary."""
        display_change_detection_table(
            all_changes,
            show_all=True,
            interactive=interactive and not auto_yes,
        )
        change_count = len(surveys_with_changes)
        fixable_count = len(surveys_with_fixable_errors)
        issues_count = len(surveys_with_issues)
        parts = []
        if change_count:
            parts.append(f"{change_count} survey(s) with changes")
        if fixable_count:
            parts.append(f"{fixable_count} survey(s) with fixable issues")
        if issues_count:
            parts.append(f"{issues_count} survey(s) with issues")
        status_msg = " + ".join(parts) if parts else "No changes"
        print(f"\n{Colors.YELLOW}→{Colors.RESET} {status_msg}")

    def _recategorize() -> None:
        """Re-categorize all_changes into surveys_with_changes / fixable / issues."""
        nonlocal surveys_with_changes, surveys_with_fixable_errors
        nonlocal surveys_with_issues, surveys_to_process
        surveys_with_changes = [c for c in all_changes if c.has_any_changes]
        surveys_with_fixable_errors = [
            c
            for c in all_changes
            if not c.has_any_changes
            and any(_fixable_detail(dim) for dim in c.dimensions.values())
        ]
        surveys_with_issues = [
            c
            for c in all_changes
            if (not c.has_any_changes)
            and c.has_any_issues
            and c not in surveys_with_fixable_errors
        ]
        surveys_to_process = (
            surveys_with_changes + surveys_with_fixable_errors + surveys_with_issues
        )
        surveys_to_process.sort(
            key=lambda s: (_get_inventory_cached(s.survey_id) or {}).get(
                "lastModified", ""
            ),
            reverse=True,
        )

    # Show table and status summary
    _display_focal_status()

    # Select surveys to sync
    if process_all:
        # --all flag: process all without prompting
        selected = surveys_to_process
    elif interactive and not auto_yes:
        # Interactive selection loop — returns to menu after fix/issue actions.
        from .interactive_menu import select_from_list

        selected = []

        while True:
            # Build choice list with survey info
            choices = []

            # Section 1: Surveys with changes to sync
            surveys_with_changes_only = [
                c for c in surveys_to_process if c.has_any_changes
            ]
            for changes in surveys_with_changes_only:
                dims = ", ".join(changes.changed_dimensions)
                choice = f"sync {changes.survey_name} ({dims})"
                choices.append(choice)

            # Section 2: Surveys with fixable issues (separator + repair options)
            surveys_with_fixable_only = [
                c
                for c in surveys_to_process
                if any(_fixable_detail(d) for d in c.dimensions.values())
            ]

            if surveys_with_fixable_only:
                # Add separator
                choices.append("─" * 60)

                for changes in surveys_with_fixable_only:
                    fixable_dims = [
                        (dim, detail)
                        for dim, info in changes.dimensions.items()
                        if (detail := _fixable_detail(info))
                    ]
                    # Create compact error description
                    error_desc = "; ".join(
                        [
                            f"{dim}: {detail.split('.')[0]}"
                            for dim, detail in fixable_dims
                        ]
                    )
                    choice = f"fix {changes.survey_name} (⚠ {error_desc})"
                    choices.append(choice)

            surveys_with_issues_only = [
                c
                for c in surveys_to_process
                if (not c.has_any_changes)
                and c.has_any_issues
                and not any(_fixable_detail(d) for d in c.dimensions.values())
            ]
            if surveys_with_issues_only:
                choices.append("─" * 60)
                for changes in surveys_with_issues_only:
                    issue_dims = [
                        dim
                        for dim, info in changes.dimensions.items()
                        if info.error_detail or info.warning_detail
                    ]
                    choice = f"issues {changes.survey_name} ({', '.join(issue_dims)})"
                    choices.append(choice)

            # Section 3: Special options
            choices.append("─" * 60)
            choices.append("✓ Sync all surveys")
            choices.append("✗ Skip / Cancel")

            selection = select_from_list(
                message="What do you want to do?",
                choices=choices,
            )

            if selection is None or "Skip" in selection or "Cancel" in selection:
                print(f"\n{Colors.DIM}Sync cancelled{Colors.RESET}")
                break
            elif "Sync all surveys" in selection or "All surveys" in selection:
                selected = surveys_to_process
                break
            elif "─" in selection:
                # User selected separator, re-show menu
                continue
            else:
                # Find which survey was selected (handle both sync and fix commands)
                matched = []
                is_fix_operation = False
                is_issue_operation = False

                for changes in surveys_to_process:
                    # Check for sync option
                    if changes.has_any_changes:
                        dims = ", ".join(changes.changed_dimensions)
                        sync_choice = f"sync {changes.survey_name} ({dims})"
                        if sync_choice == selection:
                            matched = [changes]
                            break

                    # Check for fix option
                    fixable_dims = [
                        (dim, detail)
                        for dim, info in changes.dimensions.items()
                        if (detail := _fixable_detail(info))
                    ]
                    if fixable_dims:
                        error_desc = "; ".join(
                            [
                                f"{dim}: {detail.split('.')[0]}"
                                for dim, detail in fixable_dims
                            ]
                        )
                        fix_choice = f"fix {changes.survey_name} (⚠ {error_desc})"
                        if fix_choice == selection:
                            matched = [changes]
                            is_fix_operation = True
                            break
                    issue_dims = [
                        dim
                        for dim, info in changes.dimensions.items()
                        if info.error_detail or info.warning_detail
                    ]
                    if issue_dims:
                        issues_choice = (
                            f"issues {changes.survey_name} ({', '.join(issue_dims)})"
                        )
                        if issues_choice == selection:
                            matched = [changes]
                            is_issue_operation = True
                            break

                if not matched:
                    print(f"\n{Colors.DIM}No valid selection{Colors.RESET}")
                    break

                # Handle fix operation — run fix, re-detect, return to menu.
                if is_fix_operation:
                    from .interactive_menu import confirm
                    from .survey_ref import format_survey_ref

                    changes = matched[0]
                    survey_id = changes.survey_id
                    survey_ref = format_survey_ref(survey_id, changes.survey_name)

                    # Show fixable errors
                    fixable_errors = [
                        (dim, info)
                        for dim, info in changes.dimensions.items()
                        if _fixable_detail(info)
                    ]

                    print(
                        f"\n{Colors.YELLOW}⚠ Fixable Issues Detected for {survey_ref}{Colors.RESET}"
                    )
                    for dim, info in fixable_errors:
                        detail = _fixable_detail(info) or "Issue requires repair."
                        print(f"  • {Colors.BOLD}{dim}{Colors.RESET}: {detail}")

                    ordered = ["items", "edf", "translations", "eos", "flow", "js"]
                    fixable_errors.sort(
                        key=lambda entry: (
                            ordered.index(entry[0]) if entry[0] in ordered else 99
                        )
                    )
                    fix_cmds = [
                        (dim, _autofix_command(dim, survey_id))
                        for dim, _ in fixable_errors
                    ]
                    fix_cmds = [(dim, cmd) for dim, cmd in fix_cmds if cmd]

                    print(
                        f"\n{Colors.DIM}These issues can be fixed automatically by running:{Colors.RESET}"
                    )
                    for dim, cmd in fix_cmds:
                        print(f"  • {dim}: {Colors.CYAN}{cmd}{Colors.RESET}")

                    should_fix = confirm(message="Fix these issues now?", default=True)

                    if should_fix:
                        try:
                            for dim, cmd in fix_cmds:
                                print(f"\n[sync:fix] Running {cmd} for {survey_ref}...")
                                result = _run_autofix(dim, survey_id)
                                print(f"{Colors.GREEN}✓{Colors.RESET} {result}")
                        except Exception as e:
                            print(
                                f"{Colors.RED}✗ Failed to fix issues: {e}{Colors.RESET}"
                            )

                        # Re-detect changes for the fixed survey and refresh categories
                        try:
                            new_changes = detect_survey_changes(survey_id)
                            all_changes = [
                                new_changes if c.survey_id == survey_id else c
                                for c in all_changes
                            ]
                        except Exception:
                            pass
                        _recategorize()

                        if not surveys_to_process:
                            _display_focal_status()
                            print(
                                f"\n{Colors.GREEN}✓{Colors.RESET} All issues resolved, no remaining changes"
                            )
                            break
                        _display_focal_status()
                    else:
                        print(f"{Colors.DIM}Fix cancelled by user.{Colors.RESET}")

                    continue

                if is_issue_operation:
                    changes = matched[0]
                    print(
                        f"\n{Colors.YELLOW}⚠ Issues for {changes.survey_name} ({changes.survey_id}){Colors.RESET}"
                    )
                    for dim, info in changes.dimensions.items():
                        if info.error_detail:
                            print(f"  • {dim}: {info.error_detail}")
                        elif info.warning_detail:
                            print(f"  • {dim}: {info.warning_detail}")
                    continue

                # Sync operation — select and break out to sync phase
                selected = matched
                break

        if not selected:
            _clear_inventory_cache()
            return True
    else:
        # Non-interactive or --yes: sync all
        _display_focal_status()
        selected = surveys_to_process

    # Sync selected surveys and collect summaries
    summaries = []
    show_sync_progress = use_rich and len(selected) > 1 and not interactive
    if show_sync_progress:
        with progress_context("Syncing surveys", total=len(selected)) as prog:
            for changes in selected:
                if prog:
                    progress, task_id = prog
                    progress.update(
                        task_id, description=f"Syncing {changes.survey_name}"
                    )
                summary = sync_survey(
                    changes.survey_id,
                    dimensions=None,  # Auto-detect per survey
                    interactive=interactive,
                    force_live=force_live,
                    force_preview=force_preview,
                    auto_yes=auto_yes,
                    pending_action=pending_action,
                    scope=scope,
                    per_dimension=per_dimension,
                    skip_publish=skip_publish,
                    refresh_workbooks=refresh_workbooks,
                    allow_drift=allow_drift,
                    allow_skip_embedded=allow_skip_embedded,
                    json_output=json_output,
                )
                if summary:
                    summaries.append(summary)
                if prog:
                    progress.advance(task_id)
    else:
        for changes in selected:
            summary = sync_survey(
                changes.survey_id,
                dimensions=None,  # Auto-detect per survey
                interactive=interactive,
                force_live=force_live,
                force_preview=force_preview,
                auto_yes=auto_yes,
                pending_action=pending_action,
                scope=scope,
                per_dimension=per_dimension,
                skip_publish=skip_publish,
                refresh_workbooks=refresh_workbooks,
                allow_drift=allow_drift,
                allow_skip_embedded=allow_skip_embedded,
                json_output=json_output,
            )
            if summary:
                summaries.append(summary)

    # Clear cache after batch operation
    _clear_inventory_cache()

    elapsed = time.perf_counter() - start_time

    # Display final summary table
    if summaries:
        display_sync_summary_table(summaries)

        # Show recovery instructions for any failures
        display_recovery_instructions(
            summaries,
            force_live=force_live,
            force_preview=force_preview,
            scope_expr=scope.expression if scope else None,
            auto_yes=auto_yes,
        )

        # Count successes and failures
        all_success = all(s.success for s in summaries)

        if all_success:
            print(
                f"\n{Colors.GREEN}✓{Colors.RESET} All sync operations completed successfully"
            )
        else:
            print(f"\n{Colors.YELLOW}⚠{Colors.RESET} Some sync operations failed")

        from .terminal_output import mark_timing_emitted

        print(f"{Colors.DIM}Total time: {format_elapsed(elapsed)}{Colors.RESET}")
        mark_timing_emitted()

        return all_success

    # No surveys were synced
    return True


def display_dimension_preview(
    survey_id: str,
    dimension: str,
    *,
    detailed: bool = True,
    scope: Optional[ScopeFilter] = None,
    allow_drift: bool = False,
    interactive: bool = True,
    skip_embedded: bool = False,
) -> bool:
    """Display preview for a single dimension using existing preview functions.

    Reuses the beautiful diff displays already implemented for each dimension:
    - items: colorized unified diffs with context
    - js: side-by-side diff with highlighting
    - translations: key-by-key comparison
    - eos: message content diffs
    - flow: semantic flow-structure diffs
    - master: per-field CSV vs snapshot diff preview

    Args:
        survey_id: Survey ID
        dimension: Dimension name (items, edf, js, translations, eos, flow, master)
        detailed: Show detailed diffs (default True)
        scope: Optional scope filter for items dimension

    Returns:
        True if preview displayed successfully, False otherwise
    """
    from .terminal_colors import colorize_unified_diff_lines

    inv = _get_inventory_cached(survey_id) or {}
    survey_name = str(inv.get("name") or "").strip() or survey_id
    survey_label = f"{Colors.BOLD}{survey_name}{Colors.RESET} [{survey_id}]"

    print(
        f"\n{Colors.BLUE}═══ Preview: {dimension} dimension {survey_label} ═══{Colors.RESET}"
    )

    try:
        if dimension == "items":
            # Reuse existing items preview
            from .sync_core import preview_changes
            from .workbook_resolver import WorkbookResolver
            from .qualtrics_client import load_cached_survey
            from .drift_check import confirm_preview_drift
            from .qualtrics_client import refresh_survey_cache

            resolver = WorkbookResolver()
            xlsx_path = resolver.resolve(survey_id)
            cache_path = load_cached_survey(survey_id).path

            if not xlsx_path.exists():
                print(f"{Colors.DIM}No workbook found at {xlsx_path}{Colors.RESET}")
                return False

            def _update_cache() -> None:
                refresh_survey_cache(survey_id)
                print("[qsync:items] Refreshed cached survey definition from API.")

            confirm_preview_drift(
                survey_id=survey_id,
                dimension="items",
                allow_drift=allow_drift,
                interactive=interactive,
                update_cache=_update_cache,
            )

            scope_expr = scope.expression if scope and scope.expression else None
            changes = preview_changes(
                survey_id,
                xlsx_path,
                scope_expr=scope_expr,
                check_drift=False,
                skip_embedded=True,
            )

            if not changes:
                print(
                    f"{Colors.DIM}No differences between Excel and cached survey.{Colors.RESET}"
                )
                return True

            print(f"{Colors.DIM}Found {len(changes)} change(s){Colors.RESET}\n")

            for change in changes:
                print(Colors.DIM + "─" * 80 + Colors.RESET)

                # Header with change type and QID
                header = f"{change.kind.upper()} qid={change.qid}"
                if change.choice_id is not None:
                    header += f", choice_id={change.choice_id}"
                if change.answer_id is not None:
                    header += f", answer_id={change.answer_id}"
                print(header)

                # Show diff
                diff_lines = change.diff_lines or []
                if diff_lines:
                    print(f"  context: local={xlsx_path}, cache={cache_path}")
                    for line in colorize_unified_diff_lines(diff_lines):
                        print("  " + line)
                else:
                    print(f"  context: local={xlsx_path}, cache={cache_path}")
                    old_html = (change.old_html or "").strip()
                    new_html = (change.new_html or "").strip()
                    print(f"  {Colors.RED}OLD:{Colors.RESET} {old_html}")
                    print(f"  {Colors.GREEN}NEW:{Colors.RESET} {new_html}")

            return True

        elif dimension == "edf":
            from .workbook_resolver import WorkbookResolver
            from .qualtrics_client import load_cached_survey, refresh_survey_cache
            from .drift_check import confirm_preview_drift
            from .dimensions.items_core import (
                _collect_embedded_data_changes,
                _diff_lines,
                _display_embedded_value,
                check_embedded_data_health,
                format_embedded_data_health_warning,
            )

            resolver = WorkbookResolver()
            xlsx_path = resolver.resolve(survey_id)
            if not xlsx_path.exists():
                print(f"{Colors.DIM}No workbook found at {xlsx_path}{Colors.RESET}")
                return False

            cache = load_cached_survey(survey_id)
            cache_path = cache.path

            def _update_cache() -> None:
                refresh_survey_cache(survey_id)
                print("[qsync:edf] Refreshed cached survey definition from API.")

            confirm_preview_drift(
                survey_id=survey_id,
                dimension="items",
                allow_drift=allow_drift,
                interactive=interactive,
                update_cache=_update_cache,
            )

            health = check_embedded_data_health(survey_id, cache.payload, xlsx_path)
            if not health.is_valid:
                warning = format_embedded_data_health_warning(
                    health, survey_id=survey_id
                )
                print(f"{Colors.YELLOW}⚠ {warning}{Colors.RESET}")
                return True

            embedded_changes = _collect_embedded_data_changes(
                survey_id, cache.payload, xlsx_path
            )
            if not embedded_changes:
                print(f"{Colors.DIM}No EDF differences detected.{Colors.RESET}")
                return True

            print(
                f"{Colors.DIM}Found {len(embedded_changes)} EDF field change(s){Colors.RESET}\n"
            )
            for change in embedded_changes:
                row = change.get("row")
                field = getattr(row, "field", "") if row else ""
                flow_id = getattr(row, "flow_id", "") if row else ""
                header = f"EMBEDDED field={field}"
                if flow_id:
                    header += f", flow_id={flow_id}"
                print(Colors.DIM + "─" * 80 + Colors.RESET)
                print(header)
                old_display = _display_embedded_value(change.get("old_value"))
                new_display = _display_embedded_value(change.get("new_value"))
                diff_lines = _diff_lines(
                    str(old_display or ""),
                    str(new_display or ""),
                    context=f"Field: {field}",
                )
                print(f"  context: local={xlsx_path}, cache={cache_path}")
                for line in colorize_unified_diff_lines(diff_lines):
                    print("  " + line)

            return True

        elif dimension == "js":
            # Reuse existing JS preview
            from .js_preview import preview_differences
            from .config import resolve_root
            from .drift_check import confirm_preview_drift
            from .qualtrics_client import refresh_survey_cache

            root = resolve_root(required=False) or Path.cwd()
            mapping_csv = root / "survey_js" / "survey_qid_js_map.csv"

            if not mapping_csv.exists():
                print(
                    f"{Colors.DIM}JS mapping not found at {mapping_csv}{Colors.RESET}"
                )
                return False

            scope_expr = scope.expression if scope and scope.expression else None

            def _update_cache() -> None:
                refresh_survey_cache(survey_id)
                print("[qsync:js] Refreshed cached survey definition from API.")

            drift_report = confirm_preview_drift(
                survey_id=survey_id,
                dimension="js",
                allow_drift=allow_drift,
                interactive=interactive,
                update_cache=_update_cache,
            )

            results = preview_differences(
                survey_id=survey_id,
                mapping_csv=mapping_csv,
                detailed=detailed,
                scope_expr=scope_expr,
                check_drift=False,
            )

            if drift_report.has_drift and (
                not results or all(r.status == "equal" for r in results)
            ):
                print(
                    f"{Colors.YELLOW}Note:{Colors.RESET} Preview compares local JS "
                    "files to the cached survey. They currently match, so there are "
                    "no local diffs to show. Any push will still apply the cached JS "
                    "to live and may overwrite the drift shown above."
                )

            if not results or all(r.status == "equal" for r in results):
                print(f"{Colors.DIM}No JS differences detected.{Colors.RESET}")
                return True

            return True

        elif dimension == "translations":
            # Reuse existing translations preview
            from .translations import (
                preview_translations,
            )
            from .drift_check import confirm_preview_drift
            from .qualtrics_client import refresh_survey_cache

            def _update_cache() -> None:
                refresh_survey_cache(survey_id)
                print(
                    "[qsync:translations] Refreshed cached survey definition from API."
                )

            confirm_preview_drift(
                survey_id=survey_id,
                dimension="translations",
                allow_drift=allow_drift,
                interactive=interactive,
                update_cache=_update_cache,
            )

            preview_lines = preview_translations(
                survey_id,
                None,
                detailed=detailed,
                scope=scope,
            )

            if not preview_lines or all(
                "no changes" in line.lower() for line in preview_lines
            ):
                print(f"{Colors.DIM}No translation differences detected.{Colors.RESET}")
                return True

            print(f"{Colors.DIM}Translation preview:{Colors.RESET}\n")
            for line in preview_lines:
                # Color-code the status
                if "no changes" in line.lower():
                    print(f"  {Colors.GREEN}✓{Colors.RESET} {line}")
                elif "changed=" in line.lower() or "missing=" in line.lower():
                    print(f"  {Colors.YELLOW}⚡{Colors.RESET} {line}")
                elif "not found" in line.lower():
                    print(f"  {Colors.RED}✗{Colors.RESET} {line}")
                else:
                    print(f"  {line}")

            return True

        elif dimension == "eos":
            # Reuse existing EOS preview
            from .eos_messages import preview_eos_messages, pull_eos_messages
            from .drift_check import confirm_preview_drift

            try:

                def _update_cache() -> None:
                    pull_eos_messages(
                        survey_id=survey_id,
                        allow_shared=True,
                        include_backups_scan=False,
                    )
                    print("[qsync:eos] Refreshed local EOS messages from API.")

                confirm_preview_drift(
                    survey_id=survey_id,
                    dimension="eos",
                    allow_drift=allow_drift,
                    interactive=interactive,
                    update_cache=_update_cache,
                )

                preview_lines = preview_eos_messages(
                    survey_id=survey_id,
                    allow_shared=True,  # For preview, show all
                    detailed=detailed,
                    check_drift=False,
                )
            except Exception as e:
                print(f"{Colors.RED}✗ Error previewing EOS messages:{Colors.RESET} {e}")
                return False

            if not preview_lines or all(
                "no changes" in line.lower() for line in preview_lines
            ):
                print(f"{Colors.DIM}No EOS message differences detected.{Colors.RESET}")
                return True

            print(f"{Colors.DIM}EOS message preview:{Colors.RESET}\n")
            for line in preview_lines:
                # Color-code the status
                if "no changes" in line.lower():
                    print(f"  {Colors.GREEN}✓{Colors.RESET} {line}")
                elif "CHANGED" in line:
                    print(f"  {Colors.YELLOW}⚡{Colors.RESET} {line}")
                elif "not pulled" in line.lower():
                    print(f"  {Colors.DIM}{line}{Colors.RESET}")
                else:
                    print(f"  {line}")

            return True

        elif dimension == "flow":
            # Reuse existing flow preview
            from .drift_check import confirm_preview_drift

            try:

                def _update_cache() -> None:
                    flow_dimension.pull(survey_id, force=True)
                    print("[qsync:flow] Refreshed flow baseline from API.")

                confirm_preview_drift(
                    survey_id=survey_id,
                    dimension="flow",
                    allow_drift=allow_drift,
                    interactive=interactive,
                    update_cache=_update_cache,
                )

                changes = flow_dimension.preview(survey_id)

                if not changes:
                    print(f"{Colors.DIM}No flow differences detected.{Colors.RESET}")
                    return True

                print(f"{Colors.DIM}Flow preview:{Colors.RESET}\n")
                from .dimensions.flow_diff import format_diff_for_display

                for line in format_diff_for_display(changes):
                    # Color-code the status
                    if line.startswith("+"):
                        print(f"  {Colors.GREEN}{line}{Colors.RESET}")
                    elif line.startswith("-"):
                        print(f"  {Colors.RED}{line}{Colors.RESET}")
                    elif line.startswith("~"):
                        print(f"  {Colors.YELLOW}{line}{Colors.RESET}")
                    else:
                        print(f"  {line}")

                return True

            except Exception as e:
                print(f"{Colors.RED}✗ Error previewing flow:{Colors.RESET} {e}")
                return False

        else:
            print(f"{Colors.RED}✗ Unknown dimension: {dimension}{Colors.RESET}")
            return False

    except Exception as e:
        logger.error(f"[sync:preview] Error previewing {dimension}: {e}", exc_info=True)
        print(f"{Colors.RED}✗ Error previewing {dimension}:{Colors.RESET} {e}")
        return False


def display_unified_preview(
    survey_id: str,
    dimensions: List[str],
    *,
    per_dimension: bool = False,
    detailed: bool = True,
    scope: Optional[ScopeFilter] = None,
    allow_drift: bool = False,
    interactive: bool = True,
    skip_embedded: bool = False,
) -> bool:
    """Display unified preview across multiple dimensions.

    Args:
        survey_id: Survey ID
        dimensions: List of dimension names to preview
        per_dimension: If True, prompt after each dimension; if False, show all at once
        detailed: Show detailed diffs
        scope: Optional scope filter

    Returns:
        True if all previews displayed successfully
    """

    if not dimensions:
        return True

    success = True

    if per_dimension:
        # Show each dimension separately with optional prompts
        for dim in dimensions:
            dim_success = display_dimension_preview(
                survey_id=survey_id,
                dimension=dim,
                detailed=detailed,
                scope=scope,
                allow_drift=allow_drift,
                interactive=interactive,
                skip_embedded=skip_embedded if dim == "items" else False,
            )
            success = success and dim_success

            # After each dimension (except the last), optionally prompt to continue
            if dim != dimensions[-1]:
                print()  # Blank line for spacing
    else:
        # Show all dimensions in sequence
        for dim in dimensions:
            dim_success = display_dimension_preview(
                survey_id=survey_id,
                dimension=dim,
                detailed=detailed,
                scope=scope,
                allow_drift=allow_drift,
                interactive=interactive,
                skip_embedded=skip_embedded if dim == "items" else False,
            )
            success = success and dim_success
            print()  # Blank line between dimensions

    return success
