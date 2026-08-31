from __future__ import annotations

import json
import math
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, MutableMapping

from clipper_app.modular_scanner.media import _fingerprint

from .constants import (
    OUTPUT_AUDIO_CODEC,
    OUTPUT_FPS,
    OUTPUT_PIXEL_FORMAT,
    OUTPUT_SAMPLE_RATE,
    OUTPUT_VIDEO_CODEC,
)


FAST_SEEK_STRATEGY = "bounded_input_seek_local_trim"
FALLBACK_SEEK_STRATEGY = "output_accurate_seek_fallback"
SEGMENT_DURATION_TOLERANCE = 0.15
FAST_SEEK_PREROLL_SECONDS = 5.0


class RenderMediaError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def verify_manifest_source(
    item: dict[str, Any],
    allowed_root: str | Path,
    cache: MutableMapping[tuple[str, int, int, str], Path],
    *,
    fingerprint: Callable[[Path], str] = _fingerprint,
) -> Path:
    root = Path(allowed_root).resolve(strict=True)
    raw = Path(str(item.get("canonical_path") or ""))
    candidate = raw.resolve(strict=False)
    if not _inside(candidate, root):
        raise RenderMediaError("source_outside_allowed_root", f"{item.get('source_filename')}: source is outside the allowed VOD root")
    if not candidate.exists() or not candidate.is_file():
        raise RenderMediaError("source_missing", f"{item.get('source_filename')}: source file is missing")
    expected_size = int(item.get("source_file_size", item.get("file_size", -1)))
    expected_mtime = int(item.get("source_mtime_ns", item.get("mtime_ns", -1)))
    expected_fingerprint = str(item.get("source_content_fingerprint", item.get("content_fingerprint", "")))
    key = (str(candidate).casefold(), expected_size, expected_mtime, expected_fingerprint)
    if key in cache:
        return cache[key]
    stat = candidate.stat()
    if stat.st_size != expected_size or stat.st_mtime_ns != expected_mtime:
        raise RenderMediaError("source_metadata_mismatch", f"{candidate.name}: size or modification time changed")
    if not expected_fingerprint or fingerprint(candidate) != expected_fingerprint:
        raise RenderMediaError("source_fingerprint_mismatch", f"{candidate.name}: scanner content fingerprint does not match")
    cache[key] = candidate
    return candidate


def probe_media(path: str | Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [
                "ffprobe", "-v", "error", "-print_format", "json",
                "-show_streams", "-show_format", str(path),
            ],
            capture_output=True, text=True, timeout=60, check=True,
        )
        payload = json.loads(completed.stdout)
        streams = payload.get("streams") or []
        video = next((row for row in streams if row.get("codec_type") == "video"), None)
        if not video:
            raise RenderMediaError("video_stream_missing", f"No video stream in {Path(path).name}")
        duration = float((payload.get("format") or {}).get("duration") or video.get("duration") or 0)
        if duration <= 0:
            raise RenderMediaError("duration_invalid", f"Invalid duration for {Path(path).name}")
        return {
            "duration": duration,
            "width": int(video.get("width") or 0),
            "height": int(video.get("height") or 0),
            "has_video": True,
            "has_audio": any(row.get("codec_type") == "audio" for row in streams),
            "format_name": str((payload.get("format") or {}).get("format_name") or ""),
            "video_duration": float(video.get("duration") or 0),
            "audio_duration": float(next(
                (row.get("duration") or 0 for row in streams if row.get("codec_type") == "audio"), 0,
            )),
            "video_start_time": float(video.get("start_time") or 0),
            "audio_start_time": float(next(
                (row.get("start_time") or 0 for row in streams if row.get("codec_type") == "audio"), 0,
            )),
        }
    except RenderMediaError:
        raise
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RenderMediaError("ffprobe_failed", f"ffprobe failed for {Path(path).name}: {exc}") from exc


class FFmpegPipeline:
    def __init__(
        self,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        probe: Callable[[str | Path], dict[str, Any]] = probe_media,
    ):
        self.runner = runner
        self.probe = probe

    def render(
        self,
        composition: dict[str, Any],
        work_dir: Path,
        final_path: Path,
        verified_sources: dict[int, Path],
        *,
        source_probe_cache: MutableMapping[tuple[str, int, int], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        items = sorted(composition["items"], key=lambda row: int(row["position"]))
        if not items:
            raise RenderMediaError("empty_composition", "Approved composition contains no items")
        expected_duration = sum(float(row["end_seconds"]) - float(row["start_seconds"]) for row in items)
        if any(float(row["start_seconds"]) < 0 or float(row["end_seconds"]) <= float(row["start_seconds"]) for row in items):
            raise RenderMediaError("invalid_range", "Approved composition contains an invalid source range")

        work_dir.mkdir(parents=True, exist_ok=True)
        probe_cache = source_probe_cache if source_probe_cache is not None else {}
        source_probes: dict[int, dict[str, Any]] = {}
        source_probe_seconds = 0.0
        for position, path in verified_sources.items():
            stat = path.stat()
            key = (str(path.resolve()).casefold(), stat.st_size, stat.st_mtime_ns)
            info = probe_cache.get(key)
            if info is None:
                probe_started = time.perf_counter()
                info = self.probe(path)
                source_probe_seconds += time.perf_counter() - probe_started
                probe_cache[key] = info
            source_probes[position] = info
        reference = source_probes[int(items[0]["position"])]
        target_width = int(reference["width"]) // 2 * 2
        target_height = int(reference["height"]) // 2 * 2
        if target_width <= 0 or target_height <= 0:
            raise RenderMediaError("source_geometry_invalid", "Reference source has invalid video geometry")

        geometry_changed = any(
            int(info["width"]) != target_width or int(info["height"]) != target_height
            for info in source_probes.values()
        )
        silent_positions = sorted(position for position, info in source_probes.items() if not info["has_audio"])
        if silent_positions:
            raise RenderMediaError(
                "source_audio_missing",
                f"Source audio is missing for manifest positions {silent_positions}; silence insertion is disabled",
            )
        normalization = {
            "target_width": target_width,
            "target_height": target_height,
            "target_fps": OUTPUT_FPS,
            "sample_rate": OUTPUT_SAMPLE_RATE,
            "pixel_format": OUTPUT_PIXEL_FORMAT,
            "geometry_normalized": geometry_changed,
            "silence_inserted_positions": [],
        }

        extraction_started = time.perf_counter()
        intermediates: list[Path] = []
        segment_diagnostics: list[dict[str, Any]] = []
        for item in items:
            position = int(item["position"])
            source = verified_sources[position]
            duration = float(item["end_seconds"]) - float(item["start_seconds"])
            intermediate = work_dir / f"segment_{position:03d}.mp4"
            diagnostic = self._extract(
                source, intermediate, float(item["start_seconds"]), duration,
                target_width, target_height,
                source_info=source_probes[position],
            )
            diagnostic.update({
                "composition_id": str(composition["composition_id"]),
                "position": position,
                "source_filename": str(item.get("source_filename") or source.name),
                "source_id": str(item.get("source_id") or ""),
                "requested_start": float(item["start_seconds"]),
                "requested_end": float(item["end_seconds"]),
                "requested_duration": duration,
                "source_duration": float(source_probes[position]["duration"]),
                "geometry_normalization_required": (
                    int(source_probes[position]["width"]) != target_width
                    or int(source_probes[position]["height"]) != target_height
                ),
            })
            segment_diagnostics.append(diagnostic)
            intermediates.append(intermediate)
        extraction_seconds = time.perf_counter() - extraction_started

        concat_started = time.perf_counter()
        concat_file = work_dir / "concat.txt"
        concat_file.write_text(
            "".join(f"file '{str(path.resolve()).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n" for path in intermediates),
            encoding="utf-8",
        )
        temporary = final_path.with_suffix(".partial.mp4")
        temporary.unlink(missing_ok=True)
        self._run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_file),
            "-c", "copy", "-movflags", "+faststart", "-avoid_negative_ts", "make_zero",
            str(temporary),
        ], timeout=max(300.0, expected_duration * 3.0))
        validation = self.validate_output(temporary, expected_duration, len(items))
        final_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, final_path)
        concat_seconds = time.perf_counter() - concat_started
        return {
            "rendered_duration": validation["duration"],
            "duration_delta": validation["duration"] - expected_duration,
            "extraction_seconds": extraction_seconds,
            "concat_encode_seconds": concat_seconds,
            "normalization": normalization,
            "diagnostics": {
                "source_probe_seconds": source_probe_seconds,
                "segments": segment_diagnostics,
            },
        }

    def _extract(
        self,
        source: Path,
        output: Path,
        start: float,
        duration: float,
        width: int,
        height: int,
        *,
        source_info: dict[str, Any],
    ) -> dict[str, Any]:
        extraction_started = time.perf_counter()
        fast_seconds = 0.0
        fallback_seconds = 0.0
        validation_seconds = 0.0
        fallback_reason: str | None = None
        strategy = FAST_SEEK_STRATEGY

        fast_started = time.perf_counter()
        try:
            self._run(
                self._extract_command(source, output, start, duration, width, height, fast=True),
                timeout=max(300.0, duration * 10.0),
            )
            fast_seconds = time.perf_counter() - fast_started
            validation_started = time.perf_counter()
            try:
                output_info = self.validate_segment(output, duration)
            finally:
                validation_seconds += time.perf_counter() - validation_started
        except RenderMediaError as exc:
            fast_seconds = time.perf_counter() - fast_started
            fallback_reason = exc.code
            output.unlink(missing_ok=True)
            strategy = FALLBACK_SEEK_STRATEGY
            fallback_started = time.perf_counter()
            self._run(
                self._extract_command(source, output, start, duration, width, height, fast=False),
                timeout=max(300.0, duration * 10.0),
            )
            fallback_seconds = time.perf_counter() - fallback_started
            validation_started = time.perf_counter()
            try:
                output_info = self.validate_segment(output, duration)
            finally:
                validation_seconds += time.perf_counter() - validation_started

        coarse_start = max(0.0, float(math.floor(start - FAST_SEEK_PREROLL_SECONDS)))

        return {
            "extraction_elapsed_seconds": time.perf_counter() - extraction_started,
            "intermediate_duration": float(output_info["duration"]),
            "strategy": strategy,
            "fast_path_used": strategy == FAST_SEEK_STRATEGY,
            "fallback_used": strategy == FALLBACK_SEEK_STRATEGY,
            "fallback_reason": fallback_reason,
            "fast_attempt_seconds": fast_seconds,
            "fallback_extraction_seconds": fallback_seconds,
            "validation_elapsed_seconds": validation_seconds,
            "source_video_duration": float(source_info.get("video_duration") or source_info["duration"]),
            "coarse_seek_start": coarse_start,
            "local_trim_seconds": start - coarse_start,
        }

    def _extract_command(
        self,
        source: Path,
        output: Path,
        start: float,
        duration: float,
        width: int,
        height: int,
        *,
        fast: bool,
    ) -> list[str]:
        video_filter = (
            f"fps={OUTPUT_FPS},scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format={OUTPUT_PIXEL_FORMAT},setpts=PTS-STARTPTS"
        )
        audio_filter = (
            f"aresample={OUTPUT_SAMPLE_RATE}:first_pts=0,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,asetpts=PTS-STARTPTS"
        )
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        if fast:
            # Whole-second anchoring preserves the v1 fps/aresample filter phase.
            coarse_start = max(0.0, float(math.floor(start - FAST_SEEK_PREROLL_SECONDS)))
            command.extend(["-ss", f"{coarse_start:.9f}"])
        command.extend(["-i", str(source)])
        local_start = start - coarse_start if fast else start
        command.extend(["-ss", f"{local_start:.9f}"])
        command.extend([
            "-t", f"{duration:.9f}",
            "-filter_complex", f"[0:v:0]{video_filter}[v];[0:a:0]{audio_filter}[a]",
            "-map", "[v]", "-map", "[a]",
            "-c:v", OUTPUT_VIDEO_CODEC, "-preset", "fast", "-crf", "20",
            "-pix_fmt", OUTPUT_PIXEL_FORMAT, "-fps_mode", "cfr", "-g", str(OUTPUT_FPS * 2),
            "-c:a", OUTPUT_AUDIO_CODEC, "-b:a", "192k", "-ar", str(OUTPUT_SAMPLE_RATE), "-ac", "2",
            "-movflags", "+faststart", "-shortest", str(output),
        ])
        return command

    def validate_segment(self, path: Path, expected_duration: float) -> dict[str, Any]:
        if not path.exists() or path.stat().st_size <= 0:
            raise RenderMediaError("output_missing", "Extracted segment is missing or empty")
        info = self.probe(path)
        if not info.get("has_video"):
            raise RenderMediaError("video_stream_missing", "Extracted segment has no video stream")
        if not info.get("has_audio"):
            raise RenderMediaError("audio_stream_missing", "Extracted segment has no audio stream")
        delta = float(info["duration"]) - expected_duration
        if abs(delta) > SEGMENT_DURATION_TOLERANCE:
            raise RenderMediaError(
                "segment_duration_mismatch",
                f"Extracted segment duration differs by {delta:.3f}s",
            )
        video_duration = float(info.get("video_duration") or 0)
        audio_duration = float(info.get("audio_duration") or 0)
        if video_duration > 0 and audio_duration > 0 and abs(video_duration - audio_duration) > SEGMENT_DURATION_TOLERANCE:
            raise RenderMediaError("segment_av_desync", "Extracted segment audio/video durations diverge")
        return info

    def validate_output(self, path: Path, expected_duration: float, item_count: int) -> dict[str, Any]:
        if not path.exists() or path.stat().st_size <= 0:
            raise RenderMediaError("output_missing", "Rendered output is missing or empty")
        info = self.probe(path)
        if not info["has_video"]:
            raise RenderMediaError("video_stream_missing", "Rendered output has no video stream")
        if not info["has_audio"]:
            raise RenderMediaError("audio_stream_missing", "Rendered output has no audio stream")
        tolerance = max(0.5, item_count * 2.0 / OUTPUT_FPS + 0.1)
        delta = float(info["duration"]) - expected_duration
        if abs(delta) > tolerance:
            raise RenderMediaError(
                "duration_mismatch",
                f"Rendered duration differs by {delta:.3f}s (tolerance {tolerance:.3f}s)",
            )
        if "mp4" not in info["format_name"] and "mov" not in info["format_name"]:
            raise RenderMediaError("output_format_invalid", "Rendered output is not a seekable MP4 container")
        return info

    def _run(self, command: list[str], *, timeout: float) -> None:
        try:
            completed = self.runner(command, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RenderMediaError("ffmpeg_failed", f"FFmpeg execution failed: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "unknown FFmpeg error").strip()
            raise RenderMediaError("ffmpeg_failed", detail[-2000:])
