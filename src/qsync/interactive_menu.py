"""Interactive menu utilities for qsync.

Provides arrow-key navigable menus using questionary library,
with graceful fallback if questionary is unavailable.

Questionary should work in VSCode terminals, but if you experience
terminal issues, you can disable it with:
    export QSYNC_USE_QUESTIONARY=0
"""

import os
import sys
import tempfile
import subprocess
import shlex
import shutil
import getpass
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Sequence

# Try to import questionary, fall back if not available
try:
    import questionary
    from questionary import Style

    QUESTIONARY_AVAILABLE = True
except ImportError:
    QUESTIONARY_AVAILABLE = False

# Custom style with highlighted selection and colored pointer
CUSTOM_STYLE = (
    Style(
        [
            ("qmark", "fg:#5f87ff bold"),  # Question mark - blue
            ("question", "fg:#ffffff bold"),  # Question text - white bold
            ("answer", "fg:#2ecc71 bold"),  # Selected answer - green
            ("pointer", "fg:#2ecc71 bold"),  # Pointer arrow - green
            ("highlighted", "fg:#2ecc71 bold"),  # Currently highlighted option - green
            ("selected", "fg:#2ecc71"),  # Selected option - green
            ("separator", "fg:#cc5454"),  # Separator - red
            ("instruction", "fg:#858585"),  # Instructions - gray
            ("text", ""),  # Plain text
            ("disabled", "fg:#858585 italic"),  # Disabled options - gray italic
        ]
    )
    if QUESTIONARY_AVAILABLE
    else None
)

_DEFAULT_INSTRUCTION = "↑/↓ to move, Enter to select, Ctrl+C to cancel"


@dataclass(frozen=True)
class MenuItem:
    """Structured menu entry for consistent questionary + fallback behavior."""

    label: str
    value: str | None = None
    enabled: bool = True
    disabled_reason: str | None = None
    kind: str = "option"  # "option" | "separator"

    @staticmethod
    def separator(label: str = "─" * 40) -> "MenuItem":
        return MenuItem(label=label, value=None, enabled=False, kind="separator")


def _coerce_menu_items(choices: Sequence[str | MenuItem]) -> list[MenuItem]:
    items: list[MenuItem] = []
    for choice in choices:
        if isinstance(choice, MenuItem):
            items.append(choice)
            continue
        # Treat empty/whitespace-only entries as visual separators (never selectable).
        if not str(choice).strip():
            items.append(MenuItem.separator(""))
            continue
        if _is_separator(choice):
            items.append(MenuItem.separator(choice))
        else:
            items.append(MenuItem(label=choice, value=choice))
    return items


def should_use_questionary() -> bool:
    """Decide whether to use questionary for menus (runtime check).

    This is evaluated at call time (not import time) to avoid false negatives
    in environments where TTY availability changes (or when modules are imported
    during shell completion).
    """

    if not QUESTIONARY_AVAILABLE:
        return False

    raw = os.environ.get("QSYNC_USE_QUESTIONARY", "auto").strip().lower()
    if raw in {"0", "false", "no"}:
        return False
    if raw in {"1", "true", "yes"}:
        return True
    # "auto" / unset: enable only when interactive.
    return is_interactive()


def is_interactive() -> bool:
    """Check if session is interactive (TTY)."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _is_separator(choice: str) -> bool:
    """Check if a choice string is a separator line."""
    stripped = choice.strip()
    if not stripped:
        return False
    sep_chars = set("─═—-_")
    # Pure line separators (legacy behavior)
    if len(stripped) > 2 and all(c in sep_chars for c in stripped):
        return True
    # "Titled" separators like: "── Focal surveys ──"
    if len(stripped) > 6 and stripped[0] in sep_chars and stripped[-1] in sep_chars:
        has_sep = any(c in sep_chars for c in stripped)
        has_text = any(c.isalnum() for c in stripped)
        return has_sep and has_text
    return False


def select_from_list(
    message: str,
    choices: List[str] | List[MenuItem],
    instruction: Optional[str] = None,
    default: Optional[str] = None,
) -> Optional[str]:
    """Display arrow-key navigable selection menu.

    Args:
        message: Prompt message
        choices: List of choice strings
        instruction: Optional instruction text

    Returns:
        Selected choice string, or None if cancelled
    """
    items = _coerce_menu_items(choices)
    instruction = _fit_instruction(instruction or _DEFAULT_INSTRUCTION)

    # Make fallback "Enter" behave like questionary: pick a stable default.
    effective_default = default
    if effective_default is None:
        for item in items:
            if item.kind != "separator" and item.enabled:
                effective_default = item.value or item.label
                break

    if not should_use_questionary():
        return _fallback_select_items(
            message,
            items,
            default=effective_default,
            instruction=instruction,
        )

    if not is_interactive():
        return _fallback_select_items(
            message,
            items,
            default=effective_default,
            instruction=instruction,
        )

    try:
        # Save terminal state before questionary
        import termios

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)

        # Convert to questionary choices with disabled reasons.
        from questionary import Separator

        processed_choices: list[Any] = []
        for item in items:
            if item.kind == "separator":
                processed_choices.append(Separator(item.label))
                continue
            disabled = None
            if not item.enabled:
                disabled = item.disabled_reason or "unavailable"
            processed_choices.append(
                questionary.Choice(
                    title=item.label,
                    value=item.value or item.label,
                    disabled=disabled,
                )
            )

        try:
            result = questionary.select(
                message=message,
                choices=processed_choices,
                instruction=instruction,
                use_shortcuts=False,
                use_arrow_keys=True,
                use_jk_keys=False,
                style=CUSTOM_STYLE,
                default=effective_default,
            ).ask()
            return result
        finally:
            # Restore terminal state
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    except (KeyboardInterrupt, EOFError):
        return None
    except Exception as e:
        # Fall back to simple selection if questionary fails
        print(f"\n(Arrow key menu failed: {e})")
        print("(Falling back to numbered selection)")
        return _fallback_select_items(
            message,
            items,
            default=effective_default,
            instruction=instruction,
        )


def _fit_instruction(instruction: str) -> str:
    """Fit instruction text to terminal width (questionary renders it as a single line)."""
    try:
        width = shutil.get_terminal_size(fallback=(80, 24)).columns
    except Exception:
        width = 80
    width = max(40, int(width))
    text = (instruction or "").strip()
    if not text:
        return _DEFAULT_INSTRUCTION
    if len(text) <= width - 4:
        return text
    return text[: max(0, width - 7)].rstrip() + "..."


def confirm(
    message: str,
    default: bool = True,
    yes_text: str = "Yes",
    no_text: str = "No",
) -> bool:
    """Display yes/no confirmation prompt.

    Args:
        message: Prompt message
        default: Default choice
        yes_text: Text for yes option
        no_text: Text for no option

    Returns:
        True if yes, False if no
    """
    if not should_use_questionary():
        return _fallback_confirm(message, default)

    if not is_interactive():
        return _fallback_confirm(message, default)

    try:
        # Save terminal state
        import termios

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)

        try:
            result = questionary.confirm(
                message=message,
                default=default,
                auto_enter=False,  # Require explicit confirmation
                style=CUSTOM_STYLE,
            ).ask()
            return result if result is not None else False
        finally:
            # Restore terminal state
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    except (KeyboardInterrupt, EOFError):
        return False
    except Exception:
        return _fallback_confirm(message, default)


def select_action(
    message: str,
    approve_text: str = "✓ Approve and continue",
    skip_text: str = "✗ Skip this step",
    cancel_text: str = "↩ Cancel workflow",
) -> Optional[str]:
    """Display action selection menu for approval workflows.

    Args:
        message: Prompt message
        approve_text: Text for approve option
        skip_text: Text for skip option
        cancel_text: Text for cancel option

    Returns:
        "approve", "skip", or None for cancel
    """
    choices = [
        approve_text,
        skip_text,
        cancel_text,
    ]

    result = select_from_list(message, choices)

    if result is None or cancel_text in result:
        return None
    elif approve_text in result:
        return "approve"
    elif skip_text in result:
        return "skip"
    else:
        return None


def text_input(
    message: str,
    *,
    default: Optional[str] = None,
    instruction: Optional[str] = None,
    validator: Any | None = None,
    validate_while_typing: bool = False,
    secret: bool = False,
) -> Optional[str]:
    """Prompt for free-form text input.

    Uses questionary when available, otherwise falls back to stdin input().
    """

    def _validate_fallback(value: str) -> bool:
        if validator is None:
            return True

        # Prefer questionary-style validators (Validator.validate(document) -> None)
        # and allow simple callables (value -> bool/str) as a fallback.
        try:
            from questionary import ValidationError  # type: ignore[import-not-found]
        except Exception:
            ValidationError = Exception  # type: ignore[assignment]

        if hasattr(validator, "validate"):
            class _Doc:
                def __init__(self, text: str) -> None:
                    self.text = text

            try:
                validator.validate(_Doc(value))
                return True
            except ValidationError as exc:  # type: ignore[misc]
                print(f"(Invalid input: {exc})")
                return False
            except Exception as exc:  # noqa: BLE001
                print(f"(Invalid input: {exc})")
                return False

        if callable(validator):
            try:
                out = validator(value)
            except Exception as exc:  # noqa: BLE001
                print(f"(Invalid input: {exc})")
                return False
            if out is True or out is None:
                return True
            if out is False:
                print("(Invalid input)")
                return False
            if isinstance(out, str):
                print(f"(Invalid input: {out})")
                return False
            return True

        return True

    if not should_use_questionary() or not is_interactive():
        # Fallback mode: validate after entry and optionally re-prompt in TTY.
        while True:
            val = _fallback_text_input(message, default=default, secret=secret)
            if val is None:
                return None
            if _validate_fallback(val):
                return val
            if not is_interactive():
                return None

    try:
        import termios

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            if secret:
                result = questionary.password(
                    message=message,
                    instruction=instruction,
                    style=CUSTOM_STYLE,
                ).ask()
            else:
                result = questionary.text(
                    message=message,
                    default=default or "",
                    instruction=instruction,
                    validate=validator,
                    validate_while_typing=validate_while_typing,
                    style=CUSTOM_STYLE,
                ).ask()
            if result is None:
                return None
            return str(result)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    except (KeyboardInterrupt, EOFError):
        return None
    except Exception as e:
        print(f"\n(Text input failed: {e})")
        return _fallback_text_input(message, default=default, secret=secret)


def autocomplete_from_list(
    message: str,
    choices: List[str],
    instruction: Optional[str] = None,
) -> Optional[str]:
    """Prompt with autocomplete over a list of choices.

    Uses questionary when available, otherwise falls back to text input and matching.
    """
    if not choices:
        return None
    if not should_use_questionary():
        return _fallback_autocomplete(message, choices, instruction=instruction)
    if not is_interactive():
        return _fallback_autocomplete(message, choices, instruction=instruction)

    try:
        import termios

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        prompt_message = message
        if instruction:
            prompt_message = f"{message} ({instruction})"
        try:
            result = questionary.autocomplete(
                message=prompt_message,
                choices=choices,
                ignore_case=True,
                match_middle=True,
                style=CUSTOM_STYLE,
            ).ask()
            return result
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    except (KeyboardInterrupt, EOFError):
        return None
    except Exception as e:
        print(f"\n(Autocomplete failed: {e})")
        return _fallback_autocomplete(message, choices, instruction=instruction)


def multi_select_from_list(
    message: str,
    choices: List[str],
    instruction: Optional[str] = None,
    default: Optional[List[str]] = None,
) -> Optional[List[str]]:
    """Prompt for multiple selections from a list.

    Returns a list of selected choice strings, or None if cancelled.
    """
    if not choices:
        return []
    if not should_use_questionary():
        return _fallback_multi_select(message, choices, instruction=instruction)
    if not is_interactive():
        return _fallback_multi_select(message, choices, instruction=instruction)

    try:
        import termios

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        prompt_message = message
        if instruction:
            prompt_message = f"{message} ({instruction})"

        # questionary.checkbox only supports a single `default` value; for multi-select
        # preselection, we must mark individual choices as checked.
        processed = []
        default_set = set(default or [])
        for choice in choices:
            if choice in default_set:
                processed.append(questionary.Choice(choice, checked=True))
            else:
                processed.append(choice)

        try:
            result = questionary.checkbox(
                message=prompt_message,
                choices=processed,
                style=CUSTOM_STYLE,
            ).ask()
            if result is None:
                return None
            return list(result)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    except (KeyboardInterrupt, EOFError):
        return None
    except Exception as e:
        print(f"\n(Multi-select failed: {e})")
        return _fallback_multi_select(message, choices, instruction=instruction)


def edit_text_in_editor(
    message: str,
    *,
    initial_text: str = "",
    suffix: str = ".txt",
) -> Optional[str]:
    """Open $EDITOR (or a sensible fallback) for multiline text editing.

    Returns the edited text, or None if the user cancels (empty + confirm).
    """

    if not is_interactive():
        return _fallback_text_input(message, default=initial_text)

    editor = (os.environ.get("VISUAL") or os.environ.get("EDITOR") or "").strip()
    if not editor:
        # Reasonable defaults on macOS/Linux; final fallback is single-line.
        for candidate in ("nano", "vi"):
            if shutil.which(candidate):
                editor = candidate
                break

    if not editor:
        print("(No $EDITOR found; falling back to single-line input.)")
        return text_input(message, default=initial_text)

    print(f"{message} (opening editor: {editor})")
    with tempfile.NamedTemporaryFile(
        "w+", suffix=suffix, delete=False, encoding="utf-8"
    ) as tf:
        path = Path(tf.name)
        tf.write(initial_text or "")
        tf.flush()

    try:
        try:
            cmd = shlex.split(editor) + [str(path)]
            subprocess.run(cmd, check=False)
        except FileNotFoundError:
            print(f"(Editor not found: {editor}; falling back to single-line input.)")
            return text_input(message, default=initial_text)

        new_text = path.read_text(encoding="utf-8")
    finally:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass

    # Confirm if user left it empty, to avoid accidental wipes.
    if not (new_text or "").strip():
        if not confirm("You entered empty text. Save empty value?", default=False):
            return None
    return new_text


def _fallback_text_input(
    message: str, *, default: Optional[str] = None, secret: bool = False
) -> Optional[str]:
    prompt = f"{message}"
    if default:
        prompt += f" [{default}]"
    prompt += ": "
    try:
        if secret:
            val = getpass.getpass(prompt)
        else:
            val = input(prompt)
    except (KeyboardInterrupt, EOFError):
        return None
    val = val.strip()
    if not val and default is not None:
        return default
    return val or None


def _fallback_autocomplete(
    message: str,
    choices: List[str],
    instruction: Optional[str] = None,
) -> Optional[str]:
    prompt = message
    if instruction:
        prompt = f"{message} ({instruction})"
    raw = _fallback_text_input(prompt)
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if raw in choices:
        return raw
    lowered = raw.lower()
    matches = [c for c in choices if lowered in c.lower()]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    return _fallback_select(f"{message} (matches)", matches)


def _fallback_multi_select(
    message: str,
    choices: List[str],
    instruction: Optional[str] = None,
) -> Optional[List[str]]:
    print(f"\n{message}")
    if instruction:
        print(f"({instruction})")
    for idx, choice in enumerate(choices, 1):
        print(f"  [{idx}] {choice}")

    try:
        raw = input(
            "\nEnter numbers (comma-separated, blank for none, 'q' to cancel): "
        )
    except (KeyboardInterrupt, EOFError):
        return None
    raw = raw.strip().lower()
    if raw in {"q", "quit", "cancel"}:
        return None
    if not raw:
        return []

    parts = [p.strip() for p in raw.split(",") if p.strip()]
    selected: List[str] = []
    for part in parts:
        if not part.isdigit():
            continue
        idx = int(part)
        if 1 <= idx <= len(choices):
            selected.append(choices[idx - 1])
    return selected


def _fallback_select(
    message: str, choices: List[str], default: Optional[str] = None
) -> Optional[str]:
    """Fallback selection using simple input (legacy string choices)."""
    items = _coerce_menu_items(choices)
    return _fallback_select_items(
        message,
        items,
        default=default,
        instruction=None,
    )


def _fallback_select_items(
    message: str,
    items: list[MenuItem],
    *,
    default: Optional[str],
    instruction: Optional[str],
) -> Optional[str]:
    """Fallback selection for structured menu items.

    Ensures:
    - separators are visible but never selectable
    - disabled options are visible with reasons and never selectable
    - cancelling returns None (not an implicit default)
    """
    print(f"\n{message}")
    if instruction:
        try:
            width = shutil.get_terminal_size(fallback=(80, 24)).columns
        except Exception:
            width = 80
        width = max(40, int(width))
        wrapped = textwrap.fill(str(instruction), width=width)
        print(f"({wrapped})")

    enabled_by_index: dict[int, str] = {}
    display_idx = 1

    for item in items:
        if item.kind == "separator":
            print(f"  {item.label}")
            continue

        if not item.enabled:
            reason = item.disabled_reason or "unavailable"
            print(f"  [ ] {item.label} ({reason})")
            continue

        enabled_by_index[display_idx] = item.value or item.label
        print(f"  [{display_idx}] {item.label}")
        display_idx += 1

    try:
        raw = input(
            "\nEnter number (blank for default, 'q' to cancel): "
        ).strip().lower()
    except (KeyboardInterrupt, EOFError):
        return None

    if raw in {"q", "quit", "cancel"}:
        return None
    if not raw:
        return default
    if not raw.isdigit():
        print("Invalid selection")
        return None

    idx = int(raw)
    if idx not in enabled_by_index:
        print("Invalid selection")
        return None
    return enabled_by_index[idx]


def _fallback_confirm(message: str, default: bool) -> bool:
    """Fallback confirmation using simple input."""
    default_str = "Y/n" if default else "y/N"
    try:
        response = input(f"{message} [{default_str}]: ").strip().lower()
        if not response:
            return default
        return response in ("y", "yes")
    except (KeyboardInterrupt, EOFError):
        return False


def print_install_hint():
    """Print hint about questionary status."""
    if not QUESTIONARY_AVAILABLE:
        print("\n💡 Tip: Install 'questionary' for arrow-key menus:")
        print("   pip install questionary")
    elif not should_use_questionary():
        print("\n💡 Arrow-key menus are available! To enable:")
        print("   export QSYNC_USE_QUESTIONARY=1")
        print("   (or set =0 to use numbered menus)")
