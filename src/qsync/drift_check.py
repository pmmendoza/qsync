"""
Drift detection for qsync dimensions.

Provides unified drift checking between local cached survey definitions and
live Qualtrics API state to detect out-of-band changes.
"""

from __future__ import annotations

import csv
import difflib
import hashlib
import html
import json
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from qsync.push_logger import log_push_event
from qsync.config import resolve_root, resolve_scoped_dir
from qsync.qualtrics_client import fetch_survey_definition_live, load_cached_survey

DimensionType = Literal["items", "js", "translations", "eos", "flow"]


def _translation_value_fingerprint(value: object) -> str:
    text = html.unescape(str(value or ""))
    text = text.replace("\u00a0", " ").strip()
    digest = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()
    return digest[:8]


def _normalize_translation_value_for_diff(value: object) -> str:
    text = html.unescape(str(value or ""))
    text = text.replace("\u00a0", " ")
    return text.strip()


def _translation_fingerprint_lines(
    payload: dict, *, languages: list[str] | None, qids: set[str] | None
) -> list[str]:
    """Return stable fingerprint lines for translations drift detection.

    Format (tab-separated):
      QID<TAB>LANG<TAB>FIELD<TAB>ITEM_ID<TAB>HASH8
    """

    from .translations_utils import normalize_language_code

    questions = payload.get("Questions") or {}
    lines: list[str] = []
    for qid, question in questions.items():
        if qids and qid not in qids:
            continue
        if not isinstance(question, dict):
            continue
        language_block = question.get("Language")
        if not isinstance(language_block, dict):
            continue
        for raw_lang, lang_data in language_block.items():
            lang = normalize_language_code(str(raw_lang or ""))
            if not lang:
                continue
            if languages and lang not in set(languages):
                continue
            if not isinstance(lang_data, dict):
                continue
            if "QuestionText" in lang_data:
                h = _translation_value_fingerprint(lang_data.get("QuestionText"))
                lines.append(f"{qid}\t{lang}\tQuestionText\t\t{h}")

            for section_name, kind in (
                ("Choices", "Choice"),
                ("Answers", "Answer"),
                ("Labels", "Label"),
            ):
                section = lang_data.get(section_name)
                if not isinstance(section, dict):
                    continue
                for item_id, entry in section.items():
                    if not isinstance(entry, dict):
                        continue
                    if "Display" not in entry:
                        continue
                    h = _translation_value_fingerprint(entry.get("Display"))
                    lines.append(f"{qid}\t{lang}\t{kind}\t{str(item_id)}\t{h}")

    options = payload.get("SurveyOptions") or {}
    if isinstance(options, dict):
        for key in ("SurveyLanguage", "AvailableLanguages", "MetaDataTranslations"):
            if key in options:
                h = _translation_value_fingerprint(
                    json.dumps(options.get(key), sort_keys=True)
                )
                lines.append(f"SurveyOptions\t\t{key}\t\t{h}")

    return sorted(lines)


def _translation_value_map(
    payload: dict, *, languages: list[str] | None, qids: set[str] | None
) -> dict[tuple[str, str, str, str], str]:
    from .translations_utils import normalize_language_code

    values: dict[tuple[str, str, str, str], str] = {}
    questions = payload.get("Questions") or {}
    for qid, question in questions.items():
        if qids and qid not in qids:
            continue
        if not isinstance(question, dict):
            continue
        language_block = question.get("Language")
        if not isinstance(language_block, dict):
            continue
        for raw_lang, lang_data in language_block.items():
            lang = normalize_language_code(str(raw_lang or ""))
            if not lang:
                continue
            if languages and lang not in set(languages):
                continue
            if not isinstance(lang_data, dict):
                continue
            if "QuestionText" in lang_data:
                values[(qid, lang, "QuestionText", "")] = (
                    _normalize_translation_value_for_diff(lang_data.get("QuestionText"))
                )

            for section_name, kind in (
                ("Choices", "Choice"),
                ("Answers", "Answer"),
                ("Labels", "Label"),
            ):
                section = lang_data.get(section_name)
                if not isinstance(section, dict):
                    continue
                for item_id, entry in section.items():
                    if not isinstance(entry, dict):
                        continue
                    if "Display" not in entry:
                        continue
                    values[(qid, lang, kind, str(item_id))] = (
                        _normalize_translation_value_for_diff(entry.get("Display"))
                    )

    options = payload.get("SurveyOptions") or {}
    if isinstance(options, dict):
        for key in ("SurveyLanguage", "AvailableLanguages", "MetaDataTranslations"):
            if key in options:
                values[("SurveyOptions", "", key, "")] = (
                    _normalize_translation_value_for_diff(
                        json.dumps(options.get(key), sort_keys=True)
                    )
                )

    return values


def _translation_text_diff_lines(
    cached_payload: dict,
    live_payload: dict,
    *,
    languages: list[str] | None,
    qids: set[str] | None,
) -> list[str]:
    cached_map = _translation_value_map(cached_payload, languages=languages, qids=qids)
    live_map = _translation_value_map(live_payload, languages=languages, qids=qids)
    keys = sorted(set(cached_map.keys()) | set(live_map.keys()))
    lines: list[str] = []
    for key in keys:
        old_value = cached_map.get(key, "")
        new_value = live_map.get(key, "")
        if old_value == new_value:
            continue
        qid, lang, field, item_id = key
        lines.append(f"@@ {qid}\t{lang}\t{field}\t{item_id} @@")
        lines.extend(
            difflib.unified_diff(
                old_value.splitlines(),
                new_value.splitlines(),
                fromfile="cache",
                tofile="live",
                lineterm="",
            )
        )
    return lines


def _summarize_translation_fingerprint_diff(
    cached_lines: list[str],
    live_lines: list[str],
) -> tuple[str, int, int, int]:
    """Summarize a translations fingerprint diff.

    Returns:
      (summary, changed_total, added, removed)
    """

    def parse(lines: list[str]) -> dict[tuple[str, str, str, str], str]:
        out: dict[tuple[str, str, str, str], str] = {}
        for line in lines:
            parts = line.split("\t")
            if len(parts) != 5:
                continue
            qid, lang, field, item_id, h = parts
            key = (qid, lang, field, item_id)
            out[key] = h
        return out

    cached_map = parse(cached_lines)
    live_map = parse(live_lines)

    cached_keys = set(cached_map.keys())
    live_keys = set(live_map.keys())

    added_keys = live_keys - cached_keys
    removed_keys = cached_keys - live_keys
    common = cached_keys & live_keys

    modified_keys = {k for k in common if cached_map.get(k) != live_map.get(k)}

    changed_total = len(added_keys) + len(removed_keys) + len(modified_keys)

    changed_qids = {
        qid
        for (qid, _lang, _field, _item_id) in (
            added_keys | removed_keys | modified_keys
        )
        if qid and qid != "SurveyOptions"
    }
    changed_langs = {
        lang
        for (_qid, lang, _field, _item_id) in (
            added_keys | removed_keys | modified_keys
        )
        if lang
    }
    touches_options = any(
        k[0] == "SurveyOptions" for k in (added_keys | removed_keys | modified_keys)
    )

    qid_part = f"{len(changed_qids)} QID(s)" if changed_qids else "0 QID(s)"
    lang_part = (
        f"{len(changed_langs)} language(s)" if changed_langs else "0 language(s)"
    )
    opt_part = " (includes SurveyOptions)" if touches_options else ""

    summary = (
        f"Translations differ from API: {changed_total} key(s) changed "
        f"({len(modified_keys)} modified, {len(added_keys)} added, {len(removed_keys)} removed) "
        f"across {qid_part} / {lang_part}{opt_part}"
    )
    return summary, changed_total, len(added_keys), len(removed_keys)


@dataclass
class DriftReport:
    """Report of drift detection between cache and API."""

    has_drift: bool
    summary: str
    diff_lines: list[str]
    recommendation: str
    context_lines: list[str] = field(default_factory=list)
    changed_count: int = 0
    additions: int = 0
    deletions: int = 0

    def _compute_statistics(self) -> None:
        """Compute diff statistics if not already set."""
        if self.additions > 0 or self.deletions > 0:
            return  # Already computed

        for line in self.diff_lines:
            if line.startswith("+") and not line.startswith("+++"):
                self.additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                self.deletions += 1

    def display(self, interactive: bool = True, *, show_full: bool = False) -> None:
        """
        Display drift report to user with smart truncation.

        Features:
        - Show summary + diff statistics
        - Hide diff details unless show_full=True

        Args:
            interactive: If True, allow interactive expansion of truncated diffs
        """
        from .terminal_output import info

        if not self.has_drift:
            info("[qsync]", "No drift detected - cache is up to date with API.")
            return

        # Compute statistics
        self._compute_statistics()

        # Show summary
        info("[qsync]", f"DRIFT DETECTED: {self.summary}")

        # Show statistics
        if self.changed_count > 0:
            stats_msg = f"{self.changed_count} change(s) detected"
            if self.additions > 0 or self.deletions > 0:
                stats_msg += (
                    f", +{self.additions} additions, -{self.deletions} deletions"
                )
            info("[qsync]", stats_msg)

        if not self.diff_lines:
            info("[qsync]", self.recommendation)
            return

        if show_full:
            from .terminal_colors import colorize_unified_diff_lines

            info("[qsync]", f"Full diff ({len(self.diff_lines)} lines):")
            if self.context_lines:
                for ctx in self.context_lines:
                    print(f"  {ctx}")
            for line in colorize_unified_diff_lines(self.diff_lines):
                print(f"  {line}")
        else:
            info(
                "[qsync]",
                f"Diff available ({len(self.diff_lines)} lines). Use the drift menu to view details.",
            )

        print()  # blank line
        info("[qsync]", self.recommendation)


def check_drift(
    survey_id: str,
    dimension: DimensionType,
    interactive: bool = True,
    context: dict | None = None,
) -> DriftReport:
    """
    Check for drift between cached survey and live API.

    Drift occurs when the cached survey definition differs from the live API,
    indicating out-of-band changes (e.g., edits via Qualtrics UI).

    Args:
        survey_id: Survey ID to check
        dimension: Dimension being checked
        interactive: Whether to display detailed diffs

    Returns:
        DriftReport with drift status and details
    """
    try:
        if dimension == "edf":
            dimension = "items"
        if dimension == "js":
            return _check_js_drift(survey_id, context=context)

        if dimension == "items":
            # Load cached survey
            cached = load_cached_survey(survey_id)

            # Fetch live survey from API without overwriting local cache
            live_payload = fetch_survey_definition_live(survey_id)

            # Generate simple JSON diff (ignore volatile metadata)
            cached_clean = _strip_survey_definition_noise(cached.payload)
            live_clean = _strip_survey_definition_noise(live_payload)
            qids = None
            if context and context.get("qids"):
                qids = {
                    str(qid).strip()
                    for qid in (context.get("qids") or [])
                    if str(qid).strip()
                }
            if qids:
                cached_clean = _filter_payload_questions(cached_clean, qids)
                live_clean = _filter_payload_questions(live_clean, qids)
            cached_json = json.dumps(cached_clean, indent=2, sort_keys=True)
            live_json = json.dumps(live_clean, indent=2, sort_keys=True)

            if cached_json == live_json:
                return DriftReport(
                    has_drift=False,
                    summary="Cache matches API",
                    diff_lines=[],
                    recommendation="No action needed - cache is up to date.",
                    changed_count=0,
                )

            # Generate unified diff
            diff_lines = list(
                difflib.unified_diff(
                    cached_json.splitlines(keepends=False),
                    live_json.splitlines(keepends=False),
                    fromfile=f"cache [{cached.path.name}]",
                    tofile="live [Qualtrics]",
                    lineterm="",
                )
            )

            # Count changed lines (exclude diff headers)
            changed_count = sum(
                1
                for line in diff_lines
                if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
            )
            additions = sum(
                1
                for line in diff_lines
                if line.startswith("+") and not line.startswith("+++")
            )
            deletions = sum(
                1
                for line in diff_lines
                if line.startswith("-") and not line.startswith("---")
            )

            return DriftReport(
                has_drift=True,
                summary="Cache is out of sync with API",
                diff_lines=diff_lines,
                recommendation=_drift_recommendation(dimension, survey_id),
                context_lines=[
                    f"context: cache={cached.path}, remote=Qualtrics live survey definition",
                ],
                changed_count=changed_count,
                additions=additions,
                deletions=deletions,
            )

        if dimension == "translations":
            return _check_translations_drift(survey_id, context=context)

        if dimension == "eos":
            return _check_eos_drift(survey_id, context=context)
        if dimension == "flow":
            return _check_flow_drift(survey_id, context=context)

        if dimension == "flow":
            return _check_flow_drift(survey_id, context=context)

    except FileNotFoundError:
        # No cached survey - not really drift, but missing cache
        return DriftReport(
            has_drift=True,
            summary="No cached survey found",
            diff_lines=[],
            recommendation=f"Run 'qsync {dimension} pull' to cache survey definition.",
            changed_count=0,
        )

    except Exception as e:
        # Error checking drift - treat as no drift but warn
        return DriftReport(
            has_drift=False,
            summary=f"Unable to check drift: {e}",
            diff_lines=[],
            recommendation="Proceeding without drift check. Run 'qsync doctor' if issues persist.",
            changed_count=0,
        )


def enforce_no_drift(
    survey_id: str,
    dimension: DimensionType,
    allow_drift: bool = False,
    interactive: bool = True,
    context: dict | None = None,
) -> DriftReport:
    """
    Check drift and block if drift detected (unless overridden).

    Args:
        survey_id: Survey ID to check
        dimension: Dimension being checked
        allow_drift: If True, allow proceeding despite drift
        interactive: Whether to show interactive prompts

    Returns:
        DriftReport

    Raises:
        SystemExit: If drift detected and not allowed
    """
    _warn_possible_drift(survey_id, dimension)
    report = check_drift(survey_id, dimension, interactive=interactive, context=context)

    if not report.has_drift:
        from .terminal_output import info, warn

        prefix = f"[qsync:{dimension}]"
        if report.summary.startswith("Unable to check drift:"):
            warn(prefix, report.summary)
        else:
            info(prefix, f"Drift: none ({report.summary})")
        return report

    # Drift detected
    report.display(interactive=False)

    if allow_drift:
        print(f"[qsync:{dimension}] WARNING: Proceeding despite drift (--allow-drift)")
        _log_drift_event(
            action=f"qsync.{dimension}.drift.override",
            survey_id=survey_id,
            message=report.summary,
            meta={"mode": "stage_push"},
        )
        return report

    _log_drift_event(
        action=f"qsync.{dimension}.drift.blocked",
        survey_id=survey_id,
        message=report.summary,
        meta={"mode": "stage_push"},
    )
    raise SystemExit(
        f"[qsync:{dimension}] ERROR: Drift detected. {report.recommendation}"
    )


def confirm_preview_drift(
    survey_id: str,
    dimension: DimensionType,
    *,
    allow_drift: bool,
    interactive: bool,
    update_cache: Callable[[], None] | None = None,
    context: dict | None = None,
) -> DriftReport:
    report = check_drift(survey_id, dimension, interactive=interactive, context=context)
    if not report.has_drift:
        return report

    report.display(interactive=False)

    if allow_drift:
        print(
            f"[qsync:{dimension}] WARNING: Previewing against drifted cache (--allow-drift)"
        )
        _log_drift_event(
            action=f"qsync.{dimension}.drift.override",
            survey_id=survey_id,
            message=report.summary,
            meta={"mode": "preview"},
        )
        return report

    if not interactive:
        _log_drift_event(
            action=f"qsync.{dimension}.drift.blocked",
            survey_id=survey_id,
            message=report.summary,
            meta={"mode": "preview"},
        )
        raise SystemExit(
            f"[qsync:{dimension}] ERROR: Drift detected. {report.recommendation}"
        )

    while True:
        choice = _prompt_drift_choice(dimension)
        if choice == "yes":
            _log_drift_event(
                action=f"qsync.{dimension}.drift.override",
                survey_id=survey_id,
                message=report.summary,
                meta={"mode": "preview"},
            )
            return report
        if choice == "abort":
            _log_drift_event(
                action=f"qsync.{dimension}.drift.blocked",
                survey_id=survey_id,
                message=report.summary,
                meta={"mode": "preview"},
            )
            raise SystemExit(f"[qsync:{dimension}] Aborted. {report.recommendation}")
        if choice == "show":
            report.display(interactive=False, show_full=True)
            continue
        if choice == "update":
            if update_cache is None:
                print(
                    f"[qsync:{dimension}] Update cache is not available for this command."
                )
                continue
            update_cache()
            report = check_drift(
                survey_id, dimension, interactive=interactive, context=context
            )
            if not report.has_drift:
                return report
            report.display(interactive=False)


def _prompt_drift_choice(dimension: DimensionType) -> str:
    from .interactive_menu import select_from_list

    print()
    print(f"[qsync:{dimension}] Drift detected between cache and API.")

    choices = [
        "Yes (preview against stale cache)",
        "Abort",
        "Show detailed diffs (live vs cache)",
        "Update cache (run pull for this dimension)",
    ]

    selection = select_from_list(
        message="Choose an option:",
        choices=choices,
    )
    if selection is None:
        return "abort"
    if selection.startswith("Yes"):
        return "yes"
    if selection.startswith("Abort"):
        return "abort"
    if selection.startswith("Show"):
        return "show"
    if selection.startswith("Update"):
        return "update"

    return "abort"


def _check_translations_drift(
    survey_id: str, *, context: dict | None = None
) -> DriftReport:
    cached = load_cached_survey(survey_id)
    live_payload = fetch_survey_definition_live(survey_id)

    cached_payload = _normalize_payload(cached.payload or {})
    live_payload = _normalize_payload(live_payload or {})

    languages = None
    if context and context.get("languages"):
        languages = [
            str(lang).strip() for lang in context.get("languages") if str(lang).strip()
        ]

    qids = None
    if context and context.get("qids"):
        qids = {
            str(qid).strip() for qid in (context.get("qids") or []) if str(qid).strip()
        }

    cached_lines = _translation_fingerprint_lines(
        cached_payload, languages=languages, qids=qids
    )
    live_lines = _translation_fingerprint_lines(
        live_payload, languages=languages, qids=qids
    )

    if cached_lines == live_lines:
        return DriftReport(
            has_drift=False,
            summary="Translations match survey definition API",
            diff_lines=[],
            recommendation="No action needed - cache is up to date.",
            changed_count=0,
        )

    diff_lines = _translation_text_diff_lines(
        cached_payload,
        live_payload,
        languages=languages,
        qids=qids,
    )
    summary, changed_count, additions, deletions = (
        _summarize_translation_fingerprint_diff(cached_lines, live_lines)
    )

    return DriftReport(
        has_drift=True,
        summary=summary,
        diff_lines=diff_lines,
        recommendation=(
            f"Run 'qsync survey pull --survey-id {survey_id}' to refresh the cached survey definition. "
            "Or use --allow-drift to proceed anyway."
        ),
        context_lines=[
            f"context: cache={cached.path}, remote=Qualtrics live survey definition",
            "diff format: per-key unified diffs (values are html-unescaped and NBSP-normalized)",
            f"hint: to compare workbook vs cache, use `qsync translations preview --survey-id {survey_id} --detailed`.",
        ],
        changed_count=changed_count,
        additions=additions,
        deletions=deletions,
    )


def _check_eos_drift(survey_id: str, *, context: dict | None = None) -> DriftReport:
    from .dimensions.eos_core import (
        _coerce_result_payload,
        extract_eos_message_refs,
        _latest_backup_result,
        message_dir,
    )
    from .api_push import send_api_request
    from .config import get_client_config

    cache = load_cached_survey(survey_id)
    refs = extract_eos_message_refs(survey_id, cache.payload)
    if context and context.get("operations"):
        allowed = {
            (
                str(op.get("library_id") or "").strip(),
                str(op.get("message_id") or "").strip(),
            )
            for op in (context.get("operations") or [])
            if isinstance(op, dict)
        }
        if allowed:
            refs = [
                ref
                for ref in refs
                if (str(ref.library_id), str(ref.message_id)) in allowed
            ]
    if not refs:
        return DriftReport(
            has_drift=False,
            summary="No EndSurvey DisplayMessage references found",
            diff_lines=[],
            recommendation="No action needed - no EOS messages are referenced.",
            changed_count=0,
        )

    base_url, headers = get_client_config()
    diff_lines: list[str] = []
    total_changed = 0
    drifted = 0

    for ref in refs:
        baseline = _latest_backup_result(ref.library_id, ref.message_id)
        if baseline is None:
            diff_lines.append(f"=== eos:{ref.library_id}/{ref.message_id} ===")
            diff_lines.append(
                "no baseline snapshot found on disk (missing backups/*.json); run `qsync eos pull` first"
            )
            total_changed += 1
            drifted += 1
            continue
        live_resp = send_api_request(
            action="qsync.eos.drift.message",
            method="GET",
            base_url=base_url,
            headers=headers,
            path=f"libraries/{ref.library_id}/messages/{ref.message_id}",
            survey_id=survey_id,
            log_event=False,
            timeout=60,
        )
        live = _coerce_result_payload(live_resp.json())
        baseline_json = json.dumps(baseline, indent=2, sort_keys=True)
        live_json = json.dumps(live, indent=2, sort_keys=True)
        if baseline_json == live_json:
            continue
        drifted += 1
        diff_lines.append(f"=== eos:{ref.library_id}/{ref.message_id} ===")
        local_dir = message_dir(ref.library_id, ref.message_id)
        diff_lines.append(
            f"context: baseline={local_dir}/backups/*.json, remote=Qualtrics live message"
        )
        msg_diff = list(
            difflib.unified_diff(
                baseline_json.splitlines(keepends=False),
                live_json.splitlines(keepends=False),
                fromfile="baseline [backup]",
                tofile="remote [Qualtrics]",
                lineterm="",
            )
        )
        diff_lines.extend(msg_diff)
        total_changed += sum(
            1
            for line in msg_diff
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        )

    if drifted == 0:
        return DriftReport(
            has_drift=False,
            summary="EOS messages match API",
            diff_lines=[],
            recommendation="No action needed - EOS messages are up to date.",
            changed_count=0,
        )

    return DriftReport(
        has_drift=True,
        summary=f"EOS drift detected in {drifted} message(s)",
        diff_lines=diff_lines,
        recommendation=(
            f"Run 'qsync eos pull --survey-id {survey_id}' to refresh local messages. "
            "Or use --allow-drift to proceed anyway (changes may overwrite recent API edits)."
        ),
        changed_count=total_changed,
    )


def _check_flow_drift(survey_id: str, *, context: dict | None = None) -> DriftReport:
    """Check for flow drift between local baseline and live API."""
    del context
    from .dimensions.flow import _baseline_path

    baseline_path = _baseline_path(survey_id)
    if not baseline_path.exists():
        return DriftReport(
            has_drift=False,
            summary="No flow baseline found",
            diff_lines=[],
            recommendation=f"Run 'qsync flow pull --survey-id {survey_id}' to initialize flow.",
            changed_count=0,
        )

    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return DriftReport(
            has_drift=False,
            summary=f"Could not read flow baseline: {exc}",
            diff_lines=[],
            recommendation=f"Run 'qsync flow pull --survey-id {survey_id}' to refresh.",
            changed_count=0,
        )

    live_payload = fetch_survey_definition_live(survey_id)
    live_flow = live_payload.get("result", {}).get("SurveyFlow", {})

    baseline_json = json.dumps(baseline, indent=2, sort_keys=True)
    live_json = json.dumps(live_flow, indent=2, sort_keys=True)

    if baseline_json == live_json:
        return DriftReport(
            has_drift=False,
            summary="Flow baseline matches API",
            diff_lines=[],
            recommendation="No action needed.",
            changed_count=0,
        )

    diff_lines = list(
        difflib.unified_diff(
            baseline_json.splitlines(keepends=False),
            live_json.splitlines(keepends=False),
            fromfile="baseline [local]",
            tofile="live [Qualtrics]",
            lineterm="",
        )
    )
    changed_count = sum(
        1
        for line in diff_lines
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )
    additions = sum(
        1 for line in diff_lines if line.startswith("+") and not line.startswith("+++")
    )
    deletions = sum(
        1 for line in diff_lines if line.startswith("-") and not line.startswith("---")
    )
    return DriftReport(
        has_drift=True,
        summary="Flow baseline is out of sync with API",
        diff_lines=diff_lines,
        recommendation=_drift_recommendation("flow", survey_id),
        context_lines=[
            f"context: baseline={baseline_path}, remote=Qualtrics live survey flow",
        ],
        changed_count=changed_count,
        additions=additions,
        deletions=deletions,
    )


def _log_drift_event(
    action: str,
    *,
    survey_id: str,
    message: str,
    meta: dict | None = None,
) -> None:
    log_push_event(
        action=action,
        method="LOCAL",
        path="drift_check",
        survey_id=survey_id,
        status=None,
        error={"message": message},
        meta=meta,
    )


def _warn_possible_drift(survey_id: str, dimension: DimensionType) -> None:
    if dimension not in ("items", "js"):
        return
    from .survey_inventory import load_inventory_record

    cached = load_cached_survey(survey_id)
    cached_payload = (
        cached.payload.get("result", {}) if isinstance(cached.payload, dict) else {}
    )
    cached_last = (
        cached_payload.get("LastModified")
        or cached_payload.get("lastModified")
        or cached_payload.get("LastModifiedDate")
    )
    record = load_inventory_record(survey_id) or {}
    live_last = record.get("lastModified") or record.get("lastModifiedDate")

    cached_dt = _parse_timestamp(cached_last)
    live_dt = _parse_timestamp(live_last)
    if not cached_dt or not live_dt:
        return
    if live_dt <= cached_dt:
        return

    print(
        f"[qsync:{dimension}] NOTE: inventory lastModified ({live_dt.isoformat()}) "
        f"is newer than cached survey LastModified ({cached_dt.isoformat()}). "
        "Consider running pull before staging/pushing."
    )


def _parse_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _normalize_payload(payload: dict) -> dict:
    if "result" in payload and isinstance(payload["result"], dict):
        return payload["result"]
    return payload


def _filter_payload_questions(payload: dict, qids: set[str]) -> dict:
    if not qids:
        return payload
    data = payload if isinstance(payload, dict) else {}
    result = data.get("result") if isinstance(data.get("result"), dict) else data
    if not isinstance(result, dict):
        return data
    questions = result.get("Questions")
    if isinstance(questions, dict):
        result["Questions"] = {qid: questions[qid] for qid in qids if qid in questions}
    return data


def _strip_survey_definition_noise(payload: dict) -> dict:
    """Remove volatile survey-definition fields that should not trigger drift."""
    try:
        data = json.loads(json.dumps(payload))
    except Exception:
        data = payload if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        return {}
    data.pop("meta", None)
    result = data.get("result") if isinstance(data.get("result"), dict) else data
    if isinstance(result, dict):
        for key in (
            "LastModified",
            "LastAccessed",
            "LastActivated",
            "LastModifiedDate",
            "lastModified",
            "lastModifiedDate",
        ):
            result.pop(key, None)
    return data


def _resolve_mapping_column(fieldnames: list[str], survey_id: str) -> str:
    for name in fieldnames:
        if name == "js_file":
            continue
        prefix = name.split("-", 1)[0]
        if prefix == survey_id:
            return name
    raise ValueError(
        f"Mapping CSV missing a column for survey_id '{survey_id}'. "
        f"Available columns: {', '.join(n for n in fieldnames if n != 'js_file')}"
    )


def _load_js_mapping_qids(mapping_csv: Path, survey_id: str) -> list[str]:
    mapping: dict[str, list[str]] = {}
    with mapping_csv.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        if "js_file" not in fieldnames:
            raise ValueError(
                f"Mapping CSV {mapping_csv} is missing required 'js_file' column."
            )
        column = _resolve_mapping_column(fieldnames, survey_id)
        for row in reader:
            js_file = (row.get("js_file") or "").strip()
            if not js_file:
                continue
            raw = row.get(column, "") or ""
            qids = [qid.strip() for qid in raw.split("|") if qid.strip()]
            mapping[js_file] = qids
    qids: list[str] = []
    for entry in mapping.values():
        qids.extend(entry)
    return sorted({qid for qid in qids if qid})


def _extract_question_js(question: dict) -> str:
    return question.get("QuestionJS") or question.get("QuestionJSContent") or ""


def _check_js_drift(survey_id: str, *, context: dict | None = None) -> DriftReport:
    cached = load_cached_survey(survey_id)
    live_payload = fetch_survey_definition_live(survey_id)

    cached_payload = _normalize_payload(cached.payload or {})
    live_payload = _normalize_payload(live_payload or {})

    cached_questions = cached_payload.get("Questions") or {}
    live_questions = live_payload.get("Questions") or {}

    root = resolve_root(required=False) or Path.cwd()
    mapping_csv = resolve_scoped_dir("survey_js", root=root) / "survey_qid_js_map.csv"

    if mapping_csv.exists():
        try:
            qids = _load_js_mapping_qids(mapping_csv, survey_id)
        except Exception:
            qids = []
    else:
        qids = []

    context_qids = None
    if context and context.get("qids"):
        context_qids = {
            str(qid).strip() for qid in (context.get("qids") or []) if str(qid).strip()
        }

    if context_qids:
        if qids:
            qids = [qid for qid in qids if qid in context_qids]
        else:
            qids = sorted(context_qids)

    if not qids:
        qids = sorted(
            {
                qid
                for qid in set(cached_questions.keys()) | set(live_questions.keys())
                if _extract_question_js(cached_questions.get(qid, {}))
                or _extract_question_js(live_questions.get(qid, {}))
            }
        )

    diff_lines: list[str] = []
    changed_qids: list[str] = []

    for qid in qids:
        cached_js = _extract_question_js(cached_questions.get(qid, {}))
        live_js = _extract_question_js(live_questions.get(qid, {}))
        if cached_js == live_js:
            continue

        changed_qids.append(qid)
        diff = list(
            difflib.unified_diff(
                cached_js.splitlines(keepends=False),
                live_js.splitlines(keepends=False),
                fromfile=f"{qid}:cache [{cached.path.name}]",
                tofile=f"{qid}:live [Qualtrics]",
                lineterm="",
            )
        )
        if diff:
            if diff_lines:
                diff_lines.append("")
            diff_lines.extend(diff)

    if not diff_lines:
        return DriftReport(
            has_drift=False,
            summary="Cache matches API",
            diff_lines=[],
            recommendation="No action needed - cache is up to date.",
            changed_count=0,
        )

    return DriftReport(
        has_drift=True,
        summary="Cache is out of sync with API",
        diff_lines=diff_lines,
        recommendation=_drift_recommendation("js", survey_id),
        context_lines=[
            f"context: cache={cached.path}, remote=Qualtrics live survey definition",
        ],
        changed_count=len(changed_qids),
    )


def _drift_recommendation(dimension: DimensionType, survey_id: str) -> str:
    if dimension == "js":
        return (
            f"Run 'qsync survey pull --survey-id {survey_id}' "
            "to refresh cache before continuing. Or use --allow-drift to proceed anyway "
            "(changes may overwrite recent API edits)."
        )
    if dimension == "flow":
        return (
            f"Run 'qsync flow pull --survey-id {survey_id}' "
            "to refresh flow baseline before continuing. Or use --allow-drift to proceed anyway "
            "(changes may overwrite recent API edits)."
        )
    return (
        f"Run 'qsync {dimension} pull' to refresh cache before continuing. "
        "Or use --allow-drift to proceed anyway (changes may overwrite recent API edits)."
    )


def _check_flow_drift(survey_id: str, *, context: dict | None = None) -> DriftReport:
    """Check for flow drift between baseline and live API.

    Args:
        survey_id: Survey ID to check
        context: Optional context (not used currently)

    Returns:
        DriftReport with drift status
    """
    from .dimensions.flow import _baseline_path

    baseline_path = _baseline_path(survey_id)

    if not baseline_path.exists():
        return DriftReport(
            has_drift=False,
            summary="No flow baseline found",
            diff_lines=[],
            recommendation=f"Run 'qsync flow pull --survey-id {survey_id}' to initialize flow.",
            changed_count=0,
        )

    # Load baseline
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except Exception as e:
        return DriftReport(
            has_drift=False,
            summary=f"Could not read flow baseline: {e}",
            diff_lines=[],
            recommendation=f"Run 'qsync flow pull --survey-id {survey_id}' to refresh.",
            changed_count=0,
        )

    # Fetch live flow from API
    live_payload = fetch_survey_definition_live(survey_id)
    live_flow = live_payload.get("result", {}).get("SurveyFlow", {})

    # Compare flows
    baseline_json = json.dumps(baseline, indent=2, sort_keys=True)
    live_json = json.dumps(live_flow, indent=2, sort_keys=True)

    if baseline_json == live_json:
        return DriftReport(
            has_drift=False,
            summary="Flow matches API",
            diff_lines=[],
            recommendation="No action needed.",
            changed_count=0,
        )

    # Generate diff
    diff_lines = list(
        difflib.unified_diff(
            baseline_json.splitlines(keepends=False),
            live_json.splitlines(keepends=False),
            fromfile="baseline [local]",
            tofile="live [Qualtrics]",
            lineterm="",
        )
    )

    # Count changes
    changed_count = sum(
        1
        for line in diff_lines
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )
    additions = sum(
        1 for line in diff_lines if line.startswith("+") and not line.startswith("+++")
    )
    deletions = sum(
        1 for line in diff_lines if line.startswith("-") and not line.startswith("---")
    )

    return DriftReport(
        has_drift=True,
        summary="Flow differs from API",
        diff_lines=diff_lines,
        recommendation=_drift_recommendation("flow", survey_id),
        context_lines=[
            f"context: baseline={baseline_path}, remote=Qualtrics live survey flow",
        ],
        changed_count=changed_count,
        additions=additions,
        deletions=deletions,
    )
