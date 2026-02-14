from __future__ import annotations

import argparse
from typing import Any, Iterable


def resolve_help_formatter() -> type[argparse.HelpFormatter]:
    try:
        from rich_argparse import RichHelpFormatter

        return RichHelpFormatter
    except Exception:
        return argparse.RawTextHelpFormatter


def resolve_raw_description_formatter() -> type[argparse.HelpFormatter]:
    """Help formatter that preserves description/epilog newlines when possible."""
    try:
        from rich_argparse import RawDescriptionRichHelpFormatter

        return RawDescriptionRichHelpFormatter
    except Exception:
        return argparse.RawDescriptionHelpFormatter


_DEFAULT_HELP_FORMATTER = resolve_help_formatter()


class QsyncArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("formatter_class", _DEFAULT_HELP_FORMATTER)
        super().__init__(*args, **kwargs)

    def add_subparsers(self, **kwargs):
        kwargs.setdefault("parser_class", QsyncArgumentParser)
        return super().add_subparsers(**kwargs)


def reorder_subparser_choices(subparsers: Any, order: Iterable[str]) -> None:
    """Best-effort reorder of subcommand display order in argparse help.

    argparse preserves insertion order of subcommands. Keeping code organized by
    implementation concerns doesn't always match the most readable/helpful help
    output ordering, so we reorder the *display* order without changing parsing
    behavior.

    This mutates private argparse attributes; if argparse internals change, we
    silently fall back to the original ordering.
    """

    try:
        actions = list(getattr(subparsers, "_choices_actions", []))
        if not actions:
            return

        desired = [str(name) for name in order]

        by_dest: dict[str, Any] = {}
        for action in actions:
            dest = getattr(action, "dest", None)
            if isinstance(dest, str):
                by_dest[dest] = action

        new_actions: list[Any] = []
        seen: set[str] = set()

        for name in desired:
            action = by_dest.get(name)
            if action is None or name in seen:
                continue
            new_actions.append(action)
            seen.add(name)

        # Append any remaining choices, preserving their original relative order.
        for action in actions:
            dest = getattr(action, "dest", None)
            if isinstance(dest, str) and dest not in seen:
                new_actions.append(action)
                seen.add(dest)

        subparsers._choices_actions[:] = new_actions
    except Exception:
        return


def hide_subparser_choices(subparsers: Any, names: Iterable[str]) -> None:
    """Hide specific subcommands from help output while keeping them parseable.

    This removes the corresponding pseudo-actions used only for formatting help
    output. The underlying parsers remain in `_name_parser_map`, so the hidden
    subcommands still work if invoked directly.
    """

    try:
        actions = list(getattr(subparsers, "_choices_actions", []))
        if not actions:
            return

        hide = {str(name) for name in names}
        subparsers._choices_actions[:] = [
            action
            for action in actions
            if not (isinstance(getattr(action, "dest", None), str) and action.dest in hide)
        ]
    except Exception:
        return
