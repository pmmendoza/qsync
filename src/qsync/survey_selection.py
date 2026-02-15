"""Shared interactive survey selection helpers.

Goal: keep survey picking consistent across commands.

Contract:
- Non-interactive (non-TTY): callers should require `--survey-id` explicitly.
- Interactive: allow browsing from inventory records with:
  - optional filter prompt when the list is large
  - disabled entries for locked/inactive/no-API-edit (when metadata exists)
  - on-demand "View details" table
  - manual SurveyID entry escape hatch
"""

from __future__ import annotations

from typing import Any


def pick_survey_id_from_records(
    *,
    message: str,
    records: list[dict[str, Any]],
    include_back: bool = True,
    back_label: str = "↩ Back",
    include_manual: bool = True,
    manual_label: str = "✎ Enter SurveyID manually",
    include_details: bool = True,
    details_label: str = "🔍 View details (top 30)",
) -> str | None:
    """Prompt interactively for a survey from pre-fetched records.

    Expected record keys (best-effort):
    - id, name, lastModified
    - isActive, locked, editableViaApi (optional)
    """

    from .interactive_menu import MenuItem, confirm, select_from_list, text_input
    from .input_validators import SurveyIdValidator
    from .rich_support import should_use_rich
    from .terminal_output import rich_console

    filtered = records
    if len(filtered) > 60:
        raw = input("Filter surveys by name/ID substring (blank to show all): ").strip()
        if raw:
            needle = raw.lower()
            filtered = [
                s
                for s in records
                if needle in str(s.get("id") or "").lower()
                or needle in str(s.get("name") or "").lower()
            ]
            if not filtered:
                print("[qsync] No surveys matched that filter.")
                return None
        else:
            if not confirm(
                f"List all {len(filtered)} surveys in an interactive menu? (may be slow)",
                default=False,
            ):
                return None

    TRUE_TOKENS = {"true", "1", "yes", "y", "t"}

    def _truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in TRUE_TOKENS

    def _print_details() -> None:
        rows = [r for r in filtered if (r.get("id") or "").strip()]
        rows = rows[:30]
        if should_use_rich():
            console = rich_console()
            if console is not None:
                from rich import box
                from rich.table import Table

                table = Table(title="Inventory (top 30)", box=box.SIMPLE, show_lines=False)
                table.add_column("Survey ID", no_wrap=True)
                table.add_column("Name")
                table.add_column("Active", no_wrap=True)
                table.add_column("Locked", no_wrap=True)
                table.add_column("API", no_wrap=True)
                table.add_column("Last Modified", no_wrap=True)
                for r in rows:
                    sid = str(r.get("id") or "").strip()
                    name = str(r.get("name") or "Untitled").strip()
                    active = _truthy(r.get("isActive")) if r.get("isActive") is not None else True
                    locked = _truthy(r.get("locked"))
                    editable = _truthy(r.get("editableViaApi")) if r.get("editableViaApi") is not None else True
                    last_mod = str(r.get("lastModified") or "").strip()
                    table.add_row(
                        sid,
                        name,
                        "yes" if active else "no",
                        "yes" if locked else "no",
                        "yes" if editable else "no",
                        last_mod or "-",
                    )
                console.print(table)
                print()
                return

        print("\n[qsync] Inventory (top 30):")
        for r in rows:
            sid = str(r.get("id") or "").strip()
            name = str(r.get("name") or "Untitled").strip()
            active = _truthy(r.get("isActive")) if r.get("isActive") is not None else True
            locked = _truthy(r.get("locked"))
            editable = _truthy(r.get("editableViaApi")) if r.get("editableViaApi") is not None else True
            last_mod = str(r.get("lastModified") or "").strip()
            flags = []
            if not active:
                flags.append("inactive")
            if locked:
                flags.append("locked")
            if not editable:
                flags.append("no-api-edit")
            suffix = f" ({', '.join(flags)})" if flags else ""
            print(f"  - {sid}: {name}{suffix}  lastModified={last_mod or '-'}")
        print()

    while True:
        items: list[MenuItem] = []
        for s in filtered:
            sid = str(s.get("id") or "").strip()
            if not sid:
                continue
            name = str(s.get("name") or "Untitled").strip()
            locked = _truthy(s.get("locked"))
            active = _truthy(s.get("isActive")) if s.get("isActive") is not None else True
            editable = _truthy(s.get("editableViaApi")) if s.get("editableViaApi") is not None else True

            enabled = True
            disabled_reason = None
            if locked:
                enabled = False
                disabled_reason = "locked"
            elif not active:
                enabled = False
                disabled_reason = "inactive"
            elif not editable:
                enabled = False
                disabled_reason = "no API edit"

            flags = []
            if not active:
                flags.append("inactive")
            if locked:
                flags.append("locked")
            if not editable:
                flags.append("no-api-edit")
            suffix = f" ({', '.join(flags)})" if flags else ""

            items.append(
                MenuItem(
                    label=f"{sid} - {name}{suffix}",
                    value=sid,
                    enabled=enabled,
                    disabled_reason=disabled_reason,
                )
            )

        items.append(MenuItem.separator("─" * 60))
        if include_details:
            items.append(MenuItem(label=details_label, value="details", enabled=True))
        if include_manual:
            items.append(MenuItem(label=manual_label, value="manual", enabled=True))
        if include_back:
            items.append(MenuItem(label=back_label, value="back", enabled=True))

        selection = select_from_list(message=message, choices=items)
        if selection is None or selection == "back":
            return None
        if selection == "details":
            _print_details()
            continue
        if selection == "manual":
            manual = text_input(
                "Enter Qualtrics SurveyID",
                instruction="Example: SV_...",
                validator=SurveyIdValidator(),
                validate_while_typing=True,
            )
            return (manual or "").strip() or None
        return str(selection).strip() or None

