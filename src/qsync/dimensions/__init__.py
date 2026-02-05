"""Canonical dimension entrypoints for qsync."""

from .types import DimensionChanges
from . import items, js, translations, eos

try:
    from . import edf
except Exception as exc:  # pragma: no cover - defensive compatibility fallback

    class _MissingEdfModule:
        """Compatibility shim when an install is missing qsync.dimensions.edf."""

        _reason = exc
        _message = (
            "EDF dimension is unavailable in this qsync installation. "
            "Reinstall/update qsync (for example: `qsync self-update --yes`) and retry."
        )

        @classmethod
        def _as_changes(cls) -> DimensionChanges:
            return DimensionChanges(
                dimension="edf",
                has_changes=False,
                change_summary="⚠ unavailable",
                affected_qids=set(),
                warning_detail=f"{cls._message} (import error: {cls._reason})",
                status_kind="none",
                edit_count=0,
            )

        @classmethod
        def detect_unstaged_changes(cls, *_args, **_kwargs) -> DimensionChanges:
            return cls._as_changes()

        @classmethod
        def detect_changes(cls, *_args, **_kwargs) -> DimensionChanges:
            return cls._as_changes()

        @classmethod
        def stage(cls, *_args, **_kwargs) -> bool:
            return False

        @classmethod
        def push(cls, *_args, **_kwargs) -> bool:
            return False

        @classmethod
        def repair_workbook(cls, *_args, **_kwargs):
            raise RuntimeError(cls._message)

    edf = _MissingEdfModule()

__all__ = [
    "DimensionChanges",
    "items",
    "edf",
    "js",
    "translations",
    "eos",
]
