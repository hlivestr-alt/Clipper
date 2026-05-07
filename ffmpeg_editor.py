# =============================================================================
#  ffmpeg_editor.py — Pure FFmpeg drop-in replacement for clip_editor.py
#
#  Drop-in: same public API as clip_editor.py
#    cut_raw_clip(input, start, end, output, cfg) -> bool
#    edit_clip(raw_clip_path, output_path, moment, clip_words,
#              product_events, cfg) -> bool
#    get_words_for_clip(all_words, clip_start, clip_end) -> list
#
#  In main.py, change ONE line:
#    from clip_editor import cut_raw_clip, edit_clip, get_words_for_clip
#  to:
#    from ffmpeg_editor import cut_raw_clip, edit_clip, get_words_for_clip
#
#  Speed vs MoviePy:
#    MoviePy  — decodes every frame to numpy in Python → ~8–15s per clip
#    FFmpeg   — GPU pipeline, never leaves NVENC       → ~1–3s per clip
#
#  Features implemented (identical output to clip_editor.py):
#    ✓ Karaoke subtitles (per-word color, 2-line grouping, active highlight)
#    ✓ Keyword highlight colours (yellow/green/red semantic categories)
#    ✓ Hook title text with background bar
#    ✓ Product zoom with cubic ease-in (zoompan filter)
#    ✓ Host face zoom (every N words)
#    ✓ Product caption (name + brand line above product)
#    ✓ Before/after image overlay with fade
#    ✓ Logo watermark
#    ✓ Mirror / hflip (from variation_engine)
#    ✓ Color grade (from variation_engine)
#    ✓ Speed ramp (from variation_engine)
#    ✓ Crop X offset (from variation_engine)
#    ✓ SFX audio mixing via amix+adelay
#    ✓ Optional BGM loop with voice ducking
#    ✓ Optional local B-roll intro under opening hook text for selected variants
#    ✓ NVENC hardware encode (h264_nvenc / hevc_nvenc)
# =============================================================================

from __future__ import annotations

import json
import logging
import os
import random
import re
import subprocess
import threading
import atexit
from pathlib import Path
from typing import Optional

from hook_text import ensure_hook_payload
from utils import _format_rupiah_compact

log = logging.getLogger("proya.ffmpeg_editor")

_AUDIO_EXTS = {".wav", ".mp3", ".ogg", ".aac", ".flac", ".m4a"}
_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}

# ── NVENC concurrency guard (max 3 simultaneous NVENC sessions) ──────────────
_NVENC_SEM = threading.Semaphore(3)

_HIGHLIGHT_CONFIG_LOCK = threading.Lock()
_HIGHLIGHT_CATEGORY_ORDER = ("benefit", "result", "pain")
_HIGHLIGHT_CATEGORY_ALIASES = {
    "attention benefits": "benefit",
    "attention_benefits": "benefit",
    "attention": "benefit",
    "benefit": "benefit",
    "benefits": "benefit",
    "yellow": "benefit",
    "result proof": "result",
    "result_proof": "result",
    "proof": "result",
    "result": "result",
    "results": "result",
    "green": "result",
    "pain problem": "pain",
    "pain_problem": "pain",
    "problem": "pain",
    "pain": "pain",
    "red": "pain",
}
_HIGHLIGHT_CONFIG_CACHE = {
    "path": None,
    "stamp": None,
    "payload": None,
    "dirty": False,
    "version": 0,
}
_HIGHLIGHT_MATCHER_CACHE: dict[tuple, "_HighlightMatcher"] = {}
_HIGHLIGHT_FLUSH_REGISTERED = False


class _HighlightMatcher:
    def __init__(self, rules: list[dict]) -> None:
        self.root: dict = {}
        for rule in rules:
            tokens = rule.get("tokens") or []
            if not tokens:
                continue
            node = self.root
            for token in tokens:
                node = node.setdefault(token, {})
            node.setdefault("_rules", []).append(
                {
                    "length": len(tokens),
                    "color": rule.get("color"),
                    "source_order": int(rule.get("source_order", 0)),
                }
            )

    def resolve_word_colors(self, karaoke_words: list) -> list[Optional[str]]:
        if not karaoke_words or not self.root:
            return [None] * len(karaoke_words)

        token_entries = []
        for word_idx, word_data in enumerate(karaoke_words):
            for token in _normalized_highlight_tokens(word_data.get("word", "")):
                token_entries.append({
                    "token": token,
                    "word_idx": word_idx,
                })

        if not token_entries:
            return [None] * len(karaoke_words)

        token_colors: list[Optional[str]] = [None] * len(token_entries)
        token_match_lengths = [0] * len(token_entries)
        token_source_orders = [10**12] * len(token_entries)
        total_tokens = len(token_entries)

        for start_idx in range(total_tokens):
            node = self.root
            token_idx = start_idx
            while token_idx < total_tokens:
                token = token_entries[token_idx]["token"]
                node = node.get(token)
                if node is None:
                    break

                for rule in node.get("_rules", []):
                    length = rule["length"]
                    source_order = rule["source_order"]
                    color = rule["color"]
                    end_idx = start_idx + length
                    for matched_idx in range(start_idx, end_idx):
                        current_len = token_match_lengths[matched_idx]
                        current_order = token_source_orders[matched_idx]
                        if length > current_len or (length == current_len and source_order < current_order):
                            token_match_lengths[matched_idx] = length
                            token_source_orders[matched_idx] = source_order
                            token_colors[matched_idx] = color

                token_idx += 1

        word_colors: list[Optional[str]] = [None] * len(karaoke_words)
        word_match_lengths = [0] * len(karaoke_words)
        word_source_orders = [10**12] * len(karaoke_words)
        for token_idx, entry in enumerate(token_entries):
            color = token_colors[token_idx]
            if not color:
                continue
            word_idx = entry["word_idx"]
            match_len = token_match_lengths[token_idx]
            source_order = token_source_orders[token_idx]
            if (
                match_len > word_match_lengths[word_idx]
                or (match_len == word_match_lengths[word_idx] and source_order < word_source_orders[word_idx])
            ):
                word_match_lengths[word_idx] = match_len
                word_source_orders[word_idx] = source_order
                word_colors[word_idx] = color

        return word_colors


# =============================================================================
#  PUBLIC API
# =============================================================================

def cut_raw_clip(
    input_video: str,
    start: float,
    end: float,
    output_path: str,
    cfg=None,
) -> bool:
    """
    Fast lossless-ish raw cut. Uses CPU libx264 ultrafast so NVENC slots are
    reserved for the final edit pass. Output-seek for frame accuracy.
    """
    duration = end - start
    if duration <= 0.5:
        log.error(f"Invalid clip duration {duration:.2f}s — skipping {output_path}")
        return False

    os.makedirs(Path(output_path).parent, exist_ok=True)

    # Use CPU for raw cuts — fast enough, keeps NVENC free for edits
    cmd = [
        "ffmpeg", "-y",
        "-i", input_video,
        "-ss", f"{max(0.0, start):.3f}",
        "-t",  f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-avoid_negative_ts", "make_zero",
        output_path,
    ]
    return _run_ffmpeg(cmd, output_path, timeout=180)


def edit_clip(
    raw_clip_path: str,
    output_path: str,
    moment: dict,
    clip_words: list,
    product_events: list,
    cfg,
) -> bool:
    """
    Full edit pass — hardcodes all overlays, zoom, subtitles into output video.
    Single FFmpeg invocation. GPU-accelerated via h264_nvenc.
    """
    if not Path(raw_clip_path).exists():
        log.error(f"Raw clip missing: {raw_clip_path}")
        return False

    # ── Probe raw clip dimensions & duration ─────────────────────────────────
    info = _probe_video(raw_clip_path)
    if not info:
        log.error(f"Could not probe: {raw_clip_path}")
        return False

    W, H = info["width"], info["height"]
    clip_duration = info["duration"]
    clip_fps = info.get("fps")

    os.makedirs(Path(output_path).parent, exist_ok=True)

    # ── Word corrections ──────────────────────────────────────────────────────
    try:
        from word_corrector import apply_corrections_to_subtitle_words
        clip_words = apply_corrections_to_subtitle_words(clip_words, cfg)
    except Exception:
        pass

    # ── Build ASS subtitle file ───────────────────────────────────────────────
    highlight_plan = _build_highlight_plan(clip_words, cfg, moment=moment)
    ass_path, ass_fonts_dir = _write_ass_file(
        highlight_plan["words"],
        highlight_plan["word_colors"],
        clip_duration,
        W,
        H,
        cfg,
    )

    # ── Plan zooms ────────────────────────────────────────────────────────────
    host_face_class = _normalize_product_name(getattr(cfg, "HOST_FACE_CLASS", "host_face"))
    allowed_product_classes = _allowed_product_class_map(cfg)
    face_events = [
        e for e in product_events
        if _normalize_product_name(e.get("class_name", "")) == host_face_class
    ]
    prod_events = [
        e for e in product_events
        if _normalize_product_name(e.get("class_name", "")) in allowed_product_classes
    ]

    hook_dur  = getattr(cfg, "HOOK_DURATION", 0.0)
    hook_end  = min(hook_dur, clip_duration * 0.4) if hook_dur > 0 else 0.0
    zoom_dur  = getattr(cfg, "ZOOM_DURATION", 3.0)
    zoom_scale = getattr(cfg, "ZOOM_SCALE", 1.45)

    prod_trigger = _find_zoom_trigger(clip_words, prod_events, hook_end, clip_duration, cfg)
    face_zooms   = _plan_face_zooms(clip_words, face_events, clip_duration,
                                    prod_trigger, hook_end, cfg)

    # ── Build extra image inputs (before/after, logo) ─────────────────────────
    extra_inputs = []  # list of {"path": str, "type": "ba"|"logo"}
    ba_enabled = getattr(cfg, "BEFORE_AFTER_ENABLED", False)
    ba_path = None
    if ba_enabled:
        ba_path = _pick_before_after(cfg)
        if ba_path:
            extra_inputs.append({"path": ba_path, "type": "ba"})

    emoji_overlays = _plan_emoji_overlays(clip_words, clip_duration, W, H, cfg)
    for emoji_overlay in emoji_overlays:
        extra_inputs.append({
            "path": emoji_overlay["path"],
            "type": "emoji",
            "overlay": emoji_overlay,
        })

    logo_path = getattr(cfg, "LOGO_PATH", None)
    if logo_path and Path(logo_path).exists():
        extra_inputs.append({"path": logo_path, "type": "logo"})

    # ── SFX events ────────────────────────────────────────────────────────────
    sfx_events = []
    if getattr(cfg, "SFX_ENABLED", False):
        try:
            from sfx_player import build_sfx_events
            sfx_events = build_sfx_events(
                clip_words=clip_words,
                highlight_words=highlight_plan["words"],
                highlight_word_colors=highlight_plan["word_colors"],
                clip_duration=clip_duration,
                product_zoom_start=prod_trigger["trigger_t"] if prod_trigger else None,
                cfg=cfg,
            )
        except Exception as e:
            log.debug(f"SFX build failed: {e}")

    bgm_path = _pick_bgm(cfg) if getattr(cfg, "BGM_ENABLED", False) else None

    # ── Assemble filter_complex + command ─────────────────────────────────────
    acquired = _NVENC_SEM.acquire(timeout=500)
    if not acquired:
        log.error("NVENC semaphore timeout")
        return False

    try:
        ok = _build_and_run(
            raw_clip_path=raw_clip_path,
            output_path=output_path,
            ass_path=ass_path,
            ass_fonts_dir=ass_fonts_dir,
            W=W, H=H,
            clip_duration=clip_duration,
            clip_fps=clip_fps,
            has_audio=info.get("has_audio", False),
            moment=moment,
            prod_trigger=prod_trigger,
            face_zooms=face_zooms,
            zoom_dur=zoom_dur,
            zoom_scale=zoom_scale,
            hook_end=hook_end,
            extra_inputs=extra_inputs,
            sfx_events=sfx_events,
            bgm_path=bgm_path,
            cfg=cfg,
        )
    finally:
        _NVENC_SEM.release()
        # Clean up temp ASS file
        if ass_path and Path(ass_path).exists():
            try:
                os.remove(ass_path)
            except Exception:
                pass

    return ok


def get_words_for_clip(all_words: list, clip_start: float, clip_end: float) -> list:
    """Identical to clip_editor.get_words_for_clip."""
    return [
        {
            "word":  w["word"],
            "start": round(w["start"] - clip_start, 6),
            "end":   round(w["end"]   - clip_start, 6),
        }
        for w in all_words
        if w["start"] >= clip_start and w["end"] <= clip_end + 0.5
    ]


# =============================================================================
#  ASS SUBTITLE GENERATOR
# =============================================================================

def _write_ass_file(
    karaoke_words: list,
    highlight_word_colors: list,
    clip_duration: float,
    W: int,
    H: int,
    cfg,
) -> tuple[Optional[str], Optional[str]]:
    """
    Generate an ASS subtitle file with karaoke per-word highlighting.

    Key design decisions that fix the overlap/line2-only bugs:

    1. Each dialogue event uses \\pos(cx, y) + \\an5 (middle-center anchor)
       so Y means the VERTICAL CENTER of the text, not the top edge.
       This makes Y arithmetic predictable regardless of font metrics.

    2. Both lines always emit together in the same time interval — one event
       for line1 at y_line1, one event for line2 at y_line2.
       They never share a MarginV or style position, so overlap is impossible.

    3. Time is sliced into intervals at every word boundary so we only ever
       need one event per line per interval. The active word for that interval
       is determined by checking which word's [start, end) contains the midpoint.
    """

    ass_dir = Path("temp_ass")
    ass_dir.mkdir(exist_ok=True)
    if not karaoke_words:
        return None, None

    font_sub, subtitle_fonts_dir = _resolve_subtitle_font(cfg)
    fontsize     = getattr(cfg, "SUBTITLE_FONTSIZE", 68)
    sub_y_frac   = getattr(cfg, "SUBTITLE_Y_POS", 0.80)
    stroke_w     = getattr(cfg, "SUBTITLE_STROKE_W", 3)
    inactive_op  = getattr(cfg, "KARAOKE_INACTIVE_OPACITY", 1.0)
    active_color = getattr(cfg, "KARAOKE_ACTIVE_COLOR", "#FFD600")

    play_res_x  = W
    play_res_y  = H
    ass_fontsize = int(fontsize * 0.85)

    # ── Y positions ───────────────────────────────────────────────────────────
    # \an5 = middle-center anchor, so \pos(x,y) sets the vertical CENTER
    # of the text box, not the top edge.
    # line_gap = vertical distance between the two karaoke rows.
    # Keep it tighter so the 2-line block feels cohesive on mobile.
    line_gap = int(ass_fontsize * 1.18)
    # y_line2 is the center of the bottom line, at sub_y_frac of frame height
    y_line2  = int(H * sub_y_frac)
    # y_line1 is directly above it
    y_line1  = y_line2 - line_gap
    cx       = W // 2

    # ── Color helpers ─────────────────────────────────────────────────────────
    def hex_to_ass(hex_color: str, alpha: int = 0) -> str:
        h = hex_color.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"

    def named_to_hex(color: str) -> str:
        named = {"white": "#FFFFFF", "black": "#000000", "yellow": "#FFD600",
                 "red": "#FF0000", "green": "#00FF00"}
        return named.get(color.lower(), color if color.startswith("#") else "#FFFFFF")

    white_ass      = hex_to_ass("#FFFFFF")
    active_ass     = hex_to_ass(named_to_hex(active_color))
    inactive_alpha = int((1.0 - inactive_op) * 255)

    def word_color_ass(word_idx: int) -> str:
        mapped = None
        if 0 <= word_idx < len(highlight_word_colors):
            mapped = highlight_word_colors[word_idx]
        if mapped:
            return hex_to_ass(named_to_hex(mapped))
        return active_ass

    # ── ASS header ────────────────────────────────────────────────────────────
    stroke_color_cfg = getattr(cfg, "SUBTITLE_STROKE", "#000000")
    outline_ass      = hex_to_ass(named_to_hex(stroke_color_cfg))

    # Style uses \an5 (middle-center) as the base alignment.
    # \pos() in each event overrides placement, making MarginV irrelevant.
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_res_x}
PlayResY: {play_res_y}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_sub},{ass_fontsize},{white_ass},{white_ass},{outline_ass},&H00000000,-1,0,0,0,100,100,0,0,1,{stroke_w},0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    dialogue_lines = []

    def _to_centis(seconds: float) -> int:
        s = max(0.0, min(seconds, clip_duration))
        return int(round(s * 100.0))

    def _ts_from_centis(total_cs: int) -> str:
        h = total_cs // 360000
        rem = total_cs % 360000
        m = rem // 6000
        rem %= 6000
        sec = rem // 100
        cs = rem % 100
        return f"{h}:{m:02d}:{sec:02d}.{cs:02d}"

    def build_line_text(line_words: list, active_idx: int) -> str:
        """Build ASS text for one line. active_idx = -1 means all inactive."""
        parts = []
        for i, wd in enumerate(line_words):
            display_word = _format_karaoke_display_word(wd["word"])
            if not display_word:
                continue
            if i == active_idx:
                color = word_color_ass(int(wd.get("_highlight_idx", -1)))
                parts.append(f"{{\\c{color}\\alpha&H00&}}{display_word}")
            else:
                parts.append(f"{{\\c{white_ass}\\alpha&H{inactive_alpha:02X}&}}{display_word}")
        return " ".join(parts)

    def active_idx_at(line_words: list, t_mid: float) -> int:
        for i, wd in enumerate(line_words):
            if wd["start"] <= t_mid < wd["end"]:
                return i
        return -1

    def emit(t0: float, t1: float, line_words: list, active_idx: int, y_pos: int) -> None:
        if t1 <= t0 or not line_words:
            return
        text = build_line_text(line_words, active_idx)
        if not text.strip():
            return
        start_cs = _to_centis(t0)
        end_cs = _to_centis(t1)
        if end_cs <= start_cs:
            return
        # \an5 = middle-center anchor; \pos(x,y) sets the center of this line.
        dialogue_lines.append(
            f"Dialogue: 0,{_ts_from_centis(start_cs)},{_ts_from_centis(end_cs)},"
            f"Default,,0,0,0,,{{\\an5\\pos({cx},{y_pos})}}{text}"
        )

    # ── Chunk words and emit events ───────────────────────────────────────────
    chunks = _chunk_words(karaoke_words, words_per_chunk=4)

    for chunk in chunks:
        if not chunk:
            continue

        chunk_start = chunk[0]["start"]
        chunk_end   = min(chunk[-1]["end"], clip_duration)
        if chunk_start >= clip_duration or chunk_end <= chunk_start:
            continue

        mid   = max(1, len(chunk) // 2)
        line1 = chunk[:mid]
        line2 = chunk[mid:]

        # Collect every word boundary in this chunk as time slice points
        boundaries = sorted(set(
            [chunk_start, chunk_end]
            + [max(w["start"], chunk_start) for w in chunk]
            + [min(w["end"],   chunk_end)   for w in chunk]
        ))

        for i in range(len(boundaries) - 1):
            t0    = boundaries[i]
            t1    = boundaries[i + 1]
            if t1 <= t0:
                continue
            mid_t = (t0 + t1) / 2.0
            emit(t0, t1, line1, active_idx_at(line1, mid_t), y_line1)
            if line2:
                emit(t0, t1, line2, active_idx_at(line2, mid_t), y_line2)

    ass_content = header + "\n".join(dialogue_lines) + "\n"

    ass_filename = f"sub_{random.randint(100000, 999999)}.ass"
    ass_path = ass_dir / ass_filename
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(ass_content)

    return str(ass_path), subtitle_fonts_dir


# =============================================================================
#  FILTER_COMPLEX BUILDER + FFMPEG RUNNER
# =============================================================================

def _build_and_run(
    raw_clip_path, output_path, ass_path, ass_fonts_dir,
    W, H, clip_duration, clip_fps, has_audio,
    moment, prod_trigger, face_zooms,
    zoom_dur, zoom_scale, hook_end,
    extra_inputs, sfx_events, bgm_path, cfg,
) -> bool:
    """
    Assemble -filter_complex string and run FFmpeg.

    Filter graph:
      [0:v] → variant transforms (hflip, crop, eq) → zoom chain → ass subtitles
            → hook text → product caption drawtext → overlays → [vout]
      [0:a] → amix with SFX streams + optional ducked BGM → [aout]
    """
    # ── Variant overrides ─────────────────────────────────────────────────────
    variant_baked  = getattr(cfg, "_variant_transforms_baked", False)
    mirror         = False if variant_baked else getattr(cfg, "_mirror", False)
    speed_ramp     = 1.0 if variant_baked else getattr(cfg, "_speed_ramp", 1.0)
    color_grade    = "" if variant_baked else getattr(cfg, "_color_grade_filter", "")
    crop_x_offset  = 0.0 if variant_baked else getattr(cfg, "_crop_x_offset", 0.0)
    zoom_trig_off  = getattr(cfg, "_zoom_trigger_offset",  0.0)
    output_fps     = getattr(cfg, "OUTPUT_FPS", 30)
    timeline_fps   = clip_fps if clip_fps and clip_fps > 1.0 else output_fps

    # Apply zoom trigger offset
    if prod_trigger and zoom_trig_off != 0.0:
        new_t = max(hook_end + 0.1, prod_trigger["trigger_t"] + zoom_trig_off)
        prod_trigger = {**prod_trigger, "trigger_t": new_t}

    # ── Build input list ──────────────────────────────────────────────────────
    cmd = ["ffmpeg", "-y"]
    cmd += ["-i", raw_clip_path]
    for ei in extra_inputs:
        if ei["type"] in {"ba", "emoji"}:
            # Still images need to be looped to create a real video timeline
            # so time-based fades/overlays render visibly.
            cmd += ["-loop", "1", "-t", f"{clip_duration:.3f}", "-i", ei["path"]]
        else:
            cmd += ["-i", ei["path"]]
    for sfx in sfx_events:
        cmd += ["-i", str(sfx["sfx_path"])]
    bgm_input_idx = None
    if bgm_path:
        bgm_input_idx = 1 + len(extra_inputs) + len(sfx_events)
        cmd += ["-stream_loop", "-1", "-t", f"{clip_duration:.3f}", "-i", str(bgm_path)]

    broll_intro = _prepare_broll_intro(cfg, clip_duration=clip_duration, hook_end=hook_end)
    broll_input_idx = None
    if broll_intro:
        broll_input_idx = 1 + len(extra_inputs) + len(sfx_events) + (1 if bgm_path else 0)
        cmd += [
            "-stream_loop", "-1",
            "-t", f"{broll_intro['duration']:.3f}",
            "-i", broll_intro["path"],
        ]

    # ── Build filter_complex ──────────────────────────────────────────────────
    fc = []       # filter_complex lines
    vid = "[0:v]"  # current video stream label

    # ── 1. Base video transforms ──────────────────────────────────────────────
    base_filters = []

    # Crop X offset
    if abs(crop_x_offset) > 0.005:
        crop_w = int(W * (1.0 - abs(crop_x_offset)))
        crop_x = int(W * (crop_x_offset if crop_x_offset > 0 else 0))
        base_filters.append(f"crop={crop_w}:{H}:{crop_x}:0,scale={W}:{H}")

    # Mirror
    if mirror:
        base_filters.append("hflip")

    # Speed ramp
    if abs(speed_ramp - 1.0) > 0.02:
        pts = round(1.0 / speed_ramp, 4)
        base_filters.append(f"setpts={pts}*PTS")

    # Color grade
    if color_grade:
        base_filters.append(color_grade)
    if base_filters:
        fc.append(f"{vid}{','.join(base_filters)}[vbase]")
        vid = "[vbase]"

    # ── 2. Zoom chain (zoompan) ───────────────────────────────────────────────
    zoom_exprs = _build_zoom_expressions(
        prod_trigger, face_zooms, clip_duration, W, H, zoom_dur, zoom_scale, timeline_fps
    )
    if zoom_exprs:
        zp_expr, x_expr, y_expr = zoom_exprs
        fc.append(
            f"{vid}zoompan=z='{zp_expr}':x='{x_expr}':y='{y_expr}'"
            f":d=1:s={W}x{H}:fps={timeline_fps:.6f}[vzoom]"
        )
        vid = "[vzoom]"

    # ── 3. ASS subtitles (hardcoded burn-in) ─────────────────────────────────

    if ass_path and Path(ass_path).exists():
        # Windows path escaping for FFmpeg ass= filter:
        # Forward slashes only, and drive colon must be escaped as \:
        # e.g.  C:/Users/... → C\:/Users/...
        safe_ass = _escape_ass_filter_path(ass_path)
        ass_filter = f"ass={safe_ass}"
        if ass_fonts_dir:
            safe_fonts_dir = _escape_ass_filter_path(ass_fonts_dir)
            ass_filter += f":fontsdir={safe_fonts_dir}"
        fc.append(f"{vid}{ass_filter}[vsub]")
        vid = "[vsub]"

    # ── 4. Hook title (drawtext) ──────────────────────────────────────────────
    if broll_intro and broll_input_idx is not None:
        vid = _add_broll_intro_replacement_filters(
            fc,
            vid,
            broll_input_idx,
            broll_intro,
            clip_duration,
            W,
            H,
            output_fps,
            cfg,
        )
    else:
        vid = _add_before_after_overlay_filters(fc, vid, extra_inputs, clip_duration, W, H, cfg)

    hook_dur_cfg = getattr(cfg, "HOOK_DURATION", 0.0)
    if hook_dur_cfg > 0:
        hook_overlay = ensure_hook_payload(moment)
        hook_headline = hook_overlay.get("headline", "")
        hook_subtext = hook_overlay.get("subtext", "")
        hook_cta = hook_overlay.get("cta", "")
        hook_end_t = min(hook_dur_cfg, clip_duration * 0.4)
        hook_font  = getattr(cfg, "FONT_HOOK", "")
        hook_fs    = getattr(cfg, "HOOK_FONTSIZE", 130)
        hook_sw    = getattr(cfg, "HOOK_STROKE_W", 5)
        hook_sc    = _css_to_ffmpeg_color(getattr(cfg, "HOOK_STROKE_COLOR", "black"))
        hook_shadow = _css_to_ffmpeg_color(getattr(cfg, "HOOK_SHADOW_COLOR", "#000000"))
        hook_accent = _css_to_ffmpeg_color(getattr(cfg, "HOOK_ACCENT_COLOR", "#FFD600"))

        if hook_headline:
            vid = _add_hook_text_block(
                fc=fc,
                vid=vid,
                text=hook_headline,
                frame_width=W,
                font_path=hook_font,
                font_size=int(getattr(cfg, "HOOK_TOP_FONTSIZE", hook_fs)),
                font_color=_css_to_ffmpeg_color(getattr(cfg, "HOOK_COLOR", "white")),
                stroke_width=hook_sw,
                stroke_color=hook_sc,
                shadow_color=hook_shadow,
                center_y=int(H * float(getattr(cfg, "HOOK_TOP_Y_POS", 0.20))),
                start_t=0.0,
                end_t=hook_end_t,
                block_tag="vhooktop",
                width_ratio=0.92,
            )

        if hook_subtext:
            vid = _add_hook_text_block(
                fc=fc,
                vid=vid,
                text=hook_subtext,
                frame_width=W,
                font_path=hook_font,
                font_size=int(getattr(cfg, "HOOK_MID_FONTSIZE", max(58, hook_fs * 0.52))),
                font_color=hook_accent,
                stroke_width=max(2, int(hook_sw * 0.75)),
                stroke_color=hook_sc,
                shadow_color=hook_shadow,
                center_y=int(H * float(getattr(cfg, "HOOK_MID_Y_POS", 0.60))),
                start_t=0.0,
                end_t=hook_end_t,
                block_tag="vhookmid",
                width_ratio=0.60,
                x_expr="w-text_w-w*0.07",
            )

        if hook_cta:
            vid = _add_hook_text_block(
                fc=fc,
                vid=vid,
                text=hook_cta,
                frame_width=W,
                font_path=hook_font,
                font_size=int(getattr(cfg, "HOOK_BOTTOM_FONTSIZE", max(82, hook_fs * 0.98))),
                font_color=hook_accent,
                stroke_width=hook_sw,
                stroke_color=hook_sc,
                shadow_color=hook_shadow,
                center_y=int(H * float(getattr(cfg, "HOOK_BOTTOM_Y_POS", 0.65))),
                start_t=0.0,
                end_t=hook_end_t,
                block_tag="vhookbtm",
                width_ratio=0.92,
            )

    # ── 5. Product caption (drawtext above product bbox) ─────────────────────
    if prod_trigger:
        vid = _add_product_caption_filters(fc, vid, prod_trigger, zoom_dur, W, H, cfg)

    # ── 6. Before/After overlay ───────────────────────────────────────────────
    # ── 7. Logo watermark ─────────────────────────────────────────────────────
    emoji_stream_count = 0
    for i, ei in enumerate(extra_inputs):
        if ei["type"] != "emoji":
            continue

        overlay = ei.get("overlay") or {}
        emoji_stream_count += 1
        input_idx = i + 1
        tag = f"emoji{emoji_stream_count}"
        start_t = max(0.0, min(float(overlay.get("start", 0.0)), clip_duration))
        end_t = max(start_t, min(float(overlay.get("end", start_t)), clip_duration))
        if end_t <= start_t + 0.01:
            continue

        fade_in = max(0.0, min(float(overlay.get("fade_in", 0.15)), (end_t - start_t) / 2.0))
        fade_out = max(0.0, min(float(overlay.get("fade_out", fade_in)), (end_t - start_t) / 2.0))
        size_px = max(48, int(overlay.get("size_px", max(48, int(min(W, H) * 0.16)))))
        center_x = int(overlay.get("center_x", W // 2))
        center_y = int(overlay.get("center_y", int(H * 0.76)))
        fade_out_start = max(start_t, end_t - fade_out)

        fc.append(f"[{input_idx}:v]scale={size_px}:-1:flags=lanczos,format=rgba[{tag}src]")

        emoji_chain = f"[{tag}src]"
        if fade_in > 0.0 or fade_out > 0.0:
            fc.append(
                f"{emoji_chain}fade=t=in:st={start_t:.2f}:d={fade_in:.2f}:alpha=1,"
                f"fade=t=out:st={fade_out_start:.2f}:d={fade_out:.2f}:alpha=1[{tag}fade]"
            )
            emoji_chain = f"[{tag}fade]"

        fc.append(
            f"{vid}{emoji_chain}overlay="
            f"x='{center_x}-overlay_w/2':y='{center_y}-overlay_h/2'"
            f":enable='between(t,{start_t:.2f},{end_t:.2f})'[{tag}out]"
        )
        vid = f"[{tag}out]"

    logo_input_idx = None
    for i, ei in enumerate(extra_inputs):
        if ei["type"] == "logo":
            logo_input_idx = i + 1
            break

    if logo_input_idx is not None:
        logo_h = int(H * 0.06)
        logo_x = int(W * 0.05)
        logo_y = int(H * 0.03)
        fc.append(f"[{logo_input_idx}:v]scale=-1:{logo_h}[logo]")
        fc.append(f"{vid}[logo]overlay=x={logo_x}:y={logo_y}:format=auto[vlogo]")
        vid = "[vlogo]"

    # ── 8. Audio — SFX amix ───────────────────────────────────────────────────
    n_sfx = len(sfx_events)
    aud = "[0:a]" if has_audio else "[abase]"
    if not has_audio:
        fc.append(
            f"anullsrc=channel_layout=stereo:sample_rate=44100,"
            f"atrim=0:{clip_duration:.3f}[abase]"
        )
    sfx_input_offset = 1 + len(extra_inputs)  # inputs: raw + extra_images + sfx

    if n_sfx > 0:
        sfx_labels = []
        for j, sfx in enumerate(sfx_events):
            idx = sfx_input_offset + j
            delay_ms = int(sfx["t"] * 1000)
            vol = sfx.get("volume", 0.5)
            fc.append(
                f"[{idx}:a]adelay={delay_ms}|{delay_ms},"
                f"volume={vol:.2f}[sfx{j}]"
            )
            sfx_labels.append(f"[sfx{j}]")

        all_audio = [aud] + sfx_labels
        fc.append(
            f"{''.join(all_audio)}amix=inputs={len(all_audio)}:duration=first:normalize=0[aout]"
        )
        aud = "[aout]"

    if bgm_input_idx is not None:
        bgm_volume = _clamp_float(getattr(cfg, "BGM_VOLUME", 0.12), 0.0, 1.0, 0.12)
        ducking_enabled = has_audio and getattr(cfg, "BGM_DUCKING_ENABLED", True)
        fc.append(
            f"[{bgm_input_idx}:a]"
            f"aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
            f"atrim=0:{clip_duration:.3f},asetpts=PTS-STARTPTS,"
            f"volume={bgm_volume:.4f}[bgmbase]"
        )

        bgm_label = "[bgmbase]"
        aud_for_mix = aud
        if ducking_enabled:
            threshold = _clamp_float(getattr(cfg, "BGM_DUCKING_THRESHOLD", 0.03), 0.0001, 1.0, 0.03)
            ratio = _clamp_float(getattr(cfg, "BGM_DUCKING_RATIO", 8.0), 1.0, 20.0, 8.0)
            attack_ms = int(_clamp_float(getattr(cfg, "BGM_DUCKING_ATTACK_MS", 50), 1.0, 1000.0, 50.0))
            release_ms = int(_clamp_float(getattr(cfg, "BGM_DUCKING_RELEASE_MS", 350), 10.0, 5000.0, 350.0))
            fc.append(f"{aud}asplit=2[audmain][audside]")
            fc.append(
                f"[bgmbase][audside]"
                f"sidechaincompress=threshold={threshold:.4f}:ratio={ratio:.3f}:"
                f"attack={attack_ms}:release={release_ms}:makeup=1[bgmduck]"
            )
            bgm_label = "[bgmduck]"
            aud_for_mix = "[audmain]"

        fc.append(
            f"{aud_for_mix}{bgm_label}"
            f"amix=inputs=2:duration=first:normalize=0[abgm]"
        )
        aud = "[abgm]"

    # ── 9. Speed ramp audio (atempo) ─────────────────────────────────────────
    if abs(speed_ramp - 1.0) > 0.02:
        speed_clamped = max(0.75, min(1.25, speed_ramp))
        fc.append(f"{aud}atempo={speed_clamped:.4f}[atempo]")
        aud = "[atempo]"

    # ── Finalize: -map uses whatever vid/aud labels we ended up with ──────────
    # Do NOT emit null/anull — just map the final stream labels directly.
    # If vid is still "[0:v]" (no filters applied), map 0:v directly.

    codec  = getattr(cfg, "OUTPUT_CODEC",  "h264_nvenc")
    preset = getattr(cfg, "OUTPUT_PRESET", "p1")
    fps    = output_fps
    ab     = getattr(cfg, "OUTPUT_AUDIO_BITRATE", "128k")

    fc_clean = []
    if fc:
        fc_clean = [f for f in fc if f and f.strip()]

        # 🔍 DEBUG LOG HERE
        if getattr(cfg, "LOG_FFMPEG_FILTER_COMPLEX", False):
            log.debug("FILTER_COMPLEX:\n" + ";\n".join(fc_clean))

    if fc_clean:
        cmd += ["-filter_complex", ";".join(fc_clean)]

    if vid == "[0:v]":
        cmd += ["-map", "0:v"]
    else:
        cmd += ["-map", vid]

    if aud == "[0:a]":
        cmd += ["-map", "0:a"]
    else:
        cmd += ["-map", aud]

    cmd += ["-c:v", codec, "-preset", preset]

    if codec.endswith("_nvenc"):
        cq = getattr(cfg, "OUTPUT_CQ", 28)
        cmd += ["-cq", str(cq), "-rc", "vbr", "-b:v", "0"]
    else:
        crf = getattr(cfg, "OUTPUT_CRF", 23)
        cmd += ["-crf", str(crf)]

    cmd += [
        "-c:a", "aac", "-b:a", ab,
        "-r", str(fps),
        "-movflags", "+faststart",
        output_path,
    ]

    return _run_ffmpeg(cmd, output_path, timeout=600)


# =============================================================================
#  ZOOM EXPRESSIONS
# =============================================================================

def _build_zoom_expressions(
    prod_trigger, face_zooms, clip_duration, W, H, zoom_dur, zoom_scale, timeline_fps
) -> Optional[tuple]:
    """
    Build zoompan filter expressions for product + face zooms.

    FFmpeg zoompan 'on' = output frame counter (1-based).
    We use piecewise if(between(on,s,e), val, fallback) expressions.

    Bug fix: long nested if() chains exceed FFmpeg's 4096-char expression limit.
    Solution: cap total zoom events to a safe number (product zoom + max 6 face
    zooms), and use compact single-char variable references where possible.

    Returns (z_expr, x_expr, y_expr) or None if no zooms needed.
    """
    zoom_events = []

    # Product zoom — highest priority, always included
    if prod_trigger:
        t_start = prod_trigger["trigger_t"]
        t_end   = min(t_start + zoom_dur, clip_duration - 0.05)
        zoom_events.append({
            "start_f": int(t_start * timeline_fps),
            "end_f":   int(t_end   * timeline_fps),
            "scale":   zoom_scale,
            "cx":      prod_trigger["cx"],
            "cy":      prod_trigger["cy"],
            "ease_f":  max(1, int(0.4 * timeline_fps)),
            "priority": 1,
        })

    # Face zooms — cap at 5 to keep expression length safe
    for fz in face_zooms[:5]:
        zoom_events.append({
            "start_f": int(fz["start"] * timeline_fps),
            "end_f":   int(fz["end"]   * timeline_fps),
            "scale":   fz["scale"],
            "cx":      fz["cx"],
            "cy":      fz["cy"],
            "ease_f":  max(1, int(fz.get("ease_in", 0.15) * timeline_fps)),
            "priority": 0,
        })

    if not zoom_events:
        return None

    # Sort: product zoom first (priority 1), then face zooms by start time
    zoom_events.sort(key=lambda e: (-e["priority"], e["start_f"]))

    # ── Build compact piecewise expressions ───────────────────────────────────
    # Use 4 decimal places max on floats, no spaces, to keep strings short.
    # z expression: during zoom window use quadratic ease-in, else 1
    z_expr = "1"
    for ev in reversed(zoom_events):
        s  = ev["start_f"]
        e  = ev["end_f"]
        ef = ev["ease_f"]
        sc = round(ev["scale"], 3)
        # Compact: 1+(sc-1)*min((on-s)/ef,1)^2  — quadratic ease-in
        inner = f"1+{sc-1:.3f}*pow(min((on-{s})/{ef},1),2)"
        z_expr = f"if(between(on,{s},{e}),{inner},{z_expr})"

    # x expression: keep the target bbox center in the center of the crop and
    # clamp to legal crop bounds so edge products stay as centered as possible.
    x_expr = "(iw-iw/zoom)/2"
    for ev in reversed(zoom_events):
        s  = ev["start_f"]
        e  = ev["end_f"]
        cx = round(max(0.0, min(1.0, ev["cx"])), 4)
        x_focus = f"max(0,min(iw-iw/zoom,iw*{cx}-iw/(2*zoom)))"
        x_expr = f"if(between(on,{s},{e}),{x_focus},{x_expr})"

    # y expression
    y_expr = "(ih-ih/zoom)/2"
    for ev in reversed(zoom_events):
        s  = ev["start_f"]
        e  = ev["end_f"]
        cy = round(max(0.0, min(1.0, ev["cy"])), 4)
        y_focus = f"max(0,min(ih-ih/zoom,ih*{cy}-ih/(2*zoom)))"
        y_expr = f"if(between(on,{s},{e}),{y_focus},{y_expr})"

    # Safety check: if any expression is dangerously long, fall back to
    # product zoom only (no face zooms) to guarantee it fits
    MAX_EXPR_LEN = 3800
    if max(len(z_expr), len(x_expr), len(y_expr)) > MAX_EXPR_LEN:
        log.warning(
            f"Zoom expression too long ({len(z_expr)} chars) — "
            f"dropping face zooms to stay within FFmpeg limit"
        )
        if prod_trigger:
            ev = next(e for e in zoom_events if e["priority"] == 1)
            s, e_f, ef = ev["start_f"], ev["end_f"], ev["ease_f"]
            sc = round(ev["scale"], 3)
            cx = round(max(0.0, min(1.0, ev["cx"])), 4)
            cy = round(max(0.0, min(1.0, ev["cy"])), 4)
            z_expr = f"if(between(on,{s},{e_f}),1+{sc-1:.3f}*pow(min((on-{s})/{ef},1),2),1)"
            x_expr = f"if(between(on,{s},{e_f}),max(0,min(iw-iw/zoom,iw*{cx}-iw/(2*zoom))),(iw-iw/zoom)/2)"
            y_expr = f"if(between(on,{s},{e_f}),max(0,min(ih-ih/zoom,ih*{cy}-ih/(2*zoom))),(ih-ih/zoom)/2)"
        else:
            return None

    return z_expr, x_expr, y_expr


# =============================================================================
#  BEFORE/AFTER OVERLAY
# =============================================================================

def _add_broll_intro_replacement_filters(
    fc,
    vid,
    broll_input_idx: int,
    broll_intro: dict,
    clip_duration: float,
    W: int,
    H: int,
    output_fps: int,
    cfg,
) -> str:
    intro_dur = max(0.0, min(float(broll_intro.get("duration", 0.0) or 0.0), clip_duration))
    if intro_dur <= 0.01:
        return vid

    fade_in = max(0.0, min(float(getattr(cfg, "BROLL_INTRO_FADE_IN", 0.0) or 0.0), intro_dur / 2.0))
    fade_out = max(0.0, min(float(getattr(cfg, "BROLL_INTRO_FADE_OUT", 0.20) or 0.0), intro_dur / 2.0))
    fade_out_start = max(0.0, intro_dur - fade_out)

    fc.append(
        f"[{broll_input_idx}:v]"
        f"trim=0:{intro_dur:.3f},setpts=PTS-STARTPTS,"
        f"scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},fps={output_fps},format=rgba,setsar=1[vbrollsrc]"
    )

    broll_chain = "[vbrollsrc]"
    if fade_in > 0.0:
        fc.append(f"{broll_chain}fade=t=in:st=0:d={fade_in:.2f}:alpha=1[vbrollfi]")
        broll_chain = "[vbrollfi]"
    if fade_out > 0.0:
        fc.append(
            f"{broll_chain}fade=t=out:st={fade_out_start:.2f}:d={fade_out:.2f}:alpha=1[vbrollfade]"
        )
        broll_chain = "[vbrollfade]"

    fc.append(
        f"{vid}{broll_chain}overlay=x=0:y=0:enable='between(t,0,{intro_dur:.2f})'[vbroll]"
    )
    return "[vbroll]"


def _add_before_after_overlay_filters(fc, vid, extra_inputs, clip_duration, W, H, cfg) -> str:
    ba_input_idx = None
    for i, ei in enumerate(extra_inputs):
        if ei["type"] == "ba":
            ba_input_idx = i + 1  # +1 because input 0 is the raw clip
            break

    if ba_input_idx is None:
        return vid

    ba_s = max(0.0, float(getattr(cfg, "BEFORE_AFTER_START_T", 0.0)))
    ba_dur = max(0.0, float(getattr(cfg, "BEFORE_AFTER_DURATION", 2.5)))
    ba_e = min(ba_s + ba_dur, clip_duration)
    if ba_e <= ba_s + 0.01:
        return vid

    ba_op = max(0.0, min(1.0, float(getattr(cfg, "BEFORE_AFTER_OPACITY", 0.96))))
    ba_fi = max(0.0, min(float(getattr(cfg, "BEFORE_AFTER_FADE_IN", 0.25)), ba_e - ba_s))
    ba_fo = max(0.0, min(float(getattr(cfg, "BEFORE_AFTER_FADE_OUT", 0.25)), ba_e - ba_s))

    fc.append(
        f"[{ba_input_idx}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,format=rgba,"
        f"colorchannelmixer=aa={ba_op:.3f}[bascaled]"
    )

    ba_chain = "[bascaled]"
    if ba_fi > 0.0:
        fc.append(f"{ba_chain}fade=t=in:st={ba_s:.2f}:d={ba_fi:.2f}:alpha=1[bafadein]")
        ba_chain = "[bafadein]"
    if ba_fo > 0.0:
        fade_out_start = max(ba_s, ba_e - ba_fo)
        fc.append(f"{ba_chain}fade=t=out:st={fade_out_start:.2f}:d={ba_fo:.2f}:alpha=1[bafaded]")
        ba_chain = "[bafaded]"

    fc.append(
        f"{vid}{ba_chain}overlay=x='(W-overlay_w)/2':y='(H-overlay_h)/2'"
        f":enable='between(t,{ba_s:.2f},{ba_e:.2f})'[vba]"
    )
    return "[vba]"


# =============================================================================
#  PRODUCT CAPTION VIA DRAWTEXT
# =============================================================================

def _add_product_caption_filters(fc, vid, prod_trigger, zoom_dur, W, H, cfg) -> str:
    """Add product name + brand caption drawtext filters. Returns new vid label."""
    product_name = prod_trigger.get("product_name", "")
    if not product_name:
        return vid

    t_start = prod_trigger["trigger_t"]
    t_end   = t_start + zoom_dur

    font_path  = getattr(cfg, "FONT_PRODUCT", "")
    product_fs = getattr(cfg, "ZOOM_CAPTION_FONTSIZE", 80)
    brand_fs   = getattr(cfg, "ZOOM_CAPTION_BRAND_FONTSIZE", 0)
    txt_color  = _css_to_ffmpeg_color(getattr(cfg, "ZOOM_CAPTION_TEXT_COLOR",  "white"))
    brand_color = _css_to_ffmpeg_color(getattr(cfg, "ZOOM_CAPTION_BRAND_COLOR", "#FFD600"))
    stroke_c   = _css_to_ffmpeg_color(getattr(cfg, "ZOOM_CAPTION_STROKE_COLOR", "black"))
    stroke_w   = getattr(cfg, "ZOOM_CAPTION_STROKE_WIDTH", 2)
    caption_y_frac = float(getattr(cfg, "ZOOM_CAPTION_Y_POS", 0.10))
    text_x_expr = "(w-text_w)/2"
    text_y = max(40, int(H * caption_y_frac))

    font_arg = f":fontfile='{font_path.replace(chr(92), '/')}'" if font_path and Path(font_path).exists() else ""
    safe_name = _escape_drawtext(product_name.upper())

    enable = f"between(t,{t_start:.2f},{t_end:.2f})"

    if product_fs > 0:
        fc.append(
            f"{vid}drawtext=text='{safe_name}'{font_arg}"
            f":fontsize={product_fs}:fontcolor={txt_color}"
            f":borderw={stroke_w}:bordercolor={stroke_c}"
            f":x={text_x_expr}:y={text_y}"
            f":enable='{enable}'[vcap1]"
        )
        vid = "[vcap1]"

    brand_name = getattr(cfg, "BRAND_NAME", "PROYA 5X Vitamin C")
    if brand_fs > 0 and brand_name:
        safe_brand = _escape_drawtext(brand_name.upper())
        fc.append(
            f"{vid}drawtext=text='{safe_brand}'{font_arg}"
            f":fontsize={brand_fs}:fontcolor={brand_color}"
            f":borderw=1:bordercolor={stroke_c}"
            f":x={text_x_expr}:y={text_y + int(product_fs * 1.2)}"
            f":enable='{enable}'[vcap2]"
        )
        vid = "[vcap2]"

    return vid


# =============================================================================
#  ZOOM PLANNING (port from clip_editor.py)
# =============================================================================

def _find_zoom_trigger(clip_words, prod_events, hook_end, clip_duration, cfg):
    """Find the earliest spoken product mention and pair it with the best visual event."""
    if not clip_words:
        return None

    keywords = []
    for normalized_name, display_name in _allowed_product_class_map(cfg).items():
        keywords.append((normalized_name, display_name))
    seen, unique_kw = set(), []
    for kw, disp in sorted(keywords, key=lambda x: len(x[0]), reverse=True):
        if kw not in seen:
            seen.add(kw); unique_kw.append((kw, disp))

    normalized_keywords = []
    for kw, disp in unique_kw:
        normalized_tokens = _normalized_word_tokens(kw)
        if normalized_tokens:
            normalized_keywords.append((normalized_tokens, disp))

    word_entries = [
        {
            "start": float(w["start"]),
            "end": float(w["end"]),
            "tokens": _normalized_word_tokens(w.get("word", "")),
        }
        for w in clip_words
    ]

    spoken_trigger = None
    for i, word_entry in enumerate(word_entries):
        t0 = word_entry["start"]
        if t0 < max(0.0, hook_end):
            continue
        if t0 >= clip_duration - 1.5:
            continue
        for keyword_tokens, disp in normalized_keywords:
            if _match_keyword_at(word_entries, i, keyword_tokens):
                spoken_trigger = (t0, disp)
                break
        if spoken_trigger:
            break

    if spoken_trigger is None:
        return None

    trigger_t, product_name = spoken_trigger
    best_ev = _select_best_product_event(trigger_t, product_name, prod_events)

    if best_ev:
        bbox = _select_bbox_for_event(best_ev, trigger_t)
        fw   = best_ev.get("frame_w", 1)
        fh   = best_ev.get("frame_h", 1)
        if bbox and fw > 0 and fh > 0:
            cx, cy = _bbox_center_norm(bbox, fw, fh)
            return {"trigger_t": trigger_t, "cx": cx, "cy": cy,
                    "product_name": product_name, "bbox_orig": bbox,
                    "yolo_matched": True}

    return {"trigger_t": trigger_t, "cx": 0.50, "cy": 0.65,
            "product_name": product_name, "bbox_orig": None,
            "yolo_matched": False}


def _normalized_word_tokens(text: str) -> list[str]:
    normalized = re.sub(r"[^\w\s]", " ", str(text).lower(), flags=re.UNICODE)
    return [tok for tok in normalized.split() if tok]


def _match_keyword_at(word_entries: list[dict], start_idx: int, keyword_tokens: list[str]) -> bool:
    if not keyword_tokens:
        return False

    flat_tokens = []
    for entry in word_entries[start_idx:]:
        flat_tokens.extend(entry["tokens"])
        if len(flat_tokens) >= len(keyword_tokens):
            return flat_tokens[:len(keyword_tokens)] == keyword_tokens
    return False


def _normalize_product_name(text: str) -> str:
    return " ".join(_normalized_word_tokens(text))


def _allowed_product_class_map(cfg) -> dict[str, str]:
    host_face_class = _normalize_product_name(getattr(cfg, "HOST_FACE_CLASS", "host_face"))
    product_classes = getattr(cfg, "PRODUCT_CLASSES", {})
    allowed = {}
    for cls_name in product_classes.values():
        normalized_name = _normalize_product_name(cls_name)
        if normalized_name and normalized_name != host_face_class:
            allowed[normalized_name] = str(cls_name)
    return allowed


def _select_best_product_event(trigger_t: float, product_name: str, prod_events: list) -> Optional[dict]:
    product_norm = _normalize_product_name(product_name)
    best_event = None
    best_score = float("inf")

    for ev in prod_events:
        ev_start = float(ev.get("relative_start", ev.get("start_time", 0.0)))
        ev_end = float(ev.get("relative_end", ev.get("end_time", ev_start)))
        if ev_start - trigger_t > 5.0 or trigger_t - ev_end > 5.0:
            continue

        class_norm = _normalize_product_name(ev.get("class_name", ""))
        class_match = bool(class_norm and (class_norm in product_norm or product_norm in class_norm))
        overlaps_trigger = ev_start <= trigger_t <= ev_end
        starts_after = ev_start >= trigger_t
        time_penalty = (
            (ev_start - trigger_t) * 1.0 if starts_after else (trigger_t - ev_end) * 1.6
        )
        score = time_penalty
        if class_match:
            score -= 2.0
        if overlaps_trigger:
            score -= 1.5
        score -= min(float(ev.get("best_confidence", 0.0)), 1.0) * 0.2

        if score < best_score:
            best_score = score
            best_event = ev

    return best_event


def _select_bbox_for_event(event: dict, trigger_t: float) -> Optional[list]:
    relative_track = event.get("relative_track") or []
    if relative_track:
        preferred_window = 0.9
        preferred_samples = []
        for sample in relative_track:
            sample_t = float(sample.get("relative_time", 0.0))
            bbox = sample.get("bbox")
            if not bbox:
                continue
            if trigger_t <= sample_t <= trigger_t + preferred_window:
                preferred_samples.append(sample)

        search_pool = preferred_samples or relative_track
        best_sample = min(
            search_pool,
            key=lambda sample: (
                abs(float(sample.get("relative_time", 0.0)) - trigger_t),
                -float(sample.get("confidence", 0.0)),
                -_bbox_area(sample.get("bbox")),
            )
        )
        bbox = best_sample.get("bbox")
        if bbox:
            return bbox

    ev_start = float(event.get("relative_start", event.get("start_time", 0.0)))
    if trigger_t <= ev_start and event.get("start_bbox"):
        return event.get("start_bbox")
    if trigger_t >= float(event.get("relative_end", event.get("end_time", ev_start))) and event.get("end_bbox"):
        return event.get("end_bbox")
    if event.get("best_bbox"):
        return event.get("best_bbox")
    return event.get("start_bbox") or event.get("end_bbox")


def _bbox_area(bbox) -> float:
    if not bbox or len(bbox) < 4:
        return 0.0
    try:
        return max(0.0, float(bbox[2]) - float(bbox[0])) * max(0.0, float(bbox[3]) - float(bbox[1]))
    except (TypeError, ValueError):
        return 0.0


def _bbox_center_norm(bbox, frame_w: float, frame_h: float) -> tuple[float, float]:
    if not bbox or frame_w <= 0 or frame_h <= 0:
        return 0.50, 0.50
    try:
        cx = (float(bbox[0]) + float(bbox[2])) / 2.0 / float(frame_w)
        cy = (float(bbox[1]) + float(bbox[3])) / 2.0 / float(frame_h)
    except (TypeError, ValueError):
        return 0.50, 0.50
    return max(0.0, min(1.0, cx)), max(0.0, min(1.0, cy))


def _plan_face_zooms(clip_words, face_events, clip_duration, prod_trigger, hook_end, cfg):
    """Direct port of clip_editor._plan_face_zooms."""
    if not clip_words or not getattr(cfg, "HOST_FACE_ZOOM_ENABLED", True):
        return []

    WORDS_PER_ZOOM  = getattr(cfg, "FACE_ZOOM_WORDS_TRIGGER", [4, 4, 5, 5, 4, 5])
    SCALE_MIN       = getattr(cfg, "FACE_ZOOM_SCALE_MIN",   1.25)
    SCALE_MAX       = getattr(cfg, "FACE_ZOOM_SCALE_MAX",   1.55)
    EASE_MIN        = getattr(cfg, "FACE_ZOOM_EASE_MIN",    0.0)
    EASE_MAX        = getattr(cfg, "FACE_ZOOM_EASE_MAX",    0.0)
    DUR_MIN         = getattr(cfg, "FACE_ZOOM_DUR_MIN",     1.5)
    DUR_MAX         = getattr(cfg, "FACE_ZOOM_DUR_MAX",     2.5)
    SCREEN_Y_TARGET = getattr(cfg, "FACE_ZOOM_SCREEN_Y",   0.30)
    SEARCH_WINDOW   = getattr(cfg, "FACE_ZOOM_SEARCH_WINDOW", 3.0)
    MIN_GAP         = getattr(cfg, "FACE_ZOOM_MIN_GAP",    1.0)

    pz_start = prod_trigger["trigger_t"] if prod_trigger else None
    pz_end   = (pz_start + getattr(cfg, "ZOOM_DURATION", 3.0)) if pz_start else None

    trigger_times = []
    word_counter  = 0
    cycle_idx     = 0
    next_n        = WORDS_PER_ZOOM[0]

    for wd in clip_words:
        t0 = wd["start"]
        if t0 <= hook_end:
            continue
        word_counter += 1
        if word_counter >= next_n:
            word_counter = 0
            cycle_idx    = (cycle_idx + 1) % len(WORDS_PER_ZOOM)
            next_n       = WORDS_PER_ZOOM[cycle_idx]
            if t0 < clip_duration - 2.0:
                trigger_times.append(t0)

    face_zooms = []
    last_end   = 0.0

    for trigger_t in trigger_times:
        if trigger_t < last_end + MIN_GAP:
            continue
        if pz_start is not None:
            fz_est_end = trigger_t + DUR_MAX
            if not (fz_est_end < pz_start or trigger_t > pz_end):
                continue

        scale   = round(random.uniform(SCALE_MIN, SCALE_MAX), 3)
        ease_in = round(random.uniform(EASE_MIN,  EASE_MAX),  3)
        dur     = round(random.uniform(DUR_MIN,   DUR_MAX),   2)

        best_ev, best_delta = None, float("inf")
        for ev in face_events:
            ev_t  = ev.get("relative_start", ev.get("start_time", 0))
            delta = abs(ev_t - trigger_t)
            if delta < SEARCH_WINDOW and delta < best_delta:
                best_ev, best_delta = ev, delta

        if best_ev:
            bbox = best_ev.get("best_bbox")
            fw   = best_ev.get("frame_w", 1)
            fh   = best_ev.get("frame_h", 1)
            if bbox and fw > 0 and fh > 0:
                face_cx_orig = (bbox[0]+bbox[2])/2.0/fw
                face_cy_orig = (bbox[1]+bbox[3])/2.0/fh
                cx = max(0.15, min(0.85, face_cx_orig))
                cy = face_cy_orig + (0.5 - SCREEN_Y_TARGET) / scale
                cy = max(SCREEN_Y_TARGET, min(1.0 - SCREEN_Y_TARGET, cy))
            else:
                cx, cy = 0.50, 0.22 + (0.5 - SCREEN_Y_TARGET) / scale
        else:
            cx = 0.50
            cy = 0.22 + (0.5 - SCREEN_Y_TARGET) / scale
            cy = max(SCREEN_Y_TARGET, min(1.0 - SCREEN_Y_TARGET, cy))

        fz_end = min(trigger_t + dur, clip_duration - 0.05)
        face_zooms.append({
            "start":   trigger_t,
            "end":     fz_end,
            "cx":      cx,
            "cy":      cy,
            "scale":   scale,
            "ease_in": ease_in,
        })
        last_end = fz_end

    return face_zooms


# =============================================================================
#  UTILITIES
# =============================================================================

def _chunk_words(clip_words: list, words_per_chunk: int = 4) -> list[list]:
    """Group words into chunks of N for subtitle display."""
    chunks = []
    i = 0
    while i < len(clip_words):
        chunks.append(clip_words[i:i+words_per_chunk])
        i += words_per_chunk
    return chunks


def _strip_karaoke_word_punctuation(text: str) -> str:
    text = re.sub(r"[^\w\s]", "", str(text), flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    rupiah_match = re.fullmatch(r"(?:rp|idr|rupiah)\s*(\d+)", text, flags=re.IGNORECASE)
    if rupiah_match:
        return _format_rupiah_compact(rupiah_match.group(1))
    price_match = re.fullmatch(r"(\d+)\s*(rb|ribu|ribuan|ribunya)", text, flags=re.IGNORECASE)
    if price_match:
        return f"{price_match.group(1)}rb"
    return text


def _format_karaoke_display_word(text: str) -> str:
    word = str(text or "").strip()
    if re.fullmatch(r"\d+rb", word, flags=re.IGNORECASE):
        return word.lower()
    return word.upper()


def _prepare_karaoke_words(clip_words: list) -> list[dict]:
    prepared = []
    clip_words = clip_words or []
    i = 0
    while i < len(clip_words):
        word_data = clip_words[i]
        clean_word = _strip_karaoke_word_punctuation(word_data.get("word", ""))
        if not clean_word:
            i += 1
            continue

        if clean_word.lower() in {"rp", "idr", "rupiah"} and i + 1 < len(clip_words):
            next_word_data = clip_words[i + 1]
            next_clean_word = _strip_karaoke_word_punctuation(next_word_data.get("word", ""))
            if re.fullmatch(r"\d+", next_clean_word):
                clean_word_data = dict(word_data)
                clean_word_data["word"] = _format_rupiah_compact(next_clean_word)
                clean_word_data["end"] = next_word_data.get("end", clean_word_data.get("end"))
                prepared.append(clean_word_data)
                i += 2
                continue

        clean_word_data = dict(word_data)
        clean_word_data["word"] = clean_word
        prepared.append(clean_word_data)
        i += 1
    return prepared


def _normalize_highlight_phrase(text: str) -> str:
    return _normalize_subtitle_match_text(text)


def _normalized_highlight_tokens(text: str) -> list[str]:
    return [tok for tok in _normalize_highlight_phrase(text).split(" ") if tok]


def _empty_highlight_phrase_config() -> dict:
    return {
        "version": 1,
        "categories": {category: [] for category in _HIGHLIGHT_CATEGORY_ORDER},
    }


def _highlight_phrase_config_path(cfg) -> Path:
    path_value = getattr(cfg, "HIGHLIGHT_PHRASES_PATH", "highlight_phrases.json")
    path = Path(str(path_value))
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return path


def _coerce_highlight_category(category: object) -> Optional[str]:
    normalized = _normalize_subtitle_match_text(str(category or ""))
    if normalized in _HIGHLIGHT_CATEGORY_ORDER:
        return normalized
    return _HIGHLIGHT_CATEGORY_ALIASES.get(normalized)


def _normalize_highlight_phrase_config(payload: object) -> dict:
    cleaned = _empty_highlight_phrase_config()
    if not isinstance(payload, dict):
        return cleaned

    raw_categories = payload.get("categories", {})
    if not isinstance(raw_categories, dict):
        return cleaned

    assigned: dict[str, str] = {}
    ordered_raw_categories = list(_HIGHLIGHT_CATEGORY_ORDER) + [
        key for key in raw_categories.keys() if key not in _HIGHLIGHT_CATEGORY_ORDER
    ]

    for raw_category in ordered_raw_categories:
        category = _coerce_highlight_category(raw_category)
        if not category:
            continue

        phrases = raw_categories.get(raw_category, [])
        if not isinstance(phrases, list):
            continue

        for phrase in phrases:
            normalized = _normalize_highlight_phrase(str(phrase or ""))
            if not normalized:
                continue
            if normalized in assigned:
                if assigned[normalized] != category:
                    log.warning(
                        "Highlight phrase %r is configured as both %s and %s; using %s",
                        normalized,
                        assigned[normalized],
                        category,
                        assigned[normalized],
                    )
                continue

            cleaned["categories"][category].append(normalized)
            assigned[normalized] = category

    return cleaned


def _load_highlight_phrase_config_unlocked(path: Path) -> dict:
    if not path.exists():
        return _empty_highlight_phrase_config()

    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        log.warning(f"Could not load highlight phrase config {path}: {e}")
        return _empty_highlight_phrase_config()

    return _normalize_highlight_phrase_config(payload)


def _save_highlight_phrase_config_unlocked(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=True)
        f.write("\n")
    os.replace(tmp_path, path)


def _highlight_path_stamp(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _register_highlight_flush_unlocked() -> None:
    global _HIGHLIGHT_FLUSH_REGISTERED
    if _HIGHLIGHT_FLUSH_REGISTERED:
        return
    atexit.register(flush_highlight_phrase_config)
    _HIGHLIGHT_FLUSH_REGISTERED = True


def _cached_highlight_phrase_config_unlocked(path: Path) -> dict:
    cache_path = _HIGHLIGHT_CONFIG_CACHE.get("path")
    cached_payload = _HIGHLIGHT_CONFIG_CACHE.get("payload")
    current_stamp = _highlight_path_stamp(path)
    cache_matches = cache_path == path and cached_payload is not None

    if cache_matches and _HIGHLIGHT_CONFIG_CACHE.get("dirty"):
        return cached_payload

    if cache_matches and _HIGHLIGHT_CONFIG_CACHE.get("stamp") == current_stamp:
        return cached_payload

    payload = _load_highlight_phrase_config_unlocked(path)
    _HIGHLIGHT_CONFIG_CACHE.update({
        "path": path,
        "stamp": current_stamp,
        "payload": payload,
        "dirty": False,
        "version": int(_HIGHLIGHT_CONFIG_CACHE.get("version", 0)) + 1,
    })
    _HIGHLIGHT_MATCHER_CACHE.clear()
    _register_highlight_flush_unlocked()
    return payload


def _load_highlight_phrase_config(cfg) -> dict:
    path = _highlight_phrase_config_path(cfg)
    with _HIGHLIGHT_CONFIG_LOCK:
        return _cached_highlight_phrase_config_unlocked(path)


def flush_highlight_phrase_config(cfg=None) -> None:
    path = _highlight_phrase_config_path(cfg) if cfg is not None else _HIGHLIGHT_CONFIG_CACHE.get("path")
    if path is None:
        return

    with _HIGHLIGHT_CONFIG_LOCK:
        payload = _HIGHLIGHT_CONFIG_CACHE.get("payload")
        if not _HIGHLIGHT_CONFIG_CACHE.get("dirty") or payload is None:
            return
        _save_highlight_phrase_config_unlocked(Path(path), payload)
        _HIGHLIGHT_CONFIG_CACHE["stamp"] = _highlight_path_stamp(Path(path))
        _HIGHLIGHT_CONFIG_CACHE["dirty"] = False
        log.info(f"Flushed learned highlight phrases to {path}")


def _index_highlight_phrases(phrase_config: dict) -> dict[str, str]:
    index = {}
    categories = phrase_config.get("categories", {}) if isinstance(phrase_config, dict) else {}
    for category in _HIGHLIGHT_CATEGORY_ORDER:
        for phrase in categories.get(category, []) or []:
            normalized = _normalize_highlight_phrase(str(phrase or ""))
            if normalized and normalized not in index:
                index[normalized] = category
    return index


def _discover_highlight_phrase_candidates(moment: Optional[dict]) -> list[dict]:
    if not isinstance(moment, dict):
        return []

    default_category = _coerce_highlight_category(moment.get("keyword_category")) or "benefit"
    candidates = []

    for keyword in moment.get("keywords_found", []) or []:
        if not isinstance(keyword, dict):
            continue

        category = _coerce_highlight_category(keyword.get("category")) or default_category
        phrase_source = "context" if keyword.get("context") else "word"
        phrase = _normalize_highlight_phrase(keyword.get(phrase_source, ""))
        if not phrase:
            continue

        candidates.append({
            "phrase": phrase,
            "category": category,
            "source": f"transcript_context.{phrase_source}",
        })

    return candidates


def _learn_highlight_phrases_from_moment(moment: Optional[dict], cfg) -> dict:
    path = _highlight_phrase_config_path(cfg)
    candidates = _discover_highlight_phrase_candidates(moment)

    with _HIGHLIGHT_CONFIG_LOCK:
        phrase_config = _cached_highlight_phrase_config_unlocked(path)
        if not candidates:
            return phrase_config

        existing_category_by_phrase = _index_highlight_phrases(phrase_config)
        learned = []

        for candidate in candidates:
            phrase = candidate["phrase"]
            category = candidate["category"]
            existing_category = existing_category_by_phrase.get(phrase)

            if existing_category:
                if existing_category != category:
                    log.debug(
                        "Highlight phrase %r already configured as %s; ignoring discovered category %s",
                        phrase,
                        existing_category,
                        category,
                    )
                continue

            phrase_config["categories"][category].append(phrase)
            existing_category_by_phrase[phrase] = category
            learned.append(candidate)

        if learned:
            _HIGHLIGHT_CONFIG_CACHE["dirty"] = True
            _HIGHLIGHT_CONFIG_CACHE["version"] = int(_HIGHLIGHT_CONFIG_CACHE.get("version", 0)) + 1
            _HIGHLIGHT_MATCHER_CACHE.clear()
            log.info(
                "Learned %d highlight phrase(s) from moment config=%s",
                len(learned),
                str(path),
            )
            for item in learned:
                log.debug(
                    "Learned highlight phrase category=%s phrase=%r source=%s config=%s",
                    item["category"],
                    item["phrase"],
                    item["source"],
                    str(path),
                )

        return phrase_config


def _build_highlight_rules(cfg, phrase_config: Optional[dict] = None) -> list[dict]:
    phrase_config = phrase_config or _load_highlight_phrase_config(cfg)
    categories = phrase_config.get("categories", {}) if isinstance(phrase_config, dict) else {}
    rules = []

    source_order = 0
    for category in _HIGHLIGHT_CATEGORY_ORDER:
        color = _highlight_color_for_category(category, cfg)
        if not color:
            continue

        for phrase in categories.get(category, []) or []:
            tokens = _normalized_highlight_tokens(phrase)
            if not tokens:
                continue
            rules.append({
                "category": category,
                "color": color,
                "tokens": tokens,
                "source_order": source_order,
            })
            source_order += 1

    rules.sort(key=lambda rule: (-len(rule["tokens"]), rule["source_order"]))
    return rules


def _highlight_color_key(cfg) -> tuple[str, str, str]:
    return (
        str(getattr(cfg, "HIGHLIGHT_YELLOW_COLOR", "#FFD600")),
        str(getattr(cfg, "HIGHLIGHT_GREEN_COLOR", "#00C853")),
        str(getattr(cfg, "HIGHLIGHT_RED_COLOR", "#FF3B30")),
    )


def _get_highlight_matcher(cfg, phrase_config: Optional[dict] = None) -> _HighlightMatcher:
    path = _highlight_phrase_config_path(cfg)
    with _HIGHLIGHT_CONFIG_LOCK:
        if phrase_config is None:
            phrase_config = _cached_highlight_phrase_config_unlocked(path)
        version = int(_HIGHLIGHT_CONFIG_CACHE.get("version", 0))
        cache_key = (path, version, _highlight_color_key(cfg))
        matcher = _HIGHLIGHT_MATCHER_CACHE.get(cache_key)
        if matcher is None:
            rules = _build_highlight_rules(cfg, phrase_config=phrase_config)
            matcher = _HighlightMatcher(rules)
            _HIGHLIGHT_MATCHER_CACHE.clear()
            _HIGHLIGHT_MATCHER_CACHE[cache_key] = matcher
            log.debug(f"Built highlight matcher with {len(rules)} phrase rules")
        return matcher


def _highlight_color_for_category(category: str, cfg) -> Optional[str]:
    normalized = _coerce_highlight_category(category)
    if normalized == "benefit":
        return getattr(cfg, "HIGHLIGHT_YELLOW_COLOR", "#FFD600")
    if normalized == "result":
        return getattr(cfg, "HIGHLIGHT_GREEN_COLOR", "#00C853")
    if normalized == "pain":
        return getattr(cfg, "HIGHLIGHT_RED_COLOR", "#FF3B30")
    return None


def _resolve_highlight_word_colors(karaoke_words: list, highlight_rules) -> list[Optional[str]]:
    if isinstance(highlight_rules, _HighlightMatcher):
        return highlight_rules.resolve_word_colors(karaoke_words)

    if not karaoke_words or not highlight_rules:
        return [None] * len(karaoke_words)

    token_entries = []
    for word_idx, word_data in enumerate(karaoke_words):
        for token in _normalized_highlight_tokens(word_data.get("word", "")):
            token_entries.append({
                "token": token,
                "word_idx": word_idx,
            })

    if not token_entries:
        return [None] * len(karaoke_words)

    token_colors: list[Optional[str]] = [None] * len(token_entries)
    token_match_lengths = [0] * len(token_entries)

    for rule in highlight_rules:
        rule_tokens = rule["tokens"]
        rule_len = len(rule_tokens)
        if rule_len == 0 or rule_len > len(token_entries):
            continue

        for start_idx in range(0, len(token_entries) - rule_len + 1):
            window_tokens = [entry["token"] for entry in token_entries[start_idx:start_idx + rule_len]]
            if window_tokens != rule_tokens:
                continue

            for token_idx in range(start_idx, start_idx + rule_len):
                if rule_len > token_match_lengths[token_idx]:
                    token_match_lengths[token_idx] = rule_len
                    token_colors[token_idx] = rule["color"]

    word_colors: list[Optional[str]] = [None] * len(karaoke_words)
    word_match_lengths = [0] * len(karaoke_words)
    for token_idx, entry in enumerate(token_entries):
        color = token_colors[token_idx]
        if not color:
            continue
        word_idx = entry["word_idx"]
        match_len = token_match_lengths[token_idx]
        if match_len > word_match_lengths[word_idx]:
            word_match_lengths[word_idx] = match_len
            word_colors[word_idx] = color

    return word_colors


def _build_highlight_plan(clip_words: list, cfg, moment: Optional[dict] = None) -> dict:
    karaoke_words = _prepare_karaoke_words(clip_words)
    for idx, word_data in enumerate(karaoke_words):
        word_data["_highlight_idx"] = idx

    phrase_config = _learn_highlight_phrases_from_moment(moment, cfg)
    highlight_rules = _get_highlight_matcher(cfg, phrase_config=phrase_config)

    word_colors = _resolve_highlight_word_colors(karaoke_words, highlight_rules)
    return {
        "words": karaoke_words,
        "word_colors": word_colors,
    }


def _plan_emoji_overlays(clip_words: list, clip_duration: float, W: int, H: int, cfg) -> list[dict]:
    """Plan emoji overlays from subtitle chunks using real word timestamps."""
    emoji_cfg = getattr(cfg, "EMOJI_CONFIG", {}) or {}
    emoji_rules = emoji_cfg.get("emoji_rules") or []
    karaoke_words = _prepare_karaoke_words(clip_words)
    if not karaoke_words or not emoji_rules:
        return []

    sub_y_frac = float(getattr(cfg, "SUBTITLE_Y_POS", 0.80))
    ass_fontsize = int(getattr(cfg, "SUBTITLE_FONTSIZE", 68) * 0.85)
    line_gap = int(ass_fontsize * 1.18)
    y_line2 = int(H * sub_y_frac)
    y_line1 = y_line2 - line_gap
    subtitle_block_top = max(0, int(y_line1 - ass_fontsize * 0.9))
    subtitle_block_bottom = min(H, int(y_line2 + ass_fontsize * 0.9))
    subtitle_block_left = max(0, int(W * 0.5 - W * 0.24))
    subtitle_block_right = min(W, int(W * 0.5 + W * 0.24))
    pad_x = max(24, int(W * 0.05))
    pad_y = max(24, int(H * 0.04))
    fade_in = float(emoji_cfg.get("fade_in", 0.15) or 0.0)
    overlays = []

    for chunk_idx, chunk in enumerate(_chunk_words(karaoke_words, words_per_chunk=4)):
        if not chunk:
            continue

        chunk_start = max(0.0, float(chunk[0]["start"]))
        chunk_end = min(clip_duration, float(chunk[-1]["end"]))
        if chunk_end <= chunk_start + 0.01:
            continue

        chunk_text = _normalize_subtitle_match_text(" ".join(str(w.get("word", "")) for w in chunk))
        if not chunk_text:
            continue

        matched_rule = None
        for rule in emoji_rules:
            if _chunk_matches_emoji_rule(chunk_text, rule):
                matched_rule = rule
                break
        if not matched_rule:
            continue

        asset_path = _resolve_emoji_asset_path(matched_rule.get("png_path", ""))
        if not asset_path:
            log.warning(f"Emoji asset missing for rule: {matched_rule}")
            continue

        scale = float(matched_rule.get("scale", 0.20) or 0.20)
        size_px = max(44, int(min(W, H) * min(scale * 0.62, 0.12)))
        offset_x = int(matched_rule.get("offset_x", 0) or 0)
        offset_y = int(matched_rule.get("offset_y", 0) or 0)

        seed = f"{chunk_idx}:{chunk_start:.3f}:{chunk_text}:{asset_path}"
        rng = random.Random(seed)
        half_w = size_px // 2
        half_h = size_px // 2
        gap_x = max(10, min(24, int(size_px * 0.18)))
        gap_y = max(8, min(20, int(size_px * 0.14)))

        left_x = max(pad_x + half_w, subtitle_block_left - half_w - gap_x)
        right_x = min(W - pad_x - half_w, subtitle_block_right + half_w + gap_x)
        above_y = max(pad_y + half_h, subtitle_block_top - half_h - gap_y)
        below_y = min(H - pad_y - half_h, subtitle_block_bottom + half_h + gap_y)
        mid_y = min(H - pad_y - half_h, max(pad_y + half_h, int((subtitle_block_top + subtitle_block_bottom) * 0.5)))

        candidate_positions = [
            (left_x, above_y),
            (right_x, above_y),
            (left_x, mid_y),
            (right_x, mid_y),
        ]
        if below_y > subtitle_block_bottom + half_h:
            candidate_positions.extend([
                (left_x, below_y),
                (right_x, below_y),
            ])

        base_x, base_y = candidate_positions[rng.randrange(len(candidate_positions))]
        jitter_x = rng.randint(-max(4, int(size_px * 0.10)), max(4, int(size_px * 0.10)))
        jitter_y = rng.randint(-max(4, int(size_px * 0.08)), max(4, int(size_px * 0.08)))

        center_x = int(base_x + jitter_x + offset_x)
        center_y = int(base_y + jitter_y + offset_y)

        center_x = max(pad_x + half_w, min(W - pad_x - half_w, center_x))
        center_y = max(pad_y + half_h, min(H - pad_y - half_h, center_y))

        if subtitle_block_left <= center_x <= subtitle_block_right and subtitle_block_top <= center_y <= subtitle_block_bottom:
            side = -1 if center_x < W * 0.5 else 1
            center_x = subtitle_block_left - half_w - gap_x if side < 0 else subtitle_block_right + half_w + gap_x
            center_x = max(pad_x + half_w, min(W - pad_x - half_w, center_x))
            center_y = max(pad_y + half_h, min(H - pad_y - half_h, above_y))

        overlays.append({
            "path": asset_path,
            "start": round(chunk_start, 6),
            "end": round(chunk_end, 6),
            "size_px": size_px,
            "center_x": center_x,
            "center_y": center_y,
            "fade_in": fade_in,
            "fade_out": min(fade_in, max(0.0, (chunk_end - chunk_start) / 2.0)),
        })

    return overlays


def _pick_before_after(cfg) -> Optional[str]:
    ba_dir = Path(getattr(cfg, "BEFORE_AFTER_DIR", "assets/before_after"))
    if not ba_dir.exists():
        return None
    exts = {".jpg", ".jpeg", ".png", ".webp"}
    imgs = [p for p in ba_dir.iterdir() if p.suffix.lower() in exts]
    return str(random.choice(imgs)) if imgs else None


def _pick_bgm(cfg) -> Optional[str]:
    bgm_dir = Path(getattr(cfg, "BGM_DIR", "assets/bgm"))
    if not bgm_dir.exists():
        return None
    tracks = [p for p in bgm_dir.iterdir() if p.is_file() and p.suffix.lower() in _AUDIO_EXTS]
    if not tracks:
        log.debug(f"BGM folder empty: {bgm_dir}")
        return None
    chosen = random.choice(tracks)
    log.debug(f"BGM: {chosen.name}")
    return str(chosen)


def _prepare_broll_intro(cfg, clip_duration: Optional[float] = None, hook_end: Optional[float] = None) -> Optional[dict]:
    if not getattr(cfg, "BROLL_INTRO_ENABLED", True):
        return None

    intro_path = str(getattr(cfg, "_broll_intro_path", "") or "").strip()
    if not intro_path:
        return None

    path = Path(intro_path)
    if not path.exists() or not path.is_file():
        log.warning(f"B-roll intro file missing: {intro_path}")
        return None
    if path.suffix.lower() not in _VIDEO_EXTS:
        log.warning(f"B-roll intro file has unsupported extension: {intro_path}")
        return None

    info = _probe_video(str(path))
    if not info:
        return None

    source_duration = max(0.0, float(info.get("duration") or 0.0))
    if source_duration <= 0.1:
        return None

    try:
        requested_duration = float(getattr(cfg, "_broll_intro_duration", 0.0) or 0.0)
    except (TypeError, ValueError):
        requested_duration = 0.0
    if requested_duration <= 0.0:
        try:
            requested_duration = float(getattr(cfg, "BROLL_INTRO_MAX_DURATION", 2.5) or 2.5)
        except (TypeError, ValueError):
            requested_duration = 2.5

    if hook_end is not None and hook_end > 0.0:
        requested_duration = min(requested_duration, float(hook_end))
    if clip_duration is not None and clip_duration > 0.0:
        requested_duration = min(requested_duration, float(clip_duration))

    duration = max(0.1, requested_duration)
    return {
        "path": str(path),
        "duration": duration,
        "has_audio": bool(info.get("has_audio", False)),
    }


def _clamp_float(value, lo: float, hi: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(lo, min(hi, number))


def _probe_video(path: str) -> Optional[dict]:
    """Use ffprobe to get width, height, duration of a video file."""
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_streams", "-show_format",
                path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(r.stdout)
        streams = data.get("streams", [])
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        for s in streams:
            if s.get("codec_type") == "video":
                fps = _parse_ffprobe_fps(
                    s.get("avg_frame_rate")
                    or s.get("r_frame_rate")
                    or s.get("codec_time_base")
                )
                return {
                    "width":    int(s["width"]),
                    "height":   int(s["height"]),
                    "duration": float(data["format"].get("duration", s.get("duration", 30))),
                    "fps":      fps,
                    "has_audio": has_audio,
                }
    except Exception as e:
        log.error(f"ffprobe failed for {path}: {e}")
    return None


def _parse_ffprobe_fps(value: Optional[str]) -> Optional[float]:
    """Parse FFprobe frame-rate strings such as '30000/1001'."""
    if not value:
        return None
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            den_f = float(den)
            if den_f == 0:
                return None
            fps = float(num) / den_f
        else:
            fps = float(value)
        return fps if fps > 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _normalize_subtitle_match_text(text: str) -> str:
    text = re.sub(r"[^\w\s]", " ", str(text).lower(), flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _split_hook_text_lines(text: str, max_chars_per_line: int = 18) -> list[str]:
    text = " ".join(str(text or "").split())
    if not text:
        return []

    words = text.split(" ")
    if len(words) <= 1 or len(text) <= max_chars_per_line:
        return [text]

    best_lines = [text]
    best_score = None

    for i in range(1, len(words)):
        line1 = " ".join(words[:i]).strip()
        line2 = " ".join(words[i:]).strip()
        if not line1 or not line2:
            continue

        overflow = max(0, len(line1) - max_chars_per_line) + max(0, len(line2) - max_chars_per_line)
        balance = abs(len(line1) - len(line2))
        one_word_penalty = 6 if len(words) >= 4 and (i == 1 or i == len(words) - 1) else 0
        score = (overflow * 100) + balance + one_word_penalty

        if best_score is None or score < best_score:
            best_score = score
            best_lines = [line1, line2]

    return best_lines[:2]


def _add_hook_text_block(
    fc: list[str],
    vid: str,
    text: str,
    frame_width: int,
    font_path: str,
    font_size: int,
    font_color: str,
    stroke_width: int,
    stroke_color: str,
    shadow_color: str,
    center_y: int,
    start_t: float,
    end_t: float,
    block_tag: str,
    width_ratio: float = 0.9,
    x_expr: str = "(w-text_w)/2",
) -> str:
    clean_text = " ".join(str(text or "").upper().split())
    if not clean_text:
        return vid

    font_arg = f":fontfile='{font_path}'" if font_path and Path(font_path).exists() else ""
    draw_fs = max(28, int(font_size * 0.7))
    approx_max_chars = max(10, int((frame_width * width_ratio) / max(draw_fs * 0.38, 1.0)))
    hook_lines = _split_hook_text_lines(clean_text, max_chars_per_line=approx_max_chars)
    line_gap_px = max(8, int(draw_fs * 0.9))
    first_line_y = center_y - int(((len(hook_lines) - 1) * line_gap_px) / 2)

    for idx, line in enumerate(hook_lines, start=1):
        safe_hook = _escape_drawtext(line)
        line_y = first_line_y + (idx - 1) * line_gap_px
        line_tag = f"{block_tag}{idx}"
        fc.append(
            f"{vid}drawtext=text='{safe_hook}'{font_arg}"
            f":fontsize={draw_fs}:fontcolor={font_color}"
            f":borderw={stroke_width}:bordercolor={stroke_color}"
            f":shadowcolor={shadow_color}:shadowx=3:shadowy=3"
            f":x={x_expr}:y={line_y}"
            f":enable='between(t,{start_t:.2f},{end_t:.2f})'[{line_tag}]"
        )
        vid = f"[{line_tag}]"

    return vid


def _chunk_matches_emoji_rule(chunk_text: str, rule: dict) -> bool:
    padded_text = f" {chunk_text} "
    for keyword in rule.get("keywords", []) or []:
        normalized_keyword = _normalize_subtitle_match_text(keyword)
        if normalized_keyword and f" {normalized_keyword} " in padded_text:
            return True
    return False


def _resolve_emoji_asset_path(path_str: str) -> Optional[str]:
    if not path_str:
        return None

    candidate = Path(path_str)
    if candidate.exists():
        return str(candidate)

    candidate_dir = candidate.parent if str(candidate.parent) not in {"", "."} else Path("assets/emojis")
    if not candidate_dir.exists():
        return None

    stem = candidate.stem.lower()
    suffix = candidate.suffix.lower() or ".png"
    fallback_names = [candidate.name.lower()]
    if not stem.endswith("s"):
        fallback_names.append(f"{stem}s{suffix}")
    if stem.endswith("s") and stem[:-1]:
        fallback_names.append(f"{stem[:-1]}{suffix}")

    for asset in candidate_dir.iterdir():
        if asset.name.lower() in fallback_names:
            return str(asset)

    for asset in candidate_dir.iterdir():
        if asset.suffix.lower() == suffix and asset.stem.lower().startswith(stem):
            return str(asset)

    return None


def _run_ffmpeg(cmd: list, output_path: str, timeout: int = 600) -> bool:
    """Run an FFmpeg command and check output."""
    try:
        creationflags = 0
        if os.environ.get("PROYA_QUEUE_FFMPEG_BELOW_NORMAL") == "1":
            creationflags = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=creationflags,
        )
        if r.returncode != 0:
            log.error(f"FFmpeg error:\n{r.stderr[-500:]}")
            return False
        p = Path(output_path)
        if not p.exists() or p.stat().st_size < 1024:
            log.error(f"FFmpeg produced empty output: {output_path}")
            p.unlink(missing_ok=True)
            return False
        return True
    except subprocess.TimeoutExpired:
        log.error(f"FFmpeg timed out: {output_path}")
        return False
    except FileNotFoundError:
        raise RuntimeError("ffmpeg not found — install from https://ffmpeg.org")


def _font_name_from_path(font_str: str) -> str:
    """
    Extract font name for ASS header from a font path or name string.
    ASS uses the font *family name*, not file path.
    Attempt to extract from filename; fall back to Arial.
    """
    if not font_str:
        return "Arial"
    p = Path(font_str)
    if p.suffix.lower() in (".ttf", ".otf"):
        # Use stem as display name (e.g. "Montserrat-ExtraBold")
        return p.stem.replace("-", " ").replace("_", " ")
    return font_str  # already a name


def _resolve_subtitle_font(cfg) -> tuple[str, Optional[str]]:
    default_font = getattr(cfg, "FONT_SUBTITLE", "Arial")
    default_name = _font_name_from_path(default_font)

    if not getattr(cfg, "SUBTITLE_FONT_RANDOMIZE", True):
        return default_name, None

    font_dir_raw = getattr(cfg, "SUBTITLE_FONT_DIR", "assets/fonts/subtitle")
    if not font_dir_raw:
        return default_name, None

    try:
        font_dir = Path(font_dir_raw)
        candidates = [
            p for p in font_dir.iterdir()
            if p.is_file() and p.suffix.lower() in (".ttf", ".otf")
        ]
        if not candidates:
            return default_name, None

        selected_font = random.choice(candidates)
        selected_name = _font_name_from_font_file(selected_font)
        if not selected_name:
            return default_name, None

        return selected_name, str(font_dir)
    except Exception:
        return default_name, None


def _font_name_from_font_file(font_path: Path) -> Optional[str]:
    try:
        from PIL import ImageFont

        font = ImageFont.truetype(str(font_path), size=32)
        family, style = font.getname()
    except Exception:
        return None

    family = str(family or "").strip()
    style = str(style or "").strip()
    if not family:
        return None

    style_key = re.sub(r"\s+", "", style).lower()
    family_key = re.sub(r"\s+", "", family).lower()
    if style_key and style_key not in {"regular", "normal", "book", "roman"} and style_key not in family_key:
        return f"{family} {style}"
    return family


def _escape_ass_filter_path(path_value: str) -> str:
    safe = Path(path_value).as_posix()
    return (
        safe.replace("\\", "/")
        .replace(":", r"\:")
        .replace(",", r"\,")
        .replace("[", r"\[")
        .replace("]", r"\]")
        .replace("'", r"\'")
    )


def _css_to_ffmpeg_color(color: str) -> str:
    """Convert CSS color (#RRGGBB or named) to FFmpeg color string (0xRRGGBB)."""
    named = {
        "white": "0xFFFFFF", "black": "0x000000", "yellow": "0xFFD600",
        "red": "0xFF0000", "green": "0x00C853", "gold": "0xFFD700",
    }
    c = str(color).strip()
    if c.lower() in named:
        return named[c.lower()]
    if c.startswith("#"):
        return "0x" + c[1:].upper()
    return c


def _escape_drawtext(text: str) -> str:
    """Escape special characters for FFmpeg drawtext filter."""
    # Order matters — escape backslash first
    text = text.replace("\\", "\\\\")
    text = text.replace("'",  "\\'")
    text = text.replace(":",  "\\:")
    text = text.replace("%",  "\\%")
    text = text.replace("[",  "\\[")
    text = text.replace("]",  "\\]")
    return text
