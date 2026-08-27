from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from .constants import MAXIMUM_WINDOW_SECONDS, TRANSCRIPT_SCHEMA_VERSION, WINDOW_CHARACTER_BUDGET, WINDOW_OVERLAP_SECONDS


def canonical_transcript(transcript: dict[str, Any]) -> dict[str, Any]:
    raw_segments = transcript.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ValueError("Transcript has no timed segments")
    segments = []
    last_start = -1.0
    for raw in raw_segments:
        if not isinstance(raw, dict):
            raise ValueError("Invalid transcript segment")
        start = float(raw["start"])
        end = float(raw["end"])
        text = str(raw.get("text") or "").strip()
        if start < 0 or end <= start or start < last_start or not text:
            raise ValueError("Invalid transcript segment timing or text")
        segments.append({"start": start, "end": end, "text": text})
        last_start = start
    return {"segments": segments, "metadata": dict(transcript.get("metadata") or {})}


def transcript_fingerprint(transcript: dict[str, Any]) -> str:
    canonical = canonical_transcript(transcript)
    payload = json.dumps(canonical["segments"], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_transcript(path: str | Path) -> dict[str, Any]:
    return canonical_transcript(json.loads(Path(path).read_text(encoding="utf-8")))


def write_transcript_atomic(path: str | Path, transcript: dict[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(f"{target.suffix}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, target)
    return target


def find_production_transcript(source: dict[str, Any], cfg: Any) -> Path | None:
    working = Path(getattr(cfg, "WORKING_DIR", "working"))
    if not working.is_dir():
        return None
    try:
        from transcriber import transcript_cache_is_compatible
        from stage_cache import stage_fingerprint_matches
    except ImportError:
        return None
    candidates = sorted(
        (path for path in working.glob(f"{Path(source['filename']).stem}*/transcript.json") if "modular_scanner" not in path.parts),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    expected_path = str(Path(source["canonical_path"]).resolve()).casefold()
    for candidate in candidates:
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
            if not transcript_cache_is_compatible(raw, cfg):
                continue
            declared = str((raw.get("metadata") or {}).get("source_video_path") or "")
            if not declared or str(Path(declared).resolve()).casefold() != expected_path:
                continue
            if not stage_fingerprint_matches(candidate, source["canonical_path"], cfg, "transcribe"):
                continue
            canonical_transcript(raw)
            return candidate
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return None


def copy_production_transcript(source: dict[str, Any], cfg: Any, target: Path) -> dict[str, Any] | None:
    production = find_production_transcript(source, cfg)
    if production is None:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(production, target)
    return load_transcript(target)


def transcribe_fresh(source: dict[str, Any], cfg: Any, target_dir: Path) -> dict[str, Any]:
    from transcriber import transcribe

    target_dir.mkdir(parents=True, exist_ok=True)
    raw = transcribe(source["canonical_path"], str(target_dir), cfg)
    transcript = canonical_transcript(raw)
    write_transcript_atomic(target_dir / "transcript.json", raw)
    return transcript


def build_windows(
    transcript: dict[str, Any],
    *,
    character_budget: int = WINDOW_CHARACTER_BUDGET,
    overlap_seconds: float = WINDOW_OVERLAP_SECONDS,
    maximum_seconds: float = MAXIMUM_WINDOW_SECONDS,
) -> list[dict[str, Any]]:
    segments = canonical_transcript(transcript)["segments"]
    if character_budget < 200:
        raise ValueError("character_budget is too small")
    if maximum_seconds <= 0:
        raise ValueError("maximum_seconds must be positive")
    raw_windows: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(segments):
        end_index = cursor
        used = 0
        while end_index < len(segments):
            line = _segment_line(segments[end_index])
            if end_index > cursor and used + len(line) > character_budget:
                break
            if end_index > cursor and segments[end_index]["end"] - segments[cursor]["start"] > maximum_seconds:
                break
            used += len(line)
            end_index += 1
        chosen = segments[cursor:end_index]
        raw_windows.append({
            "index": len(raw_windows),
            "start": chosen[0]["start"],
            "end": chosen[-1]["end"],
            "text": "\n".join(_segment_line(segment) for segment in chosen),
            "segments": chosen,
        })
        if end_index >= len(segments):
            break
        next_cursor = end_index
        overlap_floor = chosen[-1]["end"] - overlap_seconds
        while next_cursor > cursor + 1 and segments[next_cursor - 1]["start"] >= overlap_floor:
            next_cursor -= 1
        cursor = max(cursor + 1, next_cursor)
    for index, window in enumerate(raw_windows):
        if index == 0:
            ownership_start = window["start"]
        else:
            ownership_start = (raw_windows[index - 1]["end"] + window["start"]) / 2.0
        if index == len(raw_windows) - 1:
            ownership_end = window["end"]
        else:
            ownership_end = (window["end"] + raw_windows[index + 1]["start"]) / 2.0
        window["ownership_start"] = ownership_start
        window["ownership_end"] = ownership_end
    return raw_windows


def subdivide_window(window: dict[str, Any], *, maximum_seconds: float) -> list[dict[str, Any]]:
    """Split an analysis window on its authoritative boundaries, preserving absolute time."""
    return build_windows(
        {"segments": window["segments"]},
        character_budget=WINDOW_CHARACTER_BUDGET,
        overlap_seconds=min(WINDOW_OVERLAP_SECONDS, maximum_seconds / 10.0),
        maximum_seconds=maximum_seconds,
    )


def _segment_line(segment: dict[str, Any]) -> str:
    return f"[{segment['start']:.3f}-{segment['end']:.3f}] {segment['text']}"
