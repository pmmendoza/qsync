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
import shlex
import subprocess
import sys
import time
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


def _active_account_name() -> str | None:
    try:
        from qsync.config import get_active_account

        return get_active_account()
    except Exception:
        return None


def _is_default_account() -> bool:
    return not bool(_active_account_name())


def _disabled_reason_for_default_only() -> str | None:
    if _is_default_account():
        return None
    return "Requires default account context (current account is non-default)."


def _format_elapsed_for_ui(seconds: float) -> str:
    try:
        from qsync.terminal_output import format_elapsed

        return format_elapsed(seconds)
    except Exception:
        if seconds < 1:
            return "< 1s"
        return f"{seconds:.1f}s"


def _timed_call(func):
    started = time.perf_counter()
    value = func()
    elapsed = _format_elapsed_for_ui(time.perf_counter() - started)
    return value, elapsed


def _resolve_api_client_for_active_account() -> tuple[str, dict[str, str], str | None]:
    """Return (base_url, headers, account_name) for the currently active account context."""

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
    return base, headers, account


def _list_surveys_for_active_account() -> list[dict[str, Any]]:
    from qsync.survey_selection import list_surveys_via_api

    base, headers, _account = _resolve_api_client_for_active_account()
    surveys = list_surveys_via_api(base_url=base, headers=headers)
    surveys.sort(
        key=lambda s: (s.get("lastModified") or s.get("creationDate") or ""),
        reverse=True,
    )
    return surveys


def _fetch_survey_definition_for_active_account(survey_id: str) -> dict[str, Any]:
    from qsync.cli_survey import fetch_survey_definition

    base, headers, _account = _resolve_api_client_for_active_account()
    definition = fetch_survey_definition(base, headers, survey_id)
    if not isinstance(definition, dict):
        raise RuntimeError("Unexpected survey-definition payload shape.")
    return definition


def _ordered_qids_from_definition_for_tui(definition: dict[str, Any]) -> list[str]:
    from qsync.cli_survey import _flow_ordered_block_ids, _is_trash_block

    questions = definition.get("Questions")
    if not isinstance(questions, dict):
        return []
    blocks = definition.get("Blocks")
    if not isinstance(blocks, dict):
        return sorted(str(k).strip() for k in questions.keys() if str(k).strip())

    ordered: list[str] = []
    seen: set[str] = set()
    for block_id in _flow_ordered_block_ids(definition):
        block = blocks.get(block_id)
        if not isinstance(block, dict):
            continue
        if _is_trash_block(block):
            continue
        elements = (
            block.get("BlockElements")
            if isinstance(block.get("BlockElements"), list)
            else block.get("Elements")
        )
        if not isinstance(elements, list):
            continue
        for elem in elements:
            if not isinstance(elem, dict):
                continue
            qid = str(elem.get("QuestionID") or "").strip()
            if not qid or qid in seen or qid not in questions:
                continue
            seen.add(qid)
            ordered.append(qid)
    leftovers = sorted(
        [
            str(qid).strip()
            for qid in questions.keys()
            if str(qid).strip() and str(qid).strip() not in seen
        ]
    )
    ordered.extend(leftovers)
    return ordered


def _question_preview_from_payload(payload: dict[str, Any]) -> str:
    text = (
        str(payload.get("QuestionText") or "").strip()
        or str(payload.get("QuestionDescription") or "").strip()
        or str(payload.get("DataExportTag") or "").strip()
        or "Untitled question"
    )
    text = re.sub(r"<[^>]+>", " ", text)
    text = " ".join(text.split())
    if len(text) > 110:
        return text[:109].rstrip() + "…"
    return text


def _question_labels_from_definition_for_tui(
    definition: dict[str, Any], qids: list[str]
) -> dict[str, str]:
    out: dict[str, str] = {}
    questions = definition.get("Questions")
    if not isinstance(questions, dict):
        return out
    for qid in qids:
        payload = questions.get(qid) if isinstance(questions.get(qid), dict) else {}
        preview = _question_preview_from_payload(payload if isinstance(payload, dict) else {})
        out[qid] = f"{qid} - {preview}"
    return out


def _block_labels_from_definition_for_tui(definition: dict[str, Any]) -> dict[str, str]:
    from qsync.cli_survey import _flow_ordered_block_ids, _is_trash_block

    out: dict[str, str] = {}
    blocks = definition.get("Blocks")
    if not isinstance(blocks, dict):
        return out

    ordered: list[str] = []
    for bid in _flow_ordered_block_ids(definition):
        if bid in blocks:
            ordered.append(bid)
    for bid in sorted(str(k).strip() for k in blocks.keys() if str(k).strip()):
        if bid not in ordered:
            ordered.append(bid)

    for bid in ordered:
        block = blocks.get(bid)
        if not isinstance(block, dict):
            continue
        if _is_trash_block(block):
            continue
        name = (
            str(block.get("Description") or "").strip()
            or str(block.get("BlockDescription") or "").strip()
            or str(block.get("Type") or "").strip()
            or "Block"
        )
        if len(name) > 90:
            name = name[:89].rstrip() + "…"
        out[bid] = f"{bid} - {name}"
    return out


@dataclass
class MoveQuestionState:
    survey_id: str | None = None
    survey_name: str | None = None
    definition: dict[str, Any] | None = None
    ordered_qids: list[str] = field(default_factory=list)
    question_labels: dict[str, str] = field(default_factory=dict)
    block_labels: dict[str, str] = field(default_factory=dict)
    selected_qids: list[str] = field(default_factory=list)
    filter_query: str = ""
    target_block_id: str | None = None
    after_qid: str | None = None
    before_qid: str | None = None
    position: str = "append"
    anchor_mode: str | None = None  # after|before
    block_pick_position: str = "append"
    dry_run: bool = True
    force_live: bool = False
    publish: bool = True
    publish_description: str = ""
    last_result: str | None = None

    def reset(self) -> None:
        self.survey_id = None
        self.survey_name = None
        self.definition = None
        self.ordered_qids = []
        self.question_labels = {}
        self.block_labels = {}
        self.selected_qids = []
        self.filter_query = ""
        self.target_block_id = None
        self.after_qid = None
        self.before_qid = None
        self.position = "append"
        self.anchor_mode = None
        self.block_pick_position = "append"
        self.dry_run = True
        self.force_live = False
        self.publish = True
        self.publish_description = ""
        self.last_result = None

    def placement_summary(self) -> str:
        if self.after_qid:
            return f"After {self.after_qid}"
        if self.before_qid:
            return f"Before {self.before_qid}"
        if self.target_block_id:
            return f"{self.position} in block {self.target_block_id}"
        return f"{self.position} (auto target block)"

    def argv(self) -> list[str]:
        argv = ["survey", "move-question"]
        if self.survey_id:
            argv.extend(["--survey-id", self.survey_id])
        for qid in self.selected_qids:
            argv.extend(["--question-id", qid])
        if self.target_block_id:
            argv.extend(["--target-block-id", self.target_block_id])
        if self.after_qid:
            argv.extend(["--after-qid", self.after_qid])
        if self.before_qid:
            argv.extend(["--before-qid", self.before_qid])
        if not self.after_qid and not self.before_qid:
            argv.extend(["--position", self.position])
        if self.dry_run:
            argv.append("--dry-run")
        else:
            argv.append("--yes")
            if self.force_live:
                argv.append("--force-live")
            if not self.publish:
                argv.append("--no-publish")
            if self.publish_description.strip():
                argv.extend(["--publish-description", self.publish_description.strip()])
        return argv

    def command(self) -> str:
        return "qsync " + shlex.join(self.argv())


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
    extra_sync_args: str = ""

    def argv(self) -> list[str]:
        argv = ["sync"]
        if self.survey_id:
            argv.extend(["--survey-id", self.survey_id])
        if self.dimensions:
            argv.extend(["--dimensions", ",".join(self.dimensions)])
        extra = (self.extra_sync_args or "").strip()
        if extra:
            try:
                argv.extend(shlex.split(extra))
            except ValueError as exc:
                raise ValueError(f"Invalid additional sync args: {exc}") from exc
        return argv

    def command(self) -> str:
        return "qsync " + shlex.join(self.argv())


def _run_qsync_cli_subcommand(argv: list[str]) -> int:
    from pathlib import Path

    from qsync.config import resolve_root

    root = resolve_root(required=False) or Path.cwd()
    cmd = [sys.executable, "-m", "qsync.cli", "--root", str(root), *argv]
    result = subprocess.run(cmd, check=False)
    return int(result.returncode)


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
                    "This TUI is a wrapper around existing qsync workflows.",
                    "Operations run through the same CLI safeguards in suspended mode.",
                ]
            ),
            id="help_body",
        )
        yield Footer()


class ContextHelpScreen(Screen):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
        ("h", "app.pop_screen", "Close help"),
        ("?", "app.pop_screen", "Close help"),
    ]

    def __init__(self, *, title: str, lines: list[str]) -> None:
        super().__init__()
        self._title = title
        self._lines = lines

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            "\n".join([self._title, "", *self._lines]),
            id="help_body",
        )
        yield Footer()


class MainMenuScreen(Screen):
    BINDINGS = [
        ("q", "app.quit", "Quit"),
        ("?", "app.push_screen('help')", "Help"),
        ("h", "context_help", "Screen Help"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield OptionList(
                "Sync wizard (select survey + dimensions)",
                "Survey menu (TUI)",
                "Content editors",
                "Settings",
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
                "Sync wizard:\n- Choose single-survey or focal mode\n- Review/select dimensions\n- Add any CLI sync flags\n- Run sync directly from TUI"
            )
        elif idx == 1:
            detail.update(
                "Survey menu (TUI):\nBrowse quick actions or open the full CLI survey menu from inside TUI."
            )
        elif idx == 2:
            detail.update(
                "Content editors:\nInteractive editors that stage changes into the normal qsync pipeline.\n\nIncludes: SBS items structural edits."
            )
        elif idx == 3:
            detail.update(
                "Settings:\nWorkspace/account controls, inventory refresh, doctor checks, and cache-folder preferences."
            )
        elif idx == 4:
            detail.update("Help:\nKeyboard shortcuts and workflow notes.")
        elif idx == 5:
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
            self.app.push_screen("settings")  # type: ignore[attr-defined]
        elif idx == 4:
            self.app.push_screen("help")  # type: ignore[attr-defined]
        else:
            self.app.exit()

    def action_context_help(self) -> None:
        self.app.push_screen(  # type: ignore[attr-defined]
            ContextHelpScreen(
                title="Main Menu Help",
                lines=[
                    "Use arrows + Enter to navigate.",
                    "Settings centralizes account/workspace controls.",
                    "Survey menu exposes native quick actions and full-menu fallback.",
                    "Press b/Esc from child screens to return.",
                ],
            )
        )


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


class SettingsScreen(Screen):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
        ("?", "app.push_screen('help')", "Help"),
        ("h", "context_help", "Screen Help"),
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

    def action_context_help(self) -> None:
        self.app.push_screen(  # type: ignore[attr-defined]
            ContextHelpScreen(
                title="Settings Help",
                lines=[
                    "This screen is the command center for account/workspace setup.",
                    "Most actions call existing qsync CLI flows under suspended mode.",
                    "Use this before sync/edit workflows when environment state is uncertain.",
                ],
            )
        )

    def on_mount(self) -> None:
        menu = self.query_one("#menu", OptionList)
        menu.clear_options()
        menu.add_option("Show account status")
        menu.add_option("List configured accounts")
        menu.add_option("Set active account")
        menu.add_option("Clear active account")
        menu.add_option("Refresh inventory (no counts)")
        menu.add_option("Refresh inventory (focal counts)")
        menu.add_option("Prepare surfaces")
        menu.add_option("Doctor check API (/whoami)")
        menu.add_option("Configure survey cache folder")
        menu.add_option("Open full survey menu")
        menu.add_option("← Back")
        self._update_detail()

    def _update_detail(self, *, message: str | None = None) -> None:
        detail = self.query_one("#detail", Static)
        lines = [
            "Settings",
            "",
            *_account_context_lines(),
            "",
            "Use the left panel to run account/workspace operations.",
        ]
        if message:
            lines.extend(["", "Last result:", message])
        detail.update("\n".join(lines))

    def _run_subcommand(self, argv: list[str]) -> str:
        with self.app.suspend():  # type: ignore[attr-defined]
            print(f"\n[qsync:tui] Running: qsync {shlex.join(argv)}")

            def _runner() -> int:
                return _run_qsync_cli_subcommand(argv)

            code, elapsed = _timed_call(_runner)
            print(f"[qsync:tui] Exit={code} in {elapsed}")
        if code == 0:
            return f"Completed in {elapsed}."
        return f"Exited with code {code} after {elapsed}."

    def _prompt_text(self, label: str, default: str = "") -> str | None:
        from qsync.interactive_menu import text_input

        with self.app.suspend():  # type: ignore[attr-defined]
            return text_input(label, default=default)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:  # type: ignore[name-defined]
        option = getattr(event, "option", None)
        if option is None:
            return
        choice = str(getattr(option, "prompt", "") or getattr(option, "text", "") or "")
        if choice.startswith("←"):
            self.app.pop_screen()  # type: ignore[attr-defined]
            return

        try:
            if choice.startswith("Show account status"):
                msg = self._run_subcommand(["account", "status"])
            elif choice.startswith("List configured accounts"):
                msg = self._run_subcommand(["account", "list"])
            elif choice.startswith("Set active account"):
                typed = (self._prompt_text("Account name (`default` to clear):") or "").strip()
                if not typed:
                    msg = "Cancelled."
                elif typed.lower() == "default":
                    msg = self._run_subcommand(["account", "clear"])
                else:
                    msg = self._run_subcommand(["account", "use", typed])
            elif choice.startswith("Clear active account"):
                msg = self._run_subcommand(["account", "clear"])
            elif choice.startswith("Refresh inventory (no counts)"):
                msg = self._run_subcommand(["survey", "inventory"])
            elif choice.startswith("Refresh inventory (focal counts)"):
                msg = self._run_subcommand(["survey", "inventory", "--focal"])
            elif choice.startswith("Prepare surfaces"):
                msg = self._run_subcommand(["survey", "prepare"])
            elif choice.startswith("Doctor check API"):
                msg = self._run_subcommand(["doctor", "--check-api"])
            elif choice.startswith("Configure survey cache folder"):
                typed = (
                    self._prompt_text(
                        "Cache folder name (`default` clears preference; blank cancels):"
                    )
                    or ""
                ).strip()
                if not typed:
                    msg = "Cancelled."
                elif typed.lower() == "default":
                    msg = self._run_subcommand(["account", "cache-dir", "--clear"])
                else:
                    msg = self._run_subcommand(["account", "cache-dir", typed])
            else:
                msg = self._run_subcommand(["survey", "menu"])
        except Exception as exc:
            msg = f"ERROR: {exc}"
        self._update_detail(message=msg)


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
    FOCAL_MODE_LABEL = "Focal sync flow (multi-survey, no --survey-id)"
    SHARED_PICKER_LABEL = "Open shared picker (details/manual/regex)"

    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
        ("?", "app.push_screen('help')", "Help"),
        ("h", "context_help", "Screen Help"),
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
        surveys.add_option(self.FOCAL_MODE_LABEL)
        surveys.add_option(self.SHARED_PICKER_LABEL)
        if not rows:
            return
        for row in rows:
            surveys.add_option(_survey_option_label(row))

    def action_context_help(self) -> None:
        self.app.push_screen(  # type: ignore[attr-defined]
            ContextHelpScreen(
                title="Sync Survey Picker Help",
                lines=[
                    "This screen supports both native filtering and the shared picker flow.",
                    "Use the shared picker option for CLI-parity details/manual/regex behavior.",
                    "Use focal mode when you want multi-survey sync without --survey-id.",
                ],
            )
        )

    def _run_shared_picker(self) -> None:
        from qsync.survey_selection import pick_survey_id_from_records

        rows = list(getattr(self, "_rows", []) or [])
        if not rows:
            return
        with self.app.suspend():  # type: ignore[attr-defined]
            picked = pick_survey_id_from_records(
                message="Pick a survey for sync:",
                records=rows,
                include_back=True,
                include_manual=True,
                include_details=True,
            )
        if not picked:
            return
        for row in rows:
            if str(row.get("id") or "").strip() == picked:
                self._select_survey(row)
                return
        self._select_survey({"id": picked, "name": picked})

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
        if idx is None:
            return
        if idx == 0:
            detail = self.query_one("#detail", Static)
            detail.update(
                "\n".join(
                    [
                        self.FOCAL_MODE_LABEL,
                        "",
                        "Runs `qsync sync` without --survey-id.",
                        "Use additional sync args later for flags like:",
                        "- --all, --scope, --dimensions, --yes, --pending-action, --fix",
                        "- --force-live/--force-preview, --skip-publish, --refresh-workbooks",
                        "- --skip-refresh, --allow-drift, --allow-skip-embedded, --json",
                    ]
                )
            )
            return
        if idx == 1:
            detail = self.query_one("#detail", Static)
            detail.update(
                "\n".join(
                    [
                        self.SHARED_PICKER_LABEL,
                        "",
                        "Uses the same picker semantics as CLI survey selection:",
                        "- details table, manual SurveyID entry, regex/text narrowing",
                    ]
                )
            )
            return
        if not rows or idx < 2 or idx > len(rows) + 1:
            return
        r = rows[idx - 2]
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
        if idx is None:
            return
        state: SyncWizardState = self.app.sync_state  # type: ignore[attr-defined]
        if idx == 0:
            state.survey_id = None
            state.survey_name = None
            state.dimensions = None
            self.app.push_screen("sync_confirm")  # type: ignore[attr-defined]
            return
        if idx == 1:
            self._run_shared_picker()
            return
        if rows is None or idx < 2 or idx > len(rows) + 1:
            return
        self._select_survey(rows[idx - 2])

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
        self._render()

    def _render(self, *, message: str | None = None) -> None:
        actions = self.query_one("#actions", OptionList)
        actions.clear_options()
        state: SyncWizardState = self.app.sync_state  # type: ignore[attr-defined]
        actions.add_option("Run sync now (execute current command)")
        actions.add_option("Edit additional sync args (all `qsync sync` flags)")
        if state.extra_sync_args.strip():
            actions.add_option("Clear additional sync args")
        if state.survey_id:
            actions.add_option("Change selected dimensions")
            actions.add_option("Switch to focal sync flow (clear --survey-id)")
        else:
            actions.add_option("Pick a single survey (--survey-id)")
        actions.add_option("Launch raw `qsync sync` CLI flow")
        actions.add_option("← Back")

        detail = self.query_one("#detail", Static)
        mode = "single-survey" if state.survey_id else "focal / multi-survey"
        survey = state.survey_id or "(none)"
        dims = ", ".join(state.dimensions or []) if state.dimensions else "(default)"
        extra = state.extra_sync_args.strip() or "(none)"
        try:
            cmd = state.command()
            cmd_error = None
        except Exception as exc:
            cmd = "qsync sync"
            cmd_error = str(exc)
        detail.update(
            "\n".join(
                [
                    "Sync execution.",
                    "",
                    f"Mode: {mode}",
                    f"Survey: {survey}",
                    f"Dimensions: {dims}",
                    f"Additional args: {extra}",
                    "",
                    "Current command:",
                    cmd,
                    "",
                    "Coverage note:",
                    "- Add any CLI sync flags in 'additional args' for full parity.",
                    "- Examples: --all --scope items:QID1 --yes --pending-action push --fix safe",
                    "- Also supports: --force-live/--force-preview --skip-publish --refresh-workbooks",
                    "- And: --skip-refresh --allow-drift --allow-skip-embedded --json",
                    "",
                    *(["Command error: " + cmd_error, ""] if cmd_error else []),
                    *(["Last result:", message, ""] if message else []),
                    "Use the left panel to run or adjust this sync.",
                ]
            )
        )

    def _edit_additional_sync_args(self) -> str:
        from qsync.interactive_menu import text_input

        state: SyncWizardState = self.app.sync_state  # type: ignore[attr-defined]
        with self.app.suspend():  # type: ignore[attr-defined]
            typed = text_input(
                "Additional args for `qsync sync` (optional, everything after `qsync sync`):",
                default=state.extra_sync_args or "",
            )
        if typed is None:
            return "Additional sync args unchanged."
        state.extra_sync_args = str(typed).strip()
        return "Updated additional sync args."

    def _run_current_sync(self) -> str:
        state: SyncWizardState = self.app.sync_state  # type: ignore[attr-defined]
        argv = state.argv()
        with self.app.suspend():  # type: ignore[attr-defined]
            print(f"\n[qsync:tui] Running: qsync {shlex.join(argv)}")
            code = _run_qsync_cli_subcommand(argv)
            print(f"[qsync:tui] Sync finished with exit code {code}.")
        return "Sync completed successfully." if code == 0 else f"Sync exited with code {code}."

    def _run_raw_sync_flow(self) -> str:
        with self.app.suspend():  # type: ignore[attr-defined]
            print("\n[qsync:tui] Launching raw `qsync sync` flow...")
            code = _run_qsync_cli_subcommand(["sync"])
            print(f"[qsync:tui] Raw sync flow exited with code {code}.")
        return "Raw sync flow completed successfully." if code == 0 else f"Raw sync flow exited with code {code}."

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:  # type: ignore[name-defined]
        option = getattr(event, "option", None)
        if option is None:
            return
        choice = str(getattr(option, "prompt", "") or getattr(option, "text", "") or "")
        state: SyncWizardState = self.app.sync_state  # type: ignore[attr-defined]

        if choice.startswith("Run sync now"):
            try:
                message = self._run_current_sync()
            except Exception as exc:
                message = f"ERROR running sync: {exc}"
            self._render(message=message)
            return

        if choice.startswith("Edit additional sync args"):
            try:
                message = self._edit_additional_sync_args()
            except Exception as exc:
                message = f"ERROR editing additional args: {exc}"
            self._render(message=message)
            return

        if choice.startswith("Clear additional sync args"):
            state.extra_sync_args = ""
            self._render(message="Cleared additional sync args.")
            return

        if choice.startswith("Change selected dimensions"):
            if not state.survey_id:
                self._render(message="No survey selected; use additional args (--dimensions ...) in focal mode.")
                return
            self.app.push_screen("sync_dims")  # type: ignore[attr-defined]
            return

        if choice.startswith("Switch to focal sync flow"):
            state.survey_id = None
            state.survey_name = None
            state.dimensions = None
            self._render(message="Switched to focal sync mode (no --survey-id).")
            return

        if choice.startswith("Pick a single survey"):
            self.app.push_screen("sync_survey")  # type: ignore[attr-defined]
            return

        if choice.startswith("Launch raw `qsync sync` CLI flow"):
            try:
                message = self._run_raw_sync_flow()
            except Exception as exc:
                message = f"ERROR launching raw sync flow: {exc}"
            self._render(message=message)
            return

        self.app.pop_screen()  # type: ignore[attr-defined]


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
        self.move_question_state = MoveQuestionState()

    def on_mount(self) -> None:
        self.install_screen(HelpScreen(), name="help")
        self.install_screen(MainMenuScreen(), name="main")
        self.install_screen(ContentEditorsScreen(), name="content_editors")
        self.install_screen(SettingsScreen(), name="settings")
        self.install_screen(EmbeddedDataEditorScreen(), name="embedded_editor")
        self.install_screen(SyncSurveyScreen(), name="sync_survey")
        self.install_screen(SyncDimensionsScreen(), name="sync_dims")
        self.install_screen(SyncConfirmScreen(), name="sync_confirm")
        self.install_screen(SurveyMenuScreen(), name="survey_menu")
        self.install_screen(MoveQuestionSurveyScreen(), name="moveq_survey")
        self.install_screen(MoveQuestionSelectScreen(), name="moveq_select")
        self.install_screen(MoveQuestionPlacementScreen(), name="moveq_place")
        self.install_screen(MoveQuestionAnchorScreen(), name="moveq_anchor")
        self.install_screen(MoveQuestionBlockScreen(), name="moveq_block")
        self.install_screen(MoveQuestionRunScreen(), name="moveq_run")
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
        elif self.start_screen == "settings":
            self.push_screen("settings")
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


@dataclass(frozen=True)
class SurveyMenuEntry:
    label: str
    quick_action: str | None = None
    detail_lines: tuple[str, ...] = ()
    requires_default_account: bool = False
    section: bool = False


class SurveyMenuScreen(Screen):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
        ("?", "app.push_screen('help')", "Help"),
        ("h", "context_help", "Screen Help"),
        ("r", "refresh", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield OptionList(id="menu")
            yield Static("Survey menu (TUI).\n\nSelect an action.", id="detail")
        yield Footer()

    def action_refresh(self) -> None:
        self.on_mount()

    def action_context_help(self) -> None:
        self.app.push_screen(  # type: ignore[attr-defined]
            ContextHelpScreen(
                title="Survey Menu Help",
                lines=[
                    "This screen exposes native quick actions for all current survey-menu workflows.",
                    "Default-account-only actions are shown as disabled when a non-default account is active.",
                    "Use 'Open full survey menu' as a fallback for the original grouped CLI navigation.",
                ],
            )
        )

    def _entries(self) -> list[SurveyMenuEntry]:
        return [
            SurveyMenuEntry("── Survey Setup & Selection ──", section=True, detail_lines=("List/pull/label/focal discovery flows.",)),
            SurveyMenuEntry("List surveys", quick_action="setup-list", detail_lines=("Lists surveys with optional regex filter.",)),
            SurveyMenuEntry("Label survey ID (inventory)", quick_action="setup-label", detail_lines=("Print '<SurveyID> - <Name>' from inventory.",)),
            SurveyMenuEntry("List focal survey IDs (inventory)", quick_action="setup-focal", detail_lines=("Shows focal IDs from inventory.",)),
            SurveyMenuEntry("Pull survey definition (cache)", quick_action="setup-pull", detail_lines=("Download and cache survey JSON locally.",)),
            SurveyMenuEntry("── Edit Questions & Content ──", section=True, detail_lines=("Structural edits and guided add/move/page-break flows.",)),
            SurveyMenuEntry("Items: structural edits", quick_action="edit-structural", detail_lines=("Stage -> review -> push item-level structural ops.",)),
            SurveyMenuEntry("Move question(s) native TUI (recommended)", quick_action="__tui_move__", detail_lines=("Stay in Textual: pick survey, select QIDs, choose placement, run.",)),
            SurveyMenuEntry("Add question(s) guided wizard (CLI fallback)", quick_action="edit-add-question", detail_lines=("Clone/import question and place in flow (questionary flow).",)),
            SurveyMenuEntry("Move question(s) guided wizard (CLI fallback)", quick_action="edit-move-question", detail_lines=("Legacy guided path (questionary flow).",)),
            SurveyMenuEntry("Page breaks guided wizard (CLI fallback)", quick_action="edit-page-breaks", detail_lines=("Add/remove page-break elements using block-local placement.",)),
            SurveyMenuEntry("── Flow, Embedded Data & Integrations ──", section=True, detail_lines=("Embedded-data and Prolific workflows.",)),
            SurveyMenuEntry("Add embedded field (stage)", quick_action="flow-add-embedded", detail_lines=("Stage embedded field add in SurveyFlow.",), requires_default_account=True),
            SurveyMenuEntry("Remove embedded field (stage)", quick_action="flow-remove-embedded", detail_lines=("Stage embedded field removal.",), requires_default_account=True),
            SurveyMenuEntry("Rename embedded field (stage)", quick_action="flow-rename-embedded", detail_lines=("Stage embedded field rename.",), requires_default_account=True),
            SurveyMenuEntry("Cleanup embedded data", quick_action="flow-cleanup-embedded", detail_lines=("Cleanup duplicate/placeholder embedded rows.",), requires_default_account=True),
            SurveyMenuEntry("Prolific authenticity snippet", quick_action="flow-prolific-auth", detail_lines=("Set/review Prolific auth snippet.",), requires_default_account=True),
            SurveyMenuEntry("Prolific wiring", quick_action="flow-prolific-wiring", detail_lines=("Pull/propose/review/preview/apply Prolific wiring.",)),
            SurveyMenuEntry("── Publish, Activation & Versions ──", section=True, detail_lines=("Lifecycle and recovery operations.",)),
            SurveyMenuEntry("Activate survey", quick_action="publish-activate", detail_lines=("Activate selected surveys.",)),
            SurveyMenuEntry("Deactivate survey", quick_action="publish-deactivate", detail_lines=("Deactivate selected surveys.",)),
            SurveyMenuEntry("Publish survey-definition", quick_action="publish-publish", detail_lines=("Create a new version in Qualtrics.",)),
            SurveyMenuEntry("List versions", quick_action="publish-versions", detail_lines=("List available versions for a survey.",)),
            SurveyMenuEntry("Fetch a version", quick_action="publish-fetch-version", detail_lines=("Fetch version payload as json/qsf.",)),
            SurveyMenuEntry("Rollback questions to a version", quick_action="publish-rollback", detail_lines=("Rollback selected QIDs from a version.",)),
            SurveyMenuEntry("── Copy, Slice & Compare ──", section=True, detail_lines=("Derive and verify survey copies.",)),
            SurveyMenuEntry("Copy survey", quick_action="copy-copy", detail_lines=("Copy a survey in the current account.",)),
            SurveyMenuEntry("Slice language(s)", quick_action="copy-slice-language", detail_lines=("Create language-sliced survey copies.",)),
            SurveyMenuEntry("Slice registry (local)", quick_action="copy-slice-registry", detail_lines=("List slice manifests and open links.",)),
            SurveyMenuEntry("Parity check", quick_action="copy-parity", detail_lines=("Compare survey parity (light/deep).",)),
            SurveyMenuEntry("Copy cross-account", quick_action="copy-cross-account", detail_lines=("Copy between source/target account credentials.",)),
            SurveyMenuEntry("── Exports ──", section=True, detail_lines=("Responses and document exports.",)),
            SurveyMenuEntry("Export responses", quick_action="export-responses", detail_lines=("Export response data for a survey.",)),
            SurveyMenuEntry("Export translation document", quick_action="export-translation", detail_lines=("Generate translation review document(s).",), requires_default_account=True),
            SurveyMenuEntry("Export side-by-side document", quick_action="export-side-by-side", detail_lines=("Generate side-by-side comparison doc.",), requires_default_account=True),
            SurveyMenuEntry("── Workspace & Account ──", section=True, detail_lines=("Account context and workspace maintenance.",)),
            SurveyMenuEntry("Switch account", quick_action="workspace-switch-account", detail_lines=("Change account for this survey-menu session.",)),
            SurveyMenuEntry("Show account info", quick_action="workspace-show-account", detail_lines=("Display resolved account/base URL/token status.",)),
            SurveyMenuEntry("Check API (/whoami)", quick_action="workspace-check-api", detail_lines=("Run whoami for current account context.",)),
            SurveyMenuEntry("Refresh inventory", quick_action="workspace-refresh-inventory", detail_lines=("Refresh surveys/inventory.csv.",), requires_default_account=True),
            SurveyMenuEntry("Refresh question-bank index", quick_action="workspace-refresh-question-bank", detail_lines=("Rebuild local cross-survey QID index from pulled survey JSON.",)),
            SurveyMenuEntry("Prepare surfaces", quick_action="workspace-prepare", detail_lines=("Hydrate local editing surfaces.",), requires_default_account=True),
            SurveyMenuEntry("Configure survey cache folder", quick_action="workspace-configure-cache", detail_lines=("Set/clear/create preferred survey cache folder.",)),
            SurveyMenuEntry("── Danger Zone ──", section=True, detail_lines=("Rename/delete with explicit safeguards.",)),
            SurveyMenuEntry("Rename survey", quick_action="danger-rename", detail_lines=("Rename a survey in Qualtrics.",)),
            SurveyMenuEntry("Delete survey(s)", quick_action="danger-delete", detail_lines=("Guided delete with strict confirmations.",)),
            SurveyMenuEntry("Open full survey menu (all CLI actions)", quick_action="__full_menu__", detail_lines=("Launch original grouped survey-menu flow.",)),
            SurveyMenuEntry("← Back", quick_action="__back__"),
        ]

    def _render_menu(self) -> None:
        menu = self.query_one("#menu", OptionList)
        menu.clear_options()
        self._entries_cache = self._entries()
        for entry in self._entries_cache:
            label = entry.label
            if entry.requires_default_account and not _is_default_account():
                label = f"{label} [disabled: default-account-only]"
            menu.add_option(label)

    def on_mount(self) -> None:
        self._render_menu()
        detail = self.query_one("#detail", Static)
        detail.update(
            "\n".join(
                [
                    "Survey menu (TUI).",
                    "",
                    *_account_context_lines(),
                    "",
                    "Native quick-action parity is available on this screen.",
                    "Use full-menu fallback at the bottom if preferred.",
                ]
            )
        )

    def _entry_at(self, idx: int | None) -> SurveyMenuEntry | None:
        if idx is None:
            return None
        entries = getattr(self, "_entries_cache", None) or []
        if idx < 0 or idx >= len(entries):
            return None
        return entries[idx]

    def _run_quick_action(self, quick_action: str) -> str:
        with self.app.suspend():  # type: ignore[attr-defined]
            print(f"\n[qsync:tui] Launching survey action: {quick_action or 'full-menu'}")
            argv = ["survey", "menu"]
            if quick_action:
                argv.extend(["--quick-action", quick_action])

            def _runner() -> int:
                # Run in a subprocess so questionary/autocomplete prompts do not
                # share Textual's event loop.
                return _run_qsync_cli_subcommand(argv)

            code, elapsed = _timed_call(_runner)
            print(f"[qsync:tui] Survey action exit={code} in {elapsed}.")
        if code == 0:
            return f"Completed in {elapsed}."
        return f"Exited with code {code} after {elapsed}."

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:  # type: ignore[name-defined]
        entry = self._entry_at(getattr(event, "option_index", None))
        if entry is None:
            return
        detail = self.query_one("#detail", Static)
        lines = [entry.label, "", *entry.detail_lines, "", *_account_context_lines()]
        if entry.requires_default_account:
            reason = _disabled_reason_for_default_only()
            if reason:
                lines.extend(["", f"Disabled reason: {reason}"])
            else:
                lines.extend(["", "Availability: enabled in default account context."])
        detail.update("\n".join(lines))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:  # type: ignore[name-defined]
        entry = self._entry_at(getattr(event, "option_index", None))
        if entry is None:
            return
        if entry.section:
            return
        if entry.quick_action == "__back__":
            self.app.pop_screen()  # type: ignore[attr-defined]
            return
        if entry.quick_action == "__tui_move__":
            state: MoveQuestionState = self.app.move_question_state  # type: ignore[attr-defined]
            state.reset()
            self.app.push_screen("moveq_survey")  # type: ignore[attr-defined]
            return
        if not entry.quick_action:
            return

        detail = self.query_one("#detail", Static)
        if entry.requires_default_account:
            reason = _disabled_reason_for_default_only()
            if reason:
                detail.update(
                    "\n".join(
                        [
                            "Action blocked.",
                            "",
                            reason,
                            "",
                            "Use `qsync account clear` (default account) or switch account in Workspace section.",
                            "",
                            *_account_context_lines(),
                        ]
                    )
                )
                return
        try:
            quick_action = "" if entry.quick_action == "__full_menu__" else entry.quick_action
            message = self._run_quick_action(quick_action)
        except Exception as exc:
            message = f"ERROR: {exc}"
        detail.update(
            "\n".join(
                [
                    entry.label,
                    "",
                    message,
                    "",
                    *_account_context_lines(),
                    "",
                    "Select another action on the left.",
                ]
            )
        )


class MoveQuestionSurveyScreen(Screen):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
        ("?", "app.push_screen('help')", "Help"),
        ("h", "context_help", "Screen Help"),
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

    def action_context_help(self) -> None:
        self.app.push_screen(  # type: ignore[attr-defined]
            ContextHelpScreen(
                title="Move Questions: Survey Picker",
                lines=[
                    "This is the native in-TUI move flow.",
                    "Select a survey first; next screens stay inside Textual.",
                    "Use the guided wizard fallback from Survey Menu if needed.",
                ],
            )
        )

    def on_mount(self) -> None:
        surveys_widget = self.query_one("#surveys", OptionList)
        surveys_widget.clear_options()
        detail = self.query_one("#detail", Static)
        detail.update("\n".join(["Loading surveys from API...", "", *_account_context_lines()]))
        try:
            rows = _list_surveys_for_active_account()
            self._rows = rows[:400]
            self._filtered_rows = list(self._rows)
            self._apply_filter("")
            detail.update(
                "\n".join(
                    [
                        "Move questions (native TUI).",
                        "",
                        "Select a survey to continue.",
                        "",
                        *_account_context_lines(),
                    ]
                )
            )
        except Exception as exc:
            detail.update(f"ERROR loading surveys: {exc}\n\n" + "\n".join(_account_context_lines()))

    def _apply_filter(self, query: str) -> None:
        rows = [
            r for r in getattr(self, "_rows", []) if _survey_filter_matches(r, query)
        ]
        self._filtered_rows = rows
        surveys_widget = self.query_one("#surveys", OptionList)
        surveys_widget.clear_options()
        for row in rows:
            sid = str(row.get("id") or "").strip()
            if sid:
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
        survey = rows[idx]
        detail = self.query_one("#detail", Static)
        detail.update(
            "\n".join(
                [
                    "Move questions (native TUI)",
                    "",
                    f"Survey: {str(survey.get('id') or '').strip()}",
                    f"Name: {str(survey.get('name') or 'Untitled').strip()}",
                    f"Active: {'yes' if survey.get('isActive') else 'no'}",
                    f"Last modified: {str(survey.get('lastModified') or survey.get('creationDate') or '').strip() or '-'}",
                    "",
                    "Enter to load question list.",
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
        detail = self.query_one("#detail", Static)
        detail.update(f"Loading survey definition for {survey_id}...")
        try:
            definition = _fetch_survey_definition_for_active_account(survey_id)
            ordered_qids = _ordered_qids_from_definition_for_tui(definition)
            labels = _question_labels_from_definition_for_tui(definition, ordered_qids)
            blocks = _block_labels_from_definition_for_tui(definition)
            if not ordered_qids:
                raise RuntimeError("No questions found in selected survey definition.")

            state: MoveQuestionState = self.app.move_question_state  # type: ignore[attr-defined]
            state.reset()
            state.survey_id = survey_id
            state.survey_name = str(row.get("name") or "").strip() or None
            state.definition = definition
            state.ordered_qids = ordered_qids
            state.question_labels = labels
            state.block_labels = blocks
            self.app.push_screen("moveq_select")  # type: ignore[attr-defined]
        except Exception as exc:
            detail.update(f"ERROR loading survey definition: {exc}")


class MoveQuestionSelectScreen(Screen):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
        ("?", "app.push_screen('help')", "Help"),
        ("h", "context_help", "Screen Help"),
        ("r", "refresh", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical():
                yield Input(
                    placeholder="Filter questions by QID/text (regex/text)",
                    id="question_filter",
                )
                yield OptionList(id="questions")
            yield Static("", id="detail")
        yield Footer()

    def action_refresh(self) -> None:
        self._render()

    def action_context_help(self) -> None:
        self.app.push_screen(  # type: ignore[attr-defined]
            ContextHelpScreen(
                title="Move Questions: Selection",
                lines=[
                    "Toggle one or many QIDs for the move operation.",
                    "Use 'Continue to placement' after selecting QIDs.",
                    "This screen does not leave Textual.",
                ],
            )
        )

    def on_mount(self) -> None:
        state: MoveQuestionState = self.app.move_question_state  # type: ignore[attr-defined]
        qfilter = self.query_one("#question_filter", Input)
        qfilter.value = state.filter_query or ""
        self._render()

    def _filtered_qids(self, query: str) -> list[str]:
        state: MoveQuestionState = self.app.move_question_state  # type: ignore[attr-defined]
        raw = (query or "").strip()
        if not raw:
            return list(state.ordered_qids)
        try:
            pattern = re.compile(raw, re.IGNORECASE)
        except re.error:
            pattern = re.compile(re.escape(raw), re.IGNORECASE)
        out: list[str] = []
        for qid in state.ordered_qids:
            label = state.question_labels.get(qid, qid)
            if pattern.search(qid) or pattern.search(label):
                out.append(qid)
        return out

    def _render(self, *, message: str | None = None) -> None:
        state: MoveQuestionState = self.app.move_question_state  # type: ignore[attr-defined]
        options = self.query_one("#questions", OptionList)
        options.clear_options()
        detail = self.query_one("#detail", Static)
        if not state.survey_id:
            detail.update("No survey selected.")
            return

        visible_qids = self._filtered_qids(state.filter_query)
        self._visible_qids = visible_qids

        options.add_option(f"Continue to placement ({len(state.selected_qids)} selected)")
        options.add_option("Select all visible")
        options.add_option("Clear selection")
        for qid in visible_qids:
            mark = "[x]" if qid in state.selected_qids else "[ ]"
            options.add_option(f"{mark} {state.question_labels.get(qid, qid)}")
        options.add_option("← Back")

        lines = [
            f"Survey: {state.survey_id}",
            f"Selected QIDs: {len(state.selected_qids)}",
            "",
            "Use Enter to toggle QIDs. Continue when selection is ready.",
            "",
            *(["Last result:", message, ""] if message else []),
            *(["State:", state.last_result or "", ""] if state.last_result else []),
            *_account_context_lines(),
        ]
        detail.update("\n".join([line for line in lines if line is not None]))

    def on_input_changed(self, event: Input.Changed) -> None:  # type: ignore[name-defined]
        if str(getattr(event, "input", None).id or "") != "question_filter":
            return
        state: MoveQuestionState = self.app.move_question_state  # type: ignore[attr-defined]
        state.filter_query = str(getattr(event, "value", ""))
        self._render()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:  # type: ignore[name-defined]
        idx = getattr(event, "option_index", None)
        visible_qids = getattr(self, "_visible_qids", [])
        if idx is None:
            return
        if idx < 3 or idx >= 3 + len(visible_qids):
            return
        qid = visible_qids[idx - 3]
        state: MoveQuestionState = self.app.move_question_state  # type: ignore[attr-defined]
        detail = self.query_one("#detail", Static)
        question = {}
        if isinstance(state.definition, dict):
            questions = state.definition.get("Questions")
            if isinstance(questions, dict):
                payload = questions.get(qid)
                if isinstance(payload, dict):
                    question = payload
        detail.update(
            "\n".join(
                [
                    state.question_labels.get(qid, qid),
                    "",
                    f"DataExportTag: {str(question.get('DataExportTag') or '-').strip() or '-'}",
                    f"Type: {str(question.get('QuestionType') or '-').strip() or '-'}",
                    f"Selector: {str(question.get('Selector') or '-').strip() or '-'}",
                    "",
                    f"Currently selected: {'yes' if qid in state.selected_qids else 'no'}",
                    "",
                    "Press Enter to toggle this QID.",
                ]
            )
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:  # type: ignore[name-defined]
        idx = getattr(event, "option_index", None)
        visible_qids = getattr(self, "_visible_qids", [])
        state: MoveQuestionState = self.app.move_question_state  # type: ignore[attr-defined]
        if idx is None:
            return
        if idx == 0:
            if not state.selected_qids:
                self._render(message="Select at least one QID before continuing.")
                return
            self.app.push_screen("moveq_place")  # type: ignore[attr-defined]
            return
        if idx == 1:
            for qid in visible_qids:
                if qid not in state.selected_qids:
                    state.selected_qids.append(qid)
            self._render(message=f"Selected {len(visible_qids)} visible QID(s).")
            return
        if idx == 2:
            state.selected_qids = []
            self._render(message="Cleared selected QIDs.")
            return
        if idx == 3 + len(visible_qids):
            self.app.pop_screen()  # type: ignore[attr-defined]
            return
        if idx < 3 or idx >= 3 + len(visible_qids):
            return
        qid = visible_qids[idx - 3]
        if qid in state.selected_qids:
            state.selected_qids = [item for item in state.selected_qids if item != qid]
            self._render(message=f"Unselected {qid}.")
            return
        state.selected_qids.append(qid)
        self._render(message=f"Selected {qid}.")


class MoveQuestionPlacementScreen(Screen):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
        ("?", "app.push_screen('help')", "Help"),
        ("h", "context_help", "Screen Help"),
        ("r", "refresh", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield OptionList(id="placement")
            yield Static("", id="detail")
        yield Footer()

    def action_refresh(self) -> None:
        self._render()

    def action_context_help(self) -> None:
        self.app.push_screen(  # type: ignore[attr-defined]
            ContextHelpScreen(
                title="Move Questions: Placement",
                lines=[
                    "Choose where selected QIDs should move.",
                    "Anchor choices set after/before semantics.",
                    "Block choices set explicit target block + prepend/append.",
                ],
            )
        )

    def on_mount(self) -> None:
        self._render()

    def _render(self, *, message: str | None = None) -> None:
        state: MoveQuestionState = self.app.move_question_state  # type: ignore[attr-defined]
        menu = self.query_one("#placement", OptionList)
        menu.clear_options()
        menu.add_option("Use auto target block (append)")
        menu.add_option("Use auto target block (prepend)")
        menu.add_option("Place after anchor question")
        menu.add_option("Place before anchor question")
        menu.add_option("Place in specific block (append)")
        menu.add_option("Place in specific block (prepend)")
        menu.add_option("Continue to run options")
        menu.add_option("← Back")

        selected = ", ".join(state.selected_qids[:6]) if state.selected_qids else "(none)"
        if len(state.selected_qids) > 6:
            selected += f" … +{len(state.selected_qids) - 6}"
        lines = [
            f"Survey: {state.survey_id or '(none)'}",
            f"Selected QIDs: {selected}",
            "",
            f"Placement: {state.placement_summary()}",
            "",
            *(["Last result:", message, ""] if message else []),
            *(["State:", state.last_result or "", ""] if state.last_result else []),
            "Continue to run options when placement is ready.",
        ]
        self.query_one("#detail", Static).update("\n".join([line for line in lines if line is not None]))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:  # type: ignore[name-defined]
        idx = getattr(event, "option_index", None)
        if idx is None:
            return
        state: MoveQuestionState = self.app.move_question_state  # type: ignore[attr-defined]
        if idx == 0:
            state.after_qid = None
            state.before_qid = None
            state.target_block_id = None
            state.position = "append"
            state.last_result = "Placement updated: auto append."
            self._render()
            return
        if idx == 1:
            state.after_qid = None
            state.before_qid = None
            state.target_block_id = None
            state.position = "prepend"
            state.last_result = "Placement updated: auto prepend."
            self._render()
            return
        if idx == 2:
            state.anchor_mode = "after"
            self.app.push_screen("moveq_anchor")  # type: ignore[attr-defined]
            return
        if idx == 3:
            state.anchor_mode = "before"
            self.app.push_screen("moveq_anchor")  # type: ignore[attr-defined]
            return
        if idx == 4:
            state.block_pick_position = "append"
            self.app.push_screen("moveq_block")  # type: ignore[attr-defined]
            return
        if idx == 5:
            state.block_pick_position = "prepend"
            self.app.push_screen("moveq_block")  # type: ignore[attr-defined]
            return
        if idx == 6:
            self.app.push_screen("moveq_run")  # type: ignore[attr-defined]
            return
        self.app.pop_screen()  # type: ignore[attr-defined]


class MoveQuestionAnchorScreen(Screen):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
        ("?", "app.push_screen('help')", "Help"),
        ("h", "context_help", "Screen Help"),
        ("r", "refresh", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical():
                yield Input(
                    placeholder="Filter anchor questions by QID/text",
                    id="anchor_filter",
                )
                yield OptionList(id="anchors")
            yield Static("", id="detail")
        yield Footer()

    def action_refresh(self) -> None:
        self._render()

    def action_context_help(self) -> None:
        self.app.push_screen(  # type: ignore[attr-defined]
            ContextHelpScreen(
                title="Move Questions: Anchor Picker",
                lines=[
                    "Pick an anchor question for after/before placement.",
                    "Selected moved QIDs are excluded from anchor candidates.",
                ],
            )
        )

    def on_mount(self) -> None:
        self._render()

    def _filtered_qids(self, query: str) -> list[str]:
        state: MoveQuestionState = self.app.move_question_state  # type: ignore[attr-defined]
        candidates = [qid for qid in state.ordered_qids if qid not in state.selected_qids]
        raw = (query or "").strip()
        if not raw:
            return candidates
        try:
            pattern = re.compile(raw, re.IGNORECASE)
        except re.error:
            pattern = re.compile(re.escape(raw), re.IGNORECASE)
        out: list[str] = []
        for qid in candidates:
            label = state.question_labels.get(qid, qid)
            if pattern.search(qid) or pattern.search(label):
                out.append(qid)
        return out

    def _render(self) -> None:
        state: MoveQuestionState = self.app.move_question_state  # type: ignore[attr-defined]
        menu = self.query_one("#anchors", OptionList)
        menu.clear_options()
        qfilter = self.query_one("#anchor_filter", Input)
        query = str(qfilter.value or "")
        visible_qids = self._filtered_qids(query)
        self._visible_qids = visible_qids
        for qid in visible_qids:
            menu.add_option(state.question_labels.get(qid, qid))
        menu.add_option("← Back")
        mode = state.anchor_mode or "after"
        self.query_one("#detail", Static).update(
            "\n".join(
                [
                    f"Anchor mode: {mode}",
                    "",
                    f"Candidates: {len(visible_qids)}",
                    "Select an anchor question.",
                ]
            )
        )

    def on_input_changed(self, event: Input.Changed) -> None:  # type: ignore[name-defined]
        if str(getattr(event, "input", None).id or "") != "anchor_filter":
            return
        self._render()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:  # type: ignore[name-defined]
        idx = getattr(event, "option_index", None)
        visible_qids = getattr(self, "_visible_qids", [])
        if idx is None:
            return
        if idx == len(visible_qids):
            self.app.pop_screen()  # type: ignore[attr-defined]
            return
        if idx < 0 or idx >= len(visible_qids):
            return
        qid = visible_qids[idx]
        state: MoveQuestionState = self.app.move_question_state  # type: ignore[attr-defined]
        if state.anchor_mode == "before":
            state.before_qid = qid
            state.after_qid = None
            state.target_block_id = None
            state.last_result = f"Placement updated: before {qid}."
        else:
            state.after_qid = qid
            state.before_qid = None
            state.target_block_id = None
            state.last_result = f"Placement updated: after {qid}."
        self.app.pop_screen()  # type: ignore[attr-defined]


class MoveQuestionBlockScreen(Screen):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
        ("?", "app.push_screen('help')", "Help"),
        ("h", "context_help", "Screen Help"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield OptionList(id="blocks")
            yield Static("", id="detail")
        yield Footer()

    def action_context_help(self) -> None:
        self.app.push_screen(  # type: ignore[attr-defined]
            ContextHelpScreen(
                title="Move Questions: Block Picker",
                lines=[
                    "Pick a destination block for the selected QIDs.",
                    "Placement mode (append/prepend) is kept from prior selection.",
                ],
            )
        )

    def on_mount(self) -> None:
        state: MoveQuestionState = self.app.move_question_state  # type: ignore[attr-defined]
        menu = self.query_one("#blocks", OptionList)
        menu.clear_options()
        block_ids = list(state.block_labels.keys())
        self._block_ids = block_ids
        for bid in block_ids:
            menu.add_option(state.block_labels.get(bid, bid))
        menu.add_option("← Back")
        self.query_one("#detail", Static).update(
            "\n".join(
                [
                    f"Blocks: {len(block_ids)}",
                    f"Mode: {state.block_pick_position}",
                    "",
                    "Select target block.",
                ]
            )
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:  # type: ignore[name-defined]
        idx = getattr(event, "option_index", None)
        block_ids = getattr(self, "_block_ids", [])
        if idx is None:
            return
        if idx == len(block_ids):
            self.app.pop_screen()  # type: ignore[attr-defined]
            return
        if idx < 0 or idx >= len(block_ids):
            return
        state: MoveQuestionState = self.app.move_question_state  # type: ignore[attr-defined]
        bid = block_ids[idx]
        state.target_block_id = bid
        state.position = state.block_pick_position or "append"
        state.after_qid = None
        state.before_qid = None
        state.last_result = f"Placement updated: {state.position} in block {bid}."
        self.app.pop_screen()  # type: ignore[attr-defined]


class MoveQuestionRunScreen(Screen):
    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
        ("?", "app.push_screen('help')", "Help"),
        ("h", "context_help", "Screen Help"),
        ("r", "refresh", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield OptionList(id="actions")
            yield Static("", id="detail")
        yield Footer()

    def action_refresh(self) -> None:
        self._render()

    def action_context_help(self) -> None:
        self.app.push_screen(  # type: ignore[attr-defined]
            ContextHelpScreen(
                title="Move Questions: Run Options",
                lines=[
                    "Toggle dry-run/force-live/publish settings.",
                    "Then run inside TUI without dropping into questionary prompts.",
                    "Equivalent CLI command is always shown.",
                ],
            )
        )

    def on_mount(self) -> None:
        self._render()

    def _render(self, *, message: str | None = None) -> None:
        state: MoveQuestionState = self.app.move_question_state  # type: ignore[attr-defined]
        menu = self.query_one("#actions", OptionList)
        menu.clear_options()
        menu.add_option(f"Dry run: {'ON' if state.dry_run else 'OFF'}")
        menu.add_option(f"Force live: {'ON' if state.force_live else 'OFF'}")
        menu.add_option(f"Publish after move: {'ON' if state.publish else 'OFF'}")
        menu.add_option("Set publish description")
        menu.add_option("Run move-question now")
        menu.add_option("Show equivalent command")
        menu.add_option("← Back")

        detail_lines = [
            f"Survey: {state.survey_id or '(none)'}",
            f"Selected QIDs: {', '.join(state.selected_qids) if state.selected_qids else '(none)'}",
            f"Placement: {state.placement_summary()}",
            "",
            f"Publish description: {state.publish_description.strip() or '(auto)'}",
            "",
            "Command preview:",
            state.command(),
            "",
        ]
        if message:
            detail_lines.extend(["Last result:", message, ""])
        if state.last_result:
            detail_lines.extend(["State:", state.last_result, ""])
        detail_lines.append("Tip: run dry-run first, then live.")
        self.query_one("#detail", Static).update("\n".join(detail_lines))

    def _prompt_publish_description(self) -> str | None:
        with self.app.suspend():  # type: ignore[attr-defined]
            try:
                return input("Publish description (blank clears): ")
            except EOFError:
                return None

    def _run_move(self) -> str:
        state: MoveQuestionState = self.app.move_question_state  # type: ignore[attr-defined]
        if not state.survey_id:
            return "No survey selected."
        if not state.selected_qids:
            return "No QIDs selected."
        argv = state.argv()
        with self.app.suspend():  # type: ignore[attr-defined]
            print(f"\n[qsync:tui] Running: qsync {shlex.join(argv)}")

            def _runner() -> int:
                return _run_qsync_cli_subcommand(argv)

            code, elapsed = _timed_call(_runner)
            print(f"[qsync:tui] move-question exit={code} in {elapsed}")
        if code == 0:
            return f"Completed successfully in {elapsed}."
        return f"Exited with code {code} after {elapsed}."

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:  # type: ignore[name-defined]
        idx = getattr(event, "option_index", None)
        if idx is None:
            return
        state: MoveQuestionState = self.app.move_question_state  # type: ignore[attr-defined]
        if idx == 0:
            state.dry_run = not state.dry_run
            if state.dry_run:
                state.force_live = False
            self._render(message=f"Dry run set to {state.dry_run}.")
            return
        if idx == 1:
            if state.dry_run:
                self._render(message="Force live applies only to live runs.")
                return
            state.force_live = not state.force_live
            self._render(message=f"Force live set to {state.force_live}.")
            return
        if idx == 2:
            if state.dry_run:
                self._render(message="Publish is ignored in dry-run mode.")
                return
            state.publish = not state.publish
            self._render(message=f"Publish set to {state.publish}.")
            return
        if idx == 3:
            typed = self._prompt_publish_description()
            if typed is None:
                self._render(message="Publish description unchanged.")
                return
            state.publish_description = str(typed).strip()
            self._render(message="Updated publish description.")
            return
        if idx == 4:
            try:
                message = self._run_move()
            except Exception as exc:
                message = f"ERROR running move-question: {exc}"
            state.last_result = message
            self._render(message=message)
            return
        if idx == 5:
            self._render(message=state.command())
            return
        self.app.pop_screen()  # type: ignore[attr-defined]


class PullSurveyScreen(Screen):
    SHARED_PICKER_LABEL = "Open shared picker (details/manual/regex)"

    BINDINGS = [
        ("b", "app.pop_screen", "Back"),
        ("escape", "app.pop_screen", "Back"),
        ("q", "app.quit", "Quit"),
        ("?", "app.push_screen('help')", "Help"),
        ("h", "context_help", "Screen Help"),
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
        surveys_widget.add_option(self.SHARED_PICKER_LABEL)
        if not rows:
            return
        for row in rows:
            sid = str(row.get("id") or "").strip()
            if not sid:
                continue
            surveys_widget.add_option(_survey_option_label(row))

    def action_context_help(self) -> None:
        self.app.push_screen(  # type: ignore[attr-defined]
            ContextHelpScreen(
                title="Pull Survey Picker Help",
                lines=[
                    "Use native filter list or the shared picker option.",
                    "Shared picker includes top-30 details and manual SurveyID entry.",
                    "Pull is read-only and caches survey definitions locally.",
                ],
            )
        )

    def _run_shared_picker(self) -> None:
        from qsync.survey_selection import pick_survey_id_from_records

        rows = list(getattr(self, "_rows", []) or [])
        if not rows:
            return
        with self.app.suspend():  # type: ignore[attr-defined]
            picked = pick_survey_id_from_records(
                message="Pick a survey to pull:",
                records=rows,
                include_back=True,
                include_manual=True,
                include_details=True,
            )
        if not picked:
            return
        for row in rows:
            if str(row.get("id") or "").strip() == picked:
                self._select_survey(row)
                return
        self._select_survey({"id": picked, "name": picked})

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
        if idx is None:
            return
        if idx == 0:
            detail = self.query_one("#detail", Static)
            detail.update(
                "\n".join(
                    [
                        self.SHARED_PICKER_LABEL,
                        "",
                        "Uses shared CLI picker semantics (details/manual/regex).",
                    ]
                )
            )
            return
        if not rows or idx < 1 or idx > len(rows):
            return
        s = rows[idx - 1]
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
        if idx is None:
            return
        if idx == 0:
            self._run_shared_picker()
            return
        if rows is None or idx < 1 or idx > len(rows):
            return
        self._select_survey(rows[idx - 1])

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
                resolve_survey_cache_dir,
            )
            from qsync.qualtrics_client import download_survey_definition

            root = resolve_root(required=False) or Path.cwd()
            account = None
            try:
                account = get_active_account()
            except Exception:
                account = None
            env = load_account_env(account, root=root) if account else None
            dest = resolve_survey_cache_dir(root=root, account=account)

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

        actions.add_option("➕ Add another edit (external wizard)")
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
                from pathlib import Path
                from qsync.config import resolve_root

                with self.app.suspend():  # type: ignore[attr-defined]
                    cmd = [sys.executable, "-m", "qsync.cli"]
                    root = resolve_root(required=False) or Path.cwd()
                    cmd.extend(["--root", str(root)])
                    cmd.extend(["items", "edit", "--survey-id", survey_id])
                    result = subprocess.run(cmd, check=False)
                    if result.returncode != 0:
                        print(
                            f"[qsync:tui] External edit wizard exited with code {result.returncode}."
                        )
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
