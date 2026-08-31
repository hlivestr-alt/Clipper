from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clipper_app.modular_scanner.repository import TRANSCRIPT_RECORD_SCHEMA_VERSION
from clipper_app.modular_scanner.transcripts import transcript_fingerprint


BRIDGE_VERSION = "modular-transcript-bridge-v1"
MIN_BOUNDARY_OVERLAP_SECONDS = 0.04
MIN_CLAMPED_WORD_SECONDS = 0.02
TIMELINE_TOLERANCE_SECONDS = 0.001
RENDER_DURATION_TOLERANCE_SECONDS = 0.15


class TranscriptBridgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceTranscript:
    transcript_id: str
    transcript_fingerprint: str
    schema_version: int
    origin: str
    cache_path: str
    words: tuple[dict[str, Any], ...]
    segments: tuple[dict[str, Any], ...]
    metadata: dict[str, Any]


def _same_path(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return str(Path(left).resolve(strict=False)).casefold() == str(Path(right).resolve(strict=False)).casefold()


def _valid_word(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("word") or "").strip()
    try:
        start, end = float(raw["start"]), float(raw["end"])
    except (KeyError, TypeError, ValueError):
        return None
    if not text or start < 0 or end <= start:
        return None
    word = {"word": text, "start": start, "end": end}
    if raw.get("probability") is not None:
        try:
            word["probability"] = float(raw["probability"])
        except (TypeError, ValueError):
            pass
    return word


class SourceTranscriptResolver:
    """Read-only resolver for the exact transcript attached to a scanner generation."""

    def __init__(self, library_path: str | Path):
        self.library_path = Path(library_path).resolve(strict=False)
        self._cache: dict[tuple[str, str], SourceTranscript | None] = {}

    def resolve(self, item: dict[str, Any]) -> SourceTranscript | None:
        scan_id, source_id = str(item.get("scan_id") or ""), str(item.get("source_id") or "")
        if not scan_id or not source_id or not self.library_path.is_file():
            return None
        key = (scan_id, source_id)
        if key in self._cache:
            return self._cache[key]
        uri = f"file:{self.library_path.as_posix()}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True, timeout=5) as db:
                db.row_factory = sqlite3.Row
                row = db.execute(
                    """SELECT t.*,m.canonical_path,m.file_size,m.mtime_ns,m.content_fingerprint
                       FROM scans s
                       JOIN transcripts t ON t.transcript_id=s.transcript_id
                       JOIN media_sources m ON m.source_id=s.source_id
                       WHERE s.scan_id=? AND s.source_id=? AND t.source_id=?
                         AND s.status='completed' AND t.status='completed'""",
                    (scan_id, source_id, source_id),
                ).fetchone()
        except sqlite3.Error:
            row = None
        if row is None or int(row["schema_version"]) != TRANSCRIPT_RECORD_SCHEMA_VERSION:
            self._cache[key] = None
            return None
        expected = {
            "content_fingerprint": str(item.get("source_content_fingerprint", item.get("content_fingerprint", ""))),
            "canonical_path": str(item.get("canonical_path") or ""),
            "file_size": int(item.get("source_file_size", item.get("file_size", -1))),
            "mtime_ns": int(item.get("source_mtime_ns", item.get("mtime_ns", -1))),
        }
        if (
            str(row["content_fingerprint"]) != expected["content_fingerprint"]
            or not _same_path(str(row["canonical_path"]), expected["canonical_path"])
            or int(row["file_size"]) != expected["file_size"]
            or int(row["mtime_ns"]) != expected["mtime_ns"]
        ):
            self._cache[key] = None
            return None
        cache_path = Path(str(row["cache_path"]))
        if not cache_path.is_absolute():
            cache_path = (Path.cwd() / cache_path).resolve(strict=False)
        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
            if transcript_fingerprint(raw) != str(row["transcript_fingerprint"]):
                raise ValueError("transcript fingerprint mismatch")
        except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError):
            self._cache[key] = None
            return None
        metadata = dict(raw.get("metadata") or {})
        declared_path = str(metadata.get("source_video_path") or "")
        if declared_path and not _same_path(declared_path, expected["canonical_path"]):
            self._cache[key] = None
            return None
        raw_words = raw.get("words")
        if not isinstance(raw_words, list):
            raw_words = [word for segment in raw.get("segments", []) if isinstance(segment, dict)
                         for word in (segment.get("words") or [])]
        words = tuple(word for word in (_valid_word(value) for value in raw_words) if word is not None)
        segments = tuple(segment for segment in raw.get("segments", []) if isinstance(segment, dict))
        resolved = SourceTranscript(
            transcript_id=str(row["transcript_id"]), transcript_fingerprint=str(row["transcript_fingerprint"]),
            schema_version=int(row["schema_version"]), origin=str(row["origin"]),
            cache_path=str(cache_path), words=words, segments=segments, metadata=metadata,
        )
        self._cache[key] = resolved
        return resolved


def _segment_words(segments: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    for segment in segments:
        try:
            start, end = float(segment["start"]), float(segment["end"])
        except (KeyError, TypeError, ValueError):
            continue
        tokens = str(segment.get("text") or "").split()
        if start < 0 or end <= start or not tokens:
            continue
        duration = end - start
        for index, token in enumerate(tokens):
            words.append({
                "word": token, "start": start + duration * index / len(tokens),
                "end": start + duration * (index + 1) / len(tokens),
            })
    return words


def _synthetic_item_words(item: dict[str, Any], base_offset: float) -> list[dict[str, Any]]:
    tokens = str(item.get("transcript_text") or "").split()
    duration = float(item["end_seconds"]) - float(item["start_seconds"])
    return [{
        "word": token, "start": base_offset + duration * index / len(tokens),
        "end": base_offset + duration * (index + 1) / len(tokens), "probability": 1.0,
    } for index, token in enumerate(tokens)] if tokens else []


def crop_and_remap_words(
    source_words: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    item_start: float,
    item_end: float,
    base_offset: float,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    included: list[dict[str, Any]] = []
    clamped = dropped = 0
    for raw in sorted(source_words, key=lambda row: (float(row["start"]), float(row["end"]))):
        word_start, word_end = float(raw["start"]), float(raw["end"])
        overlap_start, overlap_end = max(word_start, item_start), min(word_end, item_end)
        overlap = overlap_end - overlap_start
        if overlap <= 0:
            continue
        boundary_word = word_start < item_start or word_end > item_end
        midpoint_inside = item_start <= (word_start + word_end) / 2.0 < item_end
        if boundary_word and overlap < MIN_BOUNDARY_OVERLAP_SECONDS and not midpoint_inside:
            dropped += 1
            continue
        if overlap < MIN_CLAMPED_WORD_SECONDS:
            dropped += 1
            continue
        if boundary_word:
            clamped += 1
        mapped = {
            "word": str(raw["word"]),
            "start": round(base_offset + overlap_start - item_start, 6),
            "end": round(base_offset + overlap_end - item_start, 6),
        }
        if raw.get("probability") is not None:
            mapped["probability"] = raw["probability"]
        mapped["_source_start"] = round(overlap_start, 6)
        mapped["_source_end"] = round(overlap_end, 6)
        included.append(mapped)
    return included, {"included": len(included), "boundary_clamped": clamped, "dropped_at_cuts": dropped}


def _validate(words: list[dict[str, Any]], intervals: list[tuple[float, float, int]], base_duration: float,
              rendered_duration: float | None) -> None:
    last_start = last_end = -1.0
    for word, (lower, upper, _position) in zip(words, intervals):
        start, end = float(word["start"]), float(word["end"])
        if start < -TIMELINE_TOLERANCE_SECONDS or end <= start:
            raise TranscriptBridgeError("invalid word interval")
        if start + TIMELINE_TOLERANCE_SECONDS < last_start or end + TIMELINE_TOLERANCE_SECONDS < last_end:
            raise TranscriptBridgeError("word timestamps are not monotonic")
        if start < lower - TIMELINE_TOLERANCE_SECONDS or end > upper + TIMELINE_TOLERANCE_SECONDS:
            raise TranscriptBridgeError("word leaked across a modular item boundary")
        if end > base_duration + TIMELINE_TOLERANCE_SECONDS:
            raise TranscriptBridgeError("word exceeds the mapped base duration")
        last_start, last_end = start, end
    if rendered_duration and words and float(words[-1]["end"]) > rendered_duration + RENDER_DURATION_TOLERANCE_SECONDS:
        raise TranscriptBridgeError("word exceeds the rendered base duration")


def _comparison_examples(items: list[dict[str, Any]], mapped_by_item: list[list[dict[str, Any]]],
                         synthetic_by_item: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    candidates: list[tuple[str, int, int]] = []
    nonempty = [index for index, words in enumerate(mapped_by_item) if words]
    if not nonempty:
        return []
    candidates.append(("early", nonempty[0], 0))
    flat = [(item_index, word_index) for item_index, words in enumerate(mapped_by_item) for word_index in range(len(words))]
    middle_item, middle_word = flat[len(flat) // 2]
    candidates.append(("middle", middle_item, middle_word))
    for boundary_index in range(min(2, len(items) - 1)):
        if mapped_by_item[boundary_index]:
            candidates.append((f"before_cut_{boundary_index + 1}", boundary_index, len(mapped_by_item[boundary_index]) - 1))
    examples = []
    for label, item_index, word_index in candidates[:4]:
        word = mapped_by_item[item_index][word_index]
        synthetic = synthetic_by_item[item_index]
        old = synthetic[min(word_index, len(synthetic) - 1)] if synthetic else None
        examples.append({
            "label": label, "word": word["word"], "item_position": int(items[item_index]["position"]),
            "source_timestamp": word.get("_source_start"),
            "old_synthetic_base_timestamp": round(float(old["start"]), 6) if old else None,
            "new_base_timestamp": word["start"],
        })
    return examples


def bridge_composition_words(composition: dict[str, Any], resolver: SourceTranscriptResolver,
                             *, rendered_duration: float | None = None) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    started = time.perf_counter()
    items = sorted(composition.get("items", []), key=lambda row: int(row["position"]))
    base_offset = 0.0
    final_words: list[dict[str, Any]] = []
    intervals: list[tuple[float, float, int]] = []
    mapped_by_item: list[list[dict[str, Any]]] = []
    synthetic_by_item: list[list[dict[str, Any]]] = []
    item_diagnostics: list[dict[str, Any]] = []
    resolve_seconds = crop_seconds = 0.0
    hook = ""
    for item in items:
        item_start, item_end = float(item["start_seconds"]), float(item["end_seconds"])
        duration = item_end - item_start
        if item_start < 0 or duration <= 0:
            raise TranscriptBridgeError("composition contains an invalid source range")
        if not hook and str(item.get("role")) == "hook":
            hook = " ".join(str(item.get("transcript_text") or "").split())
        synthetic = _synthetic_item_words(item, base_offset)
        synthetic_by_item.append(synthetic)
        resolve_started = time.perf_counter()
        source = resolver.resolve(item)
        resolve_seconds += time.perf_counter() - resolve_started
        errors: list[str] = []
        attempts: list[tuple[str, list[dict[str, Any]]]] = []
        if source and source.words:
            attempts.append(("source_word_timestamps", list(source.words)))
        elif source:
            errors.append("source_word_timestamps: unavailable")
        if source and source.segments:
            attempts.append(("source_segment_timestamps", _segment_words(source.segments)))
        elif source:
            errors.append("source_segment_timestamps: unavailable")
        else:
            errors.append("source_transcript_unavailable")
        attempts.append(("synthetic_distribution", []))
        chosen: list[dict[str, Any]] = []
        crop_counts = {"included": 0, "boundary_clamped": 0, "dropped_at_cuts": 0}
        mode = "synthetic_distribution"
        crop_started = time.perf_counter()
        for candidate_mode, source_words in attempts:
            if candidate_mode == "synthetic_distribution":
                chosen = [dict(word) for word in synthetic]
                crop_counts["included"] = len(chosen)
            else:
                chosen, crop_counts = crop_and_remap_words(source_words, item_start, item_end, base_offset)
                if not chosen and str(item.get("transcript_text") or "").strip():
                    errors.append(f"{candidate_mode}: no words in selected range")
                    continue
            try:
                local_intervals = [(base_offset, base_offset + duration, int(item["position"]))] * len(chosen)
                _validate(chosen, local_intervals, base_offset + duration, None)
            except TranscriptBridgeError as exc:
                errors.append(f"{candidate_mode}: {exc}")
                continue
            mode = candidate_mode
            break
        crop_seconds += time.perf_counter() - crop_started
        mapped_by_item.append(chosen)
        final_words.extend(chosen)
        intervals.extend([(base_offset, base_offset + duration, int(item["position"]))] * len(chosen))
        item_diagnostics.append({
            "position": int(item["position"]), "role": str(item.get("role") or ""),
            "source_id": str(item.get("source_id") or ""), "scan_id": str(item.get("scan_id") or ""),
            "source_range": [item_start, item_end], "base_range": [base_offset, base_offset + duration],
            "timing_mode": mode, "source_words_found": len(source.words) if source else 0,
            "included_after_crop": crop_counts["included"], "boundary_clamped": crop_counts["boundary_clamped"],
            "dropped_at_cuts": crop_counts["dropped_at_cuts"], "fallback_reasons": errors,
            "transcript": ({
                "transcript_id": source.transcript_id, "transcript_fingerprint": source.transcript_fingerprint,
                "schema_version": source.schema_version, "origin": source.origin,
                "timestamp_precision": source.metadata.get("timestamp_precision"),
                "word_alignment_backend": source.metadata.get("word_alignment_backend"),
            } if source else None),
        })
        base_offset += duration
    validation_started = time.perf_counter()
    _validate(final_words, intervals, base_offset, rendered_duration)
    validation_seconds = time.perf_counter() - validation_started
    comparison_examples = _comparison_examples(items, mapped_by_item, synthetic_by_item)
    for word in final_words:
        word.pop("_source_start", None); word.pop("_source_end", None)
    modes = {item["timing_mode"] for item in item_diagnostics}
    overall_mode = next(iter(modes)) if len(modes) == 1 else "mixed"
    diagnostics = {
        "bridge_version": BRIDGE_VERSION, "timing_mode": overall_mode,
        "base_duration": round(base_offset, 6), "rendered_duration": rendered_duration,
        "source_words_found": sum(item["source_words_found"] for item in item_diagnostics),
        "included_after_crop": sum(item["included_after_crop"] for item in item_diagnostics),
        "boundary_clamped": sum(item["boundary_clamped"] for item in item_diagnostics),
        "dropped_at_cuts": sum(item["dropped_at_cuts"] for item in item_diagnostics),
        "final_base_word_count": len(final_words),
        "first_timestamp": final_words[0]["start"] if final_words else None,
        "last_timestamp": final_words[-1]["end"] if final_words else None,
        "fallback_items": [item["position"] for item in item_diagnostics if item["timing_mode"] != "source_word_timestamps"],
        "validation_result": "valid", "items": item_diagnostics,
        "comparison_examples": comparison_examples,
        "performance_seconds": {
            "resolve": round(resolve_seconds, 6), "crop_remap": round(crop_seconds, 6),
            "validation": round(validation_seconds, 6), "total": round(time.perf_counter() - started, 6),
        },
    }
    return final_words, hook, diagnostics
