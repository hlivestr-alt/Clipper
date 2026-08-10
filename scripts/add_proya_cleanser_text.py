#!/usr/bin/env python3
"""Burn professionally styled Indonesian text into a PROYA cleanser video."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Sequence


TARGET_WIDTH = 720
TARGET_HEIGHT = 1280
EXPECTED_DURATION_SECONDS = 15.0
DEFAULT_DURATION_TOLERANCE_SECONDS = 1.0
CHECK_MARK = "\u2713"
CROSS_MARK = "\u2715"
PRESET_CLEANSER = "cleanser"
PRESET_VITAMIN_C_SERUM = "vitamin-c-serum"
PRESET_VITAMIN_C_SHEET_MASK = "vitamin-c-sheet-mask"
SUPPORTED_PRESETS = (
    PRESET_CLEANSER,
    PRESET_VITAMIN_C_SERUM,
    PRESET_VITAMIN_C_SHEET_MASK,
)


class ProyaTextError(RuntimeError):
    """Raised for an expected, user-actionable processing failure."""


@dataclass(frozen=True)
class FontAsset:
    path: Path
    face_name: str


@dataclass(frozen=True)
class FontSelection:
    headline: FontAsset
    caption: FontAsset
    benefit: FontAsset
    benefit_marker: str
    cross_marker: str = "X"
    label: FontAsset | None = None


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    has_audio: bool


@dataclass(frozen=True)
class OverlayEvent:
    start: float
    end: float
    style: str
    text: str
    x: int
    y: int
    alignment: int
    marker: str | None = None
    suffix_marker: str | None = None
    marker_color: str = "&H004BCB72&"
    horizontal_scale: int | None = None
    font_size: int | None = None
    fade_in_ms: int = 100
    fade_in_delay_ms: int = 0
    fade_out_ms: int = 100
    pop_in_ms: int | None = None
    pop_start_scale: int = 90
    slide_in_px: int = 0
    move_out_up_px: int = 0
    wiggle_at_ms: int | None = None
    drawing: bool = False
    layer: int = 0


def timestamp_to_ass(seconds: float) -> str:
    """Convert seconds to ASS H:MM:SS.cc time, rounded to centiseconds."""
    value = float(seconds)
    if not math.isfinite(value) or value < 0:
        raise ValueError("ASS timestamps must be finite and non-negative")
    total_centiseconds = int(value * 100.0 + 0.5)
    hours, remainder = divmod(total_centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    whole_seconds, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{centiseconds:02d}"


def escape_ass_text(text: str) -> str:
    """Escape user-visible text while preserving explicit line breaks."""
    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\\", r"\\")
    normalized = normalized.replace("{", r"\{").replace("}", r"\}")
    return normalized.replace("\n", r"\N")


def discover_fonts(assets_dir: Path) -> list[Path]:
    """Return supported fonts below an assets directory in stable order."""
    root = Path(assets_dir).expanduser().resolve()
    if not root.is_dir():
        raise ProyaTextError(f"Assets directory does not exist: {root}")

    fonts = sorted(
        {
            path.resolve()
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in {".ttf", ".otf"}
        },
        key=lambda path: path.as_posix().lower(),
    )
    if not fonts:
        raise ProyaTextError(
            f"No supported .ttf or .otf fonts found under searched directory: {root}"
        )
    return fonts


def _font_face_name(path: Path) -> str:
    try:
        from PIL import ImageFont

        family, style = ImageFont.truetype(str(path), size=32).getname()
        family = str(family or "").strip()
        style = str(style or "").strip()
        if family:
            style_key = re.sub(r"\s+", "", style).lower()
            family_key = re.sub(r"\s+", "", family).lower()
            if (
                style_key
                and style_key not in {"regular", "normal", "book", "roman"}
                and style_key not in family_key
            ):
                return f"{family} {style}"
            return family
    except Exception:
        pass

    fallback = re.sub(r"[_-]+", " ", path.stem)
    return re.sub(r"\s+", " ", fallback).strip() or "Sans"


def font_supports_character(path: Path, character: str) -> bool:
    """Return whether a font cmap contains a character; fail closed."""
    try:
        from fontTools.ttLib import TTFont

        font = TTFont(str(path), lazy=True)
        try:
            return ord(character) in (font.getBestCmap() or {})
        finally:
            font.close()
    except Exception:
        return False


def _font_score(path: Path, role: str) -> int:
    name = re.sub(r"[^a-z0-9]+", " ", path.stem.lower())
    score = 0

    sans_hints = (
        "sans",
        "grotesk",
        "inter",
        "montserrat",
        "poppins",
        "outfit",
        "nunito",
        "roboto",
        "helvetica",
        "arial",
        "tiktok",
    )
    display_or_serif_hints = (
        "serif",
        "playfair",
        "times",
        "georgia",
        "lobster",
        "script",
        "display",
        "italic",
    )
    if any(hint in name for hint in sans_hints):
        score += 35
    if any(hint in name for hint in display_or_serif_hints):
        score -= 80

    if role == "headline":
        preferences = (
            ("extra bold", 125),
            ("extrabold", 125),
            ("ultra bold", 120),
            ("black", 115),
            ("heavy", 110),
            ("bold", 95),
            ("semi bold", 75),
            ("semibold", 75),
            ("medium", 55),
        )
    else:
        preferences = (
            ("semi bold", 125),
            ("semibold", 125),
            ("bold", 115),
            ("medium", 105),
            ("extra bold", 90),
            ("extrabold", 90),
            ("black", 75),
        )

    for label, points in preferences:
        if label in name:
            score += points
            break
    if "thin" in name or "light" in name:
        score -= 60
    if path.suffix.lower() == ".ttf":
        score += 2
    return score


def _best_font(fonts: Iterable[Path], role: str) -> Path:
    candidates = list(fonts)
    if not candidates:
        raise ProyaTextError(f"No candidate fonts available for {role}")
    return min(
        candidates,
        key=lambda path: (-_font_score(path, role), path.as_posix().lower()),
    )


def _font_with_name(fonts: Sequence[Path], *needles: str) -> Path | None:
    for needle in needles:
        normalized = re.sub(r"[^a-z0-9]+", "", needle.lower())
        for path in fonts:
            stem = re.sub(r"[^a-z0-9]+", "", path.stem.lower())
            if normalized in stem:
                return path
    return None


def select_fonts(
    font_paths: Sequence[Path],
    preset: str = PRESET_CLEANSER,
) -> FontSelection:
    """Choose headline, caption, and benefit fonts without fixed filenames."""
    fonts = [Path(path).resolve() for path in font_paths]
    if not fonts:
        raise ProyaTextError("No fonts were supplied for selection")

    if preset == PRESET_VITAMIN_C_SHEET_MASK:
        # The sheet-mask art direction calls for three distinct roles. Prefer
        # the closest bundled faces, while retaining score-based fallbacks for
        # projects whose asset inventory differs.
        headline_path = (
            _font_with_name(fonts, "LilitaOne", "Nunito-ExtraBold")
            or _best_font(fonts, "headline")
        )
        caption_path = (
            _font_with_name(fonts, "Montserrat-SemiBold", "Outfit-Bold")
            or _best_font(fonts, "caption")
        )
        label_path = (
            _font_with_name(fonts, "TikTokSans-Bold", "Poppins-Bold")
            or _best_font(fonts, "headline")
        )
        return FontSelection(
            headline=FontAsset(headline_path, _font_face_name(headline_path)),
            caption=FontAsset(caption_path, _font_face_name(caption_path)),
            benefit=FontAsset(label_path, _font_face_name(label_path)),
            benefit_marker="-",
            label=FontAsset(label_path, _font_face_name(label_path)),
        )

    headline_path = _best_font(fonts, "headline")
    caption_path = _best_font(fonts, "caption")
    check_fonts = [
        path for path in fonts if font_supports_character(path, CHECK_MARK)
    ]
    benefit_path = _best_font(check_fonts, "caption") if check_fonts else caption_path
    marker = CHECK_MARK if font_supports_character(benefit_path, CHECK_MARK) else "-"
    cross_marker = (
        CROSS_MARK if font_supports_character(benefit_path, CROSS_MARK) else "X"
    )

    return FontSelection(
        headline=FontAsset(headline_path, _font_face_name(headline_path)),
        caption=FontAsset(caption_path, _font_face_name(caption_path)),
        benefit=FontAsset(benefit_path, _font_face_name(benefit_path)),
        benefit_marker=marker,
        cross_marker=cross_marker,
        label=FontAsset(benefit_path, _font_face_name(benefit_path)),
    )


def _build_cleanser_overlay_events(benefit_marker: str) -> list[OverlayEvent]:
    """Build the deterministic 15-second PROYA cleanser overlay timeline."""
    fade_safe_marker = benefit_marker if benefit_marker in {CHECK_MARK, "-", "\u2022"} else "-"
    events = [
        OverlayEvent(
            0.00,
            2.40,
            "Headline",
            "KULIT KETARIK\nSETELAH CUCI MUKA?",
            360,
            72,
            8,
        ),
        OverlayEvent(
            0.00,
            2.40,
            "Caption",
            "Habis cuci muka kok malah\nterasa ketarik?",
            360,
            1090,
            2,
        ),
        OverlayEvent(
            2.40,
            5.20,
            "Headline",
            "KENAPA BISA TERJADI?",
            360,
            72,
            8,
        ),
        OverlayEvent(
            2.40,
            5.20,
            "Caption",
            "Bisa jadi cleanser kamu terlalu\nbikin kulit terasa kering.",
            360,
            1090,
            2,
        ),
        OverlayEvent(
            5.20,
            8.20,
            "Headline",
            "KENAPA LEBIH NYAMAN?",
            360,
            72,
            8,
        ),
        OverlayEvent(
            5.35,
            8.20,
            "Benefit",
            "Busa halus",
            330,
            704,
            4,
            marker=fade_safe_marker,
            layer=1,
        ),
        OverlayEvent(
            5.75,
            8.20,
            "Benefit",
            "Membersihkan dengan lembut",
            330,
            764,
            4,
            marker=fade_safe_marker,
            layer=1,
        ),
        OverlayEvent(
            6.20,
            8.20,
            "Benefit",
            "Tidak terasa ketarik",
            330,
            824,
            4,
            marker=fade_safe_marker,
            layer=1,
        ),
        OverlayEvent(
            5.20,
            8.20,
            "Caption",
            "PROYA Facial Cleanser membersihkan dengan lembut,\n"
            "busanya halus, dan kulit tetap terasa nyaman.",
            360,
            1090,
            2,
            horizontal_scale=78,
        ),
        OverlayEvent(
            8.20,
            10.80,
            "Headline",
            "PROYA 5X VITAMIN C\nFACIAL CLEANSER",
            360,
            72,
            8,
        ),
        OverlayEvent(
            8.20,
            10.80,
            "Secondary",
            "5X Vitamin C Derivatives\n+ Amino Acid Surfactant",
            360,
            900,
            5,
        ),
        OverlayEvent(
            8.20,
            10.80,
            "Caption",
            "Dengan 5X Vitamin C Derivatives\ndan Amino Acid Surfactant.",
            360,
            1090,
            2,
        ),
        OverlayEvent(10.80, 13.50, "Headline", "CARA PAKAI", 360, 72, 8),
        OverlayEvent(
            10.80,
            13.50,
            "Caption",
            "Pakai secukupnya, busakan, pijat lembut,\nlalu bilas sampai bersih.",
            360,
            1090,
            2,
            horizontal_scale=88,
        ),
        OverlayEvent(13.50, 15.00, "Headline", "CLEAN & NYAMAN", 360, 72, 8),
        OverlayEvent(
            13.50,
            15.00,
            "Secondary",
            "COCOK UNTUK\nDAILY CLEANSING",
            54,
            800,
            4,
        ),
        OverlayEvent(
            13.50,
            15.00,
            "Caption",
            "Clean tanpa bikin kulit\nterasa kesat.",
            360,
            1090,
            2,
        ),
    ]

    for event in events:
        if event.end <= event.start:
            raise ValueError(f"Invalid overlay event range: {event}")
        if len(event.text.splitlines()) > 2:
            raise ValueError(f"Overlay event exceeds two lines: {event.text!r}")
    return events


def _build_vitamin_c_serum_overlay_events(
    benefit_marker: str,
    cross_marker: str,
) -> list[OverlayEvent]:
    """Build the exact hard-coded PROYA Vitamin C serum overlay timeline."""
    safe_check = benefit_marker if benefit_marker == CHECK_MARK else "-"
    safe_cross = cross_marker if cross_marker == CROSS_MARK else "X"
    green = "&H004BCB72&"
    red = "&H004747FF&"
    events = [
        OverlayEvent(
            0.00,
            2.20,
            "Headline",
            "KULIT KUSAM?",
            360,
            115,
            8,
            fade_in_ms=0,
            fade_out_ms=0,
            pop_in_ms=120,
        ),
        OverlayEvent(
            0.00,
            2.40,
            "Subtitle",
            "Kalau kulitmu kusam,\njangan asal pilih serum.",
            360,
            1010,
            2,
            fade_in_ms=80,
            fade_out_ms=0,
        ),
        OverlayEvent(
            2.40,
            5.10,
            "Callout",
            "SATU JALUR",
            55,
            145,
            7,
            suffix_marker=safe_cross,
            marker_color=red,
            fade_in_ms=0,
            fade_out_ms=0,
            pop_in_ms=120,
        ),
        OverlayEvent(
            2.55,
            5.10,
            "Callout",
            "MULTI-PATHWAY",
            55,
            202,
            7,
            suffix_marker=safe_check,
            marker_color=green,
            fade_in_ms=0,
            fade_out_ms=0,
            pop_in_ms=120,
        ),
        OverlayEvent(
            2.40,
            5.20,
            "Subtitle",
            "Cari brightening multi-pathway.",
            360,
            1010,
            2,
            fade_in_ms=80,
            fade_out_ms=0,
        ),
    ]

    ingredient_lines = (
        "5X VITAMIN C",
        "TRANEXAMIC ACID",
        "ALPHA-ARBUTIN",
        "ERGOTHIONEINE",
    )
    for index, text in enumerate(ingredient_lines):
        events.append(
            OverlayEvent(
                5.25 + index * 0.10,
                8.55,
                "Ingredient",
                text,
                335,
                112 + index * 47,
                7,
                suffix_marker=safe_check,
                marker_color=green,
                fade_in_ms=0,
                fade_out_ms=0,
                pop_in_ms=100,
                layer=1,
            )
        )

    events.extend(
        [
            OverlayEvent(
                5.20,
                8.60,
                "Subtitle",
                "PROYA punya 5X Vitamin C, Tranexamic Acid,\n"
                "Alpha-Arbutin, dan Ergothioneine.",
                360,
                1010,
                2,
                horizontal_scale=84,
                font_size=38,
                fade_in_ms=80,
                fade_out_ms=0,
            ),
            OverlayEvent(
                8.60,
                10.35,
                "Callout",
                "TEKSTUR RINGAN",
                45,
                105,
                7,
                suffix_marker=safe_check,
                marker_color=green,
                fade_in_ms=0,
                fade_out_ms=0,
                pop_in_ms=120,
            ),
            OverlayEvent(
                8.60,
                10.40,
                "Subtitle",
                "Teksturnya ringan dan membantu\nkulit kusam tampak lebih cerah,",
                360,
                1010,
                2,
                font_size=38,
                fade_in_ms=0,
                fade_out_ms=0,
            ),
            OverlayEvent(
                10.40,
                12.15,
                "Callout",
                "NODA TAMPAK TERSAMARKAN",
                45,
                105,
                7,
                suffix_marker=safe_check,
                marker_color=green,
                horizontal_scale=88,
                font_size=36,
                fade_in_ms=0,
                fade_out_ms=0,
                pop_in_ms=120,
            ),
            OverlayEvent(
                10.40,
                12.20,
                "Subtitle",
                "dengan noda terlihat tersamarkan.",
                360,
                1010,
                2,
                fade_in_ms=0,
                fade_out_ms=0,
            ),
            OverlayEvent(
                12.20,
                13.60,
                "Usage",
                "2-3 TETES \u2022 TEPUK LEMBUT",
                360,
                115,
                8,
                fade_in_ms=0,
                fade_out_ms=0,
                pop_in_ms=120,
            ),
            OverlayEvent(
                13.60,
                15.00,
                "CTA",
                "CEK PROYA SEKARANG",
                360,
                115,
                8,
                fade_in_ms=0,
                fade_out_ms=0,
                pop_in_ms=120,
            ),
            OverlayEvent(
                12.20,
                15.00,
                "Subtitle",
                "Pakai dua sampai tiga tetes, tepuk lembut,\n"
                "lalu cek PROYA sekarang.",
                360,
                1010,
                2,
                horizontal_scale=88,
                font_size=38,
                fade_in_ms=80,
                fade_out_ms=0,
            ),
        ]
    )

    for event in events:
        if event.end <= event.start:
            raise ValueError(f"Invalid overlay event range: {event}")
        if len(event.text.splitlines()) > 2:
            raise ValueError(f"Overlay event exceeds two lines: {event.text!r}")
    return events


def _rounded_rectangle_path(width: int, height: int, radius: int) -> str:
    """Return a compact ASS vector path for a rounded rectangle."""
    width = max(2, int(width))
    height = max(2, int(height))
    radius = max(1, min(int(radius), width // 2, height // 2))
    right = width
    bottom = height
    return (
        f"m {radius} 0 l {right - radius} 0 "
        f"b {right} 0 {right} 0 {right} {radius} "
        f"l {right} {bottom - radius} "
        f"b {right} {bottom} {right} {bottom} {right - radius} {bottom} "
        f"l {radius} {bottom} "
        f"b 0 {bottom} 0 {bottom} 0 {bottom - radius} "
        f"l 0 {radius} b 0 0 0 0 {radius} 0"
    )


def _panel_event(
    start: float,
    end: float,
    style: str,
    *,
    x: int,
    y: int,
    width: int,
    height: int,
    radius: int,
    fade_in_ms: int,
    fade_out_ms: int,
    fade_in_delay_ms: int = 0,
    slide_in_px: int = 0,
) -> OverlayEvent:
    return OverlayEvent(
        start,
        end,
        style,
        _rounded_rectangle_path(width, height, radius),
        x,
        y,
        7,
        fade_in_ms=fade_in_ms,
        fade_in_delay_ms=fade_in_delay_ms,
        fade_out_ms=fade_out_ms,
        slide_in_px=slide_in_px,
        drawing=True,
        layer=0,
    )


def _build_vitamin_c_sheet_mask_overlay_events() -> list[OverlayEvent]:
    """Build the exact 15-second PROYA Vitamin C sheet-mask timeline."""
    events: list[OverlayEvent] = [
        OverlayEvent(
            0.00,
            2.40,
            "Headline",
            "KULIT PANAS, KUSAM, KERING?",
            360,
            80,
            8,
            horizontal_scale=76,
            font_size=62,
            fade_in_ms=120,
            fade_out_ms=200,
            pop_in_ms=120,
            pop_start_scale=92,
            move_out_up_px=18,
            layer=2,
        ),
        _panel_event(
            0.00, 1.55, "SubtitlePanel",
            x=55, y=905, width=610, height=112, radius=24,
            fade_in_ms=80, fade_out_ms=80,
        ),
        OverlayEvent(
            0.00,
            1.55,
            "Subtitle",
            "Kulit lagi panas, kusam,\ndan kering?",
            360,
            1005,
            2,
            fade_in_ms=80,
            fade_out_ms=80,
            layer=2,
        ),
        _panel_event(
            1.55, 2.40, "SubtitlePanel",
            x=235, y=950, width=250, height=66, radius=24,
            fade_in_ms=60, fade_out_ms=60,
        ),
        OverlayEvent(
            1.55,
            2.40,
            "Subtitle",
            "Coba ini.",
            360,
            1004,
            2,
            fade_in_ms=60,
            fade_out_ms=60,
            layer=2,
        ),
        _panel_event(
            2.40, 5.20, "LabelPanel",
            x=48, y=91, width=594, height=62, radius=25,
            fade_in_ms=180, fade_out_ms=180, slide_in_px=16,
        ),
        OverlayEvent(
            2.40,
            5.20,
            "Label",
            "PROYA VITAMIN C SHEET MASK",
            65,
            105,
            7,
            font_size=31,
            fade_in_ms=180,
            fade_out_ms=180,
            slide_in_px=16,
            layer=2,
        ),
        OverlayEvent(
            3.25,
            5.20,
            "Supporting",
            "25 ML / LEMBAR",
            75,
            170,
            7,
            font_size=27,
            fade_in_ms=150,
            fade_out_ms=0,
            layer=2,
        ),
        _panel_event(
            2.40, 3.90, "SubtitlePanel",
            x=50, y=950, width=620, height=66, radius=24,
            fade_in_ms=80, fade_out_ms=60,
        ),
        OverlayEvent(
            2.40,
            3.90,
            "Subtitle",
            "PROYA Vitamin C Sheet Mask,",
            360,
            1004,
            2,
            fade_in_ms=80,
            fade_out_ms=60,
            layer=2,
        ),
        _panel_event(
            3.90, 5.20, "SubtitlePanel",
            x=75, y=905, width=570, height=112, radius=24,
            fade_in_ms=60, fade_out_ms=80,
        ),
        OverlayEvent(
            3.90,
            5.20,
            "Subtitle",
            "satu lembar dua puluh lima\nmililiter.",
            360,
            1005,
            2,
            fade_in_ms=60,
            fade_out_ms=80,
            layer=2,
        ),
        _panel_event(
            5.20, 8.60, "LabelPanel",
            x=48, y=91, width=390, height=62, radius=25,
            fade_in_ms=180, fade_out_ms=180, slide_in_px=16,
        ),
        OverlayEvent(
            5.20,
            8.60,
            "Label",
            "PAKAI 10\u201315 MENIT",
            65,
            105,
            7,
            font_size=32,
            fade_in_ms=180,
            fade_out_ms=180,
            slide_in_px=16,
            layer=2,
        ),
        OverlayEvent(
            7.55,
            8.60,
            "Supporting",
            "Lalu bilas",
            75,
            170,
            7,
            font_size=27,
            fade_in_ms=100,
            fade_out_ms=0,
            layer=2,
        ),
        _panel_event(
            5.20, 7.75, "SubtitlePanel",
            x=80, y=905, width=560, height=112, radius=24,
            fade_in_ms=80, fade_out_ms=80,
        ),
        OverlayEvent(
            5.20,
            7.75,
            "Subtitle",
            "Tempel sepuluh sampai\nlima belas menit,",
            360,
            1005,
            2,
            fade_in_ms=80,
            fade_out_ms=80,
            layer=2,
        ),
        _panel_event(
            7.75, 8.60, "SubtitlePanel",
            x=210, y=950, width=300, height=66, radius=24,
            fade_in_ms=60, fade_out_ms=80,
        ),
        OverlayEvent(
            7.75,
            8.60,
            "Subtitle",
            "lalu bilas.",
            360,
            1004,
            2,
            fade_in_ms=60,
            fade_out_ms=80,
            layer=2,
        ),
        _panel_event(
            8.60, 10.55, "LabelPanel",
            x=48, y=91, width=350, height=62, radius=25,
            fade_in_ms=180, fade_out_ms=150, slide_in_px=16,
        ),
        OverlayEvent(
            8.60,
            10.55,
            "Label",
            "HYALURONIC ACID",
            65,
            105,
            7,
            font_size=30,
            fade_in_ms=180,
            fade_out_ms=150,
            slide_in_px=16,
            layer=2,
        ),
        _panel_event(
            10.55, 12.20, "LabelPanel",
            x=48, y=91, width=410, height=62, radius=25,
            fade_in_ms=100, fade_out_ms=150, slide_in_px=16,
        ),
        OverlayEvent(
            10.55,
            12.20,
            "Label",
            "EKSTRAK TUMBUHAN",
            65,
            105,
            7,
            font_size=30,
            fade_in_ms=100,
            fade_out_ms=150,
            slide_in_px=16,
            layer=2,
        ),
        _panel_event(
            8.60, 12.20, "BenefitPanel",
            x=370, y=174, width=320, height=103, radius=22,
            fade_in_ms=180, fade_out_ms=200, fade_in_delay_ms=220,
        ),
        OverlayEvent(
            8.60,
            12.20,
            "BenefitCallout",
            "Membantu melembapkan\n+ menenangkan",
            530,
            187,
            8,
            font_size=28,
            fade_in_ms=180,
            fade_in_delay_ms=220,
            fade_out_ms=200,
            layer=2,
        ),
        _panel_event(
            8.60, 10.55, "SubtitlePanel",
            x=30, y=925, width=480, height=112, radius=24,
            fade_in_ms=80, fade_out_ms=80,
        ),
        OverlayEvent(
            8.60,
            10.55,
            "Subtitle",
            "Hyaluronic acid dan\nekstrak tumbuhan",
            270,
            1025,
            2,
            fade_in_ms=80,
            fade_out_ms=80,
            layer=2,
        ),
        _panel_event(
            10.55, 12.20, "SubtitlePanel",
            x=210, y=925, width=480, height=112, radius=24,
            fade_in_ms=60, fade_out_ms=80,
        ),
        OverlayEvent(
            10.55,
            12.20,
            "Subtitle",
            "membantu melembapkan\ndan menenangkan.",
            450,
            1025,
            2,
            fade_in_ms=60,
            fade_out_ms=80,
            layer=2,
        ),
        OverlayEvent(
            12.20,
            15.00,
            "CTA",
            "SAVE BUAT NANTI",
            360,
            80,
            8,
            font_size=62,
            fade_in_ms=160,
            fade_out_ms=0,
            pop_in_ms=160,
            pop_start_scale=90,
            wiggle_at_ms=800,
            layer=2,
        ),
        OverlayEvent(
            12.42,
            15.00,
            "SupportingCTA",
            "Emergency skincare kamu",
            360,
            160,
            8,
            font_size=28,
            fade_in_ms=160,
            fade_out_ms=0,
            layer=2,
        ),
        _panel_event(
            12.20, 15.00, "SubtitlePanel",
            x=90, y=905, width=540, height=112, radius=24,
            fade_in_ms=80, fade_out_ms=0,
        ),
        OverlayEvent(
            12.20,
            15.00,
            "Subtitle",
            "Simpan buat emergency\nskincare kamu.",
            360,
            1005,
            2,
            fade_in_ms=80,
            fade_out_ms=0,
            layer=2,
        ),
    ]

    for event in events:
        if event.end <= event.start:
            raise ValueError(f"Invalid overlay event range: {event}")
        if not event.drawing and len(event.text.splitlines()) > 2:
            raise ValueError(f"Overlay event exceeds two lines: {event.text!r}")
    return events


def build_overlay_events(
    benefit_marker: str = "-",
    preset: str = PRESET_CLEANSER,
    cross_marker: str = "X",
) -> list[OverlayEvent]:
    """Build one of the deterministic 15-second PROYA overlay timelines."""
    if preset == PRESET_CLEANSER:
        return _build_cleanser_overlay_events(benefit_marker)
    if preset == PRESET_VITAMIN_C_SERUM:
        return _build_vitamin_c_serum_overlay_events(
            benefit_marker,
            cross_marker,
        )
    if preset == PRESET_VITAMIN_C_SHEET_MASK:
        return _build_vitamin_c_sheet_mask_overlay_events()
    raise ProyaTextError(
        f"Unknown overlay preset {preset!r}; choose one of: {', '.join(SUPPORTED_PRESETS)}"
    )


def _ass_style_font_name(name: str) -> str:
    return re.sub(r"[\r\n,]+", " ", name).strip() or "Sans"


def _format_ass_event(event: OverlayEvent) -> str:
    tags = [f"\\an{event.alignment}"]
    duration_ms = max(1, round((event.end - event.start) * 1000))
    if event.slide_in_px:
        slide_end_ms = event.pop_in_ms or event.fade_in_ms or 180
        tags.append(
            f"\\move({event.x - event.slide_in_px},{event.y},"
            f"{event.x},{event.y},0,{slide_end_ms})"
        )
    elif event.move_out_up_px:
        move_start_ms = max(0, duration_ms - max(event.fade_out_ms, 180))
        tags.append(
            f"\\move({event.x},{event.y},{event.x},"
            f"{event.y - event.move_out_up_px},{move_start_ms},{duration_ms})"
        )
    else:
        tags.append(f"\\pos({event.x},{event.y})")
    if event.font_size is not None:
        tags.append(f"\\fs{event.font_size}")
    if event.pop_in_ms is not None:
        target_x_scale = event.horizontal_scale or 100
        start_x_scale = max(
            1,
            round(target_x_scale * event.pop_start_scale / 100.0),
        )
        tags.extend(
            [
                f"\\fscx{start_x_scale}",
                f"\\fscy{event.pop_start_scale}",
                (
                    f"\\t(0,{event.pop_in_ms},"
                    f"\\fscx{target_x_scale}\\fscy100)"
                ),
            ]
        )
    elif event.horizontal_scale is not None:
        tags.append(f"\\fscx{event.horizontal_scale}")
    if event.fade_in_delay_ms:
        fade_out_start_ms = max(
            event.fade_in_delay_ms + event.fade_in_ms,
            duration_ms - event.fade_out_ms,
        )
        tags.append(
            f"\\fade(255,0,255,{event.fade_in_delay_ms},"
            f"{event.fade_in_delay_ms + event.fade_in_ms},"
            f"{fade_out_start_ms},{duration_ms})"
        )
    elif event.fade_in_ms or event.fade_out_ms:
        tags.append(f"\\fad({event.fade_in_ms},{event.fade_out_ms})")
    if event.wiggle_at_ms is not None:
        wiggle = event.wiggle_at_ms
        tags.extend(
            [
                f"\\t({wiggle - 40},{wiggle},\\frz2.5)",
                f"\\t({wiggle},{wiggle + 45},\\frz-2.5)",
                f"\\t({wiggle + 45},{wiggle + 100},\\frz0)",
            ]
        )
    if event.drawing:
        tags.append("\\p1")
        visible = event.text
    else:
        visible = escape_ass_text(event.text)
    if event.marker:
        tags.append(f"\\1c{event.marker_color}")
        visible = (
            escape_ass_text(event.marker)
            + r" {\1c&H00FFFFFF&}"
            + visible
        )
    if event.suffix_marker:
        visible = (
            visible
            + f" {{\\1c{event.marker_color}}}"
            + escape_ass_text(event.suffix_marker)
        )
    return (
        f"Dialogue: {event.layer},{timestamp_to_ass(event.start)},"
        f"{timestamp_to_ass(event.end)},{event.style},,0,0,0,,"
        f"{{{''.join(tags)}}}{visible}"
    )


def generate_ass(
    fonts: FontSelection,
    preset: str = PRESET_CLEANSER,
    final_frame_end: float | None = None,
) -> str:
    """Generate a complete UTF-8 ASS document."""
    headline_name = _ass_style_font_name(fonts.headline.face_name)
    caption_name = _ass_style_font_name(fonts.caption.face_name)
    benefit_name = _ass_style_font_name(fonts.benefit.face_name)
    label_name = _ass_style_font_name(
        (fonts.label or fonts.benefit).face_name
    )
    if preset == PRESET_CLEANSER:
        title = "PROYA Facial Cleanser Indonesian Overlays"
        styles = f"""Style: Headline,{headline_name},58,&H005AD8FF,&H005AD8FF,&H00110C08,&H70000000,-1,0,0,0,100,100,0.4,0,1,4.2,1.4,8,40,40,72,1
Style: Caption,{caption_name},41,&H00FFFFFF,&H00FFFFFF,&H00110C08,&H70000000,-1,0,0,0,90,100,0,0,1,3.5,0.9,2,34,34,190,1
Style: Secondary,{caption_name},35,&H00B2E0FF,&H00B2E0FF,&H00110C08,&H70000000,-1,0,0,0,100,100,0.2,0,1,3.1,0.8,5,38,38,0,1
Style: Benefit,{benefit_name},36,&H00EFFFF0,&H00EFFFF0,&H00110C08,&H70000000,-1,0,0,0,100,100,0,0,1,3.2,0.8,4,48,36,0,1"""
    elif preset == PRESET_VITAMIN_C_SERUM:
        title = "PROYA Vitamin C Serum Indonesian Overlays"
        styles = f"""Style: Headline,{headline_name},62,&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,-1,0,0,0,100,100,0.3,0,1,5.0,1.6,8,38,38,72,1
Style: Subtitle,{caption_name},40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,-1,0,0,0,90,100,0,0,1,3.6,0.8,2,34,34,270,1
Style: Callout,{benefit_name},38,&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,-1,0,0,0,100,100,0,0,1,3.2,0.8,7,42,34,0,1
Style: Ingredient,{benefit_name},32,&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,-1,0,0,0,92,100,0,0,1,3.0,0.7,7,40,30,0,1
Style: Usage,{headline_name},44,&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,-1,0,0,0,100,100,0.2,0,1,4.2,1.2,8,38,38,72,1
Style: CTA,{headline_name},48,&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,-1,0,0,0,100,100,0.2,0,1,4.8,1.4,8,38,38,72,1"""
    elif preset == PRESET_VITAMIN_C_SHEET_MASK:
        title = "PROYA Vitamin C Sheet Mask Indonesian Overlays"
        styles = f"""Style: Headline,{headline_name},62,&H00A65FEA,&H00A65FEA,&H00FFFFFF,&H005F3DA4,-1,0,0,0,100,100,0.2,0,1,3.6,2.0,8,24,24,70,1
Style: CTA,{headline_name},62,&H00A65FEA,&H00A65FEA,&H00FFFFFF,&H005F3DA4,-1,0,0,0,100,100,0.2,0,1,3.6,2.0,8,24,24,70,1
Style: Subtitle,{caption_name},36,&H00FFFFFF,&H00FFFFFF,&H00352B31,&H78000000,-1,-1,0,0,100,100,0,0,1,2.2,0.8,2,42,42,250,1
Style: Label,{label_name},31,&H00312A33,&H00312A33,&H00FFFFFF,&H50000000,-1,0,0,0,100,100,0.2,0,1,0.8,0.7,7,0,0,0,1
Style: Supporting,{caption_name},27,&H00FFFFFF,&H00FFFFFF,&H00403038,&H70000000,-1,0,0,0,100,100,0,0,1,1.4,0.9,7,0,0,0,1
Style: BenefitCallout,{caption_name},28,&H00FFFFFF,&H00FFFFFF,&H00403038,&H70000000,-1,0,0,0,100,100,0,0,1,1.2,0.7,8,0,0,0,1
Style: SupportingCTA,{caption_name},28,&H00FFF3DF,&H00FFF3DF,&H00403038,&H70000000,-1,0,0,0,100,100,0,0,1,1.4,0.9,8,0,0,0,1
Style: SubtitlePanel,{label_name},10,&H4D9582C9,&H4D9582C9,&H30FFFFFF,&H00000000,0,0,0,0,100,100,0,0,1,1.2,0.7,7,0,0,0,1
Style: LabelPanel,{label_name},10,&H35DCF0FF,&H35DCF0FF,&H20FFFFFF,&H00000000,0,0,0,0,100,100,0,0,1,1.2,0.8,7,0,0,0,1
Style: BenefitPanel,{label_name},10,&H4D9582C9,&H4D9582C9,&H30FFFFFF,&H00000000,0,0,0,0,100,100,0,0,1,1.2,0.7,7,0,0,0,1"""
    else:
        raise ProyaTextError(
            f"Unknown overlay preset {preset!r}; choose one of: "
            f"{', '.join(SUPPORTED_PRESETS)}"
        )

    header = f"""[Script Info]
Title: {title}
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{styles}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = build_overlay_events(
        fonts.benefit_marker,
        preset=preset,
        cross_marker=fonts.cross_marker,
    )
    if final_frame_end is not None and final_frame_end > EXPECTED_DURATION_SECONDS:
        # ASS end timestamps are exclusive. Extend only terminal events so a
        # source frame whose PTS is exactly 15.000 still carries the overlay.
        events = [
            replace(event, end=final_frame_end)
            if event.end == EXPECTED_DURATION_SECONDS
            else event
            for event in events
        ]
    return header + "\n".join(_format_ass_event(event) for event in events) + "\n"


def _resolve_executable(name_or_path: str, label: str) -> str:
    candidate = Path(name_or_path).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())
    resolved = shutil.which(name_or_path)
    if resolved:
        return str(Path(resolved).resolve())
    raise ProyaTextError(
        f"{label} is not installed or not on PATH (searched for {name_or_path!r})"
    )


def probe_media(ffprobe_bin: str, input_path: Path) -> MediaInfo:
    command = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(input_path),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProyaTextError(f"FFprobe could not read {input_path}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown FFprobe error").strip()
        raise ProyaTextError(f"FFprobe could not read {input_path}: {detail[-1200:]}")

    try:
        payload = json.loads(result.stdout or "{}")
        streams = payload.get("streams") or []
        video = next(
            stream for stream in streams if stream.get("codec_type") == "video"
        )
        duration = float(
            (payload.get("format") or {}).get("duration")
            or video.get("duration")
            or 0.0
        )
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
    except (KeyError, TypeError, ValueError, StopIteration) as exc:
        raise ProyaTextError(
            f"Input does not contain a readable video stream: {input_path}"
        ) from exc

    if duration <= 0 or not math.isfinite(duration) or width <= 0 or height <= 0:
        raise ProyaTextError(f"Input video metadata is invalid: {input_path}")
    has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
    return MediaInfo(duration, width, height, has_audio)


def build_ffmpeg_command(
    ffmpeg_bin: str,
    input_path: Path,
    output_path: Path,
    crf: int = 19,
) -> list[str]:
    """Build a shell-free FFmpeg argument array using safe relative filter paths."""
    video_filter = (
        "scale=720:1280:force_original_aspect_ratio=decrease:flags=lanczos,"
        "pad=720:1280:(ow-iw)/2:(oh-ih)/2:color=black,"
        "setsar=1,"
        "subtitles=filename=overlay.ass:fontsdir=fonts"
    )
    return [
        ffmpeg_bin,
        "-hide_banner",
        "-y",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        video_filter,
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-fps_mode",
        "passthrough",
        "-c:a",
        "copy",
        "-map_metadata",
        "0",
        "-t",
        f"{EXPECTED_DURATION_SECONDS:.3f}",
        "-movflags",
        "+faststart",
        str(output_path),
    ]


def format_command(command: Sequence[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(list(command))
    return shlex.join(command)


def _stage_fonts(fonts: FontSelection, staging_dir: Path) -> None:
    staging_dir.mkdir(parents=True, exist_ok=True)
    unique_paths = {
        fonts.headline.path.resolve(),
        fonts.caption.path.resolve(),
        fonts.benefit.path.resolve(),
        (fonts.label or fonts.benefit).path.resolve(),
    }
    for index, source in enumerate(
        sorted(unique_paths, key=lambda path: path.as_posix().lower())
    ):
        shutil.copy2(source, staging_dir / f"{index:02d}_{source.name}")


def _validate_output(
    output_path: Path,
    ffprobe_bin: str,
    source_info: MediaInfo,
) -> MediaInfo:
    if not output_path.is_file() or output_path.stat().st_size < 1024:
        raise ProyaTextError(f"FFmpeg did not create a non-empty output: {output_path}")
    output_info = probe_media(ffprobe_bin, output_path)
    if (output_info.width, output_info.height) != (TARGET_WIDTH, TARGET_HEIGHT):
        raise ProyaTextError(
            "Output dimensions are invalid: "
            f"{output_info.width}x{output_info.height}, expected 720x1280"
        )
    if source_info.has_audio and not output_info.has_audio:
        raise ProyaTextError("Output is missing the source audio stream")
    if abs(output_info.duration - source_info.duration) > 0.20:
        raise ProyaTextError(
            "Output duration changed unexpectedly: "
            f"{source_info.duration:.3f}s -> {output_info.duration:.3f}s"
        )
    if abs(output_info.duration - EXPECTED_DURATION_SECONDS) > 0.02:
        raise ProyaTextError(
            "Output duration is invalid: "
            f"{output_info.duration:.3f}s, expected exactly 15.00s"
        )
    return output_info


def process_video(
    input_path: Path,
    output_path: Path,
    assets_dir: Path,
    *,
    ffmpeg_name: str = "ffmpeg",
    ffprobe_name: str = "ffprobe",
    crf: int = 19,
    dry_run: bool = False,
    duration_tolerance: float = DEFAULT_DURATION_TOLERANCE_SECONDS,
    preset: str = PRESET_CLEANSER,
) -> Path:
    source = Path(input_path).expanduser().resolve()
    destination = Path(output_path).expanduser().resolve()
    assets = Path(assets_dir).expanduser().resolve()

    if not source.is_file():
        raise ProyaTextError(f"Input MP4 does not exist: {source}")
    if source.suffix.lower() != ".mp4":
        raise ProyaTextError(f"Input must be an MP4 file: {source}")
    if source.stat().st_size <= 0:
        raise ProyaTextError(f"Input MP4 is empty: {source}")
    if source == destination:
        raise ProyaTextError("Input and output paths must be different")
    if not 0 <= crf <= 51:
        raise ProyaTextError("CRF must be between 0 and 51")
    if preset not in SUPPORTED_PRESETS:
        raise ProyaTextError(
            f"Unknown overlay preset {preset!r}; choose one of: "
            f"{', '.join(SUPPORTED_PRESETS)}"
        )

    print("[1/5] Checking FFmpeg, FFprobe, and input video...")
    ffmpeg_bin = _resolve_executable(ffmpeg_name, "FFmpeg")
    ffprobe_bin = _resolve_executable(ffprobe_name, "FFprobe")
    source_info = probe_media(ffprobe_bin, source)
    if abs(source_info.duration - EXPECTED_DURATION_SECONDS) > duration_tolerance:
        raise ProyaTextError(
            f"Input duration is {source_info.duration:.3f}s; expected approximately "
            f"15s (\u00b1{duration_tolerance:.1f}s)"
        )
    print(
        f"      Input: {source_info.width}x{source_info.height}, "
        f"{source_info.duration:.3f}s, "
        f"audio={'yes' if source_info.has_audio else 'no'}"
    )
    print(f"      Preset: {preset}")

    print(f"[2/5] Scanning fonts below {assets}...")
    font_paths = discover_fonts(assets)
    fonts = select_fonts(font_paths, preset=preset)
    print(f"      Headline: {fonts.headline.face_name} ({fonts.headline.path.name})")
    print(f"      Caption:  {fonts.caption.face_name} ({fonts.caption.path.name})")
    marker_label = "check mark" if fonts.benefit_marker == CHECK_MARK else "hyphen"
    print(
        f"      Benefit:  {fonts.benefit.face_name} "
        f"({fonts.benefit.path.name}, marker={marker_label})"
    )
    if preset == PRESET_VITAMIN_C_SHEET_MASK:
        label_font = fonts.label or fonts.benefit
        print(f"      Labels:   {label_font.face_name} ({label_font.path.name})")

    with tempfile.TemporaryDirectory(prefix="proya-text-overlay-") as temp_name:
        temp_dir = Path(temp_name).resolve()
        ass_path = temp_dir / "overlay.ass"
        fonts_dir = temp_dir / "fonts"

        print("[3/5] Generating ASS overlay timeline...")
        _stage_fonts(fonts, fonts_dir)
        ass_path.write_text(
            generate_ass(
                fonts,
                preset=preset,
                final_frame_end=source_info.duration,
            ),
            encoding="utf-8-sig",
        )
        command = build_ffmpeg_command(ffmpeg_bin, source, destination, crf)

        if dry_run:
            print("[4/5] Dry run; FFmpeg was not executed.")
            print(f"      Working directory: {temp_dir}")
            print(f"      Command: {format_command(command)}")
            print("[5/5] Dry run complete; no output was written.")
            return destination

        destination.parent.mkdir(parents=True, exist_ok=True)
        print(f"[4/5] Rendering captioned video to {destination}...")
        try:
            result = subprocess.run(
                command,
                cwd=temp_dir,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=600,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProyaTextError("FFmpeg timed out after 600 seconds") from exc
        except OSError as exc:
            raise ProyaTextError(f"Could not start FFmpeg: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown FFmpeg error").strip()
            raise ProyaTextError(f"FFmpeg render failed:\n{detail[-4000:]}")

        print("[5/5] Validating dimensions, audio, and duration...")
        output_info = _validate_output(destination, ffprobe_bin, source_info)
        print(
            f"Done: {destination} "
            f"({output_info.width}x{output_info.height}, "
            f"{output_info.duration:.3f}s)"
        )
        return destination


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Burn a hard-coded Indonesian PROYA text-overlay preset into a "
            "15-second MP4."
        )
    )
    parser.add_argument("--input", required=True, type=Path, help="Source MP4 path")
    parser.add_argument("--output", required=True, type=Path, help="Output MP4 path")
    parser.add_argument(
        "--assets-dir",
        type=Path,
        default=Path("assets"),
        help="Assets directory to scan recursively for .ttf/.otf fonts",
    )
    parser.add_argument(
        "--preset",
        choices=SUPPORTED_PRESETS,
        default=PRESET_CLEANSER,
        help="Hard-coded overlay timeline to render (default: cleanser)",
    )
    parser.add_argument(
        "--crf",
        type=int,
        default=19,
        help="libx264 CRF quality (default: 19)",
    )
    parser.add_argument(
        "--duration-tolerance",
        type=float,
        default=DEFAULT_DURATION_TOLERANCE_SECONDS,
        help="Allowed distance from 15 seconds for the input (default: 1.0)",
    )
    parser.add_argument(
        "--ffmpeg-bin",
        default="ffmpeg",
        help="FFmpeg executable name or path",
    )
    parser.add_argument(
        "--ffprobe-bin",
        default="ffprobe",
        help="FFprobe executable name or path",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and print the resolved FFmpeg command without rendering",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        process_video(
            args.input,
            args.output,
            args.assets_dir,
            ffmpeg_name=args.ffmpeg_bin,
            ffprobe_name=args.ffprobe_bin,
            crf=args.crf,
            dry_run=args.dry_run,
            duration_tolerance=args.duration_tolerance,
            preset=args.preset,
        )
    except ProyaTextError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("ERROR: Cancelled by user", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
