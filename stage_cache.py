from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from utils import _path_identity


STAGE_CONFIG_KEYS = {
    "transcribe": [
        "WHISPER_MODEL_SIZE",
        "WHISPER_LANGUAGE",
        "WHISPER_BEAM_SIZE",
        "WHISPER_BEST_OF",
        "WORD_ALIGNMENT_BACKEND",
        "WHISPERX_ALIGN_MODEL",
        "WHISPERX_INTERPOLATE_METHOD",
        "WHISPERX_MAX_SEGMENT_SECONDS",
        "WHISPERX_ALIGN_IN_SUBPROCESS",
        "WHISPERX_ACCEPT_RAW_FALLBACK_CACHE",
        "WORD_CORRECTIONS",
    ],
    "llm": [
        "LM_STUDIO_MODEL",
        "CHUNK_DURATION",
        "CHUNK_OVERLAP",
        "MIN_CLIP_DURATION",
        "MAX_CLIP_DURATION",
        "MIN_SCORE",
        "MIN_CLIP_WORDS",
        "MIN_SPEECH_WORDS_PER_SECOND",
        "MAX_CLIP_SEGMENT_GAP",
    ],
    "yolo": [
        "YOLO_WEIGHTS",
        "YOLO_CONF_THRESHOLD",
        "YOLO_FRAME_SKIP",
        "YOLO_IMGSZ",
        "YOLO_HALF",
        "YOLO_SCAN_ONLY_MOMENTS",
        "YOLO_SCAN_PAD_BEFORE",
        "YOLO_SCAN_PAD_AFTER",
        "YOLO_SCAN_RANGE_MERGE_GAP",
        "ROI",
        "PRODUCT_CLASSES",
        "HOST_FACE_CLASS",
    ],
    "ffmpeg": [
        "OUTPUT_FPS",
        "OUTPUT_CODEC",
        "OUTPUT_CRF",
        "OUTPUT_CQ",
        "OUTPUT_PRESET",
        "OUTPUT_AUDIO_BITRATE",
        "MAX_PARALLEL_CLIPS",
        "RAW_CUT_CODEC",
        "RAW_CUT_PRESET",
        "VARIANTS_PER_CLIP",
        "VARIANT_SEED",
        "VARIANT_FFMPEG_BAKE",
        "FONT_HOOK",
        "FONT_SUBTITLE",
        "FONT_PRODUCT",
        "HOOK_FONTSIZE",
        "HOOK_COLOR",
        "HOOK_STROKE_COLOR",
        "HOOK_STROKE_W",
        "HOOK_DURATION",
        "SUBTITLE_FONTSIZE",
        "SUBTITLE_STROKE",
        "SUBTITLE_STROKE_W",
        "SUBTITLE_Y_POS",
        "KARAOKE_ACTIVE_COLOR",
        "KARAOKE_INACTIVE_OPACITY",
        "ZOOM_DURATION",
        "ZOOM_SCALE",
        "ZOOM_CAPTION_TEXT_COLOR",
        "ZOOM_CAPTION_BRAND_COLOR",
        "ZOOM_CAPTION_STROKE_COLOR",
        "ZOOM_CAPTION_STROKE_WIDTH",
        "ZOOM_CAPTION_FONTSIZE",
        "ZOOM_CAPTION_BRAND_FONTSIZE",
        "ZOOM_CAPTION_Y_POS",
        "HOST_FACE_ZOOM_ENABLED",
        "SFX_ENABLED",
        "SFX_DIR",
        "SFX_HIGHLIGHT_BLOCK_INTERVAL",
        "EMOJI_CONFIG",
        "BEFORE_AFTER_ENABLED",
        "BEFORE_AFTER_START_T",
        "BEFORE_AFTER_DURATION",
        "BEFORE_AFTER_OPACITY",
        "BEFORE_AFTER_FADE_IN",
        "BEFORE_AFTER_FADE_OUT",
        "SCORER_ENABLED",
        "SCORER_WEIGHTS",
        "SCORER_VISION_ENABLED",
        "SCORER_VISION_MODEL",
        "COMPLIANCE_ENABLED",
        "COMPLIANCE_AUTO_FIX",
        "COMPLIANCE_BLOCK_HIGH",
        "COMPLIANCE_LM_TIMEOUT",
    ],
}


def sidecar_path(output_path: str | Path) -> Path:
    path = Path(output_path)
    return path.with_name(f"{path.stem}.fingerprint.json")


def stage_fingerprint(video_path: str | Path, cfg, stage: str, extra: dict[str, Any] | None = None) -> dict:
    return {
        "stage": stage,
        "video": _path_identity(video_path),
        "model_name": _stage_model_name(cfg, stage),
        "config_hash": _config_hash(cfg, stage),
        "extra": _jsonable(extra or {}),
    }


def write_stage_fingerprint(
    output_path: str | Path,
    video_path: str | Path,
    cfg,
    stage: str,
    extra: dict[str, Any] | None = None,
) -> None:
    payload = {
        "fingerprint": stage_fingerprint(video_path, cfg, stage, extra=extra),
    }
    target = sidecar_path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(target)


def stage_fingerprint_matches(
    output_path: str | Path,
    video_path: str | Path,
    cfg,
    stage: str,
    extra: dict[str, Any] | None = None,
) -> bool:
    target = sidecar_path(output_path)
    if not Path(output_path).exists() or not target.exists():
        return False
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return False
    return payload.get("fingerprint") == stage_fingerprint(video_path, cfg, stage, extra=extra)


def _stage_model_name(cfg, stage: str) -> str:
    if stage == "transcribe":
        return str(getattr(cfg, "WHISPER_MODEL_SIZE", ""))
    if stage == "llm":
        return str(getattr(cfg, "LM_STUDIO_MODEL", ""))
    if stage == "yolo":
        return str(getattr(cfg, "YOLO_WEIGHTS", ""))
    if stage == "ffmpeg":
        return str(getattr(cfg, "OUTPUT_CODEC", ""))
    return ""


def _config_hash(cfg, stage: str) -> str:
    values = {
        key: _jsonable(getattr(cfg, key, None))
        for key in STAGE_CONFIG_KEYS.get(stage, [])
    }
    raw = json.dumps(values, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return str(value)
