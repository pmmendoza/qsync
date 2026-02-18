from __future__ import annotations

from pathlib import Path
from typing import List

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Footer, Header, Input, OptionList, Static

from ..survey_master_columns import MasterColumn, save_master_columns_yaml


class _ColumnsOptionList(OptionList):
    """OptionList with Survey Master column actions scoped to the list widget.

    Scoping bindings to the list avoids conflicting with typing in the filter input.
    """

    BINDINGS = [
        ("space", "toggle", "Toggle"),
        ("j", "move_down", "Move down"),
        ("k", "move_up", "Move up"),
        ("enter", "save_and_quit", "Save + quit"),
        ("q", "quit_without_save", "Quit"),
        ("escape", "focus_filter", "Filter"),
        ("/", "focus_filter", "Filter"),
    ]

    def action_toggle(self) -> None:
        if hasattr(self.app, "_toggle_selected"):
            self.app._toggle_selected()  # type: ignore[attr-defined]

    def action_move_down(self) -> None:
        if hasattr(self.app, "_move_selected"):
            self.app._move_selected(1)  # type: ignore[attr-defined]

    def action_move_up(self) -> None:
        if hasattr(self.app, "_move_selected"):
            self.app._move_selected(-1)  # type: ignore[attr-defined]

    def action_save_and_quit(self) -> None:
        if hasattr(self.app, "_save_columns"):
            self.app._save_columns()  # type: ignore[attr-defined]
        self.app.exit()  # type: ignore[no-untyped-call]

    def action_quit_without_save(self) -> None:
        self.app.exit()  # type: ignore[no-untyped-call]

    def action_focus_filter(self) -> None:
        if hasattr(self.app, "_focus_filter"):
            self.app._focus_filter()  # type: ignore[attr-defined]


class MasterColumnsApp(App[None]):
    """Survey Master columns picker (order + visibility)."""

    TITLE = "qsync Survey Master Columns"

    CSS = """
    Screen {
        layout: vertical;
    }
    #top {
        height: auto;
        padding: 1 2;
    }
    #filter {
        height: 3;
    }
    #columns {
        height: 1fr;
    }
    #help {
        height: auto;
        padding: 0 2 1 2;
        color: $text-muted;
    }
    #status {
        height: auto;
        padding: 0 2;
    }
    """

    def __init__(self, *, columns: List[MasterColumn], config_path: Path) -> None:
        super().__init__()
        self._columns: list[MasterColumn] = list(columns)
        self._config_path = Path(config_path)
        self._filter_text = ""
        self._visible_indices: list[int] = []
        self._highlighted_visible_idx: int = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="top"):
            yield Static(f"Config: {self._config_path}", id="status")
            yield Input(placeholder="Filter columns (type to search)...", id="filter")
        yield _ColumnsOptionList(id="columns")
        yield Static(
            "Keys (when list focused): Space toggle, j/k reorder, Enter save+quit, q quit, / filter",
            id="help",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_options()
        self.query_one("#columns", OptionList).focus()

    def _focus_filter(self) -> None:
        filt = self.query_one("#filter", Input)
        filt.focus()

    def _focus_list(self) -> None:
        self.query_one("#columns", OptionList).focus()

    def on_input_changed(self, event: Input.Changed) -> None:  # type: ignore[name-defined]
        if str(getattr(event, "input", None).id or "") != "filter":
            return
        self._filter_text = str(getattr(event, "value", "") or "")
        # Keep the selected column (by name) when possible.
        selected_name = self._selected_column_name()
        self._refresh_options(keep_name=selected_name)

    def on_key(self, event) -> None:  # type: ignore[override]
        # Convenience: Down from the filter jumps into the list (like other qsync TUIs).
        focused = getattr(self, "focused", None)
        if (
            str(getattr(event, "key", "")) == "down"
            and str(getattr(focused, "id", "")) == "filter"
        ):
            if self._visible_indices:
                event.prevent_default()
                self._focus_list()
                return
        # Esc from filter returns to list.
        if (
            str(getattr(event, "key", "")) == "escape"
            and str(getattr(focused, "id", "")) == "filter"
        ):
            event.prevent_default()
            self._focus_list()
            return

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:  # type: ignore[name-defined]
        idx = getattr(event, "option_index", None)
        if idx is None:
            return
        self._highlighted_visible_idx = int(idx)

    def _selected_column_name(self) -> str | None:
        if not self._visible_indices:
            return None
        vidx = max(0, min(self._highlighted_visible_idx, len(self._visible_indices) - 1))
        idx = self._visible_indices[vidx]
        if idx < 0 or idx >= len(self._columns):
            return None
        return self._columns[idx].name

    def _visible_match(self, col: MasterColumn) -> bool:
        q = self._filter_text.strip().lower()
        if not q:
            return True
        return q in col.name.lower()

    def _recompute_visible_indices(self) -> list[int]:
        return [i for i, c in enumerate(self._columns) if self._visible_match(c)]

    def _format_column_label(self, col: MasterColumn) -> str:
        mark = "[x]" if col.enabled else "[ ]"
        suffix = " (required)" if col.pinned else ""
        return f"{mark} {col.name}{suffix}"

    def _refresh_options(self, *, keep_name: str | None = None) -> None:
        options = self.query_one("#columns", OptionList)
        self._visible_indices = self._recompute_visible_indices()

        options.clear_options()
        for idx in self._visible_indices:
            options.add_option(self._format_column_label(self._columns[idx]))

        if not self._visible_indices:
            self._highlighted_visible_idx = 0
            return

        # Try to keep selection on the same column by name.
        if keep_name:
            for vidx, idx in enumerate(self._visible_indices):
                if self._columns[idx].name == keep_name:
                    self._highlighted_visible_idx = vidx
                    break
            else:
                self._highlighted_visible_idx = 0
        else:
            self._highlighted_visible_idx = min(
                self._highlighted_visible_idx, len(self._visible_indices) - 1
            )

        try:
            options.highlighted = self._highlighted_visible_idx  # type: ignore[attr-defined]
        except Exception:
            pass

    def _toggle_selected(self) -> None:
        name = self._selected_column_name()
        if not name:
            return
        for idx, col in enumerate(self._columns):
            if col.name != name:
                continue
            if col.pinned:
                self.query_one("#status", Static).update(
                    f"Config: {self._config_path} | {name} is required and cannot be hidden."
                )
                return
            self._columns[idx] = MasterColumn(
                name=col.name, enabled=not col.enabled, pinned=col.pinned
            )
            self._refresh_options(keep_name=name)
            return

    def _move_selected(self, delta: int) -> None:
        name = self._selected_column_name()
        if not name:
            return

        visible = self._visible_indices
        if len(visible) < 2:
            return

        vidx = max(0, min(self._highlighted_visible_idx, len(visible) - 1))
        target_vidx = vidx + int(delta)
        if target_vidx < 0 or target_vidx >= len(visible):
            return

        a = visible[vidx]
        b = visible[target_vidx]
        if a == b:
            return

        cols = list(self._columns)
        cols[a], cols[b] = cols[b], cols[a]
        self._columns = cols

        # Keep the moved item selected.
        self._refresh_options(keep_name=name)

    def _save_columns(self) -> None:
        save_master_columns_yaml(self._config_path, self._columns)
        self.query_one("#status", Static).update(f"Saved: {self._config_path}")
