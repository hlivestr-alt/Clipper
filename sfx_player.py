# =============================================================================
#  sfx_player.py - Sound Effect mixing for clip editing
#
#  Folder structure (configure in config.py):
#    assets/sfx/
#      product_zoom/      <- plays when product name zooms in
#        whoosh1.wav
#        whoosh2.mp3
#        ...
#      highlight_yellow/  <- Attention / Benefits words
#        ding1.wav
#        ...
#      highlight_green/   <- Results / Speed / Proof words
#        success1.wav
#        ...
#      highlight_red/     <- Pain / Problem words
#        impact1.wav
#        ...
#    assets/bgm/          <- optional music beds, looped under voice
#      upbeat1.mp3
#      ...
#
#  All audio files are mixed into the clip's original audio track using
#  MoviePy's CompositeAudioClip. Volume for each category is configurable.
# =============================================================================

import logging
import random
import re
from pathlib import Path

from utils import _format_rupiah_compact

log = logging.getLogger("proya.sfx")

AUDIO_EXTS = {".wav", ".mp3", ".ogg", ".aac", ".flac", ".m4a"}
_SFX_FILE_CACHE = {}


def _normalize_sfx_word(text: str) -> str:
    text = re.sub(r"[^\w\s]", "", str(text), flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    rupiah_match = re.fullmatch(r"(?:rp|idr|rupiah)\s*(\d+)", text, flags=re.IGNORECASE)
    if rupiah_match:
        return _format_rupiah_compact(rupiah_match.group(1))
    price_match = re.fullmatch(r"(\d+)\s*(rb|ribu|ribuan|ribunya)", text, flags=re.IGNORECASE)
    if price_match:
        return f"{price_match.group(1)}rb"
    return text.upper()


# -----------------------------------------------------------------------------
#  SFX LOADER
# -----------------------------------------------------------------------------

def _get_random_sfx(folder: str | Path) -> Path | None:
    """Return a random audio file from a folder, or None if folder empty/missing."""
    folder = Path(folder)
    if not folder.exists():
        return None
    try:
        stamp = (str(folder.resolve()).casefold(), folder.stat().st_mtime_ns)
    except OSError:
        stamp = (str(folder).casefold(), None)
    files = _SFX_FILE_CACHE.get(stamp)
    if files is None:
        files = [f for f in folder.iterdir() if f.suffix.lower() in AUDIO_EXTS]
        _SFX_FILE_CACHE.clear()
        _SFX_FILE_CACHE[stamp] = files
    if not files:
        log.debug(f"SFX folder empty: {folder}")
        return None
    return random.choice(files)


def _chunk_words(clip_words: list, words_per_chunk: int = 4) -> list[list]:
    """Match the karaoke subtitle block grouping used by ffmpeg_editor."""
    chunks = []
    i = 0
    while i < len(clip_words):
        chunks.append(clip_words[i:i + words_per_chunk])
        i += words_per_chunk
    return chunks


# -----------------------------------------------------------------------------
#  SFX EVENT BUILDER
# -----------------------------------------------------------------------------

def build_sfx_events(
    clip_words: list,
    highlight_words: list,
    highlight_word_colors: list,
    clip_duration: float,
    product_zoom_start: float | None,
    cfg,
) -> list[dict]:
    """
    Collect all SFX trigger events for a clip.

    Returns a list of dicts:
      { "t": float,          # trigger time in clip seconds
        "sfx_path": Path,    # file to play
        "volume": float }    # 0.0-2.0

    Events:
      1. Product zoom SFX - plays at product_zoom_start
      2. Highlight SFX - plays once every N karaoke subtitle blocks
    """
    events = []

    sfx_enabled = getattr(cfg, "SFX_ENABLED", True)
    if not sfx_enabled:
        return []

    sfx_base = Path(getattr(cfg, "SFX_DIR", "assets/sfx"))

    sfx_product_dir = sfx_base / getattr(cfg, "SFX_PRODUCT_FOLDER", "product_zoom")
    sfx_yellow_dir = sfx_base / getattr(cfg, "SFX_YELLOW_FOLDER", "highlight_yellow")
    sfx_green_dir = sfx_base / getattr(cfg, "SFX_GREEN_FOLDER", "highlight_green")
    sfx_red_dir = sfx_base / getattr(cfg, "SFX_RED_FOLDER", "highlight_red")

    vol_product = getattr(cfg, "SFX_VOLUME_PRODUCT", 0.8)
    vol_yellow = getattr(cfg, "SFX_VOLUME_YELLOW", 0.5)
    vol_green = getattr(cfg, "SFX_VOLUME_GREEN", 0.5)
    vol_red = getattr(cfg, "SFX_VOLUME_RED", 0.6)

    yellow_color = getattr(cfg, "HIGHLIGHT_YELLOW_COLOR", "#FFD600")
    green_color = getattr(cfg, "HIGHLIGHT_GREEN_COLOR", "#00C853")
    red_color = getattr(cfg, "HIGHLIGHT_RED_COLOR", "#FF3B30")

    # Keep the old parameter for compatibility with callers that still pass it.
    _ = clip_words

    # 1. Product zoom SFX
    if product_zoom_start is not None and product_zoom_start < clip_duration:
        sfx_path = _get_random_sfx(sfx_product_dir)
        if sfx_path:
            events.append({"t": product_zoom_start, "sfx_path": sfx_path, "volume": vol_product})
            log.debug(f"SFX: product_zoom at t={product_zoom_start:.2f}s -> {sfx_path.name}")
        else:
            log.debug(f"SFX: no files in {sfx_product_dir}")

    # 2. Highlight SFX
    # Fire at most once every N subtitle blocks so it does not feel too busy.
    words_per_chunk = int(getattr(cfg, "SFX_SUBTITLE_CHUNK_WORDS", 4) or 4)
    block_interval = max(1, int(getattr(cfg, "SFX_HIGHLIGHT_BLOCK_INTERVAL", 2) or 2))
    chunk_count = 0
    last_trigger_chunk = -block_interval
    for chunk in _chunk_words(highlight_words, words_per_chunk=words_per_chunk):
        if not chunk:
            continue

        chunk_count += 1
        chunk_start = float(chunk[0].get("start", 0.0))
        if chunk_start >= clip_duration:
            continue

        chosen_color = None
        chosen_word = ""
        for wd in chunk:
            word_idx = int(wd.get("_highlight_idx", -1))
            if word_idx < 0 or word_idx >= len(highlight_word_colors):
                continue
            color = highlight_word_colors[word_idx]
            if not color:
                continue
            chosen_color = color
            chosen_word = _normalize_sfx_word(wd.get("word", ""))
            break

        if not chosen_color or chunk_count - last_trigger_chunk < block_interval:
            continue

        if chosen_color == yellow_color:
            sfx_path = _get_random_sfx(sfx_yellow_dir)
            if sfx_path:
                events.append({"t": chunk_start, "sfx_path": sfx_path, "volume": vol_yellow})
                log.debug(f"SFX: chunk yellow at t={chunk_start:.2f}s ({chosen_word}) -> {sfx_path.name}")
                last_trigger_chunk = chunk_count

        elif chosen_color == green_color:
            sfx_path = _get_random_sfx(sfx_green_dir)
            if sfx_path:
                events.append({"t": chunk_start, "sfx_path": sfx_path, "volume": vol_green})
                log.debug(f"SFX: chunk green at t={chunk_start:.2f}s ({chosen_word}) -> {sfx_path.name}")
                last_trigger_chunk = chunk_count

        elif chosen_color == red_color:
            sfx_path = _get_random_sfx(sfx_red_dir)
            if sfx_path:
                events.append({"t": chunk_start, "sfx_path": sfx_path, "volume": vol_red})
                log.debug(f"SFX: chunk red at t={chunk_start:.2f}s ({chosen_word}) -> {sfx_path.name}")
                last_trigger_chunk = chunk_count

    log.debug(
        f"SFX events total: {len(events)} across {chunk_count} subtitle chunks "
        f"(block interval={block_interval})"
    )
    return events


# -----------------------------------------------------------------------------
#  FOLDER SCAFFOLD HELPER
# -----------------------------------------------------------------------------

def create_sfx_folders(cfg):
    """
    Create all SFX folders if they don't exist.
    Prints a summary so the user knows where to drop files.
    """
    sfx_base = Path(getattr(cfg, "SFX_DIR", "assets/sfx"))
    folders = {
        "product_zoom": getattr(cfg, "SFX_PRODUCT_FOLDER", "product_zoom"),
        "highlight_yellow": getattr(cfg, "SFX_YELLOW_FOLDER", "highlight_yellow"),
        "highlight_green": getattr(cfg, "SFX_GREEN_FOLDER", "highlight_green"),
        "highlight_red": getattr(cfg, "SFX_RED_FOLDER", "highlight_red"),
    }
    print(f"\n{'='*55}")
    print("SFX FOLDER SETUP")
    print(f"Base: {sfx_base.resolve()}")
    print(f"{'='*55}")
    for label, sub in folders.items():
        path = sfx_base / sub
        path.mkdir(parents=True, exist_ok=True)
        files = [f for f in path.iterdir() if f.suffix.lower() in AUDIO_EXTS] if path.exists() else []
        status = f"OK {len(files)} file(s)" if files else "WARN empty - add .wav/.mp3 files here"
        print(f"  [{label:20}]  {path}  ->  {status}")
    bgm_path = Path(getattr(cfg, "BGM_DIR", "assets/bgm"))
    bgm_path.mkdir(parents=True, exist_ok=True)
    bgm_files = [f for f in bgm_path.iterdir() if f.suffix.lower() in AUDIO_EXTS] if bgm_path.exists() else []
    bgm_status = f"OK {len(bgm_files)} file(s)" if bgm_files else "WARN empty - add music beds here"
    print(f"  [{'bgm':20}]  {bgm_path}  ->  {bgm_status}")
    print(f"{'='*55}\n")
