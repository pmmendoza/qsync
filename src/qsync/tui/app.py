"""Textual TUI entrypoint for qsync (optional extra: `qsync[tui]`).

Design goals:
- Keep base CLI stable: this module is imported only when `qsync tui` is executed.
- Provide a real two-pane UI (left menu, right context) for a small pilot flow.
- Avoid side effects on import; load workspace data only when screens mount.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
from typing import Any


def _format_ops_summary(ops: list[dict]) -> str:
    if not ops:
        return "Staged ops: (none)"
    lines = [f"Staged ops: {len(ops)}"]
    by_qid: dict[str, int] = {}
    for op in ops:
        qid = str(op.get("qid") or "").strip() or "?"
        by_qid[qid] = by_qid.get(qid, 0) + 1
    for qid, count in sorted(by_qid.items(), key=lambda kv: (-kv[1], kv[0]))[:12]:
        lines.append(f"- {qid}: {count}")
    if len(by_qid) > 12:
        lines.append(f"... ({len(by_qid) - 12} more QID(s))")
    last = ops[-3:]
    lines.append("")
    lines.append("Recent:")
    for op in last:
        op_type = str(op.get("op") or "")
        qid = str(op.get("qid") or "")
        cid = str(op.get("choice_id") or "")
        aid = str(op.get("answer_id") or "")
        tail = ""
        if cid and aid:
            tail = f" {cid}/{aid}"
        elif cid:
            tail = f" {cid}"
        elif aid:
            tail = f" {aid}"
        lines.append(f"- {op_type} {qid}{tail}".strip())
    return "\n".join(lines)


def _survey_filter_matches(row: dict[str, Any], query: str) -> bool:
    raw = (query or "").strip()
    if not raw:
        return True
    sid = str(row.get("id") or "").strip()
    name = str(row.get("name") or "Untitled").strip()
    try:
        pattern = re.compile(raw, re.IGNORECASE)
    except re.error:
        pattern = re.compile(re.escape(raw), re.IGNORECASE)
    return bool(pattern.search(sid) or pattern.search(name))


def _survey_option_label(row: dict[str, Any]) -> str:
    sid = str(row.get("id") or "").strip()
    name = str(row.get("name") or "Untitled").strip()
    return f"{sid} - {name}"

def _account_context_lines() -> list[str]:
    """Return safe (non-secret) account context lines for display."""

    try:
        from pathlib import Path

        from qsync.config import (
            get_active_account,
            get_client_config,
            load_account_env,
            resolve_root,
        )

        root = resolve_root(required=False) or Path.cwd()
        account = None
        try:
            account = get_active_account()
        except Exception:
            account = None

        env = load_account_env(account, root=root) if account else None
        base, headers = get_client_config(env) if env else get_client_config()
        token_present = False
        for k, v in (headers or {}).items():
            if "token" in str(k).lower() and bool(str(v or "").strip()):
                token_present = True
                break

        return [
            f"Account: {account or 'default'}",
            f"Base URL: {base}",
            f"Token present: {'yes' if token_present else 'no'}",
        ]
    except Exception:
        return ["Account: (unknown)"]

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, OptionList, Static


@dataclass
class SyncWizardState:
    survey_id: str | None = None
    survey_name: str | None = None
    dimensions: list[str] | None = None

    def command(self) -> str | None:
        if not self.survey_id:
            return None
        if not self.dimensions:
            return f"qsync sync --survey-id {self.survey_id}"
        dims = ",".join(self.dimensions)
        return f"qsync sync --survey-id {self.survey_id} --dimensions {dims}"


class HelpScreen(Screen):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
        ("?", "app.pop_screen", "Close help"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            "\n".join(
                [
                    "qsync TUI (pilot)",
                    "",
                    "Keys:",
                    "- ↑/↓ : move",
                    "- Enter: select",
                    "- b / Esc: back",
                    "- q: quit",
                    "- ?: help",
                    "",
                    "This TUI is a thin wrapper around existing qsync workflows.",
                    "It intentionally avoids running destructive operations directly.",
                ]
            ),
            id="help_body",
        )
        yield Footer()


class MainMenuScreen(Screen):
    BINDINGS = [
        ("q", "app.quit", "Quit"),
        ("?", "app.push_screen('help')", "Help"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield OptionList(
                "Sync wizard (select survey + dimensions)",
                "Survey menu (TUI)",
                "Content editors",
                "Help",
                "Exit",
                id="menu",
            )
            yield Static(
                "Select an action.\n\nTip: press ? for help.",
                id="detail",
            )
        yield Footer()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:  # type: ignore[name-defined]
        detail = self.query_one("#detail", Static)
        idx = getattr(event, "option_index", None)
        if idx == 0:
            detail.update(
                "Sync wizard:\n- Choose a survey from inventory\n- Review detected dimension changes\n- Choose dimensions\n- Get a safe command to run"
            )
        elif idx == 1:
            detail.update(
                "Survey menu (TUI):\nBrowse survey operations.\n\nIncludes: Items structural edits (stage → preview → push)."
            )
        elif idx == 2:
            detail.update(
                "Content editors:\nInteractive editors that stage changes into the normal qsync pipeline.\n\nIncludes: SBS items structural edits."
            )
        elif idx == 3:
            detail.update("Help:\nKeyboard shortcuts and workflow notes.")
        elif idx == 4:
            detail.update("Exit the TUI.")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:  # type: ignore[name-defined]
        idx = getattr(event, "option_index", None)
        if idx == 0:
            self.app.push_screen("sync_survey")  # type: ignore[attr-defined]
        elif idx == 1:
            self.app.push_screen("survey_menu")  # type: ignore[attr-defined]
        elif idx == 2:
            self.app.push_screen("content_editors")  # type: ignore[attr-defined]
        elif idx == 3:
            self.app.push_screen("help")  # type: ignore[attr-defined]
        else:
            self.app.exit()


class ContentEditorsScreen(Screen):
    """Global TUI entry for interactive editors (stage → preview → push)."""

    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
        ("?", "app.push_screen('help')", "Help"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield OptionList(
                "Items structural editor (SBS-first)",
                "Embedded Data editor (SurveyFlow staged)",
                "← Back",
                id="menu",
            )
            yield Static("", id="detail")
        yield Footer()

    def on_mount(self) -> None:
        detail = self.query_one("#detail", Static)
        detail.update(
            "\n".join(
                [
                    "Content editors:",
                    "",
                    *_account_context_lines(),
                    "",
                    "These editors stage changes into the normal qsync pending pipeline.",
                ]
            )
        )

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:  # type: ignore[name-defined]
        idx = getattr(event, "option_index", None)
        detail = self.query_one("#detail", Static)
        ctx = _account_context_lines()
        if idx == 0:
            detail.update(
                "\n".join(
                    [
                        "Items structural editor:",
                        "- Edit QuestionText / Options / Subitems",
                        "- SBSMatrix: columns + per-column answers supported",
                        "- Stage → review → push + workbook patch offer",
                        "",
                        *ctx,
                    ]
                )
            )
        elif idx == 1:
            detail.update(
                "\n".join(
                    [
                        "Embedded Data editor:",
                        "- Add/remove/rename embedded data fields in SurveyFlow",
                        "- Staged into pending; push via normal pipeline",
                        "",
                        *ctx,
                    ]
                )
            )
        else:
            detail.update("\n".join(ctx))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:  # type: ignore[name-defined]
        idx = getattr(event, "option_index", None)
        if idx == 0:
            self.app.structural_state = StructuralEditState()  # type: ignore[attr-defined]
            self.app.push_screen("struct_survey")  # type: ignore[attr-defined]
            return
        if idx == 1:
            self.app.push_screen("embedded_editor")  # type: ignore[attr-defined]
            return
        self.app.pop_screen()  # type: ignore[attr-defined]


class EmbeddedDataEditorScreen(Screen):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
        ("?", "app.push_screen('help')", "Help"),
        ("r", "refresh", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield OptionList(id="menu")
            yield Static("", id="detail")
        yield Footer()

    def action_refresh(self) -> None:
        self.on_mount()

    def on_mount(self) -> None:
        menu = self.query_one("#menu", OptionList)
        menu.clear_options()
        detail = self.query_one("#detail", Static)

        menu.add_option("Select survey")
        menu.add_option("Add embedded field (stage)")
        menu.add_option("Remove embedded field (stage)")
        menu.add_option("Rename embedded field (stage)")
        menu.add_option("← Back")

        survey_id = getattr(self, "_survey_id", None)
        detail.update(
            "\n".join(
                [
                    "Embedded Data editor (staged):",
                    "",
                    f"Survey: {survey_id or '(none selected)'}",
                    "",
                    "Push using the normal pipeline (e.g. `qsync push`).",
                    "",
                    *_account_context_lines(),
                ]
            )
        )

    def _ensure_survey(self) -> str:
        survey_id = getattr(self, "_survey_id", None)
        if survey_id:
            return str(survey_id)
        from .survey_selection import pick_survey_id_from_api
        from qsync.config import get_client_config

        # Best-effort: use default env in this editor until account switching is moved into TUI.
        base, headers = get_client_config()
        picked = pick_survey_id_from_api(
            message="Select a survey for Embedded Data edits:",
            base_url=base,
            headers=headers,
            include_back=True,
        )
        if not picked:
            raise RuntimeError("Cancelled.")
        self._survey_id = picked
        return picked

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:  # type: ignore[name-defined]
        option = getattr(event, "option", None)
        if option is None:
            return
        choice = str(getattr(option, "prompt", "") or getattr(option, "text", "") or "")
        detail = self.query_one("#detail", Static)

        if choice.startswith("←"):
            self.app.pop_screen()  # type: ignore[attr-defined]
            return

        if choice.startswith("Select survey"):
            try:
                _ = self._ensure_survey()
            except Exception as exc:
                detail.update(f"ERROR: {exc}")
            self.on_mount()
            return

        try:
            survey_id = self._ensure_survey()
        except Exception as exc:
            detail.update(f"ERROR: {exc}")
            self.on_mount()
            return

        from qsync.interactive_menu import text_input, select_from_list
        from qsync.cli_survey import _merge_embedded_pending, _merge_embedded_rename_pending
        from qsync.sync_core import (
            stage_add_embedded_field,
            stage_remove_embedded_field,
            stage_rename_embedded_field,
        )

        if choice.startswith("Add embedded"):
            field = (text_input("Embedded field name") or "").strip()
            if not field:
                detail.update("Cancelled (field required).")
                return
            value = (text_input("Value (optional)", default="") or "").strip() or None
            with self.app.suspend():  # type: ignore[attr-defined]
                entry = stage_add_embedded_field(
                    survey_id,
                    field=field,
                    value=value,
                    flow_id=None,
                    dry_run=False,
                )
                _merge_embedded_pending(survey_id, [entry])
                print(f"[qsync:tui] Staged embedded field add: {field}")
            self.on_mount()
            return

        if choice.startswith("Remove embedded"):
            field = (text_input("Embedded field name to remove") or "").strip()
            if not field:
                detail.update("Cancelled (field required).")
                return
            with self.app.suspend():  # type: ignore[attr-defined]
                removed = stage_remove_embedded_field(
                    survey_id,
                    field=field,
                    flow_id=None,
                    dry_run=False,
                )
                if removed:
                    _merge_embedded_pending(survey_id, removed)
                    print(f"[qsync:tui] Staged embedded field removal: {field} (rows={len(removed)})")
                else:
                    print(f"[qsync:tui] No matching embedded field rows found for: {field}")
            self.on_mount()
            return

        if choice.startswith("Rename embedded"):
            old_field = (text_input("Rename from (old field)") or "").strip()
            if not old_field:
                detail.update("Cancelled (old field required).")
                return
            new_field = (text_input("Rename to (new field)") or "").strip()
            if not new_field:
                detail.update("Cancelled (new field required).")
                return
            all_occ = select_from_list("Apply to:", ["One occurrence (FlowID specific)", "All occurrences"]) == "All occurrences"
            flow_id = None
            if not all_occ:
                flow_id = (text_input("FlowID (required for one occurrence)") or "").strip() or None
                if not flow_id:
                    detail.update("Cancelled (FlowID required for one occurrence).")
                    return
            with self.app.suspend():  # type: ignore[attr-defined]
                renamed = stage_rename_embedded_field(
                    survey_id,
                    old_field=old_field,
                    new_field=new_field,
                    flow_id=flow_id,
                    all_occurrences=all_occ,
                    dry_run=False,
                )
                _merge_embedded_rename_pending(survey_id, renamed)
                print(f"[qsync:tui] Staged embedded field rename: {old_field} -> {new_field} (rows={len(renamed)})")
            self.on_mount()
            return


class SyncSurveyScreen(Screen):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
        ("?", "app.push_screen('help')", "Help"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical():
                yield Input(
                    placeholder="Filter surveys by ID or name (regex/text)",
                    id="survey_filter",
                )
                yield OptionList(id="surveys")
            yield Static(
                "Loading surveys from inventory...\n\nIf this is empty, run `qsync survey inventory` first.",
                id="detail",
            )
        yield Footer()

    def on_mount(self) -> None:
        surveys_widget = self.query_one("#surveys", OptionList)
        detail = self.query_one("#detail", Static)
        surveys_widget.clear_options()
        try:
            from qsync.survey_inventory import load_cached_inventory_records

            records = load_cached_inventory_records()
            focal = [r for r in records.values() if r.get("focal")]
            rows = focal or list(records.values())
            rows.sort(key=lambda r: (r.get("lastModified") or ""), reverse=True)
            if not rows:
                detail.update(
                    "No inventory records found.\n\nRun: qsync survey inventory"
                )
                return
            self._rows = rows[:400]
            self._filtered_rows = list(self._rows)
            self._apply_filter("")
            detail.update("Select a survey to continue.")
        except Exception as exc:
            detail.update(f"Failed to load inventory: {exc}\n\nRun: qsync survey inventory")

    def _apply_filter(self, query: str) -> None:
        rows = [r for r in getattr(self, "_rows", []) if _survey_filter_matches(r, query)]
        self._filtered_rows = rows
        surveys = self.query_one("#surveys", OptionList)
        surveys.clear_options()
        if not rows:
            return
        for row in rows:
            surveys.add_option(_survey_option_label(row))

    def on_input_changed(self, event: Input.Changed) -> None:  # type: ignore[name-defined]
        if str(getattr(event, "input", None).id or "") != "survey_filter":
            return
        self._apply_filter(str(getattr(event, "value", "")))

    def on_input_submitted(self, event: Input.Submitted) -> None:  # type: ignore[name-defined]
        if str(getattr(event, "input", None).id or "") != "survey_filter":
            return
        rows = getattr(self, "_filtered_rows", [])
        if len(rows) == 1:
            self._select_survey(rows[0])
        else:
            option_list = self.query_one("#surveys", OptionList)
            if len(rows) > 1:
                option_list.focus()

    def on_key(self, event) -> None:  # type: ignore[override]
        if (
            str(getattr(event, "key", "")) == "down"
            and str(getattr(self.app.focused, "id", "")) == "survey_filter"
        ):
            filtered = getattr(self, "_filtered_rows", [])
            if filtered:
                event.prevent_default()
                self.query_one("#surveys", OptionList).focus()
                return

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:  # type: ignore[name-defined]
        idx = getattr(event, "option_index", None)
        rows = getattr(self, "_filtered_rows", None)
        if idx is None or not rows or idx < 0 or idx >= len(rows):
            return
        r = rows[idx]
        detail = self.query_one("#detail", Static)
        sid = str(r.get("id") or "").strip()
        name = str(r.get("name") or "Untitled").strip()
        locked = r.get("locked")
        active = r.get("isActive")
        stage = r.get("stage")
        component = r.get("component")
        last_mod = r.get("lastModified") or r.get("lastModifiedDate")
        preview = r.get("preview_count")
        resp = r.get("response_count")
        detail.update(
            "\n".join(
                [
                    f"Survey: {sid}",
                    f"Name: {name}",
                    "",
                    f"Active: {active}",
                    f"Locked: {locked}",
                    f"Stage: {stage}",
                    f"Component: {component}",
                    "",
                    f"Last modified: {last_mod}",
                    f"Preview: {preview}  Responses: {resp}",
                    "",
                    "Enter to select. b/Esc to go back. ? for help.",
                ]
            )
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:  # type: ignore[name-defined]
        idx = getattr(event, "option_index", None)
        rows = getattr(self, "_filtered_rows", None)
        if idx is None or rows is None or idx < 0 or idx >= len(rows):
            return
        self._select_survey(rows[idx])

    def _select_survey(self, row: dict[str, Any]) -> None:
        sid = str(row.get("id") or "").strip()
        if not sid:
            return
        state: SyncWizardState = self.app.sync_state  # type: ignore[attr-defined]
        state.survey_id = sid
        state.survey_name = str(row.get("name") or "").strip() or None
        self.app.push_screen("sync_dims")  # type: ignore[attr-defined]


class SyncDimensionsScreen(Screen):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
        ("?", "app.push_screen('help')", "Help"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield OptionList(id="dims")
            yield Static("Loading change detection...", id="detail")
        yield Footer()

    def on_mount(self) -> None:
        dims = self.query_one("#dims", OptionList)
        detail = self.query_one("#detail", Static)
        state: SyncWizardState = self.app.sync_state  # type: ignore[attr-defined]
        if not state.survey_id:
            detail.update("No survey selected.")
            return
        try:
            from qsync.sync_orchestrator import detect_survey_changes

            changes = detect_survey_changes(state.survey_id)
            changed = list(changes.changed_dimensions)
            self._changes = changes
            self._changed = changed
            state.dimensions = None

            if not changed:
                detail.update("No changes detected for this survey.")
                dims.add_option("← Back")
                return

            detail.update(
                "\n".join(
                    [
                        f"Survey: {state.survey_id}",
                        f"Changed dimensions: {', '.join(changed)}",
                        "",
                        "Select one dimension, or choose 'All changed dimensions'.",
                    ]
                )
            )
            for d in changed:
                dims.add_option(d)
            dims.add_option("All changed dimensions")
            dims.add_option("← Back")
        except Exception as exc:
            detail.update(f"Failed to detect changes: {exc}")
            dims.add_option("← Back")

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:  # type: ignore[name-defined]
        option = getattr(event, "option", None)
        if option is None:
            return
        text = str(getattr(option, "prompt", "") or getattr(option, "text", "") or "")
        detail = self.query_one("#detail", Static)
        state: SyncWizardState = self.app.sync_state  # type: ignore[attr-defined]

        if text in {"← Back", "Back"}:
            detail.update("Go back to survey selection.\n\nb/Esc also works.")
            return
        if text == "All changed dimensions":
            changed = getattr(self, "_changed", None) or []
            detail.update(
                "\n".join(
                    [
                        f"Survey: {state.survey_id}",
                        "",
                        f"Will sync: {', '.join(changed) if changed else '(none)'}",
                    ]
                )
            )
            return

        changes = getattr(self, "_changes", None)
        if changes is None:
            return
        dim = text
        info = changes.dimensions.get(dim)
        if info is None:
            return
        affected = len(getattr(info, "affected_qids", []) or [])
        detail.update(
            "\n".join(
                [
                    f"Survey: {state.survey_id}",
                    f"Dimension: {dim}",
                    "",
                    f"Summary: {getattr(info, 'change_summary', '')}",
                    f"Status: {getattr(info, 'status_kind', '')}",
                    f"Affected QIDs: {affected}",
                    "",
                    f"Error: {getattr(info, 'error_detail', None) or '(none)'}",
                    f"Warning: {getattr(info, 'warning_detail', None) or '(none)'}",
                ]
            )
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:  # type: ignore[name-defined]
        option = getattr(event, "option", None)
        if option is None:
            return
        text = str(getattr(option, "prompt", "") or getattr(option, "text", "") or "")
        state: SyncWizardState = self.app.sync_state  # type: ignore[attr-defined]
        if text in {"← Back", "Back"}:
            self.app.pop_screen()  # type: ignore[attr-defined]
            return
        if text == "All changed dimensions":
            # Re-detect to ensure consistent results.
            from qsync.sync_orchestrator import detect_survey_changes

            changes = detect_survey_changes(state.survey_id or "")
            state.dimensions = list(changes.changed_dimensions)
        else:
            state.dimensions = [text]
        self.app.push_screen("sync_confirm")  # type: ignore[attr-defined]


class SyncConfirmScreen(Screen):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
        ("?", "app.push_screen('help')", "Help"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="detail")
        yield Footer()

    def on_mount(self) -> None:
        detail = self.query_one("#detail", Static)
        state: SyncWizardState = self.app.sync_state  # type: ignore[attr-defined]
        cmd = state.command() or "qsync sync"
        dims = ", ".join(state.dimensions or []) if state.dimensions else "(default)"
        detail.update(
            "\n".join(
                [
                    "Ready.",
                    "",
                    f"Survey: {state.survey_id or '(none)'}",
                    f"Dimensions: {dims}",
                    "",
                    "Safe next step (run in your shell):",
                    cmd,
                    "",
                    "Press b/Esc to go back, or q to quit.",
                ]
            )
        )


class QsyncTuiApp(App):
    """qsync TUI app (pilot)."""

    CSS = """
    Screen { padding: 1; }
    #menu, #surveys, #dims, #actions { width: 50%; }
    #survey_filter { width: 100%; }
    #detail { width: 50%; padding-left: 2; }
    #help_body { padding: 1; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("?", "push_screen('help')", "Help"),
    ]

    def __init__(self, *, start_screen: str | None = None) -> None:
        super().__init__()
        self.start_screen = start_screen
        self.sync_state = SyncWizardState()
        self.structural_state = StructuralEditState()

    def on_mount(self) -> None:
        self.install_screen(HelpScreen(), name="help")
        self.install_screen(MainMenuScreen(), name="main")
        self.install_screen(ContentEditorsScreen(), name="content_editors")
        self.install_screen(EmbeddedDataEditorScreen(), name="embedded_editor")
        self.install_screen(SyncSurveyScreen(), name="sync_survey")
        self.install_screen(SyncDimensionsScreen(), name="sync_dims")
        self.install_screen(SyncConfirmScreen(), name="sync_confirm")
        self.install_screen(SurveyMenuScreen(), name="survey_menu")
        self.install_screen(PullSurveyScreen(), name="pull_survey")
        self.install_screen(StructuralSurveyScreen(), name="struct_survey")
        self.install_screen(StructuralSessionScreen(), name="struct_session")
        self.install_screen(StructuralQidScreen(), name="struct_qid")
        self.install_screen(StructuralSurfaceScreen(), name="struct_surface")
        self.install_screen(StructuralActionScreen(), name="struct_action")
        self.install_screen(StructuralTextEditScreen(), name="struct_text")
        self.install_screen(StructuralReviewScreen(), name="struct_review")

        if self.start_screen == "sync":
            self.push_screen("sync_survey")
        elif self.start_screen == "survey_menu":
            self.push_screen("survey_menu")
        else:
            self.push_screen("main")


@dataclass
class StructuralEditState:
    survey_id: str | None = None
    survey_name: str | None = None
    qid: str | None = None
    surface: str | None = None  # question_text|options|subitems|sbs_columns|sbs_column_answers
    action: str | None = None  # add|edit|remove (mostly)
    item_id: str | None = None  # ChoiceId/AnswerId/ColumnId depending on surface
    column_id: str | None = None  # for SBS column answers
    ops: list[dict] = field(default_factory=list)


class SurveyMenuScreen(Screen):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
        ("?", "app.push_screen('help')", "Help"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield OptionList(
                "Pull survey definition (cache JSON)",
                "Refresh inventory (update surveys/inventory.csv)",
                "List surveys (API top 30)",
                "Items: structural edits (stage → preview → push)",
                "← Back",
                id="menu",
            )
            yield Static(
                "Survey menu (TUI).\n\nSelect an action.\n\nNote: account switching remains in the CLI survey menu for now.",
                id="detail",
            )
        yield Footer()

    def on_mount(self) -> None:
        detail = self.query_one("#detail", Static)
        detail.update(
            "\n".join(
                [
                    "Survey menu (TUI).",
                    "",
                    *_account_context_lines(),
                    "",
                    "Select an action.",
                    "",
                    "Note: account switching remains in the CLI survey menu for now.",
                ]
            )
        )

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:  # type: ignore[name-defined]
        detail = self.query_one("#detail", Static)
        idx = getattr(event, "option_index", None)
        ctx = _account_context_lines()
        if idx == 0:
            detail.update(
                "\n".join(
                    [
                        "Pull survey definition:",
                        "- Uses live API list to pick a survey",
                        "- Writes JSON to surveys/ (or surveys/.<account>/ if active account set)",
                        "",
                        *ctx,
                    ]
                )
            )
        elif idx == 1:
            detail.update(
                "\n".join(
                    [
                        "Refresh inventory:",
                        "- Fetches surveys via API",
                        "- Updates surveys/inventory.csv",
                        "",
                        *ctx,
                    ]
                )
            )
        elif idx == 2:
            detail.update(
                "\n".join(
                    [
                        "List surveys:",
                        "- Fetches surveys via API",
                        "- Shows top 30 in the right pane",
                        "",
                        *ctx,
                    ]
                )
            )
        elif idx == 3:
            detail.update(
                "\n".join(
                    [
                        "Items structural edits:",
                        "- Stage → review → push (uses existing CLI wizard in a suspended terminal)",
                        "",
                        *ctx,
                    ]
                )
            )
        else:
            detail.update("\n".join(ctx))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:  # type: ignore[name-defined]
        idx = getattr(event, "option_index", None)
        if idx == 0:
            self.app.push_screen("pull_survey")  # type: ignore[attr-defined]
            return
        if idx == 1:
            detail = self.query_one("#detail", Static)
            try:
                from pathlib import Path

                from qsync.config import (
                    get_active_account,
                    get_client_config,
                    load_account_env,
                    resolve_root,
                )
                from qsync.survey_inventory import refresh_inventory

                root = resolve_root(required=False) or Path.cwd()
                account = None
                try:
                    account = get_active_account()
                except Exception:
                    account = None
                env = load_account_env(account, root=root) if account else None
                base, headers = get_client_config(env) if env else get_client_config()

                with self.app.suspend():  # type: ignore[attr-defined]
                    print(f"\n[qsync:tui] Refreshing inventory (account={account or 'default'})...")
                    _all, changed = refresh_inventory(
                        base,
                        headers,
                        progress=True,
                        quiet=False,
                    )
                    print(f"[qsync:tui] Inventory refresh done. Changed={len(changed)}")
                detail.update(
                    "\n".join(
                        [
                            "Inventory refreshed.",
                            "",
                            *_account_context_lines(),
                            "",
                            "Select another action on the left.",
                        ]
                    )
                )
            except Exception as exc:
                detail.update(f"ERROR refreshing inventory: {exc}\n\n" + "\n".join(_account_context_lines()))
            return
        if idx == 2:
            detail = self.query_one("#detail", Static)
            try:
                from pathlib import Path

                from qsync.config import (
                    get_active_account,
                    get_client_config,
                    load_account_env,
                    resolve_root,
                )
                from qsync.survey_selection import list_surveys_via_api

                root = resolve_root(required=False) or Path.cwd()
                account = None
                try:
                    account = get_active_account()
                except Exception:
                    account = None
                env = load_account_env(account, root=root) if account else None
                base, headers = get_client_config(env) if env else get_client_config()

                surveys = list_surveys_via_api(base_url=base, headers=headers)
                surveys.sort(key=lambda s: (s.get("lastModified") or s.get("creationDate") or ""), reverse=True)
                head = surveys[:30]
                lines = ["Surveys (API top 30):", ""]
                for s in head:
                    sid = str(s.get("id") or "").strip()
                    name = str(s.get("name") or "Untitled").strip()
                    active = s.get("isActive")
                    lines.append(f"- {sid} [{'active' if active else 'inactive'}] {name}")
                if len(surveys) > 30:
                    lines.append("")
                    lines.append(f"(Showing 30 of {len(surveys)}.)")
                lines.append("")
                lines.extend(_account_context_lines())
                detail.update("\n".join(lines))
            except Exception as exc:
                detail.update(f"ERROR listing surveys: {exc}\n\n" + "\n".join(_account_context_lines()))
            return
        if idx == 3:
            self.app.structural_state = StructuralEditState()  # type: ignore[attr-defined]
            self.app.push_screen("struct_survey")  # type: ignore[attr-defined]
            return
        else:
            self.app.pop_screen()  # type: ignore[attr-defined]


class PullSurveyScreen(Screen):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
        ("?", "app.push_screen('help')", "Help"),
        ("r", "refresh", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical():
                yield Input(
                    placeholder="Filter surveys by ID or name (regex/text)",
                    id="survey_filter",
                )
                yield OptionList(id="surveys")
            yield Static("Loading surveys from API...", id="detail")
        yield Footer()

    def action_refresh(self) -> None:
        self.on_mount()

    def on_mount(self) -> None:
        surveys_widget = self.query_one("#surveys", OptionList)
        surveys_widget.clear_options()
        detail = self.query_one("#detail", Static)
        detail.update("\n".join(["Loading surveys from API...", "", *_account_context_lines()]))

        try:
            from pathlib import Path

            from qsync.config import (
                get_active_account,
                get_client_config,
                load_account_env,
                resolve_root,
            )
            from qsync.survey_selection import list_surveys_via_api

            root = resolve_root(required=False) or Path.cwd()
            account = None
            try:
                account = get_active_account()
            except Exception:
                account = None
            env = load_account_env(account, root=root) if account else None
            base, headers = get_client_config(env) if env else get_client_config()

            surveys = list_surveys_via_api(base_url=base, headers=headers)
            surveys.sort(key=lambda s: (s.get("lastModified") or s.get("creationDate") or ""), reverse=True)
            self._rows = surveys[:400]
            self._filtered_rows = list(self._rows)
            self._apply_filter("")

            detail.update(
                "\n".join(
                    [
                        "Select a survey to pull (cache JSON).",
                        "",
                        *_account_context_lines(),
                        "",
                        "Writes to surveys/ (or surveys/.<account>/ if active account set).",
                    ]
                )
            )
        except Exception as exc:
            detail.update(f"Failed to list surveys via API: {exc}\n\n" + "\n".join(_account_context_lines()))

    def _apply_filter(self, query: str) -> None:
        rows = [r for r in getattr(self, "_rows", []) if _survey_filter_matches(r, query)]
        self._filtered_rows = rows
        surveys_widget = self.query_one("#surveys", OptionList)
        surveys_widget.clear_options()
        if not rows:
            return
        for row in rows:
            sid = str(row.get("id") or "").strip()
            if not sid:
                continue
            surveys_widget.add_option(_survey_option_label(row))

    def on_input_changed(self, event: Input.Changed) -> None:  # type: ignore[name-defined]
        if str(getattr(event, "input", None).id or "") != "survey_filter":
            return
        self._apply_filter(str(getattr(event, "value", "")))

    def on_input_submitted(self, event: Input.Submitted) -> None:  # type: ignore[name-defined]
        if str(getattr(event, "input", None).id or "") != "survey_filter":
            return
        rows = getattr(self, "_filtered_rows", [])
        if len(rows) == 1:
            self._select_survey(rows[0])
        else:
            option_list = self.query_one("#surveys", OptionList)
            if len(rows) > 1:
                option_list.focus()

    def on_key(self, event) -> None:  # type: ignore[override]
        if (
            str(getattr(event, "key", "")) == "down"
            and str(getattr(self.app.focused, "id", "")) == "survey_filter"
        ):
            filtered = getattr(self, "_filtered_rows", [])
            if filtered:
                event.prevent_default()
                self.query_one("#surveys", OptionList).focus()
                return

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:  # type: ignore[name-defined]
        idx = getattr(event, "option_index", None)
        rows = getattr(self, "_filtered_rows", None)
        if idx is None or not rows or idx < 0 or idx >= len(rows):
            return
        s = rows[idx]
        detail = self.query_one("#detail", Static)
        sid = str(s.get("id") or "").strip()
        name = str(s.get("name") or "Untitled").strip()
        last_mod = str(s.get("lastModified") or s.get("creationDate") or "").strip()
        active = s.get("isActive")
        detail.update(
            "\n".join(
                [
                    "Pull survey definition (cache JSON)",
                    "",
                    f"Survey: {sid}",
                    f"Name: {name}",
                    f"Active: {'yes' if active else 'no'}",
                    f"Last modified: {last_mod or '-'}",
                    "",
                    *_account_context_lines(),
                    "",
                    "Enter to pull. b/Esc to go back.",
                ]
            )
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:  # type: ignore[name-defined]
        idx = getattr(event, "option_index", None)
        rows = getattr(self, "_filtered_rows", None)
        if idx is None or rows is None or idx < 0 or idx >= len(rows):
            return
        self._select_survey(rows[idx])

    def _select_survey(self, row: dict[str, Any]) -> None:
        survey_id = str(row.get("id") or "").strip()
        if not survey_id:
            return

        try:
            from pathlib import Path

            from qsync.config import (
                get_active_account,
                get_client_config,
                load_account_env,
                resolve_root,
                resolve_scoped_dir,
            )
            from qsync.qualtrics_client import download_survey_definition

            root = resolve_root(required=False) or Path.cwd()
            account = None
            try:
                account = get_active_account()
            except Exception:
                account = None
            env = load_account_env(account, root=root) if account else None
            dest = resolve_scoped_dir("surveys", root=root, account=account) if account else resolve_scoped_dir("surveys", root=root)

            with self.app.suspend():  # type: ignore[attr-defined]
                print(f"\n[qsync:tui] Pulling survey definition {survey_id}...")
                saved = download_survey_definition(survey_id, target_dir=dest, env=env)
                print(f"[qsync:tui] Saved to: {saved}")
        except Exception as exc:
            detail = self.query_one("#detail", Static)
            detail.update(f"ERROR pulling survey: {exc}\n\n" + "\n".join(_account_context_lines()))
            return

        self.app.pop_screen()  # type: ignore[attr-defined]


class StructuralSurveyScreen(Screen):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
        ("?", "app.push_screen('help')", "Help"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical():
                yield Input(
                    placeholder="Filter surveys by ID or name (regex/text)",
                    id="survey_filter",
                )
                yield OptionList(id="surveys")
            yield Static(
                "Loading surveys from inventory...\n\nIf empty, run `qsync survey inventory` first.",
                id="detail",
            )
        yield Footer()

    def on_mount(self) -> None:
        surveys = self.query_one("#surveys", OptionList)
        detail = self.query_one("#detail", Static)
        surveys.clear_options()
        try:
            from qsync.survey_inventory import load_cached_inventory_records

            records = load_cached_inventory_records()
            focal = [r for r in records.values() if r.get("focal")]
            rows = focal or list(records.values())
            rows.sort(key=lambda r: (r.get("lastModified") or ""), reverse=True)
            if not rows:
                detail.update("No inventory records found.\n\nRun: qsync survey inventory")
                return
            self._rows = rows[:400]
            self._filtered_rows = list(self._rows)
            self._apply_filter("")
            detail.update("Select a survey for structural edits.")
        except Exception as exc:
            detail.update(f"Failed to load inventory: {exc}\n\nRun: qsync survey inventory")

    def _apply_filter(self, query: str) -> None:
        rows = [r for r in getattr(self, "_rows", []) if _survey_filter_matches(r, query)]
        self._filtered_rows = rows
        surveys = self.query_one("#surveys", OptionList)
        surveys.clear_options()
        if not rows:
            return
        for row in rows:
            surveys.add_option(_survey_option_label(row))

    def on_input_changed(self, event: Input.Changed) -> None:  # type: ignore[name-defined]
        if str(getattr(event, "input", None).id or "") != "survey_filter":
            return
        self._apply_filter(str(getattr(event, "value", "")))

    def on_input_submitted(self, event: Input.Submitted) -> None:  # type: ignore[name-defined]
        if str(getattr(event, "input", None).id or "") != "survey_filter":
            return
        rows = getattr(self, "_filtered_rows", [])
        if len(rows) == 1:
            self._select_survey(rows[0])
        else:
            option_list = self.query_one("#surveys", OptionList)
            if len(rows) > 1:
                option_list.focus()

    def on_key(self, event) -> None:  # type: ignore[override]
        if (
            str(getattr(event, "key", "")) == "down"
            and str(getattr(self.app.focused, "id", "")) == "survey_filter"
        ):
            filtered = getattr(self, "_filtered_rows", [])
            if filtered:
                event.prevent_default()
                self.query_one("#surveys", OptionList).focus()
                return

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:  # type: ignore[name-defined]
        idx = getattr(event, "option_index", None)
        rows = getattr(self, "_filtered_rows", None)
        if idx is None or not rows or idx < 0 or idx >= len(rows):
            return
        r = rows[idx]
        detail = self.query_one("#detail", Static)
        sid = str(r.get("id") or "").strip()
        name = str(r.get("name") or "Untitled").strip()
        detail.update(
            "\n".join(
                [
                    _format_ops_summary(getattr(self.app.structural_state, "ops", [])),  # type: ignore[attr-defined]
                    "",
                    f"Survey: {sid}",
                    f"Name: {name}",
                    "",
                    "Enter to select. b/Esc to go back.",
                ]
            )
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:  # type: ignore[name-defined]
        idx = getattr(event, "option_index", None)
        rows = getattr(self, "_filtered_rows", None)
        if idx is None or rows is None or idx < 0 or idx >= len(rows):
            return
        self._select_survey(rows[idx])

    def _select_survey(self, row: dict[str, Any]) -> None:
        sid = str(row.get("id") or "").strip()
        if not sid:
            return
        state: StructuralEditState = self.app.structural_state  # type: ignore[attr-defined]
        state.survey_id = sid
        state.survey_name = str(row.get("name") or "").strip() or None
        self.app.push_screen("struct_session")  # type: ignore[attr-defined]


class StructuralSessionScreen(Screen):
    """TUI wrapper around the structural edit workflow.

    The editor itself runs in a suspended terminal session using the existing
    `interactive_choice_wizard` (questionary-based). The TUI provides:
    - a persistent staged-ops summary panel (right pane)
    - stage → review → push/revert/abort gating
    """

    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
        ("?", "app.push_screen('help')", "Help"),
        ("r", "refresh", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield OptionList(id="actions")
            yield Static("", id="detail")
        yield Footer()

    def action_refresh(self) -> None:
        self.on_mount()

    def on_mount(self) -> None:
        actions = self.query_one("#actions", OptionList)
        actions.clear_options()
        detail = self.query_one("#detail", Static)
        state: StructuralEditState = self.app.structural_state  # type: ignore[attr-defined]
        if not state.survey_id:
            actions.add_option("← Back")
            detail.update("No survey selected.")
            return

        # Load staged ops from pending (if present) to make the session resumable.
        try:
            from qsync.pending_stage import load_pending, ItemsPendingPayload

            record = load_pending(state.survey_id, "items")
            if record and isinstance(record.payload, ItemsPendingPayload):
                state.ops = list(record.payload.structural_ops or [])
        except Exception:
            pass

        actions.add_option("➕ Add another edit (opens CLI wizard)")
        actions.add_option("🔍 Review staged diffs")
        actions.add_option("🚀 Push staged edits now")
        actions.add_option("🧹 Revert staged edits (clear pending)")
        actions.add_option("⏸ Abort (leave staged pending)")
        actions.add_option("← Back")

        detail.update(
            "\n".join(
                [
                    _format_ops_summary(state.ops),
                    "",
                    f"Survey: {state.survey_id}",
                    "",
                    "Notes:",
                    "- Edits are staged into the normal qsync pending pipeline.",
                    "- Push will use existing qsync safeguards (may block on policy).",
                    "",
                    "Tip: press r to refresh pending ops from disk.",
                ]
            )
        )

    def _append_op_to_pending(self, *, survey_id: str, op: dict) -> None:
        from qsync.pending_stage import (
            ItemsPendingPayload,
            PendingStagedChanges,
            load_pending,
            save_pending,
        )
        from qsync.workbook_resolver import WorkbookResolver
        from qsync.dimensions.items_structural import summarize_structural_ops

        resolver = WorkbookResolver()
        xlsx_path = resolver.resolve(survey_id)

        record = load_pending(survey_id, "items")
        if not record or not isinstance(record.payload, ItemsPendingPayload):
            payload = ItemsPendingPayload(
                qids=[],
                workbook=str(xlsx_path) if xlsx_path.exists() else None,
                structural_ops=[],
                structural_summary={},
                push_journal={},
                changes=[],
                embedded_fields=[],
            )
            record = PendingStagedChanges(survey_id=survey_id, dimension="items", payload=payload)

        payload = record.payload
        assert isinstance(payload, ItemsPendingPayload)

        ops = list(payload.structural_ops or [])
        ops.append(dict(op))
        payload.structural_ops = ops
        payload.structural_summary = summarize_structural_ops(ops)
        qid = str(op.get("qid") or "").strip()
        if qid and qid not in (payload.qids or []):
            payload.qids = list(dict.fromkeys([*(payload.qids or []), qid]))
        if xlsx_path.exists():
            payload.workbook = str(xlsx_path)
        save_pending(record)

    def _clear_pending_structural(self, *, survey_id: str) -> None:
        from qsync.pending_stage import load_pending, save_pending, ItemsPendingPayload
        from qsync.qualtrics_client import refresh_survey_cache

        record = load_pending(survey_id, "items")
        if record and isinstance(record.payload, ItemsPendingPayload):
            record.payload.structural_ops = []
            record.payload.structural_summary = {}
            record.payload.push_journal = {}
            save_pending(record)
        try:
            refresh_survey_cache(survey_id)
        except Exception:
            pass

    def _review_diffs_text(self, ops: list[dict]) -> str:
        import difflib

        if not ops:
            return "No staged ops."
        lines: list[str] = []
        for op in ops[:12]:
            qid = str(op.get("qid") or "").strip()
            op_type = str(op.get("op") or "").strip()
            surface = str(op.get("surface") or op.get("target") or "").strip()
            cid = str(op.get("choice_id") or "").strip()
            aid = str(op.get("answer_id") or "").strip()
            label = f"{op_type} qid={qid}"
            if surface:
                label += f" surface={surface}"
            if cid:
                label += f" id={cid}"
            if aid:
                label += f"/{aid}"
            lines.append("-" * 60)
            lines.append(label)
            before = str(op.get("prev_html") or "")
            after = str(op.get("html") or "")
            if before or after:
                diff = list(
                    difflib.unified_diff(
                        before.splitlines(),
                        after.splitlines(),
                        fromfile="cache",
                        tofile="staged",
                        lineterm="",
                    )
                )
                for d in diff[:80]:
                    lines.append(d)
                if len(diff) > 80:
                    lines.append("... (diff truncated)")
        if len(ops) > 12:
            lines.append("-" * 60)
            lines.append(f"(Showing first 12 ops; total {len(ops)}.)")
        return "\n".join(lines)

    def _push_now(self, *, survey_id: str) -> str:
        """Push staged ops in a suspended terminal session to avoid corrupting TUI output."""

        from qsync.pending_stage import load_pending, ItemsPendingPayload, save_pending
        from qsync.qualtrics_client import load_cached_survey, refresh_survey_cache
        from qsync.dimensions.items_structural import push_structural_ops

        record = load_pending(survey_id, "items")
        if not record or not isinstance(record.payload, ItemsPendingPayload):
            return "No pending items record found."
        ops = list(record.payload.structural_ops or [])
        if not ops:
            return "No staged structural ops to push."

        # Ensure cache is present and reasonably fresh.
        refresh_survey_cache(survey_id)
        survey = load_cached_survey(survey_id)

        def _save_journal(journal: dict) -> None:
            record.payload.push_journal = dict(journal)
            save_pending(record)

        publish = True
        with self.app.suspend():  # type: ignore[attr-defined]
            # Keep delete policy strict: if deletes are present, push_structural_ops will
            # prompt (since interactive=True) before proceeding.
	            push_structural_ops(
	                survey_id=survey_id,
	                payload=survey.payload,
	                structural_ops=ops,
	                push_journal=dict(record.payload.push_journal or {}),
	                interactive=True,
	                allow_delete=False,
	                force_live=False,
	                force_preview=False,
	                publish=publish,
	                dry_run=False,
	                refresh_cache=True,
	                save_journal_cb=_save_journal,
	            )

        return f"Pushed {len(ops)} staged op(s)."

    def _offer_workbook_patch(self, *, survey_id: str, ops: list[dict]) -> str:
        from qsync.workbook_resolver import WorkbookResolver
        from qsync.dimensions.items_structural import _wipe_workbook_qid_cells  # type: ignore

        resolver = WorkbookResolver()
        xlsx_path = resolver.resolve(survey_id)
        if not xlsx_path.exists():
            return "No workbook found; skipping workbook patch."
        qids = sorted({str(op.get("qid") or "").strip() for op in ops if op.get("qid")})
        if not qids:
            return "No affected QIDs."
        with self.app.suspend():  # type: ignore[attr-defined]
            print("\n[qsync:tui] Workbook patch (dry run):")
            for q in qids:
                notes = _wipe_workbook_qid_cells(survey_id=survey_id, qid=q, dry_run=True)
                for n in notes[:6]:
                    print("  -", n)
            typed = input("\nType 'patch' to apply workbook patch (or Enter to skip): ").strip()
            if typed != "patch":
                print("[qsync:tui] Skipped workbook patch.")
                return "Workbook patch skipped."
            for q in qids:
                _wipe_workbook_qid_cells(survey_id=survey_id, qid=q, dry_run=False)
            print("[qsync:tui] Workbook patched.")
        return f"Workbook patched for {len(qids)} QID(s)."

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:  # type: ignore[name-defined]
        option = getattr(event, "option", None)
        if option is None:
            return
        choice = str(getattr(option, "prompt", "") or getattr(option, "text", "") or "")
        state: StructuralEditState = self.app.structural_state  # type: ignore[attr-defined]
        survey_id = state.survey_id or ""
        detail = self.query_one("#detail", Static)

        if choice.startswith("←"):
            self.app.pop_screen()  # type: ignore[attr-defined]
            return

        if choice.startswith("➕"):
            try:
                from qsync.dimensions.items_structural import interactive_choice_wizard

                with self.app.suspend():  # type: ignore[attr-defined]
                    prev_questionary = os.environ.get("QSYNC_USE_QUESTIONARY")
                    # Textual runs its own event loop; force plain fallback prompts
                    # for the suspended CLI wizard to avoid asyncio/questionary clashes.
                    os.environ["QSYNC_USE_QUESTIONARY"] = "0"
                    try:
                        op = interactive_choice_wizard(
                            survey_id=survey_id,
                            qid=None,
                            allow_delete=False,
                            experimental_unsupported=False,
                        )
                    finally:
                        if prev_questionary is None:
                            os.environ.pop("QSYNC_USE_QUESTIONARY", None)
                        else:
                            os.environ["QSYNC_USE_QUESTIONARY"] = prev_questionary
                if op:
                    self._append_op_to_pending(survey_id=survey_id, op=op)
            except Exception as exc:
                detail.update(f"{_format_ops_summary(state.ops)}\n\nERROR: {exc}")
            self.on_mount()
            return

        if choice.startswith("🔍"):
            # Reload ops then show a readable diff excerpt in-pane.
            try:
                from qsync.pending_stage import load_pending, ItemsPendingPayload

                record = load_pending(survey_id, "items")
                if record and isinstance(record.payload, ItemsPendingPayload):
                    state.ops = list(record.payload.structural_ops or [])
            except Exception:
                pass
            detail.update(
                "\n".join(
                    [
                        _format_ops_summary(state.ops),
                        "",
                        self._review_diffs_text(state.ops),
                        "",
                        "Select another action on the left.",
                    ]
                )
            )
            return

        if choice.startswith("🧹"):
            self._clear_pending_structural(survey_id=survey_id)
            state.ops = []
            self.on_mount()
            return

        if choice.startswith("⏸"):
            # Leave pending as-is.
            self.app.pop_screen()  # type: ignore[attr-defined]
            return

        if choice.startswith("🚀"):
            # Push in suspended terminal mode.
            try:
                from qsync.pending_stage import load_pending, ItemsPendingPayload

                record = load_pending(survey_id, "items")
                ops: list[dict] = []
                if record and isinstance(record.payload, ItemsPendingPayload):
                    ops = list(record.payload.structural_ops or [])
                msg = self._push_now(survey_id=survey_id)
                # Offer workbook patch after push.
                patch_msg = self._offer_workbook_patch(survey_id=survey_id, ops=ops)
                # Clear staged ops after push.
                self._clear_pending_structural(survey_id=survey_id)
                state.ops = []
                detail.update(f"{msg}\n{patch_msg}")
            except Exception as exc:
                detail.update(f"{_format_ops_summary(state.ops)}\n\nERROR pushing: {exc}")
            self.on_mount()
            return


class StructuralQidScreen(Screen):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
        ("?", "app.push_screen('help')", "Help"),
        ("r", "refresh", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield OptionList(id="qids")
            yield Static("Loading QIDs...", id="detail")
        yield Footer()

    def action_refresh(self) -> None:
        self.on_mount()

    def on_mount(self) -> None:
        qids = self.query_one("#qids", OptionList)
        qids.clear_options()
        detail = self.query_one("#detail", Static)
        state: StructuralEditState = self.app.structural_state  # type: ignore[attr-defined]
        if not state.survey_id:
            detail.update("No survey selected.")
            return
        try:
            from qsync.qualtrics_client import refresh_survey_cache, load_cached_survey
            from qsync.dimensions.items_structural import (
                _format_qid_label,
                iter_active_qids_in_flow,
                iter_all_qids,
            )

            refresh_survey_cache(state.survey_id)
            survey = load_cached_survey(state.survey_id)
            active = list(iter_active_qids_in_flow(survey))
            self._survey = survey
            self._active_set = set(active)
            self._label_to_qid = {}
            rows = active[:300]
            for qid in rows:
                label = _format_qid_label(
                    survey=survey,
                    qid=qid,
                    active_set=self._active_set,
                    include_flow_status=False,
                )
                self._label_to_qid[label] = qid
                qids.add_option(label)
            qids.add_option("Show all questions")
            qids.add_option("← Back")
            detail.update(
                "\n".join(
                    [
                        _format_ops_summary(state.ops),
                        "",
                        f"Survey: {state.survey_id}",
                        "Select a QID to edit.",
                        "",
                        "Tip: press r to refresh cache + list.",
                    ]
                )
            )
        except Exception as exc:
            detail.update(f"Failed to load survey/QIDs: {exc}")
            qids.add_option("← Back")

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:  # type: ignore[name-defined]
        option = getattr(event, "option", None)
        if option is None:
            return
        text = str(getattr(option, "prompt", "") or getattr(option, "text", "") or "")
        state: StructuralEditState = self.app.structural_state  # type: ignore[attr-defined]
        detail = self.query_one("#detail", Static)
        if text in {"Show all questions", "← Back"}:
            detail.update(_format_ops_summary(state.ops))
            return
        survey = getattr(self, "_survey", None)
        if survey is None:
            return
        label_to_qid = getattr(self, "_label_to_qid", {}) or {}
        qid = str(label_to_qid.get(text) or text.split()[0] or "").strip()
        q = survey.questions.get(qid) or {}
        tag = str((q.get("DataExportTag") or "")).strip()
        qtype = str((q.get("QuestionType") or "")).strip()
        sel = str((q.get("Selector") or "")).strip()
        preview = str(q.get("QuestionText") or "")
        preview = re.sub(r"<[^>]+>", " ", preview)
        preview = " ".join(preview.split())
        if len(preview) > 140:
            preview = preview[:139].rstrip() + "…"
        detail.update(
            "\n".join(
                [
                    _format_ops_summary(state.ops),
                    "",
                    f"QID: {qid}",
                    f"Tag: {tag or '-'}",
                    f"Type: {qtype}{'/' + sel if sel else ''}",
                    f"Text: {preview or '-'}",
                ]
            )
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:  # type: ignore[name-defined]
        option = getattr(event, "option", None)
        if option is None:
            return
        text = str(getattr(option, "prompt", "") or getattr(option, "text", "") or "")
        state: StructuralEditState = self.app.structural_state  # type: ignore[attr-defined]
        if text == "← Back":
            self.app.pop_screen()  # type: ignore[attr-defined]
            return
        if text == "Show all questions":
            try:
                from qsync.dimensions.items_structural import _format_qid_label, iter_all_qids

                survey = getattr(self, "_survey", None)
                if survey is None:
                    return
                qids = self.query_one("#qids", OptionList)
                qids.clear_options()
                active_set = getattr(self, "_active_set", set()) or set()
                self._label_to_qid = {}
                for qid in iter_all_qids(survey)[:600]:
                    label = _format_qid_label(
                        survey=survey,
                        qid=qid,
                        active_set=active_set,
                        include_flow_status=True,
                    )
                    self._label_to_qid[label] = qid
                    qids.add_option(label)
                qids.add_option("← Back")
            except Exception:
                return
            return
        if not text:
            return
        label_to_qid = getattr(self, "_label_to_qid", {}) or {}
        state.qid = str(label_to_qid.get(text) or text.split()[0] or "").strip()
        self.app.push_screen("struct_surface")  # type: ignore[attr-defined]


class StructuralSurfaceScreen(Screen):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
        ("?", "app.push_screen('help')", "Help"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield OptionList(id="surfaces")
            yield Static("", id="detail")
        yield Footer()

    def on_mount(self) -> None:
        surfaces = self.query_one("#surfaces", OptionList)
        surfaces.clear_options()
        detail = self.query_one("#detail", Static)
        state: StructuralEditState = self.app.structural_state  # type: ignore[attr-defined]
        if not state.survey_id or not state.qid:
            detail.update("Missing survey/QID.")
            surfaces.add_option("← Back")
            return
        try:
            from qsync.qualtrics_client import load_cached_survey
            from qsync.dimensions.items_structural import _is_matrix_question, _is_sbs_matrix_question  # type: ignore

            survey = load_cached_survey(state.survey_id)
            q = survey.questions.get(state.qid) or {}
            is_matrix = _is_matrix_question(q) if isinstance(q, dict) else False
            is_sbs = _is_sbs_matrix_question(q) if isinstance(q, dict) else False

            surfaces.add_option("Question text")
            if is_sbs:
                surfaces.add_option("Subitems (rows/statements)")
                surfaces.add_option("SBS Columns (headers)")
                surfaces.add_option("SBS Column Answers (per-column)")
            elif is_matrix:
                surfaces.add_option("Options (columns)")
                surfaces.add_option("Subitems (rows/statements)")
            else:
                if isinstance(q.get("Choices"), dict):
                    surfaces.add_option("Options")
                if isinstance(q.get("Answers"), dict):
                    surfaces.add_option("Subitems")

            surfaces.add_option("Review / push / revert")
            surfaces.add_option("← Back")
            detail.update(
                "\n".join(
                    [
                        _format_ops_summary(state.ops),
                        "",
                        f"Survey: {state.survey_id}",
                        f"QID: {state.qid}",
                        "",
                        "Select a surface to edit.",
                    ]
                )
            )
        except Exception as exc:
            detail.update(f"Failed: {exc}")
            surfaces.add_option("← Back")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:  # type: ignore[name-defined]
        option = getattr(event, "option", None)
        if option is None:
            return
        text = str(getattr(option, "prompt", "") or getattr(option, "text", "") or "")
        state: StructuralEditState = self.app.structural_state  # type: ignore[attr-defined]
        if text in {"← Back", "Back"}:
            self.app.pop_screen()  # type: ignore[attr-defined]
            return
        if text.startswith("Review"):
            self.app.push_screen("struct_review")  # type: ignore[attr-defined]
            return
        if text.startswith("Question"):
            state.surface = "question-text"
            state.action = "edit"
            state.item_id = None
            self.app.push_screen("struct_text")  # type: ignore[attr-defined]
            return
        if text.startswith("Options"):
            state.surface = "options"
        elif text.startswith("Subitems"):
            state.surface = "subitems"
        elif text.startswith("SBS Columns"):
            state.surface = "sbs_columns"
        else:
            state.surface = "sbs_column_answers"
        self.app.push_screen("struct_action")  # type: ignore[attr-defined]


class StructuralActionScreen(Screen):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
        ("?", "app.push_screen('help')", "Help"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield OptionList(id="actions")
            yield Static("", id="detail")
        yield Footer()

    def on_mount(self) -> None:
        actions = self.query_one("#actions", OptionList)
        actions.clear_options()
        detail = self.query_one("#detail", Static)
        state: StructuralEditState = self.app.structural_state  # type: ignore[attr-defined]
        surf = state.surface or ""
        if surf in {"sbs_columns", "sbs_column_answers"}:
            actions.add_option("Edit")
        elif surf == "question-text":
            actions.add_option("Edit")
        else:
            actions.add_option("Add")
            actions.add_option("Edit")
            actions.add_option("Remove")
        actions.add_option("← Back")
        detail.update(
            "\n".join(
                [
                    _format_ops_summary(state.ops),
                    "",
                    f"Survey: {state.survey_id}",
                    f"QID: {state.qid}",
                    f"Surface: {surf}",
                ]
            )
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:  # type: ignore[name-defined]
        option = getattr(event, "option", None)
        if option is None:
            return
        text = str(getattr(option, "prompt", "") or getattr(option, "text", "") or "")
        state: StructuralEditState = self.app.structural_state  # type: ignore[attr-defined]
        if text.startswith("←"):
            self.app.pop_screen()  # type: ignore[attr-defined]
            return
        state.action = text.strip().lower()
        # For MVP: go straight to text edit; advanced ID selection is handled inside the text screen.
        self.app.push_screen("struct_text")  # type: ignore[attr-defined]


class StructuralTextEditScreen(Screen):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
        ("?", "app.push_screen('help')", "Help"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="detail")
        yield Footer()

    def on_mount(self) -> None:
        # Deprecated in favor of StructuralSessionScreen.
        detail = self.query_one("#detail", Static)
        state: StructuralEditState = self.app.structural_state  # type: ignore[attr-defined]
        detail.update(
            "\n".join(
                [
                    _format_ops_summary(state.ops),
                    "",
                    "This screen is no longer used.",
                    "",
                    "Next:",
                    f"- Use CLI survey menu for structural edits: qsync survey menu",
                    f"- Or run non-interactive: qsync items edit --survey-id {state.survey_id} --qid {state.qid}",
                    "",
                    "Press b/Esc to go back.",
                ]
            )
        )


class StructuralReviewScreen(Screen):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
        ("?", "app.push_screen('help')", "Help"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="detail")
        yield Footer()

    def on_mount(self) -> None:
        state: StructuralEditState = self.app.structural_state  # type: ignore[attr-defined]
        detail = self.query_one("#detail", Static)
        detail.update(
            "\n".join(
                [
                    _format_ops_summary(state.ops),
                    "",
                    "This screen is no longer used.",
                ]
            )
        )
