from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote


log = logging.getLogger("proya.product_broll")

PRODUCT_FOLDERS = ("cleanser", "toner", "serum", "eye_cream", "mask", "skin_cream")
PRODUCT_LABELS = {
    "cleanser": "Cleanser",
    "toner": "Toner",
    "serum": "Serum",
    "eye_cream": "Eye Cream",
    "mask": "Mask",
    "skin_cream": "Skin Cream",
}
PRODUCT_ALIASES = {
    "cleanser": (
        "cleanser",
        "face wash",
        "facial wash",
        "sabun muka",
        "sabun wajah",
        "pembersih wajah",
        "cleansernya",
    ),
    "toner": ("toner", "tonernya"),
    "serum": ("serum", "vitamin c serum", "serumnya"),
    "eye_cream": ("eye cream", "eyecream", "krim mata", "cream mata", "mata panda"),
    "mask": ("mask", "masker", "sheet mask", "sheetmask"),
    "skin_cream": (
        "skin cream",
        "cream",
        "krim",
        "moisturizer",
        "moisturiser",
        "pelembap",
        "night cream",
        "day cream",
    ),
}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
DEFAULT_CROSSFADE_SECONDS = 0.3


@dataclass(frozen=True)
class BrollClip:
    path: str
    duration: float
    width: int = 0
    height: int = 0
    fps: float | None = None


@dataclass(frozen=True)
class BrollTransition:
    offset: float
    duration: float


@dataclass(frozen=True)
class BrollPlan:
    product_key: str
    product_label: str
    folder: str
    clips: tuple[BrollClip, ...]
    transitions: tuple[BrollTransition, ...]
    target_duration: float
    crossfade: float


ProbeFn = Callable[[str], dict[str, Any] | None]


def product_broll_root(cfg) -> Path:
    root = Path(str(getattr(cfg, "PRODUCT_BROLL_DIR", "assets/product_broll") or "assets/product_broll"))
    if not root.is_absolute():
        root = Path.cwd() / root
    return root.resolve()


def product_broll_folder(cfg, product_key: str) -> Path:
    return product_broll_root(cfg) / str(product_key or "")


def supported_video_exts(cfg) -> set[str]:
    raw = getattr(cfg, "PRODUCT_BROLL_VIDEO_EXTS", VIDEO_EXTS)
    try:
        return {str(ext).lower() for ext in raw}
    except TypeError:
        return set(VIDEO_EXTS)


def canonical_product(value: Any) -> str | None:
    text = _normalize_text(value)
    if not text or text in {"general", "unknown", "tidak jelas", "produk", "product"}:
        return None
    direct = text.replace(" ", "_")
    if direct in PRODUCT_FOLDERS:
        return direct
    for product, aliases in PRODUCT_ALIASES.items():
        if text == product or direct == product:
            return product
        for alias in aliases:
            alias_norm = _normalize_text(alias)
            if alias_norm and alias_norm in text:
                return product
    return None


def resolve_product_events_key(product_events: list[dict[str, Any]] | None) -> str | None:
    scores: dict[str, float] = {}
    for event in product_events or []:
        if not isinstance(event, dict):
            continue
        product = canonical_product(
            event.get("class_name")
            or event.get("product")
            or event.get("label")
            or event.get("name")
        )
        if not product:
            continue
        scores[product] = scores.get(product, 0.0) + _event_product_score(event)
    if not scores:
        return None
    return max(scores.items(), key=lambda item: item[1])[0]


def resolve_moment_product_key(
    moment: dict[str, Any],
    product_events: list[dict[str, Any]] | None = None,
) -> str | None:
    product = canonical_product(moment.get("product") if isinstance(moment, dict) else "")
    if product:
        return product
    if not isinstance(moment, dict):
        return resolve_product_events_key(product_events)
    search_parts = [
        moment.get("hook", ""),
        moment.get("reason", ""),
        moment.get("selected_text", ""),
    ]
    hook_overlay = moment.get("hook_overlay")
    if isinstance(hook_overlay, dict):
        search_parts.extend(str(hook_overlay.get(key, "")) for key in ("headline", "subtext", "cta"))
    for segment in moment.get("segments", []) or []:
        if isinstance(segment, dict):
            search_parts.append(str(segment.get("text", "") or segment.get("description", "")))
        else:
            search_parts.append(str(segment))
    product = canonical_product(" ".join(str(part or "") for part in search_parts))
    if product:
        return product
    product = canonical_product(
        moment.get("_detected_product_key")
        or moment.get("detected_product_key")
        or moment.get("_detected_product")
        or moment.get("detected_product")
    )
    if product:
        return product
    return resolve_product_events_key(product_events)


def list_product_broll_files(cfg, product_key: str) -> list[Path]:
    folder = product_broll_folder(cfg, product_key)
    if not folder.exists() or not folder.is_dir():
        return []
    exts = supported_video_exts(cfg)
    try:
        return sorted(path for path in folder.iterdir() if path.is_file() and path.suffix.lower() in exts)
    except OSError as exc:
        log.warning("Could not read product B-roll folder %s: %s", folder, exc)
        return []


def product_broll_preview_sources(cfg) -> dict[str, Any]:
    root = product_broll_root(cfg)
    products = []
    for product_key in PRODUCT_FOLDERS:
        folder = product_broll_folder(cfg, product_key)
        files = list_product_broll_files(cfg, product_key)
        preview = _artifact_ref(files[0]) if files else None
        products.append({
            "product_key": product_key,
            "label": PRODUCT_LABELS.get(product_key, product_key.replace("_", " ").title()),
            "folder": str(folder),
            "exists": folder.exists() and folder.is_dir(),
            "video_count": len(files),
            "preview": preview,
        })
    return {
        "root": str(root),
        "exists": root.exists() and root.is_dir(),
        "products": products,
    }


def product_broll_asset_fingerprint(cfg) -> str:
    root = product_broll_root(cfg)
    entries: list[dict[str, Any]] = []
    for product_key in PRODUCT_FOLDERS:
        folder = product_broll_folder(cfg, product_key)
        for path in list_product_broll_files(cfg, product_key):
            try:
                stat = path.stat()
            except OSError:
                continue
            try:
                rel = str(path.resolve().relative_to(root))
            except ValueError:
                rel = str(path.resolve())
            entries.append({
                "product": product_key,
                "path": rel.replace("\\", "/"),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            })
        if not folder.exists():
            entries.append({"product": product_key, "missing": True})
    raw = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_broll_plan(
    moment: dict[str, Any],
    cfg,
    target_duration: float,
    rng: random.Random | None = None,
    probe_fn: ProbeFn | None = None,
    product_events: list[dict[str, Any]] | None = None,
) -> tuple[BrollPlan | None, str]:
    try:
        target = float(target_duration)
    except (TypeError, ValueError):
        target = 0.0
    if target <= 0.1:
        return None, "clip duration is too short for product B-roll"

    product_key = resolve_moment_product_key(moment, product_events=product_events)
    if not product_key:
        return None, "moment has no supported product for product B-roll"

    folder = product_broll_folder(cfg, product_key)
    if not folder.exists() or not folder.is_dir():
        return None, f"product B-roll folder missing for {product_key}: {folder}"

    files = list_product_broll_files(cfg, product_key)
    if not files:
        return None, f"product B-roll folder has no supported videos for {product_key}: {folder}"

    probe = probe_fn or probe_video
    clips: list[BrollClip] = []
    for path in files:
        info = probe(str(path))
        if not info:
            continue
        try:
            duration = float(info.get("duration") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        if duration <= 0.1:
            continue
        clips.append(BrollClip(
            path=str(path),
            duration=duration,
            width=int(info.get("width") or 0),
            height=int(info.get("height") or 0),
            fps=info.get("fps"),
        ))

    if not clips:
        return None, f"product B-roll folder has no valid video files for {product_key}: {folder}"

    picker = rng or random.Random()
    crossfade = _configured_crossfade(cfg)
    selected: list[BrollClip] = []
    transitions: list[BrollTransition] = []
    timeline_duration = 0.0
    previous: BrollClip | None = None
    max_segments = max(1, int(target / max(min(clip.duration for clip in clips), 0.1)) + len(clips) + 12)

    while timeline_duration < target and len(selected) < max_segments:
        clip = _pick_next_clip(clips, previous, picker)
        selected.append(clip)
        if len(selected) == 1:
            timeline_duration = clip.duration
        else:
            transition_duration = _transition_duration(crossfade, timeline_duration, clip.duration)
            offset = max(0.0, timeline_duration - transition_duration)
            transitions.append(BrollTransition(offset=round(offset, 6), duration=round(transition_duration, 6)))
            timeline_duration += max(0.0, clip.duration - transition_duration)
        previous = clip

    if not selected or timeline_duration < target:
        return None, f"valid product B-roll clips could not cover {target:.2f}s for {product_key}"

    return (
        BrollPlan(
            product_key=product_key,
            product_label=PRODUCT_LABELS.get(product_key, product_key.replace("_", " ").title()),
            folder=str(folder),
            clips=tuple(selected),
            transitions=tuple(transitions),
            target_duration=target,
            crossfade=crossfade,
        ),
        "",
    )


def plan_manifest(plan: BrollPlan) -> dict[str, Any]:
    return {
        "product_key": plan.product_key,
        "product_label": plan.product_label,
        "folder": plan.folder,
        "clip_count": len(plan.clips),
        "clips": [str(Path(clip.path).name) for clip in plan.clips],
        "crossfade": plan.crossfade,
    }


def probe_video(path: str) -> dict[str, Any] | None:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_streams",
                "-show_format",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        payload = json.loads(result.stdout or "{}")
        streams = payload.get("streams") or []
        video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
        if not video_stream:
            return None
        duration = (
            payload.get("format", {}).get("duration")
            or video_stream.get("duration")
            or 0.0
        )
        return {
            "width": int(video_stream.get("width") or 0),
            "height": int(video_stream.get("height") or 0),
            "duration": float(duration or 0.0),
            "fps": _parse_fps(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")),
        }
    except Exception as exc:
        log.debug("Could not probe product B-roll video %s: %s", path, exc)
        return None


def _configured_crossfade(cfg) -> float:
    try:
        raw = float(getattr(cfg, "PRODUCT_BROLL_CROSSFADE_SECONDS", DEFAULT_CROSSFADE_SECONDS) or 0.0)
    except (TypeError, ValueError):
        raw = DEFAULT_CROSSFADE_SECONDS
    return max(0.0, min(raw, 2.0))


def _pick_next_clip(clips: list[BrollClip], previous: BrollClip | None, rng: random.Random) -> BrollClip:
    if previous is not None and len(clips) > 1:
        choices = [clip for clip in clips if clip.path != previous.path]
        if choices:
            return rng.choice(choices)
    return rng.choice(clips)


def _transition_duration(crossfade: float, current_duration: float, next_duration: float) -> float:
    if crossfade <= 0.0:
        return 0.0
    max_transition = min(crossfade, max(0.0, current_duration - 0.05), max(0.0, next_duration - 0.05))
    return max(0.0, max_transition)


def _artifact_ref(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "url": f"/api/artifacts?path={quote(str(path), safe='')}",
        "kind": "video",
        "exists": path.exists() and path.is_file(),
    }


def _normalize_text(value: Any) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"[_-]+", " ", text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _event_product_score(event: dict[str, Any]) -> float:
    duration = _event_duration(event)
    confidence = _safe_float(
        event.get("best_confidence")
        or event.get("confidence")
        or event.get("score"),
        1.0,
    )
    detections = _safe_float(event.get("detection_count"), 0.0)
    if detections <= 0.0 and isinstance(event.get("track"), list):
        detections = float(len(event.get("track") or []))
    return max(duration, 0.1) * max(confidence, 0.1) + max(detections, 1.0) * 0.01


def _event_duration(event: dict[str, Any]) -> float:
    if "relative_start" in event and "relative_end" in event:
        duration = _safe_float(event.get("relative_end"), 0.0) - _safe_float(event.get("relative_start"), 0.0)
        if duration > 0.0:
            return duration
    duration = _safe_float(event.get("duration"), 0.0)
    if duration > 0.0:
        return duration
    start = _safe_float(event.get("start_time"), 0.0)
    end = _safe_float(event.get("end_time"), start)
    return max(0.0, end - start)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_fps(value: Any) -> float | None:
    text = str(value or "")
    if not text:
        return None
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            denominator_value = float(denominator)
            if denominator_value == 0:
                return None
            return float(numerator) / denominator_value
        parsed = float(text)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None
