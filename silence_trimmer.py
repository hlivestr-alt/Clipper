from __future__ import annotations

import copy
import logging
import math
import os
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger("proya.silence_trimmer")

SKIP_INSUFFICIENT_WORDS = "insufficient_words"
SKIP_NO_GAPS_FOUND = "no_gaps_found"
SKIP_REMOVAL_FRACTION_EXCEEDED = "removal_fraction_exceeded"
SKIP_WORD_TIMING_INVALID = "word_timing_invalid"


def build_silence_trim_plan(clip_words: list, clip_duration: float, cfg) -> dict[str, Any]:
    """Build a word-timestamp based plan for compacting dead air in one clip."""
    duration = _positive_float(clip_duration)
    if duration <= 0.0:
        return _fallback_plan(duration, SKIP_WORD_TIMING_INVALID)

    if not bool(getattr(cfg, "SILENCE_TRIM_ENABLED", True)):
        return _fallback_plan(duration, None)

    min_words = max(0, int(getattr(cfg, "SILENCE_TRIM_MIN_WORDS", 6) or 0))
    min_gap = max(0.0, float(getattr(cfg, "SILENCE_TRIM_MIN_GAP", 1.2) or 0.0))
    keep_gap = max(0.0, float(getattr(cfg, "SILENCE_TRIM_KEEP_GAP", 0.35) or 0.0))
    edge_keep = max(0.0, float(getattr(cfg, "SILENCE_TRIM_EDGE_KEEP", 0.25) or 0.0))
    max_fraction = max(0.0, min(1.0, float(getattr(cfg, "SILENCE_TRIM_MAX_REMOVAL_FRACTION", 0.45) or 0.0)))

    raw_words = [word for word in clip_words or [] if _word_text(word)]
    if len(raw_words) < min_words:
        return _fallback_plan(duration, SKIP_INSUFFICIENT_WORDS)

    timed_words = _coerce_timed_words(raw_words, duration)
    if timed_words is None:
        return _fallback_plan(duration, SKIP_WORD_TIMING_INVALID)

    first_start = timed_words[0]["start"]
    last_end = timed_words[-1]["end"]
    internal_gaps = []
    for left, right in zip(timed_words, timed_words[1:]):
        gap = right["start"] - left["end"]
        if gap > min_gap:
            internal_gaps.append((left["end"], right["start"], gap))

    has_leading_gap = first_start > min_gap
    has_trailing_gap = (duration - last_end) > min_gap
    if not has_leading_gap and not has_trailing_gap and not internal_gaps:
        return _fallback_plan(duration, SKIP_NO_GAPS_FOUND)

    kept_ranges = []
    removed_ranges = []
    current_start = 0.0

    if has_leading_gap:
        current_start = max(0.0, first_start - edge_keep)
        if current_start > 0.01:
            removed_ranges.append(_range_payload(0.0, current_start))

    for gap_start, gap_end, gap in internal_gaps:
        kept = min(keep_gap, gap)
        keep_before = kept / 2.0
        keep_after = kept - keep_before
        range_end = min(duration, gap_start + keep_before)
        next_start = max(0.0, gap_end - keep_after)

        if range_end > current_start + 0.01:
            kept_ranges.append(_range_payload(current_start, range_end))
        if next_start > range_end + 0.01:
            removed_ranges.append(_range_payload(range_end, next_start))
        current_start = next_start

    final_end = duration
    if has_trailing_gap:
        final_end = min(duration, last_end + edge_keep)

    if final_end > current_start + 0.01:
        kept_ranges.append(_range_payload(current_start, final_end))
    if final_end < duration - 0.01:
        removed_ranges.append(_range_payload(final_end, duration))

    kept_ranges = _attach_output_timeline(_merge_ranges(kept_ranges), duration)
    rendered_duration = kept_ranges[-1]["output_end"] if kept_ranges else 0.0
    removed_seconds = max(0.0, duration - rendered_duration)
    if removed_seconds <= 0.01 or not kept_ranges:
        return _fallback_plan(duration, SKIP_NO_GAPS_FOUND)

    if duration > 0 and (removed_seconds / duration) > max_fraction:
        return _fallback_plan(duration, SKIP_REMOVAL_FRACTION_EXCEEDED)

    return {
        "enabled": True,
        "trimmed": True,
        "skip_reason": None,
        "original_duration": round(duration, 6),
        "rendered_duration": round(rendered_duration, 6),
        "removed_seconds": round(removed_seconds, 6),
        "kept_ranges": kept_ranges,
        "silence_ranges": [_round_range(item) for item in _merge_ranges(removed_ranges)],
    }


def remap_words_to_compacted_timeline(words: list, plan: dict[str, Any]) -> list[dict[str, Any]]:
    if not plan or not plan.get("trimmed"):
        return copy.deepcopy(words or [])

    remapped = []
    for word in words or []:
        if not isinstance(word, dict):
            continue
        try:
            start = float(word.get("start"))
            end = float(word.get("end"))
        except (TypeError, ValueError):
            continue
        overlaps = _kept_overlaps(start, end, plan)
        if not overlaps:
            continue
        mapped = dict(word)
        mapped_start = _map_time_inside_kept(overlaps[0][0], plan)
        mapped_end = _map_time_inside_kept(overlaps[-1][1], plan)
        if mapped_start is None or mapped_end is None:
            continue
        mapped["start"] = round(max(0.0, mapped_start), 6)
        mapped["end"] = round(max(mapped["start"] + 0.01, mapped_end), 6)
        remapped.append(mapped)
    return remapped


def remap_events_to_compacted_timeline(events: list, plan: dict[str, Any]) -> list[dict[str, Any]]:
    if not plan or not plan.get("trimmed"):
        return copy.deepcopy(events or [])

    remapped_events = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        mapped = dict(event)
        relative_track = _remap_relative_track(event.get("relative_track") or [], plan)

        rel_start = _safe_float(event.get("relative_start", event.get("start_time")))
        rel_end = _safe_float(event.get("relative_end", event.get("end_time", rel_start)))
        interval_mapped = False
        if rel_start is not None and rel_end is not None:
            start = min(rel_start, rel_end)
            end = max(rel_start, rel_end)
            overlaps = _kept_overlaps(start, end, plan)
            if overlaps:
                mapped_start = _map_time_inside_kept(overlaps[0][0], plan)
                mapped_end = _map_time_inside_kept(overlaps[-1][1], plan)
                if mapped_start is not None and mapped_end is not None:
                    mapped["relative_start"] = round(mapped_start, 6)
                    mapped["relative_end"] = round(max(mapped_start, mapped_end), 6)
                    mapped["duration"] = round(max(0.0, mapped["relative_end"] - mapped["relative_start"]), 6)
                    interval_mapped = True

        if relative_track:
            mapped["relative_track"] = relative_track
            if not interval_mapped:
                mapped["relative_start"] = relative_track[0]["relative_time"]
                mapped["relative_end"] = relative_track[-1]["relative_time"]
                mapped["duration"] = round(max(0.0, mapped["relative_end"] - mapped["relative_start"]), 6)
                interval_mapped = True
        else:
            mapped["relative_track"] = []

        if interval_mapped:
            remapped_events.append(mapped)
    return remapped_events


def build_silence_compacted_ffmpeg_command(
    input_video: str,
    clip_start: float,
    output_path: str,
    kept_ranges: list[dict[str, Any]],
    variant,
    cfg,
) -> list[str]:
    ranges = _normalize_kept_ranges(kept_ranges)
    if not ranges:
        raise ValueError("silence compaction requires at least one kept range")

    raw_codec = getattr(cfg, "RAW_CUT_CODEC", "h264_nvenc")
    raw_preset = getattr(cfg, "RAW_CUT_PRESET", "p1")
    initial_seek = max(0.0, float(clip_start) + ranges[0]["source_start"] - 0.05)
    cmd = ["ffmpeg", "-y", "-ss", f"{initial_seek:.3f}", "-i", input_video]

    fc = []
    for index, item in enumerate(ranges):
        local_start = max(0.0, float(clip_start) + item["source_start"] - initial_seek)
        local_end = max(local_start + 0.01, float(clip_start) + item["source_end"] - initial_seek)
        fc.append(
            f"[0:v]trim=start={local_start:.6f}:end={local_end:.6f},"
            f"setpts=PTS-STARTPTS[v{index}]"
        )
        fc.append(
            f"[0:a]atrim=start={local_start:.6f}:end={local_end:.6f},"
            f"asetpts=PTS-STARTPTS[a{index}]"
        )

    if len(ranges) == 1:
        vid = "[v0]"
        aud = "[a0]"
    else:
        concat_inputs = "".join(f"[v{index}][a{index}]" for index in range(len(ranges)))
        fc.append(f"{concat_inputs}concat=n={len(ranges)}:v=1:a=1[vcat][acat]")
        vid = "[vcat]"
        aud = "[acat]"

    if variant is not None:
        vf = _variant_vf_chain(input_video, variant)
        if vf:
            fc.append(f"{vid}{vf}[vout]")
            vid = "[vout]"
        speed = float(getattr(variant, "speed_ramp", 1.0) or 1.0)
        if abs(speed - 1.0) > 0.02:
            speed = max(0.75, min(1.25, speed))
            fc.append(f"{aud}atempo={speed:.4f}[aout]")
            aud = "[aout]"

    cmd += ["-filter_complex", ";".join(fc), "-map", vid, "-map", aud]
    cmd += ["-c:v", raw_codec, "-preset", raw_preset, "-c:a", "aac"]
    if raw_codec == "libx264":
        cmd += ["-crf", "28"]
    elif str(raw_codec).endswith("_nvenc"):
        cmd += ["-cq", str(getattr(cfg, "OUTPUT_CQ", 35))]
    cmd += ["-avoid_negative_ts", "make_zero", output_path]
    return cmd


def cut_raw_clip_with_silence_plan(
    input_video: str,
    clip_start: float,
    output_path: str,
    kept_ranges: list[dict[str, Any]],
    variant,
    cfg,
) -> bool:
    os.makedirs(Path(output_path).parent, exist_ok=True)
    cmd = build_silence_compacted_ffmpeg_command(
        input_video=input_video,
        clip_start=clip_start,
        output_path=output_path,
        kept_ranges=kept_ranges,
        variant=variant,
        cfg=cfg,
    )
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            log.error("FFmpeg silence trim error: %s", (result.stderr or "")[-400:])
            return False
        output = Path(output_path)
        if output.exists() and output.stat().st_size < 1024:
            log.error("FFmpeg produced empty/tiny silence-trimmed file: %s", output_path)
            output.unlink(missing_ok=True)
            return False
        return output.exists()
    except subprocess.TimeoutExpired:
        log.error("FFmpeg silence trim timed out: %s", output_path)
        return False
    except FileNotFoundError:
        raise RuntimeError("FFmpeg not found")


def _fallback_plan(duration: float, reason: str | None) -> dict[str, Any]:
    safe_duration = max(0.0, float(duration or 0.0))
    return {
        "enabled": reason is not None,
        "trimmed": False,
        "skip_reason": reason,
        "original_duration": round(safe_duration, 6),
        "rendered_duration": round(safe_duration, 6),
        "removed_seconds": 0.0,
        "kept_ranges": [_range_payload(0.0, safe_duration, output_start=0.0)] if safe_duration > 0 else [],
        "silence_ranges": [],
    }


def _coerce_timed_words(words: list[dict[str, Any]], duration: float) -> list[dict[str, float]] | None:
    timed = []
    prev_start = -math.inf
    for word in words:
        if not isinstance(word, dict):
            return None
        start = _safe_float(word.get("start"))
        end = _safe_float(word.get("end"))
        if start is None or end is None:
            return None
        if not math.isfinite(start) or not math.isfinite(end):
            return None
        if end < start or start < prev_start - 1e-6:
            return None
        prev_start = start
        start = max(0.0, min(duration, start))
        end = max(start, min(duration, end))
        timed.append({"start": start, "end": end})
    return timed


def _attach_output_timeline(ranges: list[dict[str, float]], duration: float) -> list[dict[str, float]]:
    output_t = 0.0
    result = []
    for item in ranges:
        source_start = max(0.0, min(duration, float(item["source_start"])))
        source_end = max(source_start, min(duration, float(item["source_end"])))
        if source_end <= source_start + 0.01:
            continue
        output_start = output_t
        output_end = output_start + (source_end - source_start)
        result.append(
            {
                "source_start": round(source_start, 6),
                "source_end": round(source_end, 6),
                "output_start": round(output_start, 6),
                "output_end": round(output_end, 6),
            }
        )
        output_t = output_end
    return result


def _merge_ranges(ranges: list[dict[str, float]]) -> list[dict[str, float]]:
    ordered = sorted(ranges or [], key=lambda item: float(item["source_start"]))
    merged = []
    for item in ordered:
        start = float(item["source_start"])
        end = float(item["source_end"])
        if end <= start + 0.01:
            continue
        if merged and start <= merged[-1]["source_end"] + 0.001:
            merged[-1]["source_end"] = max(merged[-1]["source_end"], end)
        else:
            merged.append({"source_start": start, "source_end": end})
    return merged


def _range_payload(start: float, end: float, output_start: float | None = None) -> dict[str, float]:
    payload = {"source_start": float(start), "source_end": float(end)}
    if output_start is not None:
        payload["output_start"] = float(output_start)
        payload["output_end"] = float(output_start) + max(0.0, float(end) - float(start))
    return payload


def _round_range(item: dict[str, Any]) -> dict[str, float]:
    return {
        "source_start": round(float(item["source_start"]), 6),
        "source_end": round(float(item["source_end"]), 6),
    }


def _kept_overlaps(start: float, end: float, plan: dict[str, Any]) -> list[tuple[float, float]]:
    overlaps = []
    for item in plan.get("kept_ranges") or []:
        source_start = float(item["source_start"])
        source_end = float(item["source_end"])
        overlap_start = max(float(start), source_start)
        overlap_end = min(float(end), source_end)
        if overlap_end >= overlap_start:
            overlaps.append((overlap_start, overlap_end))
    return overlaps


def _map_time_inside_kept(source_time: float, plan: dict[str, Any]) -> float | None:
    t = float(source_time)
    for item in plan.get("kept_ranges") or []:
        source_start = float(item["source_start"])
        source_end = float(item["source_end"])
        if source_start - 1e-6 <= t <= source_end + 1e-6:
            clamped = max(source_start, min(source_end, t))
            return float(item["output_start"]) + (clamped - source_start)
    return None


def _remap_relative_track(track: list, plan: dict[str, Any]) -> list[dict[str, Any]]:
    remapped = []
    for sample in track or []:
        if not isinstance(sample, dict):
            continue
        sample_time = _safe_float(sample.get("relative_time", sample.get("time")))
        if sample_time is None:
            continue
        mapped_time = _map_time_inside_kept(sample_time, plan)
        if mapped_time is None:
            continue
        remapped.append({**sample, "relative_time": round(mapped_time, 6)})
    remapped.sort(key=lambda sample: float(sample.get("relative_time", 0.0)))
    return remapped


def _normalize_kept_ranges(ranges: list[dict[str, Any]]) -> list[dict[str, float]]:
    normalized = []
    for item in ranges or []:
        start = _safe_float(item.get("source_start"))
        end = _safe_float(item.get("source_end"))
        if start is None or end is None or end <= start:
            continue
        normalized.append({"source_start": start, "source_end": end})
    return normalized


def _variant_vf_chain(input_video: str, variant) -> str:
    try:
        from variation_engine import _probe_video_dimensions, build_ffmpeg_vf_chain

        frame_w, frame_h = _probe_video_dimensions(input_video)
        return build_ffmpeg_vf_chain(variant, frame_w=frame_w, frame_h=frame_h)
    except Exception as exc:
        log.warning("Could not build variant filters for silence-trimmed cut: %s", exc)
        return ""


def _word_text(word: Any) -> str:
    if not isinstance(word, dict):
        return ""
    return str(word.get("word", "")).strip()


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive_float(value: Any) -> float:
    number = _safe_float(value)
    return max(0.0, number or 0.0)
