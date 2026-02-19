from __future__ import annotations

from .translations_paths import translation_key_snapshot_path as _translation_key_snapshot_path


def translation_key_snapshot_path(
    survey_id: str, label: str, language: str, root=None
):
    """Backward-compatible alias for translation key snapshots.

    Canonical storage now lives under the survey's translations directory.
    """

    return _translation_key_snapshot_path(
        survey_id=survey_id,
        label=label,
        language=language,
        root=root,
    )
