from __future__ import annotations

import argparse


def resolve_help_formatter() -> type[argparse.HelpFormatter]:
    try:
        from rich_argparse import RichHelpFormatter

        return RichHelpFormatter
    except Exception:
        return argparse.RawTextHelpFormatter


_DEFAULT_HELP_FORMATTER = resolve_help_formatter()


class QsyncArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("formatter_class", _DEFAULT_HELP_FORMATTER)
        super().__init__(*args, **kwargs)

    def add_subparsers(self, **kwargs):
        kwargs.setdefault("parser_class", QsyncArgumentParser)
        return super().add_subparsers(**kwargs)

