# =============================================================================
#  variation_engine.py — Clip variation generator for PROYA Clipper
#
#  Turns 1 raw clip moment into N styled variants using purely parameter-level
#  mutations (no re-transcription, no re-detection).
#
#  Variation axes (mix-and-match per variant):
#    1. Mirror / horizontal flip
#    2. Subtitle font + color palette
#    3. Subtitle Y position (top / mid / bottom)
#    4. Zoom timing offset  (+/- seconds from original trigger)
#    5. Zoom scale magnitude
#    6. Color grade (brightness / contrast / saturation via FFmpeg filter)
#    7. Speed ramp (0.9×, 1.0×, 1.1× — slight slow/fast)
#    8. Crop offset (re-frame slightly left/right within 9:16)
#    9. Hook text display (show / hide, different duration)
#   10. Karaoke active word highlight colour
#
#  Usage in main.py:
#    from variation_engine import expand_moments_with_variants
#    moments = expand_moments_with_variants(moments, cfg)
#    # then proceed with the normal clip-editing loop — each variant is its own job
# =============================================================================

from __future__ import annotations

import copy
import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("proya.variation")

_BROLL_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}


# ─────────────────────────────────────────────────────────────────────────────
#  STYLE PALETTE LIBRARY
#  Each palette defines a complete visual identity for one variant.
#  Add more palettes to increase variety without any code changes.
# ─────────────────────────────────────────────────────────────────────────────

SUBTITLE_PALETTES = [
    # name, font, active_color, inactive_opacity, stroke_color, stroke_w
    ("tiktok_classic",   "assets/fonts/Montserrat-ExtraBold.ttf",  "#FFD600", 1.0, "#000000", 3),
    ("tiktok_white",     "assets/fonts/Montserrat-ExtraBold.ttf",  "#FFFFFF", 0.6, "#000000", 4),
    ("neon_green",       "assets/fonts/Anton-Regular.ttf",         "#00FF7F", 1.0, "#003300", 3),
    ("hot_pink",         "assets/fonts/Anton-Regular.ttf",         "#FF2D78", 1.0, "#1A0008", 4),
    ("ice_blue",         "assets/fonts/Montserrat-ExtraBold.ttf",  "#00D4FF", 1.0, "#001A2E", 3),
    ("orange_punch",     "assets/fonts/Anton-Regular.ttf",         "#FF6B00", 1.0, "#1A0E00", 3),
    ("purple_glow",      "assets/fonts/Montserrat-ExtraBold.ttf",  "#C77DFF", 1.0, "#0D0020", 3),
    ("cream_soft",       "assets/fonts/Montserrat-ExtraBold.ttf",  "#FFF5D7", 0.7, "#2B1A00", 2),
    ("red_alarm",        "assets/fonts/Anton-Regular.ttf",         "#FF3B30", 1.0, "#000000", 4),
    ("playful_yellow",   "assets/fonts/PlayfairDisplay-Italic-VariableFont_wght.ttf", "#FFE500", 1.0, "#222200", 3),
]

HOOK_PALETTES = [
    # name, color, stroke_color, stroke_w, fontsize_multiplier
    ("bold_white",  "white",   "black",   5,  1.0),
    ("bold_yellow", "#FFD600", "black",   5,  1.0),
    ("bold_pink",   "#FF2D78", "black",   4,  0.95),
    ("big_white",   "white",   "#333333", 6,  1.1),
    ("neon_cyan",   "#00D4FF", "black",   4,  1.0),
]

# Y-position presets (fraction of frame height)
SUBTITLE_Y_POSITIONS = [0.72, 0.78, 0.83, 0.88]

# Zoom scale variants
ZOOM_SCALES = [1.25, 1.35, 1.45, 1.55, 1.65]

# Zoom timing offsets in seconds (applied to the detected trigger time)
ZOOM_OFFSETS = [-1.5, -1.0, -0.5, 0.0, 0.5, 1.0]

# Speed ramp multipliers
SPEED_RAMPS = [0.90, 0.95, 1.00, 1.05, 1.10]

# Color grade presets (FFmpeg vf filter strings)
COLOR_GRADES = [
    ("natural",    ""),                                                           # no filter
    ("vivid",      "eq=saturation=1.3:contrast=1.05"),
    ("warm",       "colortemperature=temperature=7500"),
    ("cool",       "colortemperature=temperature=5000"),
    ("bright",     "eq=brightness=0.05:contrast=1.1"),
    ("cinematic",  "eq=saturation=0.85:contrast=1.15:brightness=-0.02"),
    ("punch",      "eq=saturation=1.5:contrast=1.2"),
    ("matte",      "eq=saturation=0.7:contrast=0.95:brightness=0.03"),
]

# Crop offset as fraction of width (shifts the 9:16 reframe left/right)
CROP_X_OFFSETS = [-0.04, -0.02, 0.0, 0.02, 0.04]


# ─────────────────────────────────────────────────────────────────────────────
#  VARIANT CONFIG DATACLASS
#  A VariantConfig patches cfg values at render time.
#  It's passed as a thin override layer on top of the main config.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class VariantConfig:
    """Per-variant style overrides. All fields are optional."""
    variant_id: str = ""
    variant_index: int = 0          # 0 = original, 1+ = variant

    # Mirror
    mirror: bool = False

    # Subtitles
    font_subtitle: str = ""
    karaoke_active_color: str = ""
    karaoke_inactive_opacity: float = 1.0
    subtitle_stroke: str = "#000000"
    subtitle_stroke_w: int = 3
    subtitle_y_pos: float = 0.80

    # Hook
    hook_color: str = "white"
    hook_stroke_color: str = "black"
    hook_stroke_w: int = 5
    hook_fontsize_mult: float = 1.0
    hook_duration: float = 0.0     # 0 = disabled

    # Zoom
    zoom_scale: float = 1.45
    zoom_trigger_offset: float = 0.0   # seconds relative to detected trigger

    # Speed (1.0 = no change; applies during FFmpeg raw cut)
    speed_ramp: float = 1.0

    # Color grade (empty string = no filter)
    color_grade_filter: str = ""

    # Crop X offset
    crop_x_offset: float = 0.0

    # Optional intro B-roll used behind the opening hook text
    broll_intro_enabled: bool = False
    broll_intro_path: str = ""
    broll_intro_duration: float = 0.0
    broll_intro_product: str = ""


def apply_variant_to_cfg(base_cfg, variant: VariantConfig):
    """
    Return a lightweight object that looks like cfg but with variant overrides.
    Does NOT mutate base_cfg. Uses __dict__ copy + override.
    """
    class PatchedCfg:
        pass

    patched = PatchedCfg()
    # Copy all base cfg attributes
    for k, v in vars(base_cfg).items():
        setattr(patched, k, v)
    # Also copy module-level attributes (config.py is a module, not a class)
    import types
    if isinstance(base_cfg, types.ModuleType):
        for k in dir(base_cfg):
            if not k.startswith("_"):
                setattr(patched, k, getattr(base_cfg, k))

    # Apply overrides
    if variant.font_subtitle:
        patched.FONT_SUBTITLE = variant.font_subtitle
    if variant.karaoke_active_color:
        patched.KARAOKE_ACTIVE_COLOR = variant.karaoke_active_color
    patched.KARAOKE_INACTIVE_OPACITY = variant.karaoke_inactive_opacity
    patched.SUBTITLE_STROKE = variant.subtitle_stroke
    patched.SUBTITLE_STROKE_W = variant.subtitle_stroke_w
    patched.SUBTITLE_Y_POS = variant.subtitle_y_pos
    patched.HOOK_COLOR = variant.hook_color
    patched.HOOK_STROKE_COLOR = variant.hook_stroke_color
    patched.HOOK_STROKE_W = variant.hook_stroke_w
    patched.HOOK_FONTSIZE = int(getattr(base_cfg, "HOOK_FONTSIZE", 130) * variant.hook_fontsize_mult)
    patched.HOOK_DURATION = variant.hook_duration
    patched.ZOOM_SCALE = variant.zoom_scale
    patched._zoom_trigger_offset = variant.zoom_trigger_offset  # read by edit_clip
    patched._mirror = variant.mirror
    patched._speed_ramp = variant.speed_ramp
    patched._color_grade_filter = variant.color_grade_filter
    patched._crop_x_offset = variant.crop_x_offset
    patched._variant_id = variant.variant_id
    patched._variant_index = variant.variant_index
    patched._broll_intro_enabled = variant.broll_intro_enabled
    patched._broll_intro_path = variant.broll_intro_path
    patched._broll_intro_duration = variant.broll_intro_duration
    patched._broll_intro_product = variant.broll_intro_product

    return patched


# ─────────────────────────────────────────────────────────────────────────────
#  VARIANT GENERATION
# ─────────────────────────────────────────────────────────────────────────────

def _draw_style_pairs(
    count: int,
    rng: random.Random,
) -> list[tuple[tuple[Any, ...], tuple[Any, ...]]]:
    """
    Draw subtitle/hook style pairs without repeating visible identities first.

    The common six-variant setup needs five mutated versions. There are enough
    subtitle palettes and hook palettes to make those all visibly distinct, so
    do not let random choice burn render time on duplicate identities.
    """
    if count <= 0:
        return []
    if not SUBTITLE_PALETTES:
        raise ValueError("SUBTITLE_PALETTES must contain at least one palette")
    if not HOOK_PALETTES:
        raise ValueError("HOOK_PALETTES must contain at least one palette")

    pairs = []
    seen = set()
    round_index = 0
    max_unique = len(SUBTITLE_PALETTES) * len(HOOK_PALETTES)
    warned_reuse = False

    while len(pairs) < count:
        if len(seen) >= max_unique:
            if not warned_reuse:
                log.warning(
                    "Requested %s mutated variants but only %s unique "
                    "subtitle/hook identities exist; reusing identities after "
                    "the full style matrix is exhausted.",
                    count,
                    max_unique,
                )
                warned_reuse = True
            seen.clear()
            round_index = 0

        subtitle_order = list(SUBTITLE_PALETTES)
        hook_order = list(HOOK_PALETTES)
        rng.shuffle(subtitle_order)
        rng.shuffle(hook_order)

        made_progress = False
        for subtitle_pos, subtitle in enumerate(subtitle_order):
            if len(pairs) >= count:
                break

            for hook_offset in range(len(hook_order)):
                hook = hook_order[
                    (subtitle_pos + round_index + hook_offset) % len(hook_order)
                ]
                style_key = (subtitle[0], hook[0])
                if style_key in seen:
                    continue

                pairs.append((subtitle, hook))
                seen.add(style_key)
                made_progress = True
                break

        if not made_progress:
            seen.clear()
            round_index = 0
        else:
            round_index += 1

    return pairs


def _discover_broll_intro_assets(base_cfg) -> list[Path]:
    if not getattr(base_cfg, "BROLL_INTRO_ENABLED", True):
        return []

    broll_dir = Path(getattr(base_cfg, "BROLL_INTRO_DIR", "assets/broll_intro"))
    if not broll_dir.exists():
        return []

    exts = getattr(base_cfg, "BROLL_INTRO_VIDEO_EXTS", _BROLL_VIDEO_EXTS)
    exts = {str(ext).lower() for ext in exts}
    try:
        return sorted(
            p for p in broll_dir.iterdir()
            if p.is_file() and p.suffix.lower() in exts
        )
    except OSError as exc:
        log.warning(f"Could not read B-roll intro folder {broll_dir}: {exc}")
        return []


def _normalize_broll_product(text: str) -> str:
    normalized = re.sub(r"[^\w\s]", " ", str(text).lower(), flags=re.UNICODE)
    return " ".join(tok for tok in normalized.split() if tok)


def _contains_normalized_phrase(haystack: str, needle: str) -> bool:
    haystack = _normalize_broll_product(haystack)
    needle = _normalize_broll_product(needle)
    if not haystack or not needle:
        return False
    return f" {needle} " in f" {haystack} "


def _discover_broll_intro_assets_by_product(base_cfg) -> dict[str, list[Path]]:
    if not getattr(base_cfg, "BROLL_INTRO_ENABLED", True):
        return {}

    broll_dir = Path(getattr(base_cfg, "BROLL_INTRO_DIR", "assets/broll_intro"))
    if not broll_dir.exists():
        return {}

    exts = getattr(base_cfg, "BROLL_INTRO_VIDEO_EXTS", _BROLL_VIDEO_EXTS)
    exts = {str(ext).lower() for ext in exts}
    product_assets: dict[str, list[Path]] = {}
    try:
        for folder in sorted(p for p in broll_dir.iterdir() if p.is_dir()):
            assets = sorted(
                p for p in folder.iterdir()
                if p.is_file() and p.suffix.lower() in exts
            )
            if assets:
                product_key = _normalize_broll_product(folder.name)
                if product_key:
                    product_assets[product_key] = assets
    except OSError as exc:
        log.warning(f"Could not read product B-roll intro folders in {broll_dir}: {exc}")
    return product_assets


def _broll_intro_has_assets(base_cfg) -> bool:
    if _discover_broll_intro_assets(base_cfg):
        return True
    return any(_discover_broll_intro_assets_by_product(base_cfg).values())


def _broll_intro_rate_bounds(base_cfg) -> tuple[float, float]:
    def _rate(name: str, default: float) -> float:
        try:
            return float(getattr(base_cfg, name, default))
        except (TypeError, ValueError):
            return default

    lo = max(0.0, min(1.0, _rate("BROLL_INTRO_MIN_VARIANT_RATE", 0.20)))
    hi = max(0.0, min(1.0, _rate("BROLL_INTRO_MAX_VARIANT_RATE", 0.40)))
    if hi < lo:
        lo, hi = hi, lo
    return lo, hi


def _assign_broll_intro_variants(
    variants: list[VariantConfig],
    rng: random.Random,
    base_cfg,
) -> None:
    if not variants or not _broll_intro_has_assets(base_cfg):
        return

    root_assets = _discover_broll_intro_assets(base_cfg)
    allow_generic_root = (
        bool(getattr(base_cfg, "BROLL_INTRO_ALLOW_GENERIC_ROOT", False))
        or not bool(getattr(base_cfg, "BROLL_INTRO_REQUIRE_PRODUCT_MATCH", True))
    )

    candidate_indices = list(range(len(variants)))
    if not getattr(base_cfg, "BROLL_INTRO_APPLY_TO_ORIGINAL", False):
        candidate_indices = [idx for idx in candidate_indices if idx != 0]
    if not candidate_indices:
        return

    lo, hi = _broll_intro_rate_bounds(base_cfg)
    if hi <= 0.0:
        return

    target_rate = rng.uniform(lo, hi)
    target_count = int(round(len(variants) * target_rate))
    if target_count <= 0 and len(variants) >= 3:
        target_count = 1
    target_count = min(target_count, len(candidate_indices))
    if target_count <= 0:
        return

    try:
        intro_duration = float(getattr(base_cfg, "BROLL_INTRO_MAX_DURATION", 2.5))
    except (TypeError, ValueError):
        intro_duration = 2.5
    intro_duration = max(0.0, intro_duration)

    for idx in sorted(rng.sample(candidate_indices, target_count)):
        variant = variants[idx]
        variant.broll_intro_enabled = True
        variant.broll_intro_duration = intro_duration
        if root_assets and allow_generic_root:
            variant.broll_intro_path = str(rng.choice(root_assets))
        if "_broll" not in variant.variant_id:
            variant.variant_id = f"{variant.variant_id}_broll"


def generate_variants(base_cfg, n_variants: int, seed: int | None = None) -> list[VariantConfig]:
    """
    Generate `n_variants` VariantConfig objects.

    Variant 0 is always the "original" (no mutations) so the base clip is
    always produced. Variants 1..N each randomly sample from the axes above,
    using a deterministic seed so re-runs produce the same set.

    Args:
        base_cfg:    The main config module/object.
        n_variants:  Total variants including original (so 1 = no extra variants).
        seed:        RNG seed for reproducibility.

    Returns:
        List of VariantConfig objects, length == n_variants.
    """
    rng = random.Random(seed)
    variants = []
    style_pairs = _draw_style_pairs(max(0, n_variants - 1), rng)

    hook_dur_base = getattr(base_cfg, "HOOK_DURATION", 0.0)

    for i in range(n_variants):
        if i == 0:
            # Original — keep everything default
            vc = VariantConfig(
                variant_id="v0_original",
                variant_index=0,
                zoom_scale=getattr(base_cfg, "ZOOM_SCALE", 1.45),
                subtitle_y_pos=getattr(base_cfg, "SUBTITLE_Y_POS", 0.80),
                font_subtitle=getattr(base_cfg, "FONT_SUBTITLE", ""),
                karaoke_active_color=getattr(base_cfg, "KARAOKE_ACTIVE_COLOR", "#FFD600"),
                karaoke_inactive_opacity=getattr(base_cfg, "KARAOKE_INACTIVE_OPACITY", 1.0),
                hook_duration=hook_dur_base,
            )
        else:
            subtitle_palette, hook_palette = style_pairs[i - 1]
            palette_name, font, active_color, inactive_op, stroke_c, stroke_w = subtitle_palette
            hook_name, hook_col, hook_stroke_c, hook_stroke_w, hook_fs_mult = hook_palette

            vc = VariantConfig(
                variant_id=f"v{i}_{palette_name}_{hook_name}",
                variant_index=i,
                mirror=rng.random() < 0.30,                      # 30% chance flip
                font_subtitle=font,
                karaoke_active_color=active_color,
                karaoke_inactive_opacity=inactive_op,
                subtitle_stroke=stroke_c,
                subtitle_stroke_w=stroke_w,
                subtitle_y_pos=rng.choice(SUBTITLE_Y_POSITIONS),
                hook_color=hook_col,
                hook_stroke_color=hook_stroke_c,
                hook_stroke_w=hook_stroke_w,
                hook_fontsize_mult=hook_fs_mult,
                hook_duration=hook_dur_base,
                zoom_scale=rng.choice(ZOOM_SCALES),
                zoom_trigger_offset=rng.choice(ZOOM_OFFSETS),
                speed_ramp=rng.choice(SPEED_RAMPS),
                color_grade_filter=rng.choice(COLOR_GRADES)[1],
                crop_x_offset=rng.choice(CROP_X_OFFSETS),
            )

        variants.append(vc)

    _assign_broll_intro_variants(variants, rng, base_cfg)

    broll_count = sum(1 for variant in variants if variant.broll_intro_enabled)
    if broll_count:
        log.info(f"B-roll intro selected for {broll_count}/{len(variants)} variant configs")
    elif getattr(base_cfg, "BROLL_INTRO_ENABLED", True):
        log.debug("No B-roll intro assets found; variants render without intro pre-roll")

    log.info(f"Generated {len(variants)} variant configs (seed={seed})")
    return variants


def _broll_intro_alias_map(base_cfg, product_assets: dict[str, list[Path]]) -> dict[str, set[str]]:
    aliases: dict[str, set[str]] = {key: {key} for key in product_assets}

    for cls_name in getattr(base_cfg, "PRODUCT_CLASSES", {}).values():
        key = _normalize_broll_product(cls_name)
        if key in aliases:
            aliases[key].add(str(cls_name))

    configured_aliases = getattr(base_cfg, "BROLL_INTRO_PRODUCT_ALIASES", {})
    if isinstance(configured_aliases, dict):
        for product_name, product_aliases in configured_aliases.items():
            key = _normalize_broll_product(product_name)
            if key not in aliases:
                continue
            aliases[key].add(str(product_name))
            if isinstance(product_aliases, str):
                aliases[key].add(product_aliases)
            else:
                try:
                    aliases[key].update(str(alias) for alias in product_aliases)
                except TypeError:
                    pass

    return aliases


def _iter_broll_alias_phrases(aliases: dict[str, set[str]]) -> list[tuple[str, str, str]]:
    entries = []
    for key, phrases in aliases.items():
        for phrase in phrases:
            phrase_norm = _normalize_broll_product(phrase)
            if phrase_norm:
                entries.append((key, str(phrase), phrase_norm))
    return sorted(entries, key=lambda item: len(item[2]), reverse=True)


def _moment_search_text(moment: dict) -> str:
    parts = [
        moment.get("product", ""),
        moment.get("hook", ""),
        moment.get("reason", ""),
        moment.get("selected_text", ""),
    ]
    hook_overlay = moment.get("hook_overlay")
    if isinstance(hook_overlay, dict):
        parts.extend(str(hook_overlay.get(key, "")) for key in ("headline", "subtext", "cta"))

    for segment in moment.get("segments", []) or []:
        if isinstance(segment, dict):
            parts.append(str(segment.get("text", "")))
        else:
            parts.append(str(segment))

    return " ".join(str(part or "") for part in parts)


def _resolve_broll_product_key(
    moment: dict,
    base_cfg,
    product_assets: dict[str, list[Path]],
) -> str:
    if not product_assets:
        return ""

    aliases = _broll_intro_alias_map(base_cfg, product_assets)
    product_text = str(moment.get("product", "") or "")
    product_norm = _normalize_broll_product(product_text)
    generic_products = {"", "general", "unknown", "none", "null", "produk", "product"}

    if product_norm not in generic_products:
        for key in sorted(product_assets, key=len, reverse=True):
            if product_norm == key or _contains_normalized_phrase(product_norm, key):
                return key
        for key, _phrase, phrase_norm in _iter_broll_alias_phrases(aliases):
            if (
                product_norm == phrase_norm
                or _contains_normalized_phrase(product_norm, phrase_norm)
                or _contains_normalized_phrase(phrase_norm, product_norm)
            ):
                return key

    search_text = _moment_search_text(moment)
    for key, phrase, _phrase_norm in _iter_broll_alias_phrases(aliases):
        if _contains_normalized_phrase(search_text, phrase):
            return key

    return ""


def _assign_broll_intro_for_moment(
    variant: VariantConfig,
    moment: dict,
    base_cfg,
    seed: int,
    base_clip_id: str,
) -> None:
    if not variant.broll_intro_enabled:
        return

    product_assets = _discover_broll_intro_assets_by_product(base_cfg)
    product_key = _resolve_broll_product_key(moment, base_cfg, product_assets)
    choices = product_assets.get(product_key, []) if product_key else []
    allow_generic_root = (
        bool(getattr(base_cfg, "BROLL_INTRO_ALLOW_GENERIC_ROOT", False))
        or not bool(getattr(base_cfg, "BROLL_INTRO_REQUIRE_PRODUCT_MATCH", True))
    )

    if not choices and variant.broll_intro_path and allow_generic_root:
        variant.broll_intro_product = "generic"
        return

    if not choices and allow_generic_root:
        choices = _discover_broll_intro_assets(base_cfg)
        product_key = "generic" if choices else ""

    if not choices:
        variant.broll_intro_enabled = False
        variant.broll_intro_path = ""
        variant.broll_intro_product = ""
        if variant.variant_id.endswith("_broll"):
            variant.variant_id = variant.variant_id[:-6]
        return

    picker = random.Random(f"{seed}|{base_clip_id}|{variant.variant_id}|{product_key}")
    variant.broll_intro_path = str(picker.choice(choices))
    variant.broll_intro_product = product_key


# ─────────────────────────────────────────────────────────────────────────────
#  MOMENT EXPANSION
#  Takes the LLM moments list and clones each moment N times (one per variant).
#  Each clone carries variant metadata so the editor can apply the right style.
# ─────────────────────────────────────────────────────────────────────────────

def expand_moments_with_variants(
    moments: list[dict],
    base_cfg,
    n_variants: int | None = None,
    seed: int = 42,
) -> list[dict]:
    """
    Expand the moments list so each moment appears N times — once per variant.

    Args:
        moments:     Original moments from detect_moments().
        base_cfg:    Config module.
        n_variants:  How many variants per clip. Defaults to cfg.VARIANTS_PER_CLIP (or 4).
        seed:        RNG seed for variant generation.

    Returns:
        Expanded list with (len(moments) * n_variants) entries.
        Each entry has a "_variant" key containing the VariantConfig.
    """
    if n_variants is None:
        n_variants = getattr(base_cfg, "VARIANTS_PER_CLIP", 4)

    if n_variants <= 1:
        # No expansion — just tag every moment as v0_original
        for m in moments:
            m["_variant"] = VariantConfig(variant_id="v0_original", variant_index=0)
        return moments

    variants = generate_variants(base_cfg, n_variants, seed=seed)
    expanded = []

    for moment in moments:
        base_clip_id = moment.get("clip_id", "clip_unknown")
        for vc in variants:
            variant_for_moment = copy.deepcopy(vc)
            _assign_broll_intro_for_moment(
                variant_for_moment,
                moment,
                base_cfg,
                seed,
                str(base_clip_id),
            )
            m = copy.deepcopy(moment)
            m["_variant"] = variant_for_moment
            # Give variant its own clip_id so files don't collide
            m["clip_id"] = f"{base_clip_id}_{variant_for_moment.variant_id}"
            expanded.append(m)

    log.info(
        f"Expanded {len(moments)} moments × {n_variants} variants "
        f"= {len(expanded)} total clip jobs"
    )
    return expanded


# ─────────────────────────────────────────────────────────────────────────────
#  FFmpeg VARIANT HELPERS
#  Called inside cut_raw_clip (or a wrapper) to bake mirror, speed, grade, crop
#  into the raw cut stage (before MoviePy editing) for maximum throughput.
#  Python-level image ops (mirror) on MoviePy clips are slow; FFmpeg is fast.
# ─────────────────────────────────────────────────────────────────────────────

def build_ffmpeg_vf_chain(variant: VariantConfig | None, frame_w: int = 1080, frame_h: int = 1920) -> str:
    """
    Compose an FFmpeg -vf filter chain string for a variant.
    Returns empty string if no filters needed.

    Filters applied in order:
      1. crop offset (reframe X)
      2. scale back to target resolution
      3. hflip (mirror)
      4. setpts (speed ramp)
      5. color grade (eq / colortemperature)
    """
    if variant is None:
        return ""

    filters = []

    # 1. Crop X offset — shift horizontal slice before other transforms
    ox = getattr(variant, "crop_x_offset", 0.0)
    if abs(ox) > 0.005:
        # Crop a slightly narrower strip then scale back up
        crop_w = int(frame_w * (1.0 - abs(ox)))
        crop_x = int(frame_w * (ox if ox > 0 else 0))
        filters.append(f"crop={crop_w}:{frame_h}:{crop_x}:0")
        filters.append(f"scale={frame_w}:{frame_h}")

    # 2. Mirror
    if getattr(variant, "mirror", False):
        filters.append("hflip")

    # 3. Speed ramp — setpts changes presentation timestamps
    speed = getattr(variant, "speed_ramp", 1.0)
    if abs(speed - 1.0) > 0.02:
        pts = round(1.0 / speed, 4)
        filters.append(f"setpts={pts}*PTS")

    # 4. Color grade
    grade = getattr(variant, "color_grade_filter", "")
    if grade:
        filters.append(grade)

    return ",".join(filters) if filters else ""


def build_ffmpeg_atempo(speed: float) -> list[str]:
    """
    Build FFmpeg -af atempo arguments for audio speed matching.
    atempo only supports 0.5–2.0 per filter; chain filters for extremes.
    """
    if abs(speed - 1.0) <= 0.02:
        return []
    # clamp to reasonable range for TikTok
    speed = max(0.75, min(1.25, speed))
    return ["-af", f"atempo={round(speed, 4)}"]


def _probe_video_dimensions(input_video: str) -> tuple[int, int]:
    import json
    import subprocess

    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "json",
                input_video,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode == 0:
            payload = json.loads(r.stdout or "{}")
            stream = (payload.get("streams") or [{}])[0]
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
            if width > 0 and height > 0:
                return width, height
    except Exception as exc:
        log.warning(f"Could not probe source dimensions for variant cut: {exc}")
    return 1080, 1920


def cut_raw_clip_with_variant(
    input_video: str,
    start: float,
    end: float,
    output_path: str,
    variant,
    cfg,
) -> bool:
    import os
    import subprocess
    from pathlib import Path

    os.makedirs(Path(output_path).parent, exist_ok=True)

    # Guard: skip zero or negative duration clips
    duration = end - start
    if duration <= 0.5:
        log.error(f"Skipping clip with invalid duration: start={start} end={end}")
        return False

    raw_codec  = getattr(cfg, "RAW_CUT_CODEC", "h264_nvenc")
    raw_preset = getattr(cfg, "RAW_CUT_PRESET", "p1")

    # Use -ss AFTER -i (output seek) — slower but accurate, avoids empty output
    # Also add -avoid_negative_ts make_zero to handle edge cases
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{max(0.0, start):.3f}",   # input seek — BEFORE -i
        "-i", input_video,
        "-t", f"{duration:.3f}",
        "-c:v", raw_codec, "-preset", raw_preset,
        "-c:a", "aac", "-avoid_negative_ts", "make_zero",
    ]

    if raw_codec == "libx264":
        cmd += ["-crf", "28"]
    elif raw_codec.endswith("_nvenc"):
        cmd += ["-cq", str(getattr(cfg, "OUTPUT_CQ", 35))]

    # Variant filters
    if variant is not None:
        frame_w, frame_h = _probe_video_dimensions(input_video)
        vf = build_ffmpeg_vf_chain(variant, frame_w=frame_w, frame_h=frame_h)
        if vf:
            cmd += ["-vf", vf]
        af = build_ffmpeg_atempo(getattr(variant, "speed_ramp", 1.0))
        if af:
            cmd += af

    cmd.append(output_path)

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            log.error(f"FFmpeg variant cut error: {r.stderr[-300:]}")
            return False
        # Extra guard: check output file actually has content
        if Path(output_path).exists() and Path(output_path).stat().st_size < 1024:
            log.error(f"FFmpeg produced empty/tiny file (<1KB): {output_path}")
            Path(output_path).unlink(missing_ok=True)
            return False
        return True
    except subprocess.TimeoutExpired:
        log.error(f"FFmpeg timed out: {output_path}")
        return False
    except FileNotFoundError:
        raise RuntimeError("FFmpeg not found")

# ─────────────────────────────────────────────────────────────────────────────
#  THROUGHPUT MATH HELPER
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG ADDITIONS (paste these into config.py)
# ─────────────────────────────────────────────────────────────────────────────

SUGGESTED_CONFIG_ADDITIONS = """
# ── Variation Engine ──────────────────────────────────────────────────────────
# How many style variants to render per detected moment.
# 1 = no variation (just the original). 6 = 6x clip output.
# With a ~1h livestream → ~60 moments → 6 variants = ~360 clips.
# For 8–18k clips target across multiple VODs, set to 8–12.
VARIANTS_PER_CLIP = 6

# Seed for variant randomisation. Change to get a different style mix.
VARIANT_SEED = 42

# Whether to bake mirror/speed/grade into the FFmpeg raw cut (recommended True).
# False = these transforms are done in MoviePy (slower).
VARIANT_FFMPEG_BAKE = True
"""
