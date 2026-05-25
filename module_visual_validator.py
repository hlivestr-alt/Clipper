from __future__ import annotations

import json
import logging
import os
import threading
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

from module_extractor import (
    QUALITY_APPROVED,
    QUALITY_BLOCKED,
    QUALITY_NEEDS_REVIEW,
    QUALITY_NO_VISUAL_EVENTS,
    REVIEW_PENDING,
    ROLE_FOLDERS,
    canonical_product,
    library_index_lock,
    module_file_lock,
    module_quality_fields,
    rebuild_library_index,
)
from utils import _path_identity

log = logging.getLogger("proya.module_visual_validator")

VISUAL_VALIDATOR_SCHEMA_VERSION = 1
VISUAL_VALIDATOR_VERSION = "module_visual_validator_v4"
VISUAL_PASSED = "passed"
VISUAL_FAILED = "failed"
VISUAL_NOT_RUN = "not_run"
VISUAL_ALL = "all"
VALID_VISUAL_STATUS_FILTERS = {VISUAL_NOT_RUN, VISUAL_FAILED, VISUAL_PASSED, VISUAL_ALL}
VALID_VISUAL_PRIORITIES = {"assembly_ready", "index_order"}
_CUDA_VALIDATION_DISABLED = False
_CUDA_VALIDATION_LOCK = threading.Lock()


def validate_module_record_visual(record: dict[str, Any], cfg, model: Any | None = None) -> dict[str, Any]:
    """Run optional YOLO validation for one module sidecar record."""
    fingerprint = build_visual_validation_fingerprint(record, cfg)
    record["visual_validation_mode"] = "module_file"
    product = canonical_product(record.get("product"))
    if not product:
        return _apply_not_run(record, "unknown_product", fingerprint)

    module_path = Path(str(record.get("file_path") or ""))
    if not module_path.exists():
        return _apply_not_run(record, "module_file_missing", fingerprint)

    try:
        scan = scan_module_visual_events(module_path, product, cfg, model=model)
    except Exception as exc:
        log.warning("Visual validation could not run for %s: %s", module_path, exc)
        return _apply_not_run(record, f"validator_error:{exc}", fingerprint)

    hits = int(scan.get("hits") or 0)
    confidence_max = float(scan.get("confidence_max") or 0.0)
    events = scan.get("events") if isinstance(scan.get("events"), list) else []
    min_hits = max(1, int(getattr(cfg, "MODULE_VISUAL_VALIDATION_MIN_HITS", 1) or 1))

    record["visual_validation_fingerprint"] = fingerprint
    if hits >= min_hits:
        record["visual_validation_status"] = VISUAL_PASSED
        record["visual_validation_reason"] = "matched_product"
        _clear_previous_visual_mismatch(record, cfg)
    else:
        record["visual_validation_status"] = VISUAL_FAILED
        record["visual_validation_reason"] = "no_matching_product_detection"
        _demote_visual_mismatch(record, cfg)

    record["visual_product_hits"] = hits
    record["visual_product_confidence_max"] = round(confidence_max, 4)
    record["visual_product_events"] = events
    return record


def validate_module_library_visual(
    cfg,
    product: str | None = None,
    limit: int | None = None,
    visual_status: str = VISUAL_NOT_RUN,
    role: str | None = None,
    approved_only: bool = False,
    priority: str = "assembly_ready",
    force: bool = False,
) -> dict[str, Any]:
    """Validate existing library modules, atomically updating sidecars and rebuilding index."""
    library = Path(getattr(cfg, "MODULE_LIBRARY_DIR", r"D:\proya_modules"))
    product_filter = canonical_product(product) if product else None
    if product and not product_filter:
        raise ValueError(f"Unknown module product: {product}")
    status_filter = _normalize_visual_status_filter(visual_status)
    role_filter = _normalize_role_filter(role)
    priority = _normalize_priority(priority)

    with library_index_lock(library, cfg):
        index = rebuild_library_index(library, cfg, write=True)

    summaries, skipped_filter, skipped_current_filter = _select_visual_candidate_summaries(
        index=index,
        library=library,
        cfg=cfg,
        product_filter=product_filter,
        status_filter=status_filter,
        role_filter=role_filter,
        approved_only=bool(approved_only),
        priority=priority,
        force=bool(force),
    )
    if limit is not None:
        summaries = summaries[: max(0, int(limit))]

    counts: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    skipped_current = skipped_current_filter
    sidecar_error = 0
    model = None
    for summary in summaries:
        media_path = Path(str(summary.get("file_path") or ""))
        sidecar_path = Path(str(summary.get("sidecar_path") or media_path.with_suffix(".json")))
        lock_path = media_path.with_suffix(media_path.suffix + ".lock")
        with module_file_lock(lock_path, cfg):
            record = _read_json(sidecar_path)
            if not isinstance(record, dict):
                sidecar_error += 1
                rows.append(
                    {
                        "module_id": summary.get("module_id") or sidecar_path.stem,
                        "status": "sidecar_error",
                        "reason": "sidecar_unreadable",
                    }
                )
                continue
            if not force and visual_fingerprint_current(record, cfg):
                skipped_current += 1
                status = _normalize_visual_status(record.get("visual_validation_status"))
                rows.append(
                    {
                        "module_id": record.get("module_id") or sidecar_path.stem,
                        "product": record.get("product", ""),
                        "role": record.get("role", ""),
                        "status": status,
                        "skipped": "fingerprint_current",
                        "hits": int(record.get("visual_product_hits") or 0),
                        "confidence_max": float(record.get("visual_product_confidence_max") or 0.0),
                        "reason": record.get("visual_validation_reason", ""),
                        "quality_status": record.get("quality_status", ""),
                        "sidecar_path": str(sidecar_path.resolve()),
                    }
                )
                continue
            record = validate_module_record_visual(record, cfg, model=model)
            _write_json_atomic(sidecar_path, record)
            status = _normalize_visual_status(record.get("visual_validation_status"))
            counts[status] += 1
            rows.append(
                {
                    "module_id": record.get("module_id") or sidecar_path.stem,
                    "product": record.get("product", ""),
                    "role": record.get("role", ""),
                    "status": status,
                    "hits": int(record.get("visual_product_hits") or 0),
                    "confidence_max": float(record.get("visual_product_confidence_max") or 0.0),
                    "reason": record.get("visual_validation_reason", ""),
                    "quality_status": record.get("quality_status", ""),
                    "sidecar_path": str(sidecar_path.resolve()),
                }
            )

    with library_index_lock(library, cfg):
        rebuilt = rebuild_library_index(library, cfg, write=True)

    return {
        "validated": sum(counts.values()),
        "passed": counts[VISUAL_PASSED],
        "failed": counts[VISUAL_FAILED],
        "not_run": counts[VISUAL_NOT_RUN],
        "skipped_current": skipped_current,
        "skipped_filter": skipped_filter,
        "sidecar_error": sidecar_error,
        "product_filter": product_filter or "",
        "visual_status_filter": status_filter,
        "role_filter": role_filter or "",
        "approved_only": bool(approved_only),
        "priority": priority,
        "force": bool(force),
        "limit": limit,
        "index_path": str((library / "index.json").resolve()),
        "index_module_count": rebuilt.get("module_count", 0),
        "modules": rows,
    }


def build_visual_validation_fingerprint(record: dict[str, Any], cfg) -> dict[str, Any]:
    """Return the deterministic identity for a module visual validation run."""
    module_path = Path(str(record.get("file_path") or ""))
    yolo_path = Path(str(getattr(cfg, "YOLO_WEIGHTS", "")))
    fingerprint = {
        "schema_version": VISUAL_VALIDATOR_SCHEMA_VERSION,
        "validator_version": VISUAL_VALIDATOR_VERSION,
        "module_media_identity": _fingerprint_path_identity(module_path),
        "yolo_weights_identity": _fingerprint_path_identity(yolo_path),
        "settings": {
            "confidence": float(getattr(cfg, "MODULE_VISUAL_VALIDATION_MIN_CONFIDENCE", 0.55) or 0.55),
            "sample_fps": float(getattr(cfg, "MODULE_VISUAL_VALIDATION_SAMPLE_FPS", 1.0) or 1.0),
            "min_hits": max(1, int(getattr(cfg, "MODULE_VISUAL_VALIDATION_MIN_HITS", 1) or 1)),
            "image_size": getattr(cfg, "YOLO_IMGSZ", None),
            "half": bool(getattr(cfg, "YOLO_HALF", False)),
        },
        "product_class_mapping_hash": _product_class_mapping_hash(cfg),
    }
    fingerprint["fingerprint_hash"] = _fingerprint_hash(fingerprint)
    return fingerprint


def visual_fingerprint_current(record: dict[str, Any], cfg) -> bool:
    stored = record.get("visual_validation_fingerprint")
    if not isinstance(stored, dict):
        return False
    current = build_visual_validation_fingerprint(record, cfg)
    stored_hash = stored.get("fingerprint_hash")
    if stored_hash and stored_hash == current.get("fingerprint_hash"):
        return True
    return _normalized_fingerprint_without_hash(stored, record, cfg) == _normalized_fingerprint_without_hash(current, record, cfg)


def _select_visual_candidate_summaries(
    index: dict[str, Any],
    library: Path,
    cfg,
    product_filter: str | None,
    status_filter: str,
    role_filter: str | None,
    approved_only: bool,
    priority: str,
    force: bool,
) -> tuple[list[dict[str, Any]], int, int]:
    indexed = [
        (position, summary)
        for position, summary in enumerate(index.get("modules", []) or [])
        if isinstance(summary, dict)
    ]
    filtered: list[tuple[int, dict[str, Any]]] = []
    skipped_filter = 0
    skipped_current_filter = 0
    for position, summary in indexed:
        if product_filter and summary.get("product") != product_filter:
            skipped_filter += 1
            continue
        if role_filter and summary.get("role") != role_filter:
            skipped_filter += 1
            continue
        if approved_only and summary.get("quality_status") not in {QUALITY_APPROVED, QUALITY_NO_VISUAL_EVENTS}:
            skipped_filter += 1
            continue
        visual_status = _normalize_visual_status(summary.get("visual_validation_status"))
        if status_filter != VISUAL_ALL and visual_status != status_filter:
            if status_filter == VISUAL_NOT_RUN and not force and visual_fingerprint_current(summary, cfg):
                skipped_current_filter += 1
                continue
            skipped_filter += 1
            continue
        filtered.append((position, summary))

    if priority == "assembly_ready":
        filtered = _sort_by_assembly_priority(filtered, index, library, cfg)
    else:
        filtered.sort(key=lambda item: item[0])
    return [summary for _position, summary in filtered], skipped_filter, skipped_current_filter


def _sort_by_assembly_priority(
    filtered: list[tuple[int, dict[str, Any]]],
    index: dict[str, Any],
    library: Path,
    cfg,
) -> list[tuple[int, dict[str, Any]]]:
    priority_by_key: dict[str, tuple[int, int]] = {}
    try:
        from module_assembler import build_modular_assembly_jobs

        jobs = build_modular_assembly_jobs(index, library / "_visual_validation_priority", cfg)
    except Exception as exc:
        log.warning("Could not build assembly-ready visual priority; using index order: %s", exc)
        return sorted(filtered, key=lambda item: item[0])

    for job_position, job in enumerate(jobs):
        components = job.get("components") if isinstance(job, dict) else []
        for component_position, component in enumerate(components or []):
            if not isinstance(component, dict):
                continue
            priority = (job_position, component_position)
            for key in _record_identity_keys(component):
                priority_by_key.setdefault(key, priority)

    def sort_key(item: tuple[int, dict[str, Any]]) -> tuple[int, int, int]:
        position, summary = item
        priorities = [
            priority_by_key[key]
            for key in _record_identity_keys(summary)
            if key in priority_by_key
        ]
        if not priorities:
            return (1, position, 0)
        best_job, best_component = min(priorities)
        return (0, best_job, best_component * 100000 + position)

    return sorted(filtered, key=sort_key)


def _record_identity_keys(record: dict[str, Any]) -> set[str]:
    keys = set()
    for field in ("module_id", "file_path", "sidecar_path"):
        value = str(record.get(field) or "").strip()
        if value:
            keys.add(value.casefold())
            try:
                keys.add(str(Path(value).resolve()).casefold())
            except OSError:
                pass
    file_path = str(record.get("file_path") or "").strip()
    if file_path:
        keys.add(Path(file_path).stem.casefold())
    return keys


def _normalize_visual_status_filter(value: Any) -> str:
    status = str(value or VISUAL_NOT_RUN).strip().lower()
    if status not in VALID_VISUAL_STATUS_FILTERS:
        raise ValueError(f"module visual status must be one of: {', '.join(sorted(VALID_VISUAL_STATUS_FILTERS))}")
    return status


def _normalize_visual_status(value: Any) -> str:
    status = str(value or VISUAL_NOT_RUN).strip().lower()
    if status in {VISUAL_PASSED, VISUAL_FAILED, VISUAL_NOT_RUN}:
        return status
    return VISUAL_NOT_RUN


def _normalize_role_filter(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    role = str(value).strip().lower()
    if role not in ROLE_FOLDERS:
        raise ValueError(f"module visual role must be one of: {', '.join(ROLE_FOLDERS)}")
    return role


def _normalize_priority(value: Any) -> str:
    priority = str(value or "assembly_ready").strip().lower()
    if priority not in VALID_VISUAL_PRIORITIES:
        raise ValueError(f"module visual priority must be one of: {', '.join(sorted(VALID_VISUAL_PRIORITIES))}")
    return priority


def _product_class_mapping_hash(cfg) -> str:
    configured = getattr(cfg, "PRODUCT_CLASSES", {}) or {}
    if isinstance(configured, dict):
        mapping = {str(key): str(value) for key, value in sorted(configured.items(), key=lambda item: str(item[0]))}
    else:
        mapping = str(configured)
    payload = {
        "product_classes": mapping,
        "host_face_class": str(getattr(cfg, "HOST_FACE_CLASS", "")),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    import hashlib

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _fingerprint_hash(fingerprint: dict[str, Any]) -> str:
    import hashlib

    raw = json.dumps(_fingerprint_without_hash(fingerprint), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _fingerprint_without_hash(fingerprint: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(fingerprint)
    cleaned.pop("fingerprint_hash", None)
    return cleaned


def _fingerprint_path_identity(path: str | Path) -> dict[str, Any]:
    identity = _path_identity(path)
    identity["path"] = _resolved_casefold_path(path)
    return identity


def _resolved_casefold_path(path: str | Path) -> str:
    candidate = Path(path)
    try:
        return str(candidate.resolve(strict=False)).casefold()
    except OSError:
        return str(candidate.absolute()).casefold()


def _normalized_fingerprint_without_hash(fingerprint: dict[str, Any], record: dict[str, Any], cfg) -> dict[str, Any]:
    cleaned = deepcopy(_fingerprint_without_hash(fingerprint))
    _normalize_fingerprint_identity_path(cleaned, "module_media_identity", record.get("file_path"))
    _normalize_fingerprint_identity_path(cleaned, "yolo_weights_identity", getattr(cfg, "YOLO_WEIGHTS", ""))
    return cleaned


def _normalize_fingerprint_identity_path(fingerprint: dict[str, Any], key: str, canonical_path: Any) -> None:
    identity = fingerprint.get(key)
    if not isinstance(identity, dict):
        return
    identity["path"] = _resolved_casefold_path(str(canonical_path or identity.get("path") or ""))


def scan_module_visual_events(
    module_path: Path,
    product: str,
    cfg,
    model: Any | None = None,
) -> dict[str, Any]:
    """Scan a module MP4 and return product-only, module-relative events."""
    import cv2

    product = canonical_product(product) or str(product)
    model = model or _load_yolo_model(cfg)
    cap = cv2.VideoCapture(str(module_path))
    if not cap.isOpened():
        return {"events": [], "hits": 0, "confidence_max": 0.0, "reason": "video_open_failed"}

    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    if fps <= 0:
        fps = 30.0
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    sample_fps = max(0.1, float(getattr(cfg, "MODULE_VISUAL_VALIDATION_SAMPLE_FPS", 1.0) or 1.0))
    frame_step = max(1, int(round(fps / sample_fps)))

    samples = []
    frame_idx = 0
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break
        if frame_idx % frame_step == 0:
            samples.append({"frame": frame, "frame_idx": frame_idx, "time": frame_idx / fps})
        frame_idx += 1
        if total_frames > 0 and frame_idx >= total_frames:
            break
    cap.release()

    if not samples:
        return {"events": [], "hits": 0, "confidence_max": 0.0, "reason": "no_video_frames"}

    min_confidence = float(getattr(cfg, "MODULE_VISUAL_VALIDATION_MIN_CONFIDENCE", 0.55) or 0.55)
    results = _predict_visual_validation(
        model,
        [sample["frame"] for sample in samples],
        cfg,
        min_confidence,
        context=str(module_path),
    )

    detections = []
    confidence_max = 0.0
    for sample, result in zip(samples, results):
        boxes = getattr(result, "boxes", None)
        if boxes is None:
            continue
        for box in boxes:
            class_id = int(box.cls[0])
            confidence = float(box.conf[0])
            class_name = _class_name_for_detection(model, cfg, class_id, product)
            detected_product = canonical_product(class_name)
            if detected_product != product:
                continue
            xyxy = box.xyxy[0].tolist()
            confidence_max = max(confidence_max, confidence)
            detections.append(
                {
                    "time": round(float(sample["time"]), 3),
                    "frame": int(sample["frame_idx"]),
                    "class_id": class_id,
                    "class_name": class_name,
                    "product": product,
                    "confidence": round(confidence, 4),
                    "bbox": [round(float(value), 1) for value in xyxy],
                    "frame_w": frame_w,
                    "frame_h": frame_h,
                }
            )

    gap_threshold = max(1.5, 2.0 / sample_fps)
    events = _group_product_events(detections, product, gap_threshold=gap_threshold)
    return {
        "events": events,
        "hits": len(detections),
        "confidence_max": confidence_max,
        "reason": "matched_product" if detections else "no_matching_product_detection",
    }


def scan_source_video_window_visual_events(
    video_path: str | Path,
    product: str,
    start: float,
    end: float,
    cfg,
    model: Any | None = None,
) -> dict[str, Any]:
    """Scan a source VOD range and return product-only, module-relative events."""
    import cv2

    product = canonical_product(product) or str(product)
    start = max(0.0, float(start or 0.0))
    end = max(start, float(end or start))
    if end <= start:
        return {"status": VISUAL_NOT_RUN, "events": [], "hits": 0, "confidence_max": 0.0, "reason": "empty_source_window"}

    model = model or _load_yolo_model(cfg)
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return {"status": VISUAL_NOT_RUN, "events": [], "hits": 0, "confidence_max": 0.0, "reason": "video_open_failed"}

    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    if fps <= 0:
        fps = 30.0
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    sample_fps = max(0.1, float(getattr(cfg, "MODULE_VISUAL_VALIDATION_SAMPLE_FPS", 1.0) or 1.0))
    frame_step = max(1, int(round(fps / sample_fps)))

    start_frame = max(0, int(start * fps))
    end_frame = max(start_frame, int(end * fps + 0.999))
    if total_frames > 0:
        end_frame = min(total_frames, end_frame)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

    samples = []
    frame_idx = start_frame
    while cap.isOpened() and frame_idx < end_frame:
        ret, frame = cap.read()
        if not ret:
            break
        if (frame_idx - start_frame) % frame_step == 0:
            module_time = max(0.0, (frame_idx / fps) - start)
            samples.append({"frame": frame, "frame_idx": frame_idx, "time": module_time})
        frame_idx += 1
    cap.release()

    if not samples:
        return {"status": VISUAL_NOT_RUN, "events": [], "hits": 0, "confidence_max": 0.0, "reason": "no_video_frames"}

    min_confidence = float(getattr(cfg, "MODULE_VISUAL_VALIDATION_MIN_CONFIDENCE", 0.55) or 0.55)
    results = _predict_visual_validation(
        model,
        [sample["frame"] for sample in samples],
        cfg,
        min_confidence,
        context=f"{video_path}:{start:.3f}-{end:.3f}",
    )

    detections = []
    confidence_max = 0.0
    for sample, result in zip(samples, results):
        boxes = getattr(result, "boxes", None)
        if boxes is None:
            continue
        for box in boxes:
            class_id = int(box.cls[0])
            confidence = float(box.conf[0])
            class_name = _class_name_for_detection(model, cfg, class_id, product)
            detected_product = canonical_product(class_name)
            if detected_product != product:
                continue
            xyxy = box.xyxy[0].tolist()
            confidence_max = max(confidence_max, confidence)
            detections.append(
                {
                    "time": round(float(sample["time"]), 3),
                    "frame": int(sample["frame_idx"]),
                    "class_id": class_id,
                    "class_name": class_name,
                    "product": product,
                    "confidence": round(confidence, 4),
                    "bbox": [round(float(value), 1) for value in xyxy],
                    "frame_w": frame_w,
                    "frame_h": frame_h,
                }
            )

    gap_threshold = max(1.5, 2.0 / sample_fps)
    events = _group_product_events(detections, product, gap_threshold=gap_threshold)
    for event in events:
        event["source"] = "source_vod_visual_validation"
    return {
        "events": events,
        "hits": len(detections),
        "confidence_max": confidence_max,
        "reason": "source_vod_matched_product" if events else "source_vod_no_visual_events",
    }


def apply_source_vod_visual_result(
    record: dict[str, Any],
    scan: dict[str, Any] | None,
    cfg,
) -> dict[str, Any]:
    """Attach source-VOD pre-cut validation results using the bulk fingerprint schema."""
    fingerprint = build_visual_validation_fingerprint(record, cfg)
    record["visual_validation_fingerprint"] = fingerprint
    record["visual_validation_mode"] = "source_vod_pre_cut"

    if not isinstance(scan, dict):
        return _apply_not_run(record, "source_vod_not_run", fingerprint)
    if str(scan.get("status") or "").strip().lower() == VISUAL_NOT_RUN:
        return _apply_not_run(record, str(scan.get("reason") or "source_vod_not_run"), fingerprint)

    events = scan.get("events") if isinstance(scan.get("events"), list) else []
    hits = int(scan.get("hits") or 0)
    confidence_max = float(scan.get("confidence_max") or 0.0)
    min_hits = max(1, int(getattr(cfg, "MODULE_VISUAL_VALIDATION_MIN_HITS", 1) or 1))

    if events and hits >= min_hits:
        record["visual_validation_status"] = VISUAL_PASSED
        record["visual_validation_reason"] = str(scan.get("reason") or "source_vod_matched_product")
        _clear_previous_visual_mismatch(record, cfg)
    else:
        record["visual_validation_status"] = VISUAL_FAILED
        record["visual_validation_reason"] = "source_vod_no_visual_events"
        if record.get("quality_status") != QUALITY_BLOCKED and record.get("review_status") != QUALITY_BLOCKED:
            record["review_status"] = str(record.get("review_status") or REVIEW_PENDING) or REVIEW_PENDING
            record["quality_status"] = QUALITY_NO_VISUAL_EVENTS
            record["quality_reason"] = "source_vod_no_visual_events"
            record.pop("quality_score", None)
            record.update(module_quality_fields(record, cfg))

    record["visual_product_hits"] = hits
    record["visual_product_confidence_max"] = round(confidence_max, 4)
    record["visual_product_events"] = events
    return record


class _DeviceOverrideCfg:
    def __init__(self, cfg, device: str):
        self._cfg = cfg
        self.YOLO_DEVICE = device

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cfg, name)


def _load_yolo_model(cfg, device_override: str | None = None):
    from vision_scanner import _load_model

    if device_override is not None:
        return _load_model(_DeviceOverrideCfg(cfg, device_override))
    return _load_model(cfg)


def _predict_visual_validation(
    model: Any,
    frames: list[Any],
    cfg,
    min_confidence: float,
    context: str,
) -> list[Any]:
    """Run YOLO for module visual validation, falling back to CPU after CUDA failures."""
    requested_device = _visual_validation_device(cfg)
    device = requested_device
    half = _half_enabled_for_device(cfg, device)
    if _cuda_validation_disabled() and _is_cuda_device(device):
        device = "cpu"
        half = False
        model = _load_yolo_model(cfg, device_override="cpu")

    try:
        return _predict_visual_validation_on_device(model, frames, cfg, min_confidence, device=device, half=half)
    except Exception as exc:
        if not _is_cuda_device(device) or not _is_cuda_runtime_error(exc):
            raise

        first_cuda_failure = _disable_cuda_validation()
        if first_cuda_failure:
            log.warning(
                "YOLO visual validation CUDA failed for %s; retrying on CPU for this process: %s",
                context,
                exc,
            )
        _clear_torch_cuda_cache()
        cpu_model = _load_yolo_model(cfg, device_override="cpu")
        try:
            return _predict_visual_validation_on_device(
                cpu_model,
                frames,
                cfg,
                min_confidence,
                device="cpu",
                half=False,
            )
        except Exception as cpu_exc:
            raise RuntimeError(f"YOLO CUDA prediction failed and CPU fallback also failed: {cpu_exc}") from exc


def _predict_visual_validation_on_device(
    model: Any,
    frames: list[Any],
    cfg,
    min_confidence: float,
    device: str,
    half: bool,
) -> list[Any]:
    return model.predict(
        frames,
        conf=min_confidence,
        verbose=False,
        device=device,
        imgsz=getattr(cfg, "YOLO_IMGSZ", 640),
        half=half,
    )


def _visual_validation_device(cfg) -> str:
    device = getattr(cfg, "MODULE_VISUAL_VALIDATION_DEVICE", None)
    if device is None or str(device).strip() == "":
        device = getattr(cfg, "YOLO_DEVICE", "cpu")
    return str(device or "cpu").strip() or "cpu"


def _half_enabled_for_device(cfg, device: str) -> bool:
    return bool(getattr(cfg, "YOLO_HALF", False)) and _is_cuda_device(device)


def _is_cuda_device(device: Any) -> bool:
    normalized = str(device or "").strip().lower()
    return normalized not in {"", "cpu", "-1", "none"}


def _is_cuda_runtime_error(exc: Exception) -> bool:
    current: BaseException | None = exc
    while current is not None:
        message = str(current).lower()
        if "cuda" in message or "cudnn" in message:
            return True
        current = current.__cause__ or current.__context__
    return False


def _cuda_validation_disabled() -> bool:
    with _CUDA_VALIDATION_LOCK:
        return _CUDA_VALIDATION_DISABLED


def _disable_cuda_validation() -> bool:
    global _CUDA_VALIDATION_DISABLED
    with _CUDA_VALIDATION_LOCK:
        was_disabled = _CUDA_VALIDATION_DISABLED
        _CUDA_VALIDATION_DISABLED = True
    return not was_disabled


def _clear_torch_cuda_cache() -> None:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _class_name_for_detection(model: Any, cfg, class_id: int, target_product: str) -> str:
    candidates = []
    names = getattr(model, "names", None)
    if isinstance(names, dict) and class_id in names:
        candidates.append(str(names[class_id]))
    elif isinstance(names, dict) and str(class_id) in names:
        candidates.append(str(names[str(class_id)]))
    elif isinstance(names, (list, tuple)) and 0 <= class_id < len(names):
        candidates.append(str(names[class_id]))

    configured = getattr(cfg, "PRODUCT_CLASSES", {}) or {}
    if isinstance(configured, dict) and class_id in configured:
        candidates.append(str(configured[class_id]))

    for candidate in candidates:
        if canonical_product(candidate) == target_product:
            return candidate
    return candidates[0] if candidates else f"class_{class_id}"


def _group_product_events(detections: list[dict[str, Any]], product: str, gap_threshold: float) -> list[dict[str, Any]]:
    events = []
    current = None
    for detection in sorted(detections, key=lambda item: item.get("time", 0.0)):
        if current is None or float(detection["time"]) - float(current["end_time"]) > gap_threshold:
            if current is not None:
                events.append(_finalize_event(current))
            current = _new_event(detection, product)
        else:
            current["end_time"] = detection["time"]
            current["end_bbox"] = detection["bbox"]
            current["detection_count"] += 1
            if detection["confidence"] >= current["best_confidence"]:
                current["best_confidence"] = detection["confidence"]
                current["best_bbox"] = detection["bbox"]
            current["track"].append(_track_sample(detection))
    if current is not None:
        events.append(_finalize_event(current))
    return events


def _new_event(detection: dict[str, Any], product: str) -> dict[str, Any]:
    return {
        "source": "module_visual_validation",
        "product": product,
        "class_id": detection.get("class_id"),
        "class_name": detection.get("class_name"),
        "start_time": detection.get("time"),
        "end_time": detection.get("time"),
        "relative_start": detection.get("time"),
        "relative_end": detection.get("time"),
        "best_confidence": detection.get("confidence", 0.0),
        "start_bbox": detection.get("bbox"),
        "end_bbox": detection.get("bbox"),
        "best_bbox": detection.get("bbox"),
        "frame_w": detection.get("frame_w", 0),
        "frame_h": detection.get("frame_h", 0),
        "detection_count": 1,
        "track": [_track_sample(detection)],
        "relative_track": [_relative_track_sample(detection, detection.get("time", 0.0))],
    }


def _finalize_event(event: dict[str, Any]) -> dict[str, Any]:
    start = float(event.get("start_time") or 0.0)
    end = float(event.get("end_time") or start)
    event["start_time"] = round(start, 3)
    event["end_time"] = round(max(start, end), 3)
    event["relative_start"] = event["start_time"]
    event["relative_end"] = event["end_time"]
    event["duration"] = round(event["relative_end"] - event["relative_start"], 3)
    event["best_confidence"] = round(float(event.get("best_confidence") or 0.0), 4)
    event["relative_track"] = [
        _relative_track_sample(sample, sample.get("time", sample.get("relative_time", 0.0)))
        for sample in event.get("track", [])
    ]
    return event


def _track_sample(detection: dict[str, Any]) -> dict[str, Any]:
    return {
        "time": round(float(detection.get("time") or 0.0), 3),
        "bbox": detection.get("bbox"),
        "confidence": round(float(detection.get("confidence") or 0.0), 4),
    }


def _relative_track_sample(sample: dict[str, Any], time_value: Any) -> dict[str, Any]:
    return {
        "relative_time": round(float(time_value or 0.0), 3),
        "bbox": sample.get("bbox"),
        "confidence": round(float(sample.get("confidence") or 0.0), 4),
    }


def _apply_not_run(record: dict[str, Any], reason: str, fingerprint: dict[str, Any] | None = None) -> dict[str, Any]:
    record["visual_validation_status"] = VISUAL_NOT_RUN
    record["visual_product_hits"] = int(record.get("visual_product_hits") or 0)
    record["visual_product_confidence_max"] = float(record.get("visual_product_confidence_max") or 0.0)
    record["visual_validation_reason"] = reason
    record.setdefault("visual_product_events", [])
    if fingerprint is not None:
        record["visual_validation_fingerprint"] = fingerprint
    return record


def _demote_visual_mismatch(record: dict[str, Any], cfg) -> None:
    if record.get("quality_status") == QUALITY_BLOCKED or record.get("review_status") == QUALITY_BLOCKED:
        return
    if record.get("review_status") == QUALITY_APPROVED:
        return
    record["review_status"] = REVIEW_PENDING
    record["quality_status"] = QUALITY_NEEDS_REVIEW
    record["quality_reason"] = "visual_product_mismatch"
    record.pop("quality_score", None)
    record.update(module_quality_fields(record, cfg))


def _clear_previous_visual_mismatch(record: dict[str, Any], cfg) -> None:
    if record.get("quality_reason") in {"visual_product_mismatch", "source_vod_no_visual_events"} and record.get("review_status") != QUALITY_APPROVED:
        record.pop("quality_status", None)
        record.pop("quality_reason", None)
        record.pop("quality_score", None)
        record.update(module_quality_fields(record, cfg))


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
