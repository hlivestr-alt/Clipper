from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 8
PREVIEW_RENDER_VERSION = 12
MIN_VARIANTS = 1
MAX_VARIANTS = 6

HOOK_TYPES = ("none", "text", "before_after_image", "text_before_after_image", "b_roll", "text_b_roll", "transitional_hook")
LEGACY_HOOK_TYPES = ("auto", "pain", "result", "curiosity", "value", "product_focus")
SUBTITLE_POSITIONS = ("top", "center", "bottom")
SUBTITLE_SIZES = ("small", "medium", "large")
SUBTITLE_SIZE_PIXELS = {"small": 96, "medium": 120, "large": 144}
COLOR_GRADES = ("original", "warm", "cool", "vivid", "desaturated", "cinematic")
BGM_MODES = ("auto", "none", "selected")
ZOOM_INTENSITIES = ("none", "subtle", "normal", "strong")
VISUAL_MODES = ("host", "broll_audio")
BEFORE_AFTER_MODES = ("fullscreen",)
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
        template["font_id"] = default_font
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
        "bgm_tracks": discover_bgm_tracks(cfg),
        "hook_types": list(HOOK_TYPES),
        "visual_modes": list(VISUAL_MODES),
        "before_after_modes": list(BEFORE_AFTER_MODES),
        "subtitle_positions": list(SUBTITLE_POSITIONS),
        "subtitle_sizes": list(SUBTITLE_SIZES),
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
    fonts = discover_fonts(cfg)
    font_ids = {item["id"] for item in fonts}
    default_font = fonts[min(index, len(fonts) - 1)]["id"] if fonts else _FALLBACK_FONTS[0]

    font_id = str(raw.get("font_id") or defaults.get("font_id") or default_font).strip()
    if font_ids and font_id not in font_ids:
        font_id = default_font
    elif not font_id:
        font_id = default_font

    bgm_tracks = {item["path"] for item in discover_bgm_tracks(cfg)}
    bgm_path = str(raw.get("bgm_path") or "").strip()
    bgm_mode = _choice(raw.get("bgm_mode"), BGM_MODES, defaults["bgm_mode"])
    if bgm_mode == "selected" and bgm_tracks and bgm_path not in bgm_tracks:
        bgm_path = ""
        bgm_mode = "auto"
    if bgm_mode != "selected":
        bgm_path = ""

    subtitle_position = _choice(raw.get("subtitle_position"), SUBTITLE_POSITIONS, defaults["subtitle_position"])
    subtitle_size = _choice(raw.get("subtitle_size"), SUBTITLE_SIZES, defaults.get("subtitle_size", "medium"))
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
        "font_id": font_id,
        "font_color": _hex(raw.get("font_color"), defaults["font_color"]),
        "highlight_color": _hex(raw.get("highlight_color"), defaults["highlight_color"]),
        "subtitle_position": subtitle_position,
        "subtitle_size": subtitle_size,
        "subtitle_y_frac": _clamp_float(
            raw.get("subtitle_y_frac"),
            SUBTITLE_Y_FRAC_RANGE[0],
            SUBTITLE_Y_FRAC_RANGE[1],
            _subtitle_y_for_position(subtitle_position),
        ),
        "color_grade": _choice(raw.get("color_grade"), COLOR_GRADES, defaults["color_grade"]),
        "bgm_mode": bgm_mode,
        "bgm_path": bgm_path,
        "sfx_enabled": bool(raw.get("sfx_enabled", defaults["sfx_enabled"])),
        "zoom_intensity": _choice(raw.get("zoom_intensity"), ZOOM_INTENSITIES, defaults["zoom_intensity"]),
        "product_zoom_enabled": bool(raw.get("product_zoom_enabled", defaults.get("product_zoom_enabled", True))),
        "subtitle_enabled": bool(raw.get("subtitle_enabled", defaults.get("subtitle_enabled", True))),
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


def generate_previews(cfg, payload: dict[str, Any], variant_index: int | None = None) -> dict[str, Any]:
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
    previews = []
    output = preview_root / f"{profile['revision'][:12]}_p{PREVIEW_RENDER_VERSION}_v{selected_index}.jpg"
    if not output.exists():
        _render_preview_image(source, output, variant, selected_index)
    previews.append({
        "variant_index": selected_index,
        "variant_name": variant["name"],
        "path": str(output),
        "url": f"/api/artifacts?path={_quote_artifact_path(output)}",
        "kind": "image",
        "exists": output.exists() and output.is_file(),
    })
    return {
        "profile_revision": profile["revision"],
        "source_clip": str(source),
        "preview_source": source_ref,
        "previews": previews,
        "message": "",
    }


def _render_preview_image(source: Path, output: Path, variant: dict[str, Any], index: int) -> None:
    filters = [
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
    subtitle_y = int(640 * _clamp_float(variant.get("subtitle_y_frac"), 0.08, 0.92, 0.84))
    subtitle_fs = max(16, int(_subtitle_size_pixels(str(variant.get("subtitle_size") or "medium")) * 0.20))
    font_color = _ffmpeg_color(str(variant.get("font_color") or "#FFFFFF"))
    highlight = _ffmpeg_color(str(variant.get("highlight_color") or "#FFD600"))
    visual_label = _visual_label(str(variant.get("visual_mode") or "host"))
    label = _escape_drawtext(
        f"V{index + 1} {_hook_label(str(variant.get('hook_type') or 'text')).upper()} / {visual_label.upper()}"
    )
    subtitle_label = "subtitles off" if not variant.get("subtitle_enabled", True) else f"{variant.get('subtitle_position', 'bottom')} subtitles"
    zoom_label = "product zoom off" if not variant.get("product_zoom_enabled", True) else str(variant.get("zoom_intensity", "normal"))
    sub = _escape_drawtext(f"{subtitle_label} / {zoom_label}")
    filters.append(
        "drawtext="
        f"text='{label}':fontsize=27:fontcolor={font_color}:borderw=3:bordercolor=black:"
        f"x=(w-text_w)/2:y={hook_y}"
    )
    filters.append(
        "drawtext="
        f"text='{sub}':fontsize=22:fontcolor={highlight}:borderw=2:bordercolor=black:"
        f"x=(w-text_w)/2:y={subtitle_y}"
    )
    if variant.get("subtitle_enabled", True):
        filters.append(
            "drawtext="
            f"text='SAMPLE SUBTITLE SIZE':fontsize={subtitle_fs}:fontcolor={font_color}:borderw=2:bordercolor=black:"
            f"x=(w-text_w)/2:y={max(0, subtitle_y - subtitle_fs - 8)}"
        )

    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        "1.0",
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-vf",
        ",".join(filters),
        "-q:v",
        "3",
        str(output),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
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
