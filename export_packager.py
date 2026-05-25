from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PACKAGER_SCHEMA_VERSION = 1
HASH_BYTES = 64 * 1024
TIER_DIRS = {"export_ready", "review_needed", "rejected"}


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


def package_export_batches(
    output_root: str | Path,
    cfg=None,
    batch_size: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Move export-ready clips into numbered affiliate batch folders."""
    if cfg is None:
        import config as cfg  # type: ignore

    root = Path(output_root)
    size = max(1, int(batch_size or getattr(cfg, "EXPORT_BATCH_SIZE", 30) or 30))
    batch_dir_name = str(getattr(cfg, "EXPORT_BATCH_DIR_NAME", "export_batches") or "export_batches")
    batch_root = root / batch_dir_name
    manifest_path = batch_root / "_manifest.json"
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
    one_variant_per_clip = bool(getattr(cfg, "EXPORT_PACK_ONE_VARIANT_PER_CLIP", True))

    raw_candidates = _discover_export_ready_candidates(root, batch_root)
    candidate_pool = raw_candidates
    variant_stats = {
        "one_variant_per_clip": one_variant_per_clip,
        "excluded_variant_count": 0,
        "excluded_existing_base_count": 0,
    }
    if one_variant_per_clip:
        candidate_pool, variant_stats = _select_one_variant_per_base_clip(
            raw_candidates,
            existing_base_clip_keys=_existing_base_clip_keys(existing_items),
        )
    candidates, duplicate_stats = _dedupe_candidates(
        candidate_pool,
        existing_source_keys=existing_source_keys,
        existing_hashes=existing_hashes,
    )
    candidates.sort(
        key=lambda item: (
            -item.total_score,
            item.normalized_source_vod,
            item.clip_id.casefold(),
            item.source_output_file.casefold(),
        )
    )

    existing_counts = _existing_batch_counts(batch_root, existing_items)
    assignments = _assign_score_round_robin(candidates, existing_counts, size)
    planned_destinations: set[str] = set()
    moved_items: list[dict[str, Any]] = []
    errors: list[str] = []
    now = _now_iso()

    if assignments and not dry_run:
        batch_root.mkdir(parents=True, exist_ok=True)

    for candidate, batch_number in assignments:
        destination = _destination_for_candidate(
            candidate,
            batch_root / str(batch_number),
            planned_destinations,
        )
        planned_destinations.add(str(destination.resolve()).casefold())
        source_path_before = candidate.source_path
        relative_destination = _relative_path(destination, candidate.source_dir)
        item_payload = {
            "source_vod": candidate.source_vod,
            "normalized_source_vod": candidate.normalized_source_vod,
            "clip_id": candidate.clip_id,
            "base_clip_id": candidate.base_clip_id,
            "selected_variant": candidate.variant_id,
            "excluded_variants": list(candidate.excluded_variants or []),
        "selection_reason": "stable_variant_rotation" if one_variant_per_clip else "all_variants_included",
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
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not source_path_before.exists():
                errors.append(f"missing source before move: {source_path_before}")
                continue
            shutil.move(str(source_path_before), str(destination))
        except Exception as exc:
            errors.append(f"{source_path_before} -> {destination}: {exc}")
            continue

        _update_source_manifest(candidate.source_dir, candidate.clip_id, relative_destination, item_payload)
        _update_source_scores_summary(candidate.source_dir, candidate.clip_id, relative_destination, destination)
        moved_items.append(item_payload)

    if moved_items and not dry_run:
        manifest_items = existing_items + moved_items
        updated_manifest = {
            "schema_version": PACKAGER_SCHEMA_VERSION,
            "updated_at": now,
            "output_root": str(root.resolve()),
            "batch_root": str(batch_root.resolve()),
            "batch_size": size,
            "order": "score_round_robin",
            "append_only": True,
            "items": manifest_items,
            "counts_by_batch": _counts_by_batch(manifest_items, batch_root),
        }
        _write_json_atomic(manifest_path, updated_manifest)

    return {
        "schema_version": PACKAGER_SCHEMA_VERSION,
        "output_root": str(root.resolve()),
        "batch_root": str(batch_root.resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "batch_size": size,
        "dry_run": dry_run,
        "eligible_count": len(raw_candidates),
        "candidate_count_after_variant_filter": len(candidate_pool),
        "new_unique_count": len(candidates),
        "packaged_count": len(moved_items),
        "one_variant_per_clip": one_variant_per_clip,
        "excluded_variant_count": variant_stats["excluded_variant_count"],
        "excluded_existing_base_count": variant_stats["excluded_existing_base_count"],
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
    for source_dir in sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.name.casefold()):
        if source_dir.name.casefold() in excluded_names:
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
    if not _is_export_ready_path(source_dir, output_file, clip_path):
        return None
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
    if not clip_id or not _is_export_ready_path(source_dir, output_file, ""):
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
        variants = sorted(candidates_by_base[base_key], key=_variant_sort_key)
        if not variants:
            continue
        selected_index = _stable_variant_index(base_key, len(variants))
        chosen = variants[selected_index]
        chosen.excluded_variants = [
            candidate.variant_id
            for index, candidate in enumerate(variants)
            if index != selected_index
        ]
        excluded_variant_count += max(0, len(variants) - 1)
        selected.append(chosen)
    return selected, {
        "one_variant_per_clip": True,
        "excluded_variant_count": excluded_variant_count,
        "excluded_existing_base_count": excluded_existing_base_count,
    }


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
    batch_size: int,
) -> list[tuple[ExportCandidate, int]]:
    if not candidates:
        return []
    counts = Counter(existing_counts)
    existing_total = sum(max(0, value) for value in counts.values())
    max_existing_folder = max(counts.keys(), default=0)
    required_folder_count = max(max_existing_folder, math.ceil((existing_total + len(candidates)) / batch_size))
    for folder_number in range(1, required_folder_count + 1):
        counts.setdefault(folder_number, 0)

    available = [folder for folder in range(1, required_folder_count + 1) if counts[folder] < batch_size]
    assignments: list[tuple[ExportCandidate, int]] = []
    cursor = 0
    next_folder = required_folder_count + 1
    for candidate in candidates:
        if not available:
            available.append(next_folder)
            counts[next_folder] = 0
            next_folder += 1
        cursor = cursor % len(available)
        folder_number = available[cursor]
        assignments.append((candidate, folder_number))
        counts[folder_number] += 1
        if counts[folder_number] >= batch_size:
            available.pop(cursor)
            if available:
                cursor %= len(available)
        else:
            cursor += 1
    return assignments


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
    manifest_path = source_dir / "manifest.json"
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
        _write_json_atomic(manifest_path, rows)


def _update_source_scores_summary(
    source_dir: Path,
    clip_id: str,
    relative_destination: str,
    destination: Path,
) -> None:
    summary_path = source_dir / "scores_summary.json"
    if not summary_path.exists():
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
    manifest_path = source_dir / "manifest.json"
    if not manifest_path.exists():
        return []
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return payload if isinstance(payload, list) else []


def _load_score_clips(source_dir: Path) -> list[dict[str, Any]]:
    summary_path = source_dir / "scores_summary.json"
    if not summary_path.exists():
        return []
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    clips = payload.get("clips", []) if isinstance(payload, dict) else []
    return [clip for clip in clips if isinstance(clip, dict)]


def _resolve_clip_path(source_dir: Path, output_file: str, clip_path: str) -> Path | None:
    candidates: list[Path] = []
    if clip_path:
        candidates.append(Path(clip_path))
    if output_file:
        output_candidate = Path(output_file)
        candidates.append(output_candidate if output_candidate.is_absolute() else source_dir / output_candidate)
    for candidate in candidates:
        try:
            if candidate.exists() and candidate.is_file():
                return candidate
        except OSError:
            continue
    return candidates[0] if candidates else None


def _is_export_ready_path(source_dir: Path, output_file: str, clip_path: str) -> bool:
    normalized_output = str(output_file or "").replace("\\", "/").lstrip("./")
    if normalized_output.split("/", 1)[0].casefold() == "export_ready":
        return True
    for raw_path in (clip_path, output_file):
        if not raw_path:
            continue
        path = Path(str(raw_path))
        if not path.is_absolute():
            path = source_dir / path
        try:
            path.resolve().relative_to((source_dir / "export_ready").resolve())
            return True
        except (OSError, ValueError):
            continue
    return False


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


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
