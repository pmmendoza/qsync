"""Log rotation helpers for qsync JSONL logs."""

from __future__ import annotations

import gzip
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ROTATION_SIZE_BYTES = 10 * 1024 * 1024
DEFAULT_RETENTION_MONTHS = 12

_ARCHIVE_RE = re.compile(
    r"^(?P<name>.+)\.(?P<year>\d{4})-(?P<month>\d{2})(?:\.(?P<seq>\d+))?\.gz$"
)


@dataclass(frozen=True)
class RotationResult:
    rotated: bool
    reason: str | None
    archive_path: Path | None
    deleted_archives: tuple[Path, ...]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
        return value if value > 0 else default
    except Exception:
        return default


def resolve_rotation_size_bytes(override: int | None = None) -> int:
    if override is not None:
        return max(1, int(override))
    return _env_int("QSYNC_LOG_ROTATION_SIZE", DEFAULT_ROTATION_SIZE_BYTES)


def resolve_retention_months(override: int | None = None) -> int:
    if override is not None:
        return max(1, int(override))
    return _env_int("QSYNC_LOG_RETENTION_MONTHS", DEFAULT_RETENTION_MONTHS)


def _month_key(dt: datetime) -> int:
    return dt.year * 12 + dt.month


def _extract_archive_period(path: Path, *, log_name: str) -> tuple[int, int, int]:
    match = _ARCHIVE_RE.match(path.name)
    if not match or match.group("name") != log_name:
        return (0, 0, 0)
    year = int(match.group("year"))
    month = int(match.group("month"))
    seq = int(match.group("seq") or "0")
    return (year, month, seq)


def list_archives(log_file: Path) -> list[Path]:
    archive_dir = log_file.parent / "archive"
    if not archive_dir.exists():
        return []
    candidates = [p for p in archive_dir.glob(f"{log_file.name}.*.gz") if p.is_file()]
    candidates.sort(
        key=lambda p: _extract_archive_period(p, log_name=log_file.name),
        reverse=True,
    )
    return candidates


def _next_archive_path(log_file: Path, *, month: datetime) -> Path:
    archive_dir = log_file.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    base = archive_dir / f"{log_file.name}.{month.year:04d}-{month.month:02d}"
    candidate = base
    seq = 1
    while candidate.exists() or Path(f"{candidate}.gz").exists():
        candidate = archive_dir / (
            f"{log_file.name}.{month.year:04d}-{month.month:02d}.{seq}"
        )
        seq += 1
    return candidate


def _compress_file(path: Path) -> Path:
    gz_path = Path(f"{path}.gz")
    with path.open("rb") as src, gzip.open(gz_path, "wb") as dst:
        shutil.copyfileobj(src, dst)
    path.unlink()
    return gz_path


def should_rotate(
    log_file: Path,
    *,
    now: datetime | None = None,
    max_bytes: int | None = None,
) -> tuple[bool, str | None]:
    if not log_file.exists():
        return (False, None)
    try:
        stat = log_file.stat()
    except OSError:
        return (False, None)

    if stat.st_size <= 0:
        return (False, None)

    limit = resolve_rotation_size_bytes(max_bytes)
    if stat.st_size >= limit:
        return (True, "size")

    current = now or datetime.now(timezone.utc)
    modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    if (modified.year, modified.month) != (current.year, current.month):
        return (True, "month")
    return (False, None)


def cleanup_old_archives(
    log_file: Path,
    *,
    retention_months: int | None = None,
    now: datetime | None = None,
) -> list[Path]:
    keep = resolve_retention_months(retention_months)
    cutoff_month = _month_key((now or datetime.now(timezone.utc))) - keep + 1
    deleted: list[Path] = []
    archives = list_archives(log_file)
    newest_archive = archives[0] if archives else None
    for path in archives:
        if newest_archive and path == newest_archive:
            continue
        year, month, _ = _extract_archive_period(path, log_name=log_file.name)
        if year == 0 or month == 0:
            continue
        if (year * 12 + month) < cutoff_month:
            try:
                path.unlink()
                deleted.append(path)
            except OSError:
                continue
    return deleted


def rotate_log_file(
    log_file: Path,
    *,
    force: bool = False,
    now: datetime | None = None,
    max_bytes: int | None = None,
    retention_months: int | None = None,
) -> RotationResult:
    current = now or datetime.now(timezone.utc)
    should, reason = should_rotate(log_file, now=current, max_bytes=max_bytes)
    if not force and not should:
        return RotationResult(
            rotated=False,
            reason=reason,
            archive_path=None,
            deleted_archives=tuple(),
        )
    if not log_file.exists() or log_file.stat().st_size == 0:
        return RotationResult(
            rotated=False,
            reason=None,
            archive_path=None,
            deleted_archives=tuple(),
        )

    modified = datetime.fromtimestamp(log_file.stat().st_mtime, tz=timezone.utc)
    archive_path = _next_archive_path(log_file, month=modified)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(log_file), str(archive_path))
    compressed_archive = _compress_file(archive_path)
    deleted_archives = cleanup_old_archives(
        log_file,
        retention_months=retention_months,
        now=current,
    )
    return RotationResult(
        rotated=True,
        reason=reason or ("forced" if force else "unknown"),
        archive_path=compressed_archive,
        deleted_archives=tuple(deleted_archives),
    )
