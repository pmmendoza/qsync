"""Shared interactive survey selection helpers.

Goal: keep survey picking consistent across commands.

Contract:
- Non-interactive (non-TTY): callers should require `--survey-id` explicitly.
- Interactive: allow browsing from inventory records with:
  - optional filter prompt when the list is large
  - disabled entries for locked/inactive/no-API-edit (when metadata exists)
  - on-demand "View details" table
  - manual SurveyID entry escape hatch (or regex filter)
"""

from __future__ import annotations

import re
from typing import Any


def _is_valid_survey_id(value: str) -> bool:
    value = (value or "").strip()
    if not value:
        return False
    try:
        from .input_validators import SurveyIdValidator

        SurveyIdValidator()(value)
        return True
    except Exception:
        return False


def _compile_filter(raw: str) -> re.Pattern[str] | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return re.compile(raw, flags=re.IGNORECASE)
    except re.error:
        return re.compile(re.escape(raw), flags=re.IGNORECASE)


def _filter_records(records: list[dict[str, Any]], raw: str) -> list[dict[str, Any]]:
    pat = _compile_filter(raw)
    if pat is None:
        return list(records)
    out: list[dict[str, Any]] = []
    for r in records:
        sid = str(r.get("id") or "")
        name = str(r.get("name") or "")
        if pat.search(sid) or pat.search(name):
            out.append(r)
    return out


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
    from .rich_support import should_use_rich
    from .terminal_output import rich_console

    filtered = records
    if len(filtered) > 60:
        raw = input(
            "Filter surveys by name/ID (regex or plain text; blank to show all): "
        ).strip()
        if raw:
            if _is_valid_survey_id(raw):
                return raw
            filtered = _filter_records(records, raw)
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
                "Enter SurveyID or regex",
                instruction="Example SurveyID: SV_...  |  Example regex: (?i)brand|test",
            )
            manual = (manual or "").strip()
            if not manual:
                return None
            if _is_valid_survey_id(manual):
                return manual

            narrowed = _filter_records(filtered, manual)
            if not narrowed:
                print("[qsync] No surveys matched that regex/text filter.")
                continue
            if len(narrowed) == 1:
                only_id = str(narrowed[0].get("id") or "").strip()
                return only_id or None

            filtered = narrowed
            continue
        return str(selection).strip() or None


def list_surveys_via_api(
    *,
    base_url: str,
    headers: dict[str, str],
    timeout: int = 60,
) -> list[dict[str, Any]]:
    """Fetch all surveys via Qualtrics API (follows pagination until exhausted)."""

    from .api_push import send_api_request

    url: str | None = f"https://{base_url}/API/v3/surveys"
    surveys: list[dict[str, Any]] = []
    first_page = True
    seen_urls: set[str] = set()

    while url:
        if url in seen_urls:
            break
        seen_urls.add(url)

        params = {"pageSize": 100} if first_page else None
        resp = send_api_request(
            action="qsync.survey.list",
            method="GET",
            base_url=base_url,
            headers=headers,
            path=url,
            log_event=False,
            params=params,
            timeout=timeout,
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


def pick_survey_id_from_api(
    *,
    message: str,
    base_url: str,
    headers: dict[str, str],
    include_back: bool = True,
    include_manual: bool = True,
    include_details: bool = True,
) -> str | None:
    """Pick a survey ID using live API listing (remote source-of-truth)."""

    surveys = list_surveys_via_api(base_url=base_url, headers=headers)
    shaped: list[dict[str, Any]] = []
    for s in surveys:
        sid = str(s.get("id") or "").strip()
        if not sid:
            continue
        shaped.append(
            {
                "id": sid,
                "name": str(s.get("name") or "Untitled").strip(),
                "lastModified": str(
                    s.get("lastModified") or s.get("creationDate") or ""
                ).strip(),
                "isActive": s.get("isActive"),
            }
        )

    return pick_survey_id_from_records(
        message=message,
        records=shaped,
        include_back=include_back,
        include_manual=include_manual,
        manual_label="✎ Enter SurveyID or regex filter",
        include_details=include_details,
        details_label="🔍 View details (top 30)",
    )
