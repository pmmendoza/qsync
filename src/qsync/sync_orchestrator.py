"""Sync orchestrator for multi-dimension coordination.

This module provides the `qsync sync` command that orchestrates changes across
multiple dimensions (items, js, translations, eos) for one or more surveys.

Features:
- Automatic change detection across all dimensions
- Interactive dimension selection
- Per-dimension workflow (pull, preview, stage, push)
- Cross-dimension conflict detection and resolution
- Non-interactive automation with --yes

Created: 2026-01-22 for QSYNC-HARM-022 (Stage 3: Orchestration)
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .pending_stage import clear_pending, list_pending, load_pending
from .dimensions import eos as eos_dimension
from .dimensions import items as items_dimension
from .dimensions import js as js_dimension
from .dimensions import translations as translations_dimension
from .dimensions.types import DimensionChanges
from .scope_filter import ScopeFilter
from .survey_inventory import get_focal_survey_ids, load_inventory_record
from .terminal_colors import Colors, colorize_unified_diff_lines

logger = logging.getLogger(__name__)

# Performance optimization: Cache inventory records
_inventory_cache: Optional[Dict[str, dict]] = None


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


def _autofix_command(dimension: str, survey_id: str) -> Optional[str]:
    if dimension == "items":
        return f"qsync items pull --survey-id {survey_id}"
    if dimension == "translations":
        return f"qsync items pull --survey-id {survey_id}"
    if dimension == "eos":
        return f"qsync eos pull --survey-id {survey_id}"
    return None


def _run_autofix(dimension: str, survey_id: str) -> str:
    if dimension == "items":
        from .sync_core import init_survey_to_excel
        from .workbook_resolver import WorkbookResolver

        resolver = WorkbookResolver()
        xlsx_path = resolver.resolve(survey_id)
        init_survey_to_excel(survey_id, xlsx_path)
        return f"Regenerated Excel file at {xlsx_path}"
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
        init_survey_to_excel(survey_id, xlsx_path, languages=languages or None)
        return f"Refreshed translation columns in {xlsx_path}"
    if dimension == "eos":
        from .eos_messages import pull_eos_messages

        pull_eos_messages(survey_id=survey_id, allow_shared=True)
        return "Pulled EOS messages to contents/qualtrics_library_messages"
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
        dimension: Dimension name (items, js, translations, eos)

    Returns:
        DimensionChanges with detection status and affected QIDs
    """
    try:
        if dimension == "items":
            return items_dimension.detect_changes(survey_id)
        if dimension == "js":
            return js_dimension.detect_changes(survey_id)
        if dimension == "translations":
            return translations_dimension.detect_changes(survey_id)
        if dimension == "eos":
            return eos_dimension.detect_changes(survey_id)

        return DimensionChanges(
            dimension=dimension,
            has_changes=False,
            change_summary="No changes",
            affected_qids=set(),
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
    choices.append("✓ Apply all (safe merge: items → js → translations)")
    choices.append("✗ Skip this QID")

    selection = select_from_list(
        message="Resolve conflict:",
        choices=choices,
    )

    if selection is None or "Skip" in selection:
        return []
    elif "Apply all" in selection:
        # Safe merge order: items first, then js, then translations
        order = ["items", "js", "translations", "eos"]
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

    Safe merge order: items → js → translations → eos

    Args:
        conflicts: List of conflicts to resolve

    Returns:
        Dict mapping QID to list of dimensions to apply (in order)
    """
    resolutions = {}
    order = ["items", "js", "translations", "eos"]

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
        "js": detect_dimension_changes(survey_id, "js"),
        "translations": detect_dimension_changes(survey_id, "translations"),
        "eos": detect_dimension_changes(survey_id, "eos"),
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


def display_change_detection_table(
    all_changes: List[SurveyChanges], show_all: bool = False
):
    """Display survey × dimension change detection table.

    Args:
        all_changes: List of detected changes for all surveys
        show_all: If True, show all surveys including those with no changes
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
    col_dim = 18  # Increased from 12 to fit longer messages

    # Header
    header = (
        f"{Colors.DIM}"
        f"{'Survey ID':<{col_survey_id}} "
        f"{'Name':<{col_name}} "
        f"{'Items':<{col_dim}} "
        f"{'JS':<{col_dim}} "
        f"{'Trans':<{col_dim}} "
        f"{'EOS':<{col_dim}}"
        f"{Colors.RESET}"
    )
    separator = f"{Colors.DIM}{'─' * (col_survey_id + col_name + col_dim * 4 + 4)}{Colors.RESET}"

    print(header)
    print(separator)

    for changes in display_changes:
        # Get status for each dimension - show actual summary or dash
        def format_status(dim_changes):
            if dim_changes.has_changes:
                summary = dim_changes.change_summary
                dim_name = dim_changes.dimension
                # Truncate if too long (keep room for color codes)
                max_len = col_dim - 2
                if len(summary) > max_len:
                    # Smart truncation - dimension-aware
                    if ":" in summary:
                        prefix, rest = summary.split(":", 1)
                        # Extract numbers if present
                        import re

                        # For items unstaged: extract QID count, not change count
                        if dim_name == "items" and "QID(s)" in summary:
                            # Pattern: "N change(s) in M QID(s)" -> extract M
                            qid_match = re.search(r"in (\d+) QID", rest)
                            if qid_match:
                                summary = f"{prefix}: {qid_match.group(1)} QIDs"
                            else:
                                summary = summary[:max_len]
                        else:
                            # Default: extract first number
                            num_match = re.search(r"(\d+)", rest)
                            if num_match:
                                # Infer unit from dimension
                                if dim_name == "items":
                                    unit = "QIDs"
                                elif dim_name == "js":
                                    unit = "files"
                                elif dim_name == "translations":
                                    unit = "langs"
                                elif dim_name == "eos":
                                    unit = "msgs"
                                else:
                                    unit = "items"
                                summary = f"{prefix}: {num_match.group(1)} {unit}"
                            else:
                                summary = summary[:max_len]
                    else:
                        summary = summary[:max_len]

                # Color code based on type
                if summary.startswith("✓"):
                    return f"{Colors.GREEN}{summary}{Colors.RESET}"
                elif summary.startswith("⚡"):
                    return f"{Colors.YELLOW}{summary}{Colors.RESET}"
                elif "Error" in summary or summary.startswith("✗"):
                    return f"{Colors.RED}✗ error{Colors.RESET}"
                else:
                    return summary
            return f"{Colors.DIM}─{Colors.RESET}"

        items_status = format_status(changes.dimensions["items"])
        js_status = format_status(changes.dimensions["js"])
        trans_status = format_status(changes.dimensions["translations"])
        eos_status = format_status(changes.dimensions["eos"])

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
            f"{_pad_to_width(js_status, col_dim)} "
            f"{_pad_to_width(trans_status, col_dim)} "
            f"{_pad_to_width(eos_status, col_dim)}"
        )
        print(row)

    # Print error explanations if any
    errors = []
    for changes in display_changes:
        for dim_name, dim_changes in changes.dimensions.items():
            if dim_changes.error_detail:
                errors.append((changes.survey_name, dim_name, dim_changes.error_detail))

    if errors:
        print(f"\n{Colors.YELLOW}⚠️  Errors detected:{Colors.RESET}")
        for survey_name, dimension, detail in errors:
            # Highlight commands in error messages
            if "Run:" in detail or "Add" in detail:
                parts = (
                    detail.split("Run:") if "Run:" in detail else detail.split("Add")
                )
                if len(parts) == 2:
                    prefix = parts[0].strip()
                    cmd = parts[1].strip()
                    separator = "Run:" if "Run:" in detail else "Add"
                    print(
                        f"  {Colors.DIM}•{Colors.RESET} {survey_name} ({dimension}): {prefix} {separator} {Colors.CYAN}{cmd}{Colors.RESET}"
                    )
                else:
                    print(
                        f"  {Colors.DIM}•{Colors.RESET} {survey_name} ({dimension}): {detail}"
                    )
            else:
                print(
                    f"  {Colors.DIM}•{Colors.RESET} {survey_name} ({dimension}): {detail}"
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
        f"{Colors.DIM}{'Survey ID':<22} {'Name':<30} {'Items':<12} {'JS':<12} {'Trans':<12} {'EOS':<12}{Colors.RESET}"
    )
    print(f"{Colors.DIM}{'─' * 96}{Colors.RESET}")

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
        js_status = get_status("js")
        trans_status = get_status("translations")
        eos_status = get_status("eos")

        # Truncate name if needed
        name = (
            summary.survey_name[:28]
            if len(summary.survey_name) > 28
            else summary.survey_name
        )

        print(
            f"{summary.survey_id:<22} {name:<30} {items_status:<20} {js_status:<20} {trans_status:<20} {eos_status:<20}"
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
    col_dim = 18

    def _format_status(dim_changes: DimensionChanges) -> str:
        if dim_changes.error_detail:
            return f"{Colors.RED}✗ error{Colors.RESET}"
        if not dim_changes.has_changes:
            return f"{Colors.DIM}─{Colors.RESET}"
        summary = dim_changes.change_summary
        max_len = col_dim - 2
        if len(summary) > max_len:
            summary = summary[: max_len - 1] + "…"
        if summary.startswith("⚡"):
            return f"{Colors.YELLOW}{summary}{Colors.RESET}"
        if summary.startswith("✓"):
            return f"{Colors.GREEN}{summary}{Colors.RESET}"
        return summary

    print(
        f"\n{Colors.BLUE}═══ QID-mode Change Detection {survey_ref} ═══{Colors.RESET}"
    )

    header = (
        f"{Colors.DIM}"
        f"{'Scope':<{col_scope}} "
        f"{'Items':<{col_dim}} "
        f"{'JS':<{col_dim}} "
        f"{'Trans':<{col_dim}}"
        f"{Colors.RESET}"
    )
    separator = f"{Colors.DIM}{'─' * (col_scope + col_dim * 3 + 3)}{Colors.RESET}"

    scope_display = (
        scope_label[: col_scope - 1] + "…"
        if len(scope_label) > col_scope
        else scope_label
    )
    row = (
        f"{_pad_to_width(scope_display, col_scope)} "
        f"{_pad_to_width(_format_status(unstaged['items']), col_dim)} "
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
    import re

    dims = ["items", "js", "translations"]

    def count_label(dim_changes: DimensionChanges) -> str:
        if dim_changes.error_detail:
            return "✗ error"
        if not dim_changes.has_changes:
            return "none"
        match = re.search(r"(\d+)", dim_changes.change_summary or "")
        return f"⚡ {match.group(1)}" if match else "⚡ ?"

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
    for dim in ["items", "js", "translations", "eos"]:
        if changes.dimensions[dim].error_detail:
            errors.append((dim, changes.dimensions[dim].error_detail))

    if errors:
        print(f"\n{Colors.YELLOW}⚠️  Errors:{Colors.RESET}")
        for dimension, detail in errors:
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
        for dim in ["items", "js", "translations", "eos"]
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
        dimension: Dimension name (items, js, translations, eos)
        scope: Optional scope filter

    Returns:
        True if staging succeeded, False otherwise
    """
    try:
        if dimension == "items":
            return items_dimension.stage(
                survey_id,
                scope=scope,
                ignore_embedded=ignore_embedded,
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
) -> DimensionSyncResult:
    """Sync a single dimension for a survey (push staged changes).

    Args:
        survey_id: Survey ID
        dimension: Dimension name (items, js, translations, eos)
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
            load_pending,
        )

        pending = load_pending(survey_id, dimension)
        if pending is None:
            return False

        payload = getattr(pending, "payload", None)
        if dimension == "items" and isinstance(payload, ItemsPendingPayload):
            return bool(
                list(payload.qids or [])
                or list(payload.embedded_fields or [])
                or list(getattr(payload, "structural_ops", None) or [])
            )
        if dimension == "js" and isinstance(payload, JsPendingPayload):
            return bool(list(payload.entries or []))
        if dimension == "translations" and isinstance(
            payload, TranslationsPendingPayload
        ):
            return bool(list(payload.qids or []) or list(payload.metadata_keys or []))
        if dimension == "eos" and isinstance(payload, EosPendingPayload):
            return bool(list(payload.operations or []))

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
                    )
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

    return "staged"


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
        )
        if changes:
            qids = set(c.qid for c in changes if c.qid)
            return DimensionChanges(
                dimension="items",
                has_changes=True,
                change_summary=f"⚡ Unstaged: {len(changes)} change(s) in {len(qids)} QID(s)",
                affected_qids=qids,
            )
    return DimensionChanges(
        dimension="items",
        has_changes=False,
        change_summary="No changes",
        affected_qids=set(),
    )


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
            )
    except Exception:
        return DimensionChanges(
            dimension="js",
            has_changes=False,
            change_summary="✗ Error",
            affected_qids=set(),
            error_detail="JS detection failed.",
            safe_to_autofix=False,
        )

    return DimensionChanges(
        dimension="js",
        has_changes=False,
        change_summary="No changes",
        affected_qids=set(),
    )


def _detect_unstaged_translations(
    survey_id: str,
    *,
    scope: Optional[ScopeFilter] = None,
) -> DimensionChanges:
    return translations_dimension.detect_unstaged_changes(survey_id, scope=scope)


def _detect_unstaged_eos(survey_id: str) -> DimensionChanges:
    return eos_dimension.detect_unstaged_changes(survey_id)


def _detect_unstaged_changes(
    survey_id: str,
    *,
    scope: Optional[ScopeFilter] = None,
) -> Dict[str, DimensionChanges]:
    return {
        "items": _detect_unstaged_items(survey_id, scope=scope),
        "js": _detect_unstaged_js(survey_id, scope=scope),
        "translations": _detect_unstaged_translations(survey_id, scope=scope),
        "eos": _detect_unstaged_eos(survey_id),
    }


def _display_survey_overview(
    survey_ref: str,
    *,
    staged: Dict[str, str],
    unstaged: Dict[str, DimensionChanges],
    has_pending: bool,
) -> None:

    print(f"\n{Colors.BLUE}═══ Survey Overview {survey_ref} ═══{Colors.RESET}")
    print(f"{Colors.BOLD}Staged changes:{Colors.RESET}")
    for dim in ["items", "js", "translations", "eos"]:
        summary = staged.get(dim, "none")
        print(f"  • {dim}: {summary}")

    print(f"\n{Colors.BOLD}Unstaged changes:{Colors.RESET}")
    col_dim = 18

    def _format_status(dim_changes: DimensionChanges) -> str:
        if dim_changes.error_detail:
            return f"{Colors.RED}✗ error{Colors.RESET}"
        if not dim_changes.has_changes:
            return f"{Colors.DIM}─{Colors.RESET}"
        summary = dim_changes.change_summary
        max_len = col_dim - 2
        if len(summary) > max_len:
            summary = summary[: max_len - 1] + "…"
        if summary.startswith("✓"):
            return f"{Colors.GREEN}{summary}{Colors.RESET}"
        if summary.startswith("⚡"):
            return f"{Colors.YELLOW}{summary}{Colors.RESET}"
        if "Error" in summary or summary.startswith("✗"):
            return f"{Colors.RED}✗ error{Colors.RESET}"
        return summary

    header = (
        f"{Colors.DIM}"
        f"{'Items':<{col_dim}} "
        f"{'JS':<{col_dim}} "
        f"{'Trans':<{col_dim}} "
        f"{'EOS':<{col_dim}}"
        f"{Colors.RESET}"
    )
    separator = f"{Colors.DIM}{'─' * (col_dim * 4 + 3)}{Colors.RESET}"
    row = (
        f"{_pad_to_width(_format_status(unstaged['items']), col_dim)} "
        f"{_pad_to_width(_format_status(unstaged['js']), col_dim)} "
        f"{_pad_to_width(_format_status(unstaged['translations']), col_dim)} "
        f"{_pad_to_width(_format_status(unstaged['eos']), col_dim)}"
    )
    print(header)
    print(separator)
    print(row)

    errors: list[tuple[str, str]] = []
    for dim in ["items", "js", "translations", "eos"]:
        info = unstaged.get(dim)
        if info and info.error_detail:
            errors.append((dim, info.error_detail))
    if errors:
        print(f"\n{Colors.YELLOW}⚠️  Errors:{Colors.RESET}")
        for dim, detail in errors:
            print(f"  {Colors.DIM}•{Colors.RESET} {dim}: {detail}")

    print(f"\n{Colors.BOLD}Next actions:{Colors.RESET}")
    if has_pending:
        print("  • Preview staged changes (live vs cache)")
        print("  • Push staged changes now")
        print("  • Discard staged changes (clear pending + refresh cache)")
    if any(info.has_changes for info in unstaged.values()):
        print("  • Sync dimensions (preview → stage → push)")
        print("  • QID-mode (items/js/translations only)")
    if not has_pending and not any(info.has_changes for info in unstaged.values()):
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
    from .pending_stage import ItemsPendingPayload, TranslationsPendingPayload

    if not pending:
        print(f"{Colors.DIM}No staged changes to preview.{Colors.RESET}")
        return

    print(f"\n{Colors.BLUE}═══ Staged Preview (live vs cache) ═══{Colors.RESET}")
    safe_order = ["items", "js", "translations", "eos"]
    use_context = True
    if interactive:
        scope_choice = select_from_list(
            message="Preview drift scope:",
            choices=[
                "Full survey (unscoped)",
                "Staged-only (scoped)",
                "↩ Cancel",
            ],
        )
        if scope_choice is None or "Cancel" in scope_choice:
            return
        use_context = scope_choice.startswith("Staged")

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

        print(f"\n{Colors.BOLD}{dim}{Colors.RESET}:")
        if not use_context:
            report = check_drift(survey_id, dim, interactive=interactive, context=None)
            show_full = False
            if report.has_drift and report.diff_lines and interactive:
                show_full = confirm("Show full diff?", default=False)
            report.display(interactive=interactive, show_full=show_full)
            continue

        # Staged-only preview (pending vs cache), with live drift warning
        drift_report = check_drift(survey_id, dim, interactive=False, context=None)
        if drift_report.has_drift:
            print(
                f"{Colors.YELLOW}⚠ Live drift detected; preview shows staged vs cache.{Colors.RESET}"
            )

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
) -> bool:
    from .interactive_menu import select_from_list
    from .qualtrics_client import refresh_survey_cache

    safe_order = ["items", "js", "translations", "eos"]

    while True:
        choices = [
            "👀 Preview staged changes (live vs cache)",
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
            emb_count = (
                len(pending.payload.embedded_fields)
                if pending.payload.embedded_fields
                else 0
            )
            if qid_count:
                parts.append(f"items:{qid_count}Q")
            if emb_count:
                parts.append(f"emb:{emb_count}")
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
                "[sync:conflict] Using safe merge order: items → js → translations → eos"
            )

    # Sort dimensions in safe merge order
    safe_order = ["items", "js", "translations", "eos"]
    dimensions_sorted = [d for d in safe_order if d in dimensions]

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
        )

        if not preview_success:
            print(f"{Colors.YELLOW}⚠ Warning:{Colors.RESET} Some previews failed")

        # For unstaged dimensions, prompt to stage
        js_stale_pending = False
        if "js" in dimensions_sorted and _is_dimension_staged(survey_id, "js"):
            js_stale_pending = _js_pending_out_of_sync(survey_id, scope=scope)
            if js_stale_pending:
                print(
                    f"{Colors.YELLOW}⚠ Staged JS no longer matches local files.{Colors.RESET} "
                    "Re-stage to refresh the cache (or clear staged changes)."
                )

        unstaged_dims = [
            dim
            for dim in dimensions_sorted
            if not _is_dimension_staged(survey_id, dim)
            or (dim == "js" and js_stale_pending)
        ]

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
                        ignore_embedded=ignore_embedded if dim == "items" else False,
                        allow_drift=allow_drift,
                        interactive=interactive and not auto_yes,
                    )

                    if not stage_success:
                        print(f"{Colors.RED}✗ Failed to stage {dim}{Colors.RESET}")
                        return None

                    print(f"{Colors.GREEN}✓ Staged {dim}{Colors.RESET}")

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
                if dim == "items":
                    qid_count = len(pending.payload.qids) if pending.payload.qids else 0
                    emb_count = (
                        len(pending.payload.embedded_fields)
                        if pending.payload.embedded_fields
                        else 0
                    )
                    summary_parts = []
                    if qid_count:
                        summary_parts.append(f"{qid_count} QID(s)")
                    if emb_count:
                        summary_parts.append(f"{emb_count} embedded field(s)")
                    summary = (
                        ", ".join(summary_parts) if summary_parts else "no changes"
                    )
                    print(f"  • {Colors.BOLD}{dim}{Colors.RESET}: {summary}")
                elif dim == "js":
                    count = (
                        len(pending.payload.entries) if pending.payload.entries else 0
                    )
                    print(f"  • {Colors.BOLD}{dim}{Colors.RESET}: {count} JS file(s)")
                elif dim == "translations":
                    count = (
                        len(pending.payload.languages)
                        if pending.payload.languages
                        else 0
                    )
                    langs = (
                        ", ".join(pending.payload.languages)
                        if pending.payload.languages
                        else "none"
                    )
                    print(
                        f"  • {Colors.BOLD}{dim}{Colors.RESET}: {count} language(s) ({langs})"
                    )
                elif dim == "eos":
                    count = (
                        len(pending.payload.operations)
                        if pending.payload.operations
                        else 0
                    )
                    print(f"  • {Colors.BOLD}{dim}{Colors.RESET}: {count} operation(s)")
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
        scope: Optional scope filter
        per_dimension: Preview and approve each dimension separately
        skip_publish: Skip auto-publish step
        refresh_workbooks: Refresh Excel workbooks after successful sync
        allow_drift: Allow drift during sync

    Returns:
        SurveySyncSummary with per-dimension results, or None if nothing synced
    """
    from .survey_ref import format_survey_ref
    from .interactive_menu import select_from_list, confirm, autocomplete_from_list

    dimension_results: Dict[str, DimensionSyncResult] = {}
    summary_name: Optional[str] = None

    # Detect staged changes
    changes = detect_survey_changes(survey_id)
    survey_ref = format_survey_ref(survey_id, getattr(changes, "survey_name", None))

    # Check for fixable errors in interactive single-survey mode
    if interactive and not auto_yes:
        fixable_errors = [
            (dim, info)
            for dim, info in changes.dimensions.items()
            if info.safe_to_autofix and info.error_detail
        ]

        if fixable_errors:
            print(f"\n{Colors.YELLOW}⚠ Fixable Issues Detected{Colors.RESET}")
            for dim, info in fixable_errors:
                print(f"  • {Colors.BOLD}{dim}{Colors.RESET}: {info.error_detail}")

            ordered = ["items", "translations", "eos", "js"]
            fixable_errors.sort(
                key=lambda entry: ordered.index(entry[0]) if entry[0] in ordered else 99
            )
            fix_cmds = [
                (dim, _autofix_command(dim, survey_id)) for dim, _ in fixable_errors
            ]
            fix_cmds = [(dim, cmd) for dim, cmd in fix_cmds if cmd]

            print(
                f"\n{Colors.DIM}These issues can be fixed automatically by running:{Colors.RESET}"
            )
            for dim, cmd in fix_cmds:
                print(f"  • {dim}: {Colors.CYAN}{cmd}{Colors.RESET}")

            should_fix = confirm(message="Fix these issues now?", default=True)

            if should_fix:
                for dim, cmd in fix_cmds:
                    print(f"\n[sync:fix] Running {cmd} for {survey_ref}...")
                    try:
                        result = _run_autofix(dim, survey_id)
                        print(f"{Colors.GREEN}✓{Colors.RESET} {result}")
                    except Exception as e:
                        print(f"{Colors.RED}✗ Failed to fix {dim}: {e}{Colors.RESET}")
                        return None

                print("\n[sync] Re-detecting changes after fix...")
                changes = detect_survey_changes(survey_id)
                survey_ref = format_survey_ref(
                    survey_id, getattr(changes, "survey_name", None)
                )
            else:
                if fix_cmds:
                    print(
                        f"{Colors.DIM}Fix cancelled. Please run manually:{Colors.RESET}"
                    )
                    for _, cmd in fix_cmds:
                        print(f"  {Colors.CYAN}{cmd}{Colors.RESET}")
                return None

    pending = list_pending(survey_id)
    if auto_yes and pending:
        action = (pending_action or "abort").strip().lower()
        if action == "abort":
            raise SystemExit(
                "Pending staged changes detected. Resolve them interactively before using --yes."
            )
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
            safe_order = ["items", "js", "translations", "eos"]
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
            for dim in ["items", "js", "translations", "eos"]
        }

        _display_survey_overview(
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
            "🔎 QID-mode (items/js/translations)",
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
                    f"{Colors.DIM}Refreshing the workbook will overwrite any uncommitted local edits in Excel.{Colors.RESET}"
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
) -> bool:
    """Sync all focal surveys with detected changes.

    Args:
        interactive: Whether to prompt interactively
        force_live: Force push despite live responses
        force_preview: Suppress preview-only response warnings
        auto_yes: Skip all confirmation prompts
        pending_action: If pending staged changes exist and auto_yes is True, what to do: push/discard/abort
        scope: Optional scope filter
        process_all: Process all focal surveys without prompting
        per_dimension: Preview and approve each dimension separately
        skip_publish: Skip auto-publish step
        refresh_workbooks: Refresh Excel workbooks after successful sync
        allow_drift: Allow drift during sync

    Returns:
        True if all syncs succeeded, False otherwise
    """
    import time

    start_time = time.time()

    focal_ids = get_focal_survey_ids()

    if not focal_ids:
        print("[sync] No focal surveys found in inventory")
        return True

    # Performance optimization: Parallel change detection
    print(f"[sync] Scanning {len(focal_ids)} focal surveys for staged changes...")

    all_changes = []
    if len(focal_ids) > 3:
        # Use parallel detection for multiple surveys
        with ThreadPoolExecutor(max_workers=min(10, len(focal_ids))) as executor:
            future_to_id = {
                executor.submit(detect_survey_changes, sid): sid for sid in focal_ids
            }

            for future in as_completed(future_to_id):
                try:
                    changes = future.result()
                    all_changes.append(changes)
                except Exception as e:
                    survey_id = future_to_id[future]
                    logger.error(f"[sync] Error detecting changes for {survey_id}: {e}")
    else:
        # Serial detection for small numbers
        for sid in focal_ids:
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
                            "js": DimensionChanges("js", False, "No changes", set()),
                            "translations": DimensionChanges(
                                "translations", False, "No changes", set()
                            ),
                            "eos": DimensionChanges("eos", False, "No changes", set()),
                        },
                    )
                )

    surveys_with_changes = [c for c in all_changes if c.has_any_changes]

    # Also include surveys with fixable errors
    surveys_with_fixable_errors = [
        c
        for c in all_changes
        if not c.has_any_changes
        and any(
            dim.safe_to_autofix and dim.error_detail for dim in c.dimensions.values()
        )
    ]

    # Combine both lists
    surveys_to_process = surveys_with_changes + surveys_with_fixable_errors

    # Sort by lastModified (newest first)
    surveys_to_process.sort(
        key=lambda s: (_get_inventory_cached(s.survey_id) or {}).get(
            "lastModified", ""
        ),
        reverse=True,
    )

    elapsed = time.time() - start_time
    print(
        f"{Colors.DIM}[sync] Change detection complete ({elapsed:.1f}s){Colors.RESET}"
    )

    if not surveys_to_process:
        # Show table of all surveys with no changes
        display_change_detection_table(all_changes, show_all=True)
        print(
            f"\n{Colors.GREEN}✓{Colors.RESET} No changes detected in any focal survey"
        )
        print(
            f"{Colors.DIM}Run pull/preview/stage commands first to prepare changes{Colors.RESET}"
        )
        _clear_inventory_cache()
        return True

    # Show table of surveys with changes
    display_change_detection_table(all_changes, show_all=True)

    # Build descriptive message
    change_count = len(surveys_with_changes)
    fixable_count = len(surveys_with_fixable_errors)

    parts = []
    if change_count:
        parts.append(f"{change_count} survey(s) with changes")
    if fixable_count:
        parts.append(f"{fixable_count} survey(s) with fixable issues")

    status_msg = " + ".join(parts) if parts else "No changes"
    print(f"\n{Colors.YELLOW}→{Colors.RESET} {status_msg}")

    # Select surveys to sync
    if process_all:
        # --all flag: process all without prompting
        selected = surveys_to_process
    elif interactive and not auto_yes:
        # Interactive selection with arrow-key menu
        from .interactive_menu import select_from_list

        # Build choice list with survey info
        choices = []

        # Section 1: Surveys with changes to sync
        surveys_with_changes_only = [c for c in surveys_to_process if c.has_any_changes]
        for changes in surveys_with_changes_only:
            dims = ", ".join(changes.changed_dimensions)
            choice = f"sync {changes.survey_name} ({dims})"
            choices.append(choice)

        # Section 2: Surveys with fixable issues (separator + repair options)
        surveys_with_fixable_only = [
            c
            for c in surveys_to_process
            if any(d.safe_to_autofix and d.error_detail for d in c.dimensions.values())
        ]

        if surveys_with_fixable_only:
            # Add separator
            choices.append("─" * 60)

            for changes in surveys_with_fixable_only:
                fixable_dims = [
                    (dim, info.error_detail)
                    for dim, info in changes.dimensions.items()
                    if info.safe_to_autofix and info.error_detail
                ]
                # Create compact error description
                error_desc = "; ".join(
                    [f"{dim}: {detail.split('.')[0]}" for dim, detail in fixable_dims]
                )
                choice = f"fix {changes.survey_name} (⚠ {error_desc})"
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
            _clear_inventory_cache()
            return True
        elif "Sync all surveys" in selection or "All surveys" in selection:
            selected = surveys_to_process
        elif "─" in selection:
            # User selected separator, treat as cancel
            print(f"\n{Colors.DIM}Sync cancelled{Colors.RESET}")
            _clear_inventory_cache()
            return True
        else:
            # Find which survey was selected (handle both sync and fix commands)
            selected = []
            is_fix_operation = False

            for changes in surveys_to_process:
                # Check for sync option
                if changes.has_any_changes:
                    dims = ", ".join(changes.changed_dimensions)
                    sync_choice = f"sync {changes.survey_name} ({dims})"
                    if sync_choice == selection:
                        selected = [changes]
                        break

                # Check for fix option
                fixable_dims = [
                    (dim, info.error_detail)
                    for dim, info in changes.dimensions.items()
                    if info.safe_to_autofix and info.error_detail
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
                        selected = [changes]
                        is_fix_operation = True
                        break

            if not selected:
                print(f"\n{Colors.DIM}No valid selection{Colors.RESET}")
                _clear_inventory_cache()
                return True

            # Handle fix operation specially - run fix and return to menu
            if is_fix_operation:
                from .interactive_menu import confirm
                from .survey_ref import format_survey_ref

                changes = selected[0]
                survey_id = changes.survey_id
                survey_ref = format_survey_ref(survey_id, changes.survey_name)

                # Show fixable errors
                fixable_errors = [
                    (dim, info)
                    for dim, info in changes.dimensions.items()
                    if info.safe_to_autofix and info.error_detail
                ]

                print(
                    f"\n{Colors.YELLOW}⚠ Fixable Issues Detected for {survey_ref}{Colors.RESET}"
                )
                for dim, info in fixable_errors:
                    print(f"  • {Colors.BOLD}{dim}{Colors.RESET}: {info.error_detail}")

                ordered = ["items", "translations", "eos", "js"]
                fixable_errors.sort(
                    key=lambda entry: (
                        ordered.index(entry[0]) if entry[0] in ordered else 99
                    )
                )
                fix_cmds = [
                    (dim, _autofix_command(dim, survey_id)) for dim, _ in fixable_errors
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
                        print(f"{Colors.RED}✗ Failed to fix issues: {e}{Colors.RESET}")

                # Return to menu by recursively calling sync_focal_surveys
                print(f"\n{Colors.DIM}Returning to sync menu...{Colors.RESET}\n")
                return sync_focal_surveys(
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
                    process_all=False,  # Always show menu after fix
                )
    else:
        # Non-interactive or --yes: sync all
        selected = surveys_to_process

    # Sync selected surveys and collect summaries
    summaries = []
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
        )
        if summary:
            summaries.append(summary)

    # Clear cache after batch operation
    _clear_inventory_cache()

    elapsed = time.time() - start_time

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

        print(f"{Colors.DIM}Total time: {elapsed:.1f}s{Colors.RESET}")

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
) -> bool:
    """Display preview for a single dimension using existing preview functions.

    Reuses the beautiful diff displays already implemented for each dimension:
    - items: colorized unified diffs with context
    - js: side-by-side diff with highlighting
    - translations: key-by-key comparison
    - eos: message content diffs

    Args:
        survey_id: Survey ID
        dimension: Dimension name (items, js, translations, eos)
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
            )
            success = success and dim_success
            print()  # Blank line between dimensions

    return success
