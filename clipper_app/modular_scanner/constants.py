from __future__ import annotations

ANALYZER_VERSION = "modscan-v2"
PROMPT_VERSION = "modscan-prompt-v2"
TRANSCRIPT_SCHEMA_VERSION = 1
MINIMUM_DURATION_SECONDS = 15.0
MINIMUM_REPAIRABLE_DURATION_SECONDS = 10.0
WINDOW_CHARACTER_BUDGET = 24_000
WINDOW_OVERLAP_SECONDS = 45.0
MAXIMUM_WINDOW_SECONDS = 15 * 60.0
EMPTY_FALLBACK_TARGET_SECONDS = 8 * 60.0
EMPTY_FALLBACK_MINIMUM_SECONDS = 6 * 60.0
EMPTY_FALLBACK_MAX_DEPTH = 2
PRODUCT_CONTEXT_SECONDS = 30.0
DEDUPE_OVERLAP_THRESHOLD = 0.8
WAIT_POLL_SECONDS = 2.0
MAX_ANALYSIS_RETRIES = 2

PRODUCTS = ("cleanser", "toner", "serum", "eye_cream", "mask", "skin_cream")
ROLES = ("hook", "benefits", "ingredients", "cta")
VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"})

PRODUCT_ALIASES = {
    "cleanser": ("cleanser", "cleansing", "pembersih", "sabun wajah", "face wash"),
    "toner": ("toner", "face mist", "facemist"),
    "serum": ("serum", "vitamin c serum", "vit c serum"),
    "eye_cream": ("eye cream", "eyecream", "eye krim", "air cream", "air krim", "krim mata", "mata panda"),
    "mask": ("mask", "masker", "sheet mask", "sheetmask", "sheet mesh", "sheet mes"),
    "skin_cream": ("skin cream", "face cream", "krim wajah", "moisturizer", "pelembap", "cream", "krim"),
}
