"""Questionary validators for interactive text inputs.

These are local-only validators (no network/disk IO) intended for use with
questionary prompts via qsync.interactive_menu.text_input(...).
"""

from __future__ import annotations

import re

from questionary import ValidationError, Validator


_SV_RE = re.compile(r"^SV_[A-Za-z0-9]+$")
_QID_RE = re.compile(r"^QID\\d+$", re.IGNORECASE)
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")
_DC_RE = re.compile(r"^[a-z]{3}\\d+$", re.IGNORECASE)


class SurveyIdValidator(Validator):
    def validate(self, document) -> None:  # type: ignore[override]
        text = (document.text or "").strip()
        if not text:
            raise ValidationError(
                message="Survey ID is required (example: SV_...)",
                cursor_position=0,
            )
        if not text.startswith("SV_"):
            raise ValidationError(
                message="Survey ID must start with 'SV_'",
                cursor_position=len(document.text or ""),
            )
        if not _SV_RE.match(text):
            raise ValidationError(
                message="Survey ID must look like SV_<alphanum>",
                cursor_position=len(document.text or ""),
            )


class QidValidator(Validator):
    def validate(self, document) -> None:  # type: ignore[override]
        text = (document.text or "").strip()
        if not text:
            raise ValidationError(message="QID is required (example: QID15)", cursor_position=0)
        if not _QID_RE.match(text):
            raise ValidationError(
                message="QID must look like QID<number> (example: QID15)",
                cursor_position=len(document.text or ""),
            )


class DatacenterHostValidator(Validator):
    """Validate a Qualtrics datacenter host or subdomain.

    Accepts:
    - subdomain like: iad1
    - host like: iad1.qualtrics.com
    Rejects:
    - schemes like: https://...
    - paths like: host/path
    """

    def validate(self, document) -> None:  # type: ignore[override]
        text = (document.text or "").strip()
        if not text:
            raise ValidationError(
                message="Datacenter is required (example: iad1 or iad1.qualtrics.com)",
                cursor_position=0,
            )
        lowered = text.lower()
        if "://" in lowered:
            raise ValidationError(
                message="Omit scheme (use iad1 or iad1.qualtrics.com, not https://...)",
                cursor_position=len(document.text or ""),
            )
        if "/" in lowered:
            raise ValidationError(
                message="Enter host only (no path)",
                cursor_position=len(document.text or ""),
            )
        if not _HOST_RE.match(text):
            raise ValidationError(
                message="Host contains invalid characters",
                cursor_position=len(document.text or ""),
            )
        # Accept either a subdomain code (iad1) or a dotted hostname.
        if "." in text:
            return
        if not _DC_RE.match(text):
            raise ValidationError(
                message="Datacenter should look like iad1 (3 letters + digits)",
                cursor_position=len(document.text or ""),
            )


class ApiTokenValidator(Validator):
    def validate(self, document) -> None:  # type: ignore[override]
        text = (document.text or "").strip()
        if not text:
            raise ValidationError(message="API token is required", cursor_position=0)
        if len(text) < 10:
            raise ValidationError(
                message="Token looks too short; paste the full X-API-TOKEN value",
                cursor_position=len(document.text or ""),
            )

