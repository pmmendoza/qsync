"""Terminal color support for qsync output.

Provides ANSI color codes for terminal output with automatic detection of
terminal support. Color output can be configured globally (auto/always/never).
"""

from __future__ import annotations

import os
import sys
from typing import Iterable, Literal


# ANSI color codes
class Colors:
    """ANSI color codes for terminal output."""

    # Text colors
    RED = "\033[91m"
    YELLOW = "\033[93m"
    GREEN = "\033[92m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    MAGENTA = "\033[95m"
    GRAY = "\033[90m"
    WHITE = "\033[97m"

    # Background colors
    BG_RED = "\033[41m"
    BG_YELLOW = "\033[43m"
    BG_GREEN = "\033[42m"

    # Text styles
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"


def _supports_color() -> bool:
    """Detect if terminal supports color output.

    Returns True if:
    - stdout is a TTY (not piped)
    - TERM environment variable is not 'dumb'
    """
    if not sys.stdout.isatty():
        return False

    term = os.environ.get("TERM", "").lower()
    if term == "dumb":
        return False

    return True


ColorMode = Literal["auto", "always", "never"]

_color_mode: ColorMode = "auto"
_color_enabled = _supports_color()


def set_color_mode(mode: ColorMode) -> None:
    """Set global color mode.

    Modes:
    - auto: enable only when stdout is a TTY and TERM is not dumb
    - always: force enable (except in JSON-only output paths, which should avoid color)
    - never: disable
    """
    global _color_mode, _color_enabled
    _color_mode = mode
    if mode == "never":
        _color_enabled = False
    elif mode == "always":
        _color_enabled = True
    else:
        _color_enabled = _supports_color()


def get_color_mode() -> ColorMode:
    return _color_mode


def colors_enabled() -> bool:
    return bool(_color_enabled)


def enable_colors(force: bool = True) -> None:
    """Enable color output globally."""
    global _color_enabled
    _color_enabled = force


def disable_colors() -> None:
    """Disable color output globally."""
    global _color_enabled
    _color_enabled = False


def colored(text: str, color: str, bold: bool = False, dim: bool = False) -> str:
    """Return colored text if colors are enabled.

    Args:
        text: Text to color
        color: Color code (e.g., Colors.RED)
        bold: If True, add bold styling
        dim: If True, add dim styling

    Returns:
        Colored text if enabled, plain text otherwise
    """
    if not _color_enabled:
        return text

    style_prefix = ""
    if bold:
        style_prefix += Colors.BOLD
    if dim:
        style_prefix += Colors.DIM
    result = f"{style_prefix}{color}{text}{Colors.RESET}"

    return result


def diff_colored(old: str, new: str, max_width: int = 60) -> tuple:
    """Format old and new values with color coding for diffs.

    Args:
        old: Old value
        new: New value
        max_width: Maximum width before truncation

    Returns:
        Tuple of (old_formatted, new_formatted) strings
    """
    # Truncate long values
    old_trunc = old[: max_width - 3] if len(old) > max_width else old
    new_trunc = new[: max_width - 3] if len(new) > max_width else new

    if len(old) > max_width:
        old_trunc += "..."
    if len(new) > max_width:
        new_trunc += "..."

    # Add color coding
    old_formatted = colored(old_trunc, Colors.GRAY)
    new_formatted = colored(new_trunc, Colors.GREEN)

    return old_formatted, new_formatted


def _highlight_inline_diff(
    old_text: str, new_text: str, is_addition: bool, color_code: str
) -> str:
    """Highlight character-level differences with bold using difflib.

    Args:
        old_text: Original text
        new_text: Modified text
        is_addition: True for + lines (highlight additions), False for - lines (highlight deletions)
        color_code: ANSI color code to maintain after bold resets

    Returns:
        Text with bold on changed characters only, maintaining color throughout
    """
    import difflib

    # Use SequenceMatcher for character-level diff
    matcher = difflib.SequenceMatcher(None, old_text, new_text)

    result = []

    if is_addition:
        # For + lines: show new text with additions bolded
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                result.append(new_text[j1:j2])
            elif tag == "insert" or tag == "replace":
                # Bold the new/changed text, then restore color
                result.append(
                    f"{Colors.BOLD}{new_text[j1:j2]}{Colors.RESET}{color_code}"
                )
            # skip 'delete' for additions
    else:
        # For - lines: show old text with deletions bolded
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                result.append(old_text[i1:i2])
            elif tag == "delete" or tag == "replace":
                # Bold the old/changed text, then restore color
                result.append(
                    f"{Colors.BOLD}{old_text[i1:i2]}{Colors.RESET}{color_code}"
                )
            # skip 'insert' for deletions

    return "".join(result)


def colorize_unified_diff_lines(lines: Iterable[str]) -> list[str]:
    """Colorize unified diff lines with inline character-level highlighting.

    Colors:
    - @@ hunks: cyan bold
    - +++ / --- headers: green/red bold
    - + lines: green (with bold on added characters)
    - - lines: red (with bold on deleted characters)
    - context: unchanged

    For consecutive -/+ pairs (modified lines), highlights the specific
    characters that changed with bold text.
    """
    lines_list = list(lines)
    out: list[str] = []
    i = 0

    while i < len(lines_list):
        line = lines_list[i]

        if line.startswith("@@"):
            out.append(colored(line, Colors.CYAN, bold=True))
            i += 1
        elif line.startswith("+++"):
            out.append(colored(line, Colors.GREEN, bold=True))
            i += 1
        elif line.startswith("---"):
            out.append(colored(line, Colors.RED, bold=True))
            i += 1
        elif (
            line.startswith("-")
            and i + 1 < len(lines_list)
            and lines_list[i + 1].startswith("+")
        ):
            # Consecutive -/+ pair: modified line with inline diff
            old_line = line[1:]  # Remove - prefix
            new_line = lines_list[i + 1][1:]  # Remove + prefix

            # Only apply inline diff if lines are reasonable length
            if len(old_line) < 1000 and len(new_line) < 1000:
                # Add the - line with bold on removed characters, maintaining red color
                highlighted_old = _highlight_inline_diff(
                    old_line, new_line, is_addition=False, color_code=Colors.RED
                )
                out.append(f"{Colors.RED}-{highlighted_old}{Colors.RESET}")

                # Add the + line with bold on added characters, maintaining green color
                highlighted_new = _highlight_inline_diff(
                    old_line, new_line, is_addition=True, color_code=Colors.GREEN
                )
                out.append(f"{Colors.GREEN}+{highlighted_new}{Colors.RESET}")

                i += 2  # Skip both lines
            else:
                # Lines too long - use simple coloring
                out.append(colored(line, Colors.RED))
                i += 1
        elif line.startswith("+"):
            out.append(colored(line, Colors.GREEN))
            i += 1
        elif line.startswith("-"):
            out.append(colored(line, Colors.RED))
            i += 1
        else:
            out.append(line)
            i += 1

    return out


# Convenience functions for common color patterns
def error(text: str) -> str:
    """Return error text in red."""
    return colored(text, Colors.RED)


def success(text: str) -> str:
    """Return success text in green."""
    return colored(text, Colors.GREEN)


def warn(text: str) -> str:
    """Return warning text in yellow."""
    return colored(text, Colors.YELLOW)


def header(text: str) -> str:
    """Return header text in cyan and bold."""
    return colored(text, Colors.CYAN, bold=True)


def dim(text: str) -> str:
    """Return dimmed text."""
    return colored(text, Colors.GRAY, dim=True)
