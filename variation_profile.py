from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any


SCHEMA_VERSION = 12
PREVIEW_RENDER_VERSION = 22
MIN_VARIANTS = 1
MAX_VARIANTS = 6

HOOK_TYPES = ("none", "text", "before_after_image", "text_before_after_image", "b_roll", "text_b_roll", "transitional_hook")
LEGACY_HOOK_TYPES = ("auto", "pain", "result", "curiosity", "value", "product_focus")
SUBTITLE_POSITIONS = ("top", "center", "bottom")
SUBTITLE_SIZES = ("compact", "small", "medium", "large")
SUBTITLE_SIZE_PIXELS = {"compact": 72, "small": 96, "medium": 120, "large": 144}
COLOR_GRADES = ("original", "warm", "cool", "vivid", "desaturated", "cinematic")
BGM_MODES = ("auto", "none", "selected")
ZOOM_INTENSITIES = ("none", "subtle", "normal", "strong")
VISUAL_MODES = ("host", "broll_audio")
BEFORE_AFTER_MODES = ("fullscreen",)
TEXT_STYLE_IDS = (
    "current",
    "creator_bold_pop",
    "native_clean",
    "premium_skincare",
    "sales_karaoke",
    "urgency_stack",
)
SUBTITLE_ANIMATIONS = ("current", "phrase_cut")
HEADLINE_ANIMATIONS = ("current", "pop_overshoot", "soft_pop", "fade_up", "punch", "slide_up")
CAPTION_ANIMATIONS = ("current", "staggered_reveal", "fade_up", "wipe", "slide_up")
DYNAMIC_TEXT_MODES = ("off", "minimal", "balanced", "high_energy")
DYNAMIC_TEXT_ROLES = ("ingredients", "benefits", "usage", "cta")
DYNAMIC_TEXT_ANIMATIONS = ("current", "staggered_reveal", "fade_up", "wipe", "slide_up")
DYNAMIC_TEXT_DURATION_RANGE = (1.0, 6.0)
DYNAMIC_TEXT_BODY_SIZE_RANGE = (20, 72)
DYNAMIC_TEXT_CTA_SIZE_RANGE = (24, 96)
LETTERBOX_BAR_HEIGHT_FRAC = 0.20
SUBTITLE_Y_FRAC_RANGE = (0.08, 0.92)
LETTERBOX_FRAC_RANGE = (0.0, 0.40)
LETTERBOX_HOOK_FONT_SIZE_RANGE = (24, 160)
LETTERBOX_HOOK_POSITION_RANGE = (0.0, 1.0)
FIXED_PREVIEW_SOURCE = Path("assets/variation_preview/raw_cut_preview.mp4")

_AUDIO_EXTS = {".wav", ".mp3", ".ogg", ".aac", ".flac", ".m4a"}
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

_DEFAULT_VARIANTS = [
    {
        "name": "Original",
        "hook_type": "text",
        "font_color": "#FFFFFF",
        "highlight_color": "#FFD600",
        "subtitle_position": "bottom",
        "color_grade": "original",
        "bgm_mode": "auto",
        "sfx_enabled": True,
        "zoom_intensity": "normal",
        "product_zoom_enabled": True,
        "subtitle_enabled": True,
        "dynamic_text_mode": "balanced",
        "dynamic_text_roles": list(DYNAMIC_TEXT_ROLES),
        "letterbox_enabled": False,
        "mirror_enabled": False,
        "before_after_mode": "fullscreen",
    },
    {
        "name": "Before After",
        "hook_type": "text_before_after_image",
        "font_color": "#FFFFFF",
        "highlight_color": "#FF2D78",
        "subtitle_position": "center",
        "color_grade": "warm",
        "bgm_mode": "auto",
        "sfx_enabled": True,
        "zoom_intensity": "normal",
        "product_zoom_enabled": True,
        "subtitle_enabled": True,
        "dynamic_text_mode": "balanced",
        "dynamic_text_roles": list(DYNAMIC_TEXT_ROLES),
        "letterbox_enabled": False,
        "mirror_enabled": False,
        "before_after_mode": "fullscreen",
    },
    {
        "name": "B-roll Hook",
        "hook_type": "text_b_roll",
        "font_color": "#FFFFFF",
        "highlight_color": "#00D4FF",
        "subtitle_position": "bottom",
        "color_grade": "cool",
        "bgm_mode": "auto",
        "sfx_enabled": True,
        "zoom_intensity": "strong",
        "product_zoom_enabled": True,
        "subtitle_enabled": True,
        "dynamic_text_mode": "balanced",
        "dynamic_text_roles": list(DYNAMIC_TEXT_ROLES),
        "letterbox_enabled": False,
        "mirror_enabled": False,
        "before_after_mode": "fullscreen",
    },
    {
        "name": "Image Only",
        "hook_type": "before_after_image",
        "font_color": "#FFFFFF",
        "highlight_color": "#C77DFF",
        "subtitle_position": "top",
        "color_grade": "vivid",
        "bgm_mode": "auto",
        "sfx_enabled": True,
        "zoom_intensity": "subtle",
        "product_zoom_enabled": True,
        "subtitle_enabled": True,
        "dynamic_text_mode": "balanced",
        "dynamic_text_roles": list(DYNAMIC_TEXT_ROLES),
        "letterbox_enabled": False,
        "mirror_enabled": False,
        "before_after_mode": "fullscreen",
    },
    {
        "name": "B-roll Only",
        "hook_type": "b_roll",
        "font_color": "#FFFFFF",
        "highlight_color": "#FFE500",
        "subtitle_position": "center",
        "color_grade": "desaturated",
        "bgm_mode": "auto",
        "sfx_enabled": False,
        "zoom_intensity": "normal",
        "product_zoom_enabled": True,
        "subtitle_enabled": True,
        "dynamic_text_mode": "balanced",
        "dynamic_text_roles": list(DYNAMIC_TEXT_ROLES),
        "letterbox_enabled": False,
        "mirror_enabled": False,
        "before_after_mode": "fullscreen",
    },
    {
        "name": "Product Focus",
        "hook_type": "text",
        "font_color": "#FFFFFF",
        "highlight_color": "#00FF7F",
        "subtitle_position": "bottom",
        "color_grade": "cinematic",
        "bgm_mode": "auto",
        "sfx_enabled": True,
        "zoom_intensity": "strong",
        "product_zoom_enabled": True,
        "subtitle_enabled": True,
        "dynamic_text_mode": "balanced",
        "dynamic_text_roles": list(DYNAMIC_TEXT_ROLES),
        "letterbox_enabled": True,
        "mirror_enabled": False,
        "before_after_mode": "fullscreen",
    },
]

_FALLBACK_FONTS = (
    "assets/fonts/Montserrat-ExtraBold.ttf",
    "assets/fonts/Anton-Regular.ttf",
    "assets/fonts/PlayfairDisplay-Italic-VariableFont_wght.ttf",
)

TEXT_STYLE_PRESETS: dict[str, dict[str, Any]] = {
    "current": {
        "label": "Current",
        "description": "Keep the existing typography and static text motion.",
        "defaults": {
            "subtitle_animation": "current",
            "headline_animation": "current",
            "caption_animation": "current",
        },
    },
    "creator_bold_pop": {
        "label": "Creator Bold Pop — Reference",
        "description": "Montserrat, compact phrase captions, pink shadow, overshoot headlines, and staggered product copy.",
        "defaults": {
            "font_id": "assets/fonts/Montserrat-ExtraBold.ttf",
            "headline_font_id": "assets/fonts/Montserrat-ExtraBold.ttf",
            "caption_font_id": "assets/fonts/Montserrat-ExtraBold.ttf",
            "font_color": "#FFFFFF",
            "highlight_color": "#FF719B",
            "subtitle_position": "center",
            "subtitle_size": "compact",
            "subtitle_y_frac": 0.67,
            "subtitle_stroke_color": "#000000",
            "subtitle_stroke_width": 5,
            "subtitle_highlight_enabled": False,
            "subtitle_animation": "phrase_cut",
            "headline_animation": "pop_overshoot",
            "caption_animation": "staggered_reveal",
            "headline_stroke_width": 7,
            "headline_shadow_color": "#FF719B",
            "headline_shadow_x": 6,
            "headline_shadow_y": 6,
            "headline_rotation_degrees": -2.0,
            "caption_stroke_width": 5,
        },
    },
    "native_clean": {
        "label": "Native Clean",
        "description": "TikTok Sans with a restrained soft pop and clean compact captions.",
        "defaults": {
            "font_id": "assets/fonts/TikTokSans-Bold.ttf",
            "headline_font_id": "assets/fonts/TikTokSans-Bold.ttf",
            "caption_font_id": "assets/fonts/TikTokSans-Bold.ttf",
            "font_color": "#FFFFFF",
            "highlight_color": "#00F2EA",
            "subtitle_position": "bottom",
            "subtitle_size": "small",
            "subtitle_y_frac": 0.76,
            "subtitle_stroke_color": "#000000",
            "subtitle_stroke_width": 3,
            "subtitle_highlight_enabled": False,
            "subtitle_animation": "phrase_cut",
            "headline_animation": "soft_pop",
            "caption_animation": "fade_up",
            "headline_stroke_width": 5,
            "headline_shadow_color": "#000000",
            "headline_shadow_x": 3,
            "headline_shadow_y": 3,
            "headline_rotation_degrees": 0.0,
            "caption_stroke_width": 3,
        },
    },
    "premium_skincare": {
        "label": "Premium Skincare",
        "description": "Outfit with compact copy, light outlines, and gentle rise-and-fade motion.",
        "defaults": {
            "font_id": "assets/fonts/Outfit-Bold.ttf",
            "headline_font_id": "assets/fonts/Outfit-Bold.ttf",
            "caption_font_id": "assets/fonts/Outfit-Bold.ttf",
            "font_color": "#FFFFFF",
            "highlight_color": "#F5D7A1",
            "subtitle_position": "bottom",
            "subtitle_size": "compact",
            "subtitle_y_frac": 0.75,
            "subtitle_stroke_color": "#241A14",
            "subtitle_stroke_width": 2,
            "subtitle_highlight_enabled": False,
            "subtitle_animation": "phrase_cut",
            "headline_animation": "fade_up",
            "caption_animation": "fade_up",
            "headline_stroke_width": 3,
            "headline_shadow_color": "#000000",
            "headline_shadow_x": 2,
            "headline_shadow_y": 3,
            "headline_rotation_degrees": 0.0,
            "caption_stroke_width": 2,
        },
    },
    "sales_karaoke": {
        "label": "Sales Karaoke",
        "description": "Montserrat with strong active-word color and a compact punchy headline.",
        "defaults": {
            "font_id": "assets/fonts/Montserrat-ExtraBold.ttf",
            "headline_font_id": "assets/fonts/Montserrat-ExtraBold.ttf",
            "caption_font_id": "assets/fonts/Montserrat-ExtraBold.ttf",
            "font_color": "#FFFFFF",
            "highlight_color": "#FFD600",
            "subtitle_position": "bottom",
            "subtitle_size": "small",
            "subtitle_y_frac": 0.72,
            "subtitle_stroke_color": "#000000",
            "subtitle_stroke_width": 4,
            "subtitle_highlight_enabled": True,
            "subtitle_animation": "phrase_cut",
            "headline_animation": "punch",
            "caption_animation": "wipe",
            "headline_stroke_width": 6,
            "headline_shadow_color": "#000000",
            "headline_shadow_x": 4,
            "headline_shadow_y": 4,
            "headline_rotation_degrees": 0.0,
            "caption_stroke_width": 4,
        },
    },
    "urgency_stack": {
        "label": "Urgency Stack",
        "description": "Condensed Anton headlines with fast upward text motion for limited-use urgency variants.",
        "defaults": {
            "font_id": "assets/fonts/Anton-Regular.ttf",
            "headline_font_id": "assets/fonts/Anton-Regular.ttf",
            "caption_font_id": "assets/fonts/Anton-Regular.ttf",
            "font_color": "#FFFFFF",
            "highlight_color": "#FF3B30",
            "subtitle_position": "bottom",
            "subtitle_size": "small",
            "subtitle_y_frac": 0.72,
            "subtitle_stroke_color": "#000000",
            "subtitle_stroke_width": 4,
            "subtitle_highlight_enabled": True,
            "subtitle_animation": "phrase_cut",
            "headline_animation": "slide_up",
            "caption_animation": "slide_up",
            "headline_stroke_width": 6,
            "headline_shadow_color": "#000000",
            "headline_shadow_x": 4,
            "headline_shadow_y": 4,
            "headline_rotation_degrees": 0.0,
            "caption_stroke_width": 4,
        },
    },
}


class VariationProfileError(ValueError):
    pass


class VariationRevisionConflict(VariationProfileError):
    pass


def working_dir(cfg) -> Path:
    value = Path(str(getattr(cfg, "WORKING_DIR", "working") or "working"))
    if not value.is_absolute():
        value = Path.cwd() / value
    return value.resolve()


def active_profile_path(cfg) -> Path:
    return working_dir(cfg) / "variation_profile.json"


def presets_dir(cfg) -> Path:
    return working_dir(cfg) / "variation_presets"


def previews_dir(cfg) -> Path:
    return working_dir(cfg) / "variation_previews"


def fixed_preview_source_path(cfg=None) -> Path:
    return (Path.cwd() / FIXED_PREVIEW_SOURCE).resolve()


def preview_source_ref(cfg) -> dict[str, Any]:
    path = fixed_preview_source_path(cfg)
    return {
        "path": str(path),
        "url": f"/api/artifacts?path={_quote_artifact_path(path)}",
        "kind": "video",
        "exists": path.exists() and path.is_file(),
    }


def has_active_profile(cfg) -> bool:
    return hasattr(cfg, "WORKING_DIR") and active_profile_path(cfg).exists()


def default_profile(cfg) -> dict[str, Any]:
    count = _clamp_int(getattr(cfg, "VARIANTS_PER_CLIP", 4), MIN_VARIANTS, MAX_VARIANTS, 4)
    fonts = discover_fonts(cfg)
    default_font = fonts[0]["id"] if fonts else _FALLBACK_FONTS[0]
    variants = []
    for index in range(count):
        template = dict(_DEFAULT_VARIANTS[index % len(_DEFAULT_VARIANTS)])
        template["name"] = template["name"] if index < len(_DEFAULT_VARIANTS) else f"Variant {index + 1}"
        template["visual_mode"] = str(template.get("visual_mode") or "host")
        template["random_broll_enabled"] = False
        template["text_style_id"] = "current"
        template["font_id"] = default_font
        template["headline_font_id"] = default_font
        template["caption_font_id"] = default_font
        template["dynamic_text_settings"] = _default_dynamic_text_settings(default_font, default_font)
        template["subtitle_stroke_color"] = str(getattr(cfg, "SUBTITLE_STROKE", "#000000"))
        template["subtitle_stroke_width"] = int(getattr(cfg, "SUBTITLE_STROKE_W", 3))
        template["subtitle_highlight_enabled"] = True
        template["subtitle_animation"] = "current"
        template["headline_animation"] = "current"
        template["caption_animation"] = "current"
        template["headline_stroke_width"] = int(getattr(cfg, "HOOK_STROKE_W", 5))
        template["headline_shadow_color"] = str(getattr(cfg, "HOOK_SHADOW_COLOR", "#000000"))
        template["headline_shadow_x"] = 3
        template["headline_shadow_y"] = 3
        template["headline_rotation_degrees"] = 0.0
        template["caption_stroke_width"] = int(getattr(cfg, "ZOOM_CAPTION_STROKE_WIDTH", 4))
        template["bgm_path"] = ""
        template["subtitle_size"] = "medium"
        template["subtitle_y_frac"] = _subtitle_y_for_position(str(template.get("subtitle_position") or "bottom"))
        _apply_letterbox_hook_defaults(template, default_font)
        variants.append(template)
    for index, variant in enumerate(variants):
        variant["letterbox_enabled"] = count > 1 and index == count - 1
        _apply_letterbox_defaults(variant)
        _apply_letterbox_hook_defaults(variant, variant.get("font_id") or default_font)
    profile = {
        "schema_version": SCHEMA_VERSION,
        "variant_count": count,
        "updated_at": "",
        "variants": variants,
    }
    profile["revision"] = profile_revision(profile)
    return profile


def load_profile_if_exists(cfg) -> dict[str, Any] | None:
    if not has_active_profile(cfg):
        return None
    payload = _read_json(active_profile_path(cfg))
    return normalize_profile(payload, cfg)


def load_active_profile(cfg) -> dict[str, Any]:
    profile = load_profile_if_exists(cfg)
    return profile if profile is not None else default_profile(cfg)


def active_profile_revision(cfg) -> str:
    return str(load_active_profile(cfg).get("revision") or "")


def save_active_profile(cfg, payload: dict[str, Any], expected_revision: str | None = None) -> dict[str, Any]:
    current = load_active_profile(cfg)
    if expected_revision and expected_revision != current.get("revision"):
        raise VariationRevisionConflict("Variation profile revision is stale; refresh before saving.")
    profile = normalize_profile(payload, cfg)
    profile["updated_at"] = _now()
    profile["revision"] = profile_revision(profile)
    _write_json_atomic(active_profile_path(cfg), profile)
    return profile


def list_presets(cfg) -> list[dict[str, str]]:
    root = presets_dir(cfg)
    if not root.exists():
        return []
    presets = []
    for path in sorted(root.glob("*.json")):
        try:
            payload = normalize_profile(_read_json(path), cfg)
        except VariationProfileError:
            continue
        presets.append({
            "preset_id": path.stem,
            "name": str(payload.get("name") or path.stem),
            "revision": str(payload.get("revision") or ""),
        })
    return presets


def save_preset(cfg, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    clean_name = " ".join(str(name or "").strip().split())
    if not clean_name:
        raise VariationProfileError("Preset name is required.")
    preset_id = _safe_identifier(clean_name)
    if not preset_id:
        raise VariationProfileError("Preset name must contain letters or numbers.")
    profile = normalize_profile(payload, cfg)
    profile["name"] = clean_name
    profile["updated_at"] = _now()
    profile["revision"] = profile_revision(profile)
    target = presets_dir(cfg) / f"{preset_id}.json"
    _write_json_atomic(target, profile)
    return profile | {"preset_id": preset_id}


def load_preset(cfg, preset_id: str) -> dict[str, Any]:
    safe = _safe_identifier(preset_id)
    if not safe:
        raise FileNotFoundError("Preset was not found.")
    path = presets_dir(cfg) / f"{safe}.json"
    if not path.exists():
        raise FileNotFoundError("Preset was not found.")
    return normalize_profile(_read_json(path), cfg)


def variation_options(cfg) -> dict[str, Any]:
    try:
        from product_broll import product_broll_preview_sources
        product_broll = product_broll_preview_sources(cfg)
    except Exception:
        product_broll = {"root": "", "exists": False, "products": []}
    return {
        "fonts": discover_fonts(cfg),
        "text_styles": [
            {
                "id": style_id,
                "label": str(TEXT_STYLE_PRESETS[style_id]["label"]),
                "description": str(TEXT_STYLE_PRESETS[style_id]["description"]),
                "defaults": dict(TEXT_STYLE_PRESETS[style_id]["defaults"]),
            }
            for style_id in TEXT_STYLE_IDS
        ],
        "bgm_tracks": discover_bgm_tracks(cfg),
        "hook_types": list(HOOK_TYPES),
        "visual_modes": list(VISUAL_MODES),
        "before_after_modes": list(BEFORE_AFTER_MODES),
        "subtitle_positions": list(SUBTITLE_POSITIONS),
        "subtitle_sizes": list(SUBTITLE_SIZES),
        "dynamic_text_modes": list(DYNAMIC_TEXT_MODES),
        "dynamic_text_roles": list(DYNAMIC_TEXT_ROLES),
        "dynamic_text_animations": list(DYNAMIC_TEXT_ANIMATIONS),
        "color_grades": list(COLOR_GRADES),
        "bgm_modes": list(BGM_MODES),
        "zoom_intensities": list(ZOOM_INTENSITIES),
        "presets": list_presets(cfg),
        "limits": {"min_variants": MIN_VARIANTS, "max_variants": MAX_VARIANTS},
        "preview_source": preview_source_ref(cfg),
        "product_broll": product_broll,
        "global_feature_flags": {
            "sfx": bool(getattr(cfg, "SFX_ENABLED", True)),
            "bgm": bool(getattr(cfg, "BGM_ENABLED", True)),
            "before_after": bool(getattr(cfg, "BEFORE_AFTER_ENABLED", True)),
            "broll_intro": bool(getattr(cfg, "BROLL_INTRO_ENABLED", True)),
            "transitional_hook": bool(getattr(cfg, "TRANSITIONAL_HOOK_ENABLED", True)),
            "host_face_zoom": bool(getattr(cfg, "HOST_FACE_ZOOM_ENABLED", True)),
        },
    }


def normalize_profile(payload: dict[str, Any], cfg) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise VariationProfileError("Variation profile must be a JSON object.")
    count = _clamp_int(payload.get("variant_count", getattr(cfg, "VARIANTS_PER_CLIP", 4)), MIN_VARIANTS, MAX_VARIANTS, 4)
    raw_variants = payload.get("variants")
    if raw_variants is None:
        raw_variants = []
    if not isinstance(raw_variants, list):
        raise VariationProfileError("Variation profile variants must be a list.")

    variants = []
    for index in range(count):
        raw = raw_variants[index] if index < len(raw_variants) and isinstance(raw_variants[index], dict) else {}
        variants.append(normalize_variant(raw, index, cfg))
    profile = {
        "schema_version": SCHEMA_VERSION,
        "variant_count": count,
        "updated_at": str(payload.get("updated_at") or ""),
        "variants": variants,
    }
    if payload.get("name"):
        profile["name"] = " ".join(str(payload.get("name") or "").split())
    profile["revision"] = profile_revision(profile)
    return profile


def normalize_variant(raw: dict[str, Any], index: int, cfg) -> dict[str, Any]:
    defaults = dict(_DEFAULT_VARIANTS[index % len(_DEFAULT_VARIANTS)])
    text_style_id = _choice(raw.get("text_style_id"), TEXT_STYLE_IDS, "current")
    text_style_defaults = dict(TEXT_STYLE_PRESETS[text_style_id]["defaults"])
    fonts = discover_fonts(cfg)
    font_ids = {item["id"] for item in fonts}
    default_font = fonts[min(index, len(fonts) - 1)]["id"] if fonts else _FALLBACK_FONTS[0]

    font_id = str(
        raw.get("font_id")
        or text_style_defaults.get("font_id")
        or defaults.get("font_id")
        or default_font
    ).strip()
    if font_ids and font_id not in font_ids:
        font_id = default_font
    elif not font_id:
        font_id = default_font

    headline_font_id = str(raw.get("headline_font_id") or text_style_defaults.get("headline_font_id") or font_id).strip()
    if font_ids and headline_font_id not in font_ids:
        headline_font_id = font_id
    elif not headline_font_id:
        headline_font_id = font_id

    caption_font_id = str(raw.get("caption_font_id") or text_style_defaults.get("caption_font_id") or font_id).strip()
    if font_ids and caption_font_id not in font_ids:
        caption_font_id = font_id
    elif not caption_font_id:
        caption_font_id = font_id

    dynamic_text_settings = _normalize_dynamic_text_settings(
        raw.get("dynamic_text_settings"),
        font_ids=font_ids,
        headline_font_id=headline_font_id,
        body_font_id=caption_font_id,
    )

    bgm_tracks = {item["path"] for item in discover_bgm_tracks(cfg)}
    bgm_path = str(raw.get("bgm_path") or "").strip()
    bgm_mode = _choice(raw.get("bgm_mode"), BGM_MODES, defaults["bgm_mode"])
    if bgm_mode == "selected" and bgm_tracks and bgm_path not in bgm_tracks:
        bgm_path = ""
        bgm_mode = "auto"
    if bgm_mode != "selected":
        bgm_path = ""

    subtitle_position = _choice(
        raw.get("subtitle_position"),
        SUBTITLE_POSITIONS,
        str(text_style_defaults.get("subtitle_position") or defaults["subtitle_position"]),
    )
    subtitle_size = _choice(
        raw.get("subtitle_size"),
        SUBTITLE_SIZES,
        str(text_style_defaults.get("subtitle_size") or defaults.get("subtitle_size", "medium")),
    )
    letterbox_enabled = bool(raw.get("letterbox_enabled", defaults["letterbox_enabled"]))
    bar_default = LETTERBOX_BAR_HEIGHT_FRAC if letterbox_enabled else 0.0
    letterbox_hook_font_id = str(raw.get("letterbox_hook_font_id") or font_id).strip()
    if font_ids and letterbox_hook_font_id not in font_ids:
        letterbox_hook_font_id = font_id
    elif not letterbox_hook_font_id:
        letterbox_hook_font_id = font_id

    visual_mode = _normalize_visual_mode(raw.get("visual_mode"), defaults.get("visual_mode", "host"))
    variant = {
        "name": _clean_label(raw.get("name") or defaults["name"] or f"Variant {index + 1}", f"Variant {index + 1}"),
        "hook_type": _normalize_hook_type(raw.get("hook_type"), defaults["hook_type"]),
        "visual_mode": visual_mode,
        "random_broll_enabled": bool(raw.get("random_broll_enabled", False)) and visual_mode != "broll_audio",
        "text_style_id": text_style_id,
        "font_id": font_id,
        "headline_font_id": headline_font_id,
        "caption_font_id": caption_font_id,
        "font_color": _hex(raw.get("font_color"), str(text_style_defaults.get("font_color") or defaults["font_color"])),
        "highlight_color": _hex(raw.get("highlight_color"), str(text_style_defaults.get("highlight_color") or defaults["highlight_color"])),
        "subtitle_position": subtitle_position,
        "subtitle_size": subtitle_size,
        "subtitle_y_frac": _clamp_float(
            raw.get("subtitle_y_frac"),
            SUBTITLE_Y_FRAC_RANGE[0],
            SUBTITLE_Y_FRAC_RANGE[1],
            float(text_style_defaults.get("subtitle_y_frac") or _subtitle_y_for_position(subtitle_position)),
        ),
        "subtitle_stroke_color": _hex(
            raw.get("subtitle_stroke_color"),
            str(text_style_defaults.get("subtitle_stroke_color") or getattr(cfg, "SUBTITLE_STROKE", "#000000")),
        ),
        "subtitle_stroke_width": _clamp_int(
            raw.get("subtitle_stroke_width"),
            0,
            12,
            int(text_style_defaults.get("subtitle_stroke_width") or getattr(cfg, "SUBTITLE_STROKE_W", 3)),
        ),
        "subtitle_highlight_enabled": bool(
            raw.get(
                "subtitle_highlight_enabled",
                text_style_defaults.get("subtitle_highlight_enabled", True),
            )
        ),
        "subtitle_animation": _choice(
            raw.get("subtitle_animation"),
            SUBTITLE_ANIMATIONS,
            str(text_style_defaults.get("subtitle_animation") or "current"),
        ),
        "headline_animation": _choice(
            raw.get("headline_animation"),
            HEADLINE_ANIMATIONS,
            str(text_style_defaults.get("headline_animation") or "current"),
        ),
        "caption_animation": _choice(
            raw.get("caption_animation"),
            CAPTION_ANIMATIONS,
            str(text_style_defaults.get("caption_animation") or "current"),
        ),
        "headline_stroke_width": _clamp_int(
            raw.get("headline_stroke_width"),
            0,
            12,
            int(text_style_defaults.get("headline_stroke_width") or getattr(cfg, "HOOK_STROKE_W", 5)),
        ),
        "headline_shadow_color": _hex(
            raw.get("headline_shadow_color"),
            str(text_style_defaults.get("headline_shadow_color") or getattr(cfg, "HOOK_SHADOW_COLOR", "#000000")),
        ),
        "headline_shadow_x": _clamp_int(
            raw.get("headline_shadow_x"),
            -20,
            20,
            int(text_style_defaults.get("headline_shadow_x") or 3),
        ),
        "headline_shadow_y": _clamp_int(
            raw.get("headline_shadow_y"),
            -20,
            20,
            int(text_style_defaults.get("headline_shadow_y") or 3),
        ),
        "headline_rotation_degrees": _clamp_float(
            raw.get("headline_rotation_degrees"),
            -10.0,
            10.0,
            float(text_style_defaults.get("headline_rotation_degrees") or 0.0),
        ),
        "caption_stroke_width": _clamp_int(
            raw.get("caption_stroke_width"),
            0,
            12,
            int(text_style_defaults.get("caption_stroke_width") or getattr(cfg, "ZOOM_CAPTION_STROKE_WIDTH", 4)),
        ),
        "color_grade": _choice(raw.get("color_grade"), COLOR_GRADES, defaults["color_grade"]),
        "bgm_mode": bgm_mode,
        "bgm_path": bgm_path,
        "sfx_enabled": bool(raw.get("sfx_enabled", defaults["sfx_enabled"])),
        "zoom_intensity": _choice(raw.get("zoom_intensity"), ZOOM_INTENSITIES, defaults["zoom_intensity"]),
        "product_zoom_enabled": bool(raw.get("product_zoom_enabled", defaults.get("product_zoom_enabled", True))),
        "subtitle_enabled": bool(raw.get("subtitle_enabled", defaults.get("subtitle_enabled", True))),
        "dynamic_text_mode": _choice(
            raw.get("dynamic_text_mode"),
            DYNAMIC_TEXT_MODES,
            str(defaults.get("dynamic_text_mode") or "balanced"),
        ),
        "dynamic_text_roles": _normalize_dynamic_text_roles(
            raw.get("dynamic_text_roles", defaults.get("dynamic_text_roles", DYNAMIC_TEXT_ROLES))
        ),
        "dynamic_text_settings": dynamic_text_settings,
        "letterbox_enabled": letterbox_enabled,
        "mirror_enabled": bool(raw.get("mirror_enabled", defaults.get("mirror_enabled", False))),
        "before_after_mode": _choice(raw.get("before_after_mode"), BEFORE_AFTER_MODES, defaults.get("before_after_mode", "fullscreen")),
        "letterbox_top_frac": _clamp_float(
            raw.get("letterbox_top_frac"),
            LETTERBOX_FRAC_RANGE[0],
            LETTERBOX_FRAC_RANGE[1],
            bar_default,
        ),
        "letterbox_bottom_frac": _clamp_float(
            raw.get("letterbox_bottom_frac"),
            LETTERBOX_FRAC_RANGE[0],
            LETTERBOX_FRAC_RANGE[1],
            bar_default,
        ),
        "letterbox_hook_enabled": bool(raw.get("letterbox_hook_enabled", False)),
        "letterbox_hook_font_id": letterbox_hook_font_id,
        "letterbox_hook_font_color": _hex(raw.get("letterbox_hook_font_color"), "#FFFFFF"),
        "letterbox_hook_font_size": _clamp_int(
            raw.get("letterbox_hook_font_size"),
            LETTERBOX_HOOK_FONT_SIZE_RANGE[0],
            LETTERBOX_HOOK_FONT_SIZE_RANGE[1],
            72,
        ),
        "letterbox_hook_x_frac": _clamp_float(
            raw.get("letterbox_hook_x_frac"),
            LETTERBOX_HOOK_POSITION_RANGE[0],
            LETTERBOX_HOOK_POSITION_RANGE[1],
            0.5,
        ),
        "letterbox_hook_y_frac": _clamp_float(
            raw.get("letterbox_hook_y_frac"),
            LETTERBOX_HOOK_POSITION_RANGE[0],
            LETTERBOX_HOOK_POSITION_RANGE[1],
            0.5,
        ),
    }
    return variant


def profile_revision(profile: dict[str, Any]) -> str:
    normalized = _revision_payload(profile)
    raw = json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_previews(
    cfg,
    payload: dict[str, Any],
    variant_index: int | None = None,
    product_key: str = "",
) -> dict[str, Any]:
    profile = normalize_profile(payload, cfg)
    source = fixed_preview_source_path(cfg)
    source_ref = preview_source_ref(cfg)
    preview_root = previews_dir(cfg)
    preview_root.mkdir(parents=True, exist_ok=True)
    if not source.exists() or not source.is_file():
        return {
            "profile_revision": profile["revision"],
            "source_clip": str(source),
            "preview_source": source_ref,
            "previews": [],
            "message": "Fixed preview clip was not found at assets/variation_preview/raw_cut_preview.mp4.",
        }

    selected_index = _clamp_int(variant_index, 0, max(0, len(profile["variants"]) - 1), 0)
    variant = profile["variants"][selected_index]
    preview_content, information_revision = _preview_dynamic_content(cfg, product_key)
    previews = []
    content_key = hashlib.sha256(
        json.dumps(preview_content, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:8]
    output = preview_root / (
        f"{profile['revision'][:12]}_p{PREVIEW_RENDER_VERSION}_v{selected_index}_{content_key}.mp4"
    )
    if not output.exists():
        _render_preview_video(source, output, variant, selected_index, preview_content, cfg)
    previews.append({
        "variant_index": selected_index,
        "variant_name": variant["name"],
        "path": str(output),
        "url": f"/api/artifacts?path={_quote_artifact_path(output)}",
        "kind": "video",
        "exists": output.exists() and output.is_file(),
    })
    return {
        "profile_revision": profile["revision"],
        "source_clip": str(source),
        "preview_source": source_ref,
        "product_information_revision": information_revision,
        "preview_product_key": product_key,
        "previews": previews,
        "message": "",
    }


def _preview_headline_motion(animation: str, font_size: int) -> tuple[str, str, str]:
    animation = str(animation or "current").strip().casefold()
    if animation == "pop_overshoot":
        return str(font_size), "1", "52"
    if animation in {"soft_pop", "punch"}:
        return str(font_size), "1", "52"
    if animation in {"fade_up", "slide_up"}:
        duration = 0.28 if animation == "fade_up" else 0.20
        distance = 18 if animation == "fade_up" else 42
        progress = f"min(1,max(0,t/{duration:.2f}))"
        return str(font_size), progress, f"52+{distance}*(1-({progress}))"
    return str(font_size), "1", "52"


def _preview_headline_segments(animation: str, font_size: int) -> list[tuple[int, float, float, str]]:
    animation = str(animation or "current").strip().casefold()
    if animation == "pop_overshoot":
        return [
            (max(1, int(font_size * 0.72)), 0.00, 0.07, "min(1,t/0.06)"),
            (max(1, int(font_size * 1.40)), 0.07, 0.16, "1"),
            (max(1, int(font_size * 0.92)), 0.16, 0.26, "1"),
            (font_size, 0.26, 1.45, "1"),
        ]
    if animation == "soft_pop":
        return [
            (max(1, int(font_size * 0.84)), 0.00, 0.10, "min(1,t/0.08)"),
            (max(1, int(font_size * 1.07)), 0.10, 0.19, "1"),
            (font_size, 0.19, 1.45, "1"),
        ]
    if animation == "punch":
        return [
            (max(1, int(font_size * 1.28)), 0.00, 0.12, "min(1,t/0.06)"),
            (font_size, 0.12, 1.45, "1"),
        ]
    fontsize, alpha, y_expr = _preview_headline_motion(animation, font_size)
    return [(max(1, int(float(fontsize))), 0.0, 1.45, alpha + "|" + y_expr)]


def _preview_dynamic_content(cfg, product_key: str) -> tuple[dict[str, Any], str]:
    try:
        from dynamic_text import concise_dynamic_fact_text
        from product_broll import PRODUCT_LABELS
        from product_information import facts_for_product, load_product_information_index

        index = load_product_information_index(cfg)
        facts = facts_for_product(index, product_key) if product_key else []
        grouped: dict[str, list[dict[str, Any]]] = {}
        for fact in facts:
            grouped.setdefault(str(fact.get("role") or ""), []).append(fact)
        role_lines: dict[str, list[str]] = {
            "ingredients": [],
            "benefits": [],
            "usage": [],
        }
        fact_role_to_setting = {
            "ingredient": "ingredients",
            "benefit": "benefits",
            "usage": "usage",
        }
        for role in ("ingredient", "benefit", "usage"):
            setting_role = fact_role_to_setting[role]
            limit = 2 if role == "ingredient" else 1
            for value in grouped.get(role, []):
                if role == "ingredient":
                    text = " ".join(str(value.get("text") or "").split())
                else:
                    text = concise_dynamic_fact_text(
                        value.get("text"),
                        max_words=6,
                    )
                if text and len(text) <= 64:
                    role_lines[setting_role].append(text)
                    if len(role_lines[setting_role]) >= limit:
                        break
        return {
            "hook": f"INFO {PRODUCT_LABELS.get(product_key, 'PRODUK').upper()}" if product_key else "INFO PRODUK",
            "role_lines": {
                "ingredients": role_lines["ingredients"] or ["[INGREDIENT]"],
                "benefits": role_lines["benefits"] or ["[BENEFIT]"],
                "usage": role_lines["usage"] or ["[CARA PAKAI]"],
            },
        }, str(index.get("revision") or "")
    except Exception:
        return {
            "hook": "INFO PRODUK",
            "role_lines": {
                "ingredients": ["[INGREDIENT]"],
                "benefits": ["[BENEFIT]"],
                "usage": ["[CARA PAKAI]"],
            },
        }, ""


def _preview_subtitle_words() -> list[dict[str, Any]]:
    cues = (
        ("Ini contoh gaya subtitle", 0.35, 1.55),
        ("Posisi dan ukuran mengikuti varian", 1.75, 3.35),
        ("Animasi terlihat di hasil render", 3.55, 5.55),
    )
    words: list[dict[str, Any]] = []
    highlight_index = 0
    for cue_index, (text, cue_start, cue_end) in enumerate(cues):
        cue_words = text.split()
        word_duration = (cue_end - cue_start) / max(1, len(cue_words))
        for word_index, word in enumerate(cue_words):
            start = cue_start + word_index * word_duration
            words.append({
                "word": word,
                "start": round(start, 3),
                "end": round(start + word_duration, 3),
                "_subtitle_group": cue_index,
                "_highlight_idx": highlight_index,
            })
            highlight_index += 1
    return words


def _preview_subtitle_cfg(cfg, variant: dict[str, Any]) -> SimpleNamespace:
    size_px = max(16, int(_subtitle_size_pixels(str(variant.get("subtitle_size") or "medium")) * 0.20))
    base_color = str(variant.get("font_color") or "#FFFFFF")
    highlight_color = (
        str(variant.get("highlight_color") or "#FFD600")
        if bool(variant.get("subtitle_highlight_enabled", True))
        else base_color
    )
    return SimpleNamespace(
        FONT_SUBTITLE=str(variant.get("font_id") or getattr(cfg, "FONT_SUBTITLE", "")),
        SUBTITLE_FONT_RANDOMIZE=False,
        SUBTITLE_FONT_DIR=str(getattr(cfg, "SUBTITLE_FONT_DIR", "assets/fonts")),
        SUBTITLE_FONTSIZE=max(16, int(round(size_px / 0.85))),
        SUBTITLE_Y_POS=_clamp_float(variant.get("subtitle_y_frac"), 0.08, 0.92, 0.84),
        SUBTITLE_STROKE_W=max(1, int(_clamp_int(variant.get("subtitle_stroke_width"), 0, 12, 3) * 0.5)),
        SUBTITLE_STROKE=str(variant.get("subtitle_stroke_color") or "#000000"),
        SUBTITLE_BASE_COLOR=base_color,
        SUBTITLE_MAX_WIDTH_FRAC=0.86,
        KARAOKE_INACTIVE_OPACITY=1.0,
        KARAOKE_ACTIVE_COLOR=highlight_color,
        _variation_profile_driven=True,
        _letterbox_enabled=bool(variant.get("letterbox_enabled", False)),
        _letterbox_top_frac=float(variant.get("letterbox_top_frac") or 0.0),
        _letterbox_bottom_frac=float(variant.get("letterbox_bottom_frac") or 0.0),
        _variant_subtitle_y_frac=_clamp_float(variant.get("subtitle_y_frac"), 0.08, 0.92, 0.84),
    )


def _preview_wrap_text(text: str, max_chars: int = 18) -> list[str]:
    rows: list[str] = []
    current = ""
    for word in str(text or "").split():
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > max_chars:
            rows.append(current)
            current = word
        else:
            current = candidate
    if current:
        rows.append(current)
    return rows


def _preview_dynamic_motion(
    animation: str,
    side: str,
    x: int,
    y: int,
    slide: int,
    progress: str,
) -> tuple[str, str]:
    if animation in {"fade_up", "slide_up"}:
        distance = 14 if animation == "slide_up" else 9
        return str(x), f"{y}+{distance}*(1-({progress}))"
    if animation == "staggered_reveal":
        return str(x), str(y)
    distance = max(8, slide // 2) if animation == "wipe" else slide
    direction = "+" if side == "right" else "-"
    return f"{x}{direction}{distance}*(1-({progress}))", str(y)


def _append_preview_dynamic_filters(
    filters: list[str],
    variant: dict[str, Any],
    index: int,
    preview_content: dict[str, Any],
    *,
    headline_font: str,
    caption_font: str,
    check_font: str,
    font_color: str,
    highlight: str,
) -> None:
    if str(variant.get("dynamic_text_mode") or "balanced") == "off":
        return
    from dynamic_text import dynamic_text_typography, resolve_dynamic_text_layout

    enabled_roles = _normalize_dynamic_text_roles(
        variant.get("dynamic_text_roles", DYNAMIC_TEXT_ROLES)
    )
    information_cycle = ("ingredients", "benefits", "usage")
    selected_content_role = next(
        (
            information_cycle[(index + offset) % len(information_cycle)]
            for offset in range(len(information_cycle))
            if information_cycle[(index + offset) % len(information_cycle)] in enabled_roles
        ),
        "",
    )
    role_lines = preview_content.get("role_lines")
    if not isinstance(role_lines, dict):
        legacy_lines = list(
            preview_content.get("lines") or ["[INGREDIENT]", "[BENEFIT]", "[CARA PAKAI]"]
        )
        role_lines = {
            role: [legacy_lines[role_index]] if role_index < len(legacy_lines) else []
            for role_index, role in enumerate(information_cycle)
        }
    preview_items = []
    dynamic_settings = variant.get("dynamic_text_settings")
    dynamic_settings = dynamic_settings if isinstance(dynamic_settings, dict) else {}
    if selected_content_role:
        selected_lines = list(role_lines.get(selected_content_role) or [])[:2]
        role = {
            "ingredients": "checklist",
            "benefits": "fact_badge",
            "usage": "usage_step",
        }[selected_content_role]
        role_duration = float((dynamic_settings.get(selected_content_role) or {}).get("duration_seconds") or 2.6)
        preview_items.append({
            "role": role,
            "content_role": selected_content_role,
            "headline": {
                "ingredients": "KANDUNGAN UTAMA:",
                "benefits": "FUNGSI PRODUK:",
                "usage": "CARA PAKAI:",
            }[selected_content_role],
            "text": selected_lines[0] if role == "usage_step" and selected_lines else "",
            "lines": selected_lines[1:2] if role == "usage_step" else selected_lines,
            "reveal_offsets": [0.0, 0.22],
            "start": 2.35,
            "end": min(5.70, 2.35 + role_duration),
        })
    if "cta" in enabled_roles:
        cta_duration = float((dynamic_settings.get("cta") or {}).get("duration_seconds") or 1.3)
        preview_items.append({
            "role": "closing_cta",
            "content_role": "cta",
            "text": "WORTH DICOBA?",
            "start": max(2.35, 5.88 - cta_duration),
            "end": 5.88,
        })

    items = resolve_dynamic_text_layout(
        preview_items,
        clip_id="variation-preview",
        variant_index=index,
        subtitle_position=str(variant.get("subtitle_position") or "bottom"),
        letterbox_top_frac=float(variant.get("letterbox_top_frac") or 0.0),
        letterbox_bottom_frac=float(variant.get("letterbox_bottom_frac") or 0.0),
    )
    band_y = {"upper": 155, "middle": 305, "lower": 445}
    for item_index, item in enumerate(items):
        role = str(item.get("role") or "")
        start = float(item.get("start") or 0.0)
        end = float(item.get("end") or start)
        layout = item.get("layout") if isinstance(item.get("layout"), dict) else {}
        side = str(layout.get("side") or "left")
        base_y = band_y.get(str(layout.get("band") or "middle"), 305)
        margin = 24
        slide = 16
        max_width = int(360 * float(layout.get("max_width_ratio") or 0.40))
        column_x = 360 - margin - max_width if side == "right" else margin
        headline_text = str(item.get("headline") or "")
        if role == "closing_cta":
            headline_text = str(item.get("text") or headline_text)
        content_role = str(item.get("content_role") or ("cta" if role == "closing_cta" else ""))
        role_setting = dynamic_settings.get(content_role)
        role_setting = role_setting if isinstance(role_setting, dict) else {}
        role_headline_font = _drawtext_font_arg(
            str(role_setting.get("headline_font_id") or "")
        ) or headline_font
        role_body_font = _drawtext_font_arg(
            str(role_setting.get("body_font_id") or "")
        ) or caption_font
        animation = str(role_setting.get("animation") or "current")
        font_scale = float(layout.get("font_scale") or 0.94)
        typography = dynamic_text_typography(640, role, font_scale, role_setting.get("font_size"))
        headline_size = typography["headline"]
        body_size = typography["body"]
        headline_chars = max(9, int(max_width / max(1.0, headline_size * 0.56)))
        body_chars = max(10, int(max_width / max(1.0, body_size * 0.55)))
        headline_rows = _preview_wrap_text(headline_text, headline_chars)
        headline_progress = f"min(1,max(0,(t-{start:.2f})/0.18))"
        for row_index, row in enumerate(headline_rows):
            row_y = base_y + row_index * max(18, headline_size + 3)
            x_expr, y_expr = _preview_dynamic_motion(
                animation, side, column_x, row_y, slide, headline_progress
            )
            filters.append(
                "drawtext="
                f"text='{_escape_drawtext(row)}'{role_headline_font}:fontsize={headline_size}:"
                f"fontcolor={highlight}:borderw=1:bordercolor=black:shadowcolor=black@0.65:shadowx=1:shadowy=1:"
                f"x='{x_expr}':y='{y_expr}':alpha='{headline_progress}':"
                f"enable='between(t,{start:.2f},{end:.2f})'"
            )
        if role == "closing_cta":
            continue
        headline_line_height = max(18, headline_size + 3)
        body_line_height = max(15, body_size + 4)
        row_y = base_y + len(headline_rows) * headline_line_height + 6
        body_values = (
            list(item.get("lines", []) or [])
            if role == "checklist"
            else ([str(item.get("text") or "")] if str(item.get("text") or "").strip() else [])
            + list(item.get("lines", []) or [])[:1]
        )
        for fact_index, fact in enumerate(body_values):
            fact_progress = f"min(1,max(0,(t-{start + 0.20 + fact_index * 0.22:.2f})/0.18))"
            wrapped = _preview_wrap_text(str(fact), body_chars)
            for row_index, row in enumerate(wrapped):
                current_y = row_y
                text_indent = body_size + 4 if role == "checklist" else 0
                if role == "checklist" and row_index == 0:
                    check_x = str(column_x)
                    filters.append(
                        "drawtext="
                        f"text='{_escape_drawtext(chr(0x2713))}'{check_font}:fontsize={body_size + 3}:fontcolor=#32D583:"
                        f"borderw=1:bordercolor=black:x='{check_x}':y={current_y}:alpha='{fact_progress}':"
                        f"enable='between(t,{start + 0.20 + fact_index * 0.22:.2f},{end:.2f})'"
                    )
                text_x, text_y = _preview_dynamic_motion(
                    animation,
                    side,
                    column_x + text_indent,
                    current_y,
                    slide,
                    fact_progress,
                )
                filters.append(
                    "drawtext="
                    f"text='{_escape_drawtext(row)}'{role_body_font}:fontsize={body_size}:fontcolor={font_color}:"
                    f"borderw=1:bordercolor=black:shadowcolor=black@0.65:shadowx=1:shadowy=1:"
                    f"x='{text_x}':y='{text_y}':alpha='{fact_progress}':"
                    f"enable='between(t,{start + 0.20 + fact_index * 0.22:.2f},{end:.2f})'"
                )
                row_y += body_line_height


def _render_preview_video(
    source: Path,
    output: Path,
    variant: dict[str, Any],
    index: int,
    preview_content: dict[str, Any] | None = None,
    cfg=None,
) -> None:
    preview_content = preview_content or {}
    subtitle_ass_path: str | None = None
    subtitle_fonts_dir: str | None = None
    if variant.get("subtitle_enabled", True):
        from ffmpeg_editor import _write_ass_file

        subtitle_words = _preview_subtitle_words()
        subtitle_ass_path, subtitle_fonts_dir = _write_ass_file(
            subtitle_words,
            [None] * len(subtitle_words),
            6.0,
            360,
            640,
            _preview_subtitle_cfg(cfg, variant),
        )
    filters = [
        "setpts=PTS-STARTPTS",
        "scale=360:640:force_original_aspect_ratio=increase",
        "crop=360:640",
        _grade_filter(str(variant.get("color_grade") or "original")),
    ]
    filters = [item for item in filters if item]
    if variant.get("mirror_enabled", False):
        filters.append("hflip")
    top_h = 0
    bottom_h = 0
    if variant.get("letterbox_enabled"):
        top_h = int(640 * _clamp_float(variant.get("letterbox_top_frac"), 0.0, 0.40, LETTERBOX_BAR_HEIGHT_FRAC))
        bottom_h = int(640 * _clamp_float(variant.get("letterbox_bottom_frac"), 0.0, 0.40, LETTERBOX_BAR_HEIGHT_FRAC))
        if top_h > 0:
            filters.append(f"drawbox=x=0:y=0:w=iw:h={top_h}:color=black@1:t=fill")
        if bottom_h > 0:
            filters.append(f"drawbox=x=0:y=ih-{bottom_h}:w=iw:h={bottom_h}:color=black@1:t=fill")
        hook_text = "AUTO HOOK TEXT"
        if top_h > 0 and variant.get("letterbox_hook_enabled"):
            raw_hook_fs = max(10, min(top_h, int(_clamp_int(
                variant.get("letterbox_hook_font_size"),
                LETTERBOX_HOOK_FONT_SIZE_RANGE[0],
                LETTERBOX_HOOK_FONT_SIZE_RANGE[1],
                72,
            ) * 0.33)))
            max_text_w = 360 * 0.94
            approx_w = max(1.0, len(hook_text) * raw_hook_fs * 0.78)
            hook_fs = max(10, int(raw_hook_fs * min(1.0, max_text_w / approx_w)))
            hook_x = _clamp_float(variant.get("letterbox_hook_x_frac"), 0.0, 1.0, 0.5)
            hook_y_frac = _clamp_float(variant.get("letterbox_hook_y_frac"), 0.0, 1.0, 0.5)
            hook_y = max(0, min(max(0, top_h - hook_fs), int((top_h * hook_y_frac) - (hook_fs / 2))))
            font_arg = _drawtext_font_arg(str(variant.get("letterbox_hook_font_id") or variant.get("font_id") or ""))
            filters.append(
                "drawtext="
                f"text='{_escape_drawtext(hook_text.upper())}'{font_arg}:fontsize={hook_fs}:"
                f"fontcolor={_ffmpeg_color(str(variant.get('letterbox_hook_font_color') or '#FFFFFF'))}:"
                "borderw=2:bordercolor=black:"
                f"x=(w-text_w)*{hook_x:.3f}:y={hook_y}"
            )

    hook_y = 52 if variant.get("subtitle_position") != "top" else 120
    if top_h > 0 and variant.get("letterbox_hook_enabled"):
        hook_y = max(hook_y, top_h + 16)
    font_color = _ffmpeg_color(str(variant.get("font_color") or "#FFFFFF"))
    highlight = _ffmpeg_color(str(variant.get("highlight_color") or "#FFD600"))
    headline_font = _drawtext_font_arg(str(variant.get("headline_font_id") or variant.get("font_id") or ""))
    subtitle_font = _drawtext_font_arg(str(variant.get("font_id") or ""))
    caption_font = _drawtext_font_arg(str(variant.get("caption_font_id") or variant.get("font_id") or ""))
    check_font = _drawtext_font_arg(r"C:\Windows\Fonts\seguisym.ttf")
    text_style_id = str(variant.get("text_style_id") or "current")
    label = _escape_drawtext(str(preview_content.get("hook") or "INFO PRODUK"))
    headline_fs = 32 if text_style_id == "creator_bold_pop" else 29
    headline_animation = str(variant.get("headline_animation") or "current")
    headline_shadow = _ffmpeg_color(str(variant.get("headline_shadow_color") or "#000000"))
    headline_shadow_x = _clamp_int(variant.get("headline_shadow_x"), -20, 20, 3)
    headline_shadow_y = _clamp_int(variant.get("headline_shadow_y"), -20, 20, 3)
    headline_stroke = max(1, int(_clamp_int(variant.get("headline_stroke_width"), 0, 12, 5) * 0.55))
    for segment_fs, segment_start, segment_end, alpha_and_y in _preview_headline_segments(headline_animation, headline_fs):
        if "|" in alpha_and_y:
            headline_alpha, headline_y_expr = alpha_and_y.split("|", 1)
        else:
            headline_alpha, headline_y_expr = alpha_and_y, "52"
        filters.append(
            "drawtext="
            f"text='{label}'{headline_font}:fontsize={segment_fs}:fontcolor={font_color}:"
            f"borderw={headline_stroke}:bordercolor=black:shadowcolor={headline_shadow}:"
            f"shadowx={max(-10, min(10, int(headline_shadow_x * 0.55)))}:"
            f"shadowy={max(-10, min(10, int(headline_shadow_y * 0.55)))}:"
            f"x=(w-text_w)/2:y='{headline_y_expr if hook_y == 52 else hook_y}':alpha='{headline_alpha}':"
            f"enable='between(t,{segment_start:.2f},{segment_end:.2f})'"
        )
    _append_preview_dynamic_filters(
        filters,
        variant,
        index,
        preview_content,
        headline_font=headline_font,
        caption_font=caption_font,
        check_font=check_font,
        font_color=font_color,
        highlight=highlight,
    )
    if subtitle_ass_path:
        from ffmpeg_editor import _escape_ass_filter_path

        subtitle_filter = f"ass={_escape_ass_filter_path(subtitle_ass_path)}"
        if subtitle_fonts_dir:
            subtitle_filter += f":fontsdir={_escape_ass_filter_path(subtitle_fonts_dir)}"
        filters.append(subtitle_filter)
    filters.append(
        "drawtext="
        f"text='V{index + 1}'{subtitle_font}:fontsize=13:fontcolor={highlight}:borderw=1:bordercolor=black:"
        "x=w-text_w-12:y=h-text_h-12"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        "1.0",
        "-stream_loop",
        "-1",
        "-i",
        str(source),
        "-t",
        "6.0",
        "-an",
        "-vf",
        ",".join(filters),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "24",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    finally:
        if subtitle_ass_path:
            Path(subtitle_ass_path).unlink(missing_ok=True)
    if result.returncode != 0:
        raise VariationProfileError(f"Preview render failed: {(result.stderr or '')[-300:]}")


def _find_latest_rendered_clip(cfg) -> Path | None:
    output_root = Path(str(getattr(cfg, "OUTPUT_DIR", r"D:\output_clips") or r"D:\output_clips"))
    if not output_root.exists():
        return None
    fallback: Path | None = None
    for manifest in sorted(output_root.glob("*/manifest.json"), key=lambda path: path.stat().st_mtime_ns, reverse=True):
        try:
            rows = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        base = manifest.parent
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("status") or "").casefold() not in {"ok", "skipped", "filtered_low_score", "filtered_low_variant"}:
                continue
            output_file = str(row.get("output_file") or row.get("clip_path") or "").strip()
            if not output_file:
                continue
            path = Path(output_file)
            if not path.is_absolute():
                path = base / path
            if path.exists() and path.suffix.lower() in _VIDEO_EXTS:
                resolved = path.resolve()
                if not bool(row.get("letterbox_enabled", False)):
                    return resolved
                if fallback is None:
                    fallback = resolved
    return fallback


def discover_fonts(cfg) -> list[dict[str, Any]]:
    seen: set[str] = set()
    fonts: list[dict[str, Any]] = []
    configured = [
        getattr(cfg, "FONT_SUBTITLE", ""),
        getattr(cfg, "FONT_HOOK", ""),
        *list(getattr(cfg, "FONT_HOOK_FALLBACKS", []) or []),
        *_FALLBACK_FONTS,
    ]
    for item in configured:
        _add_font_option(fonts, seen, str(item or ""))

    font_dir = Path(str(getattr(cfg, "SUBTITLE_FONT_DIR", "assets/fonts") or "assets/fonts"))
    if not font_dir.is_absolute():
        font_dir = Path.cwd() / font_dir
    if font_dir.exists():
        for path in sorted(font_dir.glob("*")):
            if path.suffix.lower() in {".ttf", ".otf", ".ttc"}:
                _add_font_option(fonts, seen, _relative_project_path(path))
    return fonts


def discover_bgm_tracks(cfg) -> list[dict[str, Any]]:
    bgm_dir = Path(str(getattr(cfg, "BGM_DIR", "assets/bgm") or "assets/bgm"))
    if not bgm_dir.is_absolute():
        bgm_dir = Path.cwd() / bgm_dir
    if not bgm_dir.exists():
        return []
    tracks = []
    for path in sorted(bgm_dir.iterdir()):
        if path.is_file() and path.suffix.lower() in _AUDIO_EXTS:
            tracks.append({"label": path.name, "path": _relative_project_path(path), "exists": True})
    return tracks


def _add_font_option(fonts: list[dict[str, Any]], seen: set[str], value: str) -> None:
    value = value.strip()
    if not value or value in seen:
        return
    seen.add(value)
    path = Path(value)
    exists = path.exists() if path.is_absolute() else (Path.cwd() / path).exists()
    label = Path(value).stem.replace("-", " ").replace("_", " ").strip() or value
    fonts.append({"id": value, "label": label, "path": value, "exists": bool(exists)})


def _revision_payload(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in profile.items()
        if key not in {"revision", "updated_at"}
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise VariationProfileError(f"Could not read variation profile: {exc}") from exc
    if not isinstance(payload, dict):
        raise VariationProfileError("Variation profile file must contain a JSON object.")
    return payload


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _choice(value: Any, options: tuple[str, ...], default: str) -> str:
    text = str(value or "").strip().casefold()
    return text if text in options else default


def _normalize_hook_type(value: Any, default: str) -> str:
    text = str(value or "").strip().casefold()
    if text in LEGACY_HOOK_TYPES:
        return "text"
    return text if text in HOOK_TYPES else default


def _normalize_visual_mode(value: Any, default: str = "host") -> str:
    text = str(value or "").strip().casefold()
    return text if text in VISUAL_MODES else default


def _normalize_dynamic_text_roles(value: Any) -> list[str]:
    if value is None:
        return list(DYNAMIC_TEXT_ROLES)
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",")]
    selected = {str(item or "").strip().casefold() for item in value or []}
    return [role for role in DYNAMIC_TEXT_ROLES if role in selected]


def _default_dynamic_text_settings(headline_font_id: str, body_font_id: str) -> dict[str, dict[str, Any]]:
    return {
        role: {
            "headline_font_id": headline_font_id,
            "body_font_id": body_font_id,
            "font_size": 50 if role == "cta" else 35,
            "animation": "current",
            "duration_seconds": 1.3 if role == "cta" else 2.6,
        }
        for role in DYNAMIC_TEXT_ROLES
    }


def _normalize_dynamic_text_settings(
    value: Any,
    *,
    font_ids: set[str],
    headline_font_id: str,
    body_font_id: str,
) -> dict[str, dict[str, Any]]:
    raw_settings = value if isinstance(value, dict) else {}
    defaults = _default_dynamic_text_settings(headline_font_id, body_font_id)
    normalized: dict[str, dict[str, Any]] = {}
    for role in DYNAMIC_TEXT_ROLES:
        raw = raw_settings.get(role)
        raw = raw if isinstance(raw, dict) else {}
        role_headline_font = str(raw.get("headline_font_id") or headline_font_id).strip()
        role_body_font = str(raw.get("body_font_id") or body_font_id).strip()
        if font_ids and role_headline_font not in font_ids:
            role_headline_font = headline_font_id
        if font_ids and role_body_font not in font_ids:
            role_body_font = body_font_id
        size_range = DYNAMIC_TEXT_CTA_SIZE_RANGE if role == "cta" else DYNAMIC_TEXT_BODY_SIZE_RANGE
        normalized[role] = {
            "headline_font_id": role_headline_font or headline_font_id,
            "body_font_id": role_body_font or body_font_id,
            "font_size": _clamp_int(
                raw.get("font_size"),
                size_range[0],
                size_range[1],
                int(defaults[role]["font_size"]),
            ),
            "animation": _choice(
                raw.get("animation"),
                DYNAMIC_TEXT_ANIMATIONS,
                str(defaults[role]["animation"]),
            ),
            "duration_seconds": round(
                _clamp_float(
                    raw.get("duration_seconds"),
                    DYNAMIC_TEXT_DURATION_RANGE[0],
                    DYNAMIC_TEXT_DURATION_RANGE[1],
                    float(defaults[role]["duration_seconds"]),
                ),
                1,
            ),
        }
    return normalized


def _hook_label(value: str) -> str:
    return {
        "none": "None",
        "text": "Text",
        "before_after_image": "Before/After",
        "text_before_after_image": "Text + Before/After",
        "b_roll": "B-roll",
        "text_b_roll": "Text + B-roll",
        "transitional_hook": "Transitional Hook",
    }.get(value, "Text")


def _visual_label(value: str) -> str:
    return {
        "host": "Host",
        "broll_audio": "Audio over B-roll",
    }.get(value, "Host")


def _hex(value: Any, default: str) -> str:
    text = str(value or "").strip()
    return text.upper() if _HEX_RE.match(text) else default


def _clean_label(value: Any, default: str) -> str:
    text = " ".join(str(value or "").split()).strip()
    return text[:48] if text else default


def _clamp_int(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(lo, min(hi, parsed))


def _clamp_float(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(lo, min(hi, parsed))


def _subtitle_y_for_position(position: str) -> float:
    return {
        "top": 0.34,
        "center": 0.58,
        "bottom": 0.84,
    }.get(str(position or "bottom").strip().casefold(), 0.84)


def _subtitle_size_pixels(value: str) -> int:
    return SUBTITLE_SIZE_PIXELS.get(str(value or "").strip().casefold(), SUBTITLE_SIZE_PIXELS["medium"])


def _apply_letterbox_defaults(variant: dict[str, Any]) -> None:
    enabled = bool(variant.get("letterbox_enabled", False))
    default = LETTERBOX_BAR_HEIGHT_FRAC if enabled else 0.0
    variant["letterbox_top_frac"] = _clamp_float(
        variant.get("letterbox_top_frac"),
        LETTERBOX_FRAC_RANGE[0],
        LETTERBOX_FRAC_RANGE[1],
        default,
    )
    variant["letterbox_bottom_frac"] = _clamp_float(
        variant.get("letterbox_bottom_frac"),
        LETTERBOX_FRAC_RANGE[0],
        LETTERBOX_FRAC_RANGE[1],
        default,
    )


def _apply_letterbox_hook_defaults(variant: dict[str, Any], default_font: str) -> None:
    variant["subtitle_size"] = _choice(variant.get("subtitle_size"), SUBTITLE_SIZES, "medium")
    variant["letterbox_hook_enabled"] = bool(variant.get("letterbox_hook_enabled", False))
    variant["letterbox_hook_font_id"] = str(variant.get("letterbox_hook_font_id") or default_font or "").strip()
    variant["letterbox_hook_font_color"] = _hex(variant.get("letterbox_hook_font_color"), "#FFFFFF")
    variant["letterbox_hook_font_size"] = _clamp_int(
        variant.get("letterbox_hook_font_size"),
        LETTERBOX_HOOK_FONT_SIZE_RANGE[0],
        LETTERBOX_HOOK_FONT_SIZE_RANGE[1],
        72,
    )
    variant["letterbox_hook_x_frac"] = _clamp_float(
        variant.get("letterbox_hook_x_frac"),
        LETTERBOX_HOOK_POSITION_RANGE[0],
        LETTERBOX_HOOK_POSITION_RANGE[1],
        0.5,
    )
    variant["letterbox_hook_y_frac"] = _clamp_float(
        variant.get("letterbox_hook_y_frac"),
        LETTERBOX_HOOK_POSITION_RANGE[0],
        LETTERBOX_HOOK_POSITION_RANGE[1],
        0.5,
    )


def _safe_identifier(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")[:80]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _relative_project_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve())).replace("\\", "/")
    except ValueError:
        return str(path.resolve())


def _grade_filter(name: str) -> str:
    return {
        "warm": "colortemperature=temperature=7500",
        "cool": "colortemperature=temperature=5000",
        "vivid": "eq=saturation=1.35:contrast=1.08",
        "desaturated": "eq=saturation=0.65:contrast=1.02",
        "cinematic": "eq=saturation=0.85:contrast=1.15:brightness=-0.02",
    }.get(name, "")


def _ffmpeg_color(value: str) -> str:
    return value if _HEX_RE.match(value) else "white"


def _drawtext_font_arg(value: str) -> str:
    if not value:
        return ""
    path = Path(value)
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        return ""
    return f":fontfile='{_escape_drawtext_path(path)}'"


def _escape_drawtext_path(path: Path) -> str:
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def _escape_drawtext(text: str) -> str:
    return str(text or "").replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _quote_artifact_path(path: Path) -> str:
    from urllib.parse import quote

    return quote(str(path), safe="")
