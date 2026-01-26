from __future__ import annotations

from typing import Iterable


def normalize_language_code(code: str) -> str:
    parts = [
        part.strip() for part in str(code or "").strip().split("-") if part.strip()
    ]
    if not parts:
        return ""
    return "-".join(part.upper() for part in parts)


def normalize_language_list(languages: Iterable[str] | None) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in languages or []:
        code = normalize_language_code(raw)
        if not code or code in seen:
            continue
        seen.add(code)
        cleaned.append(code)
    return cleaned
