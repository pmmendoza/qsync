"""Shared interactive survey selection helpers.

Goal: keep survey picking consistent across commands.

Contract:
- Non-interactive (non-TTY): callers should require `--survey-id` explicitly.
- Interactive: allow browsing from inventory records with:
  - autocomplete-style filtering
  - optional regex/manual filter fallback
  - disabled entries for locked/inactive/no-API-edit (when metadata exists)
  - on-demand "View details" table
  - optional multi-select for bulk flows
  - manual SurveyID entry escape hatch
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


def _normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    sid = str(record.get("id") or "").strip()
    if not sid:
        return {}
    return {
        "id": sid,
        "name": str(record.get("name") or "Untitled").strip(),
        "lastModified": str(record.get("lastModified") or "").strip(),
        "isActive": record.get("isActive"),
        "locked": record.get("locked"),
        "editableViaApi": record.get("editableViaApi"),
    }


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"true", "1", "yes", "y", "t"}


def _format_survey_label(record: dict[str, Any]) -> str:
    sid = str(record.get("id") or "")
    name = str(record.get("name") or "Untitled")
    flags = []
    if not _truthy(record.get("isActive")):
        flags.append("inactive")
    if _truthy(record.get("locked")):
        flags.append("locked")
    if not _truthy(record.get("editableViaApi")):
        flags.append("no-api-edit")
    suffix = f" ({', '.join(flags)})" if flags else ""
    return f"{sid} - {name}{suffix}"


def _strip_label_to_survey_id(value: str) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if " - " in text:
        token = text.split(" - ", 1)[0].strip()
        if _is_valid_survey_id(token):
            return token
    if _is_valid_survey_id(text):
        return text
    return None


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


def _print_details(records: list[dict[str, Any]]) -> None:
    rows = [r for r in records if (r.get("id") or "").strip()]
    rows = rows[:30]
    from .rich_support import should_use_rich
    from .terminal_output import rich_console

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
                editable = (
                    _truthy(r.get("editableViaApi"))
                    if r.get("editableViaApi") is not None
                    else True
                )
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
        editable = (
            _truthy(r.get("editableViaApi"))
            if r.get("editableViaApi") is not None
            else True
        )
        flags = []
        if not active:
            flags.append("inactive")
        if locked:
            flags.append("locked")
        if not editable:
            flags.append("no-api-edit")
        suffix = f" ({', '.join(flags)})" if flags else ""
        print(f"  - {sid}: {name}{suffix}  lastModified={str(r.get('lastModified') or '-').strip()}")
    print()


def _selection_enabled(record: dict[str, Any]) -> bool:
    return not (_truthy(record.get("locked")) or not _truthy(record.get("editableViaApi", True)))


def _select_single_record(
    *,
    message: str,
    records: list[dict[str, Any]],
    include_back: bool,
    back_label: str,
    include_manual: bool,
    manual_label: str,
    include_details: bool,
    details_label: str,
) -> str | None:
    from .interactive_menu import MenuItem, select_from_list, text_input

    current_records = list(records)

    while True:
        if not current_records:
            print("[qsync] No surveys available for selection.")
            return None

        if len(current_records) > 60:
            from .interactive_menu import autocomplete_from_list

            suggestions = [_format_survey_label(r) for r in current_records]
            suggestion = autocomplete_from_list(
                message=message,
                choices=suggestions,
                instruction="Type to filter; press Enter to accept a match",
            )
            if suggestion is None:
                return None
            if suggestion in suggestions:
                return _strip_label_to_survey_id(suggestion)
            if _is_valid_survey_id(suggestion):
                return suggestion

            narrowed = _filter_records(current_records, suggestion)
            if not narrowed:
                print("[qsync] No surveys matched that filter.")
                continue
            if len(narrowed) == 1:
                return _strip_label_to_survey_id(_format_survey_label(narrowed[0]))
            current_records = narrowed
            continue

        items: list[MenuItem] = []
        for s in current_records:
            sid = str(s.get("id") or "").strip()
            if not sid:
                continue
            name = str(s.get("name") or "Untitled").strip()
            locked = _truthy(s.get("locked"))
            active = _truthy(s.get("isActive")) if s.get("isActive") is not None else True
            editable = (
                _truthy(s.get("editableViaApi"))
                if s.get("editableViaApi") is not None
                else True
            )

            enabled = _selection_enabled({"locked": locked, "editableViaApi": editable})
            disabled_reason = None
            if not enabled:
                if locked:
                    disabled_reason = "locked"
                elif not editable:
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
            _print_details(current_records)
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

            narrowed = _filter_records(current_records, manual)
            if not narrowed:
                print("[qsync] No surveys matched that regex/text filter.")
                continue
            if len(narrowed) == 1:
                return _strip_label_to_survey_id(_format_survey_label(narrowed[0]))
            current_records = narrowed
            continue

        return _strip_label_to_survey_id(f"{selection} - placeholder") or str(selection).strip()


def _select_multiple_records(
    *,
    message: str,
    records: list[dict[str, Any]],
) -> list[str] | None:
    from .interactive_menu import multi_select_from_list

    enabled_records = [
        r
        for r in records
        if _selection_enabled(
            {
                "locked": _truthy(r.get("locked")),
                "editableViaApi": _truthy(
                    r.get("editableViaApi") if r.get("editableViaApi") is not None else True
                ),
            }
        )
    ]

    if not enabled_records:
        print("[qsync] No editable surveys available for selection.")
        return None

    choices = [_format_survey_label(r) for r in enabled_records]

    if len(choices) == 1:
        return [str(enabled_records[0].get("id") or "")]

    if len(choices) > 60:
        from .interactive_menu import autocomplete_from_list

        narrowed = autocomplete_from_list(
            message=message,
            choices=choices,
            instruction="Type a filter first, then press Enter to proceed to multi-select",
        )
        if narrowed is None:
            return None

        narrowed_id = _strip_label_to_survey_id(narrowed)
        if narrowed_id:
            return [narrowed_id]
        narrowed_records = _filter_records(enabled_records, narrowed)
        if narrowed_records:
            if len(narrowed_records) == 1:
                return [_strip_label_to_survey_id(_format_survey_label(narrowed_records[0]))]
            choices = [_format_survey_label(r) for r in narrowed_records]

    selected = multi_select_from_list(
        message=message,
        choices=choices,
        instruction="Space: toggle, Enter: confirm",
    )
    if selected is None:
        return None
    if not selected:
        return []

    values = [
        sid
        for sid in map(_strip_label_to_survey_id, selected)
        if sid is not None
    ]
    # Deduplicate but keep order.
    out: list[str] = []
    seen: set[str] = set()
    for sid in values:
        if sid in seen:
            continue
        seen.add(sid)
        out.append(sid)
    return out


def pick_survey_ids_from_records(
    *,
    message: str,
    records: list[dict[str, Any]],
    include_back: bool = True,
    back_label: str = "↩ Back",
    include_manual: bool = True,
    manual_label: str = "✎ Enter SurveyID manually",
    include_details: bool = True,
    details_label: str = "🔍 View details (top 30)",
    allow_multiple: bool = False,
) -> list[str] | None:
    """Prompt interactively for one or more surveys from pre-fetched records.

    Returns:
        A list of Survey IDs, or None when cancelled.
    """

    normalized: list[dict[str, Any]] = []
    for record in records:
        row = _normalize_record(record)
        if row:
            normalized.append(row)

    if not normalized:
        return None

    if allow_multiple:
        return _select_multiple_records(message=message, records=normalized)

    # Keep original single-selection behavior but with autocomplete/manual enhancements.
    selected = _select_single_record(
        message=message,
        records=normalized,
        include_back=include_back,
        back_label=back_label,
        include_manual=include_manual,
        manual_label=manual_label,
        include_details=include_details,
        details_label=details_label,
    )
    if selected is None:
        return None
    sid = _strip_label_to_survey_id(str(selected).strip())
    if sid is None:
        return None
    return [sid]


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
    """Legacy single-survey compatibility wrapper."""

    picked = pick_survey_ids_from_records(
        message=message,
        records=records,
        include_back=include_back,
        include_manual=include_manual,
        manual_label=manual_label,
        include_details=include_details,
        details_label=details_label,
        back_label=back_label,
        allow_multiple=False,
    )
    if not picked:
        return None
    return picked[0]


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


def pick_survey_ids_from_api(
    *,
    message: str,
    base_url: str,
    headers: dict[str, str],
    include_back: bool = True,
    include_manual: bool = True,
    include_details: bool = True,
    allow_multiple: bool = False,
) -> list[str] | None:
    """Pick one or more survey IDs using live API listing (remote source-of-truth)."""

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

    if not shaped:
        return None

    return pick_survey_ids_from_records(
        message=message,
        records=shaped,
        include_back=include_back,
        include_manual=include_manual,
        manual_label="✎ Enter SurveyID or regex filter",
        include_details=include_details,
        details_label="🔍 View details (top 30)",
        allow_multiple=allow_multiple,
    )


def pick_survey_id_from_api(
    *,
    message: str,
    base_url: str,
    headers: dict[str, str],
    include_back: bool = True,
    include_manual: bool = True,
    include_details: bool = True,
) -> str | None:
    """Legacy single-survey compatibility wrapper for API picker."""

    picked = pick_survey_ids_from_api(
        message=message,
        base_url=base_url,
        headers=headers,
        include_back=include_back,
        include_manual=include_manual,
        include_details=include_details,
        allow_multiple=False,
    )
    if not picked:
        return None
    return picked[0]
