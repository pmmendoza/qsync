"""Blocks dimension module for block-internal structure synchronization.

This module provides the standard dimension interface (pull, detect_changes,
preview, stage, push) for Survey Definition `Blocks` ownership.

Ownership scope:
- Block-internal `BlockElements` / `Elements` order
- Question placement inside blocks
- Page-break placement inside blocks

Out of scope:
- SurveyFlow routing/branching graph (flow dimension owns that)
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from .types import DimensionChanges
from ..api_push import send_api_request
from ..config import get_client_config, resolve_root, resolve_scoped_dir
from ..diff_utils import unified_diff_lines
from ..pending_stage import (
    BlocksPendingPayload,
    PendingStagedChanges,
    clear_pending,
    load_pending,
    save_pending,
)
from ..push_safeguards import SafeguardConfig, enforce_push_safeguards
from ..terminal_colors import colorize_unified_diff_lines

logger = logging.getLogger(__name__)


def _workspace_root() -> Path:
    return resolve_root(required=False) or Path.cwd()


def _flow_root_dir() -> Path:
    root = _workspace_root()
    surveys_dir = resolve_scoped_dir("surveys", root=root)
    return surveys_dir / "flow"


def _slugify(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(value))


def _inventory_survey_name(survey_id: str) -> str | None:
    surveys_dir = resolve_scoped_dir("surveys", root=_workspace_root())
    for name in ("inventory.csv", "qualtrics_surveys.csv"):
        csv_path = surveys_dir / name
        if not csv_path.exists():
            continue
        try:
            import csv

            with csv_path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    sid = str(row.get("id") or "").strip()
                    if sid != survey_id:
                        continue
                    survey_name = str(row.get("name") or "").strip()
                    return survey_name or None
        except Exception:
            continue
    return None


def _preferred_surface_dir_name(survey_id: str, *, survey_name: str | None = None) -> str:
    raw_name = str(survey_name or "").strip() or (_inventory_survey_name(survey_id) or "")
    if not raw_name:
        return survey_id
    slug = _slugify(raw_name)
    if not slug or slug == survey_id:
        return survey_id
    return f"{slug}-{survey_id}"


def _find_existing_surface_dir(
    survey_id: str, *, preferred_name: str | None = None
) -> Path | None:
    flow_root = _flow_root_dir()
    if not flow_root.exists():
        return None

    if preferred_name:
        preferred = flow_root / preferred_name
        if preferred.exists() and preferred.is_dir():
            return preferred

    slug_candidates = [p for p in flow_root.glob(f"*-{survey_id}") if p.is_dir()]
    if slug_candidates:
        slug_candidates.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
        return slug_candidates[0]

    legacy = flow_root / survey_id
    if legacy.exists() and legacy.is_dir():
        return legacy
    return None


def _surface_dir(
    survey_id: str,
    *,
    survey_name: str | None = None,
    prefer_existing: bool = True,
) -> Path:
    flow_root = _flow_root_dir()
    preferred_name = _preferred_surface_dir_name(survey_id, survey_name=survey_name)

    if prefer_existing:
        existing = _find_existing_surface_dir(survey_id, preferred_name=preferred_name)
        if existing is not None:
            return existing

    return flow_root / preferred_name


def _yaml_path(survey_id: str) -> Path:
    return _surface_dir(survey_id) / "blocks.yaml"


def _baseline_path(survey_id: str) -> Path:
    return _surface_dir(survey_id) / "blocks_baseline.json"


def _is_trash_block(block: dict[str, Any]) -> bool:
    return str(block.get("Type") or "").strip().lower() == "trash"


def _block_elements_ref(block: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    elements = block.get("BlockElements")
    if isinstance(elements, list):
        return elements, "BlockElements"
    elements = block.get("Elements")
    if isinstance(elements, list):
        return elements, "Elements"
    block["BlockElements"] = []
    return block["BlockElements"], "BlockElements"


def _deep_copy_json(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _flow_ordered_block_ids(result_payload: dict[str, Any]) -> list[str]:
    flow = result_payload.get("SurveyFlow") or result_payload.get("Flow") or {}
    ordered: list[str] = []

    def _walk(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, list):
            for child in node:
                _walk(child)
            return
        if not isinstance(node, dict):
            return

        node_type = str(node.get("Type") or "").strip()
        if node_type in {"Block", "Standard"} and node.get("ID"):
            bid = str(node.get("ID") or "").strip()
            if bid and bid not in ordered:
                ordered.append(bid)

        for key in ("Flow", "Then", "Else", "ElseFlow"):
            _walk(node.get(key))

    if isinstance(flow, dict):
        _walk(flow.get("Flow"))
    elif isinstance(flow, list):
        _walk(flow)
    return ordered


def _ordered_blocks_map(result_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    blocks = result_payload.get("Blocks") or {}
    if not isinstance(blocks, dict):
        return {}

    ordered_ids = _flow_ordered_block_ids(result_payload)
    remaining = [bid for bid in blocks.keys() if bid not in set(ordered_ids)]
    remaining.sort()

    ordered: dict[str, dict[str, Any]] = {}
    for bid in ordered_ids + remaining:
        block = blocks.get(bid)
        if isinstance(block, dict):
            ordered[bid] = _deep_copy_json(block)
    return ordered


def _blocks_to_yaml_text(
    *, survey_id: str, survey_name: str | None, blocks_map: dict[str, dict[str, Any]]
) -> str:
    import yaml as yaml_lib

    payload: dict[str, Any] = {
        "version": 1,
        "survey_id": survey_id,
        "blocks": blocks_map,
    }
    if survey_name:
        payload["survey_name"] = survey_name

    text = yaml_lib.safe_dump(
        payload,
        sort_keys=False,
        allow_unicode=False,
        width=120,
    )
    if not text.endswith("\n"):
        text += "\n"
    return text


def _load_yaml_payload(yaml_path: Path) -> dict[str, Any]:
    import yaml as yaml_lib

    raw = yaml_lib.safe_load(yaml_path.read_text(encoding="utf-8"))
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("blocks.yaml must be a mapping object")
    return raw


def _load_blocks_surface(survey_id: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    yaml_path = _yaml_path(survey_id)
    payload = _load_yaml_payload(yaml_path)
    blocks = payload.get("blocks")
    if not isinstance(blocks, dict):
        raise ValueError("blocks.yaml is missing 'blocks' map")
    normalized: dict[str, dict[str, Any]] = {}
    for block_id, block_payload in blocks.items():
        bid = str(block_id or "").strip()
        if not bid:
            continue
        if not isinstance(block_payload, dict):
            raise ValueError(f"Block {bid} must be an object")
        normalized[bid] = _deep_copy_json(block_payload)
    return payload, normalized


def _load_baseline_blocks(survey_id: str) -> dict[str, dict[str, Any]]:
    baseline_path = _baseline_path(survey_id)
    raw = json.loads(baseline_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("blocks baseline must be a JSON object")
    blocks = raw.get("blocks") if "blocks" in raw else raw
    if not isinstance(blocks, dict):
        raise ValueError("blocks baseline is missing 'blocks' map")
    normalized: dict[str, dict[str, Any]] = {}
    for block_id, block_payload in blocks.items():
        bid = str(block_id or "").strip()
        if not bid:
            continue
        if not isinstance(block_payload, dict):
            raise ValueError(f"Baseline block {bid} must be an object")
        normalized[bid] = _deep_copy_json(block_payload)
    return normalized


def _elements_signature(block: dict[str, Any]) -> list[str]:
    elements, _ = _block_elements_ref(block)
    tokens: list[str] = []
    for elem in elements:
        if not isinstance(elem, dict):
            tokens.append("<invalid>")
            continue
        elem_type = str(elem.get("Type") or "").strip()
        if elem_type == "Question":
            tokens.append(str(elem.get("QuestionID") or "").strip() or "<missing-qid>")
        elif elem_type == "Page Break":
            tokens.append("|PAGE_BREAK|")
        elif not elem_type:
            tokens.append("<missing-type>")
        else:
            tokens.append(f"|{elem_type}|")
    return tokens


def _sequence_preview(tokens: list[str], *, limit: int = 24) -> str:
    if len(tokens) <= limit:
        return " | ".join(tokens)
    head = " | ".join(tokens[:limit])
    return f"{head} | ... (+{len(tokens) - limit} more)"


def _render_sequence_lines(tokens: list[str]) -> list[str]:
    return [f"[{idx:03d}] {token}" for idx, token in enumerate(tokens)]


def _render_unified_sequence_diff(
    before_tokens: list[str],
    after_tokens: list[str],
    *,
    fromfile: str = "baseline",
    tofile: str = "blocks.yaml",
) -> list[str]:
    return unified_diff_lines(
        "\n".join(_render_sequence_lines(before_tokens)),
        "\n".join(_render_sequence_lines(after_tokens)),
        fromfile=fromfile,
        tofile=tofile,
    )


def _changes_fingerprint(changes: list[dict[str, Any]]) -> str:
    normalized: list[dict[str, Any]] = []
    for change in sorted(changes, key=lambda c: str(c.get("block_id") or "")):
        normalized.append(
            {
                "block_id": str(change.get("block_id") or ""),
                "before": list(change.get("before") or []),
                "after": list(change.get("after") or []),
            }
        )
    return _compute_hash(normalized)


def _strip_elements_fields(block: dict[str, Any]) -> dict[str, Any]:
    out = _deep_copy_json(block)
    out.pop("BlockElements", None)
    out.pop("Elements", None)
    return out


def _validate_blocks_payload(
    *, survey_id: str, edited_blocks: dict[str, dict[str, Any]], baseline_blocks: dict[str, dict[str, Any]]
) -> list[str]:
    errors: list[str] = []

    edited_ids = set(edited_blocks.keys())
    baseline_ids = set(baseline_blocks.keys())
    if edited_ids != baseline_ids:
        missing = sorted(baseline_ids - edited_ids)
        extra = sorted(edited_ids - baseline_ids)
        if missing:
            errors.append(f"Missing block IDs in blocks.yaml: {', '.join(missing[:8])}")
        if extra:
            errors.append(f"Unknown block IDs in blocks.yaml: {', '.join(extra[:8])}")

    for block_id in sorted(edited_ids & baseline_ids):
        edited_block = edited_blocks[block_id]
        baseline_block = baseline_blocks[block_id]

        if _strip_elements_fields(edited_block) != _strip_elements_fields(baseline_block):
            errors.append(
                f"{block_id}: only BlockElements edits are supported (non-element block fields changed)."
            )

        edited_elements, _ = _block_elements_ref(edited_block)
        seen_qids: set[str] = set()
        for idx, elem in enumerate(edited_elements):
            if not isinstance(elem, dict):
                errors.append(f"{block_id}[{idx}]: element must be an object")
                continue
            elem_type = str(elem.get("Type") or "").strip()
            if not elem_type:
                errors.append(f"{block_id}[{idx}]: element Type is required")
                continue
            if elem_type == "Question":
                qid = str(elem.get("QuestionID") or "").strip()
                if not qid:
                    errors.append(f"{block_id}[{idx}]: Question element missing QuestionID")
                elif qid in seen_qids and not _is_trash_block(edited_block):
                    errors.append(f"{block_id}: duplicate QuestionID {qid}")
                else:
                    seen_qids.add(qid)
            elif elem_type == "Page Break":
                continue
            else:
                # Keep unknown element types pass-through but make this explicit in stage output.
                pass

    return errors


def _diff_block_sequences(
    baseline_blocks: dict[str, dict[str, Any]],
    edited_blocks: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for block_id in sorted(set(baseline_blocks.keys()) & set(edited_blocks.keys())):
        before = _elements_signature(_deep_copy_json(baseline_blocks[block_id]))
        after = _elements_signature(_deep_copy_json(edited_blocks[block_id]))
        if before == after:
            continue
        block = edited_blocks[block_id]
        desc = str(block.get("Description") or "").strip()
        diff_lines = _render_unified_sequence_diff(
            before,
            after,
            fromfile="baseline",
            tofile="blocks.yaml",
        )
        changes.append(
            {
                "block_id": block_id,
                "description": desc,
                "before": before,
                "after": after,
                "diff_lines": diff_lines,
                "changed": len(before) != len(after) or before != after,
            }
        )
    return changes


def _compute_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _materialize_surfaces(
    *,
    survey_id: str,
    survey_name: str | None,
    blocks_map: dict[str, dict[str, Any]],
    force: bool,
) -> Path:
    surface_dir = _surface_dir(survey_id, survey_name=survey_name, prefer_existing=False)
    existing = _find_existing_surface_dir(survey_id, preferred_name=surface_dir.name)
    if existing is not None:
        surface_dir = existing
        if (
            existing.name == survey_id
            and existing != _surface_dir(survey_id, survey_name=survey_name, prefer_existing=False)
            and not _surface_dir(survey_id, survey_name=survey_name, prefer_existing=False).exists()
        ):
            existing.rename(_surface_dir(survey_id, survey_name=survey_name, prefer_existing=False))
            surface_dir = _surface_dir(survey_id, survey_name=survey_name, prefer_existing=False)

    yaml_path = surface_dir / "blocks.yaml"
    baseline_path = surface_dir / "blocks_baseline.json"

    if yaml_path.exists() and baseline_path.exists() and not force:
        edited_payload, edited_blocks = _load_blocks_surface(survey_id)
        baseline_blocks = _load_baseline_blocks(survey_id)
        if _diff_block_sequences(baseline_blocks, edited_blocks):
            raise FileExistsError(
                "blocks.yaml has local changes. Use --force to overwrite or stage/push first."
            )

    surface_dir.mkdir(parents=True, exist_ok=True)
    yaml_text = _blocks_to_yaml_text(
        survey_id=survey_id,
        survey_name=survey_name,
        blocks_map=blocks_map,
    )
    yaml_path.write_text(yaml_text, encoding="utf-8")
    baseline_path.write_text(
        json.dumps({"survey_id": survey_id, "blocks": blocks_map}, indent=2),
        encoding="utf-8",
    )
    return yaml_path


def ensure_local_surface(survey_id: str) -> Path:
    yaml_path = _yaml_path(survey_id)
    baseline_path = _baseline_path(survey_id)
    if yaml_path.exists() and baseline_path.exists():
        return yaml_path
    return pull(survey_id, force=False)


def pull(survey_id: str, *, force: bool = False) -> Path:
    from ..qualtrics_client import refresh_survey_cache

    cache, _ = refresh_survey_cache(survey_id)
    result = cache.payload.get("result", {}) or {}
    survey_name = str(result.get("SurveyName") or "").strip() or None
    blocks_map = _ordered_blocks_map(result)
    yaml_path = _materialize_surfaces(
        survey_id=survey_id,
        survey_name=survey_name,
        blocks_map=blocks_map,
        force=force,
    )
    logger.info("[sync:blocks] Pulled blocks to %s", yaml_path)
    return yaml_path


def detect_changes(survey_id: str) -> DimensionChanges:
    pending = load_pending(survey_id, "blocks")
    if pending and isinstance(pending.payload, BlocksPendingPayload):
        count = len(pending.payload.block_ids)
        return DimensionChanges(
            dimension="blocks",
            has_changes=bool(count),
            change_summary=f"✓ Staged: {count} block(s)",
            affected_qids=set(),
            status_kind="staged",
            edit_count=count,
        )

    yaml_path = _yaml_path(survey_id)
    baseline_path = _baseline_path(survey_id)
    if not yaml_path.exists() or not baseline_path.exists():
        return DimensionChanges(
            dimension="blocks",
            has_changes=False,
            change_summary="Not initialized",
            affected_qids=set(),
            warning_detail=f"Run: qsync blocks pull --survey-id {survey_id}",
            safe_to_autofix=True,
            status_kind="none",
            edit_count=0,
        )

    try:
        _, edited_blocks = _load_blocks_surface(survey_id)
        baseline_blocks = _load_baseline_blocks(survey_id)
        validation_errors = _validate_blocks_payload(
            survey_id=survey_id,
            edited_blocks=edited_blocks,
            baseline_blocks=baseline_blocks,
        )
        if validation_errors:
            return DimensionChanges(
                dimension="blocks",
                has_changes=False,
                change_summary="✗ Error",
                affected_qids=set(),
                error_detail=validation_errors[0],
                status_kind="error",
                edit_count=0,
            )

        changes = _diff_block_sequences(baseline_blocks, edited_blocks)
        if changes:
            return DimensionChanges(
                dimension="blocks",
                has_changes=True,
                change_summary=f"⚡ Unstaged: {len(changes)} block(s)",
                affected_qids=set(),
                status_kind="unstaged",
                edit_count=len(changes),
            )
        return DimensionChanges(
            dimension="blocks",
            has_changes=False,
            change_summary="No changes",
            affected_qids=set(),
            status_kind="none",
            edit_count=0,
        )
    except Exception as exc:
        return DimensionChanges(
            dimension="blocks",
            has_changes=False,
            change_summary="✗ Error",
            affected_qids=set(),
            error_detail=f"Block detection failed: {str(exc).split(chr(10))[0]}",
            status_kind="error",
            edit_count=0,
        )


def preview(
    survey_id: str,
    *,
    verbose: bool = False,
) -> list[dict[str, Any]]:
    yaml_path = _yaml_path(survey_id)
    baseline_path = _baseline_path(survey_id)

    if not yaml_path.exists() or not baseline_path.exists():
        print(f"[sync:blocks] Missing local surface. Run: qsync blocks pull --survey-id {survey_id}")
        return []

    try:
        _, edited_blocks = _load_blocks_surface(survey_id)
        baseline_blocks = _load_baseline_blocks(survey_id)
        validation_errors = _validate_blocks_payload(
            survey_id=survey_id,
            edited_blocks=edited_blocks,
            baseline_blocks=baseline_blocks,
        )
        if validation_errors:
            print("[sync:blocks] Validation error(s):")
            for err in validation_errors:
                print(f"  - {err}")
            return []

        changes = _diff_block_sequences(baseline_blocks, edited_blocks)
        if not changes:
            print("[sync:blocks] No changes detected")
            return []

        print(f"[sync:blocks] {len(changes)} block(s) changed:")
        for change in changes:
            desc = f" ({change['description']})" if change.get("description") else ""
            print(f"  ~ {change['block_id']}{desc}")
            print(f"    - before: {_sequence_preview(change['before'])}")
            print(f"    + after : {_sequence_preview(change['after'])}")
            if verbose:
                diff_lines = list(change.get("diff_lines") or [])
                for line in colorize_unified_diff_lines(diff_lines):
                    print(f"    {line}")
        return changes

    except Exception as exc:
        print(f"[sync:blocks] Error previewing changes: {exc}")
        return []


def stage(
    survey_id: str,
    *,
    allow_drift: bool = False,
    interactive: bool = True,
) -> bool:
    yaml_path = _yaml_path(survey_id)
    baseline_path = _baseline_path(survey_id)
    if not yaml_path.exists() or not baseline_path.exists():
        print(f"[sync:blocks] Missing local surface. Run: qsync blocks pull --survey-id {survey_id}")
        return False

    try:
        _, edited_blocks = _load_blocks_surface(survey_id)
        baseline_blocks = _load_baseline_blocks(survey_id)
    except Exception as exc:
        print(f"[sync:blocks] Failed to load local surface: {exc}")
        return False

    validation_errors = _validate_blocks_payload(
        survey_id=survey_id,
        edited_blocks=edited_blocks,
        baseline_blocks=baseline_blocks,
    )
    if validation_errors:
        print("[sync:blocks] Validation error(s):")
        for err in validation_errors:
            print(f"  - {err}")
        return False

    changes = _diff_block_sequences(baseline_blocks, edited_blocks)
    if not changes:
        clear_pending(survey_id, "blocks")
        print("[sync:blocks] No changes to stage")
        return True

    baseline_hash = _compute_hash(baseline_blocks)

    if not allow_drift:
        try:
            from ..qualtrics_client import fetch_survey_definition_live

            live = fetch_survey_definition_live(survey_id)
            live_blocks = live.get("result", {}).get("Blocks", {})
            if isinstance(live_blocks, dict):
                live_hash = _compute_hash(live_blocks)
                if live_hash != baseline_hash:
                    print(
                        "[sync:blocks] Drift detected: baseline differs from live API. "
                        "Run `qsync blocks pull --survey-id ...` or retry with --allow-drift."
                    )
                    return False
        except Exception as exc:
            if not allow_drift:
                print(f"[sync:blocks] Drift check failed: {exc}")
                return False

    block_ids = [c["block_id"] for c in changes]
    payload = BlocksPendingPayload(
        blocks_yaml_path=str(yaml_path),
        baseline_hash=baseline_hash,
        block_ids=block_ids,
        changes=changes,
    )
    record = PendingStagedChanges(
        survey_id=survey_id,
        dimension="blocks",
        payload=payload,
    )
    save_pending(record)
    print(f"[sync:blocks] Staged {len(changes)} block(s)")
    return True


def _prompt_confirmation(prompt: str) -> bool:
    try:
        from ..interactive_menu import confirm

        return confirm(prompt, default=False)
    except Exception:
        response = input(f"{prompt} [y/N]: ").strip().lower()
        return response in {"y", "yes"}


def push(
    survey_id: str,
    *,
    interactive: bool = True,
    force_live: bool = False,
    force_preview: bool = False,
    auto_yes: bool = False,
    allow_drift: bool = False,
    skip_publish: bool = False,
) -> bool:
    from ..qualtrics_client import (
        ensure_backup,
        fetch_survey_definition_live,
        publish_survey_definition,
        refresh_survey_cache,
    )

    pending = load_pending(survey_id, "blocks")
    if not pending or not isinstance(pending.payload, BlocksPendingPayload):
        print("[sync:blocks] No staged block changes found")
        return True

    yaml_path = Path(pending.payload.blocks_yaml_path)
    if not yaml_path.exists():
        print(f"[sync:blocks] YAML file not found: {yaml_path}")
        clear_pending(survey_id, "blocks")
        return False

    try:
        _, edited_blocks = _load_blocks_surface(survey_id)
        baseline_blocks = _load_baseline_blocks(survey_id)
    except Exception as exc:
        print(f"[sync:blocks] Failed to load local surface: {exc}")
        return False

    validation_errors = _validate_blocks_payload(
        survey_id=survey_id,
        edited_blocks=edited_blocks,
        baseline_blocks=baseline_blocks,
    )
    if validation_errors:
        print("[sync:blocks] Validation error(s):")
        for err in validation_errors:
            print(f"  - {err}")
        return False

    changes = _diff_block_sequences(baseline_blocks, edited_blocks)
    if not changes:
        clear_pending(survey_id, "blocks")
        print("[sync:blocks] No staged changes remain")
        return True

    current_change_ids = {c["block_id"] for c in changes}
    staged_ids = set(pending.payload.block_ids)
    if current_change_ids != staged_ids:
        print(
            "[sync:blocks] WARNING: blocks.yaml changed since staging "
            f"(staged={len(staged_ids)} current={len(current_change_ids)})."
        )
        if interactive and not auto_yes and not _prompt_confirmation("Continue with current local changes?"):
            print("[sync:blocks] Aborted - re-run `qsync blocks stage`")
            return False

    staged_fingerprint = _changes_fingerprint(list(pending.payload.changes or []))
    current_fingerprint = _changes_fingerprint(changes)
    if staged_fingerprint != current_fingerprint:
        print(
            "[sync:blocks] WARNING: block content changed since staging "
            "(block IDs may be unchanged)."
        )
        print(f"[sync:blocks] Next: run `qsync blocks stage --survey-id {survey_id}` to refresh staged content.")
        if interactive and not auto_yes and not _prompt_confirmation("Continue with current local changes?"):
            print("[sync:blocks] Aborted - re-run `qsync blocks stage`")
            return False

    baseline_hash = _compute_hash(baseline_blocks)
    if baseline_hash != pending.payload.baseline_hash:
        print("[sync:blocks] WARNING: baseline changed since staging.")
        if interactive and not auto_yes and not _prompt_confirmation("Continue anyway?"):
            print("[sync:blocks] Aborted")
            return False

    try:
        enforce_push_safeguards(
            SafeguardConfig(
                survey_id=survey_id,
                dimension="blocks",
                force_live=force_live,
                force_preview=force_preview,
                auto_yes=auto_yes or not interactive,
            )
        )
    except Exception as exc:
        print(f"[sync:blocks] Push safeguard failed: {exc}")
        return False

    if not allow_drift:
        try:
            live = fetch_survey_definition_live(survey_id)
            live_blocks = live.get("result", {}).get("Blocks", {})
            if not isinstance(live_blocks, dict):
                raise RuntimeError("Live survey definition missing Blocks map")
            live_hash = _compute_hash(live_blocks)
            if live_hash != pending.payload.baseline_hash:
                print(
                    "[sync:blocks] Drift detected between staged baseline and live API. "
                    "Run `qsync blocks pull` + `qsync blocks stage`, or retry with --allow-drift."
                )
                return False
        except Exception as exc:
            print(f"[sync:blocks] Drift check failed: {exc}")
            return False

    try:
        ensure_backup(survey_id)
    except Exception as exc:
        logger.warning("[sync:blocks] Backup failed: %s", exc)

    try:
        live = fetch_survey_definition_live(survey_id)
        result = live.get("result", {})
        live_blocks = result.get("Blocks", {})
        if not isinstance(live_blocks, dict):
            raise RuntimeError("Live survey definition has no Blocks map")

        base_url, headers = get_client_config()
        changed_ids = [c["block_id"] for c in changes]
        for block_id in changed_ids:
            live_block = live_blocks.get(block_id)
            edited_block = edited_blocks.get(block_id)
            if not isinstance(live_block, dict) or not isinstance(edited_block, dict):
                raise RuntimeError(f"Block {block_id} missing in live or local surface")

            edited_elements, edited_key = _block_elements_ref(edited_block)
            payload = _deep_copy_json(live_block)
            payload[edited_key] = _deep_copy_json(edited_elements)

            send_api_request(
                action="qsync.blocks.push.block",
                method="PUT",
                base_url=base_url,
                headers=headers,
                path=f"survey-definitions/{survey_id}/blocks/{block_id}",
                survey_id=survey_id,
                log_meta={"operation": "blocks_push", "block_id": block_id},
                json=payload,
                timeout=60,
            )

        if not skip_publish:
            publish_survey_definition(
                survey_id,
                description="qsync blocks push",
                published=True,
                context={"origin": "qsync.blocks.push", "changed_blocks": changed_ids},
            )

        cache, _ = refresh_survey_cache(survey_id)
        result = cache.payload.get("result", {})
        survey_name = str(result.get("SurveyName") or "").strip() or None
        blocks_map = _ordered_blocks_map(result)
        _materialize_surfaces(
            survey_id=survey_id,
            survey_name=survey_name,
            blocks_map=blocks_map,
            force=True,
        )

        clear_pending(survey_id, "blocks")
        print(f"[sync:blocks] Pushed {len(changed_ids)} block(s)")
        return True

    except Exception as exc:
        print(f"[sync:blocks] Push failed: {exc}")
        return False


def detect_unstaged_changes(survey_id: str) -> DimensionChanges:
    return detect_changes(survey_id)


def _find_question_blocks(
    blocks: dict[str, dict[str, Any]],
    qid: str,
    *,
    include_trash: bool = True,
) -> list[tuple[str, int]]:
    matches: list[tuple[str, int]] = []
    target = str(qid or "").strip()
    if not target:
        return matches

    for block_id, block in blocks.items():
        if not isinstance(block, dict):
            continue
        if _is_trash_block(block) and not include_trash:
            continue
        elements, _ = _block_elements_ref(block)
        for idx, elem in enumerate(elements):
            if not isinstance(elem, dict):
                continue
            if str(elem.get("Type") or "").strip() != "Question":
                continue
            if str(elem.get("QuestionID") or "").strip() == target:
                matches.append((block_id, idx))
    return matches


def _first_eligible_block_id(blocks: dict[str, dict[str, Any]]) -> str:
    for block_id, block in blocks.items():
        if not isinstance(block, dict):
            continue
        if _is_trash_block(block):
            continue
        return block_id
    raise ValueError("No eligible (non-trash) block found")


def _resolve_target_block_id(
    blocks: dict[str, dict[str, Any]],
    *,
    target_block_id: str | None,
    after_qid: str | None,
    before_qid: str | None,
    fallback_qid: str | None = None,
) -> str:
    if target_block_id:
        if target_block_id not in blocks:
            raise ValueError(f"Block {target_block_id} was not found")
        return target_block_id

    if after_qid:
        matches = _find_question_blocks(blocks, after_qid, include_trash=True)
        if not matches:
            raise ValueError(f"Could not find --after-qid {after_qid} in blocks")
        return matches[0][0]

    if before_qid:
        matches = _find_question_blocks(blocks, before_qid, include_trash=True)
        if not matches:
            raise ValueError(f"Could not find --before-qid {before_qid} in blocks")
        return matches[0][0]

    if fallback_qid:
        matches = _find_question_blocks(blocks, fallback_qid, include_trash=True)
        if matches:
            return matches[0][0]

    return _first_eligible_block_id(blocks)


def _resolve_insert_index(
    block: dict[str, Any],
    *,
    after_qid: str | None,
    before_qid: str | None,
    position: str,
    insert_index: int | None,
) -> int:
    elements, _ = _block_elements_ref(block)
    if insert_index is not None:
        return max(0, min(int(insert_index), len(elements)))

    if after_qid:
        for idx, elem in enumerate(elements):
            if (
                isinstance(elem, dict)
                and str(elem.get("Type") or "").strip() == "Question"
                and str(elem.get("QuestionID") or "").strip() == after_qid
            ):
                return idx + 1
        raise ValueError(f"QID {after_qid} was not found in target block")

    if before_qid:
        for idx, elem in enumerate(elements):
            if (
                isinstance(elem, dict)
                and str(elem.get("Type") or "").strip() == "Question"
                and str(elem.get("QuestionID") or "").strip() == before_qid
            ):
                return idx
        raise ValueError(f"QID {before_qid} was not found in target block")

    if position == "prepend":
        return 0
    return len(elements)


def _write_blocks_yaml(
    *,
    survey_id: str,
    payload: dict[str, Any],
    blocks_map: dict[str, dict[str, Any]],
) -> None:
    yaml_path = _yaml_path(survey_id)
    survey_name = str(payload.get("survey_name") or "").strip() or None
    yaml_text = _blocks_to_yaml_text(
        survey_id=survey_id,
        survey_name=survey_name,
        blocks_map=blocks_map,
    )
    yaml_path.write_text(yaml_text, encoding="utf-8")


def _normalize_qids(qids: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for qid in qids:
        token = str(qid or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        normalized.append(token)
    return normalized


def _remove_qids_from_blocks_map(
    blocks_map: dict[str, dict[str, Any]],
    *,
    qids: list[str],
) -> set[str]:
    qid_set = set(_normalize_qids(qids))
    if not qid_set:
        return set()

    touched: set[str] = set()
    for block_id, block in blocks_map.items():
        elements, key = _block_elements_ref(block)
        filtered: list[dict[str, Any]] = []
        changed = False
        for elem in elements:
            if (
                isinstance(elem, dict)
                and str(elem.get("Type") or "").strip() == "Question"
                and str(elem.get("QuestionID") or "").strip() in qid_set
            ):
                changed = True
                continue
            filtered.append(elem)
        if changed:
            block[key] = filtered
            touched.add(block_id)
    return touched


def _first_trash_block_id(blocks_map: dict[str, dict[str, Any]]) -> str | None:
    trash_ids = [
        str(block_id).strip()
        for block_id, block in blocks_map.items()
        if isinstance(block, dict)
        and _is_trash_block(block)
        and str(block_id).strip()
    ]
    if not trash_ids:
        return None
    return sorted(trash_ids)[0]


def apply_move_qids(
    blocks_map: dict[str, dict[str, Any]],
    *,
    qids: list[str],
    target_block_id: str | None = None,
    after_qid: str | None = None,
    before_qid: str | None = None,
    position: str = "append",
    insert_index: int | None = None,
    allow_trash_target: bool = False,
) -> dict[str, Any]:
    normalized_qids = _normalize_qids(qids)
    if not normalized_qids:
        raise ValueError("At least one QID is required")

    block_id = _resolve_target_block_id(
        blocks_map,
        target_block_id=target_block_id,
        after_qid=after_qid,
        before_qid=before_qid,
        fallback_qid=normalized_qids[0],
    )

    target_block = blocks_map.get(block_id)
    if not isinstance(target_block, dict):
        raise ValueError(f"Target block {block_id} not found")
    if _is_trash_block(target_block) and not allow_trash_target:
        raise ValueError(f"Block {block_id} is Trash and cannot be a move target")

    raw_insert_index = _resolve_insert_index(
        target_block,
        after_qid=after_qid,
        before_qid=before_qid,
        position=position,
        insert_index=insert_index,
    )

    qid_set = set(normalized_qids)
    target_elements_before, _ = _block_elements_ref(target_block)
    removed_before = 0
    bounded_raw = max(0, min(raw_insert_index, len(target_elements_before)))
    for idx, elem in enumerate(target_elements_before):
        if idx >= bounded_raw:
            break
        if (
            isinstance(elem, dict)
            and str(elem.get("Type") or "").strip() == "Question"
            and str(elem.get("QuestionID") or "").strip() in qid_set
        ):
            removed_before += 1
    adjusted_insert_index = max(0, bounded_raw - removed_before)

    touched = _remove_qids_from_blocks_map(blocks_map, qids=normalized_qids)

    target_block = blocks_map.get(block_id)
    if not isinstance(target_block, dict):
        raise ValueError(f"Target block {block_id} not found")
    elements, key = _block_elements_ref(target_block)
    insert_at = max(0, min(adjusted_insert_index, len(elements)))
    insertion = [{"Type": "Question", "QuestionID": qid} for qid in normalized_qids]
    elements[insert_at:insert_at] = insertion
    target_block[key] = elements
    touched.add(block_id)

    return {
        "qids": normalized_qids,
        "block_id": block_id,
        "insert_index": insert_at,
        "touched_blocks": sorted(touched),
    }


def apply_add_page_break(
    blocks_map: dict[str, dict[str, Any]],
    *,
    target_block_id: str | None = None,
    after_qid: str | None = None,
    before_qid: str | None = None,
    position: str = "append",
    insert_index: int | None = None,
    allow_trash_target: bool = False,
) -> dict[str, Any]:
    block_id = _resolve_target_block_id(
        blocks_map,
        target_block_id=target_block_id,
        after_qid=after_qid,
        before_qid=before_qid,
    )

    target_block = blocks_map.get(block_id)
    if not isinstance(target_block, dict):
        raise ValueError(f"Target block {block_id} not found")
    if _is_trash_block(target_block) and not allow_trash_target:
        raise ValueError(f"Block {block_id} is Trash and cannot be edited")

    idx = _resolve_insert_index(
        target_block,
        after_qid=after_qid,
        before_qid=before_qid,
        position=position,
        insert_index=insert_index,
    )

    elements, key = _block_elements_ref(target_block)
    elements[idx:idx] = [{"Type": "Page Break"}]
    target_block[key] = elements
    return {"block_id": block_id, "insert_index": idx}


def apply_remove_page_break(
    blocks_map: dict[str, dict[str, Any]],
    *,
    target_block_id: str,
    element_indices: list[int],
    allow_trash_target: bool = False,
) -> dict[str, Any]:
    block = blocks_map.get(target_block_id)
    if not isinstance(block, dict):
        raise ValueError(f"Block {target_block_id} was not found")
    if _is_trash_block(block) and not allow_trash_target:
        raise ValueError(f"Block {target_block_id} is Trash and cannot be edited")

    elements, key = _block_elements_ref(block)
    if not element_indices:
        raise ValueError("At least one element index is required")

    unique_desc = sorted(set(int(i) for i in element_indices), reverse=True)
    removed = 0
    for idx in unique_desc:
        if idx < 0 or idx >= len(elements):
            raise ValueError(
                f"Element index {idx} out of range for block {target_block_id}"
            )
        elem = elements[idx]
        elem_type = (
            str(elem.get("Type") or "").strip() if isinstance(elem, dict) else ""
        )
        if elem_type != "Page Break":
            raise ValueError(
                f"Element index {idx} is Type='{elem_type or '(missing)'}', expected 'Page Break'"
            )
        del elements[idx]
        removed += 1

    block[key] = elements
    return {"block_id": target_block_id, "removed": removed}


def apply_remove_qids(
    blocks_map: dict[str, dict[str, Any]],
    *,
    qids: list[str],
    move_to_trash: bool = True,
) -> dict[str, Any]:
    normalized_qids = _normalize_qids(qids)
    if not normalized_qids:
        raise ValueError("At least one QID is required")

    touched = _remove_qids_from_blocks_map(blocks_map, qids=normalized_qids)
    trash_block_id = _first_trash_block_id(blocks_map) if move_to_trash else None
    moved_to_trash = False
    if move_to_trash and trash_block_id:
        trash_block = blocks_map[trash_block_id]
        elements, key = _block_elements_ref(trash_block)
        elements.extend(
            {"Type": "Question", "QuestionID": qid} for qid in normalized_qids
        )
        trash_block[key] = elements
        touched.add(trash_block_id)
        moved_to_trash = True

    return {
        "qids": normalized_qids,
        "touched_blocks": sorted(touched),
        "trash_block_id": trash_block_id,
        "moved_to_trash": moved_to_trash,
    }


def move_qid(
    survey_id: str,
    *,
    qids: list[str],
    target_block_id: str | None = None,
    after_qid: str | None = None,
    before_qid: str | None = None,
    position: str = "append",
    insert_index: int | None = None,
) -> dict[str, Any]:
    ensure_local_surface(survey_id)
    payload, blocks = _load_blocks_surface(survey_id)
    result = apply_move_qids(
        blocks,
        qids=qids,
        target_block_id=target_block_id,
        after_qid=after_qid,
        before_qid=before_qid,
        position=position,
        insert_index=insert_index,
        allow_trash_target=False,
    )
    _write_blocks_yaml(survey_id=survey_id, payload=payload, blocks_map=blocks)
    return result


def add_page_break(
    survey_id: str,
    *,
    target_block_id: str | None = None,
    after_qid: str | None = None,
    before_qid: str | None = None,
    position: str = "append",
    insert_index: int | None = None,
) -> dict[str, Any]:
    ensure_local_surface(survey_id)
    payload, blocks = _load_blocks_surface(survey_id)
    result = apply_add_page_break(
        blocks,
        target_block_id=target_block_id,
        after_qid=after_qid,
        before_qid=before_qid,
        position=position,
        insert_index=insert_index,
        allow_trash_target=False,
    )
    _write_blocks_yaml(survey_id=survey_id, payload=payload, blocks_map=blocks)
    return result


def remove_page_break(
    survey_id: str,
    *,
    target_block_id: str,
    element_indices: list[int],
) -> dict[str, Any]:
    ensure_local_surface(survey_id)
    payload, blocks = _load_blocks_surface(survey_id)
    result = apply_remove_page_break(
        blocks,
        target_block_id=target_block_id,
        element_indices=element_indices,
        allow_trash_target=False,
    )
    _write_blocks_yaml(survey_id=survey_id, payload=payload, blocks_map=blocks)
    return result


def remove_qid(
    survey_id: str,
    *,
    qids: list[str],
    move_to_trash: bool = True,
) -> dict[str, Any]:
    ensure_local_surface(survey_id)
    payload, blocks = _load_blocks_surface(survey_id)
    result = apply_remove_qids(
        blocks,
        qids=qids,
        move_to_trash=move_to_trash,
    )
    _write_blocks_yaml(survey_id=survey_id, payload=payload, blocks_map=blocks)
    return result
