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
        "OUTPUT_NVENC_PRESET",
        "OUTPUT_AUDIO_BITRATE",
        "MAX_PARALLEL_CLIPS",
        "RAW_CUT_CODEC",
        "RAW_CUT_PRESET",
        "SILENCE_TRIM_ENABLED",
        "SILENCE_TRIM_MIN_GAP",
        "SILENCE_TRIM_KEEP_GAP",
        "SILENCE_TRIM_EDGE_KEEP",
        "SILENCE_TRIM_MAX_REMOVAL_FRACTION",
        "SILENCE_TRIM_MIN_WORDS",
        "SILENCE_TRIM_MAX_WORD_DURATION",
        "RENDER_STYLE_VERSION",
        "VARIANTS_PER_CLIP",
        "VARIANT_SEED",
        "VARIANT_FFMPEG_BAKE",
        "FONT_HOOK",
        "FONT_HOOK_FALLBACKS",
        "FONT_SUBTITLE",
        "SUBTITLE_FONT_RANDOMIZE",
        "SUBTITLE_FONT_DIR",
        "FONT_PRODUCT",
        "FONT_LABEL",
        "LOGO_PATH",
        "HOOK_FONTSIZE",
        "HOOK_TOP_FONTSIZE",
        "HOOK_MID_FONTSIZE",
        "HOOK_BOTTOM_FONTSIZE",
        "HOOK_TOP_Y_POS",
        "HOOK_MID_Y_POS",
        "HOOK_BOTTOM_Y_POS",
        "HOOK_COLOR",
        "HOOK_ACCENT_COLOR",
        "HOOK_SHADOW_COLOR",
        "HOOK_STROKE_COLOR",
        "HOOK_STROKE_W",
        "HOOK_DURATION",
        "CTA_ENDCARD_ENABLED",
        "CTA_ENDCARD_DURATION",
        "CTA_ENDCARD_DEFAULT_TEXT",
        "SUBTITLE_FONTSIZE",
        "SUBTITLE_STROKE",
        "SUBTITLE_STROKE_W",
        "SUBTITLE_Y_POS",
        "SUBTITLE_SAFE_ZONE_TOP",
        "SUBTITLE_SAFE_ZONE_BOTTOM",
        "WORD_CORRECTION_APPLY_TO_SUBTITLES",
        "KARAOKE_ACTIVE_COLOR",
        "KARAOKE_INACTIVE_OPACITY",
        "HIGHLIGHT_PHRASES_PATH",
        "HIGHLIGHT_YELLOW_COLOR",
        "HIGHLIGHT_GREEN_COLOR",
        "HIGHLIGHT_RED_COLOR",
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
        "FACE_ZOOM_WORDS_TRIGGER",
        "FACE_ZOOM_SCALE_MIN",
        "FACE_ZOOM_SCALE_MAX",
        "FACE_ZOOM_EASE_MIN",
        "FACE_ZOOM_EASE_MAX",
        "FACE_ZOOM_DUR_MIN",
        "FACE_ZOOM_DUR_MAX",
        "FACE_ZOOM_SCREEN_Y",
        "FACE_ZOOM_SEARCH_WINDOW",
        "FACE_ZOOM_MIN_GAP",
        "SFX_ENABLED",
        "SFX_DIR",
        "SFX_PRODUCT_FOLDER",
        "SFX_YELLOW_FOLDER",
        "SFX_GREEN_FOLDER",
        "SFX_RED_FOLDER",
        "SFX_VOLUME_PRODUCT",
        "SFX_VOLUME_YELLOW",
        "SFX_VOLUME_GREEN",
        "SFX_VOLUME_RED",
        "SFX_HIGHLIGHT_BLOCK_INTERVAL",
        "BGM_ENABLED",
        "BGM_DIR",
        "BGM_VOLUME",
        "BGM_DUCKING_ENABLED",
        "BGM_DUCKING_THRESHOLD",
        "BGM_DUCKING_RATIO",
        "BGM_DUCKING_ATTACK_MS",
        "BGM_DUCKING_RELEASE_MS",
        "EMOJI_CONFIG",
        "BEFORE_AFTER_ENABLED",
        "BEFORE_AFTER_DIR",
        "BEFORE_AFTER_START_T",
        "BEFORE_AFTER_START_OFFSET",
        "BEFORE_AFTER_DURATION",
        "BEFORE_AFTER_OPACITY",
        "BEFORE_AFTER_FADE_IN",
        "BEFORE_AFTER_FADE_OUT",
        "BROLL_INTRO_ENABLED",
        "BROLL_INTRO_DIR",
        "BROLL_INTRO_MIN_VARIANT_RATE",
        "BROLL_INTRO_MAX_VARIANT_RATE",
        "BROLL_INTRO_APPLY_TO_ORIGINAL",
        "BROLL_INTRO_MAX_DURATION",
        "BROLL_INTRO_FADE_IN",
        "BROLL_INTRO_FADE_OUT",
        "BROLL_INTRO_REQUIRE_PRODUCT_MATCH",
        "BROLL_INTRO_ALLOW_GENERIC_ROOT",
        "BROLL_INTRO_PRODUCT_ALIASES",
        "PRODUCT_BROLL_DIR",
        "PRODUCT_BROLL_CROSSFADE_SECONDS",
        "PRODUCT_BROLL_VIDEO_EXTS",
        "TRANSITIONAL_HOOK_ENABLED",
        "TRANSITIONAL_HOOK_DIR",
        "SCORER_ENABLED",
        "SCORER_WEIGHTS",
        "SCORER_VISION_ENABLED",
        "SCORER_VISION_MODEL",
        "SCORER_VISION_CONTACT_SHEET",
        "SCORER_VISION_CONTACT_SHEET_MAX_FRAMES",
        "SCORER_VISION_CONTACT_SHEET_CELL_SIZE",
        "COMPLIANCE_ENABLED",
        "COMPLIANCE_AUTO_FIX",
        "COMPLIANCE_BLOCK_HIGH",
        "COMPLIANCE_LM_TIMEOUT",
        "MODULE_EXTRACTION_ENABLED",
        "MODULE_LIBRARY_DIR",
        "MODULE_HOOK_MIN_DURATION",
        "MODULE_HOOK_MAX_DURATION",
        "MODULE_MAIN_MIN_DURATION",
        "MODULE_MAIN_MAX_DURATION",
        "MODULE_CTA_MIN_DURATION",
        "MODULE_CTA_MAX_DURATION",
        "MODULE_SENTENCE_BOUNDARY_TOLERANCE",
        "MODULE_ASSEMBLY_ENABLED",
        "MODULE_ASSEMBLY_RENDER_LIMIT",
        "MODULE_DEDUPE_IOU_THRESHOLD",
        "MODULE_PRODUCT_ZOOM_ENABLED",
    ],
}

RENDER_ASSET_PATH_KEYS = (
    "LOGO_PATH",
    "FONT_HOOK",
    "FONT_HOOK_FALLBACKS",
    "FONT_LABEL",
    "FONT_SUBTITLE",
    "SUBTITLE_FONT_DIR",
    "FONT_PRODUCT",
    "HIGHLIGHT_PHRASES_PATH",
    "BEFORE_AFTER_DIR",
    "SFX_DIR",
    "BGM_DIR",
    "BROLL_INTRO_DIR",
    "PRODUCT_BROLL_DIR",
    "TRANSITIONAL_HOOK_DIR",
)


def sidecar_path(output_path: str | Path) -> Path:
    path = Path(output_path)
    return path.with_name(f"{path.stem}.fingerprint.json")


def stage_fingerprint(video_path: str | Path, cfg, stage: str, extra: dict[str, Any] | None = None) -> dict:
    payload = {
        "stage": stage,
        "video": _path_identity(video_path),
        "model_name": _stage_model_name(cfg, stage),
        "config_hash": _config_hash(cfg, stage),
        "extra": _jsonable(extra or {}),
    }
    if stage == "ffmpeg":
        payload["asset_hash"] = _render_asset_hash(cfg)
    return payload


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


def _render_asset_hash(cfg) -> str:
    identities: list[dict[str, Any]] = []
    for key in RENDER_ASSET_PATH_KEYS:
        raw = getattr(cfg, key, None)
        values = raw if isinstance(raw, (list, tuple, set)) else [raw]
        for value in values:
            if not value:
                continue
            path = Path(str(value))
            if not path.is_absolute():
                path = Path.cwd() / path
            identities.extend(_asset_path_identities(key, path))

    emoji_cfg = getattr(cfg, "EMOJI_CONFIG", {}) or {}
    rules = emoji_cfg.get("emoji_rules", []) if isinstance(emoji_cfg, dict) else []
    for rule in rules:
        if not isinstance(rule, dict) or not rule.get("png_path"):
            continue
        path = Path(str(rule["png_path"]))
        if not path.is_absolute():
            path = Path.cwd() / path
        identities.extend(_asset_path_identities("EMOJI_CONFIG", path))

    raw = json.dumps(identities, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _asset_path_identities(key: str, path: Path) -> list[dict[str, Any]]:
    if path.is_file():
        identity = _path_identity(path)
        return [{"key": key, "relative": path.name, "identity": identity}]
    if not path.is_dir():
        return [{"key": key, "relative": str(path), "missing": True}]
    rows: list[dict[str, Any]] = []
    for child in sorted((item for item in path.rglob("*") if item.is_file()), key=lambda item: str(item).casefold()):
        rows.append({
            "key": key,
            "relative": str(child.relative_to(path)).replace("\\", "/"),
            "identity": _path_identity(child),
        })
    return rows


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, bool)) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, set):
        return [_jsonable(item) for item in sorted(value, key=str)]
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return str(value)
