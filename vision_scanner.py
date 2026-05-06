# =============================================================================
#  vision_scanner.py — YOLOv8 product detection + ROI scanning
#  Detects when PROYA products are visible and generates zoom event data.
#  Also includes the one-time training helper.
# =============================================================================

import json
import logging
from pathlib import Path

import cv2

log = logging.getLogger("proya.vision")
VISION_CACHE_SCHEMA_VERSION = 3
_MODEL_CACHE = {}


# ── Training ──────────────────────────────────────────────────────────────────

def train_model(cfg):
    """
    Train a custom YOLOv8 model on your PROYA product dataset.
    Run this once. After training, set YOLO_WEIGHTS in config.py to the output path.

    Dataset structure expected:
      dataset/
        proya.yaml
        images/train/   (your labeled images)
        images/val/
        labels/train/   (YOLO format .txt files from Roboflow)
        labels/val/
    """
    try:
        from ultralytics import YOLO
    except ImportError:
        raise RuntimeError("ultralytics not installed. Run: pip install ultralytics")

    log.info("Starting YOLO training for PROYA product detection...")
    log.info(f"Base model: {cfg.YOLO_PRETRAIN}")
    log.info(f"Dataset: {cfg.DATASET_YAML}")

    model = YOLO(cfg.YOLO_PRETRAIN)
    results = model.train(
        data=cfg.DATASET_YAML,
        epochs=120,
        imgsz=640,
        batch=8,            # lower if you run out of VRAM
        device=cfg.YOLO_DEVICE,
        patience=20,        # early stopping
        save=True,
        project="models",
        name="proya_detector",
        augment=True,       # data augmentation helps with small datasets
        # Augmentation settings tuned for product-in-hand detection:
        flipud=0.0,         # don't flip vertically (products have a top)
        fliplr=0.5,
        hsv_h=0.02,
        hsv_s=0.5,
        hsv_v=0.4,
        scale=0.5,
        translate=0.2,
        degrees=10.0,
    )

    best_path = Path("models/proya_detector/weights/best.pt")
    if best_path.exists():
        log.info(f"✓ Training complete! Best weights saved to: {best_path}")
        log.info(f"  Update YOLO_WEIGHTS in config.py to: {best_path}")
    return results


# ── Inference ─────────────────────────────────────────────────────────────────

def _load_model(cfg):
    try:
        from ultralytics import YOLO
    except ImportError:
        raise RuntimeError("ultralytics not installed. Run: pip install ultralytics")

    if not Path(cfg.YOLO_WEIGHTS).exists():
        raise FileNotFoundError(
            f"YOLO weights not found at {cfg.YOLO_WEIGHTS}. "
            "Run the training step first or check your config.py YOLO_WEIGHTS path."
        )

    cache_key = (
        str(Path(cfg.YOLO_WEIGHTS).resolve()).casefold(),
        str(getattr(cfg, "YOLO_DEVICE", "cpu")),
    )
    model = _MODEL_CACHE.get(cache_key)
    if model is not None:
        return model

    model = YOLO(cfg.YOLO_WEIGHTS)
    _MODEL_CACHE.clear()
    _MODEL_CACHE[cache_key] = model
    log.info(f"Loaded YOLO model: {cfg.YOLO_WEIGHTS}")
    log.info(f"Classes: {model.names}")
    return model


def build_scan_ranges_from_moments(moments: list, cfg) -> list:
    """Build merged scan windows around candidate moments."""
    if not getattr(cfg, "YOLO_SCAN_ONLY_MOMENTS", False):
        return []

    pad_before = max(0.0, float(getattr(cfg, "YOLO_SCAN_PAD_BEFORE", 0.0)))
    pad_after = max(0.0, float(getattr(cfg, "YOLO_SCAN_PAD_AFTER", 0.0)))
    merge_gap = max(0.0, float(getattr(cfg, "YOLO_SCAN_RANGE_MERGE_GAP", 0.0)))

    ranges = []
    for moment in moments or []:
        if not isinstance(moment, dict):
            continue
        try:
            start = float(moment.get("start", 0.0))
            end = float(moment.get("end", start))
        except (TypeError, ValueError):
            continue
        if end < start:
            start, end = end, start
        ranges.append((max(0.0, start - pad_before), max(0.0, end + pad_after)))

    return _normalize_scan_ranges(ranges, merge_gap=merge_gap)


def _vision_cache_meta_path(detections_path: Path) -> Path:
    return detections_path.with_name(f"{detections_path.stem}.meta.json")


def _path_identity(path: str | Path) -> dict:
    candidate = Path(path)
    try:
        resolved = candidate.resolve()
        stat = resolved.stat()
        return {
            "path": str(resolved).casefold(),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    except OSError:
        return {
            "path": str(candidate).casefold(),
            "size": None,
            "mtime_ns": None,
        }


def _vision_cache_fingerprint(video_path: str, cfg, scan_ranges: list | None = None) -> dict:
    normalized_scan_ranges = [
        [round(start, 3), round(end, 3)]
        for start, end in _normalize_scan_ranges(
            scan_ranges or [],
            merge_gap=max(0.0, float(getattr(cfg, "YOLO_SCAN_RANGE_MERGE_GAP", 0.0))),
        )
    ]
    return {
        "schema_version": VISION_CACHE_SCHEMA_VERSION,
        "video": _path_identity(video_path),
        "weights": _path_identity(getattr(cfg, "YOLO_WEIGHTS", "")),
        "yolo": {
            "confidence": float(getattr(cfg, "YOLO_CONF_THRESHOLD", 0.55)),
            "frame_skip": int(getattr(cfg, "YOLO_FRAME_SKIP", 1)),
            "roi": dict(getattr(cfg, "ROI", {}) or {}),
            "imgsz": int(getattr(cfg, "YOLO_IMGSZ", 640)),
            "half": bool(getattr(cfg, "YOLO_HALF", False)),
            "device": str(getattr(cfg, "YOLO_DEVICE", "cpu")),
            "scan_only_moments": bool(getattr(cfg, "YOLO_SCAN_ONLY_MOMENTS", False)),
        },
        "scan_ranges": normalized_scan_ranges,
    }


def _load_vision_cache_meta(detections_path: Path) -> dict | None:
    meta_path = _vision_cache_meta_path(detections_path)
    if not meta_path.exists():
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        return meta if isinstance(meta, dict) else None
    except Exception as exc:
        log.warning(f"Ignoring unreadable product detection metadata {meta_path}: {exc}")
        return None


def _vision_cache_fingerprint_matches(
    detections_path: Path,
    video_path: str,
    cfg,
    scan_ranges: list | None = None,
) -> bool:
    meta = _load_vision_cache_meta(detections_path)
    if not meta:
        return False
    return meta.get("fingerprint") == _vision_cache_fingerprint(video_path, cfg, scan_ranges)


def _write_vision_cache_meta(
    detections_path: Path,
    video_path: str,
    cfg,
    scan_ranges: list | None = None,
) -> None:
    meta_path = _vision_cache_meta_path(detections_path)
    payload = {"fingerprint": _vision_cache_fingerprint(video_path, cfg, scan_ranges)}
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def scan_video_for_products(video_path: str, working_dir: str, cfg, scan_ranges: list | None = None) -> list:
    """
    Scan the video for product appearances inside the configured ROI.
    Returns a list of detection events: [{time, class_id, class_name, bbox, confidence}]
    
    Saves to cache so re-runs skip this slow step.
    """
    detections_path = Path(working_dir) / "product_detections.json"

    if detections_path.exists():
        log.info(f"Loading cached product detections from {detections_path}")
        try:
            with open(detections_path, "r", encoding="utf-8") as f:
                cached_events = json.load(f)
        except Exception as exc:
            log.warning(f"Ignoring unreadable product detection cache {detections_path}: {exc}")
            cached_events = None
        if (
            _is_valid_cached_events(cached_events)
            and _vision_cache_fingerprint_matches(detections_path, video_path, cfg, scan_ranges)
        ):
            return cached_events
        log.info("Cached product detections are outdated or fingerprint changed; rebuilding vision scan")

    frame_skip = max(1, int(getattr(cfg, "YOLO_FRAME_SKIP", 1)))
    log.info(f"Scanning video for PROYA products: {video_path}")
    log.info(f"ROI: {cfg.ROI} | Frame skip: {frame_skip} | Conf: {cfg.YOLO_CONF_THRESHOLD}")

    model = _load_model(cfg)
    cap = cv2.VideoCapture(video_path)

    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0:
        fps = 30.0

    # Convert ROI fractions to pixel coords
    roi_x1 = int(cfg.ROI["x1"] * frame_w)
    roi_y1 = int(cfg.ROI["y1"] * frame_h)
    roi_x2 = int(cfg.ROI["x2"] * frame_w)
    roi_y2 = int(cfg.ROI["y2"] * frame_h)

    log.info(f"Video: {frame_w}x{frame_h} @ {fps:.1f}fps | {total_frames} frames")
    log.info(f"ROI pixels: ({roi_x1},{roi_y1}) → ({roi_x2},{roi_y2})")

    total_duration = (total_frames / fps) if total_frames > 0 else 0.0
    normalized_ranges = _normalize_scan_ranges(
        scan_ranges or [],
        merge_gap=max(0.0, float(getattr(cfg, "YOLO_SCAN_RANGE_MERGE_GAP", 0.0))),
        max_end=total_duration if total_duration > 0 else None,
    )
    if normalized_ranges:
        covered_duration = sum(end - start for start, end in normalized_ranges)
        pct = (covered_duration / total_duration * 100.0) if total_duration > 0 else 0.0
        log.info(
            f"YOLO range scan enabled: {len(normalized_ranges)} window(s) | "
            f"covering {covered_duration:.1f}s of {total_duration:.1f}s ({pct:.1f}%)"
        )
    else:
        normalized_ranges = [(0.0, total_duration)] if total_duration > 0 else []
        log.info("YOLO full-video scan enabled")

    detections = []
    sampled_frames = []
    scanned = 0
    total_target_frames = sum(
        max(0, int(end * fps + 0.999) - int(start * fps))
        for start, end in normalized_ranges
    )

    for range_idx, (range_start, range_end) in enumerate(normalized_ranges, start=1):
        start_frame = max(0, min(total_frames - 1, int(range_start * fps))) if total_frames > 0 else 0
        end_frame = max(start_frame + 1, min(total_frames, int(range_end * fps + 0.999))) if total_frames > 0 else 1

        if range_idx > 1 or start_frame > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
        frame_idx = start_frame

        log.info(
            f"  Scan window {range_idx}/{len(normalized_ranges)} | "
            f"t={range_start:.1f}s-{range_end:.1f}s | frames {start_frame}-{max(start_frame, end_frame - 1)}"
        )

        while cap.isOpened() and frame_idx < end_frame:
            ret, frame = cap.read()
            if not ret:
                break

            if frame_idx % frame_skip == 0:
                timestamp = frame_idx / fps
                roi_frame = frame[roi_y1:roi_y2, roi_x1:roi_x2]
                sampled_frames.append({
                    "frame": roi_frame,
                    "timestamp": timestamp,
                    "frame_idx": frame_idx,
                })

                scanned += 1
                if scanned % 500 == 0:
                    progress_frames = max(1, min(total_target_frames, frame_idx + 1))
                    pct = (progress_frames / max(1, total_target_frames)) * 100.0
                    log.info(f"  Sampled {scanned} frames ({pct:.1f}%)")

            frame_idx += 1

    cap.release()

    if sampled_frames:
        results = model.predict(
            [sample["frame"] for sample in sampled_frames],
            conf=cfg.YOLO_CONF_THRESHOLD,
            verbose=False,
            device=getattr(cfg, "YOLO_DEVICE", "cpu"),
            imgsz=getattr(cfg, "YOLO_IMGSZ", 640),
            half=getattr(cfg, "YOLO_HALF", False),
        )
        for sample, result in zip(sampled_frames, results):
            if result.boxes is None:
                continue
            for box in result.boxes:
                class_id = int(box.cls[0])
                conf = float(box.conf[0])
                xyxy = box.xyxy[0].tolist()

                full_bbox = [
                    xyxy[0] + roi_x1,
                    xyxy[1] + roi_y1,
                    xyxy[2] + roi_x1,
                    xyxy[3] + roi_y1,
                ]

                detections.append({
                    "time": round(sample["timestamp"], 3),
                    "frame": sample["frame_idx"],
                    "class_id": class_id,
                    "class_name": cfg.PRODUCT_CLASSES.get(class_id, f"class_{class_id}"),
                    "confidence": round(conf, 3),
                    "bbox": [round(v, 1) for v in full_bbox],
                    "frame_w": frame_w,
                    "frame_h": frame_h,
                })
    log.info(f"Scan complete: {len(detections)} product detections across {scanned} frames")

    # Group into events (consecutive detections = one event)
    events = _group_into_events(detections, gap_threshold=1.5)
    log.info(f"Grouped into {len(events)} product events")

    Path(working_dir).mkdir(parents=True, exist_ok=True)
    with open(detections_path, "w") as f:
        json.dump(events, f, indent=2)
    _write_vision_cache_meta(detections_path, video_path, cfg, scan_ranges)

    return events


def _normalize_scan_ranges(ranges: list, merge_gap: float = 0.0, max_end: float | None = None) -> list:
    normalized = []
    for item in ranges or []:
        if not item or len(item) < 2:
            continue
        try:
            start = float(item[0])
            end = float(item[1])
        except (TypeError, ValueError):
            continue
        if end < start:
            start, end = end, start
        start = max(0.0, start)
        end = max(start, end)
        if max_end is not None:
            start = min(start, max_end)
            end = min(end, max_end)
        normalized.append((start, end))

    if not normalized:
        return []

    normalized.sort(key=lambda pair: pair[0])
    merged = [normalized[0]]
    for start, end in normalized[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end + merge_gap:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _group_into_events(detections: list, gap_threshold: float = 1.5) -> list:
    """
    Merge individual frame detections into continuous product-hold events.
    gap_threshold: seconds gap allowed before starting a new event.
    """
    if not detections:
        return []

    sorted_dets = sorted(detections, key=lambda d: d["time"])
    events = []
    current_event = None

    for det in sorted_dets:
        if current_event is None:
            current_event = _new_event(det)
        elif (
            det["class_id"] == current_event["class_id"]
            and det["time"] - current_event["end_time"] <= gap_threshold
        ):
            # Extend current event
            current_event["end_time"] = det["time"]
            current_event["end_bbox"] = det["bbox"]
            current_event["detections"].append(det)
            # Track best bbox (highest confidence)
            if det["confidence"] > current_event["best_confidence"]:
                current_event["best_confidence"] = det["confidence"]
                current_event["best_bbox"] = det["bbox"]
        else:
            events.append(_finalize_event(current_event))
            current_event = _new_event(det)

    if current_event:
        events.append(_finalize_event(current_event))

    return events


def _new_event(det: dict) -> dict:
    return {
        "class_id": det["class_id"],
        "class_name": det["class_name"],
        "start_time": det["time"],
        "end_time": det["time"],
        "best_confidence": det["confidence"],
        "start_bbox": det["bbox"],
        "end_bbox": det["bbox"],
        "best_bbox": det["bbox"],
        "frame_w": det["frame_w"],
        "frame_h": det["frame_h"],
        "detections": [det],
    }


def _finalize_event(event: dict) -> dict:
    duration = event["end_time"] - event["start_time"]
    track = [
        {
            "time": round(det["time"], 3),
            "bbox": det["bbox"],
            "confidence": round(det["confidence"], 3),
        }
        for det in event["detections"]
    ]
    return {
        "_schema_version": VISION_CACHE_SCHEMA_VERSION,
        "class_id": event["class_id"],
        "class_name": event["class_name"],
        "start_time": round(event["start_time"], 3),
        "end_time": round(event["end_time"], 3),
        "duration": round(duration, 3),
        "best_confidence": round(event["best_confidence"], 3),
        "start_bbox": event["start_bbox"],
        "end_bbox": event["end_bbox"],
        "best_bbox": event["best_bbox"],
        "frame_w": event["frame_w"],
        "frame_h": event["frame_h"],
        "detection_count": len(event["detections"]),
        "track": track,
    }


def get_events_for_clip(all_events: list, clip_start: float, clip_end: float) -> list:
    """Filter product events that fall within a clip's time range."""
    return [
        {
            **e,
            "relative_start": round(e["start_time"] - clip_start, 3),
            "relative_end": round(e["end_time"] - clip_start, 3),
            "relative_track": [
                {
                    **sample,
                    "relative_time": round(sample["time"] - clip_start, 3),
                }
                for sample in e.get("track", [])
                if clip_start <= sample.get("time", clip_start - 1) <= clip_end
            ],
        }
        for e in all_events
        if e["end_time"] >= clip_start and e["start_time"] <= clip_end
    ]


def _is_valid_cached_events(cached_events: object) -> bool:
    if not isinstance(cached_events, list):
        return False
    if not cached_events:
        return True
    first = cached_events[0]
    return (
        isinstance(first, dict)
        and first.get("_schema_version") == VISION_CACHE_SCHEMA_VERSION
        and "track" in first
        and "start_bbox" in first
        and "end_bbox" in first
    )
