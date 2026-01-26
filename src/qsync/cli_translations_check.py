"""Translation language validation using language detection.

This module provides functionality to check if translations are in the correct language
using language detection. It validates:
- Question text, choices, and labels from survey definitions
- JavaScript COPY object strings
- End of Survey messages

Single-word strings can be skipped when allowed (common for technical terms).
"""

from __future__ import annotations

import argparse
import html
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import importlib
import json
import subprocess
import sys
from typing import Any

try:
    import fasttext  # type: ignore[import-not-found]
except Exception:
    fasttext = None
from langdetect import DetectorFactory, LangDetectException, detect_langs
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from qsync.config import resolve_root
from qsync.dimensions.translations_language_blocks import (
    get_base_language,
    list_enabled_languages,
)
from qsync.flow_traversal import flow_order_map, scenario_qid_order
from qsync.markdown_codec import html_to_md, normalize_text
from qsync.qualtrics_client import load_cached_survey
from qsync.translation_export import _find_copy_object, _parse_js_object_blocks
from qsync.translations_utils import normalize_language_code, normalize_language_list

# Make langdetect deterministic
DetectorFactory.seed = 0

DEFAULT_LANGUAGES = ["FR", "NL", "CS"]
DEFAULT_MIN_CONFIDENCE = 0.85
DEFAULT_MIN_MARGIN = 0.15
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")
_HEX_ESCAPE_RE = re.compile(r"\\x([0-9a-fA-F]{2})")
_CONFUSION_MAP = {
    "nl": {"af"},
    "af": {"nl"},
    "cs": {"sk"},
    "sk": {"cs"},
}
_PLACEHOLDER_TEXTS = {
    "click to write the question text",
    "click to write the question text.",
}
_META_LABELS = {
    "first click",
    "last click",
    "page submit",
    "click count",
    "operating system",
    "screen resolution",
    "flash version",
    "java support",
    "user agent",
}
_PIPED_TEXT_RE = re.compile(r"\$\{[^}]+\}")
_BRACE_TOKEN_RE = re.compile(r"\{[A-Za-z0-9_]+\}")
_LETTER_WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]+")
_NEUTRAL_BRANDS = {
    "instagram",
    "tiktok",
    "youtube",
    "youtube shorts",
    "shorts",
    "google news",
    "apple news",
    "facebook",
    "x",
    "twitter",
    "whatsapp",
    "signal",
    "telegram",
    "reddit",
    "snapchat",
    "twitch",
    "spotify",
    "netflix",
}

_FASTTEXT_MODEL = None
_FASTTEXT_MODEL_PATH: Path | None = None
_FASTTEXT_MODEL_ERROR: str | None = None
_FASTTEXT_MODEL_URL = (
    "https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.ftz"
)
_FASTTEXT_PROMPT_FILENAME = ".fasttext_setup.json"


@dataclass
class CheckStats:
    checked: int = 0
    skipped: int = 0
    uncertain: int = 0
    issues: int = 0
    untranslated: int = 0


@dataclass
class LanguageDecision:
    status: str  # pass|fail|uncertain|skip
    detected: str | None
    normalized: str
    expected_prob: float | None = None
    signal_score: float | None = None
    effective_min_confidence: float | None = None


def _signal_stats(text: str) -> tuple[int, int, float]:
    letters = _LETTER_WORD_RE.findall(text or "")
    alpha_count = sum(len(word) for word in letters)
    total_chars = len(text or "")
    letter_ratio = (alpha_count / total_chars) if total_chars else 0.0
    return alpha_count, total_chars, letter_ratio


def _signal_score(alpha_count: int, letter_ratio: float) -> float:
    alpha_score = min(1.0, alpha_count / 20.0) if alpha_count > 0 else 0.0
    ratio_score = min(1.0, letter_ratio / 0.6) if letter_ratio > 0 else 0.0
    return 0.5 * alpha_score + 0.5 * ratio_score


def _is_numeric_only(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return True
    if not any(ch.isdigit() for ch in stripped):
        return False
    clean = re.sub(r"[€$£¥%]", "", stripped)
    clean = re.sub(
        r"\b(?:kč|czk|usd|eur|gbp|sek|nok|dkk)\b", "", clean, flags=re.IGNORECASE
    )
    clean = re.sub(r"\b(?:min|max|total)\b", "", clean, flags=re.IGNORECASE)
    if not _LETTER_WORD_RE.search(clean):
        return True
    alpha_count, total_chars, letter_ratio = _signal_stats(clean)
    if letter_ratio < 0.2:
        return True
    return False


def _is_neutral_brand_list(text: str) -> bool:
    normalized = (text or "").lower()
    parts = re.split(r"[,&/()]|\s+", normalized)
    tokens = [p.strip(" .:-") for p in parts if p.strip(" .:-")]
    if not tokens:
        return False
    for token in tokens:
        if token not in _NEUTRAL_BRANDS:
            return False
    return True


def _unescape_text(text: str) -> str:
    if text is None:
        return ""
    raw = str(text)
    raw = html.unescape(raw)

    def _unicode_repl(match: re.Match) -> str:
        return chr(int(match.group(1), 16))

    def _hex_repl(match: re.Match) -> str:
        return chr(int(match.group(1), 16))

    raw = _UNICODE_ESCAPE_RE.sub(_unicode_repl, raw)
    raw = _HEX_ESCAPE_RE.sub(_hex_repl, raw)
    raw = (
        raw.replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace("\\r", "\r")
        .replace("\\f", "\f")
        .replace("\\'", "'")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
    )
    return raw


def _strip_html_tags(text: str) -> str:
    return _HTML_TAG_RE.sub(" ", text or "")


def _strip_formatting_to_plain_text(text: str) -> str:
    """Strip HTML and Markdown formatting to get plain text.

    Args:
        text: Text with possible HTML/Markdown formatting

    Returns:
        Plain text with formatting removed
    """
    text = _unescape_text(text or "")

    # Normalize survey-style gender parens
    text = re.sub(r"\(e\)", " e", text)
    text = re.sub(r"\(ne\)", " ne", text)

    # First convert HTML to Markdown if needed
    if "<" in text and ">" in text:
        text = html_to_md(text)

    # Remove any remaining HTML tags explicitly
    text = _strip_html_tags(text)

    # Normalize range dashes for tokenization
    text = re.sub(r"[–—-]", " ", text)

    # Strip common markdown formatting
    # Remove bold/italic markers
    text = re.sub(r"\*\*([^\*]+)\*\*", r"\1", text)  # **bold**
    text = re.sub(r"\*([^\*]+)\*", r"\1", text)  # *italic*
    text = re.sub(r"__([^_]+)__", r"\1", text)  # __bold__
    text = re.sub(r"_([^_]+)_", r"\1", text)  # _italic_

    # Remove links but keep text
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)  # [text](url)

    # Remove inline code backticks
    text = re.sub(r"`([^`]+)`", r"\1", text)

    # Remove Qualtrics piped text + simple brace tokens
    text = _PIPED_TEXT_RE.sub(" ", text)
    text = _BRACE_TOKEN_RE.sub(" ", text)

    # Normalize whitespace
    text = normalize_text(text)

    return text


def _prepare_detection_text(text: str) -> str:
    normalized = _strip_formatting_to_plain_text(text)
    normalized = normalized.replace("/", " ")
    words = _LETTER_WORD_RE.findall(normalized)
    if words:
        return " ".join(words)
    return normalized


def _resolve_fasttext_model_path() -> Path | None:
    env_path = os.getenv("QSYNC_FASTTEXT_MODEL")
    if env_path:
        return Path(env_path).expanduser()
    root = resolve_root(required=False) or Path.cwd()
    candidates = [
        root / "models" / "lid.176.ftz",
        root / "resources" / "lid.176.ftz",
        root / "packages" / "qsync" / "src" / "qsync" / "resources" / "lid.176.ftz",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _fasttext_prompt_state_path(root: Path) -> Path:
    return root / "surveys" / _FASTTEXT_PROMPT_FILENAME


def _load_fasttext_prompt_state(root: Path) -> dict[str, Any]:
    path = _fasttext_prompt_state_path(root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_fasttext_prompt_state(root: Path, state: dict[str, Any]) -> None:
    path = _fasttext_prompt_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _install_fasttext_module() -> tuple[bool, str | None]:
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "fasttext-wheel>=0.9.2"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return True, None
    except subprocess.CalledProcessError as exc:
        err = exc.stderr.strip() if exc.stderr else str(exc)
        return False, err


def _download_fasttext_model(dest_path: Path) -> tuple[bool, str | None]:
    try:
        import requests
    except Exception as exc:
        return False, f"requests import failed: {exc}"
    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dest_path.with_suffix(".tmp")
        with requests.get(_FASTTEXT_MODEL_URL, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            with open(tmp_path, "wb") as handle:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)
        tmp_path.replace(dest_path)
        return True, None
    except Exception as exc:
        return False, str(exc)


def _ensure_fasttext_setup(interactive: bool, root: Path, console: Console) -> None:
    global fasttext
    if fasttext is not None and _resolve_fasttext_model_path():
        return

    state = _load_fasttext_prompt_state(root)
    if state.get("prompted") and state.get("declined"):
        return

    if not interactive:
        return

    try:
        import questionary
    except Exception:
        return

    prompt = (
        "FastText is optional but improves language checks. Install fasttext "
        + "and download the model now?"
    )
    wants_setup = questionary.confirm(prompt, default=False).ask()
    _write_fasttext_prompt_state(root, {"prompted": True, "declined": not wants_setup})
    if not wants_setup:
        return

    console.print("[dim]Installing fasttext-wheel...[/dim]")
    ok, err = _install_fasttext_module()
    if not ok:
        console.print(f"[red]fasttext install failed:[/red] {err}")
        return

    try:
        fasttext = importlib.import_module("fasttext")
    except Exception as exc:
        console.print(f"[red]fasttext import failed:[/red] {exc}")
        return

    model_path = root / "models" / "lid.176.ftz"
    if not model_path.exists():
        console.print("[dim]Downloading FastText model (lid.176.ftz)...[/dim]")
        ok, err = _download_fasttext_model(model_path)
        if not ok:
            console.print(f"[red]model download failed:[/red] {err}")
            return

    _load_fasttext_model()


def _load_fasttext_model():
    global _FASTTEXT_MODEL, _FASTTEXT_MODEL_PATH, _FASTTEXT_MODEL_ERROR
    if _FASTTEXT_MODEL is not None:
        return _FASTTEXT_MODEL
    if fasttext is None:
        _FASTTEXT_MODEL_ERROR = "fasttext module not installed"
        return None
    model_path = _resolve_fasttext_model_path()
    if not model_path or not model_path.exists():
        _FASTTEXT_MODEL_ERROR = "fasttext model not found"
        return None
    try:
        _FASTTEXT_MODEL = fasttext.load_model(str(model_path))
        _FASTTEXT_MODEL_PATH = model_path
        return _FASTTEXT_MODEL
    except Exception as exc:
        _FASTTEXT_MODEL_ERROR = f"fasttext load failed: {exc}"
        return None


def _fasttext_predict_safe(model, text: str, *, k: int = 5):
    def _check(entry: str) -> str:
        if "\n" in entry:
            raise ValueError("predict processes one line at a time (remove '\\n')")
        return entry + "\n"

    try:
        import numpy as np
    except Exception as exc:
        raise RuntimeError(
            f"numpy is required for fasttext predictions: {exc}"
        ) from exc

    text = _check(text)
    try:
        predictions = model.f.predict(text, k, 0.0, "strict")
    except TypeError:
        predictions = model.f.predict(text, k)

    if predictions:
        probs, labels = zip(*predictions)
    else:
        probs, labels = ([], ())
    return labels, np.asarray(probs)


def _detect_language_probs(text: str) -> list[tuple[str, float]]:
    model = _load_fasttext_model()
    if model is not None:
        labels, probs = _fasttext_predict_safe(model, text, k=5)
        results: list[tuple[str, float]] = []
        for label, prob in zip(labels, probs):
            lang = str(label).replace("__label__", "").strip().lower()
            results.append((lang, float(prob)))
        return results
    try:
        return [(p.lang, p.prob) for p in detect_langs(text)]
    except LangDetectException:
        return []


def _detector_label() -> str:
    if _FASTTEXT_MODEL is not None:
        path = str(_FASTTEXT_MODEL_PATH) if _FASTTEXT_MODEL_PATH else "fasttext"
        return f"fasttext ({path})"
    if fasttext is None:
        return "langdetect (fasttext module missing)"
    if _FASTTEXT_MODEL_ERROR:
        return f"langdetect ({_FASTTEXT_MODEL_ERROR})"
    return "langdetect"


def _is_placeholder_text(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return True
    if lowered in _PLACEHOLDER_TEXTS:
        return True
    if lowered in _META_LABELS:
        return True
    if lowered.startswith("click to write the question text"):
        return True
    return False


def _is_untranslated(
    text: str,
    base_text: str | None,
    *,
    allow_single_words: bool,
) -> bool:
    if base_text is None:
        return False
    normalized = _strip_formatting_to_plain_text(text or "").strip()
    base_normalized = _strip_formatting_to_plain_text(base_text or "").strip()
    if not normalized or not base_normalized:
        return False
    if _is_placeholder_text(normalized):
        return False
    alpha_count, _total_chars, _ratio = _signal_stats(normalized)
    if alpha_count < 4:
        return False
    if _is_numeric_only(normalized):
        return False
    if _is_neutral_brand_list(normalized):
        return False
    words = _LETTER_WORD_RE.findall(normalized)
    if allow_single_words and len(words) == 1:
        return False
    return normalized.casefold() == base_normalized.casefold()


def _is_meta_question(question: dict[str, Any]) -> bool:
    qtype = str(question.get("QuestionType") or "").strip().lower()
    if qtype in {"timing", "meta"}:
        return True
    description = str(question.get("QuestionDescription") or "").strip().lower()
    if "timing" in description or "meta" in description:
        return True
    return False


def _risk_label(decision: LanguageDecision) -> str:
    if decision.expected_prob is None or decision.effective_min_confidence is None:
        return "Ambiguous"
    text_len = len(decision.normalized or "")
    if decision.expected_prob < decision.effective_min_confidence:
        return "High Risk" if text_len > 50 else "Ambiguous"
    return "Review"


def check_translation_language(
    text: str,
    expected_lang: str,
    *,
    allow_single_words: bool,
    min_confidence: float,
    min_margin: float,
) -> LanguageDecision:
    """Check if text is in the expected language using a binary hypothesis test."""
    normalized = _strip_formatting_to_plain_text(text or "").strip()
    if not normalized:
        return LanguageDecision(status="skip", detected=None, normalized=normalized)
    if _is_placeholder_text(normalized):
        return LanguageDecision(status="skip", detected=None, normalized=normalized)

    alpha_count, total_chars, letter_ratio = _signal_stats(normalized)
    if alpha_count < 4:
        return LanguageDecision(status="skip", detected=None, normalized=normalized)
    if _is_numeric_only(normalized):
        return LanguageDecision(status="skip", detected=None, normalized=normalized)
    if _is_neutral_brand_list(normalized):
        return LanguageDecision(status="skip", detected=None, normalized=normalized)

    words = _LETTER_WORD_RE.findall(normalized)
    if allow_single_words and len(words) == 1:
        return LanguageDecision(status="skip", detected=None, normalized=normalized)

    expected_norm = normalize_language_code(expected_lang)
    expected_lower = expected_norm.lower()
    if not expected_lower:
        return LanguageDecision(status="skip", detected=None, normalized=normalized)

    detection_text = _prepare_detection_text(normalized)
    if not detection_text.strip():
        return LanguageDecision(status="skip", detected=None, normalized=normalized)

    probs = _detect_language_probs(detection_text)
    if not probs:
        return LanguageDecision(
            status="uncertain",
            detected=None,
            normalized=normalized,
            expected_prob=None,
        )

    probs_sorted = sorted(probs, key=lambda p: p[1], reverse=True)
    top_lang, top_prob = probs_sorted[0]
    runner_prob = probs_sorted[1][1] if len(probs_sorted) > 1 else 0.0
    margin = top_prob - runner_prob

    signal_score = _signal_score(alpha_count, letter_ratio)
    effective_min_conf = min_confidence * (0.3 + 0.7 * signal_score)
    expected_prob = 0.0
    for lang, prob in probs_sorted:
        if lang == expected_lower:
            expected_prob = prob
            break

    if expected_lower in _CONFUSION_MAP and top_lang in _CONFUSION_MAP[expected_lower]:
        if top_prob >= min_confidence:
            return LanguageDecision(
                status="pass",
                detected=top_lang,
                normalized=normalized,
                expected_prob=expected_prob,
                signal_score=signal_score,
                effective_min_confidence=effective_min_conf,
            )

    if expected_prob >= effective_min_conf:
        return LanguageDecision(
            status="pass",
            detected=top_lang,
            normalized=normalized,
            expected_prob=expected_prob,
            signal_score=signal_score,
            effective_min_confidence=effective_min_conf,
        )

    if expected_prob >= max(effective_min_conf * 0.7, 0.5):
        if (expected_prob - runner_prob) >= min_margin:
            return LanguageDecision(
                status="pass",
                detected=top_lang,
                normalized=normalized,
                expected_prob=expected_prob,
                signal_score=signal_score,
                effective_min_confidence=effective_min_conf,
            )

    if top_lang == expected_lower and top_prob >= max(effective_min_conf * 0.7, 0.5):
        if margin >= min_margin:
            return LanguageDecision(
                status="pass",
                detected=top_lang,
                normalized=normalized,
                expected_prob=expected_prob,
                signal_score=signal_score,
                effective_min_confidence=effective_min_conf,
            )
        return LanguageDecision(
            status="uncertain",
            detected=top_lang,
            normalized=normalized,
            expected_prob=expected_prob,
            signal_score=signal_score,
            effective_min_confidence=effective_min_conf,
        )

    if expected_prob > 0 and (top_prob - expected_prob) <= min_margin:
        return LanguageDecision(
            status="uncertain",
            detected=top_lang,
            normalized=normalized,
            expected_prob=expected_prob,
            signal_score=signal_score,
            effective_min_confidence=effective_min_conf,
        )

    return LanguageDecision(
        status="fail",
        detected=top_lang,
        normalized=normalized,
        expected_prob=expected_prob,
        signal_score=signal_score,
        effective_min_confidence=effective_min_conf,
    )


def _extract_strings_from_language_block(
    question: dict[str, Any],
    qid: str,
    lang_code: str,
    base_language: str | None,
) -> list[tuple[str, str, str, str]]:
    """Extract all translated strings from a question's language block.

    Args:
        question: Question dictionary from survey payload
        qid: Question ID
        lang_code: Language code (FR, NL, CS)

    Returns:
        List of (key, text, base_text, qid) tuples
    """
    results = []
    base_lang = normalize_language_code(base_language or "")
    is_base = normalize_language_code(lang_code) == base_lang
    language = question.get("Language", {})
    if not isinstance(language, dict):
        return results
    lang_block = language.get(lang_code, {})

    if not lang_block:
        return results

    # Extract QuestionText
    question_text = lang_block.get("QuestionText")
    if question_text:
        base_text = None
        if base_lang and not is_base:
            base_text = question.get("QuestionText") or ""
        results.append((f"{qid}_QuestionText", question_text, base_text or "", qid))

    # Extract Choices
    choices = lang_block.get("Choices", {})
    for choice_id, choice_data in choices.items():
        if isinstance(choice_data, dict):
            display = choice_data.get("Display")
            if display:
                base_text = None
                if base_lang and not is_base:
                    base_choice = (question.get("Choices") or {}).get(choice_id) or {}
                    if isinstance(base_choice, dict):
                        base_text = base_choice.get("Display") or ""
                results.append(
                    (f"{qid}_Choice_{choice_id}", display, base_text or "", qid)
                )

    # Extract Answers (for matrix questions)
    answers = lang_block.get("Answers", {})
    for answer_id, answer_data in answers.items():
        if isinstance(answer_data, dict):
            display = answer_data.get("Display")
            if display:
                base_text = None
                if base_lang and not is_base:
                    base_answer = (question.get("Answers") or {}).get(answer_id) or {}
                    if isinstance(base_answer, dict):
                        base_text = base_answer.get("Display") or ""
                results.append(
                    (f"{qid}_Answer_{answer_id}", display, base_text or "", qid)
                )

    # Extract ChoiceGroups
    choice_groups = lang_block.get("ChoiceGroups", {})
    for group_id, group_data in choice_groups.items():
        if isinstance(group_data, dict):
            description = group_data.get("Description")
            if description:
                base_text = None
                if base_lang and not is_base:
                    base_group = (question.get("ChoiceGroups") or {}).get(
                        group_id
                    ) or {}
                    if isinstance(base_group, dict):
                        base_text = base_group.get("Description") or ""
                results.append(
                    (f"{qid}_ChoiceGroup_{group_id}", description, base_text or "", qid)
                )

    # Extract SubQuestions
    sub_questions = lang_block.get("SubQuestions", {})
    for sub_id, sub_data in sub_questions.items():
        if isinstance(sub_data, dict):
            description = sub_data.get("Description")
            if description:
                base_text = None
                if base_lang and not is_base:
                    base_sub = (question.get("SubQuestions") or {}).get(sub_id) or {}
                    if isinstance(base_sub, dict):
                        base_text = base_sub.get("Description") or ""
                results.append(
                    (
                        f"{qid}_SubQuestion_{sub_id}",
                        description,
                        base_text or "",
                        qid,
                    )
                )

    return results


def check_survey_item_translations(
    payload: dict,
    languages: list[str],
    *,
    allow_single_words: bool,
    min_confidence: float,
    min_margin: float,
    skip_meta: bool,
    allowed_qids: set[str] | None = None,
    base_language: str | None = None,
) -> tuple[dict[str, dict[str, list[tuple[str, str, float | None, str]]]], CheckStats]:
    """Check all item translations from survey definition."""
    questions = payload.get("result", {}).get("Questions", {})
    results: dict[str, dict[str, list[tuple[str, str, str]]]] = {
        lang: defaultdict(list) for lang in languages
    }
    stats = CheckStats()

    for qid, question in questions.items():
        if not isinstance(question, dict):
            continue
        if allowed_qids is not None and qid not in allowed_qids:
            continue

        if skip_meta and _is_meta_question(question):
            for lang in languages:
                strings = _extract_strings_from_language_block(
                    question, qid, lang, base_language
                )
                stats.skipped += len(strings)
            continue

        for lang in languages:
            strings = _extract_strings_from_language_block(
                question, qid, lang, base_language
            )

            for key, text, base_text, _ in strings:
                decision = check_translation_language(
                    text,
                    lang,
                    allow_single_words=allow_single_words,
                    min_confidence=min_confidence,
                    min_margin=min_margin,
                )
                if decision.status == "skip":
                    stats.skipped += 1
                    continue
                stats.checked += 1
                if _is_untranslated(
                    text,
                    base_text,
                    allow_single_words=allow_single_words,
                ):
                    stats.untranslated += 1
                    stats.issues += 1
                    results[lang][qid].append(
                        (
                            key,
                            decision.normalized,
                            decision.expected_prob,
                            "Untranslated",
                        )
                    )
                    continue
                if decision.status == "uncertain":
                    stats.uncertain += 1
                    continue
                if decision.status == "fail":
                    stats.issues += 1
                    results[lang][qid].append(
                        (
                            key,
                            decision.normalized,
                            decision.expected_prob,
                            _risk_label(decision),
                        )
                    )

    return results, stats


def check_js_copy_translations(
    payload: dict,
    languages: list[str],
    *,
    allow_single_words: bool,
    min_confidence: float,
    min_margin: float,
    allowed_qids: set[str] | None = None,
    base_language: str | None = None,
) -> tuple[dict[str, dict[str, list[tuple[str, str, float | None, str]]]], CheckStats]:
    """Check JavaScript COPY objects for correct language."""
    questions = payload.get("result", {}).get("Questions", {})

    results: dict[str, dict[str, list[tuple[str, str, str]]]] = {
        lang: defaultdict(list) for lang in languages
    }
    stats = CheckStats()

    for qid, question in questions.items():
        if not isinstance(question, dict):
            continue
        if allowed_qids is not None and qid not in allowed_qids:
            continue

        js_code = question.get("QuestionJS") or question.get("QuestionJSContent")
        if not js_code:
            continue

        copy_obj = _find_copy_object(js_code)
        if not copy_obj:
            continue

        lang_blocks = _parse_js_object_blocks(copy_obj)

        base_lang = normalize_language_code(base_language or "")
        base_strings: list[str] | None = None
        if base_lang:
            base_block = lang_blocks.get(base_lang)
            if base_block:
                base_strings = []
                string_pattern = re.compile(
                    r"""(?:['"`])([^'"`\\]*(?:\\.[^'"`\\]*)*)(?:['"`])"""
                )
                for match in string_pattern.finditer(base_block):
                    base_text = match.group(1)
                    base_text = base_text.replace("\\n", "\n")
                    base_text = base_text.replace("\\t", "\t")
                    base_text = base_text.replace("\\'", "'")
                    base_text = base_text.replace('\\"', '"')
                    base_text = base_text.replace("\\\\", "\\")
                    base_text = base_text.strip()
                    if len(base_text) < 3:
                        continue
                    base_strings.append(base_text)

        for lang in languages:
            block = lang_blocks.get(lang)
            if not block:
                continue

            string_pattern = re.compile(
                r"""(?:['"`])([^'"`\\]*(?:\\.[^'"`\\]*)*)(?:['"`])"""
            )

            strings: list[str] = []
            for match in string_pattern.finditer(block):
                text = match.group(1)
                text = text.replace("\\n", "\n")
                text = text.replace("\\t", "\t")
                text = text.replace("\\'", "'")
                text = text.replace('\\"', '"')
                text = text.replace("\\\\", "\\")
                text = text.strip()
                if len(text) < 3:
                    continue
                strings.append(text)

            for idx, text in enumerate(strings):

                decision = check_translation_language(
                    text,
                    lang,
                    allow_single_words=allow_single_words,
                    min_confidence=min_confidence,
                    min_margin=min_margin,
                )
                if decision.status == "skip":
                    stats.skipped += 1
                    continue
                stats.checked += 1
                base_text = None
                if base_lang and normalize_language_code(lang) == base_lang:
                    base_text = None
                elif base_strings and idx < len(base_strings):
                    base_text = base_strings[idx]
                if _is_untranslated(
                    text,
                    base_text,
                    allow_single_words=allow_single_words,
                ):
                    stats.untranslated += 1
                    stats.issues += 1
                    key = f"JS:{decision.normalized[:20]}"
                    results[lang][qid].append(
                        (
                            key,
                            decision.normalized,
                            decision.expected_prob,
                            "Untranslated",
                        )
                    )
                    continue
                if decision.status == "uncertain":
                    stats.uncertain += 1
                    continue
                if decision.status == "fail":
                    stats.issues += 1
                    key = f"JS:{decision.normalized[:20]}"
                    results[lang][qid].append(
                        (
                            key,
                            decision.normalized,
                            decision.expected_prob,
                            _risk_label(decision),
                        )
                    )

    return results, stats


def _looks_like_library_id(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if " " in text:
        return False
    if len(text) < 6:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_-]+", text))


def check_eos_message_translations(
    payload: dict,
    languages: list[str],
    *,
    allow_single_words: bool,
    min_confidence: float,
    min_margin: float,
    base_language: str | None = None,
) -> tuple[dict[str, list[tuple[str, str, float | None, str]]], CheckStats]:
    """Check EOS (End of Survey) message translations."""
    results: dict[str, list[tuple[str, str, str]]] = {lang: [] for lang in languages}
    stats = CheckStats()

    eos = payload.get("result", {}).get("SurveyOptions", {}).get("EndOfSurvey", {})
    if not eos:
        return results, stats

    base_lang = normalize_language_code(base_language or "")
    base_block = eos.get(base_lang, {}) if base_lang else {}

    for lang in languages:
        lang_block = eos.get(lang, {})
        if not lang_block:
            continue

        for field_name in ["Message", "MessageLibrary", "MessageHTML"]:
            message = lang_block.get(field_name)
            if not message:
                continue
            if field_name == "MessageLibrary" and _looks_like_library_id(message):
                stats.skipped += 1
                continue
            base_message = None
            if base_lang and normalize_language_code(lang) != base_lang:
                base_message = (base_block or {}).get(field_name)

            decision = check_translation_language(
                message,
                lang,
                allow_single_words=allow_single_words,
                min_confidence=min_confidence,
                min_margin=min_margin,
            )
            if decision.status == "skip":
                stats.skipped += 1
                continue
            stats.checked += 1
            if _is_untranslated(
                message,
                base_message,
                allow_single_words=allow_single_words,
            ):
                stats.untranslated += 1
                stats.issues += 1
                key = f"EOS_{field_name}"
                results[lang].append(
                    (
                        key,
                        decision.normalized,
                        decision.expected_prob,
                        "Untranslated",
                    )
                )
                continue
            if decision.status == "uncertain":
                stats.uncertain += 1
                continue
            if decision.status == "fail":
                stats.issues += 1
                key = f"EOS_{field_name}"
                results[lang].append(
                    (
                        key,
                        decision.normalized,
                        decision.expected_prob,
                        _risk_label(decision),
                    )
                )

    return results, stats


def format_check_results(
    items_results: dict[str, dict[str, list[tuple[str, str, float | None, str]]]],
    js_results: dict[str, dict[str, list[tuple[str, str, float | None, str]]]],
    eos_results: dict[str, list[tuple[str, str, float | None, str]]],
    items_stats: CheckStats,
    js_stats: CheckStats,
    eos_stats: CheckStats,
    languages: list[str],
    base_language: str,
    min_confidence: float,
    min_margin: float,
    allow_single_words: bool,
    skip_meta: bool,
    skip_js: bool,
    skip_eos: bool,
    qid_order: dict[str, int],
    detector_label: str,
    dedupe: bool,
    survey_name: str,
    survey_id: str,
    edf_overrides: dict[str, str] | None = None,
    allowed_qids: set[str] | None = None,
) -> None:
    """Format and print results with color, tables, and grouping.

    Args:
        items_results: Results from check_survey_item_translations()
        js_results: Results from check_js_copy_translations()
        eos_results: Results from check_eos_message_translations()
        items_stats: Item check stats
        js_stats: JS check stats
        eos_stats: EOS check stats
        languages: Target languages being checked
        base_language: Survey base language
        min_confidence: Confidence threshold for acceptance
        min_margin: Margin threshold for acceptance
        allow_single_words: Whether to skip single-word strings
        skip_meta: Whether meta questions were skipped
        skip_js: Whether JS checks were skipped
        skip_eos: Whether EOS checks were skipped
        detector_label: Detector label for summary output
        dedupe: Whether to deduplicate identical text across QIDs
        survey_name: Survey name for display
        survey_id: Survey ID for display
    """
    console = Console()

    items_issues = items_stats.issues
    js_issues = js_stats.issues
    eos_issues = eos_stats.issues
    total_issues = items_issues + js_issues + eos_issues
    total_checked = items_stats.checked + js_stats.checked + eos_stats.checked
    total_skipped = items_stats.skipped + js_stats.skipped + eos_stats.skipped
    total_uncertain = items_stats.uncertain + js_stats.uncertain + eos_stats.uncertain
    total_untranslated = (
        items_stats.untranslated + js_stats.untranslated + eos_stats.untranslated
    )
    confident_checked = max(total_checked - total_uncertain, 0)

    # Header
    title = f"Translation Language Check: {survey_name} ({survey_id})"
    console.print(Panel(title, style="bold blue", expand=False))
    console.print()

    # Summary
    pass_rate = (
        (confident_checked - total_issues) / confident_checked * 100
        if confident_checked > 0
        else 100
    )
    console.print("[bold]Summary:[/bold]")
    console.print(f"✓ Total strings checked: [cyan]{total_checked}[/cyan]")
    if total_skipped:
        console.print(
            f"• Skipped (empty/placeholder/meta/single-word): [cyan]{total_skipped}[/cyan]"
        )
    if total_uncertain:
        console.print(
            f"• Uncertain (close calls, not shown): [yellow]{total_uncertain}[/yellow]"
        )
    if total_issues > 0:
        console.print(f"✗ Issues found: [red bold]{total_issues}[/red bold]")
    else:
        console.print(f"✓ Issues found: [green]{total_issues}[/green]")
    if total_untranslated:
        console.print(
            f"• Untranslated (same as base): [yellow]{total_untranslated}[/yellow]"
        )
    console.print(f"📊 Pass rate (confident): [cyan]{pass_rate:.1f}%[/cyan]")
    console.print(
        f"• Languages: [cyan]{', '.join(languages)}[/cyan] (base={base_language})"
    )
    console.print(
        f"• Thresholds: min_confidence={min_confidence:.2f}, min_margin={min_margin:.2f}"
    )
    console.print(
        f"• Flags: allow_single_word={'yes' if allow_single_words else 'no'}, "
        f"skip_meta={'yes' if skip_meta else 'no'}, "
        f"skip_js={'yes' if skip_js else 'no'}, "
        f"skip_eos={'yes' if skip_eos else 'no'}"
    )
    console.print(f"• Detector: {detector_label}")
    if edf_overrides:
        edf_list = ", ".join([f"{k}={v}" for k, v in sorted(edf_overrides.items())])
        scope_count = len(allowed_qids) if allowed_qids is not None else 0
        console.print(
            f"• EDF filter: [cyan]{edf_list}[/cyan] (QIDs in scope: {scope_count})"
        )
    console.print()

    # If no issues, we're done
    if total_issues == 0:
        console.print(
            "[bold green]✓ All translations are in the correct language![/bold green]"
        )
        console.print()
        console.print(
            "[dim]Note: Single-word strings can be skipped when allowed.[/dim]"
        )
        return

    rows_by_lang: dict[str, list[tuple[int, str, str, str, str, str, str]]] = {
        lang: [] for lang in languages
    }
    fallback_index = len(qid_order) + 10_000

    for lang in languages:
        for qid, issues in items_results[lang].items():
            for key, text, expected_prob, status in issues:
                preview = text[:37] + "..." if len(text) > 40 else text
                preview = preview.replace("\n", " ")
                prob_display = (
                    f"{expected_prob:.2f}" if expected_prob is not None else "n/a"
                )
                flow_index = qid_order.get(qid, fallback_index)
                rows_by_lang[lang].append(
                    (flow_index, qid, key[-12:], preview, lang, prob_display, status)
                )

    for lang in languages:
        for qid, issues in js_results[lang].items():
            for key, text, expected_prob, status in issues:
                preview = text[:37] + "..." if len(text) > 40 else text
                preview = preview.replace("\n", " ")
                prob_display = (
                    f"{expected_prob:.2f}" if expected_prob is not None else "n/a"
                )
                flow_index = qid_order.get(qid, fallback_index)
                rows_by_lang[lang].append(
                    (flow_index, qid, key, preview, lang, prob_display, status)
                )

    for lang in languages:
        for key, text, expected_prob, status in eos_results[lang]:
            preview = text[:37] + "..." if len(text) > 40 else text
            preview = preview.replace("\n", " ")
            prob_display = (
                f"{expected_prob:.2f}" if expected_prob is not None else "n/a"
            )
            rows_by_lang[lang].append(
                (fallback_index, "EOS", key, preview, lang, prob_display, status)
            )

    for lang in languages:
        rows = rows_by_lang.get(lang, [])
        if not rows:
            continue
        rows.sort(key=lambda r: (r[0], r[1], r[2]))
        table = Table(
            title=f"Translation Language Issues ({lang})",
            show_header=True,
            header_style="bold",
        )
        table.add_column("QID", style="yellow", width=16)
        table.add_column("Key", style="magenta", width=15)
        table.add_column("Text Preview", width=40)
        table.add_column("Status", style="red", width=10)
        table.add_column("P(target)", style="red", width=9)
        if dedupe:
            merged: dict[str, dict[str, Any]] = {}
            for flow_index, qid, key, preview, expected, prob_display, status in rows:
                merge_key = f"{preview}|{key}|{status}"
                entry = merged.get(merge_key)
                if not entry:
                    merged[merge_key] = {
                        "flow_index": flow_index,
                        "qids": [qid],
                        "key": key,
                        "preview": preview,
                        "expected": expected,
                        "prob_display": prob_display,
                        "status": status,
                    }
                else:
                    entry["qids"].append(qid)
                    entry["flow_index"] = min(entry["flow_index"], flow_index)
            merged_rows = list(merged.values())
            merged_rows.sort(key=lambda r: (r["flow_index"], r["qids"][0]))
            for entry in merged_rows:
                qids = entry["qids"]
                if len(qids) > 3:
                    qid_display = ", ".join(qids[:3]) + f" +{len(qids) - 3}"
                else:
                    qid_display = ", ".join(qids)
                status = entry["status"]
                if status == "High Risk":
                    status_cell = Text(status, style="bold red")
                elif status == "Untranslated":
                    status_cell = Text(status, style="bold magenta")
                elif status == "Ambiguous":
                    status_cell = Text(status, style="dim")
                else:
                    status_cell = Text(status, style="yellow")
                table.add_row(
                    qid_display,
                    entry["key"],
                    entry["preview"],
                    status_cell,
                    entry["prob_display"],
                )
        else:
            for _, qid, key, preview, expected, prob_display, status in rows:
                if status == "High Risk":
                    status_cell = Text(status, style="bold red")
                elif status == "Untranslated":
                    status_cell = Text(status, style="bold magenta")
                elif status == "Ambiguous":
                    status_cell = Text(status, style="dim")
                else:
                    status_cell = Text(status, style="yellow")
                table.add_row(qid, key, preview, status_cell, prob_display)
        console.print(table)
        console.print()

    # Category breakdown
    breakdown_table = Table(
        title="Breakdown by Category", show_header=True, header_style="bold"
    )
    breakdown_table.add_column("Category", style="cyan", width=20)
    breakdown_table.add_column("Issues", style="red", width=15)

    if items_issues > 0:
        breakdown_table.add_row("Items (Questions/Choices)", f"{items_issues}  ✗")
    else:
        breakdown_table.add_row("Items (Questions/Choices)", f"{items_issues}  ✓")

    if js_issues > 0:
        breakdown_table.add_row("JavaScript COPY blocks", f"{js_issues}  ✗")
    else:
        breakdown_table.add_row("JavaScript COPY blocks", f"{js_issues}  ✓")

    if eos_issues > 0:
        breakdown_table.add_row("EOS Messages", f"{eos_issues}  ✗")
    else:
        breakdown_table.add_row("EOS Messages", f"{eos_issues}  ✓")

    console.print(breakdown_table)
    console.print()

    console.print("[dim]Note: Single-word strings can be skipped when allowed.[/dim]")


def handle_translations_check_language(args: argparse.Namespace) -> None:
    """CLI handler for translations check-language command.

    Args:
        args: Command-line arguments with survey_id attribute
    """
    from .terminal_output import error, info
    from .cli import _prompt_for_survey_id_if_needed

    survey_id = _prompt_for_survey_id_if_needed(
        getattr(args, "survey_id", None),
        allow_all_surveys=False,
    )
    root = resolve_root(required=False) or Path.cwd()
    interactive = sys.stdin.isatty() and sys.stdout.isatty()

    info(
        "[qsync:translations]",
        f"Checking translation languages for survey: {survey_id}",
    )

    try:

        def _collect_languages_from_args(raw: argparse.Namespace) -> list[str] | None:
            languages: list[str] = []
            raw_list = getattr(raw, "language", None)
            if raw_list:
                if isinstance(raw_list, str):
                    languages.append(raw_list)
                else:
                    languages.extend(raw_list)
            raw_csv = getattr(raw, "languages", None)
            if raw_csv:
                for item in str(raw_csv).split(","):
                    item = item.strip()
                    if item:
                        languages.append(item)
            return languages or None

        def _parse_edf_args(raw: argparse.Namespace) -> dict[str, str] | None:
            edf_args = getattr(raw, "edf", None) or []
            if not edf_args:
                return None
            parsed: dict[str, str] = {}
            for raw_item in edf_args:
                s = str(raw_item or "").strip()
                if not s:
                    continue
                if "=" not in s:
                    raise ValueError(f"Invalid --edf value (expected KEY=VALUE): {s}")
                k, v = s.split("=", 1)
                k = k.strip()
                v = v.strip()
                if not k:
                    raise ValueError(f"Invalid --edf value (empty key): {s}")
                parsed[k] = v
            return parsed or None

        _ensure_fasttext_setup(interactive=interactive, root=root, console=Console())
        cache = load_cached_survey(survey_id)
        payload = cache.payload
        survey_name = payload.get("result", {}).get("SurveyName", survey_id)

        explicit_languages = _collect_languages_from_args(args)
        base_language = get_base_language(payload) or "EN"
        try:
            edf_overrides = _parse_edf_args(args)
        except ValueError as exc:
            error("[qsync:translations]", str(exc))
            raise SystemExit(1)
        scenario_qids = (
            scenario_qid_order(payload, edf_overrides) if edf_overrides else None
        )
        allowed_qids = set(scenario_qids) if scenario_qids else None
        if scenario_qids:
            qid_order = {qid: idx for idx, qid in enumerate(scenario_qids)}
        else:
            qid_order = flow_order_map(payload)
        if explicit_languages:
            languages = normalize_language_list(explicit_languages)
        else:
            languages = list_enabled_languages(payload)
            if base_language and base_language in languages:
                languages = [lang for lang in languages if lang != base_language]
            if not languages:
                languages = DEFAULT_LANGUAGES[:]
        if not languages and base_language:
            languages = [base_language]

        min_confidence = float(getattr(args, "min_confidence", DEFAULT_MIN_CONFIDENCE))
        min_margin = float(getattr(args, "min_margin", DEFAULT_MIN_MARGIN))
        skip_meta = bool(getattr(args, "skip_meta", False))
        skip_js = bool(getattr(args, "skip_js", False))
        skip_eos = bool(getattr(args, "skip_eos", False))
        allow_single_words = not bool(getattr(args, "disallow_single_word", False))
        dedupe = not bool(getattr(args, "no_dedupe", False))

        info("[qsync:translations]", "Checking item translations...")
        items_results, items_stats = check_survey_item_translations(
            payload,
            languages,
            allow_single_words=allow_single_words,
            min_confidence=min_confidence,
            min_margin=min_margin,
            skip_meta=skip_meta,
            allowed_qids=allowed_qids,
            base_language=base_language,
        )

        if skip_js:
            info(
                "[qsync:translations]", "Skipping JavaScript translations (--skip-js)."
            )
            js_results = {lang: defaultdict(list) for lang in languages}
            js_stats = CheckStats()
        else:
            info("[qsync:translations]", "Checking JavaScript translations...")
            js_results, js_stats = check_js_copy_translations(
                payload,
                languages,
                allow_single_words=allow_single_words,
                min_confidence=min_confidence,
                min_margin=min_margin,
                allowed_qids=allowed_qids,
                base_language=base_language,
            )

        if skip_eos:
            info("[qsync:translations]", "Skipping EOS translations (--skip-eos).")
            eos_results = {lang: [] for lang in languages}
            eos_stats = CheckStats()
        else:
            info("[qsync:translations]", "Checking EOS translations...")
            eos_results, eos_stats = check_eos_message_translations(
                payload,
                languages,
                allow_single_words=allow_single_words,
                min_confidence=min_confidence,
                min_margin=min_margin,
                base_language=base_language,
            )

        detector_label = _detector_label()
        # Format and display results
        format_check_results(
            items_results=items_results,
            js_results=js_results,
            eos_results=eos_results,
            items_stats=items_stats,
            js_stats=js_stats,
            eos_stats=eos_stats,
            languages=languages,
            base_language=base_language,
            min_confidence=min_confidence,
            min_margin=min_margin,
            allow_single_words=allow_single_words,
            skip_meta=skip_meta,
            skip_js=skip_js,
            skip_eos=skip_eos,
            qid_order=qid_order,
            detector_label=detector_label,
            dedupe=dedupe,
            survey_name=survey_name,
            survey_id=survey_id,
            edf_overrides=edf_overrides,
            allowed_qids=allowed_qids,
        )

    except Exception as e:
        error("[qsync:translations]", f"Failed to check translation languages: {e}")
        raise SystemExit(1)
