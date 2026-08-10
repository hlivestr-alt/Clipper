from __future__ import annotations

import base64
import json
import logging
import math
import re
import statistics
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


log = logging.getLogger("clipper.trends.analyzer")

ANALYZER_VERSION = "editing_fingerprint_v1"
_SEMANTIC_FIELDS = {
    "hook_type": {"none", "text", "before_after_image", "text_before_after_image", "b_roll", "text_b_roll"},
    "subtitle_position": {"top", "center", "bottom"},
    "subtitle_size": {"small", "medium", "large"},
    "zoom_intensity": {"none", "subtle", "normal", "strong"},
    "color_grade": {"original", "warm", "cool", "high_contrast", "muted"},
}


def analyze_trend_video(
    video_id: str,
    video_path: str | Path,
    file_sha256: str,
    cfg: Any,
    *,
    output_root: str | Path | None = None,
) -> dict[str, Any]:
    """Build one reproducible editing fingerprint from a permission-cleared file."""

    path = Path(video_path).resolve()
    root = Path(output_root or getattr(cfg, "TREND_ANALYSIS_DIR", "working/trends/analysis"))
    work_dir = root / video_id / file_sha256[:16]
    work_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    media = _probe_media(path)
    duration = float(media.get("duration_seconds") or 0.0)
    cuts = _detect_cuts(path)
    visual, sampled_frames = _visual_metrics(path, duration)
    transcript = _transcript_metrics(path, work_dir, cfg, duration, warnings)
    semantic = _semantic_analysis(sampled_frames, cfg, warnings)
    recommendations = _recommendations(visual, semantic)

    fingerprint = {
        "schema_version": 1,
        "analyzer_version": ANALYZER_VERSION,
        "video_id": video_id,
        "file_sha256": file_sha256,
        "media": media,
        "cuts": {
            "timestamps": cuts,
            "count": len(cuts),
            "opening_three_second_count": sum(1 for item in cuts if item <= 3.0),
            "cuts_per_second": round(len(cuts) / duration, 4) if duration > 0 else 0.0,
            "median_shot_seconds": _median_shot_length(cuts, duration),
        },
        "visual": visual,
        "transcript": transcript,
        "semantic": semantic,
        "recommendations": recommendations,
        "warnings": warnings,
    }
    artifact = work_dir / "editing_fingerprint_v1.json"
    _write_json_atomic(artifact, fingerprint)
    fingerprint["artifact_path"] = str(artifact)
    return fingerprint


def _probe_media(path: Path) -> dict[str, Any]:
    command = [
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration,size:stream=index,codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
        "-of", "json", str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0:
        raise ValueError(f"ffprobe could not read trend media: {(result.stderr or '').strip()[-300:]}")
    payload = json.loads(result.stdout or "{}")
    streams = payload.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("trend media does not contain a video stream")
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    duration = _safe_float((payload.get("format") or {}).get("duration"))
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    return {
        "duration_seconds": round(duration, 3),
        "width": width,
        "height": height,
        "aspect_ratio": round(width / height, 4) if height else 0.0,
        "fps": round(_fraction(video.get("r_frame_rate")), 3),
        "video_codec": str(video.get("codec_name") or ""),
        "has_audio": audio is not None,
        "audio_codec": str((audio or {}).get("codec_name") or ""),
        "sample_rate": int((audio or {}).get("sample_rate") or 0),
        "channels": int((audio or {}).get("channels") or 0),
        "file_size": int((payload.get("format") or {}).get("size") or path.stat().st_size),
    }


def _detect_cuts(path: Path) -> list[float]:
    command = [
        "ffmpeg", "-hide_banner", "-nostdin", "-i", str(path),
        "-filter:v", "select='gt(scene,0.30)',showinfo", "-an", "-f", "null", "-",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
    if result.returncode not in {0, 255}:
        log.warning("Scene detection failed for %s: %s", path.name, (result.stderr or "")[-300:])
        return []
    values = [round(float(value), 3) for value in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", result.stderr or "")]
    return sorted(set(value for value in values if value > 0.02))


def _visual_metrics(path: Path, duration: float) -> tuple[dict[str, Any], list[Any]]:
    try:
        import cv2
        import numpy as np
    except ImportError:
        return ({
            "motion_intensity": None,
            "letterbox_top_frac": 0.0,
            "letterbox_bottom_frac": 0.0,
            "sample_count": 0,
            "available": False,
        }, [])

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return ({
            "motion_intensity": None,
            "letterbox_top_frac": 0.0,
            "letterbox_bottom_frac": 0.0,
            "sample_count": 0,
            "available": False,
        }, [])
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or max(1, int(duration * fps)))
    target_count = min(90, max(12, int(math.ceil(max(duration, 1.0) * 2.0))))
    indices = sorted(set(int(value) for value in np.linspace(0, max(0, total - 1), target_count)))
    frames: list[Any] = []
    motion: list[float] = []
    top_bars: list[float] = []
    bottom_bars: list[float] = []
    previous = None
    for index in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        frames.append(frame)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        scaled = cv2.resize(gray, (180, 320), interpolation=cv2.INTER_AREA)
        if previous is not None:
            motion.append(float(cv2.absdiff(previous, scaled).mean() / 255.0))
        previous = scaled
        row_means = gray.mean(axis=1)
        top_bars.append(_edge_black_fraction(row_means, forward=True))
        bottom_bars.append(_edge_black_fraction(row_means, forward=False))
    cap.release()
    return ({
        "motion_intensity": round(statistics.median(motion), 4) if motion else 0.0,
        "letterbox_top_frac": round(statistics.median(top_bars), 4) if top_bars else 0.0,
        "letterbox_bottom_frac": round(statistics.median(bottom_bars), 4) if bottom_bars else 0.0,
        "sample_count": len(frames),
        "available": bool(frames),
    }, frames)


def _edge_black_fraction(row_means: Any, *, forward: bool) -> float:
    values = row_means if forward else row_means[::-1]
    count = 0
    limit = max(1, int(len(values) * 0.4))
    for value in values[:limit]:
        if float(value) >= 20.0:
            break
        count += 1
    return count / max(1, len(values))


def _transcript_metrics(path: Path, work_dir: Path, cfg: Any, duration: float, warnings: list[str]) -> dict[str, Any]:
    try:
        from transcriber import transcribe

        payload = transcribe(str(path), str(work_dir), cfg)
        words = [item for item in payload.get("words", []) if item.get("word")]
    except Exception as exc:
        warnings.append(f"Transcript unavailable: {exc}")
        return {"available": False, "word_count": 0, "speech_onset_seconds": None, "words_per_minute": 0.0, "silence_ratio": None, "median_caption_words": None}
    intervals = sorted(
        (float(item.get("start") or 0.0), float(item.get("end") or item.get("start") or 0.0))
        for item in words
    )
    spoken = _merged_duration(intervals)
    phrases: list[int] = []
    count = 0
    previous_end = None
    for start, end in intervals:
        if previous_end is not None and start - previous_end > 0.65 and count:
            phrases.append(count)
            count = 0
        count += 1
        previous_end = end
    if count:
        phrases.append(count)
    return {
        "available": True,
        "word_count": len(words),
        "speech_onset_seconds": round(intervals[0][0], 3) if intervals else None,
        "words_per_minute": round(len(words) / duration * 60.0, 2) if duration > 0 else 0.0,
        "silence_ratio": round(max(0.0, 1.0 - spoken / duration), 4) if duration > 0 else None,
        "median_caption_words": round(statistics.median(phrases), 2) if phrases else None,
        "text": " ".join(str(item.get("word") or "").strip() for item in words).strip(),
    }


def _semantic_analysis(frames: list[Any], cfg: Any, warnings: list[str]) -> dict[str, Any]:
    unavailable = {"available": False, "fields": {}, "raw": ""}
    if not bool(getattr(cfg, "TREND_QWEN_ENABLED", False)) or not frames:
        return unavailable
    try:
        endpoint = str(getattr(cfg, "SCORER_VISION_BASE_URL", "http://localhost:1234/v1")).rstrip("/")
        parsed = urlparse(endpoint)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Legacy trend semantic analysis accepts only an HTTP loopback local-model endpoint.")
        import cv2
        from openai import OpenAI

        max_frames = max(2, int(getattr(cfg, "TREND_QWEN_CONTACT_SHEET_MAX_FRAMES", 8) or 8))
        selected = _even_frames(frames, max_frames)
        sheet = _contact_sheet(selected, cv2)
        ok, encoded = cv2.imencode(".jpg", sheet, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        if not ok:
            raise ValueError("contact sheet JPEG encoding failed")
        image_url = "data:image/jpeg;base64," + base64.b64encode(encoded.tobytes()).decode("ascii")
        client = OpenAI(
            base_url=endpoint,
            api_key=str(getattr(cfg, "SCORER_VISION_API_KEY", "lm-studio")),
            timeout=float(getattr(cfg, "SCORER_VISION_TIMEOUT", 120) or 120),
        )
        prompt = (
            "Analyze this chronological contact sheet for editing style only. Return strict JSON with a fields object. "
            "Each field is {value, confidence, evidence}. Allowed fields and values: "
            "hook_type none|text|before_after_image|text_before_after_image|b_roll|text_b_roll; "
            "subtitle_position top|center|bottom; subtitle_size small|medium|large; "
            "zoom_intensity none|subtle|normal|strong; color_grade original|warm|cool|high_contrast|muted; "
            "subtitle_enabled true|false. Confidence is 0 to 1 and evidence is a short frame-based phrase."
        )
        response = client.chat.completions.create(
            model=str(getattr(cfg, "SCORER_VISION_MODEL", "qwen2.5-vl-32b-instruct")),
            messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_url}},
            ]}],
            temperature=0,
        )
        raw = str(response.choices[0].message.content or "")
        payload = _parse_json_object(raw)
        return {"available": True, "fields": _normalize_semantic_fields(payload.get("fields", {})), "raw": raw[:4000]}
    except Exception as exc:
        warnings.append(f"Qwen-VL unavailable; deterministic fingerprint retained: {exc}")
        return unavailable


def _normalize_semantic_fields(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for key, allowed in _SEMANTIC_FIELDS.items():
        item = raw.get(key)
        if not isinstance(item, dict) or str(item.get("value")) not in allowed:
            continue
        normalized[key] = {
            "value": str(item["value"]),
            "confidence": round(max(0.0, min(1.0, _safe_float(item.get("confidence")))), 4),
            "evidence": str(item.get("evidence") or "")[:500],
        }
    subtitle = raw.get("subtitle_enabled")
    if isinstance(subtitle, dict) and isinstance(subtitle.get("value"), bool):
        normalized["subtitle_enabled"] = {
            "value": bool(subtitle["value"]),
            "confidence": round(max(0.0, min(1.0, _safe_float(subtitle.get("confidence")))), 4),
            "evidence": str(subtitle.get("evidence") or "")[:500],
        }
    return normalized


def _recommendations(visual: dict[str, Any], semantic: dict[str, Any]) -> dict[str, dict[str, Any]]:
    recommendations = dict(semantic.get("fields") or {})
    top = float(visual.get("letterbox_top_frac") or 0.0)
    bottom = float(visual.get("letterbox_bottom_frac") or 0.0)
    enabled = top + bottom >= 0.04
    recommendations["letterbox_enabled"] = {"value": enabled, "confidence": 0.9, "evidence": "Measured persistent dark edge bands."}
    recommendations["letterbox_top_frac"] = {"value": round(top, 4), "confidence": 0.85, "evidence": "Median sampled top band."}
    recommendations["letterbox_bottom_frac"] = {"value": round(bottom, 4), "confidence": 0.85, "evidence": "Median sampled bottom band."}
    position = recommendations.get("subtitle_position", {}).get("value")
    if position in {"top", "center", "bottom"}:
        recommendations["subtitle_y_frac"] = {
            "value": {"top": 0.2, "center": 0.5, "bottom": 0.82}[position],
            "confidence": recommendations["subtitle_position"]["confidence"],
            "evidence": recommendations["subtitle_position"].get("evidence", ""),
        }
    return recommendations


def _contact_sheet(frames: list[Any], cv2: Any) -> Any:
    resized = [cv2.resize(frame, (320, 568), interpolation=cv2.INTER_AREA) for frame in frames]
    columns = 4
    rows = []
    for offset in range(0, len(resized), columns):
        row = list(resized[offset:offset + columns])
        while len(row) < columns:
            row.append(row[-1].copy())
        rows.append(cv2.hconcat(row))
    return cv2.vconcat(rows)


def _even_frames(frames: list[Any], count: int) -> list[Any]:
    if len(frames) <= count:
        return frames
    return [frames[round(index * (len(frames) - 1) / (count - 1))] for index in range(count)]


def _parse_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("vision response did not contain JSON")
    payload = json.loads(text[start:end + 1])
    if not isinstance(payload, dict):
        raise ValueError("vision response JSON was not an object")
    return payload


def _median_shot_length(cuts: list[float], duration: float) -> float | None:
    if duration <= 0:
        return None
    points = [0.0, *[item for item in cuts if item < duration], duration]
    lengths = [points[index + 1] - points[index] for index in range(len(points) - 1)]
    return round(statistics.median(lengths), 3) if lengths else round(duration, 3)


def _merged_duration(intervals: list[tuple[float, float]]) -> float:
    total = 0.0
    current: list[float] | None = None
    for start, end in intervals:
        if current is None:
            current = [start, end]
        elif start <= current[1] + 0.05:
            current[1] = max(current[1], end)
        else:
            total += max(0.0, current[1] - current[0])
            current = [start, end]
    if current is not None:
        total += max(0.0, current[1] - current[0])
    return total


def _fraction(value: Any) -> float:
    text = str(value or "0")
    if "/" in text:
        numerator, denominator = text.split("/", 1)
        return _safe_float(numerator) / max(1e-9, _safe_float(denominator))
    return _safe_float(text)


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _write_json_atomic(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
