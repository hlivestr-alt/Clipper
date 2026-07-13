from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from clipper_app.path_safety import UnsafePathError, resolve_within_root

PACKAGER_SCHEMA_VERSION = 1
EXPORT_STATUS_SCHEMA_VERSION = 1
HASH_BYTES = 64 * 1024
TIER_DIRS = {"export_ready", "review_needed", "rejected"}
ROTATION_STRATEGY = "vod_clip_variant_rotation"
SCORE_ROUND_ROBIN_STRATEGY = "score_round_robin_all_variants"
ROTATION_LAYOUT_VERSION = 1
DEFAULT_VARIANT_COUNT = 6
_EXPORT_STATUS_LOCK = threading.RLock()


@dataclass
class ExportCandidate:
    source_dir: Path
    source_vod: str
    normalized_source_vod: str
    clip_id: str
    base_clip_id: str
    variant_id: str
    base_clip_key: str
    source_clip_key: str
    source_path: Path
    source_output_file: str
    total_score: float
    content_md5_64k: str
    product: str = ""
    clip_type: str = ""
    excluded_variants: list[str] | None = None
    allocation_strategy: str = ""
    selection_reason: str = ""
    requested_variant: str = ""
    vod_index: int | None = None
    vod_group: int | None = None
    clip_number: int | None = None
    lane_key: str = ""


def package_export_batches(
    output_root: str | Path,
    cfg=None,
    batch_size: int | None = None,
    dry_run: bool = False,
    *,
    trigger: str = "direct",
) -> dict[str, Any]:
    """Package export-ready clips and persist a compact operational snapshot."""
    if cfg is None:
        import config as cfg  # type: ignore

    root = Path(output_root).resolve(strict=False)
    batch_dir_name = str(getattr(cfg, "EXPORT_BATCH_DIR_NAME", "export_batches") or "export_batches")
    batch_root = resolve_within_root(root, batch_dir_name)
    status_path = resolve_within_root(batch_root, "_status.json")
    operation_id = uuid4().hex
    started_at_ns = time.time_ns()
    started_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="microseconds")
    previous = _load_export_status(status_path)
    base_status = {
        "schema_version": EXPORT_STATUS_SCHEMA_VERSION,
        "operation_id": operation_id,
        "trigger": str(trigger or "direct"),
        "status": "running",
        "started_at": started_at,
        "started_at_ns": started_at_ns,
        "updated_at": started_at,
        "finished_at": None,
        "output_root": str(root),
        "batch_root": str(batch_root),
        "batch_size": max(1, int(batch_size or getattr(cfg, "EXPORT_BATCH_SIZE", 30) or 30)),
        "allocation_strategy": _export_batch_strategy(cfg),
        "eligible_count": None,
        "actionable_count": None,
        "packaged_count": 0,
        "pending_count": None,
        "packaged_total": _nonnegative_int(previous.get("packaged_total")),
        "error_count": 0,
        "errors": [],
        "dry_run": bool(dry_run),
    }
    _write_export_status(status_path, base_status)
    try:
        result = _package_export_batches_impl(
            output_root,
            cfg=cfg,
            batch_size=batch_size,
            dry_run=dry_run,
        )
    except Exception as exc:
        finished_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="microseconds")
        _write_export_status(
            status_path,
            {
                **base_status,
                "status": "failed",
                "updated_at": finished_at,
                "finished_at": finished_at,
                "error_count": 1,
                "errors": [str(exc)],
            },
        )
        raise

    finished_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="microseconds")
    error_count = _nonnegative_int(result.get("error_count")) or 0
    final_status = "preflight" if dry_run else ("completed_with_errors" if error_count else "completed")
    snapshot = {
        **base_status,
        "status": final_status,
        "updated_at": finished_at,
        "finished_at": finished_at,
        "batch_size": _nonnegative_int(result.get("batch_size")) or base_status["batch_size"],
        "allocation_strategy": str(result.get("allocation_strategy") or base_status["allocation_strategy"]),
        "eligible_count": _nonnegative_int(result.get("eligible_count")) or 0,
        "actionable_count": _nonnegative_int(result.get("actionable_count")) or 0,
        "packaged_count": 0 if dry_run else (_nonnegative_int(result.get("packaged_count")) or 0),
        "pending_count": _nonnegative_int(result.get("pending_count")) or 0,
        "packaged_total": _nonnegative_int(result.get("packaged_total")) or 0,
        "error_count": error_count,
        "errors": [str(item) for item in result.get("errors", []) if item],
    }
    _write_export_status(status_path, snapshot)
    return {**result, "status_path": str(status_path), "trigger": snapshot["trigger"]}


def _package_export_batches_impl(
    output_root: str | Path,
    cfg=None,
    batch_size: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Move export-ready clips into numbered affiliate batch folders."""
    if cfg is None:
        import config as cfg  # type: ignore

    root = Path(output_root).resolve(strict=False)
    size = max(1, int(batch_size or getattr(cfg, "EXPORT_BATCH_SIZE", 30) or 30))
    batch_dir_name = str(getattr(cfg, "EXPORT_BATCH_DIR_NAME", "export_batches") or "export_batches")
    batch_root = resolve_within_root(root, batch_dir_name)
    manifest_path = resolve_within_root(batch_root, "_manifest.json")
    manifest = _load_packager_manifest(manifest_path)
    existing_items = [item for item in manifest.get("items", []) if isinstance(item, dict)]
    existing_source_keys = {
        str(item.get("source_clip_key") or "")
        for item in existing_items
        if item.get("source_clip_key")
    }
    existing_hashes = {
        str(item.get("content_md5_64k") or "")
        for item in existing_items
        if item.get("content_md5_64k")
    }
    strategy = _export_batch_strategy(cfg)
    one_variant_per_clip = bool(getattr(cfg, "EXPORT_PACK_ONE_VARIANT_PER_CLIP", False))

    raw_candidates = _discover_export_ready_candidates(root, batch_root)
    existing_counts = _existing_batch_counts(batch_root, existing_items)
    legacy_batch_folder_cutoff, cutoff_needs_persist = _resolve_legacy_batch_folder_cutoff(
        manifest,
        existing_counts,
    )
    candidate_pool = raw_candidates
    variant_stats = {
        "one_variant_per_clip": one_variant_per_clip or strategy == ROTATION_STRATEGY,
        "excluded_variant_count": 0,
        "excluded_existing_base_count": 0,
        "excluded_legacy_vod_count": 0,
    }
    rotation_layout: dict[str, Any] | None = None
    if strategy == ROTATION_STRATEGY:
        rotation_layout = _load_or_create_rotation_layout(
            manifest,
            existing_counts,
            existing_items,
            size,
        )
        size = max(1, int(rotation_layout.get("batch_size") or size))
        deduped_raw_candidates, duplicate_stats = _dedupe_candidates(
            raw_candidates,
            existing_source_keys=existing_source_keys,
            existing_hashes=existing_hashes,
        )
        candidate_pool, variant_stats = _select_rotation_candidates(
            deduped_raw_candidates,
            existing_items=existing_items,
            rotation_layout=rotation_layout,
            batch_size=size,
            variant_count=max(
                1,
                int(getattr(cfg, "EXPORT_BATCH_VARIANT_COUNT", DEFAULT_VARIANT_COUNT) or DEFAULT_VARIANT_COUNT),
            ),
        )
        candidates = candidate_pool
    elif one_variant_per_clip:
        candidate_pool, variant_stats = _select_one_variant_per_base_clip(
            raw_candidates,
            existing_base_clip_keys=_existing_base_clip_keys(existing_items),
        )
        candidates, duplicate_stats = _dedupe_candidates(
            candidate_pool,
            existing_source_keys=existing_source_keys,
            existing_hashes=existing_hashes,
        )
    else:
        candidates, duplicate_stats = _dedupe_candidates(
            candidate_pool,
            existing_source_keys=existing_source_keys,
            existing_hashes=existing_hashes,
        )
    if strategy == ROTATION_STRATEGY:
        candidates.sort(key=_rotation_candidate_sort_key)
        assignments = _assign_vod_clip_rotation(
            candidates,
            rotation_layout=rotation_layout or {},
            existing_counts=existing_counts,
        )
    else:
        candidates.sort(
            key=lambda item: (
                -item.total_score,
                item.normalized_source_vod,
                item.clip_id.casefold(),
                item.source_output_file.casefold(),
            )
        )
        assignments = _assign_score_round_robin(
            candidates,
            existing_counts,
            existing_items,
            size,
            legacy_batch_folder_cutoff=legacy_batch_folder_cutoff,
        )
    planned_destinations: set[str] = set()
    moved_items: list[dict[str, Any]] = []
    errors: list[str] = []
    move_plans: list[tuple[ExportCandidate, Path, Path, str, dict[str, Any]]] = []
    now = _now_iso()

    for candidate, batch_number in assignments:
        destination = _destination_for_candidate(
            candidate,
            batch_root / str(batch_number),
            planned_destinations,
        )
        planned_destinations.add(str(destination.resolve()).casefold())
        try:
            source_dir = resolve_within_root(root, candidate.source_dir, kind="dir")
            export_ready_root = resolve_within_root(source_dir, "export_ready", kind="dir")
            source_path_before = resolve_within_root(export_ready_root, candidate.source_path, kind="file")
            destination = resolve_within_root(batch_root, destination)
            resolve_within_root(source_dir, "manifest.json")
            resolve_within_root(source_dir, "scores_summary.json")
        except (OSError, UnsafePathError) as exc:
            errors.append(f"unsafe export assignment for {candidate.clip_id}: {exc}")
            continue
        relative_destination = _relative_path(destination, candidate.source_dir)
        item_payload = {
            "source_vod": candidate.source_vod,
            "normalized_source_vod": candidate.normalized_source_vod,
            "clip_id": candidate.clip_id,
            "base_clip_id": candidate.base_clip_id,
            "selected_variant": candidate.variant_id,
            "excluded_variants": list(candidate.excluded_variants or []),
            "selection_reason": candidate.selection_reason or (
                "best_variant_only" if one_variant_per_clip else "all_variants_included"
            ),
            "allocation_strategy": strategy,
            "requested_variant": candidate.requested_variant,
            "vod_index": candidate.vod_index,
            "vod_group": candidate.vod_group,
            "clip_number": candidate.clip_number,
            "lane_key": candidate.lane_key,
            "source_clip_key": candidate.source_clip_key,
            "base_clip_key": candidate.base_clip_key,
            "content_md5_64k": candidate.content_md5_64k,
            "total_score": candidate.total_score,
            "product": candidate.product,
            "clip_type": candidate.clip_type,
            "source_output_dir": str(candidate.source_dir.resolve()),
            "source_output_file": candidate.source_output_file,
            "source_path": str(source_path_before.resolve()),
            "batch_folder": str(batch_number),
            "destination_file": destination.name,
            "destination_output_file": relative_destination,
            "destination_path": str(destination.resolve()),
            "packaged_at": now,
        }
        if dry_run:
            moved_items.append(item_payload)
            continue
        move_plans.append((candidate, source_path_before, destination, relative_destination, item_payload))

    # The full move set is canonicalized before creating batch folders or
    # moving the first clip, preventing a late malicious row from causing a
    # partially applied package operation.
    if move_plans:
        batch_root.mkdir(parents=True, exist_ok=True)

    for candidate, source_path_before, destination, relative_destination, item_payload in move_plans:
        try:
            source_dir = resolve_within_root(root, candidate.source_dir, kind="dir")
            export_ready_root = resolve_within_root(source_dir, "export_ready", kind="dir")
            source_path_before = resolve_within_root(export_ready_root, source_path_before, kind="file")
            destination = resolve_within_root(batch_root, destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            source_path_before = resolve_within_root(export_ready_root, source_path_before, kind="file")
            destination = resolve_within_root(batch_root, destination)
            shutil.move(str(source_path_before), str(destination))
        except Exception as exc:
            errors.append(f"{source_path_before} -> {destination}: {exc}")
            continue

        _update_source_manifest(candidate.source_dir, candidate.clip_id, relative_destination, item_payload)
        _update_source_scores_summary(candidate.source_dir, candidate.clip_id, relative_destination, destination)
        moved_items.append(item_payload)

    has_existing_batches = bool(existing_items) or manifest_path.exists() or (
        batch_root.exists()
        and any(path.is_dir() and path.name.isdigit() for path in batch_root.iterdir())
    )
    should_write_manifest = moved_items or (cutoff_needs_persist and has_existing_batches)
    if should_write_manifest and not dry_run:
        manifest_items = existing_items + moved_items
        updated_manifest = dict(manifest)
        updated_manifest.update({
            "schema_version": PACKAGER_SCHEMA_VERSION,
            "updated_at": now,
            "output_root": str(root.resolve()),
            "batch_root": str(batch_root.resolve()),
            "batch_size": size,
            "legacy_batch_folder_cutoff": legacy_batch_folder_cutoff,
            "order": strategy,
            "allocation_strategy": strategy,
            "append_only": True,
            "items": manifest_items,
            "counts_by_batch": _counts_by_batch(manifest_items, batch_root),
        })
        if rotation_layout is not None:
            updated_manifest["rotation_layout"] = rotation_layout
        manifest_path = resolve_within_root(batch_root, manifest_path)
        _write_json_atomic(manifest_path, updated_manifest)

    return {
        "schema_version": PACKAGER_SCHEMA_VERSION,
        "output_root": str(root.resolve()),
        "batch_root": str(batch_root.resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "batch_size": size,
        "legacy_batch_folder_cutoff": legacy_batch_folder_cutoff,
        "dry_run": dry_run,
        "eligible_count": len(raw_candidates),
        "candidate_count_after_variant_filter": len(candidate_pool),
        "new_unique_count": len(candidates),
        "actionable_count": len(candidates),
        "packaged_count": len(moved_items),
        "pending_count": (
            len(candidates)
            if dry_run
            else max(0, len(candidates) - len(moved_items))
        ),
        "packaged_total": len(existing_items) + (0 if dry_run else len(moved_items)),
        "allocation_strategy": strategy,
        "one_variant_per_clip": variant_stats["one_variant_per_clip"],
        "excluded_variant_count": variant_stats["excluded_variant_count"],
        "excluded_existing_base_count": variant_stats["excluded_existing_base_count"],
        "excluded_legacy_vod_count": variant_stats.get("excluded_legacy_vod_count", 0),
        "duplicate_existing_count": duplicate_stats["duplicate_existing_count"],
        "duplicate_candidate_count": duplicate_stats["duplicate_candidate_count"],
        "missing_count": duplicate_stats["missing_count"],
        "error_count": len(errors),
        "errors": errors,
        "assignments": moved_items,
    }


def _discover_export_ready_candidates(root: Path, batch_root: Path) -> list[ExportCandidate]:
    if not root.exists():
        return []
    candidates: list[ExportCandidate] = []
    excluded_names = {batch_root.name.casefold(), *TIER_DIRS}
    source_dirs = _source_dirs_for_packaging(root, excluded_names)
    for source_dir in source_dirs:
        if source_dir.name.casefold() in excluded_names:
            continue
        try:
            source_dir = resolve_within_root(root, source_dir, kind="dir")
        except (OSError, UnsafePathError):
            continue
        manifest_rows = _load_source_manifest(source_dir)
        manifest_by_clip = {
            str(row.get("clip_id") or ""): row
            for row in manifest_rows
            if isinstance(row, dict) and row.get("clip_id")
        }
        scores = _load_score_clips(source_dir)
        seen_clip_ids: set[str] = set()
        for score in scores:
            score_clip_id = str(score.get("clip_id") or "")
            if score_clip_id:
                # A persisted score row is authoritative for its clip. If its
                # clip_path is inconsistent or unsafe, do not fall back to a
                # less-specific manifest row and accidentally authorize it.
                seen_clip_ids.add(score_clip_id)
            candidate = _candidate_from_score(source_dir, score, manifest_by_clip)
            if candidate is not None:
                candidates.append(candidate)
                seen_clip_ids.add(candidate.clip_id)
        for row in manifest_rows:
            if not isinstance(row, dict):
                continue
            clip_id = str(row.get("clip_id") or "")
            if not clip_id or clip_id in seen_clip_ids:
                continue
            candidate = _candidate_from_manifest_row(source_dir, row)
            if candidate is not None:
                candidates.append(candidate)
    return candidates


def _source_dirs_for_packaging(root: Path, excluded_names: set[str]) -> list[Path]:
    if _looks_like_source_output_dir(root):
        return [root]
    return sorted(
        (
            item
            for item in root.iterdir()
            if item.is_dir() and item.name.casefold() not in excluded_names
        ),
        key=lambda item: item.name.casefold(),
    )


def _looks_like_source_output_dir(path: Path) -> bool:
    return (path / "manifest.json").exists() or (path / "scores_summary.json").exists()


def _candidate_from_score(
    source_dir: Path,
    score: dict[str, Any],
    manifest_by_clip: dict[str, dict[str, Any]],
) -> ExportCandidate | None:
    clip_id = str(score.get("clip_id") or "")
    if not clip_id:
        return None
    manifest_row = manifest_by_clip.get(clip_id, {})
    if _is_blocked_or_failed(score) or _is_blocked_or_failed(manifest_row):
        return None
    output_file = str(score.get("output_file") or manifest_row.get("output_file") or "")
    clip_path = str(score.get("clip_path") or "")
    source_path = _resolve_clip_path(source_dir, output_file, clip_path)
    if source_path is None or not source_path.exists() or not source_path.is_file():
        return None
    total_score = _safe_float(score.get("total_score"), manifest_row.get("scorer_total_score"), default=0.0)
    return _build_candidate(
        source_dir=source_dir,
        clip_id=clip_id,
        source_path=source_path,
        source_output_file=output_file or _relative_path(source_path, source_dir),
        total_score=total_score,
        product=str(score.get("product") or manifest_row.get("product") or ""),
        clip_type=str(score.get("clip_type") or manifest_row.get("clip_type") or ""),
    )


def _candidate_from_manifest_row(source_dir: Path, row: dict[str, Any]) -> ExportCandidate | None:
    if _is_blocked_or_failed(row):
        return None
    clip_id = str(row.get("clip_id") or "")
    output_file = str(row.get("output_file") or "")
    if not clip_id:
        return None
    source_path = _resolve_clip_path(source_dir, output_file, "")
    if source_path is None or not source_path.exists() or not source_path.is_file():
        return None
    total_score = _safe_float(row.get("scorer_total_score"), row.get("score"), default=0.0)
    return _build_candidate(
        source_dir=source_dir,
        clip_id=clip_id,
        source_path=source_path,
        source_output_file=output_file,
        total_score=total_score,
        product=str(row.get("product") or ""),
        clip_type=str(row.get("clip_type") or ""),
    )


def _build_candidate(
    source_dir: Path,
    clip_id: str,
    source_path: Path,
    source_output_file: str,
    total_score: float,
    product: str = "",
    clip_type: str = "",
) -> ExportCandidate | None:
    try:
        content_hash = _md5_first_64k(source_path)
    except OSError:
        return None
    normalized_source = _normalize_source_vod(source_dir.name)
    normalized_clip = _normalize_key(clip_id)
    base_clip_id, variant_id = _base_and_variant_ids(clip_id, source_path, source_output_file)
    normalized_base = _normalize_key(base_clip_id)
    return ExportCandidate(
        source_dir=source_dir,
        source_vod=source_dir.name,
        normalized_source_vod=normalized_source,
        clip_id=clip_id,
        base_clip_id=base_clip_id,
        variant_id=variant_id,
        base_clip_key=f"{normalized_source}:{normalized_base}",
        source_clip_key=f"{normalized_source}:{normalized_clip}",
        source_path=source_path,
        source_output_file=source_output_file,
        total_score=total_score,
        content_md5_64k=content_hash,
        product=product,
        clip_type=clip_type,
    )


def _select_one_variant_per_base_clip(
    candidates: list[ExportCandidate],
    existing_base_clip_keys: set[str],
) -> tuple[list[ExportCandidate], dict[str, Any]]:
    selected: list[ExportCandidate] = []
    excluded_variant_count = 0
    excluded_existing_base_count = 0
    candidates_by_base: dict[str, list[ExportCandidate]] = {}
    for candidate in candidates:
        if candidate.base_clip_key in existing_base_clip_keys:
            excluded_existing_base_count += 1
            continue
        candidates_by_base.setdefault(candidate.base_clip_key, []).append(candidate)
    for base_key in sorted(candidates_by_base):
        variants = sorted(
            candidates_by_base[base_key],
            key=lambda candidate: (-candidate.total_score, _variant_sort_key(candidate)),
        )
        if not variants:
            continue
        chosen = variants[0]
        chosen.excluded_variants = [
            candidate.variant_id
            for index, candidate in enumerate(variants)
            if index != 0
        ]
        excluded_variant_count += max(0, len(variants) - 1)
        selected.append(chosen)
    return selected, {
        "one_variant_per_clip": True,
        "excluded_variant_count": excluded_variant_count,
        "excluded_existing_base_count": excluded_existing_base_count,
        "excluded_legacy_vod_count": 0,
    }


def _export_batch_strategy(cfg) -> str:
    value = str(getattr(cfg, "EXPORT_BATCH_STRATEGY", ROTATION_STRATEGY) or ROTATION_STRATEGY)
    normalized = value.casefold().strip()
    aliases = {
        "score_round_robin": SCORE_ROUND_ROBIN_STRATEGY,
        "all_variants": SCORE_ROUND_ROBIN_STRATEGY,
        SCORE_ROUND_ROBIN_STRATEGY: SCORE_ROUND_ROBIN_STRATEGY,
        ROTATION_STRATEGY: ROTATION_STRATEGY,
    }
    return aliases.get(normalized, ROTATION_STRATEGY)


def _load_or_create_rotation_layout(
    manifest: dict[str, Any],
    existing_counts: dict[int, int],
    existing_items: list[dict[str, Any]],
    batch_size: int,
) -> dict[str, Any]:
    stored = manifest.get("rotation_layout")
    if isinstance(stored, dict) and int(stored.get("version") or 0) == ROTATION_LAYOUT_VERSION:
        layout = dict(stored)
        layout["vod_order"] = [
            str(value)
            for value in layout.get("vod_order", [])
            if str(value).strip()
        ]
        layout["lanes"] = {
            str(key): int(value)
            for key, value in dict(layout.get("lanes") or {}).items()
            if str(key).strip() and _coerce_nonnegative_int(value) is not None
        }
        layout["batch_size"] = max(1, int(layout.get("batch_size") or batch_size))
        layout.setdefault("started_at_folder", max(existing_counts.keys(), default=0) + 1)
        layout.setdefault("legacy_source_vods", _legacy_source_vods(existing_items))
        return layout

    return {
        "version": ROTATION_LAYOUT_VERSION,
        "strategy": ROTATION_STRATEGY,
        "batch_size": max(1, int(batch_size)),
        "started_at_folder": max(existing_counts.keys(), default=0) + 1,
        "vod_order": [],
        "lanes": {},
        "legacy_source_vods": _legacy_source_vods(existing_items),
    }


def _legacy_source_vods(existing_items: list[dict[str, Any]]) -> list[str]:
    return sorted({
        _normalize_source_vod(str(item.get("normalized_source_vod") or item.get("source_vod") or ""))
        for item in existing_items
        if isinstance(item, dict)
        and str(item.get("allocation_strategy") or "") != ROTATION_STRATEGY
    })


def _select_rotation_candidates(
    candidates: list[ExportCandidate],
    existing_items: list[dict[str, Any]],
    rotation_layout: dict[str, Any],
    batch_size: int,
    variant_count: int,
) -> tuple[list[ExportCandidate], dict[str, Any]]:
    selected: list[ExportCandidate] = []
    excluded_variant_count = 0
    excluded_existing_base_count = 0
    excluded_legacy_vod_count = 0
    existing_bases = _existing_base_clip_keys(existing_items)
    legacy_vods = {
        _normalize_source_vod(value)
        for value in rotation_layout.get("legacy_source_vods", [])
    }
    vod_order = [
        _normalize_source_vod(value)
        for value in rotation_layout.get("vod_order", [])
    ]
    vod_indexes = {source: index for index, source in enumerate(vod_order)}
    candidates_by_vod: dict[str, list[ExportCandidate]] = {}
    for candidate in candidates:
        candidates_by_vod.setdefault(candidate.normalized_source_vod, []).append(candidate)

    for source_vod in sorted(candidates_by_vod):
        source_candidates = candidates_by_vod[source_vod]
        if source_vod in legacy_vods and source_vod not in vod_indexes:
            excluded_legacy_vod_count += len(source_candidates)
            continue
        if source_vod not in vod_indexes:
            vod_indexes[source_vod] = len(vod_order)
            vod_order.append(source_vod)
        vod_index = vod_indexes[source_vod]
        candidates_by_base: dict[str, list[ExportCandidate]] = {}
        for candidate in source_candidates:
            if candidate.base_clip_key in existing_bases:
                excluded_existing_base_count += 1
                continue
            candidates_by_base.setdefault(candidate.base_clip_key, []).append(candidate)

        for base_key in sorted(candidates_by_base, key=lambda key: _base_group_sort_key(candidates_by_base[key])):
            variants = candidates_by_base[base_key]
            clip_number = _clip_number(variants[0].base_clip_id)
            if clip_number is None:
                clip_number = _fallback_clip_position(variants[0].base_clip_id, candidates_by_base)
            requested_index = (vod_index + max(0, clip_number - 1)) % variant_count
            chosen, used_fallback = _select_rotated_variant(variants, requested_index, variant_count)
            chosen.excluded_variants = [
                candidate.variant_id
                for candidate in variants
                if candidate is not chosen
            ]
            chosen.allocation_strategy = ROTATION_STRATEGY
            chosen.selection_reason = "rotation_fallback" if used_fallback else "vod_clip_rotation"
            chosen.requested_variant = f"v{requested_index}"
            chosen.vod_index = vod_index
            chosen.vod_group = vod_index // max(1, batch_size)
            chosen.clip_number = clip_number
            chosen.lane_key = f"clip_{clip_number}:vod_group_{chosen.vod_group}"
            excluded_variant_count += max(0, len(variants) - 1)
            selected.append(chosen)

    rotation_layout["vod_order"] = vod_order
    return selected, {
        "one_variant_per_clip": True,
        "excluded_variant_count": excluded_variant_count,
        "excluded_existing_base_count": excluded_existing_base_count,
        "excluded_legacy_vod_count": excluded_legacy_vod_count,
    }


def _base_group_sort_key(candidates: list[ExportCandidate]) -> tuple[int, str]:
    base_clip_id = candidates[0].base_clip_id if candidates else ""
    clip_number = _clip_number(base_clip_id)
    return (clip_number if clip_number is not None else 999999, base_clip_id.casefold())


def _fallback_clip_position(base_clip_id: str, groups: dict[str, list[ExportCandidate]]) -> int:
    ordered = sorted(
        {items[0].base_clip_id for items in groups.values() if items},
        key=lambda value: value.casefold(),
    )
    try:
        return ordered.index(base_clip_id) + 1
    except ValueError:
        return 1


def _clip_number(base_clip_id: str) -> int | None:
    match = re.search(r"(?:^|_)clip_(\d+)(?:_|$)", str(base_clip_id or ""), flags=re.IGNORECASE)
    if match is None:
        match = re.search(r"(\d+)", str(base_clip_id or ""))
    return int(match.group(1)) if match else None


def _variant_number(candidate: ExportCandidate) -> int | None:
    match = re.match(r"v(\d+)", str(candidate.variant_id or ""), flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _select_rotated_variant(
    variants: list[ExportCandidate],
    requested_index: int,
    variant_count: int,
) -> tuple[ExportCandidate, bool]:
    ordered = sorted(
        variants,
        key=lambda candidate: (
            _variant_number(candidate) if _variant_number(candidate) is not None else 999999,
            -candidate.total_score,
            candidate.source_output_file.casefold(),
        ),
    )
    by_index: dict[int, list[ExportCandidate]] = {}
    for candidate in ordered:
        number = _variant_number(candidate)
        if number is not None:
            by_index.setdefault(number, []).append(candidate)
    for offset in range(max(1, variant_count)):
        index = (requested_index + offset) % max(1, variant_count)
        if by_index.get(index):
            return by_index[index][0], offset != 0
    return ordered[0], True


def _rotation_candidate_sort_key(candidate: ExportCandidate) -> tuple[int, int, str, str]:
    return (
        candidate.vod_index if candidate.vod_index is not None else 999999,
        candidate.clip_number if candidate.clip_number is not None else 999999,
        candidate.normalized_source_vod,
        candidate.clip_id.casefold(),
    )


def _assign_vod_clip_rotation(
    candidates: list[ExportCandidate],
    rotation_layout: dict[str, Any],
    existing_counts: dict[int, int],
) -> list[tuple[ExportCandidate, int]]:
    lanes = {
        str(key): int(value)
        for key, value in dict(rotation_layout.get("lanes") or {}).items()
        if _coerce_nonnegative_int(value) is not None
    }
    next_folder = max(
        [max(existing_counts.keys(), default=0), int(rotation_layout.get("started_at_folder") or 1) - 1, *lanes.values()]
    ) + 1
    assignments: list[tuple[ExportCandidate, int]] = []
    for candidate in sorted(candidates, key=_rotation_candidate_sort_key):
        lane_key = candidate.lane_key
        if lane_key not in lanes:
            lanes[lane_key] = next_folder
            next_folder += 1
        assignments.append((candidate, lanes[lane_key]))
    rotation_layout["lanes"] = lanes
    return assignments


def _variant_sort_key(candidate: ExportCandidate) -> tuple[int, str, str]:
    match = re.match(r"v(\d+)", str(candidate.variant_id or ""), flags=re.IGNORECASE)
    variant_number = int(match.group(1)) if match else 9999
    return (
        variant_number,
        str(candidate.variant_id or "").casefold(),
        candidate.source_output_file.casefold(),
    )


def _stable_variant_index(base_key: str, variant_count: int) -> int:
    if variant_count <= 1:
        return 0
    digest = hashlib.md5(base_key.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % variant_count


def _existing_base_clip_keys(items: list[dict[str, Any]]) -> set[str]:
    keys: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        base_key = str(item.get("base_clip_key") or "").strip()
        if base_key:
            keys.add(base_key)
            continue
        source = _normalize_source_vod(str(item.get("source_vod") or item.get("normalized_source_vod") or ""))
        base_clip_id = str(item.get("base_clip_id") or "")
        if not base_clip_id:
            clip_id = str(item.get("clip_id") or "")
            output_file = str(item.get("source_output_file") or item.get("destination_file") or "")
            base_clip_id, _variant_id = _base_and_variant_ids(clip_id, None, output_file)
        keys.add(f"{source}:{_normalize_key(base_clip_id)}")
    return keys


def _dedupe_candidates(
    candidates: list[ExportCandidate],
    existing_source_keys: set[str],
    existing_hashes: set[str],
) -> tuple[list[ExportCandidate], dict[str, int]]:
    seen_source_keys: set[str] = set()
    seen_hashes: set[str] = set()
    unique: list[ExportCandidate] = []
    duplicate_existing = 0
    duplicate_candidate = 0
    missing = 0
    for candidate in sorted(candidates, key=lambda item: -item.total_score):
        if not candidate.source_path.exists():
            missing += 1
            continue
        if candidate.source_clip_key in existing_source_keys or candidate.content_md5_64k in existing_hashes:
            duplicate_existing += 1
            continue
        if candidate.source_clip_key in seen_source_keys or candidate.content_md5_64k in seen_hashes:
            duplicate_candidate += 1
            continue
        seen_source_keys.add(candidate.source_clip_key)
        seen_hashes.add(candidate.content_md5_64k)
        unique.append(candidate)
    return unique, {
        "duplicate_existing_count": duplicate_existing,
        "duplicate_candidate_count": duplicate_candidate,
        "missing_count": missing,
    }


def _assign_score_round_robin(
    candidates: list[ExportCandidate],
    existing_counts: dict[int, int],
    existing_items: list[dict[str, Any]],
    batch_size: int,
    legacy_batch_folder_cutoff: int = 0,
) -> list[tuple[ExportCandidate, int]]:
    if not candidates:
        return []
    cutoff = max(0, int(legacy_batch_folder_cutoff or 0))
    counts = Counter(
        {
            folder_number: count
            for folder_number, count in existing_counts.items()
            if folder_number > cutoff
        }
    )
    existing_total = sum(max(0, value) for value in counts.values())
    max_existing_folder = max(counts.keys(), default=cutoff)
    required_folder_count = max(
        max_existing_folder,
        cutoff + math.ceil((existing_total + len(candidates)) / batch_size),
    )
    for folder_number in range(cutoff + 1, required_folder_count + 1):
        counts.setdefault(folder_number, 0)

    bases_by_folder = _existing_base_clip_keys_by_batch(existing_items, cutoff)
    available = [
        folder
        for folder in range(cutoff + 1, required_folder_count + 1)
        if counts[folder] < batch_size
    ]
    assignments: list[tuple[ExportCandidate, int]] = []
    cursor = 0
    next_folder = required_folder_count + 1
    for candidate in candidates:
        while True:
            if not available:
                available.append(next_folder)
                counts[next_folder] = 0
                next_folder += 1
            cursor = cursor % len(available)
            folder_number, cursor = _next_folder_for_candidate(
                candidate,
                available,
                bases_by_folder,
                cursor,
                next_folder,
            )
            if folder_number != next_folder:
                break
            available.append(next_folder)
            counts[next_folder] = 0
            next_folder += 1

        assignments.append((candidate, folder_number))
        counts[folder_number] += 1
        bases_by_folder.setdefault(folder_number, set()).add(candidate.base_clip_key)
        if counts[folder_number] >= batch_size:
            available.remove(folder_number)
            if available:
                cursor %= len(available)
        else:
            cursor += 1
    return assignments


def _next_folder_for_candidate(
    candidate: ExportCandidate,
    available: list[int],
    bases_by_folder: dict[int, set[str]],
    cursor: int,
    next_folder: int,
) -> tuple[int, int]:
    for offset in range(len(available)):
        index = (cursor + offset) % len(available)
        folder_number = available[index]
        if candidate.base_clip_key not in bases_by_folder.get(folder_number, set()):
            return folder_number, index
    return next_folder, cursor


def _existing_base_clip_keys_by_batch(
    items: list[dict[str, Any]],
    legacy_batch_folder_cutoff: int = 0,
) -> dict[int, set[str]]:
    keys_by_folder: dict[int, set[str]] = {}
    cutoff = max(0, int(legacy_batch_folder_cutoff or 0))
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            folder_number = int(str(item.get("batch_folder") or ""))
        except ValueError:
            continue
        if folder_number <= cutoff:
            continue
        base_key = str(item.get("base_clip_key") or "").strip()
        if not base_key:
            source = _normalize_source_vod(str(item.get("source_vod") or item.get("normalized_source_vod") or ""))
            base_clip_id = str(item.get("base_clip_id") or "")
            if not base_clip_id:
                clip_id = str(item.get("clip_id") or "")
                output_file = str(item.get("source_output_file") or item.get("destination_file") or "")
                base_clip_id, _variant_id = _base_and_variant_ids(clip_id, None, output_file)
            base_key = f"{source}:{_normalize_key(base_clip_id)}"
        keys_by_folder.setdefault(folder_number, set()).add(base_key)
    return keys_by_folder


def _destination_for_candidate(
    candidate: ExportCandidate,
    destination_dir: Path,
    planned_destinations: set[str],
) -> Path:
    source_slug = _safe_filename(candidate.normalized_source_vod) or "source"
    source_name = _safe_filename(candidate.source_path.stem) or _safe_filename(candidate.clip_id) or "clip"
    suffix = candidate.source_path.suffix or ".mp4"
    base_name = f"{source_slug}__{source_name}"
    destination = destination_dir / f"{base_name}{suffix}"
    if not _destination_taken(destination, planned_destinations):
        return destination
    destination = destination_dir / f"{base_name}__{candidate.content_md5_64k[:8]}{suffix}"
    if not _destination_taken(destination, planned_destinations):
        return destination
    counter = 2
    while True:
        candidate_path = destination_dir / f"{base_name}__{candidate.content_md5_64k[:8]}_{counter}{suffix}"
        if not _destination_taken(candidate_path, planned_destinations):
            return candidate_path
        counter += 1


def _destination_taken(path: Path, planned_destinations: set[str]) -> bool:
    key = str(path.resolve()).casefold()
    return key in planned_destinations or path.exists()


def _update_source_manifest(
    source_dir: Path,
    clip_id: str,
    relative_destination: str,
    item_payload: dict[str, Any],
) -> None:
    try:
        manifest_path = resolve_within_root(source_dir, "manifest.json", kind="file")
    except (OSError, UnsafePathError):
        return
    rows = _load_source_manifest(source_dir)
    if not rows:
        return
    changed = False
    for row in rows:
        if not isinstance(row, dict) or str(row.get("clip_id") or "") != clip_id:
            continue
        row["output_file"] = relative_destination
        row["export_batch_folder"] = item_payload["batch_folder"]
        row["export_batch_file"] = item_payload["destination_file"]
        row["export_batch_path"] = item_payload["destination_path"]
        row["export_packaged_at"] = item_payload["packaged_at"]
        changed = True
    if changed:
        manifest_path = resolve_within_root(source_dir, manifest_path, kind="file")
        _write_json_atomic(manifest_path, rows)


def _update_source_scores_summary(
    source_dir: Path,
    clip_id: str,
    relative_destination: str,
    destination: Path,
) -> None:
    try:
        summary_path = resolve_within_root(source_dir, "scores_summary.json", kind="file")
    except (OSError, UnsafePathError):
        return
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    destination_text = str(destination.resolve())
    output_dir_text = str(source_dir.resolve())

    def update_record(record: dict[str, Any]) -> None:
        record["clip_path"] = destination_text
        record["output_file"] = relative_destination
        record["output_dir"] = output_dir_text

    changed = False
    for record in payload.get("clips", []):
        if isinstance(record, dict) and str(record.get("clip_id") or "") == clip_id:
            update_record(record)
            changed = True
    for group in payload.get("groups", []):
        if isinstance(group, dict):
            changed = _update_group_record(group, clip_id, relative_destination, destination_text, output_dir_text) or changed
    if changed:
        summary_path = resolve_within_root(source_dir, summary_path, kind="file")
        _write_json_atomic(summary_path, payload)


def _update_group_record(
    record: dict[str, Any],
    clip_id: str,
    relative_destination: str,
    destination_text: str,
    output_dir_text: str,
) -> bool:
    changed = False
    if str(record.get("clip_id") or "") == clip_id:
        record["clip_path"] = destination_text
        record["output_file"] = relative_destination
        record["output_dir"] = output_dir_text
        changed = True
    if str(record.get("representative_clip_id") or "") == clip_id:
        record["clip_path"] = destination_text
        record["output_file"] = relative_destination
        record["representative_clip_path"] = destination_text
        record["representative_output_file"] = relative_destination
        record["output_dir"] = output_dir_text
        changed = True
    variants = record.get("variants")
    if isinstance(variants, list):
        for variant in variants:
            if isinstance(variant, dict):
                changed = _update_group_record(
                    variant,
                    clip_id,
                    relative_destination,
                    destination_text,
                    output_dir_text,
                ) or changed
    return changed


def _existing_batch_counts(batch_root: Path, existing_items: list[dict[str, Any]]) -> dict[int, int]:
    counts: Counter[int] = Counter()
    for item in existing_items:
        try:
            folder_number = int(str(item.get("batch_folder") or ""))
        except ValueError:
            continue
        counts[folder_number] += 1
    if batch_root.exists():
        for folder in batch_root.iterdir():
            if not folder.is_dir() or not folder.name.isdigit():
                continue
            actual_count = len([path for path in folder.glob("*.mp4") if path.is_file()])
            folder_number = int(folder.name)
            counts[folder_number] = max(counts[folder_number], actual_count)
    return dict(counts)


def _resolve_legacy_batch_folder_cutoff(
    manifest: dict[str, Any],
    existing_counts: dict[int, int],
) -> tuple[int, bool]:
    cutoff = _coerce_nonnegative_int(manifest.get("legacy_batch_folder_cutoff"))
    if cutoff is not None:
        return cutoff, False
    return max(existing_counts.keys(), default=0), True


def _coerce_nonnegative_int(value: Any) -> int | None:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _counts_by_batch(items: list[dict[str, Any]], batch_root: Path) -> dict[str, int]:
    counts = _existing_batch_counts(batch_root, items)
    return {str(key): counts[key] for key in sorted(counts)}


def _load_packager_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": PACKAGER_SCHEMA_VERSION, "items": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"schema_version": PACKAGER_SCHEMA_VERSION, "items": []}
    if not isinstance(payload, dict):
        return {"schema_version": PACKAGER_SCHEMA_VERSION, "items": []}
    if not isinstance(payload.get("items"), list):
        payload["items"] = []
    return payload


def _load_source_manifest(source_dir: Path) -> list[dict[str, Any]]:
    try:
        manifest_path = resolve_within_root(source_dir, "manifest.json", kind="file")
    except (OSError, UnsafePathError):
        return []
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return payload if isinstance(payload, list) else []


def _load_score_clips(source_dir: Path) -> list[dict[str, Any]]:
    try:
        summary_path = resolve_within_root(source_dir, "scores_summary.json", kind="file")
    except (OSError, UnsafePathError):
        return []
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    clips = payload.get("clips", []) if isinstance(payload, dict) else []
    return [clip for clip in clips if isinstance(clip, dict)]


def _resolve_clip_path(source_dir: Path, output_file: str, clip_path: str) -> Path | None:
    export_root = (source_dir / "export_ready").resolve(strict=False)

    def resolve_value(value: str) -> Path | None:
        if not value:
            return None
        raw = Path(value)
        candidate = raw if raw.is_absolute() else source_dir / raw
        return resolve_within_root(export_root, candidate)

    try:
        output_candidate = resolve_value(output_file)
        clip_candidate = resolve_value(clip_path)
    except (OSError, UnsafePathError):
        return None

    # Persisted score paths must agree with their output_file. In particular,
    # a valid textual output_file cannot authorize an unrelated absolute path.
    if output_candidate is not None and clip_candidate is not None and output_candidate != clip_candidate:
        return None
    candidate = clip_candidate or output_candidate
    if candidate is None:
        return None
    try:
        return resolve_within_root(export_root, candidate, kind="file")
    except (OSError, UnsafePathError):
        return None


def _is_export_ready_path(source_dir: Path, output_file: str, clip_path: str) -> bool:
    return _resolve_clip_path(source_dir, output_file, clip_path) is not None


def _is_blocked_or_failed(item: dict[str, Any]) -> bool:
    if not item:
        return False
    status = str(item.get("status") or "").casefold()
    if status in {"failed", "compliance_blocked", "filtered_low_score", "filtered_low_variant"}:
        return True
    if bool(item.get("compliance_blocked")):
        return True
    return False


def _md5_first_64k(path: Path) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        digest.update(handle.read(HASH_BYTES))
    return digest.hexdigest()


def _base_and_variant_ids(
    clip_id: str,
    source_path: Path | None,
    source_output_file: str = "",
) -> tuple[str, str]:
    for text in (
        str(clip_id or ""),
        source_path.stem if source_path is not None else "",
        Path(str(source_output_file or "").replace("\\", "/")).stem,
    ):
        text = text.strip()
        if not text:
            continue
        match = re.match(r"^(clip_\d+)(?:_(v\d+(?:_.*)?))?$", text, flags=re.IGNORECASE)
        if match:
            base = match.group(1)
            variant = match.group(2) or "original"
            return base, variant
        match = re.match(r"^(.+?)_(v\d+(?:_.*)?)$", text, flags=re.IGNORECASE)
        if match:
            return match.group(1), match.group(2)
    clean_clip_id = str(clip_id or "").strip()
    return clean_clip_id or "unknown", "original"


def _normalize_source_vod(value: str) -> str:
    return _normalize_key(value)


def _normalize_key(value: Any) -> str:
    text = str(value or "").casefold().strip()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_") or "unknown"


def _safe_filename(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text)
    text = re.sub(r"_+", "_", text)
    return text.strip(" ._")


def _safe_float(*values: Any, default: float = 0.0) -> float:
    for value in values:
        try:
            if value is None or value == "":
                continue
            return float(value)
        except (TypeError, ValueError):
            continue
    return default


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        try:
            return Path(os.path.relpath(path.resolve(), root.resolve())).as_posix()
        except ValueError:
            return path.resolve().as_posix()


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _load_export_status(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_export_status(path: Path, payload: dict[str, Any]) -> bool:
    """Atomically write the newest operation status without stale overwrite."""
    path.parent.mkdir(parents=True, exist_ok=True)
    root = path.parent.resolve(strict=False)
    path = resolve_within_root(root, path)
    operation_id = str(payload.get("operation_id") or uuid4().hex)
    started_at_ns = _nonnegative_int(payload.get("started_at_ns")) or 0
    with _EXPORT_STATUS_LOCK:
        current = _load_export_status(path)
        current_operation = str(current.get("operation_id") or "")
        current_started_at_ns = _nonnegative_int(current.get("started_at_ns")) or 0
        if current_started_at_ns > started_at_ns:
            return False
        if current_started_at_ns == started_at_ns and current_operation not in {"", operation_id}:
            return False
        tmp = resolve_within_root(root, path.with_name(f"{path.name}.{operation_id}.tmp"))
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp = resolve_within_root(root, tmp, kind="file")
        os.replace(tmp, path)
    return True


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    root = path.parent.resolve(strict=False)
    path = resolve_within_root(root, path)
    tmp = resolve_within_root(root, path.with_suffix(path.suffix + ".tmp"))
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp = resolve_within_root(root, tmp, kind="file")
    path = resolve_within_root(root, path)
    os.replace(tmp, path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
