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
from pathlib import Path
from typing import List, Optional

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
    choices: List[str],
    instruction: Optional[str] = None,
) -> Optional[str]:
    """Display arrow-key navigable selection menu.

    Args:
        message: Prompt message
        choices: List of choice strings
        instruction: Optional instruction text

    Returns:
        Selected choice string, or None if cancelled
    """
    if not should_use_questionary():
        return _fallback_select(message, choices)

    if not is_interactive():
        return _fallback_select(message, choices)

    try:
        # Save terminal state before questionary
        import termios

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)

        # Convert separator strings to questionary.Separator objects
        from questionary import Separator

        processed_choices = []
        for choice in choices:
            if _is_separator(choice):
                processed_choices.append(Separator(choice))
            else:
                processed_choices.append(choice)

        try:
            result = questionary.select(
                message=message,
                choices=processed_choices,
                instruction=instruction,
                use_shortcuts=False,
                use_arrow_keys=True,
                use_jk_keys=False,
                style=CUSTOM_STYLE,
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
        return _fallback_select(message, choices)


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
) -> Optional[str]:
    """Prompt for free-form text input.

    Uses questionary when available, otherwise falls back to stdin input().
    """

    if not should_use_questionary():
        return _fallback_text_input(message, default=default)

    if not is_interactive():
        return _fallback_text_input(message, default=default)

    try:
        import termios

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            result = questionary.text(
                message=message,
                default=default or "",
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
        return _fallback_text_input(message, default=default)


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
    message: str, *, default: Optional[str] = None
) -> Optional[str]:
    prompt = f"{message}"
    if default:
        prompt += f" [{default}]"
    prompt += ": "
    try:
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


def _fallback_select(message: str, choices: List[str]) -> Optional[str]:
    """Fallback selection using simple input."""
    print(f"\n{message}")

    # Filter out separators for display
    displayable_choices = [
        (i, c) for i, c in enumerate(choices) if not c.startswith("─")
    ]

    for display_idx, (actual_idx, choice) in enumerate(displayable_choices, 1):
        print(f"  [{display_idx}] {choice}")

    try:
        response = input("\nEnter number (or 'q' to cancel): ").strip().lower()
        if not response or response == "q":
            return None

        display_idx = int(response) - 1
        if 0 <= display_idx < len(displayable_choices):
            actual_idx = displayable_choices[display_idx][0]
            return choices[actual_idx]
        else:
            print("Invalid selection")
            return None
    except (ValueError, KeyboardInterrupt, EOFError):
        return None


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
