"""Workspace path helpers with explicit scoped-vs-shared ownership.

This module centralizes path resolution so callers don't hardcode legacy
`<root>/surveys` or `<root>/survey_js/core` assumptions.
"""

from __future__ import annotations

from pathlib import Path

from .config import resolve_root, resolve_scoped_dir


def workspace_root(root: Path | None = None) -> Path:
    """Return the resolved qsync workspace root."""

    return (root or resolve_root(required=False) or Path.cwd()).resolve()


def scoped_surface_dir(
    dirname: str,
    *,
    root: Path | None = None,
    account: str | None = None,
) -> Path:
    """Return layout-aware scoped surface directory."""

    return resolve_scoped_dir(dirname, root=workspace_root(root), account=account)


def default_surface_dir(dirname: str, *, root: Path | None = None) -> Path:
    """Return layout-aware default-account surface directory."""

    return scoped_surface_dir(dirname, root=root, account="default")


def survey_js_core_dir(*, root: Path | None = None, account: str | None = None) -> Path:
    """Return layout-aware JS core directory for the selected account context."""

    return scoped_surface_dir("survey_js", root=root, account=account) / "core"


def legacy_shared_surveys_dir(*, root: Path | None = None) -> Path:
    """Return legacy shared surveys directory under workspace root."""

    return (workspace_root(root) / "surveys").resolve()


def edf_presets_candidates(*, root: Path | None = None) -> list[Path]:
    """Return EDF preset candidate paths in lookup order."""

    root_path = workspace_root(root)
    return [
        (legacy_shared_surveys_dir(root=root_path) / "edf_presets.json").resolve(),
        (default_surface_dir("surveys", root=root_path) / "edf_presets.json").resolve(),
    ]


def resolve_edf_presets_path(*, root: Path | None = None) -> Path:
    """Resolve EDF preset path, preferring existing files in candidate order."""

    for candidate in edf_presets_candidates(root=root):
        if candidate.exists():
            return candidate
    return edf_presets_candidates(root=root)[0]


def mapping_csv_candidates(*, root: Path | None = None) -> list[Path]:
    """Return survey-master mapping CSV candidate paths in lookup order."""

    root_path = workspace_root(root)
    return [
        (
            legacy_shared_surveys_dir(root=root_path)
            / "qualtrics_api_key_mapping.csv"
        ).resolve(),
        (
            default_surface_dir("surveys", root=root_path)
            / "qualtrics_api_key_mapping.csv"
        ).resolve(),
        (root_path / "appendices" / "qualtrics_api_key_mapping.csv").resolve(),
    ]

