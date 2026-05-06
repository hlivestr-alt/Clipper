# =============================================================================
#  word_corrector.py — Transcript word correction & brand name normalization
#
#  Fixes Whisper mishearing brand/product names in the transcript.
#  Corrections are defined in config.py under WORD_CORRECTIONS.
#
#  Two strategies run in order:
#    1. Phrase-level: scan the full text for multi-word wrong phrases first
#       (e.g. "pro ya" → "PROYA") so single-word pass doesn't break them.
#    2. Word-level: replace individual wrong tokens.
#
#  Applied to:
#    - transcript segments (text field) — affects LLM input quality
#    - transcript words (word field) — affects subtitle display
# =============================================================================

import logging
import re

log = logging.getLogger("proya.corrector")
_PATTERN_CACHE = {}


def build_correction_patterns(corrections: dict) -> list[tuple]:
    """
    Pre-compile regex patterns from the corrections dict.
    Returns list of (pattern, replacement) sorted longest-match-first
    so "pro ya" is tried before "pro".
    """
    patterns = []
    sorted_keys = sorted(corrections.keys(), key=len, reverse=True)

    for wrong in sorted_keys:
        right = corrections[wrong]
        # Word-boundary aware, case-insensitive
        escaped = re.escape(wrong.strip())
        # Use \b only on word-char boundaries to avoid breaking Indonesian
        pattern = re.compile(r'(?<![a-zA-Z])' + escaped + r'(?![a-zA-Z])', re.IGNORECASE)
        patterns.append((pattern, right))

    log.debug(f"Built {len(patterns)} correction patterns")
    return patterns


def get_correction_patterns(corrections: dict) -> list[tuple]:
    key = tuple(sorted((str(k), str(v)) for k, v in (corrections or {}).items()))
    cached = _PATTERN_CACHE.get(key)
    if cached is not None:
        return cached
    patterns = build_correction_patterns(corrections)
    _PATTERN_CACHE.clear()
    _PATTERN_CACHE[key] = patterns
    return patterns


def correct_text(text: str, patterns: list[tuple]) -> str:
    """Apply all correction patterns to a string. Returns corrected string."""
    for pattern, replacement in patterns:
        text = pattern.sub(replacement, text)
    return text


def correct_word(word: str, patterns: list[tuple]) -> str:
    """
    Correct a single word token. Tries exact match first (faster),
    then falls back to pattern matching.
    """
    stripped = word.strip()
    for pattern, replacement in patterns:
        corrected = pattern.sub(replacement, stripped)
        if corrected != stripped:
            # Preserve any leading/trailing whitespace from original
            return word.replace(stripped, corrected)
    return word


def apply_corrections_to_transcript(transcript: dict, cfg) -> dict:
    """
    Apply word corrections to the entire transcript in-place.
    Modifies both segment text and individual word tokens.
    Returns the modified transcript (also mutates in place).
    """
    corrections = getattr(cfg, "WORD_CORRECTIONS", {})
    if not corrections:
        log.info("No word corrections configured — skipping")
        return transcript

    patterns = get_correction_patterns(corrections)
    
    segments_fixed = 0
    words_fixed = 0

    # ── Fix segment-level text (used for LLM input) ───────────────────────────
    for seg in transcript.get("segments", []):
        original = seg.get("text", "")
        corrected = correct_text(original, patterns)
        if corrected != original:
            seg["text"] = corrected
            segments_fixed += 1

        # Fix words inside the segment too
        for w in seg.get("words", []):
            orig_word = w.get("word", "")
            fixed_word = correct_word(orig_word, patterns)
            if fixed_word != orig_word:
                w["word"] = fixed_word
                words_fixed += 1

    # ── Fix top-level word list (used for subtitles) ──────────────────────────
    for w in transcript.get("words", []):
        orig_word = w.get("word", "")
        fixed_word = correct_word(orig_word, patterns)
        if fixed_word != orig_word:
            w["word"] = fixed_word
            # Already counted above if it was in a segment, but top-level list
            # may have duplicates so don't double count — just fix silently

    log.info(
        f"Word corrections applied: {segments_fixed} segments fixed, "
        f"{words_fixed} word tokens corrected"
    )

    # ── Log a sample of what was changed (helpful for debugging) ─────────────
    if segments_fixed > 0:
        sample = [
            seg["text"] for seg in transcript["segments"]
            if "PROYA" in seg.get("text", "") or any(
                v.upper() in seg.get("text", "").upper()
                for v in corrections.values()
                if v != v.lower()
            )
        ][:3]
        if sample:
            log.debug("Sample corrected segments:")
            for s in sample:
                log.debug(f"  → {s[:100]}")

    return transcript


def preview_corrections(transcript: dict, cfg, max_examples: int = 20) -> list[dict]:
    """
    Dry-run: show what would be corrected without modifying the transcript.
    Returns list of {original, corrected, time} dicts.
    Useful for tuning your WORD_CORRECTIONS dictionary.
    """
    corrections = getattr(cfg, "WORD_CORRECTIONS", {})
    if not corrections:
        return []

    patterns = get_correction_patterns(corrections)
    examples = []

    for seg in transcript.get("segments", []):
        original = seg.get("text", "")
        corrected = correct_text(original, patterns)
        if corrected != original and len(examples) < max_examples:
            examples.append({
                "time": seg.get("start", 0),
                "original": original,
                "corrected": corrected,
            })

    return examples


def apply_corrections_to_subtitle_words(words: list, cfg) -> list:
    """
    Apply corrections to a list of subtitle word dicts (for clip_editor.py).
    Returns a new list with corrected 'word' fields.
    Only runs if WORD_CORRECTION_APPLY_TO_SUBTITLES is True in config.
    """
    if not getattr(cfg, "WORD_CORRECTION_APPLY_TO_SUBTITLES", True):
        return words

    corrections = getattr(cfg, "WORD_CORRECTIONS", {})
    if not corrections:
        return words

    patterns = get_correction_patterns(corrections)
    result = []
    for w in words:
        fixed = dict(w)
        fixed["word"] = correct_word(w.get("word", ""), patterns)
        result.append(fixed)
    return result
