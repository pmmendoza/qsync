"""
Centralized workbook path resolution for qsync operations.

Provides consistent logic for deriving default Excel workbook paths based
on survey metadata (inventory CSV, live API listing, or survey ID fallback).
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional

from qsync.config import get_client_config, resolve_root, resolve_scoped_dir


def _slugify(value: str) -> str:
    """Make a filesystem-safe slug from a human-readable value."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(value))


class WorkbookResolver:
    """Resolve workbook paths for surveys with consistent slug derivation."""

    def __init__(
        self,
        root: Optional[Path] = None,
        *,
        account: str | None = None,
        env: dict[str, str] | None = None,
    ):
        """
        Initialize workbook resolver.

        Args:
            root: Workspace root directory. If None, resolves from config.
            account: Optional explicit account scope for workspace surfaces.
            env: Optional explicit environment mapping for API lookups.
        """
        self.root = root or resolve_root(required=False) or Path.cwd()
        self.account = account
        self.env = dict(env) if env is not None else None
        self._live_survey_names_by_id: dict[str, str] | None = None

    def resolve(
        self,
        survey_id: str,
        explicit_path: Optional[Path] = None,
    ) -> Path:
        """
        Resolve workbook path for a survey.

        Args:
            survey_id: Survey ID
            explicit_path: User-provided path override

        Returns:
            Absolute path to workbook
        """
        if explicit_path:
            # If explicit path is relative, make it absolute relative to workspace root
            if explicit_path.is_absolute():
                return explicit_path
            return (self.root / explicit_path).resolve()

        return self.default_path(survey_id)

    def default_path(self, survey_id: str) -> Path:
        """
        Get default workbook path using slug derivation precedence.

        Slug derivation order:
        1. 'name' column from surveys/inventory.csv (legacy: surveys/qualtrics_surveys.csv)
        2. SurveyTitle from cached survey definition
        3. Survey ID as fallback

        Args:
            survey_id: Survey ID

        Returns:
            Path in format: excel/{slug}-{survey-id}.xlsx

        Note:
            For backward compatibility, checks if an old-format file
            (excel/{survey-id}-{slug}.xlsx) exists and returns that path
            if found. New files are created with the new format.
        """
        slug = self._derive_slug(survey_id)

        excel_dir = resolve_scoped_dir("excel", root=self.root, account=self.account)

        # New format (preferred)
        new_format_path = excel_dir / f"{slug}-{survey_id}.xlsx"

        # Old format (for backward compatibility)
        old_format_path = excel_dir / f"{survey_id}-{slug}.xlsx"

        # If exact old format exists, use it for backward compatibility.
        if old_format_path.exists():
            return old_format_path

        # If exact new format exists, use it.
        if new_format_path.exists():
            return new_format_path

        # If any workbook already exists for this survey ID (even with a stale/renamed
        # slug), prefer it to avoid creating duplicates.
        if excel_dir.exists():
            for pattern in (f"{survey_id}-*.xlsx", f"*-{survey_id}.xlsx"):
                candidates = []
                for path in excel_dir.glob(pattern):
                    if path.name.startswith("~$"):
                        continue
                    if path.is_file():
                        candidates.append(path)
                if candidates:
                    # Deterministic-ish preference: newest mtime first.
                    candidates.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
                    return candidates[0]

        # Otherwise use new format (for new files).
        return new_format_path

    def _derive_slug(self, survey_id: str) -> str:
        """
        Derive filesystem-safe slug for survey.

        Tries in order:
        1. 'name' from surveys/inventory.csv (legacy: surveys/qualtrics_surveys.csv)
        2. Live survey name from account API inventory
        3. Survey ID fallback

        Args:
            survey_id: Survey ID

        Returns:
            Slugified string for use in filename
        """
        # 1) Try inventory CSV 'name' column
        surveys_dir = resolve_scoped_dir("surveys", root=self.root, account=self.account)
        csv_path = surveys_dir / "inventory.csv"
        if not csv_path.exists():
            csv_path = surveys_dir / "qualtrics_surveys.csv"
        if csv_path.exists():
            try:
                with csv_path.open(newline="", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        if row.get("id") == survey_id:
                            name = (row.get("name") or "").strip()
                            if name:
                                return _slugify(name)
                            break
            except Exception:
                pass

        # 2) Try live survey list for deterministic account-specific naming.
        live_name = self._lookup_live_survey_name(survey_id)
        if live_name:
            return _slugify(live_name)

        # 3) Fallback to survey ID
        return _slugify(survey_id)

    def _lookup_live_survey_name(self, survey_id: str) -> str | None:
        """Best-effort lookup of survey display name from the active account API."""

        if self._live_survey_names_by_id is None:
            self._live_survey_names_by_id = {}
            try:
                from qsync.survey_selection import list_surveys_via_api

                base_url, headers = get_client_config(self.env)
                surveys = list_surveys_via_api(
                    base_url=base_url,
                    headers=headers,
                    timeout=30,
                )
                for row in surveys:
                    sid = str(row.get("id") or "").strip()
                    name = str(row.get("name") or "").strip()
                    if sid and name:
                        self._live_survey_names_by_id[sid] = name
            except Exception:
                # Keep slug resolution resilient/offline-safe.
                self._live_survey_names_by_id = {}

        name = self._live_survey_names_by_id.get(survey_id)
        if not name:
            return None
        return str(name).strip() or None

    def __repr__(self) -> str:
        """String representation."""
        return f"WorkbookResolver(root={self.root!r}, account={self.account!r})"
