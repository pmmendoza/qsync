"""Textual TUI entrypoint for qsync (optional extra: `qsync[tui]`).

Design goals:
- Keep base CLI stable: this module is imported only when `qsync tui` is executed.
- Provide a real two-pane UI (left menu, right context) for a small pilot flow.
- Avoid side effects on import; load workspace data only when screens mount.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Footer, Header, OptionList, Static


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
            detail.update("Help:\nKeyboard shortcuts and workflow notes.")
        elif idx == 2:
            detail.update("Exit the TUI.")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:  # type: ignore[name-defined]
        idx = getattr(event, "option_index", None)
        if idx == 0:
            self.app.push_screen("sync_survey")  # type: ignore[attr-defined]
        elif idx == 1:
            self.app.push_screen("help")  # type: ignore[attr-defined]
        else:
            self.app.exit()


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
            yield OptionList(id="surveys")
            yield Static(
                "Loading surveys from inventory...\n\nIf this is empty, run `qsync survey inventory` first.",
                id="detail",
            )
        yield Footer()

    def on_mount(self) -> None:
        surveys = self.query_one("#surveys", OptionList)
        detail = self.query_one("#detail", Static)
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
            for r in rows[:400]:
                sid = str(r.get("id") or "").strip()
                name = str(r.get("name") or "Untitled").strip()
                surveys.add_option(f"{sid} - {name}")
            detail.update("Select a survey to continue.")
        except Exception as exc:
            detail.update(f"Failed to load inventory: {exc}\n\nRun: qsync survey inventory")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:  # type: ignore[name-defined]
        option = getattr(event, "option", None)
        if option is None:
            return
        text = str(getattr(option, "prompt", "") or getattr(option, "text", "") or "")
        sid = text.split(" - ", 1)[0].strip()
        if not sid:
            return
        state: SyncWizardState = self.app.sync_state  # type: ignore[attr-defined]
        state.survey_id = sid
        state.survey_name = text.split(" - ", 1)[1].strip() if " - " in text else None
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
            state.dimensions = None

            if not changed:
                detail.update("No changes detected for this survey.")
                dims.add_option("Back")
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
            dims.add_option("Back")
        except Exception as exc:
            detail.update(f"Failed to detect changes: {exc}")
            dims.add_option("Back")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:  # type: ignore[name-defined]
        option = getattr(event, "option", None)
        if option is None:
            return
        text = str(getattr(option, "prompt", "") or getattr(option, "text", "") or "")
        state: SyncWizardState = self.app.sync_state  # type: ignore[attr-defined]
        if text == "Back":
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
    #menu, #surveys, #dims { width: 50%; }
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

    def on_mount(self) -> None:
        self.install_screen(HelpScreen(), name="help")
        self.install_screen(MainMenuScreen(), name="main")
        self.install_screen(SyncSurveyScreen(), name="sync_survey")
        self.install_screen(SyncDimensionsScreen(), name="sync_dims")
        self.install_screen(SyncConfirmScreen(), name="sync_confirm")

        if self.start_screen == "sync":
            self.push_screen("sync_survey")
        else:
            self.push_screen("main")

